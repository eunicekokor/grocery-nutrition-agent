"""
Pydantic schemas for the remote eval v1 API.

EvalEntityRequest / EvalEntityResponse — orchestrator-compatible wire format.
    Mirrors the evaluate_one(evaluator, prediction, ...) signature:
      request_id  — echoed back for correlation
      evaluator   — judge name
      prediction  — structured model output dict consumed by the judge
      attributes  — span/trace attributes merged into prediction for judge access

BatchEvalRequest / BatchEvalResponse — run multiple judges against a single
    structured prediction dict in one request (internal / tooling use).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EvalEntityRequest(BaseModel):
    request_id: str = Field(description="Caller-assigned request ID echoed in the response.")
    evaluator: str = Field(description="Name of the evaluator to run, e.g. 'category_correctness'.")
    prediction: dict[str, Any] = Field(
        description="The model/agent output to evaluate.",
    )
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="Span/trace attributes merged into the prediction dict for judge access.",
    )


class EvalEntityResponse(BaseModel):
    request_id: str
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
