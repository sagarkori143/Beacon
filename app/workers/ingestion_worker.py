"""The ingestion worker.

Consumes queue messages and runs the pipeline. The parts that matter for
correctness are all about what happens when things go wrong:

* **Stale messages are reclaimed first**, before new ones are read. A worker
  that died mid-pipeline left its message in the consumer group's pending list;
  reclaiming it is what makes the crash recoverable rather than a lost document.
* **Retryable and terminal failures are distinguished.** A model server that is
  down is retried; a PDF that cannot be parsed is dead-lettered immediately,
  because retrying it only delays the operator finding out.
* **The message is acknowledged only after the job reaches a terminal state.**
  Acking on receipt would lose work on every crash.

Workers are stateless and horizontally scalable: the consumer group hands each
message to exactly one of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import Settings
from app.core.db import UnitOfWork
from app.core.logging import bind_contextvars, clear_contextvars, get_logger
from app.core.tenancy import TenantContext
from app.core.tracing import TraceContext
from app.providers.progress.base import ProgressPublisher
from app.providers.queue.base import DeliveredMessage, QueueProvider
from app.providers.registry import ProviderBundle
from app.services.ingestion.pipeline import IngestionPipeline, is_retryable

log = get_logger(__name__)


def worker_identity() -> str:
    """Stable-ish per-process identity used as the queue consumer name."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


@dataclass(slots=True)
class WorkerStats:
    processed: int = 0
    failed: int = 0
    dead_lettered: int = 0
    reclaimed: int = 0
    started_at: float = 0.0

    def snapshot(self) -> dict[str, float | int]:
        return {
            "processed": self.processed,
            "failed": self.failed,
            "dead_lettered": self.dead_lettered,
            "reclaimed": self.reclaimed,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
        }


class IngestionWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        providers: ProviderBundle,
        queue: QueueProvider,
        consumer: str | None = None,
        uow_factory: Callable[[TenantContext], UnitOfWork] | None = None,
        progress: ProgressPublisher | None = None,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.queue = queue
        self.consumer = consumer or worker_identity()
        # Injected so a test -- or a process wanting its own engine -- is not
        # forced through the module-global one.
        self._uow_factory = uow_factory or (lambda tenant: UnitOfWork(tenant, self.settings))
        self.pipeline = IngestionPipeline(settings=settings, providers=providers, progress=progress)
        self.stats = WorkerStats(started_at=time.monotonic())
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        """Finish the message in flight, then exit."""
        if not self._stopping.is_set():
            log.info("worker_stopping", consumer=self.consumer)
            self._stopping.set()

    async def run(self, *, max_messages: int | None = None) -> WorkerStats:
        """Main loop. ``max_messages`` bounds it for tests."""
        await self.queue.setup()
        log.info("worker_started", consumer=self.consumer, queue=self.queue.name)

        handled = 0
        while not self._stopping.is_set():
            if max_messages is not None and handled >= max_messages:
                break

            delivered = await self._next_messages()
            if not delivered:
                continue

            for message in delivered:
                if self._stopping.is_set():
                    # Leave it pending rather than half-processing it; another
                    # worker reclaims it after the visibility timeout.
                    await self.queue.nack(message)
                    break
                await self.process(message)
                handled += 1

        log.info("worker_stopped", consumer=self.consumer, **self.stats.snapshot())
        return self.stats

    async def _next_messages(self) -> list[DeliveredMessage]:
        """Reclaim abandoned work first, then take new work.

        Ordering matters: under sustained load, always preferring new messages
        would let a message abandoned by a crashed worker sit in the pending
        list indefinitely.
        """
        reclaimed = await self.queue.claim_stale(
            consumer=self.consumer,
            min_idle_ms=self.settings.queue.visibility_timeout_ms,
            count=self.settings.queue.batch_size,
        )
        if reclaimed:
            self.stats.reclaimed += len(reclaimed)
            return reclaimed

        return await self.queue.consume(
            consumer=self.consumer,
            count=self.settings.queue.batch_size,
            block_ms=self.settings.queue.block_ms,
        )

    async def process(self, delivered: DeliveredMessage) -> None:
        """Run one job to a terminal state, then decide the message's fate."""
        message = delivered.message
        trace = TraceContext.new(organization_id=message.organization_id, job_id=message.job_id)
        bind_contextvars(
            job_id=str(message.job_id),
            organization_id=str(message.organization_id),
            trace_id=trace.trace_id,
            attempt=delivered.delivery_count,
        )

        tenant = TenantContext(organization_id=message.organization_id)
        uow = self._uow_factory(tenant)
        started = time.perf_counter()

        try:
            await self.pipeline.run(
                uow,
                message.job_id,
                tenant=tenant,
                trace=trace,
                worker_id=self.consumer,
            )
            await self.queue.ack(delivered)
            self.stats.processed += 1
            log.info(
                "job_processed",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        except asyncio.CancelledError:
            await self.queue.nack(delivered)
            raise
        except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
            self.stats.failed += 1
            await self._handle_failure(delivered, exc)
        finally:
            clear_contextvars()

    async def _handle_failure(self, delivered: DeliveredMessage, exc: Exception) -> None:
        """Retry, or give up and dead-letter."""
        retryable = is_retryable(exc)
        attempts = delivered.delivery_count
        reason = f"{type(exc).__name__}: {exc}"

        if not retryable:
            # A document that failed to parse will fail identically next time.
            await self.queue.dead_letter(delivered, reason)
            self.stats.dead_lettered += 1
            log.error("job_failed_terminal", error=reason[:300])
            return

        if attempts >= self.settings.queue.max_attempts:
            await self.queue.dead_letter(delivered, f"exhausted {attempts} attempts: {reason}")
            self.stats.dead_lettered += 1
            log.error("job_failed_exhausted", attempts=attempts, error=reason[:300])
            return

        await self.queue.nack(delivered)
        log.warning(
            "job_failed_retrying",
            attempt=attempts,
            max_attempts=self.settings.queue.max_attempts,
            error=reason[:300],
        )


def install_signal_handlers(worker: IngestionWorker) -> None:
    """Stop cleanly on SIGTERM/SIGINT so an in-flight job finishes.

    A container orchestrator sends SIGTERM before it kills a pod. Without this,
    every deploy abandons whatever was mid-pipeline and relies on the reclaim
    path to pick it up minutes later.
    """
    loop = asyncio.get_running_loop()
    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            # Windows' proactor loop does not implement add_signal_handler.
            loop.add_signal_handler(sig, worker.request_stop)
