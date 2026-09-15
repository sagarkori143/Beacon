"""Health and readiness.

Two endpoints, because they answer different questions and a load balancer needs
them separated:

``/health`` is liveness -- can this process serve requests at all? It checks the
database and Redis, and nothing else.

``/health/ready`` additionally probes the model server and every configured
provider. **It is deliberately not what a load balancer should gate on.** An
unreachable GPU box degrades chat; document management, search over
already-indexed data and everything else still work, and pulling the instance
out of rotation for it would turn a partial outage into a total one.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.api.deps import AppSettings, Providers
from app.core.db import check_application_role, check_database
from app.core.redis import check_redis
from app.schemas.common import HealthStatus

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthStatus)
async def health(response: Response, settings: AppSettings) -> HealthStatus:
    """Liveness: the datastores this process cannot work without."""
    database = await check_database(settings)
    redis = await check_redis()

    ok = database["ok"] and redis["ok"]
    if not ok:
        response.status_code = 503
    return HealthStatus(
        status="ok" if ok else "degraded",
        checks={"database": database, "redis": redis},
    )


@router.get("/health/ready", response_model=HealthStatus)
async def readiness(
    response: Response, settings: AppSettings, providers: Providers
) -> HealthStatus:
    """Readiness: everything, including the model server.

    Reports ``degraded`` rather than failing when only providers are unhealthy,
    so this can be scraped for alerting without being mistaken for liveness.
    """
    database = await check_database(settings)
    redis = await check_redis()
    role = await check_application_role(settings)
    provider_checks = await providers.health()

    core_ok = database["ok"] and redis["ok"] and role["ok"]
    providers_ok = all(
        check.get("ok", False)
        for key, check in provider_checks.items()
        if isinstance(check, dict) and key != "skipped"
    )

    if not core_ok:
        response.status_code = 503

    return HealthStatus(
        status="ok" if core_ok and providers_ok else "degraded",
        checks={
            "database": database,
            "redis": redis,
            "application_role": role,
            **provider_checks,
        },
    )


@router.get("/health/models")
async def model_catalog(providers: Providers) -> dict:
    """Every model the router can currently choose from, with its capabilities.

    The quickest way to answer "why did it route to that model?" and "is my new
    provider actually registered?".
    """
    catalog = await providers.catalog()
    return {
        "providers": sorted(providers.llm),
        "skipped": providers.skipped,
        "models": [
            {
                "provider": info.provider,
                "model": info.name,
                "tier": info.tier.value,
                "privacy": info.privacy.value,
                "context_window": info.context_window,
                "supports_tools": info.supports_tools,
                "supports_json_schema": info.supports_json_schema,
                "cost_per_1m_input": info.cost_per_1m_input,
                "cost_per_1m_output": info.cost_per_1m_output,
            }
            for info in catalog
        ],
    }
