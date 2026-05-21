"""
Eval judges for the Grocery Nutrition Agent.

Four judges:
  1. category_correctness  — deterministic exact match
  2. dietary_safety        — deterministic recall check (most critical for safety)
  3. evidence_groundedness — LLM-as-judge: hallucination check on evidence items
  4. helpfulness           — LLM-as-judge: 1-5 rating of rationale quality

Each judge returns an EvalResult with a score (0.0-1.0) and an explanation.

These are designed to be compatible with the phoenix.evals pattern — each judge
is a pure function (input dict -> EvalResult) so run_evals.py can call them and
log the results to Arize.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalResult:
    score: float          # 0.0 – 1.0
    label: str            # human-readable verdict
    explanation: str      # reasoning trace
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Judge 1: category_correctness (deterministic)
# ---------------------------------------------------------------------------


def category_correctness(
    prediction: dict,
    expected: dict,
) -> EvalResult:
    """
    Exact match between predicted and expected recommendation category.
    Score: 1.0 = correct, 0.0 = wrong.

    prediction: Recommendation.model_dump()
    expected:   row from golden_dataset.jsonl
    """
    pred_cat = (prediction.get("category") or "").upper()
    exp_cat  = (expected.get("expected_category") or "").upper()

    if not exp_cat:
        return EvalResult(score=0.5, label="no_ground_truth", explanation="No expected_category in golden row.")

    correct = pred_cat == exp_cat
    return EvalResult(
        score=1.0 if correct else 0.0,
        label="correct" if correct else "incorrect",
        explanation=f"Predicted: {pred_cat} | Expected: {exp_cat}",
        metadata={"predicted": pred_cat, "expected": exp_cat},
    )


# ---------------------------------------------------------------------------
# Judge 2: dietary_safety (deterministic recall)
# ---------------------------------------------------------------------------


def dietary_safety(
    prediction: dict,
    expected: dict,
) -> EvalResult:
    """
    Check whether the agent caught all expected dietary conflicts.
    Uses recall — missing a conflict is worse than a false positive.

    Score: fraction of expected conflicts that appear in the prediction.
    Score = 1.0 if no expected conflicts (trivially safe).
    """
    expected_conflicts = expected.get("expected_dietary_conflicts", [])
    if not expected_conflicts:
        return EvalResult(
            score=1.0,
            label="pass",
            explanation="No expected dietary conflicts — trivially safe.",
        )

    personal_fit = prediction.get("personal_fit") or {}
    predicted_conflicts = personal_fit.get("dietary_conflicts", [])
    predicted_allergens = personal_fit.get("allergen_warnings", [])

    # Flatten predicted conflict signatures
    predicted_sigs: set[str] = set()
    for c in predicted_conflicts:
        predicted_sigs.add(f"{c.get('restriction','').lower()}:{c.get('ingredient','').lower()}")
    for w in predicted_allergens:
        predicted_sigs.add(f"allergen:{w.get('allergen','').lower()}")

    caught = 0
    missed_details: list[str] = []
    for exp in expected_conflicts:
        sig = f"{exp.get('restriction','').lower()}:{exp.get('ingredient','').lower()}"
        # Check both exact and partial matches (ingredient may be substring)
        exp_restriction = exp.get("restriction", "").lower()
        exp_ingredient  = exp.get("ingredient", "").lower()
        found = any(
            (exp_restriction in ps and any(part in ps for part in exp_ingredient.split()))
            for ps in predicted_sigs
        )
        if found:
            caught += 1
        else:
            missed_details.append(f"{exp.get('restriction')}/{exp.get('ingredient')}")

    recall = caught / len(expected_conflicts)
    label = "pass" if recall == 1.0 else ("partial" if recall > 0 else "fail")
    explanation = (
        f"Caught {caught}/{len(expected_conflicts)} expected conflicts."
        + (f" Missed: {', '.join(missed_details)}" if missed_details else "")
    )
    return EvalResult(
        score=recall,
        label=label,
        explanation=explanation,
        metadata={"caught": caught, "total": len(expected_conflicts), "missed": missed_details},
    )


# ---------------------------------------------------------------------------
# Judge 3: evidence_groundedness (LLM-as-judge)
# ---------------------------------------------------------------------------

GROUNDEDNESS_PROMPT = """\
You are an evaluation judge checking whether an AI nutritionist's recommendation
is grounded in the provided product facts.

## Product Facts
{product_facts}

## Recommendation Evidence
{evidence}

## Task
For each evidence item, determine whether the claim is supported by the product facts.
A claim is GROUNDED if it refers to a specific nutrient, ingredient, or property that
appears in the product facts with roughly the correct value.
A claim is UNGROUNDED if it states something not present in the product facts, or
contradicts the product facts (hallucination).

Return a JSON object:
{{
  "items": [
    {{"claim": "<the claim>", "grounded": true/false, "reason": "<one sentence>"}}
  ],
  "overall_score": <float 0.0-1.0, fraction of items that are grounded>,
  "summary": "<one sentence overall assessment>"
}}

Output ONLY the JSON object.
"""


def evidence_groundedness(
    prediction: dict,
    _expected: dict,
    client: Any = None,
) -> EvalResult:
    """
    LLM-as-judge: check each evidence item against the product facts.
    Falls back to a neutral score if no LLM client or no product facts.
    """
    evidence = prediction.get("evidence", [])
    product_facts = prediction.get("product_facts")

    if not evidence:
        return EvalResult(score=1.0, label="no_evidence", explanation="No evidence items to check.")
    if not product_facts:
        return EvalResult(score=0.5, label="no_facts", explanation="No product_facts in prediction — cannot verify groundedness.")

    if client is None:
        return EvalResult(score=0.5, label="skipped", explanation="No Anthropic client provided — LLM judge skipped.")

    import anthropic as _anthropic
    prompt = GROUNDEDNESS_PROMPT.format(
        product_facts=json.dumps(product_facts, indent=2),
        evidence=json.dumps(evidence, indent=2),
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1])
        data = json.loads(raw)
        score = float(data.get("overall_score", 0.5))
        return EvalResult(
            score=score,
            label="pass" if score >= 0.8 else ("partial" if score >= 0.5 else "fail"),
            explanation=data.get("summary", ""),
            metadata={"items": data.get("items", [])},
        )
    except Exception as exc:
        return EvalResult(score=0.5, label="error", explanation=f"Judge error: {exc}")


# ---------------------------------------------------------------------------
# Judge 4: helpfulness (LLM-as-judge, 1-5 rating)
# ---------------------------------------------------------------------------

HELPFULNESS_PROMPT = """\
You are an evaluation judge rating the quality of a nutritionist recommendation.

## User Profile
{profile}

## Product
{product_name}

## Recommendation
Category: {category}
Headline: {headline}
Rationale: {rationale}

## Scoring rubric (1-5):
5 — Excellent: specific, actionable, directly cites nutritional facts, fully considers the user profile.
4 — Good: mostly specific, minor gaps in addressing the profile or citing facts.
3 — Acceptable: correct category, but vague rationale or partially ignores the profile.
2 — Poor: generic boilerplate, misses key profile considerations, few specific facts.
1 — Bad: incorrect, misleading, or no real nutritional content.

Return a JSON object:
{{
  "score": <integer 1-5>,
  "normalized_score": <float 0.0-1.0, where 5->1.0>,
  "explanation": "<2-3 sentences justifying the score>"
}}

Output ONLY the JSON object.
"""


def helpfulness(
    prediction: dict,
    expected: dict,
    client: Any = None,
) -> EvalResult:
    """
    LLM-as-judge: rate the rationale quality 1-5 given the profile and product.
    """
    if client is None:
        return EvalResult(score=0.5, label="skipped", explanation="No Anthropic client provided — LLM judge skipped.")

    product_facts = prediction.get("product_facts") or {}
    profile = expected.get("profile", {})
    prompt = HELPFULNESS_PROMPT.format(
        profile=json.dumps(profile, indent=2),
        product_name=product_facts.get("name", "Unknown product"),
        category=prediction.get("category", ""),
        headline=prediction.get("headline", ""),
        rationale=prediction.get("rationale", ""),
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1])
        data = json.loads(raw)
        score = float(data.get("normalized_score", data.get("score", 3) / 5))
        raw_score = data.get("score", round(score * 5))
        return EvalResult(
            score=score,
            label=f"{raw_score}/5",
            explanation=data.get("explanation", ""),
            metadata={"raw_score": raw_score},
        )
    except Exception as exc:
        return EvalResult(score=0.5, label="error", explanation=f"Judge error: {exc}")


# ---------------------------------------------------------------------------
# Convenience: run all judges on a single (prediction, expected) pair
# ---------------------------------------------------------------------------


def run_all_judges(
    prediction: dict,
    expected: dict,
    client: Any = None,
) -> dict[str, EvalResult]:
    return {
        "category_correctness": category_correctness(prediction, expected),
        "dietary_safety": dietary_safety(prediction, expected),
        "evidence_groundedness": evidence_groundedness(prediction, expected, client),
        "helpfulness": helpfulness(prediction, expected, client),
    }
