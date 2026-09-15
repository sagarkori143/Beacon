"""Current weather, via Open-Meteo.

A real external call rather than a stub, because a tool that returns invented
data teaches the model that tool output is not to be trusted -- and because the
interesting failure modes of tool calling (latency, timeouts, upstream errors)
only exist when something is actually on the other end.

Open-Meteo needs no API key and no account, which keeps the demo runnable
anywhere. It is disabled automatically when the deployment has no outbound
network, and says so rather than guessing.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.tools.base import Tool, ToolContext, ToolDefinition, ToolResult

log = get_logger(__name__)

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

#: WMO weather codes, condensed to the cases worth distinguishing in prose.
_CONDITIONS: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "heavy rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
}


class WeatherArgs(BaseModel):
    location: str = Field(
        description="City or place name, e.g. 'Tokyo' or 'Ginza, Tokyo'.",
        min_length=2,
        max_length=120,
    )


class WeatherTool(Tool):
    definition = ToolDefinition(
        name="weather",
        description=(
            "Get current weather conditions and temperature for a city. Use for "
            "questions about weather now or today."
        ),
        input_model=WeatherArgs,
        timeout_s=12.0,
    )

    async def execute(self, args: WeatherArgs, context: ToolContext) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
                geo = await client.get(
                    _GEOCODE_URL,
                    params={"name": args.location, "count": 1, "format": "json"},
                )
                geo.raise_for_status()
                places = (geo.json() or {}).get("results") or []
                if not places:
                    return ToolResult.failure(
                        "not_found", f"No place found matching '{args.location}'."
                    )
                place = places[0]

                forecast = await client.get(
                    _FORECAST_URL,
                    params={
                        "latitude": place["latitude"],
                        "longitude": place["longitude"],
                        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                        "timezone": "auto",
                    },
                )
                forecast.raise_for_status()
                current = (forecast.json() or {}).get("current") or {}
        except httpx.TimeoutException:
            return ToolResult.failure("timeout", "The weather service did not respond in time.")
        except httpx.HTTPError as exc:
            log.warning("weather_unavailable", error=str(exc)[:200])
            return ToolResult.failure(
                "unavailable",
                "The weather service could not be reached from this deployment.",
            )

        condition = _CONDITIONS.get(int(current.get("weather_code", -1)), "unknown conditions")
        name = ", ".join(part for part in (place.get("name"), place.get("country")) if part)
        temperature = current.get("temperature_2m")

        return ToolResult.success(
            f"{name}: {temperature}°C, {condition}, "
            f"humidity {current.get('relative_humidity_2m')}%, "
            f"wind {current.get('wind_speed_10m')} km/h.",
            data={
                "location": name,
                "temperature_c": temperature,
                "condition": condition,
                "humidity_pct": current.get("relative_humidity_2m"),
                "wind_kmh": current.get("wind_speed_10m"),
                "source": "open-meteo.com",
            },
        )


TOOL = WeatherTool()
