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
| `evidence_groundedness` | LLM-as-judge | Each evidence item is traceable to the product facts (hallucination check) |
| `helpfulness` | LLM-as-judge | Rationale quality rated 1–5 given the user profile and product |

Eval results are logged back to Arize as evaluations attached to the original trace spans, keyed by `request_id`.

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
