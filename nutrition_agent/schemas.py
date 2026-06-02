"""
Pydantic schemas that form the typed contract between pipeline stages and evals.
Every stage returns one of these models; structured outputs enable deterministic evals.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# User-facing pipeline errors
# ---------------------------------------------------------------------------


class UserFacingError(ValueError):
    """
    Raised when the pipeline detects a condition the user should fix
    (e.g. submitting a non-product URL).

    server.py catches this specifically and returns the message directly
    to the UI instead of the generic fallback.
    """


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DietaryRestriction(str, Enum):
    VEGAN = "vegan"
    VEGETARIAN = "vegetarian"
    GLUTEN_FREE = "gluten_free"
    DAIRY_FREE = "dairy_free"
    NUT_FREE = "nut_free"
    KETO = "keto"
    PALEO = "paleo"
    HALAL = "halal"
    KOSHER = "kosher"
    LOW_FODMAP = "low_fodmap"


class HealthGoal(str, Enum):
    WEIGHT_LOSS = "weight_loss"
    HEART_HEALTH = "heart_health"
    MUSCLE_GAIN = "muscle_gain"
    LOW_SUGAR = "low_sugar"
    HIGH_FIBER = "high_fiber"
    LOW_SODIUM = "low_sodium"
    ENERGY = "energy"


class ProcessingLevel(str, Enum):
    WHOLE = "whole"
    MINIMALLY_PROCESSED = "minimally_processed"
    PROCESSED = "processed"
    ULTRA_PROCESSED = "ultra_processed"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecommendationCategory(str, Enum):
    MORE_OF = "MORE_OF"
    IN_MODERATION = "IN_MODERATION"
    LESS_OF = "LESS_OF"


# ---------------------------------------------------------------------------
# Input: User Profile
# ---------------------------------------------------------------------------


class UserProfile(BaseModel):
    dietary_restrictions: list[DietaryRestriction] = Field(
        default_factory=list,
        description="Hard dietary restrictions the product must respect.",
    )
    allergens: list[str] = Field(
        default_factory=list,
        description="Free-text allergens the user wants to avoid (e.g. 'sesame', 'shellfish').",
    )
    health_goals: list[HealthGoal] = Field(
        default_factory=list,
        description="Active health goals used to score goal alignment.",
    )
    daily_sodium_mg_target: int | None = Field(
        default=None,
        description="Max daily sodium in mg. None means no specific target.",
    )
    daily_sugar_g_target: int | None = Field(
        default=None,
        description="Max daily added sugar in grams. None means no specific target.",
    )


# ---------------------------------------------------------------------------
# Stage 1 output: ProductFacts
# ---------------------------------------------------------------------------


class ProductFacts(BaseModel):
    name: str = Field(description="Product name as listed on the package.")
    brand: str | None = Field(default=None, description="Brand name.")
    serving_size: str | None = Field(default=None, description="e.g. '1 cup (240g)'")
    servings_per_container: float | None = None
    nutrition: dict[str, str | None] = Field(
        default_factory=dict,
        description="Nutrient name -> value with unit, e.g. {'calories': '120', 'sodium': '480mg'}.",
    )
    ingredients: list[str] = Field(
        default_factory=list,
        description="Ingredient list in order (most prevalent first).",
    )
    claims: list[str] = Field(
        default_factory=list,
        description="Marketing/certification claims: organic, non-GMO, gluten-free, etc.",
    )
    source_url: str = Field(description="URL the facts were extracted from.")


# ---------------------------------------------------------------------------
# Stage 2 output: NutritionAnalysis
# ---------------------------------------------------------------------------


class FlaggedIngredient(BaseModel):
    ingredient: str
    reason: str
    severity: Severity


class NutritionAnalysis(BaseModel):
    nutrition_score: int = Field(
        ge=0, le=100,
        description="Overall nutrition score 0-100 (higher = healthier).",
    )
    processing_level: ProcessingLevel
    flagged_ingredients: list[FlaggedIngredient] = Field(
        default_factory=list,
        description="Ingredients of concern with reason and severity.",
    )
    macros_summary: dict[str, Any] = Field(
        default_factory=dict,
        description="Key macro highlights: protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg.",
    )
    analysis_notes: str = Field(
        default="",
        description="Free-text synthesis of the nutritional profile.",
    )


# ---------------------------------------------------------------------------
# Stage 3 output: PersonalFit
# ---------------------------------------------------------------------------


class DietaryConflict(BaseModel):
    restriction: str
    ingredient: str
    reason: str


class AllergenWarning(BaseModel):
    allergen: str
    found_in: str
    certainty: Severity


class PersonalFit(BaseModel):
    dietary_conflicts: list[DietaryConflict] = Field(
        default_factory=list,
        description="Hard conflicts with the user's dietary restrictions.",
    )
    allergen_warnings: list[AllergenWarning] = Field(
        default_factory=list,
        description="Potential or confirmed allergen matches.",
    )
    goal_alignment: dict[str, float] = Field(
        default_factory=dict,
        description="Health goal -> alignment score 0-100.",
    )
    overall_fit_score: int = Field(
        ge=0, le=100,
        description="Composite personal fit score (100 = perfect match for profile).",
    )


# ---------------------------------------------------------------------------
# Stage 4 output: Recommendation
# ---------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    claim: str = Field(description="A specific claim made in the recommendation.")
    source: str = Field(description="The ProductFacts or NutritionAnalysis field this comes from.")


class AlternativeSuggestion(BaseModel):
    name: str
    why: str


class Recommendation(BaseModel):
    category: RecommendationCategory
    headline: str = Field(
        description="One-line verdict, e.g. '✅ MORE OF: Bob's Red Mill Oats'.",
    )
    rationale: str = Field(
        description="2-4 sentence explanation citing specific facts.",
    )
    evidence: list[EvidenceItem] = Field(
        default_factory=list,
        description="Specific facts from ProductFacts/NutritionAnalysis that support the recommendation.",
    )
    alternatives: list[AlternativeSuggestion] = Field(
        default_factory=list,
        description="Better alternatives if LESS_OF; complementary items if MORE_OF.",
    )
    personal_fit: PersonalFit | None = Field(
        default=None,
        description="Personalization results; None if no profile was provided.",
    )
    product_facts: ProductFacts | None = Field(
        default=None,
        description="Extracted product facts for downstream evals.",
    )
    analysis: NutritionAnalysis | None = Field(
        default=None,
        description="Nutrition analysis for downstream evals.",
    )
    request_id: str | None = None


# ---------------------------------------------------------------------------
# Cart output: CartRecommendation
# ---------------------------------------------------------------------------


class SwapSuggestion(BaseModel):
    replace: str = Field(description="Product name to replace.")
    with_: str = Field(alias="with", description="Suggested replacement.")
    reason: str


class CartRecommendation(BaseModel):
    per_item: list[Recommendation]
    cart_balance_score: int = Field(
        ge=0, le=100,
        description="Overall cart nutritional balance (100 = excellent variety and health).",
    )
    cart_summary: str = Field(
        description="2-3 sentence overall assessment of the cart.",
    )
    swap_suggestions: list[SwapSuggestion] = Field(
        default_factory=list,
        description="Specific swaps to improve the cart.",
    )

    model_config = {"populate_by_name": True}
