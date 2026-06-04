"""
Evaluator registry and execution engine.

Used by both the HTTP endpoints (evals/evals_router.py) and the CLI runner
(evals/run_evals.py) so judge wiring lives in exactly one place.

Key public API:
  EVALUATORS          — dict[name -> callable]
  evaluate_one(...)   — run one evaluator with optional sample-rate + retry
  evaluate_all(...)   — run a list of evaluators, return {name: EvalResult}
"""

from __future__ import annotations

import random
from typing import Any

from evals.judges import (
    EvalResult,
    allergen_recall,
    category_correctness,
    dietary_safety,
    evidence_groundedness,
    helpfulness,
)

# Registry: every evaluator reachable by name through the API or CLI.
EVALUATORS: dict[str, Any] = {
    "category_correctness": category_correctness,
    "dietary_safety": dietary_safety,
    "allergen_recall": allergen_recall,
    "evidence_groundedness": evidence_groundedness,
    "helpfulness": helpfulness,
}

# Judges that need an LLM client — only these respect max_retries.
_LLM_JUDGES: frozenset[str] = frozenset({"evidence_groundedness", "helpfulness"})


def evaluate_one(
    evaluator: str,
    prediction: dict,
    expected: dict,
    *,
    client: Any = None,
    sample_rate: float = 1.0,
    max_retries: int = 1,
) -> EvalResult | None:
    """
    Run a single named evaluator.

    Returns None when sample_rate causes the call to be skipped (bucket 5:
    BYO sample-rate header).  Deterministic judges ignore max_retries.

    Args:
        evaluator:   Key from EVALUATORS.
        prediction:  Agent output dict (Recommendation.model_dump()).
        expected:    Ground-truth dict (golden dataset row).
        client:      Anthropic client for LLM-as-judge evaluators. When None,
                     LLM judges return a neutral score=0.5 / label="skipped".
        sample_rate: Fraction 0–1; calls outside the sample return None.
        max_retries: Max LLM attempts on transient failure (≥ 1).
    """
    if evaluator not in EVALUATORS:
        raise ValueError(
            f"Unknown evaluator: {evaluator!r}. Available: {sorted(EVALUATORS)}"
        )

    if sample_rate < 1.0 and random.random() > sample_rate:
        return None

    fn = EVALUATORS[evaluator]
    is_llm = evaluator in _LLM_JUDGES
    attempts = max(1, max_retries) if is_llm else 1
    last_exc: Exception | None = None

    for _ in range(attempts):
        try:
            if is_llm:
                return fn(prediction, expected, client)
            else:
                return fn(prediction, expected)
        except Exception as exc:
            last_exc = exc

    return EvalResult(
        score=0.5,
        label="error",
        explanation=f"Judge failed after {attempts} attempt(s): {last_exc}",
    )


def evaluate_all(
    evaluators: list[str],
    prediction: dict,
    expected: dict,
    *,
    client: Any = None,
    sample_rate: float = 1.0,
    max_retries: int = 1,
) -> dict[str, EvalResult]:
    """
    Run multiple evaluators. Evaluators sampled out are omitted from the result.

    Returns a dict keyed by evaluator name containing only the ones that ran.
    """
    results: dict[str, EvalResult] = {}
    for name in evaluators:
        result = evaluate_one(
            name,
            prediction,
            expected,
            client=client,
            sample_rate=sample_rate,
            max_retries=max_retries,
        )
        if result is not None:
            results[name] = result
    return results
