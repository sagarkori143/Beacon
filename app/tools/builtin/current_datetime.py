"""The current date and time, in the location's timezone.

Models cannot know the date. Without this tool, any question involving "today",
"tomorrow" or "this week" gets answered against the model's training cutoff,
confidently and wrongly. The location's own timezone is used because "is the
restaurant open now?" is a question about local time, not UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.repositories.organization import get_location
from app.tools.base import Tool, ToolContext, ToolDefinition, ToolResult

log = get_logger(__name__)


class DateTimeArgs(BaseModel):
    timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone name. Omit to use this location's own timezone, which "
            "is almost always what you want."
        ),
    )


class CurrentDateTimeTool(Tool):
    definition = ToolDefinition(
        name="current_datetime",
        description=(
            "Get the current date, time, day of week and timezone. Use whenever a "
            "question involves today, tomorrow, now, or whether something is "
            "currently open."
        ),
        input_model=DateTimeArgs,
        timeout_s=5.0,
    )

    async def execute(self, args: DateTimeArgs, context: ToolContext) -> ToolResult:
        tz_name = args.timezone or await self._location_timezone(context)

        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            return ToolResult.failure(
                "invalid_arguments",
                f"'{tz_name}' is not a recognized IANA timezone (e.g. 'Asia/Tokyo').",
            )

        now = datetime.now(UTC).astimezone(tz)
        return ToolResult.success(
            f"{now.strftime('%A, %d %B %Y, %H:%M')} ({tz_name})",
            data={
                "iso": now.isoformat(),
                "date": now.date().isoformat(),
                "time": now.strftime("%H:%M"),
                "weekday": now.strftime("%A"),
                "timezone": tz_name,
            },
        )

    async def _location_timezone(self, context: ToolContext) -> str:
        if context.tenant.location_id is None:
            return "UTC"
        try:
            async with context.uow.begin() as session:
                location = await get_location(session, context.tenant, context.tenant.location_id)
                return location.timezone or "UTC"
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            log.debug("location_timezone_lookup_failed", error=str(exc)[:200])
            return "UTC"


TOOL = CurrentDateTimeTool()
