# Grocery Nutrition Agent

A production-grade AI agent that analyzes grocery product pages and returns a nutritionist-style recommendation: **MORE OF**, **IN MODERATION**, or **LESS OF**.

The agent runs a 4-stage pipeline (extract → analyze → personalize → recommend), exposes a chat UI, and includes a full eval harness wired into Arize AX.

---

## Features

- **Multi-stage pipeline** — each stage returns a typed Pydantic model, enabling deterministic evals
- **5 tools** — web scraper, USDA FoodData Central lookup, deterministic ingredient classifier, dietary compatibility checker, and LLM-powered alternative finder
- **Personalization** — user profile (dietary restrictions, allergens, health goals, daily targets) is a first-class input carried through every stage
- **Cart mode** — analyze multiple products at once and get a cart balance score with swap suggestions
- **Arize AX tracing** — every stage and tool call is wrapped in an OpenInference span; dev and prod environments send to separate Arize spaces
- **Eval harness** — 20-row golden dataset with 4 judges (2 deterministic, 2 LLM-as-judge) that log results back to Arize

---

## Project structure

```
grocery-nutrition-agent/
  agent.py                    # CLI entry point
  server.py                   # FastAPI server (chat UI + API)
  requirements.txt
  .env.example
  nutrition_agent/
    schemas.py                # Pydantic models (the eval contract)
    tools.py                  # All 5 tools + Claude-compatible schemas
    prompts.py                # Per-stage system prompts (structured JSON output)
    pipeline.py               # 4-stage orchestrator
    tracing.py                # OTEL setup, @traced decorator, session attributes
  evals/
    golden_dataset.jsonl      # 20 hand-curated test cases
    judges.py                 # 4 eval judges
    run_evals.py              # CLI eval runner
  static/
    index.html                # Chat UI (profile drawer, cart mode)
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

`.env` variables:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key |
| `ARIZE_ENV` | No | `dev` or `prod` (default: `dev`) |
| `ARIZE_SPACE_ID_DEV` | For dev tracing | Arize space ID for the dev environment |
| `ARIZE_API_KEY_DEV` | For dev tracing | Arize API key for the dev environment |
| `ARIZE_SPACE_ID_PROD` | For prod tracing | Arize space ID for the prod environment |
| `ARIZE_API_KEY_PROD` | For prod tracing | Arize API key for the prod environment |

The bare `ARIZE_SPACE_ID` / `ARIZE_API_KEY` variables are supported as fallbacks if the env-suffixed ones are not set.

### 3. Source the env file

```bash
source .env
```

Or use a tool like `direnv` to load it automatically.

---

## Running the server

```bash
python server.py
# visit http://localhost:8000
```

The server reads `ARIZE_ENV` on startup to decide which Arize space to send traces to.

To override:

```bash
ARIZE_ENV=prod python server.py
```

---

## Tracing environments

| Environment | Credentials | OTEL endpoint |
|---|---|---|
| `dev` | `ARIZE_SPACE_ID_DEV` + `ARIZE_API_KEY_DEV` | `https://devotlp.arize.com/v1` |
| `prod` | `ARIZE_SPACE_ID_PROD` + `ARIZE_API_KEY_PROD` | Arize default cloud endpoint |

The environment can be set three ways (highest priority first):

1. `--env dev|prod` flag (CLI and eval runner)
2. `ARIZE_ENV` environment variable
3. Defaults to `dev`

---

## CLI usage

```bash
# Uses ARIZE_ENV (default: dev)
python agent.py

# Explicitly target dev or prod Arize space
python agent.py --env dev
python agent.py --env prod
```

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Chat UI |
| `POST` | `/chat` | Analyze a single product URL |
| `POST` | `/cart` | Analyze multiple URLs as a cart |
| `GET` | `/trace/{request_id}` | Returns the Arize trace URL for a request |
| `POST` | `/evals/run` | Kick off the eval runner (dev only) |

### `POST /chat`

```json
{
  "url": "https://www.wholefoodsmarket.com/product/...",
  "profile": {
    "dietary_restrictions": ["vegan", "gluten_free"],
    "allergens": ["sesame"],
    "health_goals": ["high_fiber", "heart_health"],
    "daily_sodium_mg_target": 1500,
    "daily_sugar_g_target": 40
  }
}
```

Returns a full `Recommendation` object (category, headline, rationale, evidence, personal fit, alternatives).

### `POST /cart`

```json
{
  "urls": ["https://...", "https://..."],
  "profile": { ... }
}
```

Returns a `CartRecommendation` with per-item recommendations, a cart balance score (0–100), and swap suggestions.

---

## Running evals

```bash
# Run all 20 rows against the dev Arize space
python evals/run_evals.py

# Run against prod
python evals/run_evals.py --env prod

# Run a subset of rows
python evals/run_evals.py --ids 001 002 003

# Skip Arize logging (dry run)
python evals/run_evals.py --dry-run

# Skip LLM-as-judge evaluators (faster, no Anthropic cost)
python evals/run_evals.py --no-llm-judges
```

### Judges

| Judge | Type | What it checks |
|---|---|---|
| `category_correctness` | Deterministic | Exact match between predicted and expected category |
| `dietary_safety` | Deterministic | Recall of expected dietary conflicts (safety-critical) |
| `allergen_recall` | Deterministic | Recall of allergen warnings for every profile allergen present in product (no annotation needed) |
| `evidence_groundedness` | LLM-as-judge | Each evidence item is traceable to the product facts (hallucination check) |
| `helpfulness` | LLM-as-judge | Rationale quality rated 1–5 given the user profile and product |

Eval results are logged back to Arize as evaluations attached to the original trace spans, keyed by `request_id`.

---

## Remote eval API mimic (`/evals/v1`)

This agent exposes a customer-shaped evaluation service under `/evals/v1` that mimics the patterns Arize customers use in production. Each endpoint represents a bucket from the PM synthesis:

| PM bucket | What it mimics here |
|---|---|
| 1 — Compliance / in-VPC | `REMOTE_EVAL_TOKEN` bearer gate; mTLS/IAM is an ingress concern |
| 2 — Proprietary scorers | `response_style: "terse"` returns `score + label` only, no explanation |
| 3 — Framework-integrated | Named evaluator routing — add a new entry to `evals/engine.py` `EVALUATORS` to plug in a DeepEval or Ragas scorer |
| 4 — Competitive parity | Stable versioned paths (`/evals/v1/...`) with OpenAPI schema visible at `/docs` |
| 5 — BYO flexibility | Generic `prediction + expected + extras` envelope; `X-Eval-Sample-Rate` and `X-Eval-Max-Retries` headers |

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/evals/v1/evaluators` | List registered evaluators |
| `POST` | `/evals/v1/evaluate` | Run a single named evaluator |
| `POST` | `/evals/v1/batch` | Run multiple evaluators on one prediction |

### Auth (bucket 1)

Set `REMOTE_EVAL_TOKEN` in the environment to enable token gating:

```bash
REMOTE_EVAL_TOKEN=my-secret python server.py
```

Requests must then include one of:

```
Authorization: Bearer my-secret
X-Remote-Eval-Token: my-secret
```

When `REMOTE_EVAL_TOKEN` is unset, all `/evals/v1/*` endpoints are open (dev default).

### `GET /evals/v1/evaluators`

```bash
curl http://localhost:8000/evals/v1/evaluators
# {"evaluators":["category_correctness","dietary_safety","evidence_groundedness","helpfulness"],
#  "llm_judges":["evidence_groundedness","helpfulness"],
#  "deterministic":["category_correctness","dietary_safety"]}
```

### `POST /evals/v1/evaluate`

Single evaluator. Use `response_style: "terse"` for the proprietary-scorer shape (bucket 2):

```bash
curl -X POST http://localhost:8000/evals/v1/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "evaluator": "category_correctness",
    "prediction": {"category": "MORE_OF"},
    "expected": {"expected_category": "MORE_OF"},
    "response_style": "terse"
  }'
# {"evaluator":"category_correctness","score":1.0,"label":"correct","confidence":null,"explanation":"","metadata":{}}
```

Full response (default):

```bash
curl -X POST http://localhost:8000/evals/v1/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "evaluator": "dietary_safety",
    "prediction": {"personal_fit": {"dietary_conflicts": []}},
    "expected": {"expected_dietary_conflicts": []}
  }'
# {"evaluator":"dietary_safety","score":1.0,"label":"pass","confidence":null,
#  "explanation":"No expected dietary conflicts — trivially safe.","metadata":{}}
```

### `POST /evals/v1/batch`

Multiple evaluators in one call. Deterministic-only is safe to run without Anthropic credits:

```bash
curl -X POST http://localhost:8000/evals/v1/batch \
  -H "Content-Type: application/json" \
  -d '{
    "evaluators": ["category_correctness", "dietary_safety"],
    "prediction": {"category": "IN_MODERATION", "personal_fit": {"dietary_conflicts": []}},
    "expected": {"expected_category": "IN_MODERATION", "expected_dietary_conflicts": []}
  }'
# {"results":[...], "evaluated":2, "skipped":0}
```

### Sample-rate and retries (bucket 5)

```bash
# Only execute ~50% of incoming eval requests (use for high-volume online evals)
curl -X POST http://localhost:8000/evals/v1/evaluate \
  -H "X-Eval-Sample-Rate: 0.5" \
  -H "Content-Type: application/json" \
  -d '{"evaluator":"category_correctness","prediction":{"category":"MORE_OF"},"expected":{"expected_category":"MORE_OF"}}'

# Retry LLM-as-judge up to 3 times on transient failure
curl -X POST http://localhost:8000/evals/v1/evaluate \
  -H "X-Eval-Max-Retries: 3" \
  -H "Content-Type: application/json" \
  -d '{"evaluator":"evidence_groundedness","prediction":{...},"expected":{}}'
```

### Adding a new evaluator (framework integration, bucket 3)

Register it in `evals/engine.py`:

```python
from my_deepeval_wrapper import my_custom_judge  # any callable: (prediction, expected) -> EvalResult

EVALUATORS["my_custom_judge"] = my_custom_judge
```

It is then immediately available at `/evals/v1/evaluate` and `/evals/v1/batch` with no other changes needed.

---

## Pipeline stages

```
URL + UserProfile
  │
  ▼
stage_extract    → ProductFacts         (Claude + fetch_product_info tool)
  │
  ▼
stage_analyze    → NutritionAnalysis    (Claude + lookup_nutrition_database + analyze_ingredients)
  │
  ▼
stage_personalize → PersonalFit        (deterministic rules — no LLM)
  │
  ▼
stage_recommend  → Recommendation      (Claude + find_alternatives tool)
```

Stage 3 (`personalize`) is fully deterministic — it runs dietary restriction rules and allergen matching against curated ingredient lists with no LLM call. This makes the dietary safety eval reliable and cheap.
