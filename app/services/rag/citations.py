"""Citation extraction and grounding measurement.

The answer prompt asks for ``[S1]``-style references. This module reads them
back out, checks they refer to passages that were actually supplied, and derives
a grounding ratio.

Grounding is a cheap, honest signal rather than a correctness measure: it says
what fraction of the answer's sentences carry a citation. It cannot tell whether
a cited passage supports the claim -- that needs a judge model. What it does
catch reliably is the failure that matters most here: an answer produced from
the model's own priors while sources sat unused in the context.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from app.services.rag.context_builder import Citation

#: Matches [S1], [S2, S3] and [S1][S4].
_REF_RE = re.compile(r"\[\s*(S\d+(?:\s*,\s*S\d+)*)\s*\]", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")
#: Sentences shorter than this are headings, list labels or "Here you go:" --
#: not claims, so they neither need nor count toward a citation.
_MIN_CLAIM_CHARS = 25


def extract_refs(text: str) -> set[str]:
    """All source references mentioned in an answer, normalized to 'S1' form."""
    refs: set[str] = set()
    for match in _REF_RE.finditer(text or ""):
        for part in match.group(1).split(","):
            cleaned = part.strip().upper()
            if cleaned:
                refs.add(cleaned)
    return refs


def validate_refs(text: str, citations: Sequence[Citation]) -> tuple[set[str], set[str]]:
    """Split an answer's references into real ones and invented ones.

    A model occasionally cites ``[S7]`` when six passages were supplied. Those
    are surfaced rather than quietly dropped, because a fabricated citation is a
    strong signal the surrounding sentence is also fabricated.
    """
    available = {c.ref.upper() for c in citations}
    mentioned = extract_refs(text)
    return mentioned & available, mentioned - available


def grounding_ratio(text: str, citations: Sequence[Citation]) -> float:
    """Fraction of substantive sentences that carry a valid citation."""
    if not citations or not text:
        return 0.0

    available = {c.ref.upper() for c in citations}
    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if len(s.strip()) >= _MIN_CLAIM_CHARS]
    if not sentences:
        return 1.0 if extract_refs(text) & available else 0.0

    cited = sum(1 for s in sentences if extract_refs(s) & available)
    return cited / len(sentences)


def used_citations(text: str, citations: Sequence[Citation]) -> list[Citation]:
    """The citations an answer actually referenced, in the order they appear.

    Returning only the used ones keeps a client's footnote list honest: showing
    six sources under an answer that drew on two overstates the evidence.
    """
    valid, _ = validate_refs(text, citations)
    order = {ref: index for index, ref in enumerate(_ordered_refs(text))}
    return sorted(
        (c for c in citations if c.ref.upper() in valid),
        key=lambda c: order.get(c.ref.upper(), 1_000),
    )


def _ordered_refs(text: str) -> list[str]:
    seen: list[str] = []
    for match in _REF_RE.finditer(text or ""):
        for part in match.group(1).split(","):
            ref = part.strip().upper()
            if ref and ref not in seen:
                seen.append(ref)
    return seen


def annotate(text: str, citations: Sequence[Citation]) -> dict[str, Any]:
    """Everything the API needs to render an answer with its sources."""
    valid, invalid = validate_refs(text, citations)
    return {
        "answer": text,
        "citations": [c.to_dict() for c in used_citations(text, citations)],
        "grounding_ratio": round(grounding_ratio(text, citations), 3),
        "unresolved_refs": sorted(invalid),
    }
