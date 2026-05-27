"""
FastAPI server for the Grocery Nutrition Agent chatbot UI.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from contextlib import asynccontextmanager

import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
from pydantic import BaseModel

from nutrition_agent.pipeline import run_cart_pipeline, run_pipeline
from nutrition_agent.schemas import (
    CartRecommendation,
    DietaryRestriction,
    HealthGoal,
    Recommendation,
    UserProfile,
)
from nutrition_agent.schemas import UserFacingError
from nutrition_agent.tracing import setup_tracing
from evals.evals_router import router as evals_v1_router
from evals.engine import EVALUATORS, evaluate_all


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = os.environ.get("ARIZE_ENV", "dev").lower()
    setup_tracing(env=env)  # type: ignore[arg-type]
    yield


app = FastAPI(title="Grocery Nutrition Agent", lifespan=lifespan)
app.include_router(evals_v1_router)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ProfilePayload(BaseModel):
    dietary_restrictions: list[str] = []
    allergens: list[str] = []
    health_goals: list[str] = []
    daily_sodium_mg_target: int | None = None
    daily_sugar_g_target: int | None = None


class ChatRequest(BaseModel):
    url: str
    profile: ProfilePayload | None = None


class CartRequest(BaseModel):
    urls: list[str]
    profile: ProfilePayload | None = None


def _build_profile(payload: ProfilePayload | None) -> UserProfile:
    if payload is None:
        return UserProfile()
    return UserProfile(
        dietary_restrictions=[
            DietaryRestriction(r)
            for r in payload.dietary_restrictions
            if r in DietaryRestriction._value2member_map_
        ],
        allergens=payload.allergens,
        health_goals=[
            HealthGoal(g)
            for g in payload.health_goals
            if g in HealthGoal._value2member_map_
        ],
        daily_sodium_mg_target=payload.daily_sodium_mg_target,
        daily_sugar_g_target=payload.daily_sugar_g_target,
    )


# ---------------------------------------------------------------------------
# Online eval auto-logging
# ---------------------------------------------------------------------------


def _get_online_eval_config() -> tuple[bool, list[str]]:
    """
    Read ONLINE_EVALS_ENABLED and ONLINE_EVAL_JUDGES from the environment.

    ONLINE_EVALS_ENABLED — "true" (default) or "false"
    ONLINE_EVAL_JUDGES   — comma-separated evaluator names
                           (default: all registered evaluators)
    """
    enabled = os.environ.get("ONLINE_EVALS_ENABLED", "true").lower() == "true"
    raw = os.environ.get("ONLINE_EVAL_JUDGES", "")
    if raw.strip():
        judges = [j.strip() for j in raw.split(",") if j.strip()]
        judges = [j for j in judges if j in EVALUATORS]
    else:
        judges = list(EVALUATORS)
    return enabled, judges


def _run_online_evals(recommendation_dump: dict, profile_dump: dict) -> None:
    """
    Background task: run configured evaluators on a completed pipeline result
    and log each score to the server logger.

    Fires after the HTTP response is already sent — zero latency impact for the
    end user.  LLM-as-judge evaluators are skipped when ANTHROPIC_API_KEY is
    not set.
    """
    enabled, judges = _get_online_eval_config()
    if not enabled or not judges:
        return

    request_id = recommendation_dump.get("request_id") or "unknown"

    # Build an LLM client only when the key is present.
    llm_client = None
    try:
        import anthropic as _anthropic

        if os.environ.get("ANTHROPIC_API_KEY"):
            llm_client = _anthropic.Anthropic()
    except Exception:
        pass

    # Pass profile data so helpfulness can rate against the user's goals.
    expected = {"profile": profile_dump} if profile_dump else {}

    try:
        results = evaluate_all(
            judges,
            recommendation_dump,
            expected,
            client=llm_client,
        )
    except Exception as exc:
        logger.warning(
            "[online-eval] request_id=%s eval run failed: %s", request_id[:8], exc
        )
        return

    for evaluator, result in results.items():
        logger.info(
            "[online-eval] request_id=%s evaluator=%-28s score=%.2f label=%s",
            request_id[:8],
            evaluator,
            result.score,
            result.label,
        )

    if results:
        overall = sum(r.score for r in results.values()) / len(results)
        logger.info(
            "[online-eval] request_id=%s OVERALL score=%.2f (%d judges)",
            request_id[:8],
            overall,
            len(results),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_static_dir, "index.html"))


@app.post("/chat", response_model=Recommendation)
async def chat(req: ChatRequest, background_tasks: BackgroundTasks) -> Recommendation:
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=422, detail="url must not be empty")
    profile = _build_profile(req.profile)
    try:
        recommendation = run_pipeline(url, profile)
    except UserFacingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Pipeline error for url=%s", url)
        raise HTTPException(
            status_code=500, detail="I don't know, an error occurred"
        ) from exc
    background_tasks.add_task(
        _run_online_evals,
        recommendation.model_dump(mode="json"),
        profile.model_dump(mode="json"),
    )
    return recommendation


@app.post("/cart", response_model=CartRecommendation)
async def cart(
    req: CartRequest, background_tasks: BackgroundTasks
) -> CartRecommendation:
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=422, detail="urls list must not be empty")
    if len(urls) > 10:
        raise HTTPException(status_code=422, detail="Maximum 10 URLs per cart request")
    profile = _build_profile(req.profile)
    try:
        result = run_cart_pipeline(urls, profile)
    except UserFacingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Cart pipeline error for urls=%s", urls)
        raise HTTPException(
            status_code=500, detail="I don't know, an error occurred"
        ) from exc
    profile_dump = profile.model_dump(mode="json")
    for item in result.per_item:
        background_tasks.add_task(
            _run_online_evals,
            item.model_dump(mode="json"),
            profile_dump,
        )
    return result


@app.get("/trace/{request_id}")
async def get_trace(request_id: str) -> JSONResponse:
    """Return the Arize trace URL for a given request_id (dev convenience)."""
    space_id = os.environ.get("ARIZE_SPACE_ID")
    if not space_id:
        return JSONResponse(
            {"message": "Tracing not configured (ARIZE_SPACE_ID not set)"}
        )
    trace_url = f"https://app.arize.com/organizations/{space_id}/traces?filter=session.id%3D{request_id}"
    return JSONResponse({"request_id": request_id, "trace_url": trace_url})


@app.post("/evals/run")
async def run_evals() -> JSONResponse:
    """Kick off the eval runner (dev-only). Streams output to the process stdout."""
    evals_script = os.path.join(os.path.dirname(__file__), "evals", "run_evals.py")
    if not os.path.exists(evals_script):
        raise HTTPException(status_code=404, detail="evals/run_evals.py not found")
    subprocess.Popen([sys.executable, evals_script])
    return JSONResponse({"status": "eval run started", "script": evals_script})


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Grocery Nutrition Agent server")
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default=None,
        help="Arize tracing environment. Defaults to ARIZE_ENV env var (fallback: dev).",
    )
    args = parser.parse_args()
    if args.env is not None:
        os.environ["ARIZE_ENV"] = args.env

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
