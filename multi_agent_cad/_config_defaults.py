"""
Internal: config defaults template + reset utility.

This module is intentionally separate from ``config.py`` so that
``--reset`` works even if the user has broken ``config.py`` syntax —
running ``python -m multi_agent_cad._config_defaults --reset`` does
NOT import ``config.py``; it just rewrites it from the template below.

Public CLI:

    python -m multi_agent_cad._config_defaults --reset
        Restore config.py to default values.

    python -m multi_agent_cad._config_defaults --show
        Print the default config.py template to stdout.
"""

from __future__ import annotations

import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.py"

CONFIG_TEMPLATE = '''\
"""
User-editable configuration for the multi-agent CAD pipeline.

Edit the values below to customize retry budgets, timeouts, model
parameters, and workflow routing.

To restore defaults after editing, run:

    python -m multi_agent_cad._config_defaults --reset

This file is loaded by ``nodes.py`` and ``token_tracker.py`` at import
time. Changes take effect on the next run.
"""

# ============================================================================
# API Key & Provider
# ============================================================================
#
# DashScope (Alibaba Cloud Bailian) API key for accessing qwen3.7-max.
#
# Priority (checked at runtime by ``_llm_client()`` in nodes.py):
#   1. Environment variable ``DASHSCOPE_API_KEY``  (recommended -- especially
#      for shared / version-controlled environments).
#   2. ``DS_API_KEY`` below  (fallback for local development).
#
# SECURITY NOTE: This file contains a real API key. Do NOT commit it to
# version control, share it, or paste it in chat. If leaked, regenerate
# the key in the Bailian console immediately.

DS_API_KEY = ""

# --- Env var injection for Aider / LiteLLM -------------------------------
# Aider reads these env vars to find the API endpoint + key. Override when
# switching to non-OpenAI-compatible providers.
#
#   OpenAI-compatible (default): OPENAI_API_KEY + OPENAI_API_BASE
#   Anthropic (Claude):          ANTHROPIC_API_KEY  (set API_BASE_ENV_VAR = "")
#   DeepSeek (Aider native):     DEEPSEEK_API_KEY   (set API_BASE_ENV_VAR = "")
#   Google Gemini:               GEMINI_API_KEY     (set API_BASE_ENV_VAR = "")
API_KEY_ENV_VAR = "OPENAI_API_KEY"
API_BASE_ENV_VAR = "OPENAI_API_BASE"

# ============================================================================
# Retry / Iteration Budgets
# ============================================================================

# Max outer retries for the autonomous skill loop (Phase 4).
# Each retry runs a full QA -> Aider repair -> re-execute cycle.
MAX_RETRIES = 5

# Inner exec retries within each outer retry.  When Aider edits the code
# but re-execution crashes, we immediately re-invoke Aider with the
# traceback -- without consuming an outer retry slot.
MAX_EXEC_RETRIES = 3

# ============================================================================
# Timeouts (seconds)
# ============================================================================

# LLM API call (DashScope qwen3.7-max) for spec_planner / architect / coder.
LLM_API_TIMEOUT = 120

# check_mesh.py subprocess timeout (Engine B -- STL mesh analysis).
CHECK_MESH_TIMEOUT = 180

# Generated CAD script execution timeout (subprocess.run for build123d).
CAD_SCRIPT_TIMEOUT = 180

# Iteration checkpoint input timeout -- auto-selects "1" (auto-iterate) if the
# user doesn\'t type within this many seconds.
CHECKPOINT_INPUT_TIMEOUT = 10

# User intervention input timeout -- fallback for when the user picked
# option 2 and needs to type change requirements.  Long enough to not
# interrupt thoughtful input (1 hour).
INTERVENTION_INPUT_TIMEOUT = 3600

# ============================================================================
# Model Parameters (per-stage)
# ============================================================================
#
# Each of the 4 pipeline stages can use a different model with its own
# settings. ``*_KWARGS`` is passed directly to ``client.chat.completions.create()``
# as extra keyword arguments -- use it to control thinking/reasoning.
#
# Examples for different providers (uncomment one to switch):
#   Qwen / DashScope:  {"extra_body": {"enable_thinking": True}}
#   OpenAI o1/o3:      {"reasoning_effort": "medium"}
#   DeepSeek-R1:       {}  (no toggle; use deepseek-reasoner as the model name)
#   Anthropic Claude:  {"thinking": {"type": "enabled", "budget_tokens": 2048}}
#   Plain chat models: {}  (no thinking control)
#
# Stages:
#   1. Spec Planner        -- parses user request -> CADBrief JSON
#   2. Geometric Architect -- CADBrief -> ArchitectPlan
#   3. Python Coder        -- ArchitectPlan -> build123d code (LLM fallback only)
#   4. Autonomous Skill Loop -- Aider repair + direct API fallback
#
# DS_BASE_URL is shared across all stages (same endpoint).
#
# Model names: every *_MODEL below is just the model ID served on DS_BASE_URL.
# The default "qwen3.7-max" is the flagship model of Alibaba DashScope. Swap in
# "gpt-5.6", "deepseek-v4-pro", "gemini-3.6-flash", a local Ollama model, etc.
# Nothing in the code is Qwen-specific except the `enable_thinking` toggle in
# *_KWARGS (set *_KWARGS = {} for providers without such a toggle). See the
# "Use any LLM provider" section in README.md for full per-provider setup.

DS_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

# --- Stage 1: Spec Planner -----------------------------------------------
SPEC_PLANNER_MODEL = "qwen3.7-max"
SPEC_PLANNER_TEMPERATURE = 0.0
SPEC_PLANNER_MAX_TOKENS = 32768
SPEC_PLANNER_KWARGS = {"extra_body": {"enable_thinking": True}}

# --- Stage 2: Geometric Architect ---------------------------------------
# Thinking disabled for JSON determinism (saves output tokens).
ARCHITECT_MODEL = "qwen3.7-max"
ARCHITECT_TEMPERATURE = 0.0
ARCHITECT_MAX_TOKENS = 32768
ARCHITECT_KWARGS = {"extra_body": {"enable_thinking": False}}

# --- Stage 3: Python Coder ----------------------------------------------
# Uses LLM only as fallback when the deterministic coder fails.
CODER_MODEL = "qwen3.7-max"
CODER_TEMPERATURE = 0.0
CODER_MAX_TOKENS = 32768
CODER_KWARGS = {"extra_body": {"enable_thinking": True}}

# --- Stage 4: Autonomous Skill Loop ------------------------------------
# Primary: Aider (uses Aider\'s own Model() class with provider-prefixed name).
# Switch providers by changing AIDER_MODEL:
#   Qwen:          "openai/qwen3.7-max"
#   Anthropic:     "anthropic/claude-sonnet-4-6"
#   DeepSeek:      "deepseek/deepseek-v4-pro"
#   Gemini:        "gemini/gemini-3.6-flash"
AIDER_MODEL = "openai/qwen3.7-max"
AIDER_MAX_TOKENS = 65536

# Fallback: direct DashScope API (used when Aider is unavailable, or for
# initial generation in the Aider-First workflow).
REPAIR_MODEL = "qwen3.7-max"
REPAIR_TEMPERATURE = 0.3   # slightly creative -- multiple valid fix paths
REPAIR_MAX_TOKENS = 32768
REPAIR_KWARGS = {"extra_body": {"enable_thinking": True}}

# ============================================================================
# User Request (default prompt)
# ============================================================================
#
# The default CAD generation request used by both workflows when no explicit
# request is supplied. Edit this to change what gets built when you run
# ``python -m multi_agent_cad.graph`` or ``python -m multi_agent_cad.graph_aider``.
#
# For the modify-existing workflow (graph_aider), this prompt is treated as
# MODIFICATION REQUIREMENTS for an existing .py file, not a from-scratch spec.

USER_REQUEST = "Create a single solid aerospace-style clevis bracket as a STEP model in millimeters. The part is symmetric about the XZ plane. Start with a base plate 120 mm long in X, 60 mm wide in Y, and 10 mm thick in Z, centered on the XY origin, with bottom face at Z = 0. Add two vertical clevis lugs rising from the top of the base near the center. Each lug is 18 mm thick in Y, 42 mm tall above the base, and extends 36 mm along X. The two lugs are separated by a 16 mm central gap in Y. The top of each lug has a semicircular rounded profile with radius 18 mm when viewed from the side. Add a horizontal through-hole of diameter 14 mm through both lugs along the Y direction, centered at X = 0 and Z = 34 mm. Add four base mounting holes, diameter 7 mm, through the base plate, located at X = +/-45 mm and Y = +/-20 mm. Add two triangular lightening cutouts through the base web, one on each side of the clevis, each with rounded corners of radius 3 mm. Add two diagonal reinforcing ribs from the base to the outer faces of the lugs, one on each side, thickness 6 mm. Add 3 mm fillets to the base perimeter and 2 mm fillets at lug-to-base transitions."

# ============================================================================
# Workflow Routing
# ============================================================================
#
# Which workflow to use:
#   "original" -- deterministic code translator first (uses _plan_to_code),
#                falls back to Aider on execution failure.  Faster and
#                cheaper for typical parts.
#   "aider"    -- Aider-first workflow.  Skips the deterministic coder,
#                uses Aider for initial generation.  Slower but more
#                flexible for non-standard parts.
#
# Entry points:
#   python -m multi_agent_cad.graph          -> "original"
#   python -m multi_agent_cad.graph_aider    -> "aider"
WORKFLOW_ID = "original"
'''


def reset_config() -> Path:
    """Rewrite config.py from the template. Returns the path written."""
    CONFIG_PATH.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return CONFIG_PATH


def show_template() -> None:
    """Print the default config.py template to stdout."""
    sys.stdout.write(CONFIG_TEMPLATE)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--reset":
        path = reset_config()
        print(f"[config] Restored defaults to: {path}")
        return 0
    if args[0] == "--show":
        show_template()
        return 0
    print(f"Unknown argument: {args[0]!r}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
