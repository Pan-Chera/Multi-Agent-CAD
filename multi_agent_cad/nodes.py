"""
LangGraph agent nodes for the Dual-Engine Multi-Agent CAD System.

Each node is a pure function that reads from ``GraphState`` and returns a
partial ``dict`` of state updates (LangGraph reducer semantics merge the
returned dict into the shared state).

This module keeps the node entry points and shared configuration/cache state.
Internal helpers live in:
  * _node_codegen     — CAD translation and Python-coder helpers
  * _node_step_qa     — STEP topology verification (Engine A)
  * _node_mesh_qa     — STL mesh verification (Engine B)
  * _node_qa_reports  — report merging and geometry/request checks
  * _node_execution   — child environments, execution and print orientation
  * _node_repair      — Aider generation/repair and direct-API fallback

Helpers resolve replaceable dependencies through this module at call time.
Keep shared state here: assembly runners override _CACHE_DIR per part, and
token_tracker wraps the node_* entry points exported here. Do not snapshot
these bindings in the implementation modules.

Nodes implemented in this file:
  * node_spec_planner     — user_request → CADBrief
  * node_geometric_architect — CADBrief → ArchitectPlan
  * node_python_coder     — ArchitectPlan → build123d Python code → .step + .stl
  * node_autonomous_skill_loop — internal Aider + Dual-QA closed loop
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import traceback
from pathlib import Path

from openai import OpenAI
from pydantic import ValidationError

from multi_agent_cad.schemas import (
    ArchitectPlan,
    CADBrief,
    ErrorType,
    EngineReport,
    GraphState,
    JudgeAction,
    JudgeDecision,
    QAReport,
    VerificationResult,
    VerificationTarget,
)

# ---------------------------------------------------------------------------
# Safe-print helper — prevents UnicodeEncodeError on Windows GBK terminals
# ---------------------------------------------------------------------------


def _safe_print(*args, **kwargs) -> None:
    """Print safely on Windows terminals that can't handle Unicode."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        safe_args = [
            str(a).encode("ascii", errors="replace").decode("ascii")
            for a in args
        ]
        print(*safe_args, **kwargs)


# ---------------------------------------------------------------------------
# C3 — Generated-Python subprocess env allowlist.
#
# The LLM-authored CAD script is executed via ``subprocess.run``. Passing the
# full parent env leaks every operator-set secret (ANTHROPIC_API_KEY,
# DEEPSEEK_API_KEY, etc.) plus the request-body api_key (DASHSCOPE_API_KEY,
# OPENAI_API_KEY, OPENAI_API_BASE) into the child. The allowlist below keeps
# only the runtime vars the script actually needs (PATH, locale, library
# search paths, temp dir, PYTHONPATH) and passes ITERATION explicitly.
#
# This is env-secret stripping only. The generated Python still has the
# launching user's filesystem and network access, so this is NOT a security
# sandbox; the SECURITY.md disclaimer continues to apply. Real sandboxing
# (containers / namespaces) is deferred to a future minor release.
# ---------------------------------------------------------------------------
_CHILD_ENV_ALLOWLIST = frozenset({
    "PATH",
    "HOME",
    "USERPROFILE",       # pathlib/ezdxf home lookup on Windows
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONPATH",
    "PYTHONIOENCODING",
    "LD_LIBRARY_PATH",   # OCP / build123d native libs on Linux
    "DYLD_LIBRARY_PATH",  # OCP / build123d native libs on macOS
    "SYSTEMROOT",        # Windows native DLL / side-by-side assembly loading
    "WINDIR",
})

_CHILD_ENV_ALLOW_OVERRIDE = "MAC_CHILD_ENV_ALLOW"


# ---------------------------------------------------------------------------
# Qwen / DashScope client factory
# ---------------------------------------------------------------------------
#
# All user-tunable values (model, timeouts, retries, pricing, workflow) now
# live in ``multi_agent_cad/config.py``.  Edit that file to customize.  Reset
# to defaults via: ``python -m multi_agent_cad._config_defaults --reset``.

from multi_agent_cad.config import (
    MAX_RETRIES as _CFG_MAX_RETRIES,
    MAX_EXEC_RETRIES as _CFG_MAX_EXEC_RETRIES,
    LLM_API_TIMEOUT as _CFG_LLM_API_TIMEOUT,
    LLM_CODEGEN_API_TIMEOUT as _CFG_LLM_CODEGEN_API_TIMEOUT,
    CHECK_MESH_TIMEOUT as _CFG_CHECK_MESH_TIMEOUT,
    CAD_SCRIPT_TIMEOUT as _CFG_CAD_SCRIPT_TIMEOUT,
    CHECKPOINT_INPUT_TIMEOUT as _CFG_CHECKPOINT_INPUT_TIMEOUT,
    INTERVENTION_INPUT_TIMEOUT as _CFG_INTERVENTION_INPUT_TIMEOUT,
    DS_BASE_URL as _DS_BASE_URL,
    API_KEY_ENV_VAR as _API_KEY_ENV_VAR,
    API_BASE_ENV_VAR as _API_BASE_ENV_VAR,
    # Stage 1: Spec Planner
    SPEC_PLANNER_MODEL as _SP_MODEL,
    SPEC_PLANNER_TEMPERATURE as _SP_TEMP,
    SPEC_PLANNER_MAX_TOKENS as _SP_MAX_TOKENS,
    SPEC_PLANNER_KWARGS as _SPEC_PLANNER_KWARGS,
    # Stage 1b: Spec Planner multimodal mode (user-provided reference images)
    SPEC_PLANNER_MULTIMODAL as _CFG_SP_MULTIMODAL,
    # Stage 0: User input images (shared by Spec Planner + Judge)
    USER_IMAGES_DIR as _CFG_USER_IMAGES_DIR,
    USER_IMAGE_MAX_SIZE as _CFG_USER_IMAGE_MAX_SIZE,
    USER_IMAGE_JPEG_QUALITY as _CFG_USER_IMAGE_JPEG_QUALITY,
    # Stage 2: Geometric Architect
    ARCHITECT_MODEL as _ARCH_MODEL,
    ARCHITECT_TEMPERATURE as _ARCH_TEMP,
    ARCHITECT_MAX_TOKENS as _ARCH_MAX_TOKENS,
    ARCHITECT_KWARGS as _ARCHITECT_KWARGS,
    # Stage 3: Python Coder
    CODER_MODEL as _CODER_MODEL,
    CODER_TEMPERATURE as _CODER_TEMP,
    CODER_MAX_TOKENS as _CODER_MAX_TOKENS,
    CODER_KWARGS as _CODER_KWARGS,
    # Stage 4: Autonomous Skill Loop (Aider + direct API fallback)
    AIDER_MODEL as _AIDER_MODEL_NAME,
    AIDER_MAX_TOKENS as _AIDER_MAX_TOKENS,
    REPAIR_MODEL as _REPAIR_MODEL,
    REPAIR_TEMPERATURE as _REPAIR_TEMP,
    REPAIR_MAX_TOKENS as _REPAIR_MAX_TOKENS,
    REPAIR_KWARGS as _REPAIR_KWARGS,
    # Stage 5: QA Judge (Phase 2.5 in autonomous_skill_loop)
    JUDGE_ENABLED as _CFG_JUDGE_ENABLED,
    JUDGE_MIN_RETRY as _CFG_JUDGE_MIN_RETRY,
    JUDGE_MODEL as _JUDGE_MODEL,
    JUDGE_TEMPERATURE as _JUDGE_TEMP,
    JUDGE_MAX_TOKENS as _JUDGE_MAX_TOKENS,
    JUDGE_KWARGS as _JUDGE_KWARGS,
    # Stage 5b: QA Judge visual rendering (multimodal input)
    JUDGE_MULTIMODAL as _CFG_JUDGE_MULTIMODAL,
    JUDGE_VIEWS_COUNT as _CFG_JUDGE_VIEWS_COUNT,
    JUDGE_VIEW_SIZE as _CFG_JUDGE_VIEW_SIZE,
    JUDGE_SAVE_VIEWS as _CFG_JUDGE_SAVE_VIEWS,
)


# API key lives in config.py (DS_API_KEY). Imported alongside other config.
from multi_agent_cad.config import DS_API_KEY as _HARDCODED_API_KEY


def _llm_client() -> OpenAI:
    """Return an OpenAI-compatible client pointed at the DashScope API (Qwen).

    Priority: 1) environment variable ``DASHSCOPE_API_KEY``,
              2) ``DS_API_KEY`` in ``multi_agent_cad/config.py``.
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY", "") or _HARDCODED_API_KEY
    if not api_key:
        raise RuntimeError(
            "DASHSCOPE_API_KEY is not set.  Either export it as an environment "
            "variable or set DS_API_KEY in multi_agent_cad/config.py."
        )
    return OpenAI(base_url=_DS_BASE_URL, api_key=api_key)


# ---------------------------------------------------------------------------
# Pipeline cache
# ---------------------------------------------------------------------------

_CACHE_DIR = Path.cwd() / "pipeline_cache"

# build123d API reference — passed to Aider as a read-only context file
_BUILD123D_REF = str(Path(__file__).resolve().parent / "build123d_reference.md")

# System prompts — editable markdown files in prompts/
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a system prompt from ``prompts/{name}.md``.

    Advanced users can edit the markdown files directly to tweak agent
    behavior without touching Python code.  The autonomous repair prompt
    is built dynamically in ``_build_autonomous_repair_prompt`` and is
    not loaded here.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"Prompt file not found: {path}.  Restore it from a backup or "
            f"re-install the package."
        )
    return path.read_text(encoding="utf-8").strip() + "\n"


def _load_cache(path: Path):
    """Load a JSON file from the cache, or return None."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_cache(path: Path, data) -> None:
    """Save data as JSON to the cache directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def _planning_fingerprint(stage: str, payload) -> str:
    """Invalidate plans when their inputs, prompts, model or images change."""
    from multi_agent_cad import config as cfg

    prefix = "SPEC_PLANNER_" if stage == "spec_planner" else "ARCHITECT_"
    settings = {k: getattr(cfg, k) for k in dir(cfg) if k.startswith(prefix)}
    images_by_name = {}
    image_dir = Path.cwd() / _CFG_USER_IMAGES_DIR
    if _CFG_SP_MULTIMODAL != "never":
        from multi_agent_cad.image_preprocess import _repo_root
        for directory in (image_dir, _repo_root() / image_dir.name):
            if not directory.is_dir():
                continue
            for p in directory.iterdir():
                name = p.name.lower()
                if name not in images_by_name and p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    try:
                        images_by_name[name] = hashlib.sha256(p.read_bytes()).hexdigest()
                    except OSError:
                        images_by_name[name] = "unreadable"
    images = sorted(images_by_name.items())
    data = {"payload": payload, "settings": settings, "images": images,
            "endpoint": _DS_BASE_URL, "prompt": _load_prompt(stage),
            "image_settings": [_CFG_SP_MULTIMODAL, _CFG_USER_IMAGE_MAX_SIZE,
                               _CFG_USER_IMAGE_JPEG_QUALITY]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_or_dict_to_json(plan: ArchitectPlan | dict | None) -> str:
    """Serialize an ArchitectPlan (pydantic or raw dict) to indented JSON."""
    if plan is None:
        return "{}"
    if hasattr(plan, "model_dump_json"):
        return plan.model_dump_json(indent=2)  # type: ignore[union-attr]
    return json.dumps(plan, indent=2, default=str)


def _call_llm_json_with_retry(
    client,
    messages: list[dict],
    *,
    model: str,
    max_tokens: int,
    temperature: float,
    extra_kwargs: dict | None = None,
    max_retries: int = 3,
) -> tuple[str, str]:
    """Call Qwen (DashScope), extract JSON, and retry on parse errors.

    On JSON or schema validation failure, the error is appended to the
    conversation and the LLM is re-called — much cheaper than a full
    graph-level retry that loses context.

    Parameters
    ----------
    model : str
        Model ID (e.g., ``SPEC_PLANNER_MODEL`` from config).
    max_tokens : int
        Output token cap for this call.
    temperature : float
        Sampling temperature.
    extra_kwargs : dict | None
        Optional extra kwargs (e.g., ``{"extra_body": {"enable_thinking": ...}}``).
        If None, no extra kwargs are passed.

    Returns
    -------
    (json_str, raw_response)
        The extracted JSON string and the final raw LLM response.

    Raises
    ------
    ValueError
        If the LLM returns empty content after all retries.
    json.JSONDecodeError
        If JSON parsing still fails after all retries.
    """
    json_str = ""
    raw_response = ""
    kwargs_to_use = extra_kwargs if extra_kwargs is not None else {}
    for attempt in range(max_retries):
        try:
            print(f"[API CALL] Attempt {attempt + 1}/{max_retries}...", flush=True)
            response = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=list(messages),
                max_tokens=max_tokens,
                timeout=_CFG_LLM_API_TIMEOUT,
                **kwargs_to_use,
            )
            raw_response = response.choices[0].message.content or ""

            # -- Defensive: empty response detection --
            if not raw_response.strip():
                finish = getattr(response.choices[0], "finish_reason", "?")
                print(
                    f"\n[DEBUG JSON-RETRY] Empty response on attempt {attempt + 1}/{max_retries} "
                    f"(finish_reason={finish})"
                )
                if attempt < max_retries - 1:
                    messages.append({"role": "assistant", "content": "(empty response)"})
                    messages.append(
                        {"role": "user",
                         "content": "Your response was empty. Return ONLY a valid JSON object inside a ```json fence."}
                    )
                    continue
                raise ValueError(
                    f"Empty LLM response after {max_retries} attempts "
                    f"(finish_reason={finish})"
                )

            json_str = _extract_json_from_llm(raw_response)
            json.loads(json_str)  # validate parseable

            # -- If we got here, JSON is valid.  Return raw for caller to validate. --
            return json_str, raw_response

        except json.JSONDecodeError as e:
            print(
                f"\n[DEBUG JSON-RETRY] JSON decode error on attempt {attempt + 1}/{max_retries}: {e}"
            )
            if attempt < max_retries - 1:
                # Feed error back to LLM for self-correction
                messages.append(
                    {"role": "assistant", "content": raw_response[:2000]}
                )
                messages.append(
                    {"role": "user",
                     "content": (
                         f"JSON ERROR on attempt {attempt + 1}: {e}\n\n"
                         f"The JSON you returned cannot be parsed.  Fix it and "
                         f"return ONLY a valid, complete JSON object inside a "
                         f"```json fenced code block.  Do NOT add explanatory text."
                     )}
                )
            else:
                raise

        except Exception as e:
            # BUG-022: narrow retryability classification. Previously
            # only "timeout" was retried, so 429 / 5xx / connection
            # errors (transient) were treated as permanent. Conversely,
            # 401/403/invalid-api-key/model-not-found (permanent) must
            # NOT be retried -- they burn the retry budget on errors
            # that no amount of retrying will fix.
            error_msg = str(e).lower()
            # Permanent failures: auth / config / missing dependency.
            # Match the same patterns used by is_non_retryable_error in
            # part_generator -- single source of truth for retryability.
            _non_retryable_patterns = (
                "dashscope_api_key is not set",
                "error code: 401",
                "error code: 403",
                "authenticationerror",
                "permissiondeniederror",
                "invalid api key",
                "invalid api-key",
                "incorrect api key",
                "unauthorized",
                "model not found",
                "model not exist",
                "invalid model",
                "invalid url",
                "modulenotfounderror",
            )
            if any(p in error_msg for p in _non_retryable_patterns):
                raise
            # Transient failures: retry on timeout, 429, 5xx, or
            # connection errors. These often self-resolve on retry.
            _retryable_patterns = (
                "timeout", "timed out",
                "error code: 429", "rate limit", "rate_limit",
                "error code: 500", "error code: 502", "error code: 503",
                "error code: 504", "server error", "service unavailable",
                "connection error", "connection aborted", "connection reset",
                "connection refused", "remote disconnected",
                "apiconnectionerror", "apierror",
            )
            is_retryable = any(p in error_msg for p in _retryable_patterns)
            if is_retryable:
                print(
                    f"\n[DEBUG JSON-RETRY] Retryable error on attempt "
                    f"{attempt + 1}/{max_retries}: {e}"
                )
                if attempt < max_retries - 1:
                    import time
                    time.sleep(2)
                    continue
            # Re-raise non-retryable-but-unrecognized errors or final
            # retry of a retryable class (conservative: do not loop
            # forever).
            raise

    raise RuntimeError("Unreachable: retry loop should have raised or returned")


def _parse_missed_cuts(missed: list[str]) -> dict[str, list[str]]:
    """Categorize entries from temp_missed_{iter}.json by error type.

    The generated script writes all runtime diagnostics into a single
    ``_MISSED_CUTS`` list.  Each entry is prefixed with its type:
    ``MISSED_CUT:``, ``CUT_ERROR:``, ``FILLET_FAILED:``, ``CHAMFER_FAILED:``.

    Returns a dict keyed by category name with lists of raw entries.
    """
    categories: dict[str, list[str]] = {
        "missed_cut": [],
        "fillet_failed": [],
        "chamfer_failed": [],
        "cut_error": [],
        "cosmetic_warning": [],
    }
    for entry in missed:
        s = str(entry)
        # A partial/degraded edge treatment leaves valid geometry behind.  A
        # FAILED entry with no selected edges is likewise non-structural: the
        # requested cosmetic operation was omitted, but no boolean or mating
        # feature failed.  Keep these visible without trapping the autonomous
        # loop in expensive rewrites of an otherwise valid part.
        if s.startswith((
            "FILLET_DEGRADED", "FILLET_PARTIAL",
            "CHAMFER_DEGRADED", "CHAMFER_PARTIAL",
        )) or (
            s.startswith(("FILLET_FAILED", "CHAMFER_FAILED"))
            and "no edges matched filter" in s.lower()
        ):
            categories["cosmetic_warning"].append(s)
        elif s.startswith("FILLET_FAILED"):
            categories["fillet_failed"].append(s)
        elif s.startswith("CHAMFER_FAILED"):
            categories["chamfer_failed"].append(s)
        elif s.startswith("MISSED_CUT"):
            categories["missed_cut"].append(s)
        else:
            categories["cut_error"].append(s)
    return categories


def _format_missed_cuts_errors(categories: dict[str, list[str]]) -> tuple[list[str], str]:
    """Build error_details list and summary label from categorized missed cuts.

    Returns ``(error_details, summary_label)`` where summary_label is the
    dominant error type (e.g. "CUT_POSITION_ERROR", "FILLET_FAILED").
    """
    error_details: list[str] = []
    # ``cosmetic_warning`` is intentionally omitted: callers still print the
    # raw diagnostics, but only structurally meaningful failures block PASS.
    counts = {
        k: len(v) for k, v in categories.items()
        if v and k != "cosmetic_warning"
    }

    if categories["missed_cut"]:
        n = len(categories["missed_cut"])
        error_details.append(
            f"CUT_POSITION_ERROR: {n} cut(s) missed the target body "
            f"(tool did NOT remove any material). "
            f"Check Pos() coordinates and Cylinder height of cut tools."
        )
        for m in categories["missed_cut"][:3]:
            error_details.append(str(m)[:200])

    if categories["fillet_failed"]:
        n = len(categories["fillet_failed"])
        error_details.append(
            f"FILLET_FAILED: {n} fillet operation(s) failed. "
            f"Try smaller radius, use try/except, or filter edges more precisely."
        )
        for m in categories["fillet_failed"][:3]:
            error_details.append(str(m)[:200])

    if categories["chamfer_failed"]:
        n = len(categories["chamfer_failed"])
        error_details.append(
            f"CHAMFER_FAILED: {n} chamfer operation(s) failed. "
            f"Try smaller length or filter edges more precisely."
        )
        for m in categories["chamfer_failed"][:3]:
            error_details.append(str(m)[:200])

    if categories["cut_error"]:
        for m in categories["cut_error"][:3]:
            error_details.append(str(m)[:200])

    # Determine dominant label
    if counts.get("missed_cut", 0) >= counts.get("fillet_failed", 0) and counts.get("missed_cut", 0) > 0:
        label = "CUT_POSITION_ERROR"
    elif counts.get("fillet_failed", 0) > 0:
        label = "FILLET_FAILED"
    elif counts.get("chamfer_failed", 0) > 0:
        label = "CHAMFER_FAILED"
    else:
        label = "CUT_ERROR"

    return error_details, label


def _runtime_diagnostics_path(cwd: Path, iteration: int) -> Path | None:
    """Return diagnostics produced by this exact execution iteration only.

    Never fall back to ``temp_missed_0.json``: generated scripts omit the
    file when an Aider repair removes every issue, so such a fallback revives
    stale failures and makes a corrected part impossible to pass.
    """
    path = cwd / f"temp_missed_{iteration}.json"
    return path if path.is_file() else None


def _normalize_architect_plan(plan_dict: dict) -> dict:
    """Fix common LLM naming mistakes in step_type before Pydantic validation.

    The Architect LLM sometimes uses informal names that don't match the
    ``ModelingStepType`` enum.  This function maps them to valid values so
    the plan passes validation without wasting a retry round.

    For primitive-solid step types (``box`` / ``cube`` / ``cylinder`` /
    ``sphere`` / ``cone``) — which the LLM reaches for despite
    ``geometric_architect.md`` Iron Rule 6 forbidding them — expand the
    step into a ``sketch + extrude`` pair so the plan validates without a
    retry. The LLM is trained on ``python_coder.md`` which teaches
    ``Box(x,y,z)`` as a primitive, so the cross-stage bleed is common
    even with the prompt rule.
    """
    _STEP_TYPE_ALIASES = {
        # LLM shorthand → correct enum value
        "pattern": "pattern_circular",   # ambiguous, default to circular (most common)
        "linear_pattern": "pattern_linear",
        "circular_pattern": "pattern_circular",
        "union": "boolean_union",
        "cut": "boolean_cut",
        "intersect": "boolean_intersect",
        "boolean_subtract": "boolean_cut",
        "subtract": "boolean_cut",
        # Non-existent operations → closest equivalent
        "sweep": "extrude",             # sweep ≈ extrude along path; LLM must refine
        "loft": "extrude",              # loft ≈ extrude; LLM must refine
    }
    # Primitive-solid step types forbidden by Iron Rule 6 but routinely
    # emitted by the LLM. Mapped to ``extrude`` and, if the step carries
    # dimensions info, expanded into a sketch+extrude pair (see below).
    _PRIMITIVE_SOLID_TYPES = {"box", "cube", "cylinder", "sphere", "cone",
                              "primitive", "solid"}

    steps = plan_dict.get("steps")
    if not isinstance(steps, list):
        return plan_dict

    # First pass: simple alias substitution for non-primitive types.
    for step in steps:
        if not isinstance(step, dict):
            continue
        stype = step.get("step_type", "")
        if isinstance(stype, str) and stype in _STEP_TYPE_ALIASES:
            step["step_type"] = _STEP_TYPE_ALIASES[stype]

    # Second pass: expand primitive-solid steps (box/cube/...) into
    # sketch+extrude. These steps carry dimensions in ad-hoc fields the
    # ModelingStep schema doesn't know (pydantic ignores unknowns), so
    # without expansion the schema would pass but the Coder would emit
    # no geometry (sketch_id=None, distance_mm=None). Expanding here
    # preserves the LLM's dimensional intent.
    sketches = plan_dict.get("sketches")
    if not isinstance(sketches, list):
        sketches = []
        plan_dict["sketches"] = sketches

    new_steps: list = []
    for step in steps:
        if not isinstance(step, dict):
            new_steps.append(step)
            continue
        stype = step.get("step_type", "")
        if not (isinstance(stype, str) and stype.lower() in _PRIMITIVE_SOLID_TYPES):
            new_steps.append(step)
            continue

        dims = _extract_primitive_dimensions(step)
        if dims is None:
            # No dimensions found — fallback to plain alias so schema
            # validates. Coder/Aider retry will recover the geometry.
            step["step_type"] = "extrude"
            new_steps.append(step)
            print(f"[NORMALIZE] step_type={stype!r} has no dimensions; "
                  f"aliasing to 'extrude' (Coder will need sketch_id)")
            continue

        # Got dimensions — expand into sketch + extrude pair. The new
        # sketch is appended to plan_dict.sketches; the original step is
        # rewritten in-place as the extrude step.
        prim_kind = stype.lower()
        sid_base = step.get("step_id") or f"step-{len(new_steps)+1:02d}"
        sketch_id = f"{sid_base}-autogen-sketch"
        sketch, extrude_step = _build_primitive_sketch_and_extrude(
            prim_kind, dims, step, sketch_id
        )
        if sketch is None:
            # Unsupported primitive (e.g. sphere has no sketch analogue) —
            # alias to extrude and let Aider recover geometry from notes.
            step["step_type"] = "extrude"
            new_steps.append(step)
            print(f"[NORMALIZE] step_type={stype!r} unsupported primitive "
                  f"shape; aliasing to 'extrude'")
            continue
        sketches.append(sketch)
        new_steps.append(extrude_step)
        print(f"[NORMALIZE] expanded step_type={stype!r} dims={dims} "
              f"into sketch {sketch_id!r} + extrude")

    plan_dict["steps"] = new_steps
    return plan_dict


# ============================================================================
# System Prompt — Python Coder
# ============================================================================

SYSTEM_PROMPT_PYTHON_CODER = _load_prompt("python_coder")

# ============================================================================
# System Prompt — QA Judge (Phase 2.5 in autonomous_skill_loop)
# ============================================================================

# Module-level loading — fail-fast if qa_judge.md is missing (consistent with
# SYSTEM_PROMPT_PYTHON_CODER and SYSTEM_PROMPT_SPEC_PLANNER loading strategy).
# F12 fix: was previously lazy-loaded via _get_judge_system_prompt() to avoid
# reading the file when JUDGE_ENABLED=False, but the prompt file read has
# negligible cost (~1ms) and fail-fast is more consistent.
SYSTEM_PROMPT_QA_JUDGE = _load_prompt("qa_judge")

# F5 fix: narrowed multimodal-unsupported keyword detection.
# Previous list ("image", "unsupported", "not support") was too broad and would
# false-positive on errors like "rate limit exceeded for image-generation model"
# or "model does not support streaming". New approach: two-part check — error
# must mention BOTH an image/vision/multimodal concept AND an unsupported/
# not-allowed concept. This eliminates false positives on non-multimodal errors.
_MULTIMODAL_KEYWORDS = (
    "image input", "image_url", "image content", "image_content",
    "vision input", "vision model",
    "multimodal", "multi-modal",
    "does not support image", "not support image",
    "does not support vision", "not support vision",
    "image not allowed", "image not support",
)
_UNSUPPORTED_KEYWORDS = (
    "not support", "unsupported", "not allowed", "does not support",
    "invalid content", "is not support",
)


def _is_multimodal_unsupported_error(err_str: str) -> bool:
    """Two-part check: error must mention BOTH multimodal concept AND unsupported concept."""
    has_multimodal = any(k in err_str for k in _MULTIMODAL_KEYWORDS)
    has_unsupported = any(k in err_str for k in _UNSUPPORTED_KEYWORDS)
    return has_multimodal and has_unsupported


# ============================================================================
# Node: Python Coder
# ============================================================================


def node_python_coder(state: GraphState) -> dict:
    """Translate an ArchitectPlan into executable build123d Python code.

    1. Reads the ArchitectPlan and any previous QA feedback from state.
    2. Calls Qwen 3.8-max (DashScope) to generate self-contained build123d code.
    3. Writes the code to ``temp_design_{iteration}.py``.
    4. Executes the script via ``subprocess.run``.
    5. On success: returns ``current_python_code``, ``current_step_path``,
       ``current_stl_path``.
    6. On failure: returns ``error_type=DIMENSION`` with stderr injected into
       a mock QA report so the Coder can self-correct on the next iteration.

    Parameters
    ----------
    state : GraphState
        The shared LangGraph state.  Must contain ``architect_plan`` and
        ``iteration_count``.  Optionally contains ``qa_report`` for
        dimension-level feedback loops.

    Returns
    -------
    dict
        Partial state update.  LangGraph merges this into the shared state.
    """
    # ------------------------------------------------------------------
    # 1. Extract state
    # ------------------------------------------------------------------
    iteration: int = state.get("iteration_count", 0)
    architect_plan = state.get("architect_plan")
    # Pre-compute the node_history value for _coder_failure_state calls.
    # This preserves the accumulated history so the routing retry budget
    # in graph.py (route_after_coder) counts correctly.
    _coder_history = list(state.get("node_history", [])) + ["coder"]

    # -- Route: deterministic translator on ALL iterations (no LLM fallback)
    if architect_plan is not None:
        result = _node_python_coder_deterministic(state, architect_plan, iteration)
        if result is not None:
            return result
        # Deterministic failed (NotImplementedError) → fall back to LLM

    # Original LLM-based path follows...
    qa_report = state.get("qa_report")
    cwd = Path.cwd()

    if architect_plan is None:
        return {
            "error_type": ErrorType.FATAL,
            "execution_log": [
                "node_python_coder: FATAL — architect_plan is None. "
                "The Geometric Architect must run before the Python Coder."
            ],
            "node_history": list(state.get("node_history", [])) + ["coder"],
        }

    # Serialize the plan to JSON for the LLM prompt
    plan_json = _plan_or_dict_to_json(architect_plan)

    # Extract previous QA error details if we're in a DIMENSION retry loop
    previous_feedback = ""
    if qa_report is not None:
        qa_errors = _extract_qa_error_details(qa_report)
        previous_feedback = _qa_report_or_dict(qa_errors)

    # ------------------------------------------------------------------
    # 2. Determine output paths
    # ------------------------------------------------------------------
    # We write artifacts into the current working directory with iteration-
    # scoped names so each retry is isolated and auditable.
    script_path = cwd / f"temp_design_{iteration}.py"
    step_path = cwd / f"temp_output_{iteration}.step"
    stl_path = cwd / f"temp_output_{iteration}.stl"

    step_path_str = str(step_path.resolve())
    stl_path_str = str(stl_path.resolve())

    # ------------------------------------------------------------------
    # 3. Build the user prompt
    # ------------------------------------------------------------------
    user_prompt = _build_coder_user_prompt(
        plan_json=plan_json,
        previous_feedback=previous_feedback,
        step_path=step_path_str,
        stl_path=stl_path_str,
        user_request=state.get("user_request", ""),
    )

    # ------------------------------------------------------------------
    # 4. Call Qwen (DashScope)
    # ------------------------------------------------------------------
    client = _llm_client()

    try:
        response = client.chat.completions.create(
            model=_CODER_MODEL,
            temperature=_CODER_TEMP,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_PYTHON_CODER},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=_CODER_MAX_TOKENS,
            timeout=_CFG_LLM_CODEGEN_API_TIMEOUT,
            **_CODER_KWARGS,
        )
        raw_response = response.choices[0].message.content or ""
    except Exception as exc:
        # API-level failure — bubble up as a DIMENSION retry with the error
        return _coder_failure_state(
            iteration=iteration,
            error_message=f"LLM API call failed: {exc}",
            script_path=str(script_path),
            node_history=_coder_history,
        )

    # ------------------------------------------------------------------
    # 5. Parse the generated code
    # ------------------------------------------------------------------
    python_code = _extract_code_from_llm_response(raw_response)

    if not python_code or len(python_code.strip()) < 20:
        return _coder_failure_state(
            iteration=iteration,
            error_message="LLM returned empty or unreasonably short code.",
            script_path=str(script_path),
            raw_response=raw_response,
            node_history=_coder_history,
        )

    # ------------------------------------------------------------------
    # 6. Inject API compat shim AFTER "from build123d import *"
    # ------------------------------------------------------------------
    shim_code = (
        "\n# === AUTO-INJECTED: build123d API compat shim ===\n"
        "import functools, inspect as _inspect\n"
        "def _fx(func, aliases=None, strip=True):\n"
        "    aliases=aliases or {}\n"
        "    @functools.wraps(func)\n"
        "    def w(*a,**kw):\n"
        "        ok=set(_inspect.signature(func).parameters.keys())\n"
        "        f={}\n"
        "        for k,v in kw.items():\n"
        "            if k in ok:f[k]=v\n"
        "            elif k in aliases:f[aliases[k]]=v\n"
        "            elif not strip:f[k]=v\n"
        "        return func(*a,**f)\n"
        "    return w\n"
        "import build123d.operations_part as _bp\n"
        "import build123d.operations_generic as _bg\n"
        "import build123d.operations_sketch as _bs\n"
        "_bp.extrude=_fx(_bp.extrude,{'direction':'dir'})\n"
        "_bp.revolve=_fx(_bp.revolve,{'angle':'revolution_arc'})\n"
        "_bg.fillet=_fx(_bg.fillet,{'edges':'objects'})\n"
        "_bg.chamfer=_fx(_bg.chamfer,{'edges':'objects'})\n"
        "_bg.mirror=_fx(_bg.mirror,{})\n"
        "# make_face: accept wire arg (LLM often passes wire)\n"
        "_bs.make_face=_fx(_bs.make_face,{},strip=True)\n"
        "from build123d import Hole as _H\n"
        "_H.__init__=_fx(_H.__init__,{})\n"
        "# BuildPart/BuildSketch/BuildLine: workplanes is *args, not kwarg\n"
        "from build123d import BuildPart as _BP_cls, BuildSketch as _BS_cls, BuildLine as _BL_cls\n"
        "_BP_orig_init=_BP_cls.__init__\n"
        "_BS_orig_init=_BS_cls.__init__\n"
        "_BL_orig_init=_BL_cls.__init__\n"
        "def _bp_init(self,*a,mode=Mode.ADD,**kw):\n"
        "    wp=kw.pop('workplanes',a[0] if a else Plane.XY)\n"
        "    return _BP_orig_init(self,wp,mode=mode,**kw)\n"
        "def _bs_init(self,*a,mode=Mode.ADD,**kw):\n"
        "    wp=kw.pop('workplanes',a[0] if a else Plane.XY)\n"
        "    return _BS_orig_init(self,wp,mode=mode,**kw)\n"
        "def _bl_init(self,*a,mode=Mode.ADD,**kw):\n"
        "    wp=kw.pop('workplane',kw.pop('workplanes',a[0] if a else Plane.XY))\n"
        "    return _BL_orig_init(self,wp,mode=mode,**kw)\n"
        "_BP_cls.__init__=_bp_init\n"
        "_BS_cls.__init__=_bs_init\n"
        "_BL_cls.__init__=_bl_init\n"
        "extrude,revolve,fillet,chamfer,mirror,make_face=_bp.extrude,_bp.revolve,_bg.fillet,_bg.chamfer,_bg.mirror,_bs.make_face\n"
        "Hole,BuildPart,BuildSketch,BuildLine=_H,_BP_cls,_BS_cls,_BL_cls\n"
        "# === END SHIM ===\n\n"
    )
    # Find "from build123d import *" and insert shim after it
    import re as _re
    injected = _re.sub(
        r'(from build123d import \*.*\n)',
        r'\1' + shim_code,
        python_code,
        count=1,
    )
    try:
        script_path.write_text(injected if injected != python_code else _build_shim_str() + "\n" + python_code, encoding="utf-8")
    except OSError as exc:
        return _coder_failure_state(
            iteration=iteration,
            error_message=f"Failed to write {script_path}: {exc}",
            script_path=str(script_path),
            node_history=_coder_history,
        )

    # ------------------------------------------------------------------
    # 7. Execute the script
    # ------------------------------------------------------------------
    try:
        _remove_execution_outputs(step_path, stl_path, iteration)
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_CFG_CAD_SCRIPT_TIMEOUT,
            cwd=str(cwd),
            env=_build_child_env(iteration),
        )
    except subprocess.TimeoutExpired:
        return _coder_failure_state(
            iteration=iteration,
            error_message=(
                f"Script execution timed out after {_CFG_CAD_SCRIPT_TIMEOUT} seconds."
            ),
            script_path=str(script_path),
            node_history=_coder_history,
        )
    except Exception as exc:
        return _coder_failure_state(
            iteration=iteration,
            error_message=f"subprocess.run raised: {exc}",
            script_path=str(script_path),
            node_history=_coder_history,
        )

    # ------------------------------------------------------------------
    # 8. Check results
    # ------------------------------------------------------------------
    if result.returncode != 0:
        stderr_summary = result.stderr.strip()
        stdout_tail = _tail(result.stdout.strip(), 60)
        error_message = (
            f"Script exited with returncode {result.returncode}.\n\n"
            f"--- STDERR ---\n{stderr_summary}\n\n"
            f"--- STDOUT (last 60 lines) ---\n{stdout_tail}"
        )
        # Print full stderr to console for debugging
        print(f"\n[DEBUG CODER] Script CRASHED: {script_path}")
        _safe_print(f"[DEBUG CODER] STDERR:\n{stderr_summary}\n")
        # Save a copy of the failed script for manual inspection
        debug_copy = script_path.with_suffix(".fail.py")
        try:
            shutil.copy(str(script_path), str(debug_copy))
            print(f"[DEBUG CODER] Failed script saved to: {debug_copy}\n")
        except Exception:
            pass
        return _coder_failure_state(
            iteration=iteration,
            error_message=error_message,
            script_path=str(script_path),
            node_history=_coder_history,
        )

    # Verify output files exist (defensive check)
    missing_files: list[str] = []
    if not step_path.is_file() or step_path.stat().st_size == 0:
        missing_files.append(str(step_path))
    if not stl_path.is_file() or stl_path.stat().st_size == 0:
        missing_files.append(str(stl_path))

    if missing_files:
        return _coder_failure_state(
            iteration=iteration,
            error_message=(
                f"Script ran successfully but did not produce expected output "
                f"files: {', '.join(missing_files)}.  "
                f"Ensure export_step() and export_stl() are called with the "
                f"exact paths provided."
            ),
            script_path=str(script_path),
            node_history=_coder_history,
        )

    # ------------------------------------------------------------------
    # 9. Success — return updated state
    # ------------------------------------------------------------------
    log_lines: list[str] = [
        f"node_python_coder [iter {iteration}]: SUCCESS",
        f"  Script : {script_path}",
        f"  STEP   : {step_path} ({_file_size_kb(step_path)})",
        f"  STL    : {stl_path} ({_file_size_kb(stl_path)})",
    ]
    if result.stdout.strip():
        log_lines.append(f"  stdout : {_tail(result.stdout.strip(), 10)}")

    return {
        "current_python_code": python_code,
        "current_python_code_path": str(script_path.resolve()),
        "current_step_path": str(step_path.resolve()),
        "current_stl_path": str(stl_path.resolve()),
        "execution_log": log_lines,
        "node_history": list(state.get("node_history", [])) + ["coder"],
        # Keep error_type unchanged — the QA node will set it properly
    }

# Shared QA locations and protocol constants. Implementations live in
# _node_step_qa.py, _node_mesh_qa.py and _node_qa_reports.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHECK_MESH_SCRIPT = _REPO_ROOT / "legacy_refs" / "check_mesh.py"
_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
# Bump when check_mesh.py introduces a breaking JSON protocol change.
_EXPECTED_CHECK_MESH_SCHEMA = "2.0.0"


def _extract_json_from_llm(raw: str) -> str:
    """Aggressively extract JSON object from LLM response.

    1. Try `` ```json { ... } ``` `` or `` ``` { ... } ``` `` fences. If
       multiple fenced JSON blocks appear (e.g. an example followed by the
       actual answer), the *last* one is returned -- matches the contract
       of ``_extract_code_from_llm_response`` where the final block is the
       complete payload.
    2. Fall back to outermost ``{ ... }`` span via rfind.
    3. Return raw text if nothing matches.
    """
    # Priority 1: fenced JSON block — use greedy .* so nested braces work
    pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
    matches = re.findall(pattern, raw, flags=re.DOTALL)
    if matches:
        content = matches[-1].strip()
        # Find the outermost JSON object inside the fence content
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            return content[start:end + 1].strip()
        return content

    # Priority 2: find outermost { ... } span in the full response
    start_idx = raw.find("{")
    end_idx = raw.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return raw[start_idx:end_idx + 1].strip()

    return raw.strip()


# ============================================================================
# QA Judge — model can self-terminate iteration on QA disagreement
# ============================================================================


def node_judge_qa(
    *,
    user_request: str,
    qa_report: QAReport,
    special_features: list[str],
    feature_measurements: dict | None,
    retry: int,
    workflow_id: str,
    current_stl_path: str | None = None,
    semantic_check: bool = False,
) -> JudgeDecision | None:
    """Evaluate whether a QA report's failures warrant code repair.

    This is the QA Judge agent (Phase 2.5 in ``node_autonomous_skill_loop``).
    The Judge is a separate LLM call with its own prompt
    (``prompts/qa_judge.md``) and structured JSON output. It can:

    - ``accept`` — override QA failure as PASS, return ``ErrorType.NONE``
    - ``repair`` — continue to Aider (normal flow)
    - ``halt`` — stop iteration as ``ErrorType.FATAL``

    Anti-hallucination design: the Judge sees only structured text
    (feature_measurements, error_details, special_features, user_request)
    plus **optionally** rendered multi-angle PNG views of the current STL
    (when ``JUDGE_MULTIMODAL`` is enabled and the model supports vision).
    Every accept/halt decision must cite concrete data points in the
    ``evidence`` field. Empty evidence on ACCEPT/HALT → downgrade to REPAIR
    by the caller (Phase 2.5 evidence gate in ``node_autonomous_skill_loop``).

    Visual input (when enabled):
    - ``JUDGE_MULTIMODAL="never"`` (off): text-only path, no rendering.
    - ``JUDGE_MULTIMODAL="auto"`` (default): render N isometric PNG views
      from ``current_stl_path``, send as ``image_url`` content blocks
      alongside the text prompt. If the API errors out with a
      multimodal-related message, retry without images (text-only fallback).
    - ``JUDGE_MULTIMODAL="always"``: require images; if rendering failed,
      return None (safe default → REPAIR).

    Parameters
    ----------
    user_request : str
        Original natural-language design intent (ground truth).
    qa_report : QAReport
        Merged QA report from the dual-engine QA node.
    special_features : list[str]
        Non-trivial geometric constraints extracted by Spec Planner
        (symmetry, multi-body intent, feature placement rules, etc.).
    feature_measurements : dict | None
        White-box instrumentation: per-feature bounding boxes from
        ``temp_measurements_{iter}.json``.
    retry : int
        Current outer retry number (0 = first attempt).
    workflow_id : str
        ``"original"`` (full pipeline) or ``"aider"`` (modify-existing).
    current_stl_path : str | None
        Path to the current STL file for visual rendering. None or missing
        file → text-only path (no multimodal input).

    Returns
    -------
    JudgeDecision | None
        None on any failure (safe default → caller falls through to Aider,
        never blocks the pipeline). Otherwise a parsed JudgeDecision.

        The caller (``node_autonomous_skill_loop``) applies confidence +
        evidence gates to decide whether to actually honor ``accept`` /
        ``halt`` — empty evidence or low confidence on FATAL/TOPOLOGY is
        downgraded to REPAIR.

    Notes
    -----
    - Function is prefixed ``node_`` so ``token_tracker.py`` auto-wraps it
      via ``_wrap_node`` (token_tracker.py:261-269) — per-module token
      attribution appears as a separate ``node_judge_qa`` row in the
      final token summary.
    - Returns None when ``JUDGE_ENABLED=False`` or ``retry < JUDGE_MIN_RETRY``
      (configurable in config.py). First retry always lets Aider genuinely
      try — Judge only kicks in from ``JUDGE_MIN_RETRY`` (default 1).
    - On LLM call failure or JSON parse failure, returns None (safe default
      → continue to Aider). The ``_call_llm_json_with_retry`` helper already
      retries 3× with self-correction on parse errors.
    """
    # -- Config gates -------------------------------------------------------
    if not _CFG_JUDGE_ENABLED:
        return None
    if retry < _CFG_JUDGE_MIN_RETRY and not semantic_check:
        return None

    # -- Build user prompt -------------------------------------------------
    error_details = list(qa_report.error_details or [])
    error_type = qa_report.error_type
    error_type_str = error_type.value if hasattr(error_type, "value") else str(error_type)

    # Errors section
    if error_details:
        errors_lines = [f"  [{i}] {e}" for i, e in enumerate(error_details)]
        errors_section = "\n".join(errors_lines)
    else:
        errors_section = "(no error_details — QA report has no failure items)"

    # Special features section (same format as _build_autonomous_repair_prompt)
    if special_features:
        features_lines = [f"  [{i}] {feat}" for i, feat in enumerate(special_features)]
        special_features_section = (
            "## 🔍 Special Features (non-trivial geometric constraints)\n\n"
            "These constraints were extracted from the user request by the Spec Planner.\n"
            "If a special_feature explicitly documents the design intent that the QA report\n"
            "flags as a failure (e.g. 'MUST be multi-body assembly' vs connectivity FATAL),\n"
            "cite it in your evidence.\n\n"
            + "\n".join(features_lines)
        )
    else:
        special_features_section = (
            "## 🔍 Special Features\n\n"
            "(empty — no non-trivial geometric constraints extracted by Spec Planner)"
        )

    # Feature measurements section
    if feature_measurements:
        measurements_section = (
            "## 📏 Feature Measurements (white-box instrumentation, pre-boolean-merge)\n\n"
            "Each entry is a per-feature bounding box measured BEFORE boolean merge.\n"
            "Use `size_x`/`size_y`/`size_z` to verify dimensions against the user request.\n"
            "Use `min_x`/`min_y`/`min_z`/`max_x`/`max_y`/`max_z` to verify positions.\n\n"
            "```json\n"
            + json.dumps(feature_measurements, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
    else:
        measurements_section = (
            "## 📏 Feature Measurements\n\n"
            "(not available — no temp_measurements_{iter}.json found on disk)"
        )

    task_text = (
        "The deterministic QA checks passed. Use the rendered current-model "
        "views, if present, to verify visible agreement with the original "
        "natural-language request. Check overall shape, orientation, "
        "handedness/symmetry, interface and hinge placement, feature shape, "
        "and missing or floating geometry. If a visible mismatch exists, "
        "choose repair and provide concrete modification_suggestions. If "
        "rendered views are unavailable, semantic_verification must be "
        "unverified and lack of vision must not trigger repair or halt."
        if semantic_check else
        "Evaluate whether the QA report's failures warrant code repair, or "
        "whether the current model should be accepted as-is (false positive, "
        "design intent satisfied, persistent kernel limitation) or the "
        "request declared unimplementable (halt)."
    )

    user_prompt = textwrap.dedent(f"""\
    ## Original User Request (ground truth — never deviate from this)

    {user_request}

    {special_features_section}

    {measurements_section}

    ## QA Error Report

    Error type: {error_type_str}
    Iteration: retry {retry} (workflow_id={workflow_id})

    {errors_section}

    ## Task

    {task_text}

    Follow the Anti-Hallucination Iron Rule from the system prompt: every
    `accept` or `halt` decision MUST cite concrete data points in the
    `evidence` field. If you cannot find at least one hard data point,
    output `repair` instead.

    Return ONLY the ```json fenced block — no other text.
    """)

    # -- Render multi-angle views (when multimodal enabled) ----------------
    view_pngs: list[bytes] = []
    multimodal_mode = _CFG_JUDGE_MULTIMODAL  # "auto" | "always" | "never"

    if multimodal_mode != "never" and current_stl_path and Path(current_stl_path).is_file():
        try:
            from multi_agent_cad.render_views import (
                _render_isometric_views,
                _save_views_to_disk,
                _encode_png_data_url,
            )
            view_pngs = _render_isometric_views(
                Path(current_stl_path),
                n_views=_CFG_JUDGE_VIEWS_COUNT,
                size=_CFG_JUDGE_VIEW_SIZE,
            )
            if view_pngs and _CFG_JUDGE_SAVE_VIEWS:
                cwd = Path.cwd()
                views_dir = cwd / f"temp_judge_views_{retry}"
                _save_views_to_disk(view_pngs, views_dir, prefix="view")
                print(f"[JUDGE] Rendered {len(view_pngs)} views to {views_dir.name}")
            elif view_pngs:
                print(f"[JUDGE] Rendered {len(view_pngs)} views (disk save disabled)")
        except Exception as exc:
            print(f"[JUDGE] view rendering failed: {exc}")
            if multimodal_mode == "always":
                print(f"[JUDGE] JUDGE_MULTIMODAL=always but rendering failed — returning None (safe default REPAIR)")
                return None
            # auto mode → continue with text-only
            view_pngs = []
        # F3 fix: _render_isometric_views returns [] (not exception) on failure
        # like degenerate mesh or empty STL. The except block above doesn't catch it.
        # Explicit check: if always mode and no views rendered, return None.
        if multimodal_mode == "always" and not view_pngs:
            print(f"[JUDGE] JUDGE_MULTIMODAL=always but rendering produced no views — returning None (safe default REPAIR)")
            return None
    elif multimodal_mode == "always" and (not current_stl_path or not Path(current_stl_path).is_file()):
        print(f"[JUDGE] JUDGE_MULTIMODAL=always but STL not available — returning None (safe default REPAIR)")
        return None

    # A semantic-only check needs rendered views of the generated model.
    # With text-only providers (or a rendering failure in auto/never mode),
    # skip it rather than inventing visual facts or blocking a valid result.
    if semantic_check and not view_pngs:
        print("[JUDGE] semantic visual check skipped: no rendered views")
        return None

    # -- Load user-provided reference images (independent scan) ------------
    # User dropped images into user_input_images/ to specify the design
    # visually. Loaded here in addition to the rendered view[N] blocks —
    # lets Judge compare user_image[N] (intended) vs view[N] (current).
    user_images: list[bytes] = []
    if multimodal_mode != "never":
        try:
            from multi_agent_cad.image_preprocess import (
                _load_user_images, _encode_jpeg_data_url,
            )
            user_images_dir = Path.cwd() / _CFG_USER_IMAGES_DIR
            user_images = _load_user_images(
                user_images_dir,
                max_size=_CFG_USER_IMAGE_MAX_SIZE,
                jpeg_quality=_CFG_USER_IMAGE_JPEG_QUALITY,
            )
            if user_images:
                print(f"[JUDGE] Loaded {len(user_images)} user reference images from {user_images_dir.name}")
        except Exception as exc:
            print(f"[JUDGE] user image load failed: {exc}")
            # auto mode → continue; always mode → already handled by rendering path
            # (no STL → already returned None above)

    # -- Build messages: text + user_image blocks + view blocks ------------
    # OpenAI-compatible multimodal format: user content can be a list of
    # {type: "text", text: ...} and {type: "image_url", image_url: {url: ...}}
    # blocks. The SDK accepts both str and list[dict] for the content field.
    # Order: text first (let LLM read context), then user_image[N] (intended
    # design), then view[N] (current model) — natural reading flow.
    text_first_block = {"type": "text", "text": user_prompt}
    content_list: list[dict] = [text_first_block]
    for img in user_images:
        data_url = _encode_jpeg_data_url(img)
        content_list.append({
            "type": "image_url",
            "image_url": {"url": data_url},
        })
    for png in view_pngs:
        data_url = _encode_png_data_url(png)
        content_list.append({
            "type": "image_url",
            "image_url": {"url": data_url},
        })
    if len(content_list) > 1:  # has any image blocks (user_image or view)
        user_content: list[dict] | str = content_list
        n_user = len(user_images)
        n_view = len(view_pngs)
        print(f"[JUDGE] Sending multimodal message: 1 text + {n_user} user images + {n_view} rendered views")
    else:
        user_content = user_prompt  # text-only path (no user images AND no rendered views)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT_QA_JUDGE},
        {"role": "user", "content": user_content},
    ]

    # -- Call LLM (with auto-fallback on multimodal API errors) ------------
    # Wrapped tightly so a missing API key (RuntimeError from _llm_client)
    # also falls into the safe-default path — caller proceeds to Aider.
    # Catches generic Exception (not just ValueError/RuntimeError) because the
    # OpenAI SDK raises openai.BadRequestError for "model doesn't support image"
    # — a subclass of openai.APIStatusError, NOT ValueError/RuntimeError. Without
    # the broad catch, the auto-fallback would never fire on real multimodal-
    # unsupported errors.
    try:
        client = _llm_client()
        json_str, _raw_response = _call_llm_json_with_retry(
            client, messages,
            model=_JUDGE_MODEL,
            max_tokens=_JUDGE_MAX_TOKENS,
            temperature=_JUDGE_TEMP,
            extra_kwargs=_JUDGE_KWARGS,
            max_retries=3,
        )
    except Exception as exc:
        err_str = str(exc).lower()
        # Detect multimodal-unsupported errors (varies by provider).
        # Common patterns:
        #   OpenAI: "model 'X' does not support image input"
        #   Qwen/DashScope: "param image_content is not supported by the model"
        #   Anthropic: "image input not supported"
        #   DeepSeek: "unsupported content type"
        # F5 fix: two-part check via _is_multimodal_unsupported_error() —
        # eliminates false positives on non-multimodal errors like
        # "rate limit exceeded for image-generation model" or
        # "model does not support streaming".
        multimodal_unsupported = _is_multimodal_unsupported_error(err_str)
        has_images = bool(view_pngs) or bool(user_images)
        if (
            semantic_check and has_images and multimodal_mode == "auto"
            and multimodal_unsupported
        ):
            print(
                f"[JUDGE] semantic visual check skipped: model does not "
                f"support images ({exc})"
            )
            return None
        if has_images and multimodal_mode == "auto" and multimodal_unsupported:
            # Fallback: retry without images (drop both user_image and view blocks)
            print(f"[JUDGE] multimodal unsupported ({exc}) — retrying text-only")
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT_QA_JUDGE},
                {"role": "user", "content": user_prompt},
            ]
            try:
                client = _llm_client()
                json_str, _raw_response = _call_llm_json_with_retry(
                    client, messages,
                    model=_JUDGE_MODEL,
                    max_tokens=_JUDGE_MAX_TOKENS,
                    temperature=_JUDGE_TEMP,
                    extra_kwargs=_JUDGE_KWARGS,
                    max_retries=3,
                )
            except Exception as exc2:
                print(f"[JUDGE] text-only fallback also failed: {exc2} — proceeding to repair (safe default)")
                return None
        else:
            print(f"[JUDGE] LLM call failed: {exc} — proceeding to repair (safe default)")
            return None

    # -- Parse and validate ------------------------------------------------
    try:
        parsed_dict = json.loads(json_str)
        decision = JudgeDecision.model_validate(parsed_dict)
    except json.JSONDecodeError as exc:
        print(f"[JUDGE] JSON decode failed: {exc} — proceeding to repair")
        return None
    except ValidationError as exc:
        print(f"[JUDGE] schema validation failed: {exc} — proceeding to repair")
        return None
    except Exception as exc:
        print(f"[JUDGE] parse failed: {exc} — proceeding to repair")
        return None

    if semantic_check:
        has_view_evidence = any(
            "view[" in item.lower() for item in decision.evidence
        )
        if decision.semantic_verification == "failed" and has_view_evidence:
            suggestions = list(decision.modification_suggestions)
            if not suggestions and decision.reason.strip():
                suggestions = [decision.reason.strip()]
            decision = decision.model_copy(update={
                "action": JudgeAction.REPAIR,
                "modification_suggestions": suggestions,
            })
        else:
            semantic = decision.semantic_verification
            if semantic in ("verified", "failed") and not has_view_evidence:
                semantic = "unverified"
            # A clean deterministic report must not be repaired on
            # unsupported visual claims. A repair requires an explicit
            # failed assessment grounded in a rendered current-model view.
            decision = decision.model_copy(update={
                "action": JudgeAction.ACCEPT,
                "semantic_verification": semantic,
                "modification_suggestions": [],
            })

    return decision


# ---------------------------------------------------------------------------
# System prompt for the direct repair LLM call (replaces Aider)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_REPAIR = _load_prompt("repair")


# ============================================================================
# Node: Autonomous Skill Loop (replaces old QA + Repair loop)
# ============================================================================


# ── Cross-platform stdin reader with timeout ─────────────────────────────
# Single persistent daemon thread reads stdin lines into a queue. The main
# thread pulls from the queue with a timeout. No termios/select/msvcrt
# dependency — works on Windows, macOS, Linux identically.
_INPUT_QUEUE: "queue.Queue[str]" = queue.Queue()
_READER_STARTED = False
_READER_LOCK = threading.Lock()


def _ensure_stdin_reader() -> None:
    """Start the singleton stdin reader thread on first call."""
    global _READER_STARTED
    with _READER_LOCK:
        if _READER_STARTED:
            return
        _READER_STARTED = True
        threading.Thread(target=_stdin_reader_loop, daemon=True).start()


def _stdin_reader_loop() -> None:
    """Background loop: read lines from stdin and enqueue them.
    Breaks on EOF / KeyboardInterrupt / OSError (process exit will kill the daemon)."""
    while True:
        try:
            line = sys.stdin.readline()
            if not line:  # EOF
                break
            _INPUT_QUEUE.put(line)
        except (KeyboardInterrupt, EOFError, OSError):
            break


def _readline_with_timeout(timeout: float) -> str | None:
    """Return next stdin line (stripped), or None on timeout.
    Non-TTY stdin → None immediately (skips prompt). Drains stale queued
    input from previous timed-out prompts before waiting."""
    if not sys.stdin.isatty():
        return None
    _ensure_stdin_reader()
    # Drain stale input left over from a previous timed-out prompt
    while not _INPUT_QUEUE.empty():
        try:
            _INPUT_QUEUE.get_nowait()
        except queue.Empty:
            break
    try:
        return _INPUT_QUEUE.get(timeout=timeout).strip()
    except queue.Empty:
        return None


def _prompt_iteration_choice(
    *, step_path: str, stl_path: str, qa_report, retry: int,
    max_retries: int, timeout: int = _CFG_CHECKPOINT_INPUT_TIMEOUT,
) -> int:
    """Display iteration checkpoint. Returns 1 (auto-iterate),
    2 (intervention), or 3 (stop). Auto-selects 1 after ``timeout``
    seconds with no stdin input."""
    print("\n" + "=" * 70)
    print(f"  ITERATION CHECKPOINT  (retry {retry + 1}/{max_retries})")
    print("=" * 70)
    print(f"  STEP model saved at: {step_path}")
    print(f"  STL  model saved at: {stl_path}")
    if qa_report is not None:
        passed = bool(qa_report.all_passed) and (
            getattr(qa_report.error_type, "value", "") == "none"
        )
        status = "PASS ✓" if passed else (
            f"FAIL ✗ ({getattr(qa_report, 'failed_count', 0)} issue(s))"
        )
        print(f"  QA report status  : {status}")
        details = getattr(qa_report, "error_details", None) or []
        if details:
            print(f"  QA details        :")
            for d in details[:5]:
                print(f"    - {str(d)[:140]}")
    else:
        print(f"  QA report status  : (not yet run)")
    print("=" * 70)
    print("  Choose an option:")
    print("    [1] Auto-iterate   (continue with the current flow)")
    print("    [2] User intervention (provide additional change requirements)")
    print("    [3] Stop           (halt and keep current artifacts)")
    print(f"  Auto-selects [1] in {timeout} seconds if no input.")
    print("=" * 70)
    print("  Enter choice [1/2/3]: ", end="", flush=True)

    line = _readline_with_timeout(timeout)
    if line is None:
        print(f"(timeout) → [1] Auto-iterate")
        return 1
    if line == "2":
        print(" → [2] User intervention")
        return 2
    if line == "3":
        print(" → [3] Stop")
        return 3
    print(" → [1] Auto-iterate")
    return 1


def _prompt_user_intervention() -> str:
    """Prompt user for additional change requirements. Returns the input
    string (may be empty). Blocks up to 1 hour (effectively no timeout)
    so the user has time to think."""
    print("\n" + "=" * 70)
    print("  USER INTERVENTION")
    print("=" * 70)
    print("  Enter your additional change requirements below (one line, then Enter).")
    print("  Note: avoid contradicting the original requirements.")
    print("=" * 70)
    print("  > ", end="", flush=True)
    line = _readline_with_timeout(timeout=_CFG_INTERVENTION_INPUT_TIMEOUT)
    return line if line is not None else ""


def node_autonomous_skill_loop(state: GraphState) -> dict:
    """Autonomous super-node: internal Aider + Dual-QA closed loop.

    This node **replaces** the old LangGraph ``qa → repair → qa`` cycle.
    All bug-fixing happens **inside** this node's internal ``while`` loop.
    LangGraph sees a single atomic node that eventually returns PASS or FATAL.

    Architecture::

        ┌──────────────────────────────────────────────────┐
        │  node_autonomous_skill_loop                      │
        │                                                  │
        │  ┌──────────┐    ┌──────────┐    ┌──────────┐   │
        │  │ Engine A │    │ Engine B │    │  Aider   │   │
        │  │ (cadpy)  │    │(check_   │    │ (code    │   │
        │  │          │    │ mesh.py) │    │  fix)    │   │
        │  └────┬─────┘    └────┬─────┘    └────┬─────┘   │
        │       │               │               │          │
        │       └───────┬───────┘               │          │
        │               │                       │          │
        │          ┌────▼────┐                   │          │
        │          │  Merge  │──FAIL─────────────►          │
        │          │  QA     │                              │
        │          └────┬────┘                              │
        │               │                                   │
        │             PASS                                  │
        │               │                                   │
        │          ┌────▼────┐                              │
        │          │  RETURN │                              │
        │          │  SUCCESS│                              │
        │          └─────────┘                              │
        └──────────────────────────────────────────────────┘

    Parameters
    ----------
    state : GraphState
        Must contain ``current_step_path``, ``current_stl_path``,
        ``current_python_code``, ``user_request``, and ``cad_brief``.

    Returns
    -------
    dict
        Partial state update with ``qa_report``, ``error_type``, artifact
        paths, and log lines.  ``error_type`` is ``NONE`` on success or
        ``FATAL`` when the retry budget is exhausted.
    """
    MAX_RETRIES = _CFG_MAX_RETRIES
    iteration: int = state.get("iteration_count", 0)
    user_request: str = state.get("user_request", "")
    cad_brief = state.get("cad_brief")
    step_path_str: str | None = state.get("current_step_path")
    stl_path_str: str | None = state.get("current_stl_path")
    python_code: str = state.get("current_python_code", "") or ""
    workflow_id: str = state.get("workflow_id", "original")  # "original" or "aider"
    script_path_str: str | None = state.get("current_python_code_path")  # Explicit script path from state

    # ------------------------------------------------------------------
    # 0. Guard — coder must have produced artifacts (unless Aider-First workflow)
    # ------------------------------------------------------------------
    _skip_initial_qa = False
    if not step_path_str or not stl_path_str:
        if workflow_id in ("aider", "resume"):
            # Aider-First workflow: initial generation failed, skip QA and go straight to repair
            _skip_initial_qa = True
            print("[AUTONOMOUS LOOP] Aider-First: No initial STEP/STL, skipping QA, entering repair loop")
        else:
            return _autonomous_fatal_state(
                state, iteration,
                "No STEP/STL artifacts from Python Coder. Cannot run QA.",
            )

    cwd = Path.cwd()
    current_step = Path(step_path_str) if step_path_str else None
    current_stl = Path(stl_path_str) if stl_path_str else None

    # Ensure the source script exists on disk (Aider needs a file to edit)
    # Use explicit path from state if provided, otherwise infer from workflow_id
    if script_path_str:
        script_path = Path(script_path_str)
    elif workflow_id in ("aider", "resume"):
        script_path = cwd / f"temp_design_aider_{iteration}.py"
    else:
        script_path = cwd / f"temp_design_{iteration}.py"

    if not script_path.is_file():
        if python_code:
            try:
                script_path.write_text(python_code, encoding="utf-8")
            except OSError as exc:
                return _autonomous_fatal_state(
                    state, iteration, f"Cannot write script to {script_path}: {exc}",
                )
        else:
            return _autonomous_fatal_state(
                state, iteration,
                f"Source script {script_path} not found and current_python_code is empty.",
            )

    # Track the last QA report for the exhausted-retries case
    last_qa_report = None
    all_log_lines: list[str] = []
    # Track execution feedback for the next repair prompt (Phase 3).
    # Two separate channels: actual crashes vs post-execution diagnostics.
    last_exec_error: str = ""       # script crash traceback
    last_missed_cuts: str = ""      # missed cut detection (script ran OK)

    # Load selector map from Architect's plan (for precise topology measurement)
    architect_plan = state.get("architect_plan")
    selector_map: dict[str, str] = {}
    if architect_plan:
        selector_map = _attr(architect_plan, "selector_map", {}) or {}
    if selector_map:
        print(f"[AUTONOMOUS LOOP] Loaded {len(selector_map)} selector expressions from ArchitectPlan")

    # ==================================================================
    # Autonomous retry loop
    # ==================================================================
    _skip_qa = False  # True when previous execution failed — model unchanged
    # F13: initialize judge_decision — included in ALL return paths so audit
    # trail is complete (can distinguish "Judge didn't run" (None) from "Judge
    # ran and chose REPAIR" (JudgeDecision with action=REPAIR)).
    judge_decision: JudgeDecision | None = None
    # Snapshot the original user_request so per-iteration interventions
    # (choice 2) only affect the current round. Next iteration starts
    # fresh from the original, even if the user picked 2 last round.
    original_user_request = user_request
    # The geometry currently held in ``current_step`` was measured when its
    # script last executed.  Repairing at retry N writes measurements_N, but
    # the next outer loop is N+1; using retry_iter there can silently load an
    # unrelated file left by an older run. Track the artifact, not the loop.
    current_measurement_iteration = iteration
    # MAX_RETRIES counts repairs; the extra round verifies the last repair.
    for retry in range(MAX_RETRIES + 1):
        retry_iter = iteration + retry
        judge_decision = None  # decisions belong to this geometry/QA round
        # Reset to original at the start of each iteration so interventions
        # from previous rounds do not leak into subsequent Aider calls.
        user_request = original_user_request
        print(f"\n{'='*60}")
        print(f"  AUTONOMOUS LOOP — QA round {retry + 1}/{MAX_RETRIES + 1}")
        print(f"{'='*60}")

        # Per-iteration token snapshot (for benchmark analysis)
        try:
            from multi_agent_cad.token_tracker import tracker as _iter_tracker
            _iter_snap = _iter_tracker.summary()
            print(f"[TOKEN SNAPSHOT] retry={retry} start "
                  f"total_tokens={_iter_snap['total_tokens']} "
                  f"api_calls={_iter_snap['n_calls']} "
                  f"input={_iter_snap['total_input']} "
                  f"output={_iter_snap['total_output']} "
                  f"cache_read={_iter_snap['total_cache_read']}", flush=True)
        except Exception:
            pass

        # --------------------------------------------------------------
        # Phase 1: Dual-Engine QA
        # --------------------------------------------------------------
        # Skip QA when:
        # 1. The model hasn't changed since the last QA round (previous Aider repair failed)
        # 2. This is the first iteration of Aider-First workflow and initial generation failed
        # Re-running QA on the same file would produce identical results and
        # waste an entire retry budget.  Instead, reuse the last QA report
        # and go straight to Aider with the accumulated error context.
        if (_skip_qa and last_qa_report is not None) or (_skip_initial_qa and retry == 0):
            if _skip_initial_qa and retry == 0:
                print(f"\n[AUTONOMOUS QA] ⏭️  SKIPPED — no initial STEP/STL from Aider-First")
                print(f"[AUTONOMOUS QA] Entering repair loop without QA")
                # Create a mock QA report to trigger repair
                last_qa_report = QAReport(
                    cad_brief_id="(aider-first-init)",
                    engine_a=EngineReport(engine_name="cadpy_analysis"),
                    engine_b=EngineReport(
                        engine_name="check_mesh",
                        errors=["Initial Aider generation failed — no STEP/STL produced"],
                    ),
                    all_passed=False,
                    error_type=ErrorType.DIMENSION,
                    error_details=[
                        "INITIAL FAILURE: Aider-First workflow produced no STEP/STL. "
                        "Generate a complete build123d implementation of the user request."
                    ],
                    failed_count=1,
                    passed_count=0,
                )
                all_log_lines.append(
                    f"node_autonomous_skill_loop [retry {retry}]: "
                    f"QA SKIPPED (no initial artifacts from Aider-First)"
                )
            else:
                print(f"\n[AUTONOMOUS QA] ⏭️  SKIPPED — model unchanged since last execution failure")
                print(f"[AUTONOMOUS QA] Reusing previous QA report ({last_qa_report.failed_count} failures)")
                all_log_lines.append(
                    f"node_autonomous_skill_loop [retry {retry}]: "
                    f"QA SKIPPED (model unchanged after execution failure)"
                )
        else:
            print(f"\n[AUTONOMOUS QA] Running Engine A (cadpy.analysis) ...")
            engine_a = _run_engine_a_cadpy(
                step_path=str(current_step),
                cad_brief=cad_brief,
                engine_b_mesh_resolution={},
                iteration=current_measurement_iteration,
                selector_map=selector_map,
            )

            print(f"[AUTONOMOUS QA] Running Engine B (check_mesh.py) ...")
            engine_b = _run_engine_b_check_mesh(
                stl_path=str(current_stl),
                cad_brief=cad_brief,
                iteration=current_measurement_iteration,
            )

            # Merge reports
            last_qa_report = _merge_engine_reports(
                cad_brief=cad_brief,
                engine_a=engine_a,
                engine_b=engine_b,
                iteration=retry_iter,
            )

        # --------------------------------------------------------------
        # Phase 1.5: Geometry validation gate (Aider-first workflow only)
        # --------------------------------------------------------------
        # Only run geometry validation for Aider-first workflow to catch
        # placeholder/incomplete models. Original workflow has deterministic
        # coder that generates proper geometry from ArchitectPlan.
        # Skipped when QA was skipped (model unchanged) or when no initial
        # STEP/STL exists (Aider-First first iteration).
        if not _skip_qa and not (_skip_initial_qa and retry == 0) and workflow_id in ("aider", "resume"):
            geometry_errors = _validate_geometry_against_request(
                step_path=str(current_step),
                user_request=user_request,
                engine_a=engine_a,
                measurement_iteration=current_measurement_iteration,
            )
            if geometry_errors:
                # Inject geometry errors into the QA report
                last_qa_report.error_type = ErrorType.DIMENSION
                last_qa_report.all_passed = False
                last_qa_report.error_details = geometry_errors + (last_qa_report.error_details or [])
                print(f"\n[GEOMETRY GATE] ❌ {len(geometry_errors)} geometry validation error(s):")
                for err in geometry_errors[:3]:
                    print(f"  • {err[:120]}")

        # --------------------------------------------------------------
        # Phase 1.8: Check runtime diagnostics (missed cuts / chamfer / fillet failures)
        # --------------------------------------------------------------
        # temp_missed_{iter}.json may have been written by the Python Coder
        # (initial run) or by a previous Aider re-execution.  If it exists and
        # has entries, the model has runtime issues that QA's 3 target types
        # (overall_dimension, single_body, water_tightness) cannot detect.
        # Without this check, a silently-failed chamfer/fillet would be missed
        # and the loop would report SUCCESS prematurely.
        if not _skip_qa or retry > 0:
            missed_path = _runtime_diagnostics_path(cwd, current_measurement_iteration)
            if missed_path is not None:
                try:
                    missed = json.loads(missed_path.read_text(encoding="utf-8"))
                    if missed:
                        cats = _parse_missed_cuts(missed)
                        runtime_errors, _label = _format_missed_cuts_errors(cats)
                        if runtime_errors:
                            print(f"[AUTONOMOUS LOOP] ⚠️ {len(missed)} runtime issue(s) in {missed_path.name}")
                            for m in missed[:3]:
                                print(f"  → {m[:120]}")
                            # Inject into QA report — prevent premature PASS
                            last_qa_report.all_passed = False
                            last_qa_report.error_type = ErrorType.DIMENSION
                            last_qa_report.error_details = runtime_errors + (last_qa_report.error_details or [])
                except Exception:
                    pass

        # --------------------------------------------------------------
        # Phase 1.9: User iteration checkpoint (10s timeout, auto-iterate)
        # --------------------------------------------------------------
        # Skip on the first iteration (retry == 0): the user_request is already
        # the modification requirement for the existing file. Let Aider apply
        # it directly first; only ask for user refinement from retry >= 1.
        if retry == 0 or retry == MAX_RETRIES or current_step is None or current_stl is None:
            choice = 1  # auto-iterate — apply user_request directly
            all_log_lines.append(
                f"node_autonomous_skill_loop [retry {retry}]: "
                f"CHECKPOINT skipped on first iteration (choice=1, apply user_request directly)"
            )
        else:
            choice = _prompt_iteration_choice(
                step_path=str(current_step.resolve()) if current_step else None,
                stl_path=str(current_stl.resolve()) if current_stl else None,
                qa_report=last_qa_report,
                retry=retry,
                max_retries=MAX_RETRIES,
            )
            all_log_lines.append(
                f"node_autonomous_skill_loop [retry {retry}]: CHECKPOINT choice={choice}"
            )

        if choice == 3:
            # Stop — return current artifacts as accepted (ErrorType.NONE)
            rotated_stl, orientation_info = _optimize_print_orientation(current_stl)
            all_log_lines.append(
                f"  USER HALTED — accepted current artifacts "
                f"(QA pass={last_qa_report.all_passed})"
            )
            return {
                "qa_report": last_qa_report,
                "error_type": ErrorType.NONE,
                "judge_decision": judge_decision,  # F13: might be None if Judge hasn't run
                "current_step_path": str(current_step.resolve()) if current_step else None,
                "current_stl_path": str(current_stl.resolve()) if current_stl else None,
                "current_print_stl_path": str(rotated_stl.resolve()) if rotated_stl else None,
                "current_python_code": python_code,
                "current_python_code_path": str(script_path.resolve()),
                "measurement_iteration": current_measurement_iteration,
                "iteration_count": retry_iter + 1,
                "execution_log": all_log_lines,
                "node_history": list(state.get("node_history", [])) + ["autonomous_skill_loop"],
            }

        if choice == 2:
            user_change = _prompt_user_intervention()
            if user_change:
                user_request = (
                    f"ADDITIONAL REQUIREMENTS (the user wants to make the following changes to the current model):\n"
                    f"{user_change}\n\n"
                    f"ORIGINAL REQUEST:\n{user_request}"
                )
                all_log_lines.append(
                    f"  USER INTERVENTION — prepended change requirements to user_request"
                )
                # Force Phase 2 to fall through to Aider even if QA passed
                if last_qa_report.all_passed:
                    last_qa_report.all_passed = False
                    last_qa_report.error_type = ErrorType.DIMENSION
                    last_qa_report.error_details = (
                        [
                            "USER REQUESTED CHANGES — apply the additional "
                            "requirements above and re-run."
                        ]
                        + (last_qa_report.error_details or [])
                    )
                    last_qa_report.failed_count = max(
                        1, last_qa_report.failed_count
                    )
            # Empty input → fall through to Phase 2 like auto-iterate
        # choice == 1: fall through to Phase 2

        # --------------------------------------------------------------
        # Phase 2: Check for PASS
        # --------------------------------------------------------------
        # On retry=0 of the modify-existing workflow (workflow_id == "aider"),
        # skip PASS return: the baseline QA passing only means the existing
        # model is geometrically valid, NOT that the user's modification
        # requirements have been applied. Let Aider run first.
        clean_geometric_pass = (
            (retry > 0 or workflow_id != "aider")
            and not _skip_qa and current_step is not None and current_stl is not None
            and last_qa_report.all_passed
            and last_qa_report.error_type == ErrorType.NONE
        )
        if clean_geometric_pass:
            # Deterministic QA cannot establish visible semantic agreement
            # (orientation, handedness, feature placement). Run one optional
            # visual Judge pass. Text-only providers/render failures return
            # None and leave the artifact deliverable as semantic-unverified.
            semantic_measurements = None
            semantic_measurements_file = (
                cwd / f"temp_measurements_{current_measurement_iteration}.json"
            )
            if semantic_measurements_file.is_file():
                try:
                    with open(semantic_measurements_file, "r", encoding="utf-8") as f:
                        semantic_measurements = json.load(f)
                except Exception as exc:  # noqa: BLE001
                    all_log_lines.append(
                        f"semantic measurements unavailable: {exc}"
                    )

            semantic_decision = node_judge_qa(
                user_request=user_request,
                qa_report=last_qa_report,
                special_features=_attr(cad_brief, "special_features", []) or [],
                feature_measurements=semantic_measurements,
                retry=retry,
                workflow_id=workflow_id,
                current_stl_path=str(current_stl) if current_stl else None,
                semantic_check=True,
            )
            judge_decision = semantic_decision
            if semantic_decision is not None:
                last_qa_report.semantic_verification = (
                    semantic_decision.semantic_verification
                )
                if semantic_decision.semantic_verification == "failed":
                    suggestions = list(
                        semantic_decision.modification_suggestions
                    ) or [semantic_decision.reason]
                    last_qa_report.semantic_issues = [semantic_decision.reason]
                    last_qa_report.semantic_modification_suggestions = suggestions
                    last_qa_report.all_passed = False
                    last_qa_report.error_type = ErrorType.TOPOLOGY
                    last_qa_report.failed_count = max(
                        1, last_qa_report.failed_count
                    )
                    last_qa_report.error_details = [
                        "VISUAL_SEMANTIC_MISMATCH: " + semantic_decision.reason,
                        *(f"SUGGESTED_FIX: {item}" for item in suggestions),
                    ]
                    all_log_lines.append(
                        "semantic Judge found a visible mismatch; continuing "
                        "to the existing repair loop"
                    )

        if clean_geometric_pass and last_qa_report.all_passed:
            all_log_lines.append(
                f"node_autonomous_skill_loop [retry {retry}]: ✅ ALL CHECKS PASSED"
            )
            all_log_lines.append(
                "  Semantic verification: "
                f"{last_qa_report.semantic_verification}"
            )
            all_log_lines.append(f"  Engine A: {engine_a.summary}")
            all_log_lines.append(f"  Engine B: {engine_b.summary}")
            all_log_lines.append(f"  Total retries: {retry + 1}")

            print(f"\n{'='*60}")
            print(f"  ✅ AUTONOMOUS LOOP — ALL CHECKS PASSED (retry {retry + 1}/{MAX_RETRIES})")
            print(f"{'='*60}\n")

            # Automatically optimize print orientation
            rotated_stl, orientation_info = _optimize_print_orientation(current_stl)
            if orientation_info:
                all_log_lines.append(f"  Print orientation: {orientation_info}")

            return {
                "qa_report": last_qa_report,
                "error_type": ErrorType.NONE,
                "judge_decision": judge_decision,  # F13: might be None if Judge hasn't run
                "current_step_path": str(current_step.resolve()) if current_step else None,
                "current_stl_path": str(current_stl.resolve()) if current_stl else None,
                "current_print_stl_path": str(rotated_stl.resolve()) if rotated_stl else None,
                "current_python_code": python_code,
                "current_python_code_path": str(script_path.resolve()),
                "measurement_iteration": current_measurement_iteration,
                "iteration_count": retry_iter + 1,
                "execution_log": all_log_lines,
                "node_history": list(state.get("node_history", [])) + ["autonomous_skill_loop"],
            }

        # --------------------------------------------------------------
        # Phase 2.5: QA Judge — model can override QA report
        # --------------------------------------------------------------
        # Load feature_measurements ONCE per outer retry and share between
        # Judge (here) and Aider (Phase 4). Avoids double-reading from disk.
        feature_measurements = None
        measurements_file = cwd / f"temp_measurements_{current_measurement_iteration}.json"
        if measurements_file.is_file():
            try:
                with open(measurements_file, 'r', encoding='utf-8') as f:
                    feature_measurements = json.load(f)
                print(f"[AUTONOMOUS JUDGE] Loaded {len(feature_measurements)} feature measurements from {measurements_file.name}")
            except Exception as exc:
                print(f"[AUTONOMOUS JUDGE] WARNING: Failed to load feature measurements: {exc}")

        if (judge_decision is None and current_step is not None and current_stl is not None
                and not _skip_qa and not (retry == 0 and workflow_id == "aider")):
            judge_decision = node_judge_qa(
                user_request=user_request,
                qa_report=last_qa_report,
                special_features=_attr(cad_brief, "special_features", []) or [],
                feature_measurements=feature_measurements,
                retry=retry,
                workflow_id=workflow_id,
                current_stl_path=str(current_stl) if current_stl else None,
            )
        if judge_decision is not None:
            all_log_lines.append(
                f"node_autonomous_skill_loop [retry {retry}]: "
                f"JUDGE → {judge_decision.action.value} "
                f"(confidence={judge_decision.confidence}, "
                f"evidence={len(judge_decision.evidence)} items, "
                f"reason: {judge_decision.reason[:80]})"
            )

            # -- ACCEPT: confidence + evidence gate (anti-hallucination) --
            if judge_decision.action == JudgeAction.ACCEPT:
                err = last_qa_report.error_type
                # F7 fix: filter empty/whitespace-only evidence entries — LLM can
                # output evidence: [""] or ["N/A"] or ["based on general experience"]
                # to bypass the gate. Only count entries with >3 non-whitespace chars.
                meaningful_evidence = [
                    e for e in judge_decision.evidence
                    if e and e.strip() and len(e.strip()) > 3
                ]
                has_evidence = len(meaningful_evidence) > 0
                is_high_conf = judge_decision.confidence == "high"
                # Hard gate: FATAL/TOPOLOGY ACCEPT requires high confidence AND evidence
                # DIMENSION ACCEPT requires evidence (any confidence)
                if err in (ErrorType.FATAL, ErrorType.TOPOLOGY) and not (is_high_conf and has_evidence):
                    all_log_lines.append(
                        f"  Judge ACCEPT on {err.value} requires high confidence AND "
                        f"non-empty evidence — proceeding to repair"
                    )
                elif not has_evidence:
                    all_log_lines.append(
                        f"  Judge ACCEPT has no evidence — proceeding to repair (anti-hallucination)"
                    )
                else:
                    last_qa_report.error_details = [
                        f"JUDGE OVERRIDE (confidence={judge_decision.confidence}): "
                        f"{judge_decision.reason}",
                        f"  Evidence cited: {'; '.join(judge_decision.evidence[:3])}",
                        *(last_qa_report.error_details or []),
                    ]
                    rotated_stl, _orientation_info = _optimize_print_orientation(current_stl)
                    return {
                        "qa_report": last_qa_report,
                        "error_type": ErrorType.NONE,
                        "judge_decision": judge_decision,
                        "current_step_path": str(current_step.resolve()) if current_step else None,
                        "current_stl_path": str(current_stl.resolve()) if current_stl else None,
                        "current_print_stl_path": str(rotated_stl.resolve()) if rotated_stl else None,
                        "current_python_code": python_code,
                        "current_python_code_path": str(script_path.resolve()),
                        "measurement_iteration": current_measurement_iteration,
                        "iteration_count": retry_iter + 1,
                        "execution_log": all_log_lines,
                        "node_history": list(state.get("node_history", [])) + ["autonomous_skill_loop"],
                    }

            # -- HALT: requires high confidence AND non-empty evidence --
            if judge_decision.action == JudgeAction.HALT:
                is_high_conf = judge_decision.confidence == "high"
                # F7 fix: same evidence filtering as ACCEPT gate
                meaningful_evidence = [
                    e for e in judge_decision.evidence
                    if e and e.strip() and len(e.strip()) > 3
                ]
                has_evidence = len(meaningful_evidence) > 0
                if not (is_high_conf and has_evidence):
                    all_log_lines.append(
                        f"  Judge HALT requires high confidence AND non-empty evidence — "
                        f"proceeding to repair (anti-hallucination)"
                    )
                else:
                    all_log_lines.append(
                        f"  Judge HALT — stopping iteration: {judge_decision.reason[:120]}"
                    )
                    return {
                        "qa_report": last_qa_report,
                        "error_type": ErrorType.FATAL,
                        "judge_decision": judge_decision,
                        "current_step_path": str(current_step.resolve()) if current_step else None,
                        "current_stl_path": str(current_stl.resolve()) if current_stl else None,
                        "current_python_code": python_code,
                        "current_python_code_path": str(script_path.resolve()),
                        "measurement_iteration": current_measurement_iteration,
                        "iteration_count": retry_iter + 1,
                        "execution_log": all_log_lines,
                        "node_history": list(state.get("node_history", [])) + ["autonomous_skill_loop"],
                    }

            # -- REPAIR: fall through; inject judge's reason + evidence --
            if judge_decision.reason:
                aider_context = (
                    f"JUDGE context (proceeding to repair): {judge_decision.reason}"
                )
                if judge_decision.evidence:
                    aider_context += (
                        f" | Evidence: {'; '.join(judge_decision.evidence[:2])}"
                    )
                last_qa_report.error_details = [
                    aider_context,
                    *(last_qa_report.error_details or []),
                ]

        # --------------------------------------------------------------
        # Phase 3: Build repair prompt & run Aider or fallback repair
        # --------------------------------------------------------------
        if retry == MAX_RETRIES:
            break  # never perform a repair without budget for its validation
        error_details = list(last_qa_report.error_details or [])
        # Also include improvement suggestions and warnings for richer context
        for sug in (last_qa_report.improvement_suggestions or []):
            if sug not in error_details:
                error_details.append(sug)
        if last_qa_report.connectivity_warning:
            error_details.insert(0, f"CONNECTIVITY: {last_qa_report.connectivity_warning}")

        # On retry=0 of the modify-existing workflow, inject a synthetic
        # "apply user modification" instruction. The user_request is a
        # MODIFICATION REQUIREMENT for the existing file (not a from-scratch
        # spec). Even if baseline QA passes, Aider must still apply these
        # modifications. The user_request itself is already in Aider's
        # "Original Design Requirements" section; this synthetic error
        # gives Aider a clear "what to do" signal alongside the QA report.
        if retry == 0 and workflow_id == "aider":
            error_details.insert(0,
                "USER MODIFICATION REQUEST — Apply the user's modification requirements "
                "(see \"Original Design Requirements\" section above) to evolve the existing "
                "model. The current model passes geometric QA but does NOT yet reflect the "
                "user's requested changes. Treat the QA report as context (what's already "
                "correct), and prioritize applying the user's modifications."
            )

        # Inject execution feedback from the PREVIOUS iteration (if any).
        # Two channels: actual crashes and missed-cut diagnostics.
        if last_exec_error:
            exec_error_msg = (
                f"RUNTIME ERROR — the script CRASHED when executed:\n"
                f"{last_exec_error[:2000]}"
            )
            error_details.insert(0, exec_error_msg)
            last_exec_error = ""  # clear after injecting
        if last_missed_cuts:
            error_details.insert(0, last_missed_cuts)
            last_missed_cuts = ""  # clear after injecting

        print(f"\n[AUTONOMOUS REPAIR] {len(error_details)} error(s) to fix:")
        for e in error_details[:5]:
            print(f"  • {e[:120]}")

        # --------------------------------------------------------------
        # Phase 4+5: Aider repair → Execute → (inner loop on failure)
        # --------------------------------------------------------------
        # When Aider fixes code but re-execution fails (runtime error),
        # we immediately re-invoke Aider with the traceback — within the
        # SAME outer retry, without consuming an iteration count.
        MAX_EXEC_RETRIES = _CFG_MAX_EXEC_RETRIES
        exec_error_for_next_round = ""   # crash traceback → next outer iteration
        missed_cuts_for_next_round = ""  # missed cut diagnostics → next outer iteration

        for exec_attempt in range(MAX_EXEC_RETRIES + 1):
            # -- Phase 4: Aider repair --
            # feature_measurements was hoisted to Phase 2.5 (loaded once per
            # outer retry, shared between Judge and Aider). Re-load only on
            # inner exec-retry > 0 since Aider may have rewritten the script
            # and produced fresh measurements on a successful exec.
            if exec_attempt > 0 or feature_measurements is None:
                measurements_file = cwd / f"temp_measurements_{current_measurement_iteration}.json"
                if measurements_file.is_file():
                    try:
                        with open(measurements_file, 'r', encoding='utf-8') as f:
                            feature_measurements = json.load(f)
                        print(f"[AUTONOMOUS REPAIR] Loaded {len(feature_measurements)} feature measurements from {measurements_file.name}")
                    except Exception as exc:
                        print(f"[AUTONOMOUS REPAIR] WARNING: Failed to load feature measurements: {exc}")

            aider_ok, aider_err = _run_repair_on_script(
                script_path=str(script_path),
                error_details=error_details,
                user_request=user_request,
                feature_measurements=feature_measurements,
                special_features=_attr(cad_brief, "special_features", []) or [],
            )

            # If repair failed (Aider + fallback both failed), there is no
            # point in retrying — every iteration would be an empty loop.
            # Surface the actual cause (timeout, context limit, missing key,
            # etc.) instead of guessing at credentials.
            if not aider_ok:
                _reason = aider_err or "unknown failure"
                all_log_lines.append(
                    f"node_autonomous_skill_loop [retry {retry}]: "
                    f"AIDER FAILED — {_reason}. "
                    f"Halting immediately."
                )
                return {
                    "qa_report": last_qa_report,
                    "error_type": ErrorType.FATAL,
                    "judge_decision": judge_decision,  # F13: audit trail on Aider-failed path
                    "current_step_path": str(current_step.resolve()) if current_step else None,
                    "current_stl_path": str(current_stl.resolve()) if current_stl else None,
                    "current_python_code": python_code,
                    "current_python_code_path": str(script_path.resolve()),
                    "measurement_iteration": current_measurement_iteration,
                    "iteration_count": retry_iter + 1,
                    "execution_log": all_log_lines,
                    "node_history": list(state.get("node_history", [])) + ["autonomous_skill_loop"],
                }

            # Re-read the (possibly) fixed code
            try:
                python_code = script_path.read_text(encoding="utf-8")
            except OSError:
                pass

            # -- Phase 5: Re-execute the fixed code --
            if workflow_id in ("aider", "resume"):
                new_step = cwd / f"temp_output_aider_autonomous_{retry_iter}.step"
                new_stl = cwd / f"temp_output_aider_autonomous_{retry_iter}.stl"
            else:
                new_step = cwd / f"temp_output_autonomous_{retry_iter}.step"
                new_stl = cwd / f"temp_output_autonomous_{retry_iter}.stl"

            exec_ok, exec_error = _execute_cad_script(script_path, new_step, new_stl, iteration=retry_iter)

            if exec_ok:
                current_step = new_step
                current_stl = new_stl
                current_measurement_iteration = retry_iter
                _skip_qa = False  # Model updated — next round needs fresh QA
                all_log_lines.append(
                    f"node_autonomous_skill_loop [retry {retry}]: "
                    f"Aider fixed code → re-executed OK"
                    f"{' (exec-retry ' + str(exec_attempt) + ')' if exec_attempt > 0 else ''}"
                )

                # -- Check for missed cuts / fillet failures (runtime diagnostics) --
                missed_path = _runtime_diagnostics_path(cwd, retry_iter)
                if missed_path is not None:
                    try:
                        missed = json.loads(missed_path.read_text(encoding="utf-8"))
                        if missed:
                            cats = _parse_missed_cuts(missed)
                            error_details_list, label = _format_missed_cuts_errors(cats)
                            total = sum(len(v) for v in cats.values())
                            missed_summary = "\n".join(str(m)[:200] for m in missed[:5])
                            cut_error_msg = (
                                f"{label} — {total} runtime issue(s) detected:\n"
                                f"{missed_summary}\n"
                                + "\n".join(error_details_list)
                            )
                            print(f"[AUTONOMOUS LOOP] ⚠️ {total} issue(s): {label}")
                            for m in missed[:3]:
                                print(f"  → {m[:120]}")
                            # Inject into next iteration's repair prompt
                            missed_cuts_for_next_round = cut_error_msg
                    except Exception:
                        pass

                # Execution succeeded — break out of inner loop
                break

            # -- Execution FAILED — immediate re-repair within same retry --
            if exec_attempt < MAX_EXEC_RETRIES:
                print(f"\n[EXEC-RETRY] Script crashed ({exec_attempt + 1}/{MAX_EXEC_RETRIES}).")
                print(f"[EXEC-RETRY] Feeding traceback back to Aider for immediate fix ...")
                # For the next inner attempt, only send the execution error
                # (QA errors were already addressed — the problem is now
                # a runtime crash, not a geometry issue).
                error_details = [
                    f"RUNTIME ERROR — the script you just modified CRASHED "
                    f"when executed (attempt {exec_attempt + 1}/{MAX_EXEC_RETRIES}). "
                    f"Fix the code so it runs without errors:\n"
                    f"{exec_error[:2000]}"
                ]
                all_log_lines.append(
                    f"node_autonomous_skill_loop [retry {retry}, exec-attempt {exec_attempt}]: "
                    f"re-execution FAILED → immediate Aider re-repair"
                )
            else:
                # Inner retry budget exhausted — pass error to next outer iteration.
                # Set _skip_qa so the next outer round does NOT re-run QA on the
                # unchanged model (which would produce identical results and waste
                # the retry budget).  Instead it will reuse last_qa_report and go
                # straight to Aider with the execution traceback.
                _skip_qa = True
                all_log_lines.append(
                    f"node_autonomous_skill_loop [retry {retry}]: "
                    f"Aider ran but re-execution FAILED after {MAX_EXEC_RETRIES} "
                    f"immediate retries — next QA round will be skipped (model unchanged)"
                )
                exec_error_for_next_round = exec_error
                # Keep current_step / current_stl from previous iteration
                break

        # Carry execution feedback to the next outer iteration (if any)
        last_exec_error = exec_error_for_next_round
        last_missed_cuts = missed_cuts_for_next_round

    # ==================================================================
    # Retry budget exhausted
    # ==================================================================
    all_log_lines.append(
        f"node_autonomous_skill_loop: ❌ MAX RETRIES ({MAX_RETRIES}) EXHAUSTED"
    )
    if last_qa_report is not None:
        all_log_lines.append(
            f"  Final error_type: {last_qa_report.error_type.value}"
        )
        all_log_lines.append(
            f"  Remaining failures: {last_qa_report.failed_count}"
        )

    print(f"\n{'='*60}")
    print(f"  ❌ AUTONOMOUS LOOP — MAX RETRIES ({MAX_RETRIES}) EXHAUSTED")
    print(f"  Final state: {last_qa_report.error_type.value if last_qa_report else 'unknown'}")
    print(f"{'='*60}\n")

    return {
        "qa_report": last_qa_report,
        "error_type": ErrorType.FATAL,
        "judge_decision": judge_decision,  # F13: audit trail on MAX_RETRIES path
        "current_step_path": str(current_step.resolve()) if current_step else None,
        "current_stl_path": str(current_stl.resolve()) if current_stl else None,
        "current_python_code": python_code,
        "current_python_code_path": str(script_path.resolve()),
        "measurement_iteration": current_measurement_iteration,
        "iteration_count": iteration + MAX_RETRIES,
        "execution_log": all_log_lines,
        "node_history": list(state.get("node_history", [])) + ["autonomous_skill_loop"],
    }


def _autonomous_fatal_state(
    state: GraphState, iteration: int, reason: str,
) -> dict:
    """Return a FATAL state update for the autonomous skill loop."""
    return {
        "error_type": ErrorType.FATAL,
        "execution_log": [
            f"node_autonomous_skill_loop [iter {iteration}]: FATAL — {reason}",
        ],
        "node_history": list(state.get("node_history", [])) + ["autonomous_skill_loop"],
    }


# ============================================================================
# System Prompt — Spec Planner
# ============================================================================

SYSTEM_PROMPT_SPEC_PLANNER = _load_prompt("spec_planner")


# ============================================================================
# System Prompt — Geometric Architect
# ============================================================================

SYSTEM_PROMPT_GEOMETRIC_ARCHITECT = _load_prompt("geometric_architect")


# ============================================================================
# Node: Spec Planner
# ============================================================================


def node_spec_planner(state: GraphState) -> dict:
    """Parse the user's natural-language CAD request into a formal CADBrief.

    Calls Qwen 3.8-max (DashScope) with a specialised system prompt that instructs it to
    extract dimensions, infer defaults, standardise units, define an origin,
    and — most importantly — generate concrete VerificationTarget objects
    for the downstream QA engines.

    Parameters
    ----------
    state : GraphState
        Must contain ``user_request``.

    Returns
    -------
    dict
        ``{"cad_brief": CADBrief(...), "execution_log": [...]}`` on success,
        or ``{"error_type": ErrorType.FATAL, ...}`` on failure.
    """
    user_request: str = state.get("user_request", "").strip()
    iteration: int = state.get("iteration_count", 0)
    force_refresh: bool = state.get("force_refresh", False)
    fingerprint = _planning_fingerprint("spec_planner", user_request)

    # -- Cache: skip LLM call only on fresh runs (not retries) --------------
    is_retry = (
        iteration > 0
        or any(n == "planner" for n in state.get("node_history", []))
    )
    if not force_refresh and not is_retry:
        cached = _load_cache(_CACHE_DIR / "cad_brief.json")
        if isinstance(cached, dict) and cached.get("__input_fingerprint") == fingerprint:
            try:
                cad_brief = CADBrief.model_validate(cached)
                print("[CACHE] Loaded cad_brief.json — skipping Spec Planner")
                return {
                    "cad_brief": cad_brief,
                    "execution_log": ["node_spec_planner: loaded from cache"],
                    "node_history": list(state.get("node_history", [])) + ["planner"],
                }
            except Exception:
                pass  # Corrupt cache — regenerate

    if not user_request:
        return _planner_fatal(
            iteration=iteration,
            reason="user_request is empty — nothing to plan.",
            node="node_spec_planner",
        )

    # ------------------------------------------------------------------
    # 1. Build user prompt (with retry context if self-correcting)
    # ------------------------------------------------------------------
    is_retry = (
        state.get("cad_brief") is None
        and len(state.get("node_history", [])) > 0
        and state.get("node_history", [])[-1] == "planner"
    )
    retry_context = ""
    if is_retry:
        prev_errors = state.get("execution_log", [])
        retry_context = (
            "## PREVIOUS ATTEMPT FAILED\n\n"
            "Your last JSON output was REJECTED due to validation errors. "
            "Fix ALL of these issues before regenerating:\n\n"
        )
        for err in prev_errors[-3:]:  # last 3 log entries
            retry_context += f"  - {err}\n"
        retry_context += (
            "\nCommon mistakes: using enum values not in the schema, putting "
            "arrays in key_parameters (use separate keys like HOLE_X and HOLE_Y), "
            "omitting required fields (user_request_raw, spec_version).\n\n"
        )

    user_prompt = textwrap.dedent(f"""\
    ## User Request

    {user_request}

    {retry_context}
    ## Task

    Analyse the request above and produce a complete CADBrief JSON
    specification.  Follow ALL the guidelines in the system prompt:
    extract every dimension, infer sensible defaults, set an appropriate
    origin convention, and generate a comprehensive set of verification
    targets that cover every measurable geometric feature.

    Return ONLY the ```json fenced block — no other text.
    """)

    # ------------------------------------------------------------------
    # 2. Call Qwen (DashScope) with JSON retry
    # ------------------------------------------------------------------
    # -- Load user-provided reference images (when multimodal enabled) -----
    user_images: list[bytes] = []
    sp_multimodal_mode = _CFG_SP_MULTIMODAL  # "auto" | "always" | "never"

    if sp_multimodal_mode != "never":
        try:
            from multi_agent_cad.image_preprocess import (
                _load_user_images, _encode_jpeg_data_url,
            )
            images_dir = Path.cwd() / _CFG_USER_IMAGES_DIR
            user_images = _load_user_images(
                images_dir,
                max_size=_CFG_USER_IMAGE_MAX_SIZE,
                jpeg_quality=_CFG_USER_IMAGE_JPEG_QUALITY,
            )
            if user_images:
                print(f"[SPEC PLANNER] Loaded {len(user_images)} user images from {images_dir.name}")
        except Exception as exc:
            print(f"[SPEC PLANNER] user image load failed: {exc}")
            if sp_multimodal_mode == "always":
                return {
                    "error_type": ErrorType.FATAL,
                    "cad_brief": None,
                    "execution_log": [
                        f"node_spec_planner: FATAL — "
                        f"SPEC_PLANNER_MULTIMODAL=always but image loading failed: {exc}"
                    ],
                    "node_history": list(state.get("node_history", [])) + ["planner"],
                }
            # auto mode → continue with text-only

        # F2 fix: "always" mode requires images — _load_user_images returns []
        # (not exception) on empty dir, so the except block above doesn't catch it.
        # Explicit check: if always mode and no images loaded, return FATAL.
        if sp_multimodal_mode == "always" and not user_images:
            print(f"[SPEC PLANNER] SPEC_PLANNER_MULTIMODAL=always but no images found — returning FATAL")
            return {
                "error_type": ErrorType.FATAL,
                "cad_brief": None,
                "execution_log": [
                    f"node_spec_planner: FATAL — "
                    f"SPEC_PLANNER_MULTIMODAL=always but no user images found in user_input_images/"
                ],
                "node_history": list(state.get("node_history", [])) + ["planner"],
            }

    # -- Build messages (multimodal or text-only) -------------------------
    if user_images:
        # OpenAI-compatible multimodal: user content is a list of text + image_url blocks
        content_list = [{"type": "text", "text": user_prompt}]
        for img in user_images:
            data_url = _encode_jpeg_data_url(img)
            content_list.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
        user_content: list[dict] | str = content_list
        print(f"[SPEC PLANNER] Sending multimodal: 1 text + {len(user_images)} user images")
    else:
        user_content = user_prompt  # text-only path

    client = _llm_client()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT_SPEC_PLANNER},
        {"role": "user", "content": user_content},
    ]

    json_str = ""
    raw_response = ""
    # Broad except (not just ValueError/JSONDecodeError/RuntimeError) because
    # the OpenAI SDK raises openai.BadRequestError for "model doesn't support
    # image" — a subclass of openai.APIStatusError, NOT ValueError/RuntimeError.
    # Without this, auto-fallback would never fire on real multimodal-unsupported
    # errors from the API. The isinstance checks below distinguish:
    #   - multimodal_unsupported → retry text-only
    #   - ValueError (empty response) → FATAL
    #   - JSONDecodeError → DIMENSION (let graph retry)
    #   - other Exception → FATAL (unexpected)
    try:
        json_str, raw_response = _call_llm_json_with_retry(
            client, messages,
            model=_SP_MODEL,
            max_tokens=_SP_MAX_TOKENS,
            temperature=_SP_TEMP,
            extra_kwargs=_SPEC_PLANNER_KWARGS,
            max_retries=3,
        )
    except Exception as exc:
        # Auto-fallback on multimodal-unsupported API errors (only when images
        # were sent and we're in auto mode)
        err_str = str(exc).lower()
        # F5 fix: two-part check via _is_multimodal_unsupported_error()
        multimodal_unsupported = _is_multimodal_unsupported_error(err_str)
        if user_images and sp_multimodal_mode == "auto" and multimodal_unsupported:
            print(f"[SPEC PLANNER] multimodal unsupported ({exc}) — retrying text-only")
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT_SPEC_PLANNER},
                {"role": "user", "content": user_prompt},
            ]
            try:
                client = _llm_client()
                json_str, raw_response = _call_llm_json_with_retry(
                    client, messages,
                    model=_SP_MODEL,
                    max_tokens=_SP_MAX_TOKENS,
                    temperature=_SP_TEMP,
                    extra_kwargs=_SPEC_PLANNER_KWARGS,
                    max_retries=3,
                )
            except Exception as exc2:
                print(f"[SPEC PLANNER] text-only fallback failed: {exc2}")
                # F4 fix: JSONDecodeError in text-only fallback should return
                # DIMENSION (let graph retry planner), not FATAL — same as
                # direct text-only path would do. Previous code always returned
                # FATAL, which meant enabling multimodal could upgrade a
                # recoverable JSON parse error to an unrecoverable pipeline halt.
                if isinstance(exc2, json.JSONDecodeError):
                    return {
                        "error_type": ErrorType.DIMENSION,
                        "cad_brief": None,
                        "execution_log": [
                            f"node_spec_planner: JSON Decode (text-only fallback): {exc2}"
                        ],
                        "node_history": list(state.get("node_history", [])) + ["planner"],
                    }
                return {
                    "error_type": ErrorType.FATAL,
                    "cad_brief": None,
                    "execution_log": [
                        f"node_spec_planner: FATAL — text-only fallback failed: {exc2}"
                    ],
                    "node_history": list(state.get("node_history", [])) + ["planner"],
                }
        elif isinstance(exc, ValueError):
            # Empty response after retries → FATAL
            return {
                "error_type": ErrorType.FATAL,
                "cad_brief": None,
                "execution_log": [f"node_spec_planner: FATAL — {exc}"],
                "node_history": list(state.get("node_history", [])) + ["planner"],
            }
        elif isinstance(exc, json.JSONDecodeError):
            # JSON still broken after retries → let graph retry (DIMENSION)
            _safe_print(f"\n[DEBUG PLANNER] JSON decode exhausted: {exc}\n")
            return {
                "error_type": ErrorType.DIMENSION,
                "cad_brief": None,
                "execution_log": [f"node_spec_planner: JSON Decode exhausted: {exc}"],
                "node_history": list(state.get("node_history", [])) + ["planner"],
            }
        else:
            # RuntimeError or other — unexpected, treat as FATAL
            return {
                "error_type": ErrorType.FATAL,
                "cad_brief": None,
                "execution_log": [f"node_spec_planner: FATAL — {exc}"],
                "node_history": list(state.get("node_history", [])) + ["planner"],
            }

    # ------------------------------------------------------------------
    # 3. Validate Pydantic schema
    # ------------------------------------------------------------------
    try:
        parsed_dict = json.loads(json_str)
        cad_brief = CADBrief.model_validate(parsed_dict)
    except json.JSONDecodeError as e:
        _safe_print(f"\n[DEBUG PLANNER] {e}\n")
        return {
            "error_type": ErrorType.DIMENSION,
            "cad_brief": None,  # Explicitly clear stale cad_brief
            "execution_log": [f"node_spec_planner: JSON Decode: {e}"],
            "node_history": list(state.get("node_history", [])) + ["planner"],
        }
    except ValidationError as e:
        _safe_print(f"\n[DEBUG PLANNER] Schema Error: {e}\n")
        return {
            "error_type": ErrorType.DIMENSION,
            "cad_brief": None,  # Explicitly clear stale cad_brief
            "execution_log": [f"node_spec_planner: Schema Error: {e}"],
            "node_history": list(state.get("node_history", [])) + ["planner"],
        }

    # Always stamp the original user request into the brief
    cad_brief.user_request_raw = user_request

    # ------------------------------------------------------------------
    # 4. Success
    # ------------------------------------------------------------------
    target_count = len(cad_brief.verification_targets)
    log_lines = [
        f"node_spec_planner [iter {iteration}]: SUCCESS",
        f"  Part name      : {cad_brief.part_name}",
        f"  Category       : {cad_brief.part_category}",
        f"  Origin         : {cad_brief.origin_convention}",
        f"  Workplane      : {cad_brief.primary_workplane}",
        f"  Key params     : {len(cad_brief.key_parameters)} extracted",
        f"  Verify targets : {target_count} defined",
    ]
    for vt in cad_brief.verification_targets[:8]:
        log_lines.append(f"    - {vt.id} ({vt.kind.value}) nominal={vt.nominal}")
    if target_count > 8:
        log_lines.append(f"    ... and {target_count - 8} more")

    # -- Save to cache --------------------------------------------------------
    _save_cache(_CACHE_DIR / "cad_brief.json", {
        **cad_brief.model_dump(), "__input_fingerprint": fingerprint,
    })

    return {
        "cad_brief": cad_brief,
        "execution_log": log_lines,
        "node_history": list(state.get("node_history", [])) + ["planner"],
    }


# ============================================================================
# Node: Geometric Architect
# ============================================================================


def node_geometric_architect(state: GraphState) -> dict:
    """Translate a CADBrief into a step-by-step build123d ArchitectPlan.

    Reads the CADBrief specification and any previous QA feedback (when
    ``error_type == TOPOLOGY``) and produces a structured modeling plan
    that the Python Coder can translate directly into build123d API calls.

    On a topology-level retry the QA error details are injected into the
    prompt so the Architect can fundamentally redesign the structure
    (add ribs, change the sketch profile, fix disconnected bodies, etc.).

    Parameters
    ----------
    state : GraphState
        Must contain ``cad_brief``.  Optionally contains ``qa_report`` and
        ``error_type`` for topology-level correction loops.

    Returns
    -------
    dict
        ``{"architect_plan": ArchitectPlan(...), "execution_log": [...]}``
        on success, or ``{"error_type": ErrorType.FATAL, ...}`` on failure.
    """
    iteration: int = state.get("iteration_count", 0)
    cad_brief = state.get("cad_brief")
    qa_report = state.get("qa_report")
    error_type = state.get("error_type")
    force_refresh: bool = state.get("force_refresh", False)
    fingerprint = _planning_fingerprint("geometric_architect", {
        "brief": _plan_or_dict_to_json(cad_brief),
        "request": state.get("user_request", ""),
    })

    # -- Cache: skip LLM call if we already have a valid ArchitectPlan ------
    # Only use cache for fresh designs, not retries
    is_retry = (
        error_type is not None
        and (
            (hasattr(error_type, "value") and error_type.value in ("topology", "dimension"))
            or str(error_type) in ("topology", "dimension")
        )
    )
    if not force_refresh and not is_retry:
        cached = _load_cache(_CACHE_DIR / "architect_plan.json")
        if isinstance(cached, dict) and cached.get("__input_fingerprint") == fingerprint:
            try:
                plan = ArchitectPlan.model_validate(cached)
                print("[CACHE] Loaded architect_plan.json — skipping Geometric Architect")
                return {
                    "architect_plan": plan,
                    "iteration_count": iteration,
                    "execution_log": ["node_geometric_architect: loaded from cache"],
                    "node_history": list(state.get("node_history", [])) + ["architect"],
                }
            except Exception:
                pass

    if cad_brief is None:
        return _planner_fatal(
            iteration=iteration,
            reason="cad_brief is None — the Spec Planner must run first.",
            node="node_geometric_architect",
        )

    # Serialise the brief for the LLM
    brief_json = _plan_or_dict_to_json(cad_brief)

    # ------------------------------------------------------------------
    # 1. Build user prompt (with optional topology retry context)
    # ------------------------------------------------------------------
    is_topology_retry = (
        error_type is not None
        and (
            (hasattr(error_type, "value") and error_type.value == "topology")
            or str(error_type) == "topology"
        )
        and qa_report is not None
    )

    is_dimension_retry = (
        error_type is not None
        and (
            (hasattr(error_type, "value") and error_type.value == "dimension")
            or str(error_type) == "dimension"
        )
        and qa_report is not None
    )

    is_force_edit = state.get("retry_mode") == "force_edit"

    if is_topology_retry:
        qa_errors = _extract_qa_error_details(qa_report)
        prev_plan = state.get("architect_plan")
        prev_plan_json = _plan_or_dict_to_json(prev_plan) if prev_plan is not None else "{}"
        old_step_count = len(getattr(prev_plan, "steps", []) or []) if prev_plan else 0
        old_sketch_count = len(getattr(prev_plan, "sketches", []) or []) if prev_plan else 0

        architect_feedback = (
            "## TOPOLOGY RETRY — Fix disconnected bodies, preserve ALL features\n\n"
            "The STL mesh analysis detected that the part is **split into separate "
            "shells** (disconnected bodies). This means two or more solids do not "
            "share any volume — they are physically separate pieces.\n\n"
            "### QA Findings\n\n"
        )
        for i, err in enumerate(qa_errors, 1):
            architect_feedback += f"  [{i}] {err}\n"
        architect_feedback += (
            "\n### ⚠️ IRON RULE: You MUST preserve ALL features\n\n"
            f"The previous plan has **{old_step_count} steps** across **{old_sketch_count} sketches**. "
            f"Your output MUST contain at least **{old_step_count} steps** and **{old_sketch_count} sketches**. "
            f"Deleting holes, fillets, gussets, or any other feature is FORBIDDEN. "
            f"If the previous plan had counterbore holes, your plan MUST also have them.\n\n"
            "### Allowed changes (ONLY these)\n\n"
            "  ✅ Add a `boolean_union` step to fuse disconnected bodies\n"
            "  ✅ Adjust `distance_mm` / extrusion amounts to create overlap (≥0.1mm)\n"
            "  ✅ Modify `Pos` / `hole_position` coordinates to fix alignment\n"
            "  ✅ Change `direction` from 'positive' to 'both' for symmetric overlap\n\n"
            "### Forbidden (will cause REJECTION)\n\n"
            "  ❌ Deleting ANY step, sketch, hole, fillet, chamfer, or gusset\n"
            "  ❌ Reducing step count or sketch count\n"
            "  ❌ Changing `step_type` values\n"
            "  ❌ Removing `depends_on` references\n\n"
            "### How to fix disconnected bodies (concrete example)\n\n"
            "If you have two extrusions that don't touch, add a union step:\n"
            '```json\n'
            '{\n'
            '  "step_id": "step-XX-union",\n'
            '  "step_type": "boolean_union",\n'
            '  "label": "Union base and column into single solid",\n'
            '  "target_step_id": "step-01-extrude-base",\n'
            '  "tool_step_id": "step-02-extrude-column",\n'
            '  "depends_on": ["step-01-extrude-base", "step-02-extrude-column"]\n'
            '}\n'
            '```\n'
            "Then make ALL subsequent steps depend on this union step instead.\n\n"
            "If extrusions are adjacent but not overlapping, ensure they "
            "overlap by the FULL thickness of the base/connected body — not just "
            "0.1mm.  Example: base is 42×42×5mm (Z=0..5).  Vertical plate must "
            "extrude from Z=0 to Z=42 (NOT from Z=5 to Z=47).  The vertical "
            "plate should PASS THROUGH the base, sharing the volume Z=0..5.  "
            "Two bodies that only touch at a surface CANNOT be unioned — they "
            "must share substantial 3D volume.\n\n"
            "### Previous Plan (edit THIS JSON)\n\n"
            "```json\n"
            f"{prev_plan_json}\n"
            "```\n\n"
            "**Instructions**: Copy the JSON above. Apply ONLY the allowed "
            "changes to fix the disconnected bodies. Preserve ALL features. "
            "Return the complete edited JSON.\n"
        )
        revision_note = (
            f"v{_attr(cad_brief, 'spec_version', 1)} — topology retry, "
            f"iteration {iteration}"
        )
    elif is_dimension_retry:
        qa_errors = _extract_qa_error_details(qa_report)
        # Include the previous plan so the LLM edits it in-place instead of
        # regenerating from scratch (which almost always produces identical values).
        prev_plan = state.get("architect_plan")
        prev_plan_json = _plan_or_dict_to_json(prev_plan) if prev_plan is not None else "{}"
        architect_feedback = (
            "## DIMENSION RETRY — Edit ONLY the numeric fields\n\n"
            "Below is the **previous plan JSON** that produced a part with "
            "dimension errors.  Your job: **edit this JSON directly** — change "
            "ONLY numeric values using 70% damping.  Do NOT touch sketch_ids, "
            "step_ids, depends_on, step ordering, or add/remove anything.\n\n"
            "### 70% Damping Formula\n\n"
            "  new_value = current_plan_value + 0.7 × (target_nominal − measured_value)\n\n"
            "Example: plan has hole_diameter_mm=3.4, target=4.2, measured=3.5:\n"
            "  new = 3.4 + 0.7 × (4.2 − 3.5) = 3.4 + 0.49 = 3.89\n\n"
            "### Errors to fix (measured value → target nominal)\n\n"
        )
        for i, err in enumerate(qa_errors, 1):
            architect_feedback += f"  [{i}] {err}\n"
        architect_feedback += (
            "\n### Previous Plan (edit THIS JSON)\n\n"
            "```json\n"
            f"{prev_plan_json}\n"
            "```\n\n"
            "**Instructions**: Copy the JSON above.  For each error, find the "
            "numeric field in the plan that most closely matches the measured "
            "value, apply the damping formula, and update it.  Change NOTHING "
            "else.  Return the complete edited JSON.\n"
        )
        revision_note = (
            f"v{_attr(cad_brief, 'spec_version', 1)} — dimension retry "
            f"(70% damping), iteration {iteration}"
        )
    elif is_force_edit:
        # ── FORCE EDIT MODE: JSON patch output ──────────────────────────
        stall = state.get("stall_count", 1)
        prev_plan = state.get("architect_plan")
        prev_plan_json = _plan_or_dict_to_json(prev_plan) if prev_plan is not None else "{}"
        architect_feedback = (
            f"## STALL #{stall} DETECTED — FORCE EDIT MODE\n\n"
            f"**YOU RETURNED THE EXACT SAME PLAN AS LAST TIME.**\n"
            f"This is a critical error. Do NOT output the full plan again.\n\n"
            f"### Your Last (WRONG) Answer — DO NOT REPEAT:\n\n"
            f"```json\n{prev_plan_json}\n```\n\n"
            f"### Required Output: FLAT JSON PATCH\n\n"
            f"Output ONLY a flat JSON object where keys are dot-separated paths\n"
            f"to numeric fields, and values are the CORRECTED numbers:\n\n"
            f'```json\n{{\n'
            f'  "key_dimensions.VERTICAL_HEIGHT": 42.0,\n'
            f'  "key_dimensions.SHAFT_HOLE_DIA": 22.0,\n'
            f'  "steps.3.hole_diameter_mm": 22.0,\n'
            f'  "steps.4.hole_position": {{"x": 0.0, "y": 23.5, "z": 26.0}}\n'
            f'}}\n```\n\n'
            f"Rules:\n"
            f"- Keys: \"key_dimensions.FIELD_NAME\" for key dimensions\n"
            f"- Keys: \"steps.INDEX.field_name\" for step parameters (INDEX is 0-based)\n"
            f"- Keys: \"sketches.INDEX.entities.0.width\" for sketch entity fields\n"
            f"- Values must be numbers or Point3D objects (NOT strings)\n"
            f"- Change at least ONE field\n"
            f"- Output ONLY the JSON patch — no explanations, no markdown fences\n"
        )
        revision_note = (
            f"v{_attr(cad_brief, 'spec_version', 1)} — force_edit stall #{stall}"
        )
    else:
        # Check if this is a self-correction retry (JSON/Schema validation failed)
        is_self_retry = (
            state.get("architect_plan") is None
            and len(state.get("node_history", [])) > 0
            and state.get("node_history", [])[-1] == "architect"
        )
        if is_self_retry:
            prev_errors = state.get("execution_log", [])
            architect_feedback = (
                "## SELF-CORRECTION RETRY — Previous JSON was REJECTED\n\n"
                "Your last ArchitectPlan JSON failed validation. "
                "Fix ALL of these issues:\n\n"
            )
            for err in prev_errors[-3:]:
                architect_feedback += f"  - {err}\n"
            architect_feedback += (
                "\nCommon mistakes: using wrong ModelingStepType enum values, "
                "putting arrays in key_dimensions (use separate scalar keys), "
                "missing required fields (step_id, step_type, label, depends_on), "
                "or having depends_on reference non-existent step_ids.\n"
            )
            revision_note = f"v{_attr(cad_brief, 'spec_version', 1)} — self-correction retry"
        else:
            architect_feedback = (
                "This is a fresh design — no previous QA feedback to incorporate."
            )
            revision_note = "v1 — initial design"

    if is_force_edit:
        task_text = textwrap.dedent("""\
        ## Task

        You are in FORCE EDIT MODE.  Your previous plan was IDENTICAL to
        the one before it and the QA engines detected dimension errors.
        This is your last chance before the pipeline gives up.

        Output ONLY a FLAT JSON PATCH — keys are dot-paths, values are
        corrected numbers.  Do NOT output the full plan JSON.

        Example correct output:
        {"key_dimensions.VERTICAL_HEIGHT": 42.0, "steps.2.hole_diameter_mm": 22.0}

        No markdown fences.  No explanations.  Just the patch JSON.
        """)
    elif is_dimension_retry:
        task_text = textwrap.dedent("""\
        ## Task

        The previous plan JSON is embedded in the QA Feedback section above.
        **Copy it, edit only the numeric fields** listed in the damping
        instructions, and return the complete edited JSON.  Do NOT change
        sketch_ids, step_ids, depends_on, step ordering, or anything
        structural.  Only change numbers.
        """)
    else:
        task_text = textwrap.dedent("""\
        ## Task

        Design a complete, step-by-step ArchitectPlan for this part.  Follow the
        Design Principles in the system prompt exactly:

        1. Define all 2D sketches first with unique sketch_id values.
        2. Order operations: additive → subtractive → finishing.
        3. Fillets and chamfers MUST be the last steps.
        4. Every step needs a short label and correct depends_on references.
        5. Collect all numeric dimensions into key_dimensions.
        """)

    original_request = state.get("user_request", "")

    # Extract special_features from CADBrief — these are non-trivial geometric
    # constraints (symmetry, feature placement, avoidance rules, etc.) that the
    # user request mentions but get lost in the long text.  Show them as a
    # separate section so the Architect verifies each one explicitly when
    # designing the plan.
    special_features = _attr(cad_brief, "special_features", []) or []
    if special_features:
        features_lines = []
        for i, feat in enumerate(special_features, 1):
            features_lines.append(f"  [{i}] {feat}")
        special_features_section = (
            "## 🔍 Special Features (MUST satisfy each constraint in your plan)\n\n"
            "These are non-trivial geometric constraints extracted from the user request.\n"
            "Your plan MUST satisfy every item.  When designing sketches and steps,\n"
            "verify each constraint is reflected (in coordinates, workplane_offset,\n"
            "step_type, or notes).\n\n"
            + "\n".join(features_lines)
            + "\n"
        )
    else:
        special_features_section = ""

    user_prompt = textwrap.dedent(f"""\
    ## Original User Request (ground truth — never deviate from this)

    {original_request}

    {special_features_section}\
    ## CAD Brief

    ```json
    {brief_json}
    ```

    ## QA Feedback

    {architect_feedback}

    {task_text}
    Revision note: {revision_note}

    Return ONLY the ```json fenced block — no other text.
    """)

    # ------------------------------------------------------------------
    # 2. Call Qwen (DashScope) with JSON retry
    # ------------------------------------------------------------------
    client = _llm_client()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT_GEOMETRIC_ARCHITECT},
        {"role": "user", "content": user_prompt},
    ]

    json_str = ""
    raw_response = ""
    try:
        json_str, raw_response = _call_llm_json_with_retry(
            client, messages,
            model=_ARCH_MODEL,
            max_tokens=_ARCH_MAX_TOKENS,
            temperature=_ARCH_TEMP,
            extra_kwargs=_ARCHITECT_KWARGS,  # Disable thinking for Architect to save output tokens
            max_retries=3,
        )
    except ValueError as exc:
        return {
            "error_type": ErrorType.FATAL,
            "execution_log": [
                f"node_geometric_architect: FATAL — {exc}"
            ],
            "node_history": list(state.get("node_history", [])) + ["architect"],
        }
    except json.JSONDecodeError as exc:
        print(f"\n[DEBUG ARCHITECT] JSON decode exhausted: {exc}\n")
        return {
            "error_type": ErrorType.DIMENSION,
            "execution_log": [f"node_geometric_architect: JSON Decode exhausted: {exc}"],
            "node_history": list(state.get("node_history", [])) + ["architect"],
        }
    except Exception as exc:
        return _planner_fatal(
            iteration=iteration,
            reason=f"LLM API call failed: {exc}",
            node="node_geometric_architect",
        )

    # ------------------------------------------------------------------
    # 3. Validate / apply output
    # ------------------------------------------------------------------
    if is_force_edit:
        # Parse as flat JSON patch, apply to previous plan, validate result
        try:
            parsed_dict = json.loads(json_str)
            if not isinstance(parsed_dict, dict):
                raise ValueError("Patch must be a JSON object")
            # Apply patch to previous plan dict
            prev_plan_dict = json.loads(_plan_or_dict_to_json(state.get("architect_plan")))
            _apply_dimension_patch(prev_plan_dict, parsed_dict)
            architect_plan = ArchitectPlan.model_validate(prev_plan_dict)
            print(f"[FORCE EDIT] Applied {len(parsed_dict)} field(s) from JSON patch")
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as e:
            print(f"\n[DEBUG ARCHITECT] Force edit patch failed: {e}\n")
            return {
                "error_type": ErrorType.DIMENSION,
                "execution_log": [f"node_geometric_architect: Force edit failed: {e}"],
                "node_history": list(state.get("node_history", [])) + ["architect"],
            }
    else:
        try:
            parsed_dict = json.loads(json_str)
            parsed_dict = _normalize_architect_plan(parsed_dict)
            architect_plan = ArchitectPlan.model_validate(parsed_dict)
        except json.JSONDecodeError as e:
            print(f"\n[DEBUG ARCHITECT] {e}\n")
            return {
                "error_type": ErrorType.DIMENSION,
                "execution_log": [f"node_geometric_architect: JSON Decode: {e}"],
                "node_history": list(state.get("node_history", [])) + ["architect"],
            }
        except ValidationError as e:
            print(f"\n[DEBUG ARCHITECT] Schema Error: {e}\n")
            return {
                "error_type": ErrorType.DIMENSION,
                "execution_log": [f"node_geometric_architect: Schema Error: {e}"],
                "node_history": list(state.get("node_history", [])) + ["architect"],
            }

    # ------------------------------------------------------------------
    # 4. Anti-Lazy Guard: reject plans that delete features
    # ------------------------------------------------------------------
    step_count = len(architect_plan.steps)
    sketch_count = len(architect_plan.sketches)

    if is_topology_retry:
        prev_plan = state.get("architect_plan")
        old_steps = len(getattr(prev_plan, "steps", []) or []) if prev_plan else step_count
        old_sketches = len(getattr(prev_plan, "sketches", []) or []) if prev_plan else sketch_count
        if step_count < old_steps or sketch_count < old_sketches:
            rejection_msg = (
                f"FEATURE DELETION REJECTED: previous plan had {old_steps} steps "
                f"and {old_sketches} sketches. Your new plan has only {step_count} "
                f"steps and {sketch_count} sketches. You deleted features! "
                f"All original steps and sketches MUST be preserved. "
                f"Copy the original JSON and ONLY add boolean_union or adjust positions."
            )
            print(f"\n[ANTI-LAZY GUARD] {rejection_msg}\n")
            return {
                "error_type": ErrorType.DIMENSION,
                "execution_log": [
                    f"node_geometric_architect: ANTI-LAZY GUARD — {rejection_msg}"
                ],
                "node_history": list(state.get("node_history", [])) + ["architect"],
            }

    # ------------------------------------------------------------------
    # 5. Success
    # ------------------------------------------------------------------
    log_lines = [
        f"node_geometric_architect [iter {iteration}]: SUCCESS",
        f"  Plan ID        : {architect_plan.plan_id}",
        f"  Sketches       : {sketch_count}",
        f"  Steps          : {step_count}",
        f"  Key dims       : {len(architect_plan.key_dimensions)}",
        f"  Topology retry : {is_topology_retry}",
    ]
    for step in architect_plan.steps[:8]:
        deps = ", ".join(step.depends_on) if step.depends_on else "none"
        log_lines.append(f"    {step.step_id} ({step.step_type.value}) depends_on=[{deps}]")
    if step_count > 8:
        log_lines.append(f"    ... and {step_count - 8} more steps")

    # -- Save to cache --------------------------------------------------------
    _save_cache(_CACHE_DIR / "architect_plan.json", {
        **architect_plan.model_dump(), "__input_fingerprint": fingerprint,
    })

    return {
        "architect_plan": architect_plan,
        "iteration_count": iteration,
        "execution_log": log_lines,
        "node_history": list(state.get("node_history", [])) + ["architect"],
        "retry_mode": None,     # clear force_edit after success
        "stall_count": 0,       # reset stall counter
    }


# ---------------------------------------------------------------------------
# Shared fatal-state helper for Planner & Architect nodes
# ---------------------------------------------------------------------------


def _planner_fatal(*, iteration: int, reason: str, node: str) -> dict:
    """Return a FATAL state update for upstream planning nodes.

    Unlike the Coder (which can retry on DIMENSION), Planner and Architect
    failures are hard blockers — there is no higher-level agent to fix a
    bad CADBrief or ArchitectPlan, so we escalate to FATAL.
    """
    return {
        "error_type": ErrorType.FATAL,
        "iteration_count": iteration,
        "execution_log": [
            f"{node} [iter {iteration}]: FATAL — {reason}"
        ],
    }


# Internal implementations are imported after shared configuration is initialized.
# Keep these historical names available: runners, assembly helpers and tests
# replace attributes on this module, and implementations look them up at call time.
from multi_agent_cad._node_codegen import (
    _VALIDATION_HELPERS as _VALIDATION_HELPERS,
    _extract_code_from_llm_response as _extract_code_from_llm_response,
    _extract_primitive_dimensions as _extract_primitive_dimensions,
    _build_primitive_sketch_and_extrude as _build_primitive_sketch_and_extrude,
    _node_python_coder_deterministic as _node_python_coder_deterministic,
    _build_shim_str as _build_shim_str,
    _plan_to_code as _plan_to_code,
    _derive_positions as _derive_positions,
    _apply_dimension_patch as _apply_dimension_patch,
    _sketch_workplane as _sketch_workplane,
    _gen_sketch_algebra as _gen_sketch_algebra,
    _has_unsupported_placeholders as _has_unsupported_placeholders,
    _fill_unsupported_with_aider as _fill_unsupported_with_aider,
    _infer_edge_filter as _infer_edge_filter,
    _build_coder_user_prompt as _build_coder_user_prompt,
    _extract_qa_error_details as _extract_qa_error_details,
    _coder_failure_state as _coder_failure_state,
)
from multi_agent_cad._node_step_qa import (
    _load_step_topology as _load_step_topology,
    _parse_selector as _parse_selector,
    _resolve_selector as _resolve_selector,
    _measure_entities as _measure_entities,
    _vector_axis_aligned as _vector_axis_aligned,
    _find_cylindrical_faces as _find_cylindrical_faces,
    _find_planar_faces as _find_planar_faces,
    _face_center as _face_center,
    _run_engine_a_cadpy as _run_engine_a_cadpy,
    _measure_target_cadpy as _measure_target_cadpy,
    _feature_not_found as _feature_not_found,
)
from multi_agent_cad._node_mesh_qa import (
    _cadbrief_to_spec_json as _cadbrief_to_spec_json,
    _fallback_connectivity_check as _fallback_connectivity_check,
    _build_single_body_result as _build_single_body_result,
    _run_engine_b_check_mesh as _run_engine_b_check_mesh,
    _adjust_for_radius_semantics as _adjust_for_radius_semantics,
    _extract_measurement_for_kind as _extract_measurement_for_kind,
    _find_best_feature_match as _find_best_feature_match,
    _find_compound_gap_measurement as _find_compound_gap_measurement,
    _measure_target_via_check_mesh as _measure_target_via_check_mesh,
)
from multi_agent_cad._node_qa_reports import (
    _qa_report_or_dict as _qa_report_or_dict,
    _merge_engine_reports as _merge_engine_reports,
    _attr as _attr,
    _to_verification_target as _to_verification_target,
    _validate_geometry_against_request as _validate_geometry_against_request,
)
from multi_agent_cad._node_execution import (
    _build_child_env as _build_child_env,
    _remove_execution_outputs as _remove_execution_outputs,
    _tail as _tail,
    _file_size_kb as _file_size_kb,
    _execute_cad_script as _execute_cad_script,
    _optimize_print_orientation as _optimize_print_orientation,
)
from multi_agent_cad._node_repair import (
    _join_aider_summarizer as _join_aider_summarizer,
    _build_autonomous_repair_prompt as _build_autonomous_repair_prompt,
    generate_initial_solution as generate_initial_solution,
    _run_repair_on_script as _run_repair_on_script,
    _run_direct_repair_fallback as _run_direct_repair_fallback,
)
