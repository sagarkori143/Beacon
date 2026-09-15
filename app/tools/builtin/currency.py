"""Currency conversion, via Frankfurter (European Central Bank reference rates).

Keyless and free, so the demo runs anywhere, and real, so the model is not being
trained on fabricated numbers. ECB rates are published once per working day --
the response says which date the rate is from, and so does the tool's output,
because presenting a day-old rate as "current" is the kind of quiet inaccuracy
that matters when someone is quoting a price.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field, field_validator

from app.core.logging import get_logger
from app.tools.base import Tool, ToolContext, ToolDefinition, ToolResult

log = get_logger(__name__)

_RATES_URL = "https://api.frankfurter.app/latest"


class CurrencyArgs(BaseModel):
    amount: float = Field(gt=0, le=1_000_000_000, description="Amount to convert.")
    from_currency: str = Field(
        description="Source currency as a 3-letter ISO code, e.g. 'JPY'.",
        min_length=3,
        max_length=3,
    )
    to_currency: str = Field(
        description="Target currency as a 3-letter ISO code, e.g. 'USD'.",
        min_length=3,
        max_length=3,
    )

    @field_validator("from_currency", "to_currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        code = value.strip().upper()
        if not code.isalpha():
            raise ValueError("Currency codes are three letters, e.g. USD")
        return code


class CurrencyTool(Tool):
    definition = ToolDefinition(
        name="currency",
        description=(
            "Convert an amount between two currencies at the latest published "
            "reference rate. Use for price and fee questions in another currency."
        ),
        input_model=CurrencyArgs,
        timeout_s=12.0,
    )

    async def execute(self, args: CurrencyArgs, context: ToolContext) -> ToolResult:
        if args.from_currency == args.to_currency:
            return ToolResult.success(
                f"{args.amount:,.2f} {args.from_currency} = {args.amount:,.2f} {args.to_currency}",
                data={"amount": args.amount, "rate": 1.0},
            )

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
                response = await client.get(
                    _RATES_URL,
                    params={
                        "amount": args.amount,
                        "from": args.from_currency,
                        "to": args.to_currency,
                    },
                )
                if response.status_code == 404:
                    return ToolResult.failure(
                        "invalid_arguments",
                        f"'{args.from_currency}' or '{args.to_currency}' is not a "
                        "supported currency code.",
                    )
                response.raise_for_status()
                body = response.json() or {}
        except httpx.TimeoutException:
            return ToolResult.failure("timeout", "The rate service did not respond in time.")
        except httpx.HTTPError as exc:
            log.warning("currency_unavailable", error=str(exc)[:200])
            return ToolResult.failure(
                "unavailable", "Exchange rates could not be retrieved from this deployment."
            )

        converted = (body.get("rates") or {}).get(args.to_currency)
        if converted is None:
            return ToolResult.failure(
                "unavailable", f"No rate available for {args.from_currency} to {args.to_currency}."
            )

        rate_date = body.get("date", "unknown date")
        return ToolResult.success(
            f"{args.amount:,.2f} {args.from_currency} = {converted:,.2f} {args.to_currency} "
            f"(ECB reference rate, {rate_date})",
            data={
                "amount": args.amount,
                "from": args.from_currency,
                "to": args.to_currency,
                "converted": converted,
                "rate": converted / args.amount if args.amount else None,
                "rate_date": rate_date,
                "source": "frankfurter.app (ECB)",
            },
        )


TOOL = CurrencyTool()
