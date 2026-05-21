"""
nutrition_agent.prompts
-----------------------
Versioned prompt registry for the Grocery Nutrition Agent.

Public API
----------
PROMPT_REGISTRY          dict of all prompts and their version history
get_prompt(name, ver)    fetch a prompt's content (defaults to active version)
active_version(name)     return the active version label, e.g. "v1"

Module-level constants (used directly by the pipeline — no pipeline changes
needed when you bump a prompt version):
  EXTRACT_SYSTEM
  ANALYZE_SYSTEM
  RECOMMEND_SYSTEM
  CART_SYSTEM

Folder layout
-------------
  prompts/
    __init__.py     ← you are here
    registry.py     ← assembles PROMPT_REGISTRY + accessors
    extract.py      ← versioned prompts for stage 1
    analyze.py      ← versioned prompts for stage 2
    recommend.py    ← versioned prompts for stage 4
    cart.py         ← versioned prompts for cart synthesis
"""

from .registry import PROMPT_REGISTRY, active_version, get_prompt

# Module-level constants derived from the active versions.
# The pipeline imports these so it requires no changes when prompts are iterated.
EXTRACT_SYSTEM = get_prompt("extract")
ANALYZE_SYSTEM = get_prompt("analyze")
RECOMMEND_SYSTEM = get_prompt("recommend")
CART_SYSTEM = get_prompt("cart")

__all__ = [
    "PROMPT_REGISTRY",
    "get_prompt",
    "active_version",
    "EXTRACT_SYSTEM",
    "ANALYZE_SYSTEM",
    "RECOMMEND_SYSTEM",
    "CART_SYSTEM",
]
