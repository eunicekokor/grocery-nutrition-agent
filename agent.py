"""
Grocery Nutrition Agent — CLI entry point.
Delegates all logic to nutrition_agent.pipeline.

Usage:
    python agent.py              # uses ARIZE_ENV (default: dev)
    python agent.py --env dev
    python agent.py --env prod
"""

import argparse
import sys

from nutrition_agent.pipeline import run_pipeline
from nutrition_agent.schemas import UserProfile
from nutrition_agent.tracing import setup_tracing


def main() -> None:
    parser = argparse.ArgumentParser(description="Grocery Nutrition Agent CLI")
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default=None,
        help="Arize tracing environment. Defaults to ARIZE_ENV env var (fallback: dev).",
    )
    args = parser.parse_args()

    setup_tracing(env=args.env)

    print("Grocery Nutrition Agent")
    print("=" * 40)
    url = input("Paste a grocery item URL: ").strip()
    if not url:
        print("No URL provided.")
        sys.exit(1)

    print("\nAnalyzing...\n")
    recommendation = run_pipeline(url, UserProfile())
    print(recommendation.headline)
    print()
    print(recommendation.rationale)

    if recommendation.personal_fit and recommendation.personal_fit.dietary_conflicts:
        print("\n⚠️  Dietary conflicts:")
        for c in recommendation.personal_fit.dietary_conflicts:
            print(f"  • {c.restriction}: {c.ingredient}")

    if recommendation.alternatives:
        print("\nAlternatives to consider:")
        for alt in recommendation.alternatives:
            print(f"  • {alt.name} — {alt.why}")


if __name__ == "__main__":
    main()
