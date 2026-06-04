"""
Run the full grocery-nutrition-agent eval suite as an Arize experiment.

Usage:
    # Export dataset, run pipeline + judges, upload experiment (all in one)
    python evals/run_experiment.py | ax experiments create \\
        --name "grocery-eval-$(date +%Y%m%d)" \\
        --dataset RGF0YXNldDozNDgxNzY6dTduZw== \\
        --space U3BhY2U6NDE5NDE6NFc0eQ== \\
        --file -

    # Or write runs to a file first (for inspection)
    python evals/run_experiment.py --output runs.json
    ax experiments create --name "my-run" --dataset ... --file runs.json

Options:
    --no-llm-judges     Skip evidence_groundedness and helpfulness (cheaper)
    --ids 001 002 ...   Only process specific example_ids
    --output FILE       Write runs JSON to FILE instead of stdout
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root))

import anthropic

from nutrition_agent.pipeline import run_pipeline
from nutrition_agent.schemas import DietaryRestriction, HealthGoal, UserProfile
from evals.engine import EVALUATORS, evaluate_all

DATASET_ID = "RGF0YXNldDozNDgxNzY6dTduZw=="
SPACE_ID = "U3BhY2U6NDE5NDE6NFc0eQ=="


def _load_examples(ids_filter: list[str] | None = None) -> list[dict]:
    """Export dataset examples from Arize via ax CLI."""
    import subprocess

    result = subprocess.run(
        [
            "ax", "datasets", "export", DATASET_ID,
            "--space", SPACE_ID,
            "--stdout",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"[error] ax datasets export failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    examples = json.loads(result.stdout)

    if ids_filter:
        examples = [
            ex for ex in examples
            if str(ex.get("additional_properties", {}).get("example_id", "")) in ids_filter
        ]
    return examples


def _parse_example(ex: dict) -> tuple[str, str, dict, UserProfile]:
    """Extract (server_id, url, expected, profile) from a dataset example."""
    server_id = ex["id"]
    props = ex.get("additional_properties", {})

    url = props.get("url", "")

    raw_profile = props.get("profile", "{}")
    if isinstance(raw_profile, str):
        raw_profile = json.loads(raw_profile)

    profile = UserProfile(
        dietary_restrictions=[
            DietaryRestriction(r)
            for r in raw_profile.get("dietary_restrictions", [])
            if r in DietaryRestriction._value2member_map_
        ],
        allergens=raw_profile.get("allergens", []),
        health_goals=[
            HealthGoal(g)
            for g in raw_profile.get("health_goals", [])
            if g in HealthGoal._value2member_map_
        ],
        daily_sodium_mg_target=raw_profile.get("daily_sodium_mg_target"),
        daily_sugar_g_target=raw_profile.get("daily_sugar_g_target"),
    )

    expected = {
        "example_id": props.get("example_id"),
        "expected_category": props.get("expected_category"),
        "profile": raw_profile,
    }

    raw_conflicts = props.get("expected_dietary_conflicts", "[]")
    if isinstance(raw_conflicts, str):
        raw_conflicts = json.loads(raw_conflicts)
    expected["expected_dietary_conflicts"] = raw_conflicts

    return server_id, url, expected, profile


def main() -> None:
    parser = argparse.ArgumentParser(description="Run grocery-nutrition-agent eval experiment")
    parser.add_argument("--no-llm-judges", action="store_true", help="Skip LLM-as-judge evaluators")
    parser.add_argument("--ids", nargs="*", help="Filter by example_id values (e.g. 1 2 3)")
    parser.add_argument("--output", default=None, help="Write runs JSON to this file (default: stdout)")
    args = parser.parse_args()

    llm_client = None
    if not args.no_llm_judges:
        try:
            llm_client = anthropic.Anthropic()
        except Exception:
            print("[warn] Could not init Anthropic client — LLM judges will be skipped", file=sys.stderr)

    judges = list(EVALUATORS.keys())
    if args.no_llm_judges:
        judges = [j for j in judges if j not in {"evidence_groundedness", "helpfulness"}]

    print(f"[info] Exporting dataset {DATASET_ID}…", file=sys.stderr)
    examples = _load_examples(args.ids)
    print(f"[info] {len(examples)} examples loaded. Running judges: {judges}", file=sys.stderr)

    runs = []

    for ex in examples:
        server_id, url, expected, profile = _parse_example(ex)
        example_id = expected.get("example_id", "?")
        print(f"[info] [{example_id}] {url[:70]}", file=sys.stderr, end=" ... ")

        t0 = time.time()
        try:
            recommendation = run_pipeline(url, profile)
            elapsed = round((time.time() - t0) * 1000)
            prediction = recommendation.model_dump(mode="json")
            print(f"{recommendation.category.value} ({elapsed}ms)", file=sys.stderr)
        except Exception as exc:
            elapsed = round((time.time() - t0) * 1000)
            print(f"ERRORED: {exc}", file=sys.stderr)
            runs.append({
                "example_id": server_id,
                "output": f"ERROR: {exc}",
                "metadata": {"example_id": example_id, "latency_ms": elapsed, "error": True},
            })
            continue

        # Run judges
        eval_results = evaluate_all(judges, prediction, expected, client=llm_client)

        evaluations = {
            name: {
                "label": result.label,
                "score": result.score,
                "explanation": result.explanation,
            }
            for name, result in eval_results.items()
        }

        runs.append({
            "example_id": server_id,
            "output": json.dumps({
                "category": recommendation.category.value,
                "headline": recommendation.headline,
                "rationale": recommendation.rationale[:300] if recommendation.rationale else "",
            }),
            "evaluations": evaluations,
            "metadata": {
                "example_id": str(example_id),
                "url": url,
                "latency_ms": elapsed,
                "request_id": recommendation.request_id or "",
            },
        })

    print(f"\n[info] {len(runs)} runs assembled ({sum(1 for r in runs if r.get('metadata',{}).get('error'))} errors)", file=sys.stderr)

    payload = json.dumps(runs, indent=2)
    if args.output:
        Path(args.output).write_text(payload)
        print(f"[info] Written to {args.output}", file=sys.stderr)
    else:
        print(payload)


if __name__ == "__main__":
    main()
