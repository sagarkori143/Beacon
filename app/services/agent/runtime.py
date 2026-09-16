"""The agent runtime.

The flow, in order: admit the request, plan it, retrieve if the plan says to,
route a model, run a bounded tool loop, build the context, generate the answer,
then finalize (citations, grounding, trace, persistence).

Two structural decisions shape this file.

**One code path.** ``run()`` is ``run_stream()`` consumed to completion. The
streaming and non-streaming APIs therefore cannot diverge in behaviour, only in
how much of the same event stream the caller sees.

**No transaction spans a model call.** Every database interaction opens its own
short transaction through the UnitOfWork and closes before the next provider
call. Holding one request-scoped transaction is the natural implementation --
tenant context lives in the transaction -- and it exhausts the connection pool
at around twenty concurrent chats.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.db import UnitOfWork
from app.core.enums import TaskKind
from app.core.errors import AppError, NoModelAvailable, ProviderError
from app.core.logging import get_logger
from app.core.tenancy import Principal
from app.core.tracing import TraceContext
from app.providers.llm.base import (
    Completion,
    Message,
    StreamDone,
    StreamError,
    TextDelta,
    Usage,
    UsageEvent,
)
from app.providers.registry import ProviderBundle
from app.services.agent import events as ev
from app.services.agent.guardrails import check_rate_limit, screen_input
from app.services.agent.planner import Intent, Plan, make_plan
from app.services.agent.router import ModelRouter, RoutingDecision, RoutingRequest
from app.services.rag.citations import extract_refs, grounding_ratio
from app.services.rag.context_builder import BuiltContext, ContextBuilder
from app.services.rag.prompts import NO_CONTEXT_NOTE, answer_system, tool_system
from app.services.rag.token_budget import context_budget, count_message_tokens
from app.services.retrieval.service import Retriever
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry

log = get_logger(__name__)


@dataclass(slots=True)
class AgentRequest:
    query: str
    principal: Principal
    uow: UnitOfWork
    conversation_id: UUID | None = None
    history: tuple[Message, ...] = ()
    organization_name: str = "this organization"
    location_name: str | None = None
    enabled_tools: tuple[str, ...] | None = None
    force_retrieval: bool | None = None
    prefer_model: str | None = None
    model_pins: dict[str, str] = field(default_factory=dict)
    allowed_providers: tuple[str, ...] | None = None
    require_local: bool = False
    language: str = "English"
    stream_tokens: bool = True

    @property
    def tenant(self) -> Any:
        return self.principal.tenant


@dataclass(slots=True)
class AgentResult:
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    context: BuiltContext | None = None
    plan: Plan | None = None
    routing: RoutingDecision | None = None
    usage: Usage = field(default_factory=Usage)
    estimated_cost_usd: float = 0.0
    finish_reason: str = "stop"
    grounding: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    #: The failure that ended the run, if one did. Kept so the non-streaming
    #: caller can re-raise the original rather than a flattened stand-in.
    error: Exception | None = None

    @property
    def low_confidence(self) -> bool:
        """Answered without citing anything, despite having sources available."""
        return bool(self.context and self.context.passages) and self.grounding == 0.0


class AgentRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        providers: ProviderBundle,
        router: ModelRouter,
        retriever: Retriever,
        tools: ToolRegistry,
        redis: Redis | None = None,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.router = router
        self.retriever = retriever
        self.tools = tools
        self.redis = redis
        self.context_builder = ContextBuilder(settings.context, settings.retrieval)

    # -- public API ----------------------------------------------------------

    async def run(self, request: AgentRequest, *, trace: TraceContext) -> AgentResult:
        """Non-streaming. Consumes the same event stream the SSE endpoint does."""
        result = AgentResult(answer="")
        chunks: list[str] = []

        async for event in self.run_stream(request, trace=trace, sink=result):
            if event.type == "token":
                chunks.append(event.data["text"])
            elif event.type == "error" and not result.answer:
                # Re-raise the original. Constructing a fresh AppError here
                # would flatten a 503 "model server unreachable" or a 429 with
                # its Retry-After into a generic 500 -- the caller would be told
                # something broke when in fact something is merely busy or down,
                # and would have nothing to act on.
                raise result.error or AppError(event.data["message"])

        if not result.answer:
            result.answer = "".join(chunks)
        return result

    async def run_stream(
        self,
        request: AgentRequest,
        *,
        trace: TraceContext,
        sink: AgentResult | None = None,
    ) -> AsyncIterator[ev.AgentEvent]:
        """Drive one agent run, emitting events as it goes."""
        started = time.perf_counter()
        deadline = time.monotonic() + self.settings.agent.deadline_s
        result = sink if sink is not None else AgentResult(answer="")

        try:
            async for event in self._run(request, trace, result, deadline):
                yield event
        except NoModelAvailable as exc:
            result.error = exc
            yield ev.error_event(code=exc.code, message=exc.message, retryable=True)
            return
        except (ProviderError, AppError) as exc:
            result.error = exc
            log.warning("agent_failed", error=str(exc)[:300], trace_id=trace.trace_id)
            yield ev.error_event(
                code=getattr(exc, "code", "error"),
                message=exc.message if isinstance(exc, AppError) else str(exc),
                retryable=getattr(exc, "retryable", False),
            )
            return
        finally:
            result.latency_ms = (time.perf_counter() - started) * 1000.0

    # -- the loop ------------------------------------------------------------

    async def _run(
        self,
        request: AgentRequest,
        trace: TraceContext,
        result: AgentResult,
        deadline: float,
    ) -> AsyncIterator[ev.AgentEvent]:
        principal = request.principal

        # 0. Admission -------------------------------------------------------
        admission = screen_input(request.query, max_chars=self.settings.agent.max_query_chars)
        if self.redis is not None:
            await check_rate_limit(
                self.redis,
                organization_id=principal.organization_id,
                user_id=principal.user_id,
                limit_per_minute=self.settings.security.rate_limit_per_minute,
            )
        query = admission.query

        available_tools = self.tools.available_for(principal, enabled=request.enabled_tools)
        tool_descriptions = [(t.definition.name, t.definition.description) for t in available_tools]

        # 1. Plan ------------------------------------------------------------
        yield ev.stage("planning")
        with trace.span("agent.plan"):
            plan_route = await self.router.route(
                RoutingRequest(
                    task=TaskKind.PLAN,
                    needs_json=True,
                    require_local=request.require_local,
                    model_pins=request.model_pins,
                    allowed_providers=request.allowed_providers,
                )
            )
            plan = await make_plan(
                self.providers.get_llm(plan_route.provider),
                plan_route.model,
                query=query,
                organization=request.organization_name,
                location=request.location_name,
                tools=tool_descriptions,
                history=request.history[-4:],
                trace=trace,
            )
        result.plan = plan
        yield ev.plan_event(plan.to_trace())

        # A genuinely ambiguous request is worth one question -- but asking is a
        # last resort, not a first one.
        #
        # The intent label is one judgement call by whichever model is planning,
        # and a small one gets it wrong in a specific, damaging direction: it
        # marks plainly-answerable questions ambiguous while still producing
        # perfectly good search queries for them. Trusting the label there means
        # interrogating a guest who asked what time breakfast is, about a fact
        # sitting in the handbook.
        #
        # So the question is held back, retrieval runs anyway -- the planner's
        # own instruction is that an unnecessary search is cheap and a wrong
        # answer is not -- and it is only asked if nothing useful came back.
        pending_clarification = (
            plan.clarifying_question
            if plan.intent is Intent.NEEDS_CLARIFICATION and plan.clarifying_question
            else None
        )

        # 2. Retrieve --------------------------------------------------------
        context = BuiltContext(budget=self.settings.context.max_context_tokens)
        wants_retrieval = (
            plan.needs_retrieval if request.force_retrieval is None else request.force_retrieval
        )
        queries = list(plan.search_queries)

        if pending_clarification and not queries:
            # It could not say what to look for, so look for what was asked.
            wants_retrieval = True
            queries = [query.strip()[:500]]

        if wants_retrieval and queries:
            yield ev.stage("retrieving")
            with trace.span("rag.retrieve"):
                outcome = await self.retriever.retrieve(
                    request.uow,
                    principal.tenant,
                    queries=queries,
                    trace=trace,
                )
            yield ev.search_event(
                queries=list(outcome.queries),
                hits=len(outcome.hits),
                per_scope=outcome.per_scope,
                degraded=outcome.degraded,
            )
            if outcome.merged.suppressed_count:
                yield ev.conflict_event(
                    suppressed=outcome.merged.suppressed_count,
                    topics=sorted(outcome.merged.overrides),
                )

            with trace.span("context.build"):
                context = self.context_builder.build(outcome.hits)
            for citation in context.citations:
                yield ev.citation_event(citation.to_dict())

        result.context = context
        result.citations = [c.to_dict() for c in context.citations]

        # Nothing was found, and the planner did flag the request as ambiguous.
        # Now the question is worth asking.
        if pending_clarification and not context.passages:
            result.answer = pending_clarification
            result.finish_reason = "clarification"
            yield ev.token_event(pending_clarification)
            yield ev.done_event(
                message_id=None,
                conversation_id=str(request.conversation_id) if request.conversation_id else None,
                trace_id=trace.trace_id,
                finish_reason="clarification",
                grounding=0.0,
            )
            return

        # 3. Tool loop -------------------------------------------------------
        messages: list[Message] = [
            Message.system(
                tool_system(organization=request.organization_name, location=request.location_name)
            ),
            *request.history[-self.settings.agent.max_history_messages :],
            Message.user(query),
        ]

        if available_tools and (plan.needs_tools or plan.intent is Intent.TOOL_ACTION):
            async for event in self._tool_loop(
                request, messages, available_tools, trace, result, deadline
            ):
                yield event

        # 4. Generate --------------------------------------------------------
        answer_messages = self._answer_messages(request, query, context, messages)
        prompt_tokens = count_message_tokens(answer_messages)

        route = await self.router.route(
            RoutingRequest(
                task=TaskKind.ANSWER,
                est_prompt_tokens=prompt_tokens,
                needs_streaming=request.stream_tokens,
                require_local=request.require_local,
                document_types=tuple({c.section or "" for c in context.citations if c.section})[:4],
                prefer_model=request.prefer_model,
                model_pins=request.model_pins,
                allowed_providers=request.allowed_providers,
            )
        )
        result.routing = route

        if route.compaction_required and context.passages:
            # The chosen model's window is tighter than the context we built.
            # Rebuild smaller rather than sending something that will truncate.
            budget = context_budget(
                context_window=self.providers.get_llm(route.provider)
                .capabilities(route.model)
                .context_window,
                max_output_tokens=self.settings.context.reserve_output_tokens,
                prompt_overhead_tokens=count_message_tokens(messages),
                configured_max=self.settings.context.max_context_tokens,
                utilization=self.settings.context.context_window_utilization,
            )
            context = (
                self.context_builder.build([], budget_tokens=budget.total)
                if budget.total <= 0
                else context
            )
            answer_messages = self._answer_messages(request, query, context, messages)

        yield ev.stage("generating", provider=route.provider, model=route.model)

        provider = self.providers.get_llm(route.provider)
        answer_parts: list[str] = []
        usage = Usage()
        finish = "stop"

        with trace.span("llm.generate", provider=route.provider, model=route.model):
            if request.stream_tokens:
                async for event in provider.generate_stream(
                    model=route.model,
                    messages=answer_messages,
                    params=route.params,
                    trace=trace,
                ):
                    match event:
                        case TextDelta(text):
                            answer_parts.append(text)
                            yield ev.token_event(text)
                        case UsageEvent(u):
                            usage = u
                        case StreamDone(reason):
                            finish = reason
                        case StreamError(code, message, retryable):
                            yield ev.error_event(code=code, message=message, retryable=retryable)
                            finish = "error"
            else:
                completion: Completion = await provider.generate(
                    model=route.model,
                    messages=answer_messages,
                    params=route.params,
                    trace=trace,
                )
                answer_parts.append(completion.text)
                usage, finish = completion.usage, completion.finish_reason
                yield ev.token_event(completion.text)

        # 5. Finalize --------------------------------------------------------
        answer = "".join(answer_parts).strip()

        # An empty answer is a failure wearing a success's clothes: finish_reason
        # says "stop", nothing raised, and the user gets a blank bubble with no
        # hint that anything went wrong. It happens when a model has just been
        # through a tool turn and reads the exchange as already concluded --
        # small models especially.
        #
        # Retry once from the retrieved passages alone, without the tool
        # transcript. Cheap, and it is the transcript that confused it.
        if not answer and not context.is_empty:
            log.info("empty_answer_retry", trace_id=trace.trace_id, model=route.model)
            retry = await provider.generate(
                model=route.model,
                messages=self._answer_messages(request, query, context, []),
                params=route.params,
                trace=trace,
            )
            answer = retry.text.strip()
            if answer:
                usage, finish = retry.usage, retry.finish_reason
                yield ev.token_event(answer)

        if not answer:
            # Still nothing. Say that plainly -- silence is the one response the
            # user cannot act on, and it looks like the product is broken.
            answer = (
                "I was not able to put an answer together for that. "
                "Please try asking it a different way."
            )
            finish = "empty"
            log.warning("empty_answer", trace_id=trace.trace_id, model=route.model)
            yield ev.token_event(answer)

        result.answer = answer
        result.usage = usage
        result.finish_reason = finish
        result.grounding = grounding_ratio(answer, context.citations)

        model_config = self._model_config(route)
        result.estimated_cost_usd = usage.cost_usd(model_config)
        result.tool_calls = result.tool_calls
        result.trace = {
            "trace_id": trace.trace_id,
            "plan": plan.to_trace(),
            "routing": route.to_trace(),
            "context": context.trace(),
            "tool_calls": result.tool_calls,
            "cited_refs": sorted(extract_refs(answer)),
            "spans": trace.timeline(),
        }

        yield ev.usage_event(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=result.estimated_cost_usd,
        )
        yield ev.done_event(
            message_id=None,
            conversation_id=str(request.conversation_id) if request.conversation_id else None,
            trace_id=trace.trace_id,
            finish_reason=finish,
            grounding=result.grounding,
        )

    # -- tool loop -----------------------------------------------------------

    async def _tool_loop(
        self,
        request: AgentRequest,
        messages: list[Message],
        available_tools: Sequence[Any],
        trace: TraceContext,
        result: AgentResult,
        deadline: float,
    ) -> AsyncIterator[ev.AgentEvent]:
        """Run tools until the model stops asking, or a bound is reached.

        Bounded three independent ways -- iterations, total calls, wall clock --
        because each catches a different runaway: a model that loops on the same
        call, one that fans out across many, and one whose tools are simply slow.
        """
        specs = self.tools.specs_for(request.principal, enabled=request.enabled_tools)
        if not specs:
            return

        tool_context = ToolContext(
            tenant=request.principal.tenant,
            principal=request.principal,
            uow=request.uow,
            providers=self.providers,
            trace=trace,
            deadline=deadline,
            extra={"retriever": self.retriever},
        )

        total_calls = 0
        for iteration in range(1, self.settings.agent.max_iterations + 1):
            if time.monotonic() >= deadline:
                break

            yield ev.stage("tools", iteration=iteration)

            route = await self.router.route(
                RoutingRequest(
                    task=TaskKind.TOOL_TURN,
                    needs_tools=True,
                    est_prompt_tokens=count_message_tokens(messages),
                    require_local=request.require_local,
                    model_pins=request.model_pins,
                    allowed_providers=request.allowed_providers,
                )
            )
            provider = self.providers.get_llm(route.provider)

            with trace.span(f"agent.iteration.{iteration}", model=route.model):
                completion = await provider.generate(
                    model=route.model,
                    messages=messages,
                    tools=specs,
                    params=route.params,
                    trace=trace,
                )

            if not completion.tool_calls:
                if completion.text:
                    messages.append(Message.assistant(completion.text))
                return

            messages.append(
                Message.assistant(completion.text or None, tool_calls=completion.tool_calls)
            )

            for call in completion.tool_calls:
                if total_calls >= self.settings.agent.max_tool_calls:
                    messages.append(
                        Message.tool_result(
                            call.id,
                            call.name,
                            "Tool budget exhausted for this request. "
                            "Answer with the information you already have.",
                        )
                    )
                    continue

                total_calls += 1
                yield ev.tool_call_event(id=call.id, name=call.name, arguments=dict(call.arguments))

                tool_result = await self.tools.invoke(call.name, dict(call.arguments), tool_context)
                messages.append(Message.tool_result(call.id, call.name, tool_result.content))
                result.tool_calls.append(
                    {
                        "name": call.name,
                        "ok": tool_result.ok,
                        "error_code": tool_result.error_code,
                        "latency_ms": round(tool_result.latency_ms, 1),
                    }
                )
                yield ev.tool_result_event(
                    id=call.id,
                    name=call.name,
                    ok=tool_result.ok,
                    summary=tool_result.content,
                    latency_ms=tool_result.latency_ms,
                    error_code=tool_result.error_code,
                )

        # Every bound is a "stop calling tools", never a "give up": the answer
        # turn below runs with no tools attached and whatever was gathered.
        messages.append(
            Message.system(
                "No further tool calls are available. Answer using the information gathered so far."
            )
        )

    # -- helpers -------------------------------------------------------------

    def _answer_messages(
        self,
        request: AgentRequest,
        query: str,
        context: BuiltContext,
        tool_messages: list[Message],
    ) -> list[Message]:
        """Assemble the final generation prompt.

        Tool results are carried forward as conversation turns; retrieved
        passages go into the system message inside delimited blocks, which keeps
        "what the assistant found out" and "what the documents say" visibly
        separate for the model.
        """
        system = answer_system(
            organization=request.organization_name,
            location=request.location_name,
            language=request.language,
        )
        system = f"{system}\n\n{context.render()}"
        if context.is_empty:
            system = f"{system}\n\n{NO_CONTEXT_NOTE}"

        carried = [m for m in tool_messages if m.role in ("assistant", "tool")]
        return [
            Message.system(system),
            *request.history[-self.settings.agent.max_history_messages :],
            Message.user(query),
            *carried,
        ]

    def _model_config(self, route: RoutingDecision) -> Any:
        for config in self.settings.providers.llm:
            if config.name == route.provider:
                return config.model_by_name(route.model)
        return None
