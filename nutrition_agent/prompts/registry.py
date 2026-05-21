"""
Assembles the PROMPT_REGISTRY from individual prompt modules and exposes
get_prompt() / active_version() accessors.
"""

from __future__ import annotations

from . import analyze, cart, extract, recommend

PROMPT_REGISTRY: dict[str, dict] = {
    "extract": extract.ENTRY,
    "analyze": analyze.ENTRY,
    "recommend": recommend.ENTRY,
    "cart": cart.ENTRY,
}


def get_prompt(name: str, version: str | None = None) -> str:
    """
    Return the content of a named prompt at a specific version.
    If version is None, the active version is used.

    Raises KeyError if the name or version does not exist.
    """
    entry = PROMPT_REGISTRY[name]
    v = version or entry["active"]
    return entry["versions"][v]["content"]


def active_version(name: str) -> str:
    """Return the active version label for a named prompt, e.g. 'v1'."""
    return PROMPT_REGISTRY[name]["active"]
