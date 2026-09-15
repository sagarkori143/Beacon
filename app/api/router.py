"""API router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, chat, documents, health, ingestion, organizations, search


def build_api_router(prefix: str) -> APIRouter:
    """Versioned routes, mounted under the configured prefix."""
    router = APIRouter(prefix=prefix)
    router.include_router(auth.router)
    router.include_router(organizations.router)
    router.include_router(documents.router)
    router.include_router(ingestion.router)
    router.include_router(search.router)
    router.include_router(chat.router)
    return router


#: Health lives at the root, unversioned: probes should not have to know the API
#: version, and the path must stay stable across version bumps.
health_router = health.router
