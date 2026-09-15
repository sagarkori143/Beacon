"""Structured facts about the caller's location.

Address, phone number, front-desk hours, amenities -- the kind of thing that
belongs in a field rather than in a document. Answering these from a row is both
exact and free, where retrieval would be approximate and cost an embedding call.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.core.errors import NotFoundError
from app.repositories.organization import get_location, get_organization
from app.tools.base import Tool, ToolContext, ToolDefinition, ToolResult


class LocationInfoArgs(BaseModel):
    field: str | None = Field(
        default=None,
        description=(
            "A specific field to read, such as 'address', 'phone', "
            "'front_desk_hours' or 'amenities'. Omit to get everything known."
        ),
        max_length=64,
    )


class LocationInfoTool(Tool):
    definition = ToolDefinition(
        name="location_info",
        description=(
            "Get structured facts about this location: name, address, phone, "
            "timezone, and any configured details such as front desk hours or "
            "amenities. Use for contact and identity questions."
        ),
        input_model=LocationInfoArgs,
        timeout_s=10.0,
    )

    async def execute(self, args: LocationInfoArgs, context: ToolContext) -> ToolResult:
        async with context.uow.begin() as session:
            organization = await get_organization(session, context.tenant.organization_id)

            if context.tenant.location_id is None:
                return ToolResult.success(
                    f"Organization: {organization.name}. "
                    "This session is not scoped to a specific location, so no "
                    "location details are available.",
                    data={"organization": organization.name, "location": None},
                )

            try:
                location = await get_location(session, context.tenant, context.tenant.location_id)
            except NotFoundError:
                return ToolResult.failure("not_found", "This location no longer exists.")

            facts: dict[str, Any] = {
                "organization": organization.name,
                "location": location.name,
                "timezone": location.timezone,
                **(location.settings or {}),
            }

        if args.field:
            key = args.field.strip().lower()
            if key not in facts:
                available = ", ".join(sorted(facts)) or "(none)"
                return ToolResult.failure(
                    "not_found",
                    f"No field '{args.field}' is recorded for this location. "
                    f"Known fields: {available}.",
                )
            return ToolResult.success(f"{key}: {_render(facts[key])}", data={key: facts[key]})

        rendered = "\n".join(f"- {k}: {_render(v)}" for k, v in facts.items())
        return ToolResult.success(rendered, data=facts)


def _render(value: Any) -> str:
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


TOOL = LocationInfoTool()
