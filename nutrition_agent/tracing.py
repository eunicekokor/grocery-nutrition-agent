"""
Observability setup for the Grocery Nutrition Agent.

Provides:
  - setup_tracing(env)         Register Arize OTEL + instrument Anthropic client.
                               env="dev"  -> ARIZE_SPACE_ID_DEV / ARIZE_API_KEY_DEV,
                                            endpoint https://devotlp.arize.com/v1
                               env="prod" -> ARIZE_SPACE_ID_PROD / ARIZE_API_KEY_PROD,
                                            no endpoint (Arize default / cloud)
                               Falls back to the bare ARIZE_SPACE_ID / ARIZE_API_KEY
                               if the env-suffixed vars are absent.
  - @traced(name, kind)        Decorator that wraps any function in an OpenInference span.
  - set_session_attributes()   Attach UserProfile fields to the current span for
                               Arize segmentation. session.id is handled separately
                               via using_session() in the pipeline caller.
  - using_session(session_id)  Re-exported from openinference.instrumentation.
                               Wrap pipeline calls with this to propagate session.id
                               to all spans, including auto-instrumented Anthropic calls.
  - using_attributes(...)      Re-exported; combine session_id + user_id in one call.

Environment variables:
  ARIZE_ENV               "dev" (default) or "prod" — used when no env arg is passed.
  ARIZE_SPACE_ID_DEV      Arize space ID for dev environment.
  ARIZE_API_KEY_DEV       Arize API key for dev environment.
  ARIZE_SPACE_ID_PROD     Arize space ID for prod environment.
  ARIZE_API_KEY_PROD      Arize API key for prod environment.
  ARIZE_SPACE_ID          Fallback used when the env-suffixed vars are not set.
  ARIZE_API_KEY           Fallback used when the env-suffixed vars are not set.
"""

from __future__ import annotations

import functools
import json
import os
from typing import Any, Callable, Literal

from openinference.instrumentation import using_attributes, using_session
from opentelemetry import trace

# Re-export so callers only need to import from this module.
__all__ = [
    "setup_tracing",
    "get_tracer",
    "traced",
    "set_session_attributes",
    "using_session",
    "using_attributes",
]

# OpenInference semantic-convention attribute keys
_INPUT_VALUE = "input.value"
_OUTPUT_VALUE = "output.value"
_SPAN_KIND = "openinference.span.kind"

_DEV_ENDPOINT = "https://devotlp.arize.com/v1"
_tracer = trace.get_tracer(__name__)
_tracer_provider: Any | None = None

ArizeEnv = Literal["dev", "prod"]


# ---------------------------------------------------------------------------
# Tracing setup
# ---------------------------------------------------------------------------


def setup_tracing(
    env: ArizeEnv | None = None,
    project_name: str = "grocery-nutrition-agent",
) -> None:
    """
    Register Arize AX OTEL exporter and instrument the Anthropic client.

    env:          "dev" or "prod". When omitted, reads from the ARIZE_ENV environment
                  variable (default: "dev").
    project_name: Arize project to send traces to. Override for eval runs to keep
                  golden-dataset traces separate from live traffic
                  (e.g. "grocery-nutrition-agent-evals").

    Credential resolution order for each environment:
      dev  -> ARIZE_SPACE_ID_DEV  / ARIZE_API_KEY_DEV  then ARIZE_SPACE_ID / ARIZE_API_KEY
      prod -> ARIZE_SPACE_ID_PROD / ARIZE_API_KEY_PROD then ARIZE_SPACE_ID / ARIZE_API_KEY
    """
    if env is None:
        env = os.environ.get("ARIZE_ENV", "dev").lower()  # type: ignore[assignment]

    if env not in ("dev", "prod"):
        print(
            f"[tracing] unknown ARIZE_ENV '{env}' — expected 'dev' or 'prod', skipping tracing"
        )
        return

    suffix = env.upper()
    space_id = os.environ.get(f"ARIZE_SPACE_ID_{suffix}") or os.environ.get(
        "ARIZE_SPACE_ID"
    )
    api_key = os.environ.get(f"ARIZE_API_KEY_{suffix}") or os.environ.get(
        "ARIZE_API_KEY"
    )

    if not space_id or not api_key:
        print(
            f"[tracing] ARIZE_SPACE_ID_{suffix} / ARIZE_API_KEY_{suffix} not set "
            f"(and no fallback ARIZE_SPACE_ID / ARIZE_API_KEY) — skipping tracing"
        )
        return

    from arize.otel import register
    from openinference.instrumentation.anthropic import AnthropicInstrumentor

    register_kwargs: dict[str, Any] = dict(
        space_id=space_id,
        api_key=api_key,
        project_name=project_name,
        log_to_console=True,
        set_global_tracer_provider=False,
    )
    if env == "dev":
        register_kwargs["endpoint"] = _DEV_ENDPOINT

    global _tracer, _tracer_provider

    tracer_provider = register(**register_kwargs)
    _tracer_provider = tracer_provider
    _tracer = tracer_provider.get_tracer(__name__)
    AnthropicInstrumentor().instrument(tracer_provider=tracer_provider)
    endpoint_note = (
        f"endpoint={_DEV_ENDPOINT}" if env == "dev" else "endpoint=default (prod)"
    )
    print(f"[tracing] Arize AX tracing enabled — env={env}, {endpoint_note}")


def get_tracer(name: str) -> trace.Tracer:
    """Return a tracer from the active Arize provider when tracing is enabled."""
    if _tracer_provider is not None:
        return _tracer_provider.get_tracer(name)
    return trace.get_tracer(name)


# ---------------------------------------------------------------------------
# @traced decorator
# ---------------------------------------------------------------------------


def traced(name: str | None = None, kind: str = "CHAIN") -> Callable:
    """
    Decorator factory that wraps a function in an OpenInference span.

    Usage:
        @traced(kind="TOOL")
        def fetch_product_info(url: str) -> str: ...

        @traced(kind="CHAIN")
        def stage_extract(url: str) -> ProductFacts: ...
    """

    def decorator(fn: Callable) -> Callable:
        span_name = name or fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with _tracer.start_as_current_span(span_name) as span:
                span.set_attribute(_SPAN_KIND, kind)

                # Serialize inputs
                try:
                    input_repr = json.dumps(
                        {
                            "args": [_safe_serialize(a) for a in args],
                            "kwargs": {
                                k: _safe_serialize(v) for k, v in kwargs.items()
                            },
                        }
                    )
                    span.set_attribute(_INPUT_VALUE, input_repr)
                except Exception:
                    pass

                result = fn(*args, **kwargs)

                # Serialize output
                try:
                    span.set_attribute(_OUTPUT_VALUE, _safe_serialize(result))
                except Exception:
                    pass

                return result

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Session attributes
# ---------------------------------------------------------------------------


def set_session_attributes(profile: Any) -> None:
    """
    Attach user profile fields to the current active span for Arize segmentation.

    NOTE: session.id is NOT set here. Wrap the pipeline call with
    `using_session(session_id=request_id)` instead — this propagates session.id
    through the OTEL context so every auto-instrumented span (including all
    Anthropic API calls inside the pipeline) inherits it automatically, per:
    https://arize.com/docs/ax/instrument/set-up-sessions
    """
    if profile is None:
        return

    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return

    if profile.dietary_restrictions:
        span.set_attribute(
            "user.dietary_restrictions",
            json.dumps([r.value for r in profile.dietary_restrictions]),
        )
    if profile.allergens:
        span.set_attribute("user.allergens", json.dumps(profile.allergens))
    if profile.health_goals:
        span.set_attribute(
            "user.health_goals",
            json.dumps([g.value for g in profile.health_goals]),
        )
    if profile.daily_sodium_mg_target is not None:
        span.set_attribute(
            "user.daily_sodium_mg_target", profile.daily_sodium_mg_target
        )
    if profile.daily_sugar_g_target is not None:
        span.set_attribute("user.daily_sugar_g_target", profile.daily_sugar_g_target)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_serialize(obj: Any) -> str:
    """Best-effort JSON serialization; falls back to repr."""
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump())
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return repr(obj)
