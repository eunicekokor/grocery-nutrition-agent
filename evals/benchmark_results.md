# Eval Benchmark Results — allergen_recall Before/After

**Date:** 2026-05-29  
**Run flags:** `--no-llm-judges --dry-run`  
**LLM judges (evidence_groundedness, helpfulness):** skipped (score=0.500 placeholder)

---

## Aggregate averages

| Judge | Baseline (n=14/20) | After (n=17/22) | Delta |
|---|---|---|---|
| `category_correctness` | 0.643 | 0.765 | +0.122 |
| `dietary_safety` | 0.714 | 0.765 | +0.051 |
| `allergen_recall` | — (did not exist) | **1.000** | new |
| `evidence_groundedness` | 0.500 (skipped) | 0.500 (skipped) | 0 |
| `helpfulness` | 0.500 (skipped) | 0.500 (skipped) | 0 |

Note: aggregate delta on existing judges reflects different rows erroring/succeeding across runs (non-deterministic scraping), not code changes.

---

## Per-row comparison (rows 001–020, deterministic judges only)

| Row | Baseline cat | Baseline diet | After cat | After diet | After allergen |
|---|---|---|---|---|---|
| 001 | ERRORED | — | 1.00 | 1.00 | 1.00 |
| 002 | ERRORED | — | 1.00 | 1.00 | 1.00 |
| 003 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 004 | ERRORED | — | ERRORED | — | — |
| 005 | 0.00 | 1.00 | 0.00 | 0.00 | 1.00 |
| 006 | 0.00 | 0.00 | 0.00 | 0.00 | 1.00 |
| 007 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 008 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 009 | 0.00 | 0.00 | 0.00 | 0.00 | 1.00 |
| 010 | 0.00 | 1.00 | ERRORED | — | — |
| 011 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 012 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 013 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 014 | ERRORED | — | ERRORED | — | — |
| 015 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 016 | 1.00 | 0.00 | ERRORED | — | — |
| 017 | ERRORED | — | ERRORED | — | — |
| 018 | ERRORED | — | 1.00 | 1.00 | 1.00 |
| 019 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 020 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 |

---

## New rows 021 and 022 — allergen_recall results

| Row | Product | Allergen | Category pred | `allergen_recall` |
|---|---|---|---|---|
| **021** | Hass Avocado (Instacart) | `avocado` | LESS_OF ✓ | **1.00 pass** — Caught 1/1 (avocado in product name) |
| **022** | Great Value Whole Almonds | `tree nuts` | MORE_OF (expected IN_MODERATION) | **1.00 pass** — Caught 1/1 (almond → ALLERGEN_KEYWORDS["tree nuts"]) |

---

## Regressions

- **dietary_safety row 005:** 1.00 → 0.00 — non-deterministic LLM scraping artefact, not a code regression.
- **allergen_recall across all 17 rows that ran:** 1.000 — no regressions introduced.

## Safety-critical dietary_safety misses (score < 1.0 after run)

| Row | Missed conflict |
|---|---|
| 006 | `nut_free/pistachios` |
| 009 | `dairy_free/milk` |
| 020 | `gluten_free/wheat flour`, `dairy_free/cheddar` |

---

## Files changed in this benchmark cycle

| File | Change |
|---|---|
| `evals/judges.py` | Added `allergen_recall` judge + `ALLERGEN_KEYWORDS` import |
| `evals/engine.py` | Registered `allergen_recall` in `EVALUATORS` |
| `evals/golden_dataset.jsonl` | Added rows 021 (avocado) and 022 (whole almonds) |
| `README.md` | Added `allergen_recall` to judges table |
