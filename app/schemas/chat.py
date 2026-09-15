"""Chat and search request/response models."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    #: Restrict which tools the agent may use for this turn.
    tools: list[str] | None = None
    #: Override the planner's retrieval decision. ``None`` leaves it to the plan.
    force_retrieval: bool | None = None
    #: "provider/model". Honoured only if the organization allows that provider.
    model: str | None = None
    language: str = Field(default="English", max_length=40)
    include_trace: bool = Field(
        default=False, description="Return routing, retrieval and tool diagnostics."
    )


class CitationOut(BaseModel):
    ref: str
    chunk_id: UUID
    document_id: UUID
    document_version: int
    source: str
    locator: str
    section: str | None = None
    scope: str
    page_from: int | None = None
    page_to: int | None = None
    score: float = 0.0


class UsageOut(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0


class ChatResponse(BaseModel):
    answer: str
    conversation_id: UUID | None = None
    citations: list[CitationOut] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    usage: UsageOut = Field(default_factory=UsageOut)
    grounding_ratio: float = 0.0
    #: True when sources were available but the answer cited none of them.
    low_confidence: bool = False
    finish_reason: str = "stop"
    latency_ms: float = 0.0
    trace: dict[str, Any] | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=8, ge=1, le=50)
    #: Narrow within the caller's scope. Widening is rejected.
    location_id: UUID | None = None
    document_types: list[str] = Field(default_factory=list, max_length=10)
    languages: list[str] = Field(default_factory=list, max_length=5)
    #: Return each level separately instead of the merged, override-resolved set.
    hierarchical: bool = True


class SearchHitOut(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_version: int
    source: str
    scope: str
    section: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    content: str
    score: float
    vector_score: float | None = None
    keyword_score: float | None = None
    matched: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHitOut] = Field(default_factory=list)
    total: int = 0
    took_ms: float = 0.0
    #: Passages suppressed because a location-specific passage covered the topic.
    suppressed_by_override: int = 0
    overridden_topics: list[str] = Field(default_factory=list)
    #: True when the ANN arm under-returned and a wider probe was used.
    degraded: bool = False
