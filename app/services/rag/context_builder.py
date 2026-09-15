"""Turning retrieved passages into the context a model actually receives.

The knowledge base is everything stored. The context is the small subset chosen
for *this* question. Confusing the two is how RAG systems end up slow, expensive
and wrong, so the selection is explicit:

* exact and near duplicates removed -- overlapping chunks and re-uploaded
  documents otherwise spend the budget saying the same thing twice;
* a hard token budget, computed from the chosen model's window with room left
  for the answer;
* every passage labelled with its scope and carrying a citation reference, so
  the answer can be attributed and the location-precedence rule can be stated to
  the model rather than merely hoped for;
* retrieved text wrapped in delimited blocks and escaped, because a document is
  untrusted input and an instruction inside one is a prompt-injection attempt,
  not an instruction.

The output is a structured object, never a pre-concatenated blob: the API
returns its citations, the trace records its chunk ids, and rendering to prompt
text is one method on it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.config import ContextSettings, RetrievalSettings
from app.core.enums import KnowledgeScope
from app.core.logging import get_logger
from app.providers.search.base import SearchHit
from app.services.rag.token_budget import count_tokens, truncate_to_tokens

log = get_logger(__name__)

_WHITESPACE = re.compile(r"\s+")
#: Stripped from retrieved text so a document cannot close our own delimiters.
_DELIMITER_RE = re.compile(r"</?(?:source|sources|system|instructions)\b[^>]*>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Citation:
    """A reference the answer can point at, and a user can verify."""

    ref: str
    chunk_id: UUID
    document_id: UUID
    document_version: int
    source_name: str
    scope: str
    section: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    score: float = 0.0

    @property
    def locator(self) -> str:
        if self.page_from is None:
            return self.source_name
        if self.page_to in (None, self.page_from):
            return f"{self.source_name} p.{self.page_from}"
        return f"{self.source_name} pp.{self.page_from}-{self.page_to}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "document_version": self.document_version,
            "source": self.source_name,
            "locator": self.locator,
            "section": self.section,
            "scope": self.scope,
            "page_from": self.page_from,
            "page_to": self.page_to,
            "score": round(self.score, 4),
        }


@dataclass(frozen=True, slots=True)
class Passage:
    ref: str
    content: str
    scope: str
    citation: Citation
    token_count: int


@dataclass(slots=True)
class BuiltContext:
    passages: list[Passage] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    token_count: int = 0
    budget: int = 0
    dropped_duplicates: int = 0
    dropped_for_budget: int = 0
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.passages

    @property
    def has_location_specific(self) -> bool:
        return any(p.scope == KnowledgeScope.LOCATION.value for p in self.passages)

    def render(self) -> str:
        """Render for the prompt.

        Each passage is delimited and tagged with its reference and scope. The
        scope tag is what makes the precedence instruction in the system prompt
        actionable: the model can see which passages are location-specific.
        """
        if not self.passages:
            return "<sources>\n(no relevant sources were found)\n</sources>"

        blocks = [
            f'<source ref="{p.ref}" scope="{p.scope}" from="{p.citation.locator}">\n'
            f"{p.content}\n"
            f"</source>"
            for p in self.passages
        ]
        return "<sources>\n" + "\n\n".join(blocks) + "\n</sources>"

    def trace(self) -> dict[str, Any]:
        return {
            "passages": len(self.passages),
            "tokens": self.token_count,
            "budget": self.budget,
            "dropped_duplicates": self.dropped_duplicates,
            "dropped_for_budget": self.dropped_for_budget,
            "truncated": self.truncated,
            "chunk_ids": [str(p.citation.chunk_id) for p in self.passages],
            "scopes": sorted({p.scope for p in self.passages}),
        }


class ContextBuilder:
    def __init__(self, settings: ContextSettings, retrieval: RetrievalSettings) -> None:
        self.settings = settings
        self.retrieval = retrieval

    def build(
        self,
        hits: Sequence[SearchHit],
        *,
        budget_tokens: int | None = None,
        min_score: float | None = None,
    ) -> BuiltContext:
        budget = budget_tokens or self.settings.max_context_tokens
        floor = self.retrieval.min_score if min_score is None else min_score

        context = BuiltContext(budget=budget)
        seen_hashes: set[str] = set()
        seen_shingles: list[frozenset[str]] = []

        for hit in hits:
            if hit.score < floor:
                continue

            cleaned = _sanitize(hit.content)
            if not cleaned:
                continue

            fingerprint = _normalize(cleaned)
            digest = str(hash(fingerprint))
            shingles = _shingles(fingerprint)

            if digest in seen_hashes or _is_near_duplicate(
                shingles, seen_shingles, self.retrieval.dedup_threshold
            ):
                context.dropped_duplicates += 1
                continue

            tokens = count_tokens(cleaned)
            remaining = budget - context.token_count

            if tokens > remaining:
                # Truncating the first passage is better than returning nothing;
                # truncating a later one produces a fragment that adds noise, so
                # everything after the budget is simply dropped.
                if not context.passages and remaining > self.retrieval.top_k * 8:
                    cleaned = truncate_to_tokens(cleaned, remaining)
                    tokens = count_tokens(cleaned)
                    context.truncated = True
                else:
                    context.dropped_for_budget += 1
                    continue

            ref = f"S{len(context.passages) + 1}"
            citation = _citation_for(hit, ref)
            context.passages.append(
                Passage(
                    ref=ref,
                    content=cleaned,
                    scope=hit.scope.value,
                    citation=citation,
                    token_count=tokens,
                )
            )
            context.citations.append(citation)
            context.token_count += tokens
            seen_hashes.add(digest)
            seen_shingles.append(shingles)

        log.info("context_built", **context.trace())
        return context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _citation_for(hit: SearchHit, ref: str) -> Citation:
    return Citation(
        ref=ref,
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_version=hit.document_version,
        source_name=hit.source_name,
        scope=hit.scope.value,
        section=hit.breadcrumb or hit.heading,
        page_from=hit.page_from,
        page_to=hit.page_to,
        score=hit.score,
    )


def _sanitize(text: str) -> str:
    """Neutralize anything in a document that imitates our prompt structure.

    Uploaded documents are untrusted. A document containing ``</sources>`` or a
    fake system block would otherwise let its author restructure the prompt.
    """
    return _DELIMITER_RE.sub("", text or "").strip()


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _shingles(text: str, size: int = 5) -> frozenset[str]:
    """Word n-grams, used for cheap near-duplicate detection.

    Chunk overlap means adjacent chunks legitimately share a sentence or two;
    a re-uploaded document shares nearly everything. Shingle containment
    separates the two without needing the embeddings.
    """
    words = text.split()
    if len(words) <= size:
        return frozenset({" ".join(words)}) if words else frozenset()
    return frozenset(" ".join(words[i : i + size]) for i in range(len(words) - size + 1))


def _is_near_duplicate(
    shingles: frozenset[str], seen: list[frozenset[str]], threshold: float
) -> bool:
    if not shingles:
        return False
    for previous in seen:
        if not previous:
            continue
        # Containment, not Jaccard: a short passage fully contained in a longer
        # one is a duplicate even though Jaccard would score it low.
        overlap = len(shingles & previous) / min(len(shingles), len(previous))
        if overlap >= threshold:
            return True
    return False
