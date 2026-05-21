# Agent Instructions — Grocery Nutrition Agent

This file provides guidance for AI coding agents working in this repository.

---

## Updating Prompts

Prompts live in `nutrition_agent/prompts/` — one file per pipeline stage:

```
nutrition_agent/prompts/
  extract.py     ← Stage 1: scrape + parse product page
  analyze.py     ← Stage 2: score nutrition, flag ingredients
  recommend.py   ← Stage 4: final verdict + evidence + alternatives
  cart.py        ← Cart synthesis: balance score + swap suggestions
  registry.py    ← assembles PROMPT_REGISTRY + get_prompt / active_version
  __init__.py    ← re-exports constants used by the pipeline
```

### Rules

1. **Never edit an existing version.** Previous versions are the audit history. Always add a new version entry.
2. **One file per prompt.** Changes to the extract prompt go in `extract.py` only.
3. **Flip `active` to activate.** The pipeline picks up the change automatically — no other files need editing.
4. **Write a `notes` field.** Explain what changed and why (one sentence is enough).

### How to add a new version

Edit the relevant prompt file (e.g. `nutrition_agent/prompts/extract.py`):

```python
ENTRY = {
    "active": "v2",          # ← flip to the new version
    "versions": {
        "v1": { ... },       # ← leave v1 untouched
        "v2": {
            "created": "YYYY-MM-DD",
            "notes": "One sentence describing what changed and why.",
            "content": """\
<new prompt text here>
""",
        },
    },
}
```

That's it. The constants `EXTRACT_SYSTEM`, `ANALYZE_SYSTEM`, etc. are derived
from the active versions at import time, so the pipeline uses the new prompt
immediately without any further changes.

### How to inspect the registry at runtime

```python
from nutrition_agent.prompts import PROMPT_REGISTRY, active_version, get_prompt

active_version("extract")          # -> "v1"
get_prompt("extract")              # -> active version content
get_prompt("extract", "v1")        # -> specific version content
list(PROMPT_REGISTRY["extract"]["versions"])  # -> ["v1", ...]
```

### Evals after a prompt change

After bumping a prompt version, run the eval suite to measure the impact:

```bash
python evals/run_evals.py --no-llm-judges   # fast, deterministic judges only
python evals/run_evals.py                   # full suite including LLM-as-judge
```

Eval results are logged to Arize keyed by `request_id` so you can compare
traces from the old and new versions side by side in the Arize AX UI.
