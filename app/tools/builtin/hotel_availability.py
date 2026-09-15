"""Room availability -- a simulated property management system.

This one is explicitly a stand-in for the integration a real deployment would
have, and it says so in its own output. That matters: a tool that silently
invents bookable inventory would have the model quoting availability that does
not exist, and the first person to find out would be a guest at the front desk.

It is still a *real* tool in every way that affects the architecture -- schema
validation, tenant scoping, timeouts, structured results -- so replacing it with
a PMS client is a change to this file alone.

Results are deterministic for a given (location, date, room type) so tests can
assert on them and so the same question twice does not produce two answers.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.tools.base import Tool, ToolContext, ToolDefinition, ToolResult

RoomType = Literal["standard", "deluxe", "suite"]

_ROOM_INVENTORY: dict[str, int] = {"standard": 40, "deluxe": 16, "suite": 6}
_BASE_RATE_JPY: dict[str, int] = {"standard": 24000, "deluxe": 38000, "suite": 72000}
_MAX_HORIZON_DAYS = 365

_DISCLAIMER = (
    "(simulated availability - this deployment is not connected to a live "
    "property management system)"
)


class AvailabilityArgs(BaseModel):
    check_in: str = Field(description="Check-in date, YYYY-MM-DD.")
    nights: int = Field(default=1, ge=1, le=30, description="Number of nights.")
    room_type: RoomType = Field(default="standard", description="Room category.")
    guests: int = Field(default=2, ge=1, le=8)

    @field_validator("check_in")
    @classmethod
    def _parse_date(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("check_in must be a date in YYYY-MM-DD form") from exc
        today = datetime.now(UTC).date()
        if parsed < today - timedelta(days=1):
            raise ValueError("check_in is in the past")
        if parsed > today + timedelta(days=_MAX_HORIZON_DAYS):
            raise ValueError(f"check_in is more than {_MAX_HORIZON_DAYS} days ahead")
        return parsed.isoformat()


class HotelAvailabilityTool(Tool):
    definition = ToolDefinition(
        name="hotel_availability",
        description=(
            "Check room availability and nightly rate for this location on a "
            "given date. Returns simulated data: state clearly that availability "
            "must be confirmed with the front desk before relying on it."
        ),
        input_model=AvailabilityArgs,
        scopes=frozenset({"tools:basic"}),
        timeout_s=10.0,
    )

    async def execute(self, args: AvailabilityArgs, context: ToolContext) -> ToolResult:
        if context.tenant.location_id is None:
            return ToolResult.failure(
                "invalid_arguments",
                "Availability is per-property; this session is not scoped to a location.",
            )

        check_in = date.fromisoformat(args.check_in)
        nights = []
        for offset in range(args.nights):
            night = check_in + timedelta(days=offset)
            nights.append(
                self._night(
                    location_id=str(context.tenant.location_id),
                    night=night,
                    room_type=args.room_type,
                )
            )

        bookable = min(night["available"] for night in nights)
        total = sum(night["rate_jpy"] for night in nights)

        if bookable == 0:
            sold_out = next(n["date"] for n in nights if n["available"] == 0)
            return ToolResult.success(
                f"No {args.room_type} rooms available for the full stay: "
                f"{sold_out} is sold out. {_DISCLAIMER}",
                data={"available": 0, "nights": nights, "room_type": args.room_type},
            )

        summary = ", ".join(
            f"{n['date']}: {n['available']} left at ¥{n['rate_jpy']:,}" for n in nights
        )
        return ToolResult.success(
            f"{bookable} {args.room_type} room(s) available for {args.nights} night(s) "
            f"from {args.check_in}. {summary}. Total ¥{total:,} for one room. {_DISCLAIMER}",
            data={
                "available": bookable,
                "room_type": args.room_type,
                "nights": nights,
                "total_jpy": total,
                "guests": args.guests,
                "simulated": True,
            },
        )

    @staticmethod
    def _night(*, location_id: str, night: date, room_type: str) -> dict[str, object]:
        """Deterministic pseudo-inventory for one night.

        Hashing the inputs gives a stable answer per (property, date, room type)
        without storing anything, and weekend uplift makes the numbers behave
        plausibly enough to demonstrate the agent reasoning over them.
        """
        seed = hashlib.blake2b(
            f"{location_id}:{night.isoformat()}:{room_type}".encode(), digest_size=8
        ).digest()
        capacity = _ROOM_INVENTORY[room_type]
        occupancy = 0.45 + (seed[0] / 255) * 0.5
        if night.weekday() >= 4:  # Friday-Sunday run fuller and dearer
            occupancy = min(0.98, occupancy + 0.18)

        available = max(0, int(capacity * (1 - occupancy)))
        rate = int(
            _BASE_RATE_JPY[room_type]
            * (1 + (0.25 if night.weekday() >= 4 else 0.0))
            * (1 + (seed[1] / 255) * 0.15)
        )
        return {
            "date": night.isoformat(),
            "available": available,
            "rate_jpy": rate - rate % 100,
        }


TOOL = HotelAvailabilityTool()
