"""
Eval runner for the Grocery Nutrition Agent.

Usage:
    python evals/run_evals.py                    # run all rows, env from ARIZE_ENV (default: dev)
    python evals/run_evals.py --env prod         # send traces + eval logs to prod
    python evals/run_evals.py --ids 001 002 003  # run specific rows
    python evals/run_evals.py --dry-run          # skip Arize logging

Steps:
  1. Load golden_dataset.jsonl
  2. For each row, run run_pipeline(url, profile) -> Recommendation
  3. Run all judges on the result
  4. Print a summary table
  5. Log eval results back to Arize attached to the original trace span
     (if ARIZE_SPACE_ID / ARIZE_API_KEY are configured)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Allow running from project root or from evals/ directory
_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root))

import anthropic

from nutrition_agent.pipeline import run_pipeline
from nutrition_agent.schemas import (
    DietaryRestriction,
    HealthGoal,
    UserProfile,
)
from nutrition_agent.tracing import setup_tracing
from evals.engine import EVALUATORS, evaluate_all
from evals.judges import EvalResult

DATASET_PATH = Path(__file__).parent / "golden_dataset.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_dataset(ids: list[str] | None = None) -> list[dict]:
    rows = []
    with open(DATASET_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if ids is None or row.get("id") in ids:
                rows.append(row)
    return rows


def build_profile(raw: dict) -> UserProfile:
    return UserProfile(
        dietary_restrictions=[
            DietaryRestriction(r)
            for r in raw.get("dietary_restrictions", [])
            if r in DietaryRestriction._value2member_map_
        ],
        allergens=raw.get("allergens", []),
        health_goals=[
            HealthGoal(g)
            for g in raw.get("health_goals", [])
            if g in HealthGoal._value2member_map_
        ],
        daily_sodium_mg_target=raw.get("daily_sodium_mg_target"),
        daily_sugar_g_target=raw.get("daily_sugar_g_target"),
    )


def score_color(score: float) -> str:
    if score >= 0.8:
        return "\033[92m"  # green
    if score >= 0.5:
        return "\033[93m"  # yellow
    return "\033[91m"  # red


RESET = "\033[0m"


def print_row_result(row_id: str, url: str, results: dict[str, EvalResult]) -> None:
    print(f"\n  Row {row_id}: {url[:60]}{'…' if len(url) > 60 else ''}")
    for judge, result in results.items():
        c = score_color(result.score)
        print(
            f"    {judge:<28} {c}{result.score:.2f}{RESET}  [{result.label}]  {result.explanation[:80]}"
        )


def log_to_arize(
    row_id: str,
    request_id: str,
    results: dict[str, EvalResult],
) -> None:
    """
    Log eval results to Arize as evaluations on the trace span.
    Uses arize.pandas.logger if configured; falls back to a print.
    """
    space_id = os.environ.get("ARIZE_SPACE_ID")
    api_key = os.environ.get("ARIZE_API_KEY")
    if not space_id or not api_key:
        print(f"    [arize] skipped — ARIZE_SPACE_ID/ARIZE_API_KEY not set")
        return

    try:
        import pandas as pd
        from arize.pandas.logger import Client as ArizeClient
        from arize.utils.types import (
            Environments,
            ModelTypes,
            EmbeddingColumnNames,
            Schema,
        )

        arize_client = ArizeClient(space_key=space_id, api_key=api_key)

        records = []
        for judge_name, result in results.items():
            records.append(
                {
                    "prediction_id": request_id,
                    "eval_name": judge_name,
                    "eval_score": result.score,
                    "eval_label": result.label,
                    "eval_explanation": result.explanation,
                    "row_id": row_id,
                }
            )

        df = pd.DataFrame(records)
        # Arize evaluation logging via the pandas logger
        # The request_id ties back to the span's session.id attribute set in tracing.py
        response = arize_client.log(
            dataframe=df,
            model_id="grocery-nutrition-agent",
            model_version="1.0",
            environment=Environments.PRODUCTION,
            model_type=ModelTypes.GENERATIVE_LLM,
            schema=Schema(
                prediction_id_column_name="prediction_id",
                tag_column_names=[
                    "eval_name",
                    "eval_label",
                    "eval_explanation",
                    "row_id",
                ],
                prediction_score_column_name="eval_score",
            ),
        )
        if response.status_code == 200:
            print(
                f"    [arize] logged {len(records)} eval results for request {request_id[:8]}…"
            )
        else:
            print(
                f"    [arize] log returned {response.status_code}: {response.text[:100]}"
            )

    except ImportError:
        print("    [arize] arize or pandas not installed — skipping Arize logging")
    except Exception as exc:
        print(f"    [arize] logging failed: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run evals for the Grocery Nutrition Agent"
    )
    parser.add_argument("--ids", nargs="*", help="Row IDs to run (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Skip Arize logging")
    parser.add_argument(
        "--no-llm-judges",
        action="store_true",
        help="Skip LLM-as-judge (faster, cheaper)",
    )
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default=None,
        help="Arize tracing environment. Defaults to ARIZE_ENV env var (fallback: dev).",
    )
    args = parser.parse_args()

    setup_tracing(env=args.env)
    rows = load_dataset(args.ids)
    if not rows:
        print("No rows found in dataset (check --ids filter).")
        sys.exit(1)

    llm_client = None if args.no_llm_judges else anthropic.Anthropic()

    print(f"\nRunning evals on {len(rows)} rows…")
    print("=" * 70)

    all_scores: dict[str, list[float]] = {name: [] for name in EVALUATORS}
    errors: list[str] = []

    for row in rows:
        row_id = row.get("id", "?")
        url = row["url"]
        profile_raw = row.get("profile", {})

        print(f"\nRow {row_id}: {url[:70]}")
        try:
            profile = build_profile(profile_raw)
            t0 = time.time()
            recommendation = run_pipeline(url, profile)
            elapsed = time.time() - t0
            print(
                f"  Pipeline: {elapsed:.1f}s | Category: {recommendation.category.value}"
            )

            prediction = recommendation.model_dump(mode="json")
            results = evaluate_all(list(EVALUATORS), prediction, row, client=llm_client)
            print_row_result(row_id, url, results)

            for judge, result in results.items():
                all_scores[judge].append(result.score)

            if not args.dry_run and recommendation.request_id:
                log_to_arize(row_id, recommendation.request_id, results)

        except Exception as exc:
            print(f"  ERROR: {exc}")
            errors.append(f"Row {row_id}: {exc}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for judge, scores in all_scores.items():
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        c = score_color(avg)
        print(f"  {judge:<28} avg={c}{avg:.3f}{RESET}  (n={len(scores)})")

    if errors:
        print(f"\n{len(errors)} row(s) failed:")
        for e in errors:
            print(f"  • {e}")

    total_rows = len(rows)
    success_rows = total_rows - len(errors)
    print(f"\nCompleted {success_rows}/{total_rows} rows.")


if __name__ == "__main__":
    main()
