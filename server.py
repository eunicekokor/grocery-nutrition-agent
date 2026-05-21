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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger(__name__)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nutrition_agent.pipeline import run_cart_pipeline, run_pipeline
from nutrition_agent.schemas import (
    CartRecommendation,
    DietaryRestriction,
    HealthGoal,
    Recommendation,
    UserProfile,
)
from nutrition_agent.tracing import setup_tracing


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = os.environ.get("ARIZE_ENV", "dev").lower()
    setup_tracing(env=env)  # type: ignore[arg-type]
    yield


app = FastAPI(title="Grocery Nutrition Agent", lifespan=lifespan)

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
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_static_dir, "index.html"))


@app.post("/chat", response_model=Recommendation)
async def chat(req: ChatRequest) -> Recommendation:
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=422, detail="url must not be empty")
    profile = _build_profile(req.profile)
    try:
        return run_pipeline(url, profile)
    except Exception as exc:
        logger.exception("Pipeline error for url=%s", url)
        raise HTTPException(
            status_code=500, detail="I don't know, an error occurred"
        ) from exc


@app.post("/cart", response_model=CartRecommendation)
async def cart(req: CartRequest) -> CartRecommendation:
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=422, detail="urls list must not be empty")
    if len(urls) > 10:
        raise HTTPException(status_code=422, detail="Maximum 10 URLs per cart request")
    profile = _build_profile(req.profile)
    try:
        return run_cart_pipeline(urls, profile)
    except Exception as exc:
        logger.exception("Cart pipeline error for urls=%s", urls)
        raise HTTPException(
            status_code=500, detail="I don't know, an error occurred"
        ) from exc


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
