"""
All tools available to the Grocery Nutrition Agent.

Tools 1 and 5 involve LLM calls / external HTTP.
Tools 3 and 4 are fully deterministic (no LLM) — important for reliable evals.
Tool 2 calls the free USDA FoodData Central API with an LLM fallback.
"""

from __future__ import annotations

import json
import re

import requests
from bs4 import BeautifulSoup

from .tracing import traced

# ---------------------------------------------------------------------------
# Curated ingredient knowledge bases (deterministic — no LLM)
# ---------------------------------------------------------------------------

# Ultra-processed markers: if any of these appear near the top of the ingredient
# list the product is likely ultra-processed.
ULTRA_PROCESSED_MARKERS = {
    "high fructose corn syrup", "corn syrup", "dextrose", "maltodextrin",
    "modified starch", "hydrogenated", "partially hydrogenated", "mono- and diglycerides",
    "sodium nitrate", "sodium nitrite", "carrageenan", "artificial flavor",
    "artificial colour", "artificial color", "red 40", "yellow 5", "yellow 6",
    "blue 1", "blue 2", "titanium dioxide", "tbhq", "bha", "bht",
    "propyl gallate", "sodium benzoate", "potassium bromate", "acesulfame",
    "sucralose", "aspartame", "saccharin", "neotame",
}

# Additives that are moderate concern (processed but not ultra)
PROCESSED_MARKERS = {
    "enriched flour", "bleached flour", "enriched wheat", "palm oil",
    "canola oil", "soybean oil", "sugar", "brown sugar", "cane sugar",
    "evaporated cane juice", "invert sugar", "corn starch", "modified corn starch",
    "soy lecithin", "xanthan gum", "carob bean gum", "guar gum",
}

# Common top-8 + sesame allergens
ALLERGEN_KEYWORDS: dict[str, list[str]] = {
    "milk": ["milk", "cream", "butter", "cheese", "whey", "lactose", "casein", "dairy"],
    "eggs": ["egg", "albumin", "mayonnaise"],
    "fish": ["fish", "cod", "salmon", "tuna", "tilapia", "bass", "flounder", "anchovy"],
    "shellfish": ["shrimp", "crab", "lobster", "clam", "oyster", "mussel", "scallop"],
    "tree nuts": ["almond", "cashew", "walnut", "pecan", "pistachio", "macadamia", "hazelnut", "brazil nut"],
    "peanuts": ["peanut", "groundnut", "arachis"],
    "wheat": ["wheat", "flour", "semolina", "spelt", "kamut", "farro", "durum", "bulgur"],
    "soy": ["soy", "soya", "tofu", "tempeh", "edamame", "miso", "soybean"],
    "sesame": ["sesame", "tahini", "til", "gingelly"],
}

# Vegan-incompatible ingredients
NON_VEGAN = {
    "meat", "beef", "pork", "chicken", "turkey", "fish", "shrimp", "lobster",
    "crab", "oyster", "gelatin", "lard", "tallow", "whey", "casein", "lactose",
    "milk", "cream", "cheese", "butter", "egg", "honey", "beeswax", "carmine",
    "cochineal", "isinglass", "rennet", "albumin",
}

# Gluten-containing grains
GLUTEN_INGREDIENTS = {
    "wheat", "barley", "rye", "oat", "spelt", "kamut", "farro", "triticale",
    "semolina", "durum", "bulgur", "flour", "malt",
}

# Keto-incompatible: high carb
HIGH_CARB_INGREDIENTS = {
    "sugar", "corn syrup", "high fructose corn syrup", "dextrose", "maltodextrin",
    "rice", "potato", "bread", "pasta", "grain", "oat", "wheat", "flour",
    "honey", "maple syrup", "agave",
}


# ---------------------------------------------------------------------------
# Tool 1: fetch_product_info
# ---------------------------------------------------------------------------


@traced(name="fetch_product_info", kind="TOOL")
def fetch_product_info(url: str) -> str:
    """Fetch a grocery product page and return the visible text (max 6000 chars)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"Error fetching page: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    return text[:6000]


# ---------------------------------------------------------------------------
# Tool 2: lookup_nutrition_database (USDA FoodData Central, no key required)
# ---------------------------------------------------------------------------


@traced(name="lookup_nutrition_database", kind="TOOL")
def lookup_nutrition_database(product_name: str, brand: str | None = None) -> str:
    """
    Query USDA FoodData Central for nutritional data.
    Returns a JSON string with nutrients, or a not-found message.
    No API key required for the basic search endpoint.
    """
    query = f"{brand} {product_name}".strip() if brand else product_name
    try:
        resp = requests.get(
            "https://api.nal.usda.gov/fdc/v1/foods/search",
            params={"query": query, "pageSize": 3, "dataType": "Branded,SR Legacy"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        foods = data.get("foods", [])
        if not foods:
            return f"No USDA match found for '{query}'."

        # Take the first result and extract key nutrients
        food = foods[0]
        nutrients: dict[str, str] = {}
        for n in food.get("foodNutrients", []):
            name = n.get("nutrientName", "")
            value = n.get("value")
            unit = n.get("unitName", "")
            if value is not None and name:
                nutrients[name] = f"{value}{unit}"

        result = {
            "source": "USDA FoodData Central",
            "description": food.get("description"),
            "brand": food.get("brandOwner"),
            "serving_size": f"{food.get('servingSize', '?')} {food.get('servingSizeUnit', '')}".strip(),
            "nutrients": nutrients,
        }
        return json.dumps(result, indent=2)

    except requests.RequestException as e:
        return f"USDA lookup failed: {e}"


# ---------------------------------------------------------------------------
# Tool 3: analyze_ingredients (deterministic — no LLM)
# ---------------------------------------------------------------------------


@traced(name="analyze_ingredients", kind="TOOL")
def analyze_ingredients(ingredients: list[str]) -> str:
    """
    Deterministically classify each ingredient against curated knowledge bases.
    Returns a JSON string with flagged ingredients and processing level.
    No LLM involved — important for consistent evals.
    """
    ingredients_lower = [i.lower().strip() for i in ingredients]
    flagged: list[dict] = []
    ultra_count = 0
    processed_count = 0

    for raw, norm in zip(ingredients, ingredients_lower):
        # Check ultra-processed markers
        for marker in ULTRA_PROCESSED_MARKERS:
            if marker in norm:
                ultra_count += 1
                flagged.append({
                    "ingredient": raw,
                    "reason": f"Ultra-processed marker: {marker}",
                    "severity": "high",
                })
                break

        # Check processed markers (only if not already flagged as ultra)
        if not any(f["ingredient"] == raw for f in flagged):
            for marker in PROCESSED_MARKERS:
                if marker in norm:
                    processed_count += 1
                    flagged.append({
                        "ingredient": raw,
                        "reason": f"Processed ingredient: {marker}",
                        "severity": "medium",
                    })
                    break

        # Allergen detection
        for allergen, keywords in ALLERGEN_KEYWORDS.items():
            if any(kw in norm for kw in keywords):
                existing = next((f for f in flagged if f["ingredient"] == raw), None)
                if existing:
                    existing["allergen"] = allergen
                else:
                    flagged.append({
                        "ingredient": raw,
                        "reason": f"Potential allergen: {allergen}",
                        "severity": "medium",
                        "allergen": allergen,
                    })

    # Determine processing level from ratio of flagged ingredients
    total = max(len(ingredients), 1)
    if ultra_count / total >= 0.15 or ultra_count >= 3:
        processing_level = "ultra_processed"
    elif (ultra_count + processed_count) / total >= 0.25:
        processing_level = "processed"
    elif (ultra_count + processed_count) / total > 0:
        processing_level = "minimally_processed"
    else:
        processing_level = "whole"

    return json.dumps({
        "processing_level": processing_level,
        "flagged_count": len(flagged),
        "flagged_ingredients": flagged,
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool 4: check_dietary_compatibility (deterministic — no LLM)
# ---------------------------------------------------------------------------


@traced(name="check_dietary_compatibility", kind="TOOL")
def check_dietary_compatibility(
    ingredients: list[str],
    claims: list[str],
    dietary_restrictions: list[str],
    allergens: list[str],
) -> str:
    """
    Deterministically check product ingredients against a user's dietary
    restrictions and allergen list. Returns a JSON string with conflicts and warnings.
    No LLM involved.
    """
    ingredients_lower = [i.lower().strip() for i in ingredients]
    claims_lower = [c.lower().strip() for c in claims]
    conflicts: list[dict] = []
    warnings: list[dict] = []

    restriction_rules: dict[str, set[str]] = {
        "vegan": NON_VEGAN,
        "vegetarian": {
            "meat", "beef", "pork", "chicken", "turkey", "fish", "shrimp",
            "lobster", "crab", "oyster", "gelatin", "lard", "tallow",
            "carmine", "cochineal", "isinglass", "rennet",
        },
        "gluten_free": GLUTEN_INGREDIENTS,
        "dairy_free": {
            "milk", "cream", "butter", "cheese", "whey", "lactose",
            "casein", "dairy", "yogurt",
        },
        "nut_free": {
            "almond", "cashew", "walnut", "pecan", "pistachio",
            "macadamia", "hazelnut", "brazil nut", "tree nut",
        },
        "keto": HIGH_CARB_INGREDIENTS,
        "halal": {"pork", "lard", "gelatin", "alcohol", "wine", "beer"},
        "kosher": {"pork", "shellfish", "lard"},
        "low_fodmap": {
            "onion", "garlic", "apple", "pear", "wheat", "lactose", "honey",
            "high fructose corn syrup", "fructose", "inulin",
        },
    }

    for restriction in dietary_restrictions:
        rule_ingredients = restriction_rules.get(restriction, set())
        # Check if product claims to be compatible first
        if restriction.replace("_", "-") in claims_lower or restriction.replace("_", " ") in claims_lower:
            continue
        for raw, norm in zip(ingredients, ingredients_lower):
            if any(r in norm for r in rule_ingredients):
                conflicts.append({
                    "restriction": restriction,
                    "ingredient": raw,
                    "reason": f"'{raw}' is incompatible with {restriction} diet.",
                })

    # Check user-provided allergens
    for allergen in allergens:
        allergen_lower = allergen.lower()
        known_keywords = ALLERGEN_KEYWORDS.get(allergen_lower, [allergen_lower])
        for raw, norm in zip(ingredients, ingredients_lower):
            if any(kw in norm for kw in known_keywords):
                warnings.append({
                    "allergen": allergen,
                    "found_in": raw,
                    "certainty": "high",
                })
                break
        else:
            # Partial match check
            for raw, norm in zip(ingredients, ingredients_lower):
                if allergen_lower in norm:
                    warnings.append({
                        "allergen": allergen,
                        "found_in": raw,
                        "certainty": "medium",
                    })
                    break

    return json.dumps({
        "conflicts": conflicts,
        "allergen_warnings": warnings,
        "is_compatible": len(conflicts) == 0,
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool 5: find_alternatives (LLM-driven)
# ---------------------------------------------------------------------------


@traced(name="find_alternatives", kind="TOOL")
def find_alternatives_tool_call(product_name: str, category: str, health_goals: list[str]) -> str:
    """
    Return a JSON list of up to 3 healthier alternative product suggestions.
    This is a stub — the pipeline feeds this as a tool to Claude so the model
    reasons about alternatives given the full context.
    """
    # The actual LLM reasoning happens in the pipeline's tool loop.
    # This function is a placeholder so the tool schema is consistent.
    return json.dumps({
        "note": "Alternatives are generated by the LLM during the recommend stage.",
        "product": product_name,
        "category": category,
        "health_goals": health_goals,
    })


# ---------------------------------------------------------------------------
# Claude-compatible tool schemas
# ---------------------------------------------------------------------------

EXTRACT_TOOLS = [
    {
        "name": "fetch_product_info",
        "description": (
            "Fetches a grocery item page and returns its visible text content "
            "(product name, ingredients, nutrition facts, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL of the grocery product page.",
                }
            },
            "required": ["url"],
        },
    }
]

ANALYZE_TOOLS = [
    {
        "name": "lookup_nutrition_database",
        "description": "Look up a product in the USDA FoodData Central database for authoritative nutritional data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string"},
                "brand": {"type": "string"},
            },
            "required": ["product_name"],
        },
    },
    {
        "name": "analyze_ingredients",
        "description": "Deterministically classify a list of ingredients against known ultra-processed markers, additives, and allergens.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ingredients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ingredient list extracted from the product.",
                }
            },
            "required": ["ingredients"],
        },
    },
]

RECOMMEND_TOOLS = [
    {
        "name": "find_alternatives",
        "description": "Generate up to 3 healthier alternative product suggestions for a given product.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": ["MORE_OF", "IN_MODERATION", "LESS_OF"],
                },
                "health_goals": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["product_name", "category"],
        },
    }
]


def dispatch_tool(name: str, inputs: dict) -> str:
    """Route a tool call by name to the appropriate Python function."""
    if name == "fetch_product_info":
        return fetch_product_info(inputs["url"])
    if name == "lookup_nutrition_database":
        return lookup_nutrition_database(inputs["product_name"], inputs.get("brand"))
    if name == "analyze_ingredients":
        return analyze_ingredients(inputs["ingredients"])
    if name == "check_dietary_compatibility":
        return check_dietary_compatibility(
            inputs["ingredients"],
            inputs.get("claims", []),
            inputs.get("dietary_restrictions", []),
            inputs.get("allergens", []),
        )
    if name == "find_alternatives":
        return find_alternatives_tool_call(
            inputs["product_name"],
            inputs.get("category", "IN_MODERATION"),
            inputs.get("health_goals", []),
        )
    return f"Unknown tool: {name}"
