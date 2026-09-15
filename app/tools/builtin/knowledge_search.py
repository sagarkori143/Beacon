"""Search the tenant's knowledge base.

The agent normally retrieves before generating, but this tool lets the model go
back for more when the first pass missed something -- a follow-up question, or a
second subject the original query did not cover.

It reuses the same retriever as the main path, so the tenant filters, the
hierarchy merge and the location-override rules are identical. There is no
second retrieval code path that could drift.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.tools.base import Tool, ToolContext, ToolDefinition, ToolResult

log = get_logger(__name__)


class KnowledgeSearchArgs(BaseModel):
    query: str = Field(
        description="What to look for, in the vocabulary a policy document would use.",
        min_length=2,
        max_length=500,
    )
    top_k: int = Field(default=5, ge=1, le=15, description="How many passages to return.")


class KnowledgeSearchTool(Tool):
    definition = ToolDefinition(
        name="knowledge_search",
        description=(
            "Search this organization's documents (policies, procedures, hours, "
            "fees, facilities). Returns passages with source references. Use when "
            "the answer should come from official documentation."
        ),
        input_model=KnowledgeSearchArgs,
        scopes=frozenset({"knowledge:read"}),
        timeout_s=20.0,
    )

    async def execute(self, args: KnowledgeSearchArgs, context: ToolContext) -> ToolResult:
        retriever = context.extra.get("retriever")
        if retriever is None:
            return ToolResult.failure("unavailable", "Knowledge search is not configured.")

        outcome = await retriever.retrieve(
            context.uow,
            context.tenant,
            queries=[args.query],
            top_k=args.top_k,
            trace=context.trace,
        )
        if not outcome.hits:
            return ToolResult.success(
                f"No passages found for '{args.query}'.",
                data={"query": args.query, "hits": 0},
            )

        lines = []
        citations = []
        for index, hit in enumerate(outcome.hits, start=1):
            scope = "location-specific" if hit.location_id else "organization-wide"
            lines.append(
                f"[{index}] ({scope}) {hit.source_name}"
                f"{f' - {hit.breadcrumb}' if hit.breadcrumb else ''}\n{hit.content}"
            )
            citations.append(
                {
                    "chunk_id": str(hit.chunk_id),
                    "document_id": str(hit.document_id),
                    "source": hit.source_name,
                    "scope": hit.scope.value,
                    "score": round(hit.score, 4),
                }
            )

        return ToolResult.success(
            "\n\n".join(lines),
            data={
                "query": args.query,
                "hits": len(outcome.hits),
                "suppressed_by_override": outcome.merged.suppressed_count,
            },
            citations=tuple(citations),
        )


TOOL = KnowledgeSearchTool()
