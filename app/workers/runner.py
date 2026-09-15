"""Worker entry point.

Run with ``python -m app.workers.runner``. Same image as the API, different
command -- so there is one dependency set, one build, and no chance of the two
drifting apart.
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.db import dispose_engine, init_engine
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis, init_redis
from app.providers.registry import build_providers
from app.workers.ingestion_worker import IngestionWorker, install_signal_handlers

log = get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    init_engine(settings)
    redis = init_redis(settings)
    providers = build_providers(settings, redis=redis)

    worker = IngestionWorker(
        settings=settings,
        providers=providers,
        queue=providers.require_queue(),
    )
    install_signal_handlers(worker)

    try:
        await worker.run()
    finally:
        await providers.aclose()
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        log.info("worker_interrupted")
