"""
Pydantic schemas for the remote eval v1 API.

EvalEntityRequest / EvalEntityResponse — orchestrator-compatible wire format.
    Accepts any entity type (span, trace, session, example) identified by
    entity_type + entity_id.  The output field is JSON-parsed server-side into
    the prediction dict consumed by judges.

BatchEvalRequest / BatchEvalResponse — run multiple judges against a single
    structured prediction dict in one request (internal / tooling use).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EvalContext(BaseModel):
    trace_id: str | None = None
    model_id: str | None = None
    timestamp: str | None = None  # ISO8601


class EvalEntityRequest(BaseModel):
    request_id: str = Field(description="Caller-assigned request ID echoed in the response.")
    evaluation_name: str = Field(description="Name of the evaluator to run, e.g. 'category_correctness'.")
    entity_type: str = Field(
        description="Entity kind: 'span' | 'trace' | 'session' | 'example'. Pass-through — echoed in response.",
    )
    entity_id: str = Field(description="ID of the entity being evaluated (span_id, trace_id, etc.).")
    input: str | None = Field(default=None, description="Raw input to the model/agent.")
    output: str | None = Field(
        default=None,
        description=(
            "Raw output from the model/agent. JSON-parsed into a prediction dict "
            "server-side; falls back to {'output': raw_string} if not valid JSON."
        ),
    )
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="Span/trace attributes merged into the prediction dict for judge access.",
    )
    context: EvalContext = Field(
        default_factory=EvalContext,
        description="Trace context metadata (trace_id, model_id, timestamp).",
    )
    expected: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Ground-truth data for deterministic judges "
            "(e.g. {'expected_category': 'MORE_OF'}). Optional for LLM-as-judge."
        ),
    )
    response_style: str = Field(
        default="full",
        description="'full' includes explanation; 'terse' omits it.",
    )


class EvalEntityResponse(BaseModel):
    request_id: str
    entity_type: str
    entity_id: str
    results: dict[str, Any] = Field(
        description=(
            "Evaluation results. Any combination of label (str), score (0–1), "
            "explanation (str), confidence (0–1). Fields absent when not applicable."
        ),
    )


class BatchEvalRequest(BaseModel):
    evaluators: list[str] = Field(
        ...,
        description="List of evaluator names to run against this prediction.",
    )
    prediction: dict[str, Any] = Field(
        ...,
        description="The agent output to evaluate (structured dict, not raw string).",
    )
    expected: dict[str, Any] = Field(
        default_factory=dict,
        description="Ground-truth / golden row.",
    )
    response_style: str = Field(
        default="full",
        description="'full' or 'terse'.",
    )
    extras: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary pass-through context.",
    )


class EvalResponse(BaseModel):
    """Used by /batch only."""
    evaluator: str
    score: float = Field(..., ge=0.0, le=1.0, description="0.0–1.0 normalized score.")
    label: str = Field(..., description="Human-readable verdict, e.g. 'correct'.")
    confidence: float | None = Field(
        default=None,
        description="Model confidence where available (null for deterministic judges).",
    )
    explanation: str = Field(
        default="",
        description="Reasoning trace. Empty when response_style='terse'.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra judge-specific data. Empty when response_style='terse'.",
    )


class BatchEvalResponse(BaseModel):
    results: list[EvalResponse]
    evaluated: int = Field(..., description="Number of evaluators that ran.")
    skipped: int = Field(
        default=0,
        description="Number sampled out via X-Eval-Sample-Rate.",
    )
