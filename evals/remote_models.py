"""
Pydantic schemas for the remote eval v1 API.

EvalRequest / BatchEvalRequest  — incoming payloads from the Arize orchestrator
                                   or any curl/SDK client.
EvalResponse / BatchEvalResponse — response envelope matching the shape customers
                                   expect from a proprietary-scorer endpoint
                                   (bucket 2: score + label + confidence + optional
                                   explanation).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EvalRequest(BaseModel):
    evaluator: str = Field(
        ...,
        description="Name of the evaluator to run, e.g. 'category_correctness'.",
    )
    prediction: dict[str, Any] = Field(
        ...,
        description="The agent output to evaluate (Recommendation.model_dump()).",
    )
    expected: dict[str, Any] = Field(
        default_factory=dict,
        description="Ground-truth / golden row. Optional for LLM-as-judge evaluators.",
    )
    response_style: str = Field(
        default="full",
        description=(
            "'full' returns score + label + explanation + metadata. "
            "'terse' omits explanation and metadata — matches Capital-One-style "
            "proprietary scorer APIs that return score+label only."
        ),
    )
    extras: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary pass-through context (BYO flexibility, bucket 5).",
    )


class BatchEvalRequest(BaseModel):
    evaluators: list[str] = Field(
        ...,
        description="List of evaluator names to run against this prediction.",
    )
    prediction: dict[str, Any] = Field(
        ...,
        description="The agent output to evaluate.",
    )
    expected: dict[str, Any] = Field(
        default_factory=dict,
        description="Ground-truth / golden row.",
    )
    response_style: str = Field(
        default="full",
        description="'full' or 'terse' — see EvalRequest.",
    )
    extras: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary pass-through context.",
    )


class EvalResponse(BaseModel):
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
