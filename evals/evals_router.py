"""
FastAPI router for the remote eval v1 API.

Endpoints:
  GET  /evals/v1/evaluators   — list available evaluators
  POST /evals/v1/evaluate     — run a single named evaluator (orchestrator format)
  POST /evals/v1/batch        — run several evaluators on one prediction

Auth (bucket 1 — compliance/token gate):
  If REMOTE_EVAL_TOKEN is set in the environment, every /evals/v1/* request
  must supply one of:
    Authorization: Bearer <token>
    X-Remote-Eval-Token: <token>
  When REMOTE_EVAL_TOKEN is unset the endpoints are open (dev default).
  mTLS and IAM-signed requests are out of scope here — terminate them at the
  ingress / reverse-proxy layer before traffic reaches this server.

Sample-rate + retries (bucket 5 — BYO flexibility):
  X-Eval-Sample-Rate: 0.5      — only execute ~50% of incoming requests
  X-Eval-Max-Retries: 3        — LLM judges only; deterministic judges ignore this
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from evals.engine import EVALUATORS, _LLM_JUDGES, evaluate_all, evaluate_one
from evals.remote_models import (
    BatchEvalRequest,
    BatchEvalResponse,
    EvalEntityRequest,
    EvalEntityResponse,
    EvalResponse,
)

router = APIRouter(prefix="/evals/v1", tags=["remote-evals"])


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def _check_auth(
    authorization: str | None = Header(default=None),
    x_remote_eval_token: str | None = Header(default=None),
) -> None:
    required = os.environ.get("REMOTE_EVAL_TOKEN")
    if not required:
        return  # token not configured — open access

    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_remote_eval_token:
        token = x_remote_eval_token.strip()

    if token != required:
        raise HTTPException(status_code=401, detail="Invalid or missing eval token.")


# ---------------------------------------------------------------------------
# Header-driven execution controls
# ---------------------------------------------------------------------------


def _parse_sample_rate(
    x_eval_sample_rate: str | None = Header(default=None),
) -> float:
    if x_eval_sample_rate is None:
        return 1.0
    try:
        return max(0.0, min(1.0, float(x_eval_sample_rate)))
    except ValueError:
        return 1.0


def _parse_max_retries(
    x_eval_max_retries: str | None = Header(default=None),
) -> int:
    if x_eval_max_retries is None:
        return 1
    try:
        return max(1, min(5, int(x_eval_max_retries)))
    except ValueError:
        return 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_response(evaluator: str, result: Any, response_style: str) -> EvalResponse:
    """Convert an EvalResult to the wire schema, respecting response_style."""
    return EvalResponse(
        evaluator=evaluator,
        score=result.score,
        label=result.label,
        # Deterministic judges don't natively expose a confidence value; LLM
        # judges could surface one from metadata once they're extended to do so.
        confidence=None,
        explanation="" if response_style == "terse" else result.explanation,
        metadata={} if response_style == "terse" else result.metadata,
    )


_SAMPLED_OUT_RESPONSE = EvalResponse(
    evaluator="",
    score=0.5,
    label="sampled_out",
    explanation="Request sampled out per X-Eval-Sample-Rate.",
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/evaluators")
async def list_evaluators(_: None = Depends(_check_auth)) -> dict:
    """List all registered evaluators and their types."""
    return {
        "evaluators": sorted(EVALUATORS.keys()),
        "llm_judges": sorted(_LLM_JUDGES),
        "deterministic": sorted(set(EVALUATORS) - _LLM_JUDGES),
    }


@router.post("/evaluate", response_model=EvalEntityResponse)
async def evaluate(
    req: EvalEntityRequest,
    _: None = Depends(_check_auth),
    sample_rate: float = Depends(_parse_sample_rate),
    max_retries: int = Depends(_parse_max_retries),
) -> EvalEntityResponse:
    """
    Run a single named evaluator on any entity (span, trace, session, or example).

    Request fields:
      - evaluation_name: evaluator key (see GET /evals/v1/evaluators)
      - entity_type: "span" | "trace" | "session" | "example" (echoed in response)
      - entity_id: ID of the entity being evaluated
      - output: raw model output string — JSON-parsed into prediction dict server-side
      - expected: ground-truth dict for deterministic judges (optional for LLM judges)

    LLM-as-judge evaluators (evidence_groundedness, helpfulness) return
    label='skipped' when ANTHROPIC_API_KEY is not available server-side.
    """
    if req.evaluation_name not in EVALUATORS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown evaluator: {req.evaluation_name!r}. Available: {sorted(EVALUATORS)}",
        )

    # Parse output string into a prediction dict for judges
    try:
        prediction: dict[str, Any] = (
            json.loads(req.output) if isinstance(req.output, str) else (req.output or {})
        )
        if not isinstance(prediction, dict):
            prediction = {"output": req.output}
    except (json.JSONDecodeError, TypeError):
        prediction = {"output": req.output or ""}

    # Make span/trace attributes available to judges that need them
    prediction.setdefault("attributes", req.attributes)

    result = evaluate_one(
        req.evaluation_name,
        prediction,
        req.expected,
        client=None,
        sample_rate=sample_rate,
        max_retries=max_retries,
    )

    if result is None:
        return EvalEntityResponse(
            request_id=req.request_id,
            entity_type=req.entity_type,
            entity_id=req.entity_id,
            results={"label": "sampled_out"},
        )

    results: dict[str, Any] = {
        "label": result.label,
        "score": result.score,
    }
    if req.response_style != "terse" and result.explanation:
        results["explanation"] = result.explanation

    return EvalEntityResponse(
        request_id=req.request_id,
        entity_type=req.entity_type,
        entity_id=req.entity_id,
        results=results,
    )


@router.post("/batch", response_model=BatchEvalResponse)
async def batch_evaluate(
    req: BatchEvalRequest,
    _: None = Depends(_check_auth),
    sample_rate: float = Depends(_parse_sample_rate),
    max_retries: int = Depends(_parse_max_retries),
) -> BatchEvalResponse:
    """
    Run multiple evaluators against a single prediction in one request.

    Use evaluators: ["category_correctness", "dietary_safety"] to avoid
    LLM costs when ANTHROPIC_API_KEY is not available.
    """
    unknown = [e for e in req.evaluators if e not in EVALUATORS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown evaluator(s): {unknown}. Available: {sorted(EVALUATORS)}",
        )

    results = evaluate_all(
        req.evaluators,
        req.prediction,
        req.expected,
        client=None,
        sample_rate=sample_rate,
        max_retries=max_retries,
    )

    skipped = len(req.evaluators) - len(results)
    responses = [
        _to_response(name, result, req.response_style)
        for name, result in results.items()
    ]
    return BatchEvalResponse(
        results=responses,
        evaluated=len(responses),
        skipped=skipped,
    )
