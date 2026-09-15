"""Provider-agnostic token counting and budgeting.

Every model family tokenizes differently, and the system routes across several at
once, so an exact count is not available before the call. What matters is not
exactness but **never underestimating**: the budget decides how much retrieved
context is sent, and underestimating overflows the context window.

So: count with a single reference tokenizer, then apply a safety multiplier that
is calibrated from observed usage. A calibration factor above 1.0 means the real
tokenizer is denser than the reference, and the budget shrinks accordingly.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.providers.llm.base import Message

log = get_logger(__name__)

#: Per-message protocol overhead (role markers, delimiters). Small, but it adds
#: up across a long conversation and it is always non-zero.
_MESSAGE_OVERHEAD_TOKENS = 4

#: Used when tiktoken is unavailable. English averages ~4 characters per token;
#: 3.5 deliberately over-counts a little, which is the safe direction.
_CHARS_PER_TOKEN_FALLBACK = 3.5


@lru_cache(maxsize=1)
def _encoder() -> Any | None:
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # noqa: BLE001 - tokenizer is an optimization
        log.warning("tokenizer_unavailable", error=str(exc)[:200])
        return None


def count_tokens(text: str) -> int:
    """Approximate token count for a string."""
    if not text:
        return 0
    encoder = _encoder()
    if encoder is None:
        return int(len(text) / _CHARS_PER_TOKEN_FALLBACK) + 1
    return len(encoder.encode(text, disallowed_special=()))


def count_message_tokens(messages: Sequence[Message]) -> int:
    """Approximate token count for a conversation, including tool payloads."""
    total = 0
    for message in messages:
        total += _MESSAGE_OVERHEAD_TOKENS
        if message.content:
            total += count_tokens(message.content)
        for call in message.tool_calls:
            total += count_tokens(call.name)
            total += count_tokens(str(dict(call.arguments)))
    return total


@dataclass(slots=True)
class TokenCalibrator:
    """Learns how far the reference tokenizer is from a model's real one.

    Each provider reports actual prompt tokens after a call. Comparing that with
    what we predicted gives a per-model correction that converges quickly and
    costs nothing to maintain.
    """

    factors: dict[str, float] = field(default_factory=dict)
    #: Exponential moving average weight for new observations.
    alpha: float = 0.3
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def factor_for(self, model: str) -> float:
        return self.factors.get(model, 1.0)

    def observe(self, model: str, *, predicted: int, actual: int) -> None:
        if predicted <= 0 or actual <= 0:
            return
        ratio = actual / predicted
        # Ignore wild readings: they usually mean the provider counted something
        # we never sent (a cached system prefix, a tool schema we did not model).
        if not 0.25 <= ratio <= 4.0:
            return
        with self._lock:
            current = self.factors.get(model, 1.0)
            self.factors[model] = current * (1 - self.alpha) + ratio * self.alpha

    def estimate(self, model: str, raw_tokens: int) -> int:
        return int(raw_tokens * max(1.0, self.factor_for(model)))


@dataclass(frozen=True, slots=True)
class TokenBudget:
    """How many tokens the retrieved context may occupy for one request."""

    total: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def can_fit(self, tokens: int) -> bool:
        return tokens <= self.remaining

    def consume(self, tokens: int) -> TokenBudget:
        return TokenBudget(total=self.total, used=self.used + tokens)


def context_budget(
    *,
    context_window: int,
    max_output_tokens: int,
    prompt_overhead_tokens: int,
    configured_max: int,
    utilization: float = 0.75,
) -> TokenBudget:
    """Decide how much of the window retrieved context may use.

    Takes the smaller of the operator's configured cap and what actually fits
    after reserving room for the system prompt, the conversation so far and the
    answer itself. Reserving output space matters: a context that fills the
    window leaves the model no room to reply and it gets truncated mid-sentence.
    """
    available = int(context_window * utilization) - max_output_tokens - prompt_overhead_tokens
    return TokenBudget(total=max(0, min(configured_max, available)))


def truncate_to_tokens(text: str, max_tokens: int, *, suffix: str = "\n[truncated]") -> str:
    """Trim to a token budget at a line boundary where possible.

    Cutting mid-line produces fragments that read as corrupted data to the model;
    cutting at a newline reads as a document that simply ends.
    """
    if max_tokens <= 0:
        return ""
    if count_tokens(text) <= max_tokens:
        return text

    encoder = _encoder()
    if encoder is None:
        approx_chars = int(max_tokens * _CHARS_PER_TOKEN_FALLBACK)
        head = text[:approx_chars]
    else:
        head = encoder.decode(encoder.encode(text, disallowed_special=())[:max_tokens])

    boundary = head.rfind("\n")
    # Only honour the line boundary if it does not throw away most of the text.
    if boundary > len(head) * 0.6:
        head = head[:boundary]
    return head.rstrip() + suffix
