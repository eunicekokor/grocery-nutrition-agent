# gRPC probe fixtures

Each `.json` file here is a complete `EvalEntityRequest` body
(`evals/remote_models.py`) used by `scripts/test_remote_endpoint.sh` as the
`request_body` sent through `generative.LlmGenerateService/TestRemoteEndpoint`.

Fixtures are organized into three groups that map to the `--mode` flag:

| Mode | Directory | Judge type | Needs |
|---|---|---|---|
| `reference_free` (default) | `fixtures/reference_free/` | LLM-as-judge | `ANTHROPIC_API_KEY` on the server |
| `golden` | `fixtures/golden/` | Deterministic | `expected` ground truth in fixture |
| `root` | `fixtures/` | Utility / error cases | — |

---

## Usage

```bash
export BEARER_TOKEN="$REMOTE_EVAL_TOKEN"
export EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY="<32-byte-key>"

# List cases for each mode
./scripts/test_remote_endpoint.sh --list                        # reference_free (default)
./scripts/test_remote_endpoint.sh --mode golden --list
./scripts/test_remote_endpoint.sh --mode root --list

# Reference-free LLM judge probes
./scripts/test_remote_endpoint.sh groundedness_basic
./scripts/test_remote_endpoint.sh --mode reference_free helpfulness_weak

# Golden / reference-based judge probes
./scripts/test_remote_endpoint.sh --mode golden 001_category
./scripts/test_remote_endpoint.sh --mode golden 021_allergen

# Utility probes (error cases, minimal payloads)
./scripts/test_remote_endpoint.sh --mode root unknown_eval
```

---

## Mode: `reference_free` — LLM-as-judge evaluators

These fixtures test `evidence_groundedness` and `helpfulness`. They carry no
`expected` field — judges score the prediction on its own merits using Claude.

> **Requirement:** `ANTHROPIC_API_KEY` must be set in the server's environment
> (Railway env vars, local `.env`, etc.). Without it the server logs
> `"LLM judges will return label='skipped'"` and every result comes back
> `label: skipped`.

| Case id | Evaluator | Scenario | Expected judge outcome |
|---|---|---|---|
| `groundedness_basic` | `evidence_groundedness` | All three evidence claims are directly supported by `product_facts` | High groundedness score (≥ 0.7) |
| `groundedness_hallucination` | `evidence_groundedness` | Claims directly contradict `product_facts` (sodium 50 mg vs actual 1080 mg; organic claim false) | Low groundedness score (≤ 0.4) |
| `helpfulness_strong` | `helpfulness` | Specific, fact-cited rationale with protein-to-calorie ratio, exact numbers | High helpfulness score (4–5 / 5) |
| `helpfulness_weak` | `helpfulness` | Vague boilerplate rationale with no product facts cited | Low helpfulness score (1–2 / 5) |

---

## Mode: `golden` — Reference-based / deterministic evaluators

These fixtures test `category_correctness`, `dietary_safety`, and
`allergen_recall`. They include an `expected` field carrying ground truth so
the judge can return a meaningful `correct`/`incorrect`/`flagged`/`missed` label.

| Case id | Evaluator | Scenario | `expected` field | Expected judge outcome |
|---|---|---|---|---|
| `001_category` | `category_correctness` | `MORE_OF` prediction for oats | `{"expected_category": "MORE_OF"}` | `label: correct` |
| `003_dietary` | `dietary_safety` | SPAM with halal/pork conflict flagged | `{"expected_dietary_conflicts": [{"restriction": "halal", "ingredient": "pork"}]}` | `label: pass` (conflict correctly flagged) |
| `021_allergen` | `allergen_recall` | Fresh avocado — allergen in product name | `{"profile": {"allergens": ["avocado"]}}` | `label: flagged` (allergen correctly caught) |
| `022_allergen` | `allergen_recall` | Justin's Almond Butter — tree nut | `{"profile": {"allergens": ["tree nuts"]}}` | `label: flagged` (tree nut correctly caught) |

---

## Mode: `root` — Utility / error cases

Mode-agnostic fixtures for wiring and error-handling probes.

| Case id | Evaluator | Scenario | Expected outcome |
|---|---|---|---|
| `unknown_eval` | `not_a_real_judge` | Invalid evaluator name | HTTP 400 in RPC response body |
| `minimal` | `category_correctness` | Required fields only, no `expected` | `label: no_ground_truth` |
| `001_pass` | `category_correctness` | Rich prediction, no `expected` | `label: no_ground_truth` — plumbing test |
| `003_halal` | `dietary_safety` | SPAM payload, no `expected` | `label: pass` (no ground truth to compare) |
| `021_avocado` | `allergen_recall` | Avocado payload, no `expected` | `label: pass` (no profile to compare) |
| `022_almonds` | `allergen_recall` | Almonds payload, no `expected` | `label: pass` (no profile to compare) |

---

## Adding a new fixture

1. Decide which mode your fixture belongs to and create it in the corresponding directory:
   - `scripts/fixtures/reference_free/<case_id>.json` — LLM judges (no `expected`)
   - `scripts/fixtures/golden/<case_id>.json` — deterministic judges (include `expected`)
   - `scripts/fixtures/<case_id>.json` — utility / mode-agnostic

2. Required fields: `request_id`, `evaluator`, `prediction`.

3. Add a row to this README.

### `EvalEntityRequest` schema

```jsonc
{
  "request_id": "string",                 // echoed in response
  "evaluator": "string",                  // e.g. "evidence_groundedness"
  "prediction": { ... },                  // structured agent output
  "attributes": { ... },                  // optional span/trace metadata
  "expected": {                           // golden mode only; omit for reference_free
    "expected_category": "MORE_OF",       // category_correctness
    "expected_dietary_conflicts": [...],  // dietary_safety
    "profile": { "allergens": [...] }     // allergen_recall
  }
}
```
