"""Audit log writes.

Records that something happened and to what -- never the content involved. No
prompts, no document text, no credentials ever reach this table.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AuditAction
from app.core.tenancy import TenantContext
from app.models.audit import AuditLog


async def record(
    session: AsyncSession,
    *,
    organization_id: UUID,
    action: AuditAction,
    outcome: str = "SUCCESS",
    actor_user_id: UUID | None = None,
    location_id: UUID | None = None,
    resource_type: str | None = None,
    resource_id: UUID | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    message: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        organization_id=organization_id,
        location_id=location_id,
        actor_user_id=actor_user_id,
        action=action,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        trace_id=trace_id,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:256] or None,
        message=message[:1000] if message else None,
        detail=detail or {},
    )
    session.add(entry)
    await session.flush()
    return entry


async def list_entries(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    action: AuditAction | None = None,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.organization_id == tenant.organization_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    result = await session.execute(
        stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    )
    return result.scalars().all()
