"""
Multi-stage orchestration pipeline for the Grocery Nutrition Agent.

Stages:
  1. extract   — fetch + parse product page -> ProductFacts
  2. analyze   — nutrition scoring + ingredient analysis -> NutritionAnalysis
  3. personalize — deterministic rules -> PersonalFit
  4. recommend — final verdict with evidence + alternatives -> Recommendation

Each stage runs a Claude tool-use loop and returns a typed Pydantic model.
Every stage is wrapped in an OpenInference CHAIN span for Arize tracing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# Braintrust tracing: init the logger (project "Grocery") and enable
# auto-instrumentation before any AI library is imported or a client created.
import braintrust

braintrust.init_logger(project="Grocery")
braintrust.auto_instrument()

import anthropic

from .prompts import ANALYZE_SYSTEM, CART_SYSTEM, EXTRACT_SYSTEM, RECOMMEND_SYSTEM
from .schemas import (
    CartRecommendation,
    DietaryConflict,
    FlaggedIngredient,
    NutritionAnalysis,
    PersonalFit,
    AllergenWarning,
    ProductFacts,
    Recommendation,
    RecommendationCategory,
    Severity,
    SwapSuggestion,
    UserFacingError,
    UserProfile,
)
from .tools import (
    ALLERGEN_KEYWORDS,
    ANALYZE_TOOLS,
    EXTRACT_TOOLS,
    RECOMMEND_TOOLS,
    check_dietary_compatibility,
    dispatch_tool,
)
from .tracing import get_tracer, set_session_attributes, traced, using_session

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024


# ---------------------------------------------------------------------------
# Shared LLM tool-use loop
# ---------------------------------------------------------------------------


def _run_tool_loop(
    client: anthropic.Anthropic,
    system: str,
    user_message: str,
    tools: list[dict],
    max_tokens: int = MAX_TOKENS,
) -> str:
    """
    Run a Claude tool-use loop. Returns the final assistant text content.
    Callers are responsible for wrapping in spans.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )

        text_blocks = [b.text for b in response.content if hasattr(b, "text")]

        if response.stop_reason == "end_turn":
            return "\n".join(text_blocks).strip()

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(
                        f"[pipeline] tool_call: {block.name}({json.dumps(block.input)[:120]})"
                    )
                    result = dispatch_tool(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
        else:
            return "\n".join(text_blocks).strip() or f"Stopped: {response.stop_reason}"


def _parse_json_output(raw: str, stage: str) -> dict:
    """
    Extract a JSON object from a Claude response.

    Handles three common cases:
      1. Raw JSON with no wrapping (ideal).
      2. ```json ... ``` or ``` ... ``` block anywhere in the response —
         the model sometimes adds prose before/after the fence.
      3. The first {...} or [...] span in the response as a last resort.
    """
    raw = raw.strip()

    # Case 2: find a fenced code block anywhere in the output
    import re

    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", raw)
    if fence_match:
        raw = fence_match.group(1).strip()

    # Case 1: try parsing directly (works whether we just extracted from a
    # fence or the model returned clean JSON from the start)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Case 3: scan for the first top-level { ... } block in the full response
    brace_match = re.search(r"\{[\s\S]*\}", raw)
    if brace_match:
        try:
            return json.loads(brace_match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Stage '{stage}' returned invalid JSON.\nRaw output:\n{raw}")


# ---------------------------------------------------------------------------
# Stage 1: Extract
# ---------------------------------------------------------------------------


@traced(name="stage_extract", kind="CHAIN")
def stage_extract(client: anthropic.Anthropic, url: str) -> ProductFacts:
    raw = _run_tool_loop(
        client,
        EXTRACT_SYSTEM,
        f"Extract product facts from this URL: {url}",
        EXTRACT_TOOLS,
    )
    # Guard: detect prose/clarification responses before attempting JSON parse.
    # A valid response starts with '{' or a fenced code block; anything else
    # that is short and contains a '?' is almost certainly a clarification ask.
    stripped = raw.strip()
    if not stripped.startswith(("{", "`")) and "?" in stripped and len(stripped) < 300:
        raise ValueError(
            f"Extract stage: model asked for clarification instead of returning JSON. "
            f"URL was: {url}"
        )
    data = _parse_json_output(raw, "extract")
    data["source_url"] = url
    if not data.get("name"):
        raise UserFacingError(
            "That URL doesn't look like a product page — no product name could be "
            "found. Please paste a direct link to a specific grocery item "
            "(e.g. a Target, Whole Foods, or Kroger product page)."
        )
    return ProductFacts(**data)


# ---------------------------------------------------------------------------
# Stage 2: Analyze
# ---------------------------------------------------------------------------


@traced(name="stage_analyze", kind="CHAIN")
def stage_analyze(
    client: anthropic.Anthropic, facts: ProductFacts
) -> NutritionAnalysis:
    facts_json = facts.model_dump_json(indent=2)
    raw = _run_tool_loop(
        client,
        ANALYZE_SYSTEM,
        f"Analyze the nutritional profile of this product:\n\n{facts_json}",
        ANALYZE_TOOLS,
    )
    data = _parse_json_output(raw, "analyze")

    # Coerce flagged_ingredients to typed models
    flagged = [
        FlaggedIngredient(
            ingredient=f.get("ingredient", ""),
            reason=f.get("reason", ""),
            severity=Severity(f.get("severity", "medium")),
        )
        for f in data.pop("flagged_ingredients", [])
    ]
    return NutritionAnalysis(flagged_ingredients=flagged, **data)


# ---------------------------------------------------------------------------
# Stage 3: Personalize (deterministic — no LLM)
# ---------------------------------------------------------------------------


@traced(name="stage_personalize", kind="CHAIN")
def stage_personalize(
    facts: ProductFacts,
    analysis: NutritionAnalysis,
    profile: UserProfile,
) -> PersonalFit:
    if (
        not profile.dietary_restrictions
        and not profile.allergens
        and not profile.health_goals
    ):
        return PersonalFit(overall_fit_score=100)

    compat_json = check_dietary_compatibility(
        facts.ingredients,
        facts.claims,
        [r.value for r in profile.dietary_restrictions],
        profile.allergens,
    )
    compat = json.loads(compat_json)

    conflicts = [
        DietaryConflict(
            restriction=c["restriction"],
            ingredient=c["ingredient"],
            reason=c["reason"],
        )
        for c in compat.get("conflicts", [])
    ]

    warnings = [
        AllergenWarning(
            allergen=w["allergen"],
            found_in=w["found_in"],
            certainty=Severity(w.get("certainty", "medium")),
        )
        for w in compat.get("allergen_warnings", [])
    ]

    # Whole-product allergen check — catches cases where the product itself IS
    # the allergen (e.g. avocado, eggs, tree nuts) and the ingredient list is
    # empty (common with login-walled pages like Instacart).
    product_text = f"{facts.name} {facts.brand or ''}".lower()
    already_warned = {w.allergen.lower() for w in warnings}
    for allergen in profile.allergens:
        allergen_lower = allergen.lower()
        if allergen_lower in already_warned:
            continue
        known_keywords = ALLERGEN_KEYWORDS.get(allergen_lower, [allergen_lower])
        if any(kw in product_text for kw in known_keywords) or allergen_lower in product_text:
            warnings.append(
                AllergenWarning(
                    allergen=allergen,
                    found_in=facts.name,
                    certainty=Severity("high"),
                )
            )

    # Goal alignment scoring (deterministic heuristics)
    goal_alignment: dict[str, int] = {}
    macros = analysis.macros_summary
    score = analysis.nutrition_score

    for goal in profile.health_goals:
        g = goal.value
        if g == "weight_loss":
            cal_str = facts.nutrition.get("calories", "0")
            cal = _extract_number(cal_str)
            goal_alignment[g] = max(0, 100 - max(0, (cal - 150) * 2)) if cal else score
        elif g == "heart_health":
            sodium = macros.get("sodium_mg", 0) or 0
            sat_fat = macros.get("fat_g", 0) or 0
            goal_alignment[g] = max(0, 100 - (sodium // 10) - (sat_fat * 5))
        elif g == "muscle_gain":
            protein = macros.get("protein_g", 0) or 0
            goal_alignment[g] = min(100, int(protein * 5))
        elif g == "low_sugar":
            sugar = macros.get("sugar_g", 0) or 0
            goal_alignment[g] = max(0, 100 - int(sugar * 4))
        elif g == "high_fiber":
            fiber = macros.get("fiber_g", 0) or 0
            goal_alignment[g] = min(100, int(fiber * 10))
        elif g == "low_sodium":
            sodium = macros.get("sodium_mg", 0) or 0
            goal_alignment[g] = max(0, 100 - (sodium // 5))
        elif g == "energy":
            carbs = macros.get("carbs_g", 0) or 0
            goal_alignment[g] = min(100, int(carbs * 2))
        else:
            goal_alignment[g] = score

    # Personal fit score: start from nutrition_score, penalize conflicts/warnings
    fit = score
    fit -= len(conflicts) * 20
    fit -= len(warnings) * 10
    # Penalize if sodium/sugar targets exceeded
    if profile.daily_sodium_mg_target:
        serving_sodium = macros.get("sodium_mg") or 0
        if serving_sodium > profile.daily_sodium_mg_target * 0.4:
            fit -= 15
    if profile.daily_sugar_g_target:
        serving_sugar = macros.get("sugar_g") or 0
        if serving_sugar > profile.daily_sugar_g_target * 0.4:
            fit -= 15

    return PersonalFit(
        dietary_conflicts=conflicts,
        allergen_warnings=warnings,
        goal_alignment=goal_alignment,
        overall_fit_score=max(0, min(100, fit)),
    )


def _extract_number(s: str | None) -> float:
    if not s:
        return 0.0
    import re

    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else 0.0


# ---------------------------------------------------------------------------
# Stage 4: Recommend
# ---------------------------------------------------------------------------


@traced(name="stage_recommend", kind="CHAIN")
def stage_recommend(
    client: anthropic.Anthropic,
    facts: ProductFacts,
    analysis: NutritionAnalysis,
    fit: PersonalFit,
    profile: UserProfile,
) -> Recommendation:
    context = {
        "product_facts": facts.model_dump(),
        "nutrition_analysis": analysis.model_dump(),
        "personal_fit": fit.model_dump(),
        "user_health_goals": [g.value for g in profile.health_goals],
    }
    raw = _run_tool_loop(
        client,
        RECOMMEND_SYSTEM,
        f"Provide a final recommendation for this product:\n\n{json.dumps(context, indent=2)}",
        RECOMMEND_TOOLS,
    )
    data = _parse_json_output(raw, "recommend")

    from .schemas import AlternativeSuggestion, EvidenceItem

    evidence = [EvidenceItem(**e) for e in data.pop("evidence", [])]
    alternatives = [AlternativeSuggestion(**a) for a in data.pop("alternatives", [])]
    category = RecommendationCategory(data.pop("category"))

    return Recommendation(
        category=category,
        evidence=evidence,
        alternatives=alternatives,
        personal_fit=fit,
        product_facts=facts,
        analysis=analysis,
        **data,
    )


# ---------------------------------------------------------------------------
# Main pipeline entry point
# ---------------------------------------------------------------------------


def run_pipeline(url: str, profile: UserProfile | None = None) -> Recommendation:
    """
    Run the full 4-stage pipeline and return a structured Recommendation.
    All stages are individually traced with OTEL CHAIN spans.

    session.id is propagated via `using_session` so every auto-instrumented
    Anthropic span inside the pipeline inherits it automatically.
    """
    if profile is None:
        profile = UserProfile()

    request_id = str(uuid.uuid4())
    client = anthropic.Anthropic()

    # using_session injects session.id into the OTEL context — all spans
    # created within this block (including auto-instrumented Anthropic calls)
    # will have session.id = request_id attached automatically.
    with using_session(session_id=request_id):
        with get_tracer(__name__).start_as_current_span("run_pipeline") as span:
            span.set_attribute("openinference.span.kind", "CHAIN")
            span.set_attribute("input.value", json.dumps({"url": url}))
            set_session_attributes(profile)

            facts = stage_extract(client, url)
            analysis = stage_analyze(client, facts)
            fit = stage_personalize(facts, analysis, profile)
            recommendation = stage_recommend(client, facts, analysis, fit, profile)
            recommendation.request_id = request_id

            span.set_attribute("output.value", recommendation.model_dump_json())
            return recommendation


# ---------------------------------------------------------------------------
# Cart pipeline
# ---------------------------------------------------------------------------


def run_cart_pipeline(
    urls: list[str], profile: UserProfile | None = None
) -> CartRecommendation:
    """
    Run the pipeline for each URL and synthesize a CartRecommendation.

    A single cart-level session ID is generated for the synthesis step.
    Each per-item run_pipeline call gets its own session ID so individual
    product traces are also discoverable in Arize.
    """
    if profile is None:
        profile = UserProfile()

    cart_session_id = str(uuid.uuid4())
    client = anthropic.Anthropic()
    per_item: list[Recommendation] = []

    for url in urls:
        rec = run_pipeline(url, profile)
        per_item.append(rec)

    # Synthesis call uses the cart-level session so the summary trace is
    # grouped with this cart request in Arize.
    with using_session(session_id=cart_session_id):
        items_json = json.dumps(
            [
                {
                    "product": r.product_facts.name if r.product_facts else "Unknown",
                    "category": r.category.value,
                    "headline": r.headline,
                }
                for r in per_item
            ],
            indent=2,
        )

        raw = _run_tool_loop(
            client,
            CART_SYSTEM,
            f"Synthesize an overall cart assessment for these {len(per_item)} items:\n\n{items_json}",
            [],
        )

    cart_data = _parse_json_output(raw, "cart")

    swaps = [
        SwapSuggestion(replace=s["replace"], **{"with": s["with"]}, reason=s["reason"])
        for s in cart_data.get("swap_suggestions", [])
    ]

    return CartRecommendation(
        per_item=per_item,
        cart_balance_score=cart_data.get("cart_balance_score", 50),
        cart_summary=cart_data.get("cart_summary", ""),
        swap_suggestions=swaps,
    )
