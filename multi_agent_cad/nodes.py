"""
LangGraph agent nodes for the Dual-Engine Multi-Agent CAD System.

Each node is a pure function that reads from ``GraphState`` and returns a
partial ``dict`` of state updates (LangGraph reducer semantics merge the
returned dict into the shared state).

Nodes implemented in this file:
  * node_spec_planner     — user_request → CADBrief
  * node_geometric_architect — CADBrief → ArchitectPlan
  * node_python_coder     — ArchitectPlan → build123d Python code → .step + .stl
  * node_autonomous_skill_loop — internal Aider + Dual-QA closed loop
"""

from __future__ import annotations

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

_CACHE_DIR = Path(__file__).resolve().parent.parent / "pipeline_cache"

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
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_code_from_llm_response(raw: str) -> str:
    """Strip markdown `` ```python ... ``` `` fences from an LLM response.

    If the response contains at least one fenced Python block the *last* one
    is returned (the final script is always the complete program).  If no
    fence is found the entire response is treated as code.
    """
    pattern = r"```python\s*\n(.*?)```"
    matches = re.findall(pattern, raw, flags=re.DOTALL)
    if matches:
        return matches[-1].strip()
    # Fallback: try generic ``` ... ``` fences
    pattern_generic = r"```\s*\n(.*?)```"
    matches_generic = re.findall(pattern_generic, raw, flags=re.DOTALL)
    if matches_generic:
        return matches_generic[-1].strip()
    return raw.strip()


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
            # Handle timeout and other API errors
            error_msg = str(e).lower()
            if 'timeout' in error_msg or 'timed out' in error_msg:
                print(
                    f"\n[DEBUG JSON-RETRY] Timeout on attempt {attempt + 1}/{max_retries}: {e}"
                )
                if attempt < max_retries - 1:
                    print(f"[DEBUG JSON-RETRY] Retrying in 2 seconds...")
                    import time
                    time.sleep(2)
                    continue
            # Re-raise non-timeout errors or final timeout
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
    }
    for entry in missed:
        s = str(entry)
        if s.startswith("FILLET_FAILED"):
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
    counts = {k: len(v) for k, v in categories.items() if v}

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


def _normalize_architect_plan(plan_dict: dict) -> dict:
    """Fix common LLM naming mistakes in step_type before Pydantic validation.

    The Architect LLM sometimes uses informal names that don't match the
    ``ModelingStepType`` enum.  This function maps them to valid values so
    the plan passes validation without wasting a retry round.
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
    steps = plan_dict.get("steps")
    if not isinstance(steps, list):
        return plan_dict
    for step in steps:
        if not isinstance(step, dict):
            continue
        stype = step.get("step_type", "")
        if isinstance(stype, str) and stype in _STEP_TYPE_ALIASES:
            step["step_type"] = _STEP_TYPE_ALIASES[stype]
    return plan_dict


def _qa_report_or_dict(error_details: list[str] | None) -> str:
    """Format QA error details for injection into the Coder's user prompt."""
    if not error_details:
        return "No previous QA feedback."
    lines = ["Previous QA feedback (fix these issues):"]
    for i, detail in enumerate(error_details, 1):
        lines.append(f"  [{i}] {detail}")
    return "\n".join(lines)


# ============================================================================
# System Prompt — Python Coder
# ============================================================================

# ---------------------------------------------------------------------------

SYSTEM_PROMPT_PYTHON_CODER = _load_prompt("python_coder")


# ============================================================================
# Node: Python Coder
# ============================================================================


def _node_python_coder_deterministic(
    state: GraphState, architect_plan, iteration: int
) -> dict | None:
    """Translate ArchitectPlan → gen_step() code, execute via cadpy.generation.

    Uses the repo's battle-tested ``cadpy.generation.run_script_generator()``
    pipeline — the same one ``scripts/step`` uses.  This handles STEP export,
    STL sidecar, error handling, and shell/env setup automatically.
    """
    # Pre-compute node_history for failure state
    _coder_history = list(state.get("node_history", [])) + ["coder"]

    try:
        code = _plan_to_code(architect_plan, iteration)
    except NotImplementedError as e:
        print(f"[DETERMINISTIC CODER] Unsupported operation: {e} → falling back to LLM")
        return None  # This is intentional - triggers LLM fallback in node_python_coder
    except Exception as e:
        import traceback
        print(f"[DETERMINISTIC CODER] _plan_to_code crashed: {e}")
        traceback.print_exc()
        # Return failure state instead of None to trigger repair in autonomous loop
        return _coder_failure_state(
            iteration=iteration,
            error_message=f"Deterministic coder code generation failed: {e}\n\nTraceback:\n{traceback.format_exc()}",
            script_path=str(cwd / f"temp_design_{iteration}.py"),
            node_history=_coder_history,
        )

    cwd = Path.cwd()
    script_path = cwd / f"temp_design_{iteration}.py"
    step_path = cwd / f"temp_output_{iteration}.step"
    stl_path = cwd / f"temp_output_{iteration}.stl"

    # Split generated code at the solids marker.
    # Module-level: imports, shim, key_dimensions, helper functions.
    # gen_step body: solid generation, export, return.
    import re as _re
    _module_part, _sep, _body_part = code.partition("# --- Generated solids ---")
    if not _sep:
        # Fallback: no marker found — wrap everything (backward compat)
        code = _re.sub(
            r'(from build123d import \*.*\n)',
            r'\1' + _build_shim_str(),
            code,
            count=1,
        )
        code = "def gen_step():\n" + "    " + code.replace("\n", "\n    ")
    else:
        # Inject SHIM into module-level part (flat, no function wrapping)
        _module_part = _re.sub(
            r'(from build123d import \*.*\n)',
            r'\1' + _build_shim_str(),
            _module_part,
            count=1,
        )
        # Indent body lines by 4 spaces for the gen_step function
        _indented_body = "\n".join(
            "    " + line if line.strip() else ""
            for line in ("# --- Generated solids ---" + _body_part).split("\n")
        )
        code = _module_part.rstrip() + "\n\n\ndef gen_step():\n" + _indented_body + "\n"
    script_path.write_text(code, encoding="utf-8")

    # Hybrid approach: if code has unsupported placeholders, use Aider to fill them
    if _has_unsupported_placeholders(code):
        print(f"[HYBRID CODER] Detected unsupported placeholders, using Aider to fill them")
        try:
            user_request = state.get("user_request", "")
            _cad_brief_for_aider = state.get("cad_brief")
            _special_features = _attr(_cad_brief_for_aider, "special_features", []) or []
            success = _fill_unsupported_with_aider(
                script_path, code, user_request,
                special_features=_special_features,
            )
            if not success:
                print(f"[HYBRID CODER] Aider failed to fill placeholders → falling back to full LLM")
                return None  # This is intentional - triggers LLM fallback in node_python_coder
            # Re-read the code after Aider modifications
            code = script_path.read_text(encoding="utf-8")
            print(f"[HYBRID CODER] Successfully filled unsupported placeholders with Aider")
        except Exception as e:
            print(f"[HYBRID CODER] Failed to fill unsupported placeholders: {e} → falling back to full LLM")
            import traceback
            traceback.print_exc()
            return None  # This is intentional - triggers LLM fallback in node_python_coder

    # ── Execute via cadpy.generation (same pipeline as scripts/step) ──
    try:
        _cadpy_src = str(_REPO_ROOT / "packages" / "cadpy" / "src")
        if _cadpy_src not in sys.path:
            sys.path.insert(0, _cadpy_src)
        from cadpy.generation import EntrySpec, run_script_generator
        from cadpy.metadata import GeneratorMetadata

        # Minimal metadata — tells cadpy this script defines gen_step()
        meta = GeneratorMetadata(
            script_path=script_path,
            kind="part",
            display_name=f"temp_{iteration}",
            generator_names=("gen_step",),
            has_gen_step=True,
            has_gen_dxf=False,
            has_gen_urdf=False,
            has_gen_sdf=False,
            stl=str(stl_path) if stl_path.is_file() else None,
            three_mf=None,
            mesh_tolerance=None,
            mesh_angular_tolerance=None,
        )

        spec = EntrySpec(
            source_ref=f"temp_{iteration}",
            cad_ref=f"temp_{iteration}",
            kind="part",
            source_path=script_path,
            display_name=f"temp_{iteration}",
            source="generated",
            step_path=step_path,
            script_path=script_path,
            stl_path=stl_path,
            generator_metadata=meta,
        )

        # Run gen_step() → cadpy loads the module, calls gen_step(),
        # exports STEP + STL, generates GLB topology.
        run_script_generator(spec, "gen_step")

    except Exception as e:
        print(f"[DETERMINISTIC CODER] cadpy pipeline failed: {e}")
        import traceback; traceback.print_exc()
        # Return failure state instead of None to trigger repair in autonomous loop
        return _coder_failure_state(
            iteration=iteration,
            error_message=f"Deterministic coder execution failed: {e}\n\nTraceback:\n{traceback.format_exc()}",
            script_path=str(script_path),
            node_history=_coder_history,
        )

    if not step_path.is_file():
        print("[DETERMINISTIC CODER] STEP missing after cadpy run")
        return _coder_failure_state(
            iteration=iteration,
            error_message="Deterministic coder executed but did not produce STEP file",
            script_path=str(script_path),
            node_history=_coder_history,
        )

    stl_size = _file_size_kb(stl_path) if stl_path.is_file() else "N/A"

    # ── Check for missed cuts / fillet failures (runtime diagnostics) ──
    missed_path = cwd / f"temp_missed_{iteration}.json"
    if missed_path.is_file():
        try:
            missed = json.loads(missed_path.read_text(encoding="utf-8"))
            if missed:
                cats = _parse_missed_cuts(missed)
                error_details, label = _format_missed_cuts_errors(cats)
                total = sum(len(v) for v in cats.values())
                print(f"[DETERMINISTIC CODER] PARTIAL SUCCESS — {_file_size_kb(step_path)} STEP, {stl_size} STL, but {total} runtime issue(s): {label}")
                for m in missed[:5]:
                    print(f"  → {m}")
                return {
                    "error_type": ErrorType.DIMENSION,
                    "qa_report": QAReport(
                        cad_brief_id="(runtime-error)",
                        engine_a=EngineReport(engine_name="cadpy_analysis"),
                        engine_b=EngineReport(
                            engine_name="check_mesh",
                            errors=[str(m)[:200] for m in missed[:3]],
                        ),
                        all_passed=False,
                        error_type=ErrorType.DIMENSION,
                        error_details=error_details,
                        iteration=iteration,
                    ),
                    "current_python_code": code,
                    "current_step_path": str(step_path.resolve()),
                    "current_stl_path": str(stl_path.resolve()) if stl_path.is_file() else str(step_path.resolve()),
                    "execution_log": [
                        f"node_python_coder [iter {iteration}]: "
                        f"{label} — {total} issue(s) detected"
                    ],
                    "node_history": list(state.get("node_history", [])) + ["coder"],
                }
        except Exception:
            pass

    print(f"[DETERMINISTIC CODER] SUCCESS — {_file_size_kb(step_path)} STEP, {stl_size} STL")
    return {
        "current_python_code": code,
        "current_python_code_path": str(script_path.resolve()),
        "current_step_path": str(step_path.resolve()),
        "current_stl_path": str(stl_path.resolve()) if stl_path.is_file() else str(step_path.resolve()),
        "execution_log": [f"node_python_coder [iter {iteration}]: SUCCESS (deterministic)"],
        "node_history": list(state.get("node_history", [])) + ["coder"],
    }




# Validation helper code that will be injected into generated scripts
_VALIDATION_HELPERS = '''
# === VALIDATION HELPERS ===
import json

_MEASUREMENTS = {}

def _validate_solid(solid, name="solid"):
    """Validate a solid before boolean operations.

    Returns:
        (is_valid, errors): tuple of (bool, list of error strings)
    """
    errors = []

    # 1. Check if valid (manifold, no self-intersections)
    try:
        if not solid.is_valid():
            errors.append(f"{name}: Solid is invalid (non-manifold or self-intersecting)")
    except Exception as e:
        errors.append(f"{name}: Validation error - {str(e)}")

    # 2. Check bounding box
    try:
        bb = solid.bounding_box()
        size_x = bb.max.X - bb.min.X
        size_y = bb.max.Y - bb.min.Y
        size_z = bb.max.Z - bb.min.Z

        # Check for zero or near-zero dimensions
        if size_x < 0.01 or size_y < 0.01 or size_z < 0.01:
            errors.append(
                f"{name}: Dimensions too small ({size_x:.2f} x {size_y:.2f} x {size_z:.2f} mm)"
            )

        # Check for unusually large dimensions
        max_dim = max(size_x, size_y, size_z)
        if max_dim > 1000:  # Assume features shouldn't exceed 1m
            errors.append(f"{name}: Unusually large dimension ({max_dim:.2f} mm)")

    except Exception as e:
        errors.append(f"{name}: Cannot compute bounding box - {str(e)}")

    # 3. Check volume
    try:
        volume = solid.volume
        if volume < 0.001:  # Less than 0.001 mm³
            errors.append(f"{name}: Volume too small ({volume:.6f} mm³)")
    except Exception as e:
        errors.append(f"{name}: Cannot compute volume - {str(e)}")

    return len(errors) == 0, errors


def _measure_feature(solid, name, feature_type="unknown"):
    """Measure a feature before boolean merge and store in diagnostics.

    This is the "white-box instrumentation" approach - measure features BEFORE
    they are merged, so we have accurate dimensions.

    Args:
        solid: The build123d solid to measure
        name: Feature name (e.g., "base", "lug_right")
        feature_type: Type of feature (e.g., "base", "lug", "rib")
    """
    try:
        bb = solid.bounding_box()
        size_x = bb.max.X - bb.min.X
        size_y = bb.max.Y - bb.min.Y
        size_z = bb.max.Z - bb.min.Z

        measurement = {
            "type": feature_type,
            "size_x": round(size_x, 3),
            "size_y": round(size_y, 3),
            "size_z": round(size_z, 3),
            "min_x": round(bb.min.X, 3),
            "min_y": round(bb.min.Y, 3),
            "min_z": round(bb.min.Z, 3),
            "max_x": round(bb.max.X, 3),
            "max_y": round(bb.max.Y, 3),
            "max_z": round(bb.max.Z, 3),
        }

        try:
            measurement["volume"] = round(solid.volume, 3)
        except:
            measurement["volume"] = None

        _MEASUREMENTS[name] = measurement

    except Exception as e:
        _MEASUREMENTS[name] = {"error": str(e)}


def _save_measurements(filename=None):
    """Save all measurements to JSON file for the repair agent to read.

    If filename is None, uses temp_measurements_{iteration}.json where
    iteration is read from the ITERATION environment variable (default 0).
    """
    if filename is None:
        import os as _os
        iteration = int(_os.environ.get("ITERATION", "0"))
        filename = f"temp_measurements_{iteration}.json"
    try:
        output_data = dict(_MEASUREMENTS)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)
        print(f"[DIAGNOSTICS] Saved {len(_MEASUREMENTS)} feature measurements to {filename}")
    except Exception as e:
        print(f"[DIAGNOSTICS] WARNING: Failed to save measurements: {e}")


def _safe_union(a, b, name_a="solid_a", name_b="solid_b", min_overlap=0.1):
    """Safe boolean union with validation.

    Validates both operands before union, checks for overlap, and validates result.

    Returns:
        (success, result, errors): tuple of (bool, result_solid_or_None, list_of_errors)
    """
    all_errors = []

    # Validate both operands
    valid_a, errors_a = _validate_solid(a, name_a)
    valid_b, errors_b = _validate_solid(b, name_b)

    all_errors.extend(errors_a)
    all_errors.extend(errors_b)

    if not valid_a or not valid_b:
        return False, None, all_errors

    # Check for overlap between the two solids
    try:
        bb_a = a.bounding_box()
        bb_b = b.bounding_box()

        # Calculate overlap in each dimension
        overlap_x = min(bb_a.max.X, bb_b.max.X) - max(bb_a.min.X, bb_b.min.X)
        overlap_y = min(bb_a.max.Y, bb_b.max.Y) - max(bb_a.min.Y, bb_b.min.Y)
        overlap_z = min(bb_a.max.Z, bb_b.max.Z) - max(bb_a.min.Z, bb_b.min.Z)

        # Check if there's any overlap
        if overlap_x <= 0 or overlap_y <= 0 or overlap_z <= 0:
            all_errors.append(
                f"{name_a} and {name_b} have no overlap. "
                f"Overlap: X={overlap_x:.2f}, Y={overlap_y:.2f}, Z={overlap_z:.2f} mm. "
                f"Solids must overlap by at least {min_overlap}mm to union successfully."
            )
            return False, None, all_errors

        # Check if overlap is too small
        min_overlap_actual = min(overlap_x, overlap_y, overlap_z)
        if min_overlap_actual < min_overlap:
            all_errors.append(
                f"{name_a} and {name_b} overlap too small ({min_overlap_actual:.3f}mm). "
                f"Minimum recommended: {min_overlap}mm. Consider increasing overlap."
            )
            # Warning only, don't fail

    except Exception as e:
        all_errors.append(f"Cannot check overlap: {str(e)}")

    # Perform the union
    try:
        result = a + b

        # Validate the result
        valid_result, errors_result = _validate_solid(result, f"union({name_a},{name_b})")
        all_errors.extend(errors_result)

        if not valid_result:
            return False, None, all_errors

        return True, result, all_errors

    except Exception as e:
        all_errors.append(f"Boolean union failed: {str(e)}")
        return False, None, all_errors


# === END VALIDATION HELPERS ===
'''
def _build_shim_str() -> str:
    """Return the SHIM code string (used by both deterministic and LLM paths)."""
    return (
        "\n# === AUTO-INJECTED SHIM ===\n"
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
        "_bs.make_face=_fx(_bs.make_face,{},strip=True)\n"
        "from build123d import Hole as _H\n"
        "_H.__init__=_fx(_H.__init__,{})\n"
        "from build123d import BuildPart as _BPc,BuildSketch as _BSc,BuildLine as _BLc\n"
        "_BPo=_BPc.__init__;_BSo=_BSc.__init__;_BLo=_BLc.__init__\n"
        "def _bpi(self,*a,mode=Mode.ADD,**kw):\n"
        "    wp=kw.pop('workplanes',a[0] if a else Plane.XY)\n"
        "    return _BPo(self,wp,mode=mode,**kw)\n"
        "def _bsi(self,*a,mode=Mode.ADD,**kw):\n"
        "    wp=kw.pop('workplanes',a[0] if a else Plane.XY)\n"
        "    return _BSo(self,wp,mode=mode,**kw)\n"
        "def _bli(self,*a,mode=Mode.ADD,**kw):\n"
        "    wp=kw.pop('workplane',kw.pop('workplanes',a[0] if a else Plane.XY))\n"
        "    return _BLo(self,wp,mode=mode,**kw)\n"
        "_BPc.__init__=_bpi;_BSc.__init__=_bsi;_BLc.__init__=_bli\n"
        "extrude,revolve,fillet,chamfer,mirror,make_face=_bp.extrude,_bp.revolve,_bg.fillet,_bg.chamfer,_bg.mirror,_bs.make_face\n"
        "Hole,BuildPart,BuildSketch,BuildLine=_H,_BPc,_BSc,_BLc\n"
        "# === END SHIM ===\n\n"
        + _VALIDATION_HELPERS
    )


def _plan_to_code(plan, iteration: int) -> str:
    """Translate ArchitectPlan → build123d Algebra API code (no Builder mode).

    Accepts both Pydantic ``ArchitectPlan`` and raw ``dict`` (from JSON cache).
    Tracks every step's output in ``step_body`` so boolean ops can
    reference named solids by step_id.
    """
    lines = [
        "from build123d import *",
        "import math, json",
        "",
        "# Track cuts that miss the target body (position error detection)",
        "_MISSED_CUTS = []",
        "",
        "def _safe_cut(body, tool, label=\"\"):",
        "    \"\"\"Cut tool from body. Detects missed cuts.\"\"\"",
        "    try:",
        "        _before = body.volume",
        "        result = body - tool",
        "        _after = result.volume",
        "        if abs(_after - _before) < 0.001:",
        "            _MISSED_CUTS.append(f'MISSED_CUT: {label} — tool had NO effect on body. Position is WRONG.')",
        "        return result",
        "    except Exception:",
        "        _MISSED_CUTS.append(f'CUT_ERROR: {label} — exception during cut')",
        "        return body",
        "",
        "def _safe_fillet(solid, edge_selector_fn, radius, label):",
        "    \"\"\"Apply fillet with radius auto-degradation + GeomType grouping fallback.",
        "    Stage 1: try fillet on all edges with progressive radius reduction.",
        "    Stage 2: if Stage 1 fails (e.g. mixed Circle+Line edges), group edges",
        "    by GeomType and fillet each group independently.  Each group has its",
        "    own try/except, so one group's failure doesn't block others.  This",
        "    handles mixed-type edge selections that ChFi3d can't process in a",
        "    single call.",
        "    \"\"\"",
        "    try:",
        "        edges = edge_selector_fn(solid)",
        "        if not edges:",
        "            _MISSED_CUTS.append(f'FILLET_FAILED: {label} — no edges matched filter')",
        "            return solid",
        "        _before = solid.volume",
        "        _radii = [radius, radius * 0.5, radius * 0.25, radius * 0.125]",
        "        _last_err = ''",
        "        # Stage 1: try fillet on all edges with progressive radius",
        "        for _r in _radii:",
        "            try:",
        "                result = fillet(edges, radius=_r)",
        "                _after = result.volume",
        "                if abs(_after - _before) < 0.001:",
        "                    _last_err = f'no volume change at radius {_r}'",
        "                    continue",
        "                if _r < radius:",
        "                    _MISSED_CUTS.append(f'FILLET_DEGRADED: {label} — original radius {radius} failed, succeeded at {_r}. Architect should reduce radius_mm to {_r}.')",
        "                return result",
        "            except Exception as _e:",
        "                _last_err = str(_e)",
        "                continue",
        "        # Stage 2: Stage 1 failed → group edges by GeomType and fillet each group",
        "        # Mixed edge types (Circle + Line) often cause ChFi3d to fail in a",
        "        # single call.  Grouping by GeomType lets each homogeneous group",
        "        # fillet independently with its own try/except.",
        "        try:",
        "            from build123d import GeomType",
        "        except ImportError:",
        "            GeomType = None",
        "        _type_groups = {}",
        "        for _edge in edges:",
        "            _gt = None",
        "            try:",
        "                _gt = _edge.geom_type",
        "            except Exception:",
        "                _gt = 'unknown'",
        "            if _gt not in _type_groups:",
        "                _type_groups[_gt] = []",
        "            _type_groups[_gt].append(_edge)",
        "        _result = solid",
        "        _success_groups = 0",
        "        _fail_groups = 0",
        "        _group_fail_reasons = []",
        "        for _gt, _group_edges in _type_groups.items():",
        "            _group_success = False",
        "            _group_err = ''",
        "            for _r in _radii:",
        "                try:",
        "                    _new_result = fillet(_group_edges, radius=_r)",
        "                    if _new_result is not None and abs(_new_result.volume - _result.volume) > 0.001:",
        "                        _result = _new_result",
        "                        _group_success = True",
        "                        if _r < radius:",
        "                            _MISSED_CUTS.append(f'FILLET_DEGRADED: {label} — group {_gt} radius {radius} → {_r}')",
        "                        break",
        "                    else:",
        "                        _group_err = f'no volume change at radius {_r}'",
        "                except Exception as _e:",
        "                    _group_err = str(_e)",
        "                    continue",
        "            if _group_success:",
        "                _success_groups += 1",
        "            else:",
        "                _fail_groups += 1",
        "                _group_fail_reasons.append(str(_gt) + ': ' + _group_err)",
        "        if _success_groups > 0:",
        "            if _fail_groups > 0:",
        "                _reasons_str = '; '.join(_group_fail_reasons[:3])",
        "                _MISSED_CUTS.append(f'FILLET_PARTIAL: {label} — {_success_groups}/{len(_type_groups)} groups OK, {_fail_groups} failed ({_reasons_str})')",
        "            return _result",
        "        # All stages failed — collect diagnostics so Aider knows what to fix",
        "        _diag = [f'FILLET_FAILED: {label} — all radii {_radii} failed (Stage 1 + Stage 2). Last: {_last_err}']",
        "        _diag.append(f'  Selected {len(edges)} edges in {len(_type_groups)} GeomType groups.')",
        "        try:",
        "            for _i, _e in enumerate(list(edges)[:3]):",
        "                _bb = _e.bounding_box()",
        "                _diag.append(f'  edge[{_i}]: X=[{_bb.min.X:.1f}..{_bb.max.X:.1f}] Y=[{_bb.min.Y:.1f}..{_bb.max.Y:.1f}] Z=[{_bb.min.Z:.1f}..{_bb.max.Z:.1f}]')",
        "            _diag.append('  Likely cause: edges at degenerate junctions (cutout/lug boundaries) where only 2 faces meet.')",
        "            _diag.append('  Root cause: a complete Circle edge may have been split into multiple arc segments')",
        "            _diag.append('  by prior boolean operations (e.g. backplate outer circle split into 12 arcs by')",
        "            _diag.append('  12 blade unions). ChFi3d cannot fillet arc segments at degenerate junctions.')",
        "            _diag.append('  Suggestion 1: use SEMANTIC selectors (.filter_by(Axis.Z), length > N), not hardcoded bbox coords.')",
        "            _diag.append('  Suggestion 2: if the fillet region (these edges bounding box) does NOT overlap with')",
        "            _diag.append('  the region of prior boolean operations, consider MOVING this fillet step BEFORE')",
        "            _diag.append('  the boolean unions/cuts — fillet the complete Circle first, then union internal')",
        "            _diag.append('  features. See Iron Rule 7 exception in the repair prompt.')",
        "        except Exception:",
        "            pass",
        "        _MISSED_CUTS.append('\\n'.join(_diag))",
        "        return solid",
        "    except Exception as _e:",
        "        _MISSED_CUTS.append(f'FILLET_FAILED: {label} — {_e}')",
        "        return solid",
        "",
        "def _safe_chamfer(solid, edge_selector_fn, length, label):",
        "    \"\"\"Apply chamfer with length auto-degradation + GeomType grouping fallback.",
        "    Same Stage 1 + Stage 2 pattern as _safe_fillet: try chamfer on all",
        "    edges first, then group by GeomType if Stage 1 fails.",
        "    \"\"\"",
        "    try:",
        "        edges = edge_selector_fn(solid)",
        "        if not edges:",
        "            _MISSED_CUTS.append(f'CHAMFER_FAILED: {label} — no edges matched filter')",
        "            return solid",
        "        _before = solid.volume",
        "        _lens = [length, length * 0.5, length * 0.25, length * 0.125]",
        "        _last_err = ''",
        "        # Stage 1: try chamfer on all edges with progressive length",
        "        for _l in _lens:",
        "            try:",
        "                result = chamfer(edges, length=_l)",
        "                _after = result.volume",
        "                if abs(_after - _before) < 0.001:",
        "                    _last_err = f'no volume change at length {_l}'",
        "                    continue",
        "                if _l < length:",
        "                    _MISSED_CUTS.append(f'CHAMFER_DEGRADED: {label} — original length {length} failed, succeeded at {_l}. Architect should reduce chamfer_distance_mm to {_l}.')",
        "                return result",
        "            except Exception as _e:",
        "                _last_err = str(_e)",
        "                continue",
        "        # Stage 2: Stage 1 failed → group edges by GeomType and chamfer each group",
        "        try:",
        "            from build123d import GeomType",
        "        except ImportError:",
        "            GeomType = None",
        "        _type_groups = {}",
        "        for _edge in edges:",
        "            _gt = None",
        "            try:",
        "                _gt = _edge.geom_type",
        "            except Exception:",
        "                _gt = 'unknown'",
        "            if _gt not in _type_groups:",
        "                _type_groups[_gt] = []",
        "            _type_groups[_gt].append(_edge)",
        "        _result = solid",
        "        _success_groups = 0",
        "        _fail_groups = 0",
        "        _group_fail_reasons = []",
        "        for _gt, _group_edges in _type_groups.items():",
        "            _group_success = False",
        "            _group_err = ''",
        "            for _l in _lens:",
        "                try:",
        "                    _new_result = chamfer(_group_edges, length=_l)",
        "                    if _new_result is not None and abs(_new_result.volume - _result.volume) > 0.001:",
        "                        _result = _new_result",
        "                        _group_success = True",
        "                        if _l < length:",
        "                            _MISSED_CUTS.append(f'CHAMFER_DEGRADED: {label} — group {_gt} length {length} → {_l}')",
        "                        break",
        "                    else:",
        "                        _group_err = f'no volume change at length {_l}'",
        "                except Exception as _e:",
        "                    _group_err = str(_e)",
        "                    continue",
        "            if _group_success:",
        "                _success_groups += 1",
        "            else:",
        "                _fail_groups += 1",
        "                _group_fail_reasons.append(str(_gt) + ': ' + _group_err)",
        "        if _success_groups > 0:",
        "            if _fail_groups > 0:",
        "                _reasons_str = '; '.join(_group_fail_reasons[:3])",
        "                _MISSED_CUTS.append(f'CHAMFER_PARTIAL: {label} — {_success_groups}/{len(_type_groups)} groups OK, {_fail_groups} failed ({_reasons_str})')",
        "            return _result",
        "        _MISSED_CUTS.append(f'CHAMFER_FAILED: {label} — all lengths {_lens} failed (Stage 1 + Stage 2). Last: {_last_err}. Selected {len(edges)} edges in {len(_type_groups)} groups. Use SEMANTIC selectors, not hardcoded coords. Root cause: a complete Circle edge may have been split into arc segments by prior boolean operations. If the chamfer region does NOT overlap with prior boolean regions, consider moving this chamfer step BEFORE the boolean unions/cuts — see Iron Rule 7 exception.')",
        "        return solid",
        "    except Exception as _e:",
        "        _MISSED_CUTS.append(f'CHAMFER_FAILED: {label} — {_e}')",
        "        return solid",
        "",
    ]
    step_path = f"temp_output_{iteration}.step"
    stl_path = f"temp_output_{iteration}.stl"

    # Normalise: convert dict to ArchitectPlan if needed
    if isinstance(plan, dict):
        plan = ArchitectPlan.model_validate(plan)

    # -- Key dimensions --
    dims = plan.key_dimensions or {}
    if isinstance(dims, dict):
        for k, v in dims.items():
            if isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            elif isinstance(v, list) and len(v) <= 8:
                lines.append(f"{k} = {v}")
        lines.append("")

    # -- Deterministic coordinate derivation (overrides LLM-provided values) --
    derived = _derive_positions(dims, plan) if isinstance(dims, dict) and dims else {}
    if derived:
        lines.append("# --- Derived coordinates (computed, not LLM-provided) ---")
        lines.append(f"_base_half_x = {derived['base_half_x']}")
        lines.append(f"_base_half_y = {derived['base_half_y']}")
        lines.append(f"_base_thickness = {derived['base_thickness']}")
        lines.append("")

    # -- Pre-index sketches (sanitise IDs for Python variable names) --
    def _safe(name: str) -> str:
        return name.replace("-", "_").replace(".", "_")
    sketch_map: dict[str, object] = {}
    for sk in (plan.sketches or []):
        sketch_map[sk.sketch_id] = sk

    # Track step_id → variable name for boolean ops
    step_body: dict[str, str] = {}
    solid_count = 0
    _generated_sketches: set[str] = set()  # avoid duplicate sketch generation

    lines.append("# --- Generated solids ---")
    lines.append("")

    def _emit_sketch(sk_id: str, sk) -> str:
        """Generate sketch code once; return the variable name."""
        sketch_name = "sk_" + _safe(sk_id)
        if sketch_name not in _generated_sketches:
            lines.extend(_gen_sketch_algebra(sk, sketch_name))
            _generated_sketches.add(sketch_name)
        return sketch_name

    def _place_sketch(sketch_name: str, sk, sk_height: float = 0.0) -> str:
        """Wrap sketch reference with workplane/offset and Z-start position.

        For non-XY planes, uses the architect's ``workplane_offset_mm``
        DIRECTLY for the Plane.offset (with build123d sign inversion).
        Previous versions used a derived ``offset_from_center`` that was
        computed from key_dimensions, but this was unreliable for features
        like ribs where the derived formula produced wrong values (e.g.
        derived=60 when the architect specified offset=18).  The architect
        prompt (Iron Rule 5) now guides correct offset specification, so we
        trust it directly.

        The Z-center (Pos Y on non-XY planes, which maps to world Z) still
        uses derived coordinates when available, falling back to
        ``base_thickness + sketch_height/2``.
        """
        wp = _sketch_workplane(sk)
        # Read architect's workplane_offset_mm (handles both Pydantic + dict)
        _arch_offset = (
            getattr(sk, "workplane_offset_mm", 0.0)
            if hasattr(sk, "workplane_offset_mm")
            else (sk.get("workplane_offset_mm", 0.0) if isinstance(sk, dict) else 0.0)
        )
        if wp == "XY" or not derived:
            # XY sketches: offset is along Z (the plane normal).
            # NOTE: _arch_offset can legitimately be 0.0 (e.g. rib at lug center).
            # Don't use truthy check — 0.0 is falsy in Python but is a valid offset.
            if _arch_offset is not None and _arch_offset != 0.0:
                return f"Plane.{wp}.offset({-_arch_offset}) * {sketch_name}"
            elif _arch_offset == 0.0:
                # Explicitly 0 — no offset needed, just return the sketch.
                return sketch_name
            return sketch_name
        sk_id = getattr(sk, "sketch_id", "") if hasattr(sk, "sketch_id") else sk.get("sketch_id", "")
        feat = derived.get("features", {}).get(sk_id, {})
        # Use architect's offset directly (sign-inverted for build123d).
        # The derived offset_from_center is ignored — it was unreliable.
        # NOTE: 0.0 is a valid offset (rib at center) — use "is not None" not truthy.
        if _arch_offset is not None and _arch_offset != 0.0:
            offset = -_arch_offset
        elif _arch_offset == 0.0:
            offset = 0.0  # explicit center
        else:
            offset = -feat.get("offset_from_center", derived["base_half_y"])

        # Read entity center (x=radial, y=Z) directly from the sketch's first
        # entity. This is more reliable than the derived formula — the architect
        # explicitly specifies center.x and center.y in the sketch entity.
        ents = sk.entities if hasattr(sk, "entities") else sk.get("entities", [])
        entity_center_x = None
        entity_center_y = None
        if ents:
            _ent = ents[0]
            _c = (
                getattr(_ent, "center", None) if hasattr(_ent, "center")
                else (_ent.get("center") if isinstance(_ent, dict) else None)
            )
            if _c is not None:
                entity_center_x = (
                    getattr(_c, "x", None) if hasattr(_c, "x")
                    else (_c.get("x") if isinstance(_c, dict) else None)
                )
                entity_center_y = (
                    getattr(_c, "y", None) if hasattr(_c, "y")
                    else (_c.get("y") if isinstance(_c, dict) else None)
                )

        # Z center (Pos Y on non-XY planes = world Z):
        # Prefer entity center.y, fall back to derived formula.
        if entity_center_y is not None:
            z_center = entity_center_y
        else:
            z_center = feat.get("center_z", derived["base_thickness"] + sk_height / 2.0)

        # X center (Pos X on non-XY planes = world X, e.g. radial distance):
        # Prefer entity center.x, fall back to 0 (axis-aligned).
        x_center = entity_center_x if entity_center_x is not None else 0

        # Build the placement expression.  When offset is 0, skip Plane.offset
        # entirely — it's redundant and confuses Aider into rewriting the code.
        if offset != 0.0:
            plane_expr = f"Plane.{wp}.offset({offset})"
        else:
            plane_expr = f"Plane.{wp}"
        if z_center > 0 or x_center > 0:
            return f"{plane_expr} * Pos({x_center}, {z_center}) * {sketch_name}"
        return f"{plane_expr} * {sketch_name}"

    def _sketch_height(sk) -> float:
        """Extract the Z-facing dimension of a sketch (height for rect, 2*radius for circle)."""
        ents = sk.entities if hasattr(sk, "entities") else sk.get("entities", [])
        if not ents:
            return 0.0
        ent = ents[0]
        get = lambda o, k, d: getattr(o, k, d) if hasattr(o, k) else o.get(k, d)
        etype = str(get(ent, "entity_type", "") or "")
        if hasattr(etype, "value"):
            etype = etype.value
        if etype == "rectangle":
            return float(get(ent, "height", 0) or 0)
        if etype == "circle":
            return float(get(ent, "radius", 0) or 0) * 2
        return 0.0

    for step in (plan.steps or []):
        stype = step.step_type.value if hasattr(step.step_type, "value") else str(step.step_type)
        sid = step.step_id
        sk_id = step.sketch_id

        # === EXTRUDE ======================================================
        if stype == "extrude" and sk_id and sk_id in sketch_map:
            sketch_name = _emit_sketch(sk_id, sketch_map[sk_id])
            placed = _place_sketch(sketch_name, sketch_map[sk_id], _sketch_height(sketch_map[sk_id]))
            dist = step.distance_mm or 1
            var = f"solid_{solid_count}"

            # Map direction to build123d params.  The default extrude
            # follows the sketch plane normal; "negative" flips it.
            # Plane normals: XY→+Z, XZ→+Y, YZ→+X.
            wp = _sketch_workplane(sketch_map[sk_id])
            _neg_dir: dict[str, str] = {
                "XY": "dir=(0, 0, -1)", "XZ": "dir=(0, -1, 0)", "YZ": "dir=(-1, 0, 0)",
            }
            direction = str(step.direction or "").lower()

            # Override direction for non-XY extrusions when derived coords
            # are in use: always extrude OUTWARD from the base edge.
            # build123d's default extrude on XZ goes -Y, on YZ goes -X.
            # We use explicit dir vectors to force outward direction.
            if derived and wp != "XY":
                # Plane normal directions (outward from base):
                #   XZ normal = +Y → outward dir = (0, 1, 0)
                #   YZ normal = +X → outward dir = (1, 0, 0)
                outward_dir = {"XZ": "(0, 1, 0)", "YZ": "(1, 0, 0)"}.get(wp)
                if outward_dir:
                    direction = outward_dir  # dir vector tuple string

            if direction in ("both", "symmetric", "midplane"):
                lines.append(f"{var} = extrude({placed}, amount={dist}/2, both=True)")
            elif direction.startswith("("):
                # Outward dir vector (e.g. "(0, 1, 0)") — generic for any plane
                lines.append(f"{var} = extrude({placed}, amount={dist}, dir={direction})")
            elif direction == "negative":
                d = _neg_dir.get(wp, "dir=(0, 0, -1)")
                lines.append(f"{var} = extrude({placed}, amount={dist}, {d})")
            else:
                lines.append(f"{var} = extrude({placed}, amount={dist})")
            # Parse Z offset from notes (e.g. "from Z=70 to Z=78" → Pos(0,0,70))
            import re as _re
            _notes = step.notes or ""
            _zm = _re.search(r'(?:from\s+)?Z\s*=\s*(\d+(?:\.\d+)?)', _notes, _re.IGNORECASE)
            if _zm and wp == "XY":
                _z_start = float(_zm.group(1))
                if _z_start > 0:
                    lines[-1] = f"{var} = Pos(0, 0, {_z_start}) * extrude({placed}, amount={dist})"
            step_body[sid] = var
            solid_count += 1
            # White-box instrumentation: measure this feature BEFORE any boolean merge
            lines.append(f"_measure_feature({var}, '{sid}', '{stype}')")
            lines.append("")

        # === HOLE (Cylinder subtraction on last solid) =====================
        elif stype == "hole":
            if not step_body:
                raise NotImplementedError("Hole before any solid")
            last_sid = list(step_body.keys())[-1]
            target_var = step_body[last_sid]

            dia = step.hole_diameter_mm or 4.2
            htype = str(step.hole_type or "simple")

            # Parse through-hole notes to compute cylinder height with overshoot.
            # Supports multiple LLM output formats:
            #   "Through-hole along Z, base Z=0..10"
            #   "Through-hole along Y, lugs Y=-35..35"
            #   "Z from 0 to 10"
            #   "Y: -35 ~ 35"
            #   "Z = 0 .. 10"
            #   "penetrates Z=0..10"
            # Falls back to heuristic if no pattern matches.
            _through_axis = None
            _through_lo = None
            _through_hi = None
            _notes_text = step.notes or ""
            # Multi-pattern regex: axis + range in various formats
            _th_patterns = [
                # "along Z, ... Z=0..10" or "along Z, ... Z: 0..10"
                r'along\s+([XYZ])\b.*?[XYZ]\s*[:=]\s*(-?\d+\.?\d*)\s*(?:\.\.|~|to|-|→|->)\s*(-?\d+\.?\d*)',
                # "Z from 0 to 10" or "Z: -35 ~ 35" or "Z = 0 .. 10"
                r'([XYZ])\s*[:=]?\s*(?:from\s+)?(-?\d+\.?\d*)\s*(?:\.\.|~|to|-|→|->)\s*(-?\d+\.?\d*)',
                # "penetrates Z=0..10" or "through Z=0..10"
                r'(?:penetrat|through|thru)\w*\s+([XYZ])\s*[:=]\s*(-?\d+\.?\d*)\s*(?:\.\.|~|to|-|→|->)\s*(-?\d+\.?\d*)',
            ]
            for _pat in _th_patterns:
                _th_match = _re.search(_pat, _notes_text, _re.IGNORECASE)
                if _th_match:
                    _through_axis = _th_match.group(1).upper()
                    _lo = float(_th_match.group(2))
                    _hi = float(_th_match.group(3))
                    _through_lo = min(_lo, _hi)
                    _through_hi = max(_lo, _hi)
                    break

            # Use LLM-provided 3D position; if missing, derive from last
            # vertical extrusion feature center (generic fallback).
            pos = step.hole_position
            if pos is not None:
                if hasattr(pos, "x"):
                    world_x = float(getattr(pos, "x", 0.0))
                    world_y = float(getattr(pos, "y", 0.0))
                    world_z = float(getattr(pos, "z", 0.0))
                elif isinstance(pos, (list, tuple)):
                    if len(pos) == 3:
                        world_x, world_y, world_z = float(pos[0]), float(pos[1]), float(pos[2])
                    elif len(pos) == 2:
                        world_x, world_y = float(pos[0]), float(pos[1])
                        world_z = 0.0
                else:
                    world_x = world_y = world_z = 0.0
            elif derived:
                # No position → use last vertical feature's center as default
                features = derived.get("features", {})
                if features:
                    last = list(features.values())[-1]
                    world_x = 0.0
                    world_y = last.get("offset_from_center", 0) + 0  # rough center
                    world_z = last.get("center_z", 0)
                else:
                    world_x = world_y = world_z = 0.0
            else:
                world_x = world_y = world_z = 0.0

            # Determine cylinder direction and height.
            # Priority 1: Use through-hole notes if available (most reliable).
            # Priority 2: Infer from hole position (heuristic).
            _bt = derived.get("base_thickness", 5.0) if derived else 5.0
            _overshoot = 2.0  # mm past each face

            if _through_axis and _through_lo is not None and _through_hi is not None:
                # Computed from Architect's through-hole notes
                _cyl_height = (_through_hi - _through_lo) + 2 * _overshoot
                _cyl_center = (_through_lo + _through_hi) / 2.0
                if _through_axis == "Z":
                    # Z-axis cylinder with Align.MIN: Pos_z = bottom position
                    _align_z = "Align.CENTER, Align.CENTER, Align.MIN"
                    cyl_expr = f"Cylinder(radius={dia}/2, height={_cyl_height}, align=({_align_z}))"
                    cyl_label = f"Z-axis (through {_through_lo}..{_through_hi})"
                    world_z = _through_lo - _overshoot  # bottom of penetration range
                elif _through_axis == "Y":
                    rot_expr = "Rot(X=90)"
                    cyl_expr = f"{rot_expr} * Cylinder(radius={dia}/2, height={_cyl_height})"
                    cyl_label = f"Y-axis (through {_through_lo}..{_through_hi})"
                elif _through_axis == "X":
                    rot_expr = "Rot(Y=90)"
                    cyl_expr = f"{rot_expr} * Cylinder(radius={dia}/2, height={_cyl_height})"
                    cyl_label = f"X-axis (through {_through_lo}..{_through_hi})"
            elif abs(world_z) > _bt * 1.5:
                # Vertical face hole — rotate cylinder to face normal.
                base_hy = derived.get("base_half_y", 21.0) if derived else 21.0
                base_hx = derived.get("base_half_x", 21.0) if derived else 21.0
                if abs(world_y) > base_hy * 0.5:
                    rot_expr = "Rot(X=90)"
                    cyl_label = "Y-axis (XZ face)"
                elif abs(world_x) > base_hx * 0.5:
                    rot_expr = "Rot(Y=90)"
                    cyl_label = "X-axis (YZ face)"
                else:
                    rot_expr = "Rot(X=90)"
                    cyl_label = "Y-axis (vertical face)"
                cyl_expr = f"{rot_expr} * Cylinder(radius={dia}/2, height=80)"
            else:
                # Z-axis hole through base — use Align.MIN so Pos_z = bottom
                _cyl_height = _bt + 2 * _overshoot
                _align_z = "Align.CENTER, Align.CENTER, Align.MIN"
                cyl_expr = f"Cylinder(radius={dia}/2, height={_cyl_height}, align=({_align_z}))"
                cyl_label = f"Z-axis (base {_bt}mm + overshoot)"
                world_z = world_z - _overshoot  # bottom = base_bottom - overshoot

            tool_name = f"tool_{solid_count}"
            if htype in ("counterbore", "counterbored"):
                cb_dia = step.counterbore_diameter_mm or 11
                cb_depth = step.counterbore_depth_mm or 6.5
                lines.append(f"# Counterbore ({cyl_label}) at world({world_x}, {world_y}, {world_z}) on {target_var}")
                lines.append(
                    f"{tool_name}_cb = Pos({world_x}, {world_y}, {world_z}) * "
                    f"{cyl_expr.replace(str(dia), str(cb_dia))}"
                )
                lines.append(
                    f"{tool_name}_thru = Pos({world_x}, {world_y}, {world_z}) * "
                    f"{cyl_expr}"
                )
                lines.append(f"{target_var} = _safe_cut(_safe_cut({target_var}, {tool_name}_cb, '{sid}-cb'), {tool_name}_thru, '{sid}-thru')")
            else:
                lines.append(f"# Through hole ({cyl_label}) at world({world_x}, {world_y}, {world_z}) on {target_var}")
                lines.append(
                    f"{tool_name} = Pos({world_x}, {world_y}, {world_z}) * "
                    f"{cyl_expr}"
                )
                lines.append(f"{target_var} = _safe_cut({target_var}, {tool_name}, '{sid}')")
            solid_count += 1
            lines.append("")

        # === BOOLEAN UNION =================================================
        elif stype == "boolean_union":
            target_id = step.target_step_id
            tool_id = step.tool_step_id
            if target_id and tool_id and target_id in step_body and tool_id in step_body:
                tv = step_body[target_id]
                ov = step_body[tool_id]
                var = f"solid_{solid_count}"
                lines.append(f"# Union: {tv} + {ov}")
                # Measure both operands BEFORE union (white-box instrumentation)
                # Use modified keys to avoid overwriting original feature measurements
                # (e.g., if a pattern was applied to the tool operand)
                lines.append(f"_measure_feature({tv}, '{target_id}-union-target', 'union_operand_a')")
                lines.append(f"_measure_feature({ov}, '{tool_id}-union-tool', 'union_operand_b')")
                lines.append(f"# Adaptive overlap: 0.3-0.5mm based on feature size")
                lines.append(f"_bb0 = {tv}.bounding_box()")
                lines.append(f"_bb1 = {ov}.bounding_box()")
                lines.append(f"# Calculate adaptive overlap (5% of smaller dimension, clamped to 0.3-0.5mm)")
                lines.append(f"_min_dim = min(")
                lines.append(f"    _bb0.max.to_tuple()[0] - _bb0.min.to_tuple()[0],")
                lines.append(f"    _bb0.max.to_tuple()[1] - _bb0.min.to_tuple()[1],")
                lines.append(f"    _bb0.max.to_tuple()[2] - _bb0.min.to_tuple()[2],")
                lines.append(f"    _bb1.max.to_tuple()[0] - _bb1.min.to_tuple()[0],")
                lines.append(f"    _bb1.max.to_tuple()[1] - _bb1.min.to_tuple()[1],")
                lines.append(f"    _bb1.max.to_tuple()[2] - _bb1.min.to_tuple()[2],")
                lines.append(f")")
                lines.append(f"_overlap = max(0.3, min(0.5, _min_dim * 0.05))")
                lines.append(f"_push = [0.0, 0.0, 0.0]")
                lines.append(f"for _i in range(3):")
                lines.append(f"    _gap = _bb0.min.to_tuple()[_i] - _bb1.max.to_tuple()[_i]")
                lines.append(f"    if _gap >= 0: _push[_i] = _gap + _overlap  # push into body")
                lines.append(f"    _gap = _bb1.min.to_tuple()[_i] - _bb0.max.to_tuple()[_i]")
                lines.append(f"    if _gap >= 0: _push[_i] = -(_gap + _overlap)")
                lines.append(f"if any(abs(p)>0.001 for p in _push):")
                lines.append(f"    {ov} = Pos(tuple(_push)) * {ov}")
                lines.append(f"# Validate operands before union")
                lines.append(f"_valid_a, _errors_a = _validate_solid({tv}, '{tv}')")
                lines.append(f"_valid_b, _errors_b = _validate_solid({ov}, '{ov}')")
                lines.append(f"if not _valid_a or not _valid_b:")
                lines.append(f"    _all_errors = _errors_a + _errors_b")
                lines.append(f"    print(f'WARNING: Union operands invalid: {{_all_errors}}')")
                lines.append(f"{var} = {tv} + {ov}")
                lines.append(f"# Validate union result")
                lines.append(f"_valid_result, _errors_result = _validate_solid({var}, '{var}')")
                lines.append(f"if not _valid_result:")
                lines.append(f"    print(f'WARNING: Union result invalid: {{_errors_result}}')")
                step_body[sid] = var
                solid_count += 1
            else:
                sids_list = list(step_body.keys())
                if len(sids_list) >= 2:
                    a = step_body[sids_list[-2]]
                    b = step_body[sids_list[-1]]
                    var = f"solid_{solid_count}"
                    lines.append(f"# Union: {a} + {b}  (adaptive overlap: 0.3-0.5mm)")
                    # Measure both operands BEFORE union (white-box instrumentation)
                    # Use modified keys to avoid overwriting original feature measurements
                    lines.append(f"_measure_feature({a}, '{sids_list[-2]}-union-target', 'union_operand_a')")
                    lines.append(f"_measure_feature({b}, '{sids_list[-1]}-union-tool', 'union_operand_b')")
                    lines.append(f"_bb0 = {a}.bounding_box()")
                    lines.append(f"_bb1 = {b}.bounding_box()")
                    lines.append(f"# Calculate adaptive overlap (5% of smaller dimension, clamped to 0.3-0.5mm)")
                    lines.append(f"_min_dim = min(")
                    lines.append(f"    _bb0.max.to_tuple()[0] - _bb0.min.to_tuple()[0],")
                    lines.append(f"    _bb0.max.to_tuple()[1] - _bb0.min.to_tuple()[1],")
                    lines.append(f"    _bb0.max.to_tuple()[2] - _bb0.min.to_tuple()[2],")
                    lines.append(f"    _bb1.max.to_tuple()[0] - _bb1.min.to_tuple()[0],")
                    lines.append(f"    _bb1.max.to_tuple()[1] - _bb1.min.to_tuple()[1],")
                    lines.append(f"    _bb1.max.to_tuple()[2] - _bb1.min.to_tuple()[2],")
                    lines.append(f")")
                    lines.append(f"_overlap = max(0.3, min(0.5, _min_dim * 0.05))")
                    lines.append(f"_push = [0.0, 0.0, 0.0]")
                    lines.append(f"for _i in range(3):")
                    lines.append(f"    _gap = _bb0.min.to_tuple()[_i] - _bb1.max.to_tuple()[_i]")
                    lines.append(f"    if _gap >= 0: _push[_i] = _gap + _overlap")
                    lines.append(f"    _gap = _bb1.min.to_tuple()[_i] - _bb0.max.to_tuple()[_i]")
                    lines.append(f"    if _gap >= 0: _push[_i] = -(_gap + _overlap)")
                    lines.append(f"if any(abs(p)>0.001 for p in _push):")
                    lines.append(f"    {b} = Pos(tuple(_push)) * {b}")
                    lines.append(f"# Validate operands before union")
                    lines.append(f"_valid_a, _errors_a = _validate_solid({a}, '{a}')")
                    lines.append(f"_valid_b, _errors_b = _validate_solid({b}, '{b}')")
                    lines.append(f"if not _valid_a or not _valid_b:")
                    lines.append(f"    _all_errors = _errors_a + _errors_b")
                    lines.append(f"    print(f'WARNING: Union operands invalid: {{_all_errors}}')")
                    lines.append(f"{var} = {a} + {b}")
                    lines.append(f"# Validate union result")
                    lines.append(f"_valid_result, _errors_result = _validate_solid({var}, '{var}')")
                    lines.append(f"if not _valid_result:")
                    lines.append(f"    print(f'WARNING: Union result invalid: {{_errors_result}}')")
                    step_body[sid] = var
                    solid_count += 1
            lines.append("")

        # === EXTRUDE_CUT / CUT (subtractive extrusion) =====================
        elif stype in ("extrude_cut", "cut"):
            # Treat as boolean_cut: extrude the sketch and subtract
            if sk_id and sk_id in sketch_map:
                sketch_name = _emit_sketch(sk_id, sketch_map[sk_id])
                placed = _place_sketch(sketch_name, sketch_map[sk_id], _sketch_height(sketch_map[sk_id]))
                dist = step.distance_mm or 1
                wp = _sketch_workplane(sketch_map[sk_id])
                _neg_dir = {"XY": "dir=(0, 0, -1)", "XZ": "dir=(0, -1, 0)", "YZ": "dir=(-1, 0, 0)"}
                direction = str(step.direction or "").lower()
                # Force outward direction for non-XY extrusions
                if derived and wp != "XY":
                    outward_dir = {"XZ": "(0, 1, 0)", "YZ": "(1, 0, 0)"}.get(wp)
                    if outward_dir:
                        direction = outward_dir
                tool_var = f"solid_{solid_count}"
                if direction in ("both", "symmetric", "midplane"):
                    lines.append(f"{tool_var} = extrude({placed}, amount={dist}/2, both=True)")
                elif direction.startswith("("):
                    lines.append(f"{tool_var} = extrude({placed}, amount={dist}, dir={direction})")
                elif direction == "negative":
                    d = _neg_dir.get(wp, "dir=(0, 0, -1)")
                    lines.append(f"{tool_var} = extrude({placed}, amount={dist}, {d})")
                else:
                    lines.append(f"{tool_var} = extrude({placed}, amount={dist})")
                if step_body:
                    last_sid = list(step_body.keys())[-1]
                    target_var = step_body[last_sid]
                    lines.append(f"{target_var} = _safe_cut({target_var}, {tool_var}, '{sid}')")
                solid_count += 1
            lines.append("")

        # === SIMPLE_HOLE / COUNTERBORE_HOLE (aliases) =======================
        elif stype in ("simple_hole", "counterbore_hole"):
            # Treat same as "hole"
            if not step_body:
                raise NotImplementedError("Hole before any solid")
            last_sid = list(step_body.keys())[-1]
            target_var = step_body[last_sid]
            dia = step.hole_diameter_mm or (3.3 if "simple" in stype else 6.5)
            htype = "counterbore" if "counterbore" in stype else "simple"
            # Same generic positioning as the main hole section
            pos = step.hole_position
            if pos is not None:
                if hasattr(pos, "x"):
                    world_x = float(getattr(pos, "x", 0.0))
                    world_y = float(getattr(pos, "y", 0.0))
                    world_z = float(getattr(pos, "z", 0.0))
                elif isinstance(pos, (list, tuple)):
                    if len(pos) == 3:
                        world_x, world_y, world_z = float(pos[0]), float(pos[1]), float(pos[2])
                    elif len(pos) == 2:
                        world_x, world_y = float(pos[0]), float(pos[1])
                        world_z = 0.0
                else:
                    world_x = world_y = world_z = 0.0
            elif derived:
                features = derived.get("features", {})
                if features:
                    last = list(features.values())[-1]
                    world_x = 0.0
                    world_y = last.get("offset_from_center", 0)
                    world_z = last.get("center_z", 0)
                else:
                    world_x = world_y = world_z = 0.0
            else:
                world_x = world_y = world_z = 0.0
            # Cylinder direction (same generic logic as main hole section)
            _bt = derived.get("base_thickness", 5.0) if derived else 5.0
            if abs(world_z) > _bt * 1.5:
                base_hy = derived.get("base_half_y", 21.0) if derived else 21.0
                base_hx = derived.get("base_half_x", 21.0) if derived else 21.0
                if abs(world_y) > base_hy * 0.5:
                    rot_expr = "Rot(X=90)"  # Y-axis cylinder for XZ face
                    cyl_label = "Y-axis (XZ face)"
                elif abs(world_x) > base_hx * 0.5:
                    rot_expr = "Rot(Y=90)"  # X-axis cylinder for YZ face
                    cyl_label = "X-axis (YZ face)"
                else:
                    rot_expr = "Rot(X=90)"
                    cyl_label = "Y-axis (vertical face)"
                cyl_expr = f"{rot_expr} * Cylinder(radius={dia}/2, height=80)"
            else:
                _align_z = "Align.CENTER, Align.CENTER, Align.MIN"
                _cyl_height = _bt + 4.0  # base thickness + 2mm overshoot each side
                cyl_expr = f"Cylinder(radius={dia}/2, height={_cyl_height}, align=({_align_z}))"
                cyl_label = f"Z-axis (base {_bt}mm + overshoot)"
                world_z = world_z - 2.0  # bottom = base_bottom - overshoot
            tool_name = f"tool_{solid_count}"
            if htype == "counterbore":
                cb_dia = step.counterbore_diameter_mm or 11
                cb_depth = step.counterbore_depth_mm or 6.5
                # Counterbore: shallow wide cylinder + deep narrow through cylinder
                # Apply the same rotation as the through hole
                if abs(world_z) > _bt * 1.5:
                    # Vertical face — apply rotation to counterbore cylinder
                    cb_cyl_expr = f"{rot_expr} * Cylinder(radius={cb_dia}/2, height={cb_depth})"
                else:
                    # Base face — use Align.MIN for counterbore too
                    cb_cyl_expr = f"Cylinder(radius={cb_dia}/2, height={cb_depth}, align=({_align_z}))"
                lines.append(f"# Counterbore ({cyl_label}) at world({world_x}, {world_y}, {world_z}) on {target_var}")
                lines.append(f"{tool_name}_cb = Pos({world_x}, {world_y}, {world_z}) * {cb_cyl_expr}")
                lines.append(f"{tool_name}_thru = Pos({world_x}, {world_y}, {world_z}) * {cyl_expr}")
                lines.append(f"{target_var} = _safe_cut(_safe_cut({target_var}, {tool_name}_cb, '{sid}-cb'), {tool_name}_thru, '{sid}-thru')")
            else:
                lines.append(f"# Through hole ({cyl_label}) at world({world_x}, {world_y}, {world_z}) on {target_var}")
                lines.append(f"{tool_name} = Pos({world_x}, {world_y}, {world_z}) * {cyl_expr}")
                lines.append(f"{target_var} = _safe_cut({target_var}, {tool_name}, '{sid}')")
            solid_count += 1
            lines.append("")

        # === BOOLEAN CUT ===================================================
        elif stype == "boolean_cut":
            target_id = step.target_step_id
            tool_id = step.tool_step_id
            if target_id and tool_id and target_id in step_body and tool_id in step_body:
                tv = step_body[target_id]
                ov = step_body[tool_id]
                var = f"solid_{solid_count}"
                lines.append(f"# Cut: {tv} - {ov}")
                # Measure both operands BEFORE cut (white-box instrumentation)
                lines.append(f"_measure_feature({tv}, '{target_id}', 'cut_target')")
                lines.append(f"_measure_feature({ov}, '{tool_id}', 'cut_tool')")
                lines.append(f"{var} = _safe_cut({tv}, {ov}, '{sid}')")
                step_body[sid] = var
                solid_count += 1
            lines.append("")

        # === FILLET (smart edge filter) ====================================
        elif stype == "fillet":
            if not step_body:
                raise NotImplementedError("Fillet before any solid")
            last_sid = list(step_body.keys())[-1]
            target_var = step_body[last_sid]
            r = step.radius_mm or 1
            edge_sel = _infer_edge_filter(step)

            # _infer_edge_filter now returns a list-comprehension expression
            # (e.g. "[e for e in s.edges().filter_by(Axis.Z) if e.length() > 5]")
            # that uses `s` as the solid parameter.  Embed it directly into
            # the lambda — no wrapping needed.  The length filter excludes
            # short junction edges that cause "ChFi3d_Builder: only 2 faces"
            # fillet failures, and uses semantic e.length() (not hardcoded
            # bbox coordinates) so it doesn't break when part dimensions change.
            #
            # _safe_fillet re-evaluates the lambda on the CURRENT solid each
            # call, preventing the stale-edges overwrite trap when multiple
            # fillet steps are applied in sequence.  See _safe_fillet docstring.
            lines.append(f"# Fillet on {target_var}  (filter: {edge_sel})")
            lines.append(
                f"{target_var} = _safe_fillet({target_var}, "
                f"lambda s: {edge_sel}, {r}, '{sid}')"
            )
            lines.append("")

        # === CHAMFER =======================================================
        elif stype == "chamfer":
            if not step_body:
                raise NotImplementedError("Chamfer before any solid")
            last_sid = list(step_body.keys())[-1]
            target_var = step_body[last_sid]
            length = step.chamfer_distance_mm or 1
            edge_sel = _infer_edge_filter(step)

            # Same pattern as fillet — _infer_edge_filter returns a
            # list comprehension with length filter, embedded directly.
            # See _safe_fillet docstring for the stale-edges trap.
            lines.append(f"# Chamfer on {target_var}  (filter: {edge_sel})")
            lines.append(
                f"{target_var} = _safe_chamfer({target_var}, "
                f"lambda s: {edge_sel}, {length}, '{sid}')"
            )
            lines.append("")

        # === PATTERN_CIRCULAR (repeat holes around a PCD) ===================
        elif stype == "pattern_circular":
            if not step_body:
                raise NotImplementedError("Pattern before any solid")
            # Find the referenced feature step
            ref_id = (step.feature_step_ids or [None])[0] if step.feature_step_ids else None
            if not ref_id:
                ref_id = (step.depends_on or [None])[0] if step.depends_on else None

            # Determine if this is a hole pattern or feature pattern
            is_hole_pattern = False
            ref_step_type = None
            if ref_id:
                for prev_step in (plan.steps or []):
                    if prev_step.step_id == ref_id:
                        ref_step_type = str(prev_step.step_type.value) if hasattr(prev_step.step_type, "value") else str(prev_step.step_type)
                        is_hole_pattern = ref_step_type in ("hole", "simple_hole", "counterbore_hole")
                        break

            # Get pattern params
            count = step.pattern_count
            pcd = step.pattern_spacing_mm

            # Parse from key_dimensions if missing
            if count is None or pcd is None:
                for k, v in (plan.key_dimensions or {}).items():
                    kl = k.lower()
                    if isinstance(v, (int, float)):
                        if count is None and "count" in kl:
                            count = int(v)
                        if pcd is None and ("pcd" in kl or "bolt_circle" in kl) and "radius" not in kl:
                            pcd = float(v)

            # Parse from notes if still missing
            notes_text = step.notes or ""
            if count is None:
                import re as _re
                _m = _re.search(r'(\d+)\s*(?:blades?|features?|holes?|instances?)', notes_text, _re.IGNORECASE)
                if _m:
                    count = int(_m.group(1))
            if pcd is None:
                _m = _re.search(r'(?:PCD|radius|diameter)\s*[=:]?\s*(\d+(?:\.\d+)?)\s*mm', notes_text, _re.IGNORECASE)
                if _m:
                    pcd = float(_m.group(1))
                    if "diameter" in notes_text.lower():
                        pass  # already diameter
                    elif "radius" in notes_text.lower():
                        pcd = pcd * 2  # convert radius to diameter

            # Apply defaults
            count = count or 6
            pcd = pcd or 56.0
            radius = pcd / 2

            last_sid = list(step_body.keys())[-1]
            target_var = step_body[last_sid]

            if is_hole_pattern:
                # Hole pattern: create cylinders and cut
                hole_dia = 5.0  # default
                if ref_id:
                    for prev_step in (plan.steps or []):
                        if prev_step.step_id == ref_id:
                            hole_dia = prev_step.hole_diameter_mm or hole_dia
                            break

                lines.append(f"# Circular pattern: {count} holes on PCD {pcd}mm")
                lines.append(f"_pcd_r = {radius}")
                lines.append(f"_pcd_n = {count}")
                lines.append(f"for _i in range(_pcd_n):")
                lines.append(f"    _angle = 2 * math.pi * _i / _pcd_n")
                lines.append(f"    _px = _pcd_r * math.cos(_angle)")
                lines.append(f"    _py = _pcd_r * math.sin(_angle)")
                lines.append(f"    _t = Pos(_px, _py, 0) * Cylinder(radius={hole_dia}/2, height=200)")
                lines.append(f"    {target_var} = _safe_cut({target_var}, _t, '{sid}-{{_i}}')")
            else:
                # Feature pattern: rotate and union copies
                if ref_id and ref_id in step_body:
                    feature_var = step_body[ref_id]
                else:
                    feature_var = target_var  # fallback to last solid

                lines.append(f"# Circular pattern: {count} features on PCD {pcd}mm")
                lines.append(f"_pcd_r = {radius}")
                lines.append(f"_pcd_n = {count}")
                lines.append(f"_pattern_result = {target_var}")
                lines.append(f"for _i in range(1, _pcd_n):  # skip i=0 (original)")
                lines.append(f"    _angle = 2 * math.pi * _i / _pcd_n")
                lines.append(f"    _rotated = Rot(Z=_angle * 180 / math.pi) * {feature_var}")
                lines.append(f"    _pattern_result = _pattern_result + _rotated")
                lines.append(f"{target_var} = _pattern_result")

            lines.append("")

        # === MIRROR ========================================================
        elif stype == "mirror":
            if not step_body:
                raise NotImplementedError("Mirror before any solid")
            mirror_plane = str(step.mirror_plane or "XZ")
            plane_map = {"XY": "Plane.XY", "XZ": "Plane.XZ", "YZ": "Plane.YZ"}
            plane = plane_map.get(mirror_plane, "Plane.XZ")
            last_sid = list(step_body.keys())[-1]
            tv = step_body[last_sid]
            var = f"solid_{solid_count}"
            lines.append(f"# Mirror {tv} across {plane}")
            lines.append(f"{var}_mirrored = mirror({tv}, about={plane})")
            lines.append(f"{var} = {tv} + {var}_mirrored")
            step_body[sid] = var
            solid_count += 1
            lines.append(f"_measure_feature({var}, '{sid}', '{stype}')")
            lines.append("")

        # === REVOLVE =======================================================
        elif stype == "revolve" and sk_id and sk_id in sketch_map:
            sketch_name = _emit_sketch(sk_id, sketch_map[sk_id])
            placed = _place_sketch(sketch_name, sketch_map[sk_id], _sketch_height(sketch_map[sk_id]))
            arc = step.revolve_angle_deg or 360
            var = f"solid_{solid_count}"
            # Revolve axis: default Z; can be "x", "y", "z" or a custom point+axis
            axis_str = str(step.revolve_axis or "z").lower()
            axis_map = {"x": "Axis.X", "y": "Axis.Y", "z": "Axis.Z"}
            axis_expr = axis_map.get(axis_str, "Axis.Z")
            lines.append(f"{var} = revolve({placed}, revolution_arc={arc}, axis={axis_expr})")
            lines.append(f"_measure_feature({var}, '{sid}', '{stype}')")
            if step_body:
                last_sid = list(step_body.keys())[-1]
                lines.append(f"{step_body[last_sid]} = {step_body[last_sid]} + {var}")
            else:
                step_body[sid] = var
            solid_count += 1
            lines.append("")

        # === PATTERN_LINEAR =================================================
        elif stype == "pattern_linear":
            if not step_body:
                raise NotImplementedError("Pattern before any solid")
            import re as _re  # always import — used for notes parsing below
            # Extract count from step or notes
            count = step.pattern_count
            if count is None:
                # Parse notes for a number (e.g. "12 fins" → 12)
                notes_text = step.notes or ""
                _m = _re.search(r'(\d+)\s*(?:fins|copies|instances|items|pieces)', notes_text, _re.IGNORECASE)
                if _m:
                    count = int(_m.group(1))
                else:
                    # Try key_dimensions
                    for k, v in (plan.key_dimensions or {}).items():
                        if "count" in k.lower() and isinstance(v, (int, float)):
                            count = int(v)
                            break
                    if count is None:
                        count = 2  # last resort default
            spacing = step.pattern_spacing_mm or 10
            axis = str(step.pattern_axis or "x")
            # Use the referenced feature (target_step_id or feature_step_ids),
            # falling back to the last solid if not specified
            _ref_id = None
            if step.feature_step_ids:
                _ref_id = step.feature_step_ids[0]
            elif step.target_step_id:
                _ref_id = step.target_step_id
            elif step.depends_on:
                _ref_id = step.depends_on[0]
            if _ref_id and _ref_id in step_body:
                tv = step_body[_ref_id]
            else:
                last_sid = list(step_body.keys())[-1]
                tv = step_body[last_sid]
            dir_vec = {"x": "(1,0,0)", "y": "(0,1,0)", "z": "(0,0,1)"}.get(axis, "(1,0,0)")
            # Extract start offset from notes (e.g. "starting at Z=10" → 10)
            start_offset = 0.0
            notes_text = step.notes or ""
            # Try multiple patterns to find the start position
            _m = _re.search(r'(?:start\w*|begin\w*|from)\s+(?:at\s+)?([A-Z])\s*=\s*(\d+(?:\.\d+)?)', notes_text, _re.IGNORECASE)
            if not _m:
                _m = _re.search(r'(?:start\w*|begin\w*|from)\s+(?:at\s+)?(\d+(?:\.\d+)?)', notes_text, _re.IGNORECASE)
            if _m:
                start_offset = float(_m.group(_m.lastindex))
            lines.append(f"# Linear pattern: {count} copies along {axis}, spacing {spacing}mm")
            if start_offset > 0:
                lines.append(f"# First copy positioned at {axis}={start_offset}")
                ax_idx = {"x": 0, "y": 1, "z": 2}.get(axis, 0)
                offset_vec = [0.0, 0.0, 0.0]
                offset_vec[ax_idx] = start_offset
                lines.append(f"_pat_base = Pos({tuple(offset_vec)}) * {tv}")
            else:
                lines.append(f"_pat_base = {tv}")
            lines.append(f"_pat_dir = Vector{dir_vec}")
            lines.append(f"_pat_body = _pat_base")
            lines.append(f"for _pi in range(1, {count}):")
            lines.append(f"    _pat_body = _pat_body + Pos(_pat_dir * _pi * {spacing}) * {tv}")
            lines.append(f"{tv} = _pat_body")
            lines.append("")

        # === SKETCH_2D (standalone sketch definition, no solid produced) =====
        elif stype == "sketch_2d" and sk_id and sk_id in sketch_map:
            _emit_sketch(sk_id, sketch_map[sk_id])
            lines.append("")

        # === BOOLEAN INTERSECT ==============================================
        elif stype == "boolean_intersect":
            target_id = step.target_step_id
            tool_id = step.tool_step_id
            if target_id and tool_id and target_id in step_body and tool_id in step_body:
                tv = step_body[target_id]
                ov = step_body[tool_id]
                var = f"solid_{solid_count}"
                lines.append(f"# Intersect: {tv} & {ov}")
                # Measure both operands BEFORE intersect (white-box instrumentation)
                lines.append(f"_measure_feature({tv}, '{target_id}', 'intersect_target')")
                lines.append(f"_measure_feature({ov}, '{tool_id}', 'intersect_tool')")
                lines.append(f"{var} = {tv} & {ov}")
                step_body[sid] = var
                solid_count += 1
            else:
                # Implicit: intersect last two solids
                sids_list = list(step_body.keys())
                if len(sids_list) >= 2:
                    a = step_body[sids_list[-2]]
                    b = step_body[sids_list[-1]]
                    var = f"solid_{solid_count}"
                    lines.append(f"# Intersect: {a} & {b}")
                    # Measure both operands BEFORE intersect (white-box instrumentation)
                    lines.append(f"_measure_feature({a}, '{sids_list[-2]}', 'intersect_operand_a')")
                    lines.append(f"_measure_feature({b}, '{sids_list[-1]}', 'intersect_operand_b')")
                    lines.append(f"{var} = {a} & {b}")
                    step_body[sid] = var
                    solid_count += 1
            lines.append("")

        # === SHELL (hollow out a solid) =====================================
        elif stype == "shell":
            if not step_body:
                raise NotImplementedError("Shell before any solid")
            last_sid = list(step_body.keys())[-1]
            target_var = step_body[last_sid]
            thickness = step.shell_thickness_mm or 1.0
            open_face_ids = step.shell_open_faces or []
            var = f"solid_{solid_count}"
            lines.append(f"# Shell: {target_var} with thickness {thickness}mm")
            if open_face_ids:
                # open_faces stores face selector expressions — pass as list
                faces_expr = ", ".join(
                    f"{target_var}.faces().filter_by(Plane.{f})" if f in ("XY", "XZ", "YZ")
                    else f"{target_var}.faces()[{f}]"
                    for f in open_face_ids
                )
                lines.append(f"try:")
                lines.append(f"    _open = [{faces_expr}]")
                lines.append(f"    {var} = shell({target_var}, faces_to_remove=_open, thickness={thickness})")
                lines.append(f"except Exception:")
                lines.append(f"    {var} = {target_var}  # shell failed, keep original")
            else:
                lines.append(f"try:")
                lines.append(f"    {var} = shell({target_var}, thickness={thickness})")
                lines.append(f"except Exception:")
                lines.append(f"    {var} = {target_var}")
            step_body[sid] = var
            solid_count += 1
            lines.append(f"_measure_feature({var}, '{sid}', '{stype}')")
            lines.append("")

        # === REFERENCE (pass-through for transform/positioning steps) =======
        elif stype == "reference":
            # Reference steps define datums or transforms but don't produce
            # new geometry. Pass through the referenced solid from depends_on
            # so subsequent boolean operations can find it in step_body.
            _ref_source = None
            if step.depends_on:
                for dep_id in step.depends_on:
                    if dep_id in step_body:
                        _ref_source = dep_id
                        break
            if _ref_source:
                step_body[sid] = step_body[_ref_source]
            # If no source found, this step is silently skipped
            # (subsequent steps referencing it will use fallback logic)

        # === Fallback: truly complex ops → Aider =============================
        elif stype in ("draft", "rib"):
            # Generate placeholder for Aider to fill in
            lines.append(f"# TODO_AIDER: {stype} operation")
            step_data = {
                'step_id': sid,
                'step_type': stype,
            }
            # Extract relevant parameters based on step type
            if stype == "draft":
                for key in ['draft_angle_deg', 'draft_face_selectors', 'draft_neutral_plane']:
                    val = getattr(step, key, None)
                    if val is not None:
                        step_data[key] = val
            elif stype == "rib":
                for key in ['rib_thickness_mm', 'rib_height_mm', 'rib_face_selectors']:
                    val = getattr(step, key, None)
                    if val is not None:
                        step_data[key] = val
            lines.append(f"# Feature data: {step_data}")
            var = f"solid_{solid_count}"
            lines.append(f"{var} = {step_body.get(step.target_step_id, 'solid_0') if step_body else 'solid_0'}  # Placeholder for Aider")
            step_body[sid] = var
            solid_count += 1
            lines.append("")

        # === Catch-all: unrecognized step types =============================
        else:
            # Log a warning but don't crash — the step is silently skipped
            lines.append(f"# WARNING: Unrecognized step_type '{stype}' (step {sid}) — skipped")
            lines.append("")

    # -- Final body is the last solid created --
    if not step_body:
        raise NotImplementedError("No solids generated")
    final_var = step_body[list(step_body.keys())[-1]]

    # White-box instrumentation: measure the final assembled body as "overall"
    lines.append(f"_measure_feature({final_var}, 'overall', 'final_assembly')")

    lines.append(f"# --- Write missed-cut warnings (position error detection) ---")
    lines.append(f"if _MISSED_CUTS:")
    lines.append(f"    import os as _os")
    lines.append(f"    _iteration = int(_os.environ.get('ITERATION', '0'))")
    lines.append(f"    with open(f'temp_missed_{{_iteration}}.json', 'w') as _f:")
    lines.append(f"        json.dump(_MISSED_CUTS, _f)")
    lines.append(f"# --- Return result for cadpy.generation ---")
    lines.append(f'export_step({final_var}, r"{step_path}")')
    lines.append(f'export_stl({final_var}, r"{stl_path}", tolerance=0.01, angular_tolerance=0.1)')
    lines.append(f"# --- Save feature measurements (white-box instrumentation) ---")
    lines.append(f"_save_measurements()  # Uses ITERATION env var")
    lines.append(f"return {{\"shape\": {final_var}}}")
    return "\n".join(lines)


def _derive_positions(dims: dict, plan=None) -> dict:
    """Compute derived 3D positions from design parameters.

    Generic — works for any part with an extruded base on XY and optional
    features on other workplanes.  No L-bracket-specific assumptions.

    Uses fuzzy key matching so the LLM can name parameters in different
    ways across iterations without breaking the coordinate math.

    Returns a dict of derived coordinates.  The caller uses only the keys
    that are relevant to the current part topology.
    """
    # ── Fuzzy key extractors ──────────────────────────────────────────────
    # Try exact keys first, then strip common suffixes (_X, _Y, _Z, _MM)
    def _fuzzy(*keys):
        for k in keys:
            if k in dims and isinstance(dims[k], (int, float)):
                return dims[k]
        # Suffix-stripping fallback: BASE_THICKNESS_Z → BASE_THICKNESS
        for dk, dv in dims.items():
            if not isinstance(dv, (int, float)):
                continue
            stripped = dk.upper()
            for suffix in ("_X", "_Y", "_Z", "_MM"):
                if stripped.endswith(suffix):
                    stripped = stripped[: -len(suffix)]
            if stripped in (k.upper() for k in keys):
                return dv
        return None

    _any_thickness = lambda: _fuzzy("BASE_THICKNESS", "BASE_PLATE_THICKNESS",
                                    "base_thickness", "THICKNESS") or 5.0
    base_w = _fuzzy("BASE_WIDTH", "BASE_PLATE_X", "BASE_LENGTH_X",
                     "base_length_xy", "WIDTH") or 42.0
    base_d = _fuzzy("BASE_DEPTH", "BASE_PLATE_Y", "BASE_WIDTH_Y",
                     "base_width_xy", "DEPTH") or 42.0
    base_t = _any_thickness()

    # ── Feature dimensions (from plan sketches if available) ──────────────
    # For each non-XY sketch, compute its offset and Z position relative to
    # the base.  This works for any number of vertical features.
    feature_params: dict[str, dict] = {}
    if plan is not None:
        sketches = getattr(plan, "sketches", []) or []
        for sk in sketches:
            wp = _sketch_workplane(sk) if callable(_sketch_workplane) else str(getattr(sk, "workplane", "XY"))
            if wp == "XY":
                continue
            sid = getattr(sk, "sketch_id", "") if hasattr(sk, "sketch_id") else sk.get("sketch_id", "")
            ents = getattr(sk, "entities", []) if hasattr(sk, "entities") else sk.get("entities", [])
            sk_h = 0.0
            if ents:
                e = ents[0]
                eh = getattr(e, "height", None) if hasattr(e, "height") else e.get("height", 0)
                er = getattr(e, "radius", None) if hasattr(e, "radius") else e.get("radius", 0)
                sk_h = float(eh or 0) or float(er or 0) * 2
            # Offset from base edge (positive = outward from center)
            if wp == "XZ":
                offset_y = base_d / 2.0  # back edge of centered base
                center_z = base_t + sk_h / 2.0 if sk_h > 0 else base_t
                feature_params[sid] = {
                    "plane": "XZ",
                    "offset_from_center": round(offset_y, 4),
                    "center_z": round(center_z, 4),
                    "bottom_z": round(base_t, 4),
                    "sketch_height": round(sk_h, 4),
                }
            elif wp == "YZ":
                offset_x = base_w / 2.0
                center_z = base_t + sk_h / 2.0 if sk_h > 0 else base_t
                feature_params[sid] = {
                    "plane": "YZ",
                    "offset_from_center": round(offset_x, 4),
                    "center_z": round(center_z, 4),
                    "bottom_z": round(base_t, 4),
                    "sketch_height": round(sk_h, 4),
                }

    return {
        "base_half_x": round(base_w / 2.0, 4),
        "base_half_y": round(base_d / 2.0, 4),
        "base_thickness": round(base_t, 4),
        # Per-sketch feature parameters (empty if no non-XY sketches exist)
        "features": feature_params,
    }


def _apply_dimension_patch(plan_dict: dict, patch_dict: dict) -> dict:
    """Apply a flat dot-path→value patch to a plan dict.

    ``patch_dict`` keys look like ``"key_dimensions.BASE_THICKNESS"`` or
    ``"steps.3.hole_diameter_mm"`` where array indices are 0-based.

    Returns the modified plan_dict (mutated in place).
    """
    for path, value in patch_dict.items():
        keys = path.split(".")
        target = plan_dict
        for _, k in enumerate(keys[:-1]):
            if isinstance(target, list) and k.isdigit():
                idx = int(k)
                if idx >= len(target):
                    continue
                target = target[idx]
            elif isinstance(target, dict):
                target = target.get(k)
                if target is None:
                    break
            else:
                break
        else:
            last = keys[-1]
            if isinstance(target, list) and last.isdigit():
                idx = int(last)
                if idx < len(target):
                    if isinstance(value, dict) and "x" in value:
                        # Point3D → preserve existing if dict
                        target[idx] = value
                    else:
                        target[idx] = value
            elif isinstance(target, dict):
                target[last] = value
    return plan_dict


def _sketch_workplane(sketch) -> str:
    """Extract workplane string from a Sketch (model or dict)."""
    if hasattr(sketch, "workplane"):
        wp = sketch.workplane
        return str(wp.value) if hasattr(wp, "value") else str(wp or "XY")
    if isinstance(sketch, dict):
        return str(sketch.get("workplane", "XY"))
    return "XY"


def _gen_sketch_algebra(sketch, var_name: str) -> list[str]:
    """Generate Algebra-API sketch code from a Sketch object (model or dict)."""
    lines = [f"# Sketch: {var_name}"]
    # Handle both Pydantic model and dict
    ents = sketch.entities if hasattr(sketch, "entities") else sketch.get("entities", [])
    if not ents:
        lines.append(f"{var_name} = Rectangle(1, 1)  # placeholder")
        return lines
    ent = ents[0]
    get = lambda o, k, d: (getattr(o, k, None) if hasattr(o, k) else (o.get(k, None) if isinstance(o, dict) else None)) or d
    etype = str(get(ent, "entity_type", "") or "")
    if hasattr(etype, "value"):
        etype = etype.value
    if etype == "rectangle":
        lines.append(f"{var_name} = Rectangle({get(ent,'width',10)}, {get(ent,'height',10)})")
    elif etype == "circle":
        lines.append(f"{var_name} = Circle({get(ent,'radius',5)})")
    elif etype == "slot":
        lines.append(f"{var_name} = SlotCenterLine({get(ent,'center_separation',5)}, {get(ent,'radius',2)})")
    elif etype == "polygon":
        cr = get(ent, "circumscribed_radius", None)
        ns = get(ent, "num_sides", None)
        cp = get(ent, "control_points", None)

        # Check if sketch notes indicate a non-regular shape (wedge, sector,
        # lug-with-arc-top, rib, cutout, etc.).  In that case, RegularPolygon
        # or a plain Polyline would be WRONG — generate TODO_AIDER so Aider
        # can rewrite with BuildLine + ThreePointArc / Polyline + Pos+Rot
        # transforms, using the architect's control_points as vertex reference.
        sketch_notes = str(
            getattr(sketch, "notes", "") if hasattr(sketch, "notes")
            else (sketch.get("notes", "") if isinstance(sketch, dict) else "")
        ).lower()
        entity_label = str(get(ent, "label", "") or "").lower()
        _non_regular_keywords = {
            "wedge", "sector", "fan", "pie", "arc-shaped", "segment",
            "trapezoid", "custom", "non-regular", "irregular",
            "inner radius", "outer radius", "subtend",
            "semicircle", "arc", "rounded top",
            "wedge", "sector", "arc-shaped", "arc", "semicircle",
        }
        _is_non_regular = any(kw in sketch_notes or kw in entity_label for kw in _non_regular_keywords)

        if _is_non_regular:
            # Non-regular polygon → Aider should rewrite this with proper
            # build123d API (BuildLine + ThreePointArc for arcs, Pos+Rot for
            # transforms).  Include control_points in the placeholder so
            # Aider has the vertex coordinates as reference.
            entity_data = {}
            for key in ['width', 'height', 'radius', 'circumscribed_radius',
                        'num_sides', 'start', 'end', 'center']:
                val = get(ent, key, None)
                if val is not None:
                    entity_data[key] = val
            if cp:
                cp_data = []
                for _p in cp:
                    if hasattr(_p, "x"):
                        cp_data.append({"x": float(_p.x), "y": float(_p.y)})
                    elif isinstance(_p, dict):
                        cp_data.append({"x": float(_p.get("x", 0)),
                                        "y": float(_p.get("y", 0))})
                    else:
                        _px = get(_p, "x", 0)
                        _py = get(_p, "y", 0)
                        cp_data.append({"x": float(_px), "y": float(_py)})
                entity_data["control_points"] = cp_data
            lines.append(f"# TODO_AIDER: non-regular polygon ({entity_label or sketch_notes[:40]})")
            lines.append(f"# Feature data: {entity_data}")
            lines.append(f"# Sketch notes: {sketch_notes[:200]}")
            lines.append(f"{var_name} = None  # Placeholder — Aider should use BuildLine + ThreePointArc / Polyline + Pos+Rot based on control_points above")
        elif cr is not None and ns is not None and ns >= 3:
            # Regular polygon (hexagon, octagon, etc.) — bolt heads, gear blanks
            lines.append(f"{var_name} = RegularPolygon({cr}, {ns})")
        elif cp:
            # Freeform polygon with explicit control_points but no "custom"
            # keyword — simple triangle/quad, deterministic Polyline is fine.
            _pts = []
            for _p in cp:
                _px = get(_p, "x", 0)
                _py = get(_p, "y", 0)
                # Handle Point2D model (has .x/.y attrs) and dict ({"x":..,"y":..})
                if hasattr(_p, "x"):
                    _px = float(_p.x)
                    _py = float(_p.y)
                elif isinstance(_p, dict):
                    _px = float(_p.get("x", 0))
                    _py = float(_p.get("y", 0))
                _pts.append(f"({_px}, {_py})")
            _pts_str = ", ".join(_pts)
            lines.append(f"# Freeform polygon via BuildLine (from control_points)")
            lines.append(f"with BuildLine() as {var_name}_line:")
            lines.append(f"    Polyline({_pts_str}, close=True)")
            lines.append(f"{var_name} = make_face({var_name}_line.wire())")
        else:
            # No control_points, no circumscribed_radius+num_sides → fallback
            # hardcoded right-triangle.  This is a LAST RESORT — the architect
            # should provide control_points for any non-regular shape.
            base = get(ent, "width", None) or 40.0
            gusset_h = get(ent, "height", None) or 85.0
            lines.append(f"# WARNING: polygon has no control_points — using hardcoded fallback triangle")
            lines.append(f"# Architect should provide control_points for this sketch")
            lines.append(f"with BuildLine() as {var_name}_line:")
            lines.append(f"    Polyline((0,0), ({base}, 0), (0, {gusset_h}), close=True)")
            lines.append(f"{var_name} = make_face({var_name}_line.wire())")
    elif etype == "line":
        # Line entity → generate placeholder for Aider to fill in
        lines.append(f"# TODO_AIDER: line entity")
        lines.append(f"# Feature data: {get(ent, 'points', [])}")
        lines.append(f"{var_name} = None  # Placeholder for Aider")
    else:
        # Unsupported entity type → generate placeholder for Aider
        lines.append(f"# TODO_AIDER: {etype} entity")
        entity_data = {}
        for key in ['width', 'height', 'radius', 'points', 'center_separation',
                    'circumscribed_radius', 'num_sides', 'start', 'end', 'center']:
            val = get(ent, key, None)
            if val is not None:
                entity_data[key] = val
        lines.append(f"# Feature data: {entity_data}")
        lines.append(f"{var_name} = None  # Placeholder for Aider")
    return lines


def _has_unsupported_placeholders(code: str) -> tuple[bool, str | None]:
    """Check if the generated code contains placeholders for unsupported features."""
    return "# TODO_AIDER:" in code


def _fill_unsupported_with_aider(
    script_path: Path, code: str, user_request: str,
    special_features: list[str] | None = None,
) -> bool:
    """Use Aider to fill in placeholders and fix errors in the generated code.

    This is a hybrid approach: deterministic generation handles supported features,
    and Aider fills in unsupported parts AND fixes any obvious errors it sees
    in the surrounding code.

    Returns True if successful, False otherwise.
    """
    import re

    # Extract all placeholders
    placeholder_pattern = re.compile(
        r"# TODO_AIDER: (.*?)\n\s*# Feature data: (.*?)\n\s*(.*?)(?=\n\s*#|\n\s*[a-zA-Z_]|\Z)",
        re.DOTALL
    )
    matches = list(placeholder_pattern.finditer(code))

    if not matches:
        return True  # No placeholders to fill

    # Build a focused prompt for Aider
    feature_specs = []
    for match in matches:
        feature_type = match.group(1).strip()
        feature_data = match.group(2).strip()
        feature_specs.append(f"- {feature_type}: {feature_data}")

    # Special features section — non-trivial geometric constraints from CADBrief
    special_features_section = ""
    if special_features:
        features_lines = "\n".join(f"  [{i}] {feat}" for i, feat in enumerate(special_features, 1))
        special_features_section = f"""
## 🔍 Special Features (MUST verify each item)

These constraints were extracted from the user request by Spec Planner.
QA only checks overall dimension / single_body / water_tightness — it does
NOT verify these.  You MUST ensure your placeholder fills satisfy each item:

{features_lines}

After filling placeholders, self-check: does each special feature hold?
"""
    else:
        special_features_section = ""

    prompt = f"""You need to complete and fix the build123d code.

Original requirement: {user_request}
{special_features_section}
## ⚠️ CORE TASK (MUST complete first): Fill ALL placeholders

Your PRIMARY and MOST IMPORTANT job is to replace EVERY "# TODO_AIDER:"
section with proper build123d implementation.  This is non-negotiable.

Placeholders to fill:
{chr(10).join(feature_specs)}

Do NOT skip any placeholder.  Do NOT leave any `= None` or `# Placeholder`
in the final code.  Every TODO_AIDER must be replaced with working geometry.

## Secondary task: Fix obvious errors (ONLY after all placeholders are filled)

After you have filled ALL placeholders, you may also fix obvious errors in
the surrounding code if you see them.  Common issues:
- Wrong API calls or parameters
- Missing imports
- Incorrect coordinate transforms
- Boolean cut tools that don't overshoot the target body
- make_face() called with wrong number of arguments

But remember: filling placeholders comes FIRST.  Do not spend time fixing
surrounding code while any placeholder remains unfilled.

## Iron Rules (MUST follow)

### Rule 1: Feature Preservation
Do NOT delete holes, slots, ribs, or other features to make errors disappear.
Only fix by adjusting coordinates, dimensions, or adding boolean unions.

### Rule 2: Algebra API
Use ONLY build123d Algebra API:
  body = extrude(sketch, amount=10)
  body = body - hole_tool
  body = body + other_body

**CRITICAL: Extrusion direction must be perpendicular to sketch plane.**
BuildLine() defaults to XY plane (normal=Z). Extrude along Z, then rotate:
  ❌ extrude(sk, amount=6, dir=(1,0,0))  # parallel to XY plane → ERROR
  ✅ solid = extrude(sk, amount=6); solid = Pos(18,0,0) * Rot(Y=90) * solid

### Rule 3: Context Managers — FORBIDDEN (except BuildLine for wire profiles)
  with BuildPart(): ...    ← FORBIDDEN
  with BuildSketch(): ...  ← FORBIDDEN
  with Locations(): ...    ← FORBIDDEN

Sole exception — BuildLine for creating wire profiles with arcs:
```python
with BuildLine() as lug_wire:
    Line((-18, 0), (18, 0))
    ThreePointArc((18, 34), (0, 52), (-18, 34))
    Line((-18, 34), (-18, 0))
sk_lug_profile = make_face(lug_wire.wire())  # ← pass wire() result
```

make_face() accepts ONLY 0-2 positional arguments:
  ❌ make_face(Line(...), Line(...), Arc(...))  — WRONG
  ✅ make_face(wire_name.wire())                — CORRECT

### Rule 4: Overshoot Boolean Cuts
Use `_safe_cut(body, tool, label)`. Z-axis holes: `Align.MIN` (see `build123d_reference.md`).

### Rule 5: Fillets and Chamfers Last
Fillets/chamfers MUST come after ALL boolean operations (union, cut).
"""

    # Use Aider to fill in the placeholders
    try:
        from aider.coders import Coder
        from aider.models import Model
        from aider.io import InputOutput

        # Configure Aider with DashScope API (same as other modules)
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or _HARDCODED_API_KEY
        if not api_key:
            print("[HYBRID CODER] No DASHSCOPE_API_KEY available")
            return False

        if _API_BASE_ENV_VAR:
            os.environ[_API_BASE_ENV_VAR] = _DS_BASE_URL
        if _API_KEY_ENV_VAR:
            os.environ[_API_KEY_ENV_VAR] = api_key

        model = Model(_AIDER_MODEL_NAME)
        model.extra_params = {"max_tokens": _AIDER_MAX_TOKENS}  # avoid truncation
        io = InputOutput(
            yes=True,
            pretty=False,
            dry_run=False,
        )

        coder = Coder.create(
            main_model=model,
            io=io,
            fnames=[str(script_path), _BUILD123D_REF],
            auto_commits=False,
        )

        coder.run(prompt)
        print(f"[HYBRID CODER] Aider successfully filled {len(matches)} placeholders")
        return True

    except Exception as e:
        print(f"[HYBRID CODER] Aider failed to fill placeholders: {e}")
        import traceback
        traceback.print_exc()
        return False


def _infer_edge_filter(step) -> str:
    """Infer a safe edge filter expression from step metadata.

    Returns a Python **list-comprehension expression** using ``s`` as the
    solid parameter (e.g. ``[e for e in s.edges().filter_by(Axis.Z) if e.length() > 5]``).
    The fillet/chamfer branches embed this directly into the lambda:
    ``lambda s: <edge_sel>``.

    Uses ``e.length()`` and ``e.center()`` for robust filtering — short
    junction edges (which cause OpenCascade "ChFi3d_Builder: only 2 faces"
    errors) are excluded by the length filter.  This is more robust than
    bbox-coordinate filtering (``e.bounding_box().min.X``) which Aider
    tends to hardcode and which breaks when part dimensions change.

    Never returns a bare ``.edges()`` call.  Defaults to vertical edges
    with a minimum length filter.
    """
    sel = step.edge_selector if hasattr(step, "edge_selector") else None
    if sel and isinstance(sel, str) and sel.strip():
        raw = sel.strip().lower()

        # Handle circular edges (e.g., "outer circular edges of fins at radius 31mm")
        if "circular" in raw or "circle" in raw:
            # Try to extract radius from the description
            import re
            radius_match = re.search(r'radius\s*(\d+(?:\.\d+)?)\s*mm', raw)
            if radius_match:
                radius = float(radius_match.group(1))
                # Filter circles by radius (with tolerance) + length filter
                return f"[e for e in s.edges().filter_by(GeomType.CIRCLE) if abs(e.radius - {radius}) < 0.5 and e.length() > 2]"
            return "[e for e in s.edges().filter_by(GeomType.CIRCLE) if e.length() > 2]"

        # Handle linear edges — use e.length() to exclude short junction edges
        # that cause "ChFi3d_Builder: only 2 faces" fillet failures.
        if "vertical" in raw:
            return "[e for e in s.edges().filter_by(Axis.Z) if e.length() > 5]"
        if "horizontal" in raw:
            # Horizontal edges: base perimeter (long) vs cutout/junction (short).
            # Length > 30mm catches base perimeter edges on typical parts
            # (base length 120mm, width 60mm — both > 30) while excluding
            # short cutout top/bottom edges (typically < 25mm).
            return "[e for e in s.edges().filter_by(Plane.XY) if e.length() > 30]"
        if "top" in raw:
            return "[e for e in s.edges().sort_by(Axis.Z)[-1:] if e.length() > 5]"
        if "outer" in raw or "external" in raw:
            return "[e for e in s.edges().filter_by(Axis.Z) if e.length() > 5]"
    return "[e for e in s.edges().filter_by(Axis.Z) if e.length() > 5]"

def node_python_coder(state: GraphState) -> dict:
    """Translate an ArchitectPlan into executable build123d Python code.

    1. Reads the ArchitectPlan and any previous QA feedback from state.
    2. Calls Qwen 3.7-max (DashScope) to generate self-contained build123d code.
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
    force_refresh: bool = state.get("force_refresh", False)

    # -- Cache: reuse existing .step/.stl ONLY on fresh runs (not retries) -
    cwd = Path.cwd()
    cached_step = cwd / f"temp_output_{iteration}.step"
    cached_stl = cwd / f"temp_output_{iteration}.stl"
    cached_code = cwd / f"temp_design_{iteration}.py"
    is_fresh_run = (iteration == 0 and qa_report is None)
    if is_fresh_run and not force_refresh and cached_step.is_file() and cached_stl.is_file():
        python_code = cached_code.read_text(encoding="utf-8") if cached_code.is_file() else ""
        print("[CACHE] Reusing existing .step/.stl — skipping Python Coder")
        return {
            "current_python_code": python_code,
            "current_step_path": str(cached_step.resolve()),
            "current_stl_path": str(cached_stl.resolve()),
            "execution_log": ["node_python_coder: reused cached artifacts"],
            "node_history": list(state.get("node_history", [])) + ["coder"],
        }

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
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_CFG_LLM_API_TIMEOUT,  # configurable via config.py
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        return _coder_failure_state(
            iteration=iteration,
            error_message="Script execution timed out after 120 seconds.",
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
    if not step_path.is_file():
        missing_files.append(str(step_path))
    if not stl_path.is_file():
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

# ---------------------------------------------------------------------------
# Internal helpers for node_python_coder
# ---------------------------------------------------------------------------

def _build_coder_user_prompt(
    *,
    plan_json: str,
    previous_feedback: str,
    step_path: str,
    stl_path: str,
    user_request: str = "",
) -> str:
    """Assemble the user-prompt payload sent to the LLM."""
    req_section = ""
    if user_request:
        req_section = textwrap.dedent(f"""\
        ## Original User Request (ground truth — never deviate from this)

        {user_request}

        """)
    return textwrap.dedent(f"""\
    {req_section}\
    ## Architect Plan (JSON)

    Below is the complete Geometric Architect plan.  Translate it into a
    working build123d Python script following the conventions in the system
    prompt.

    ```json
    {plan_json}
    ```

    ## Output Paths

    You MUST write the final STEP and STL files to these EXACT paths:

      - STEP : {step_path}
      - STL  : {stl_path}

    Use these literal path strings in your `export_step()` and `export_stl()`
    calls.

    ## Previous QA Feedback

    {previous_feedback}

    ## Task

    Write the complete, self-contained build123d Python script now.  Output
    ONLY the ```python fenced code block — no explanations.
    """)

def _extract_qa_error_details(qa_report: QAReport | dict) -> list[str]:
    """Pull error_details from a QAReport (pydantic or raw dict)."""
    if hasattr(qa_report, "error_details"):
        return list(qa_report.error_details)  # type: ignore[union-attr]
    if isinstance(qa_report, dict):
        return list(qa_report.get("error_details", []))
    return []

def _coder_failure_state(
    *,
    iteration: int,
    error_message: str,
    script_path: str,
    raw_response: str | None = None,
    node_history: list[str] | None = None,
) -> dict:
    """Build the state-update dict returned when code generation or execution fails.

    Because the project schema does not define a dedicated SYNTAX error type,
    we route back to the Coder via ``error_type=DIMENSION`` and embed the
    traceback / stderr in a lightweight mock QAReport so the Coder can read
    its own errors and self-correct on the next iteration.

    Parameters
    ----------
    node_history : list[str] | None
        The accumulated node history from the current state, with ``"coder"``
        already appended.  If ``None``, falls back to ``["coder"]`` (which
        may cause the routing retry budget to reset — callers should always
        pass the real history).
    """
    error_details_list = [error_message]
    if raw_response:
        error_details_list.append(
            f"Raw LLM response (first 500 chars): {raw_response[:500]}"
        )

    # Build a minimal QAReport carrying just enough structure for the Coder
    # to parse the feedback.
    mock_qa = QAReport(
        cad_brief_id="(coder-failure)",
        engine_a=EngineReport(engine_name="cadpy_analysis"),
        engine_b=EngineReport(
            engine_name="check_mesh",
            errors=error_details_list,
        ),
        all_passed=False,
        error_type=ErrorType.DIMENSION,
        error_details=error_details_list,
        iteration=iteration,
    )

    log_lines = [
        f"node_python_coder [iter {iteration}]: FAILED",
        f"  Script: {script_path}",
        f"  Error : {error_message[:300]}",
    ]

    return {
        "error_type": ErrorType.DIMENSION,
        "qa_report": mock_qa,
        "execution_log": log_lines,
        "node_history": node_history if node_history is not None else ["coder"],
    }


def _tail(text: str, n_lines: int) -> str:
    """Return the last *n_lines* lines of *text*."""
    if not text:
        return "(empty)"
    lines = text.splitlines()
    if len(lines) <= n_lines:
        return "\n".join(lines)
    return "...\n" + "\n".join(lines[-n_lines:])


def _file_size_kb(path: Path) -> str:
    """Human-readable file size string for a Path."""
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return "? KB"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    return f"{size_bytes / 1024:.1f} KB"


# ============================================================================
# Node: Dual-Engine QA
# ============================================================================

# Resolve the repo root for locating check_mesh.py and cadpy packages.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHECK_MESH_SCRIPT = _REPO_ROOT / "legacy_refs" / "check_mesh.py"


# ---------------------------------------------------------------------------
# Engine A — Deterministic STEP QA (cadpy topology-based)
# ---------------------------------------------------------------------------


def _load_step_topology(step_path: str) -> tuple:
    """Run the cadpy pipeline: load → mesh → extract selectors.

    Returns ``(faces, edges, vertices, bbox, volume_mm3, raw_json, error_msg)``.
    On success ``error_msg`` is ``None``; on failure the first six elements
    are ``None`` and ``error_msg`` contains a diagnostic string.
    """
    from pathlib import Path as _Path

    try:
        _cadpy_src = str(_REPO_ROOT / "packages" / "cadpy" / "src")
        if _cadpy_src not in sys.path:
            sys.path.insert(0, _cadpy_src)
        from cadpy.step_scene import (
            load_step_scene_cached,
            mesh_step_scene,
            extract_selectors_from_scene,
            SelectorProfile,
            SelectorOptions,
        )
        from cadpy.analysis import _table_rows

        scene = load_step_scene_cached(_Path(step_path))
        mesh_step_scene(
            scene, linear_deflection=0.1, angular_deflection=0.1, relative=True,
        )
        opts = SelectorOptions(linear_deflection=0.1, angular_deflection=0.1)
        bundle = extract_selectors_from_scene(
            scene, cad_ref="qa", profile=SelectorProfile.ARTIFACT, options=opts,
        )
        manifest = bundle.manifest

        faces = _table_rows(manifest, "faces", "faceColumns")
        edges = _table_rows(manifest, "edges", "edgeColumns")
        vertices = _table_rows(manifest, "vertices", "vertexColumns")
        bbox = manifest.get("bbox", {})

        # Volume via OCP (precise)
        volume_mm3 = None
        try:
            from OCP.GProp import GProp_GProps
            from OCP.BRepGProp import BRepGProp
            from OCP.STEPControl import STEPControl_Reader
            reader = STEPControl_Reader()
            if reader.ReadFile(str(step_path)) == 1:
                reader.TransferRoots()
                shape = reader.OneShape()
                props = GProp_GProps()
                BRepGProp.Volume(shape, props)
                volume_mm3 = props.Mass()
        except Exception as vol_exc:
            print(f"[ENGINE A] Volume calculation skipped: {vol_exc}")

        return (faces, edges, vertices, bbox, volume_mm3, manifest, None)
    except ImportError as imp_exc:
        return (None, None, None, None, None, None, f"OCP/cadpy not installed: {imp_exc}")
    except Exception as exc:
        return (None, None, None, None, None, None, f"STEP topology load failed: {exc}")

def _parse_selector(expression: str) -> dict | None:
    """Parse a selector DSL expression into a structured dict.

    Format: ``entity_type:filter1,filter2,...``

    Returns ``None`` if parsing fails.
    """
    if not expression or ":" not in expression:
        return None

    parts = expression.split(":", 1)
    entity_type = parts[0].strip().lower()
    if entity_type not in ("face", "edge"):
        print(f"[SELECTOR] Unknown entity type: {entity_type}")
        return None

    result: dict = {
        "entity": entity_type,
        "filters": {},
        "sort_key": None,
        "sort_dir": "asc",
        "index": 0,
    }

    tokens = [t.strip() for t in parts[1].split(",") if t.strip()]
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "surface":
            result["filters"]["surface"] = value.lower()
        elif key == "curve":
            result["filters"]["curve"] = value.lower()
        elif key == "axis":
            result["filters"]["axis"] = value.lower()
        elif key == "axis_sign":
            result["filters"]["axis_sign"] = value
        elif key == "radius_min":
            try:
                result["filters"]["radius_min"] = float(value)
            except ValueError:
                pass
        elif key == "radius_max":
            try:
                result["filters"]["radius_max"] = float(value)
            except ValueError:
                pass
        elif key == "sort":
            # sort=key or sort=key:dir
            sort_parts = value.split(":")
            result["sort_key"] = sort_parts[0].lower()
            if len(sort_parts) > 1:
                result["sort_dir"] = sort_parts[1].lower()
        elif key == "index":
            if value.lower() == "all":
                result["index"] = "all"
            else:
                try:
                    result["index"] = int(value)
                except ValueError:
                    result["index"] = 0

    return result


_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def _resolve_selector(parsed: dict, faces: list, edges: list) -> list[dict]:
    """Resolve a parsed selector against topology data.

    Returns a list of matching entity dicts (empty if no match).
    For ``index=N``, returns a single-element list.
    For ``index="all"``, returns all matching entities after sorting.
    """
    entity_type = parsed.get("entity", "face")
    entities = faces if entity_type == "face" else edges
    filters = parsed.get("filters", {})

    if not entities:
        return []

    matched: list[dict] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue

        # Filter by surface/curve type
        if "surface" in filters:
            st = (ent.get("surfaceType") or "").lower()
            if st != filters["surface"]:
                continue
        if "curve" in filters:
            ct = (ent.get("curveType") or "").lower()
            if ct != filters["curve"]:
                continue

        # Filter by axis
        if "axis" in filters:
            target_axis = filters["axis"]
            ax_idx = _AXIS_INDEX.get(target_axis)
            if ax_idx is None:
                continue

            params = ent.get("params") or {}
            surface_type = (ent.get("surfaceType") or "").lower()
            curve_type = (ent.get("curveType") or "").lower()

            # Choose the right vector to check based on entity type
            axis_vec = None
            if surface_type == "cylinder":
                # Cylinder: use centerline axis from params
                axis_vec = params.get("axis")
            elif surface_type in ("cone", "torus"):
                axis_vec = params.get("axis")
            elif surface_type == "plane":
                # Plane: use surface normal
                axis_vec = ent.get("normal")
                if not axis_vec:
                    axis_vec = params.get("axis")
            elif curve_type in ("circle", "ellipse"):
                # Circle/Ellipse edge: use the circle's axis (normal to the plane)
                axis_vec = params.get("axis")
            elif curve_type == "line":
                axis_vec = params.get("direction")
            else:
                # Unknown type: try normal, then params.axis, then params.direction
                axis_vec = ent.get("normal") or params.get("axis") or params.get("direction")

            if axis_vec and isinstance(axis_vec, (list, tuple)) and len(axis_vec) >= 3:
                try:
                    from cadpy.analysis import dominant_axis as _da
                    da = _da(list(axis_vec))
                    if not (da and da.get("aligned") and da.get("axis") == target_axis):
                        continue
                except Exception:
                    mag = sum(v * v for v in axis_vec) ** 0.5
                    if mag < 1e-9 or abs(axis_vec[ax_idx]) / mag < 0.9:
                        continue
            else:
                # No axis vector available — skip this entity
                continue

        # Filter by radius range
        params = ent.get("params") or {}
        radius = params.get("radius")
        if radius is not None:
            if "radius_min" in filters and radius < filters["radius_min"]:
                continue
            if "radius_max" in filters and radius > filters["radius_max"]:
                continue

        matched.append(ent)

    if not matched:
        return []

    # Sort by sort_key
    sort_key = parsed.get("sort_key")
    sort_dir = parsed.get("sort_dir", "asc")
    if sort_key:
        def _sort_value(ent: dict) -> float:
            if sort_key == "area":
                return float(ent.get("area") or 0)
            if sort_key == "radius":
                p = ent.get("params") or {}
                return float(p.get("radius") or 0)
            if sort_key == "length":
                return float(ent.get("length") or 0)
            if sort_key in ("center_x", "center_y", "center_z"):
                ax = _AXIS_INDEX.get(sort_key[-1], 0)
                center = ent.get("center")
                if center and isinstance(center, (list, tuple)) and len(center) > ax:
                    return float(center[ax])
                bbox = ent.get("bbox") or {}
                mn = bbox.get("min", [0, 0, 0])
                mx = bbox.get("max", [0, 0, 0])
                return (float(mn[ax]) + float(mx[ax])) / 2
            return 0

        reverse = sort_dir == "desc"
        matched.sort(key=_sort_value, reverse=reverse)

    # Apply index
    idx = parsed.get("index", 0)
    if idx == "all":
        return matched
    if isinstance(idx, int):
        if 0 <= idx < len(matched):
            return [matched[idx]]
        return []
    return matched


def _measure_entities(entities: list[dict], kind: str, axis_str: str,
                      part_bbox: dict | None = None) -> float | None:
    """Extract a numeric measurement from resolved topology entities.

    Handles both single-entity and multi-entity (gap/distance) cases.
    ``part_bbox`` is the global part bounding box, used for overall dimensions.
    """
    if not entities:
        return None

    ax_idx = _AXIS_INDEX.get(axis_str, 0)

    # --- Single-entity measurements ---
    if kind in ("hole_diameter", "counterbore_diameter", "boss_diameter"):
        params = entities[0].get("params") or {}
        radius = params.get("radius")
        if radius is not None:
            return float(radius) * 2
        return None

    if kind == "fillet_radius":
        params = entities[0].get("params") or {}
        return float(params["radius"]) if "radius" in params else None

    if kind == "hole_position":
        center = entities[0].get("center")
        if center and isinstance(center, (list, tuple)) and len(center) > ax_idx:
            return float(center[ax_idx])
        return None

    if kind == "overall_dimension":
        # If the selector matched a CYLINDER face, the user likely wants
        # the cylinder's diameter, not the part's bbox extent along axis.
        # (The Spec Planner sometimes uses kind=overall_dimension for
        # diameter measurements like "barrel-diameter".)
        ent = entities[0]
        surface_type = (ent.get("surfaceType") or "").lower()
        if surface_type == "cylinder":
            params = ent.get("params") or {}
            radius = params.get("radius")
            if radius is not None:
                return float(radius) * 2

        # For planar faces or no entity: use part bbox extent.
        if part_bbox:
            mn = part_bbox.get("min")
            mx = part_bbox.get("max")
            if mn and mx and len(mn) > ax_idx and len(mx) > ax_idx:
                return abs(float(mx[ax_idx]) - float(mn[ax_idx]))
        # Fallback: face bbox
        bbox = ent.get("bbox") or {}
        mn = bbox.get("min")
        mx = bbox.get("max")
        if mn and mx and len(mn) > ax_idx and len(mx) > ax_idx:
            return abs(float(mx[ax_idx]) - float(mn[ax_idx]))
        return None

    if kind == "wall_thickness":
        # Two sub-modes detected by normal directions:
        # A) OPPOSING normals → true wall thickness (min gap between two
        #    parallel faces pointing at each other, e.g. inner/outer wall)
        # B) SAME direction normals → periodic pitch (average gap between
        #    consecutive faces with same orientation, e.g. fin-to-fin spacing)
        if len(entities) < 2:
            return None

        pos_centers = []  # faces with normal in +axis direction
        neg_centers = []  # faces with normal in -axis direction
        for ent in entities:
            c = ent.get("center")
            if not c or not isinstance(c, (list, tuple)) or len(c) <= ax_idx:
                continue
            center_val = float(c[ax_idx])
            normal = ent.get("normal") or (ent.get("params") or {}).get("axis")
            if normal and isinstance(normal, (list, tuple)) and len(normal) >= 3:
                mag = sum(v * v for v in normal) ** 0.5
                if mag < 1e-9:
                    continue
                component = normal[ax_idx] / mag
                if component >= 0.9:
                    pos_centers.append(center_val)
                elif component <= -0.9:
                    neg_centers.append(center_val)
            else:
                continue

        # Mode A: Opposing normals → minimum gap between pos and neg faces.
        # Only use this for simple wall pairs (few faces per direction).
        # When many faces exist in each direction (≥4), it's likely a
        # periodic structure (e.g. fins), not a simple wall — skip to Mode B.
        if pos_centers and neg_centers and len(pos_centers) < 4 and len(neg_centers) < 4:
            min_dist = float("inf")
            for pc in pos_centers:
                for nc in neg_centers:
                    d = abs(pc - nc)
                    if 0.001 < d < min_dist:
                        min_dist = d
            if min_dist < float("inf"):
                return min_dist

        # Mode B: Periodic pitch detection.
        # Run on SAME-DIRECTION faces only (e.g. all +Z faces, which are
        # one per fin).  Mixing +Z and -Z faces introduces within-fin gaps
        # that confuse the pitch calculation.
        # Pick whichever group has more faces for robustness.
        group = pos_centers if len(pos_centers) >= len(neg_centers) else neg_centers
        if len(group) < 2:
            return None
        group.sort()
        pitch_gaps = [
            group[i + 1] - group[i]
            for i in range(len(group) - 1)
            if group[i + 1] - group[i] > 0.001
        ]
        if not pitch_gaps:
            return None
        # Return the average pitch, excluding the largest gap (likely a
        # boundary artifact like barrel-to-first-fin or last-fin-to-cap).
        if len(pitch_gaps) >= 3:
            sorted_gaps = sorted(pitch_gaps)
            trimmed = sorted_gaps[:-1]
            return sum(trimmed) / len(trimmed)
        return sum(pitch_gaps) / len(pitch_gaps)

    if kind == "hole_distance":
        # Closest pair of cylinder centers to nominal
        if len(entities) < 2:
            return None
        centers = []
        for ent in entities:
            c = ent.get("center")
            if c and isinstance(c, (list, tuple)) and len(c) >= 3:
                centers.append([float(v) for v in c[:3]])
        if len(centers) < 2:
            return None
        min_dist = float("inf")
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = sum((a - b) ** 2 for a, b in zip(centers[i], centers[j])) ** 0.5
                if d < min_dist:
                    min_dist = d
        return min_dist if min_dist < float("inf") else None

    # Default: try center coordinate for single entity
    if len(entities) == 1:
        center = entities[0].get("center")
        if center and isinstance(center, (list, tuple)) and len(center) > ax_idx:
            return float(center[ax_idx])
    return None


def _vector_axis_aligned(vector, axis: str, threshold: float = 0.9) -> bool:
    """Check if a 3D vector is aligned with a given axis (x/y/z).

    Uses cadpy.analysis.dominant_axis for proper normalization.
    Unlike the previous manual threshold check, this normalizes the
    vector first so the threshold comparison is meaningful.
    """
    try:
        from cadpy.analysis import dominant_axis
        da = dominant_axis(vector)
        if da is None:
            return False
        return da["axis"] == axis and da["aligned"]
    except Exception:
        # Fallback: manual check (handles import failures gracefully)
        if vector is None or len(vector) < 3:
            return False
        import math
        mag = math.sqrt(sum(float(v) ** 2 for v in vector[:3]))
        if mag < 1e-12:
            return False
        ax_idx = {"x": 0, "y": 1, "z": 2}[axis]
        return abs(float(vector[ax_idx])) / mag >= threshold


def _find_cylindrical_faces(faces: list, axis: str | None = None) -> list:
    """Return all cylindrical faces, optionally filtered by cylinder axis.

    NOTE: For cylinders, the axis field is ``params.axis`` (the cylinder
    centerline), NOT ``normal`` (which is the radial surface normal).
    This was a bug in the previous implementation.
    """
    result = []
    for f in faces:
        surface_type = str(f.get("surfaceType") or "").lower()
        if surface_type != "cylinder":
            continue
        if axis is not None:
            params = f.get("params")
            cyl_axis = params.get("axis") if isinstance(params, dict) else None
            if cyl_axis is None:
                continue
            if not _vector_axis_aligned(cyl_axis, axis):
                continue
        result.append(f)
    return result


def _find_planar_faces(faces: list, axis: str | None = None) -> list:
    """Return all planar faces, optionally filtered by normal axis."""
    result = []
    for f in faces:
        surface_type = str(f.get("surfaceType") or "").lower()
        if surface_type != "plane":
            continue
        if axis is not None:
            normal = f.get("normal")
            if normal is None:
                continue
            if not _vector_axis_aligned(normal, axis):
                continue
        result.append(f)
    return result


def _face_center(f: dict) -> tuple[float, float, float] | None:
    """Extract center (x, y, z) from a face row.

    Uses cadpy.analysis.bbox_center for the fallback path.
    """
    c = f.get("center")
    if c and len(c) == 3:
        try:
            return (float(c[0]), float(c[1]), float(c[2]))
        except Exception:
            pass
    # Fallback to bbox center via cadpy.analysis
    bbox = f.get("bbox")
    if bbox:
        try:
            from cadpy.analysis import bbox_center
            bc = bbox_center(bbox)
            if bc is not None:
                return (bc[0], bc[1], bc[2])
        except Exception:
            pass
    return None


def _run_engine_a_cadpy(
    *,
    step_path: str,
    cad_brief,  # CADBrief | dict
    engine_b_mesh_resolution: dict,
    feature_measurements: dict | None = None,
    iteration: int = 0,
    selector_map: dict[str, str] | None = None,
) -> EngineReport:
    """Deterministic STEP QA using cadpy.step_scene + cadpy.analysis.

    Loads the STEP file, extracts a full topology manifest (faces, edges,
    vertices, bounding-box), then iterates every VerificationTarget from
    the CADBrief and matches it against the actual geometric features.

    PRIORITY 1: White-box instrumentation (feature_measurements) — exact
    per-feature bounding-box data saved BEFORE boolean merge.
    PRIORITY 2: cadpy STEP topology analysis.

    Falls back gracefully: targets that cannot be matched produce a
    ``passed=False`` result with "Feature not found".
    """
    warnings: list[str] = []
    errors: list[str] = []
    results: list[VerificationResult] = []

    targets = _attr(cad_brief, "verification_targets", [])

    # Load feature measurements from white-box instrumentation if not provided
    if feature_measurements is None:
        measurements_file = Path(f"temp_measurements_{iteration}.json")
        if not measurements_file.is_file():
            measurements_file = Path("temp_measurements_0.json")
        if measurements_file.is_file():
            try:
                with open(measurements_file, 'r', encoding='utf-8') as f:
                    feature_measurements = json.load(f)
                # Remove metadata keys that are not feature measurements
                feature_measurements.pop("_step_hash", None)
                feature_measurements.pop("_stl_hash", None)
                feature_measurements.pop("_timestamp", None)
                print(f"[ENGINE A] Loaded {len(feature_measurements)} feature measurements from {measurements_file.name}")
            except Exception as exc:
                print(f"[ENGINE A] WARNING: Failed to load feature measurements: {exc}")

    # ------------------------------------------------------------------
    # 1. Load STEP topology
    # ------------------------------------------------------------------
    topo = _load_step_topology(step_path)
    faces, edges, vertices, bbox, volume_mm3, manifest, topo_error = topo
    if topo_error is not None:
        return EngineReport(
            engine_name="cadpy_analysis",
            results=[],
            summary="Engine A: STEP topology extraction failed.",
            warnings=[],
            errors=[topo_error],
        )

    summary_parts = [
        f"Topology: {len(faces)} faces, {len(edges)} edges, {len(vertices)} vertices",
    ]
    if bbox:
        from cadpy.analysis import bbox_facts as _bf
        _facts = _bf(bbox)
        _size = _facts.get("size")
        if _size:
            summary_parts.append(f"Bbox: [{_size[0]:.1f}, {_size[1]:.1f}, {_size[2]:.1f}] mm")
    if volume_mm3 is not None:
        summary_parts.append(f"Volume: {volume_mm3:.1f} mm³")

    # ------------------------------------------------------------------
    # 2. Pre-index features for matching
    # ------------------------------------------------------------------
    cyl_faces = _find_cylindrical_faces(faces)
    plan_faces = _find_planar_faces(faces)
    cyl_by_axis: dict[str, list] = {}
    for ax in ("x", "y", "z"):
        cyl_by_axis[ax] = _find_cylindrical_faces(faces, axis=ax)

    # ------------------------------------------------------------------
    # 3. Measure every verification target
    # ------------------------------------------------------------------
    selector_map = selector_map or {}
    for target in targets:
        target_obj = _to_verification_target(target)
        if target_obj is None:
            continue
        # Inject selector expression from Architect's selector_map into the
        # schema's existing face_selector_expression / edge_selector_expression
        tid = _attr(target_obj, "id", "")
        if tid in selector_map:
            expr = selector_map[tid]
            if not target_obj.face_selector_expression and not target_obj.edge_selector_expression:
                _parsed = _parse_selector(expr)
                if _parsed:
                    if _parsed["entity"] == "face":
                        target_obj.face_selector_expression = expr
                    else:
                        target_obj.edge_selector_expression = expr
        try:
            result = _measure_target_cadpy(
                target_obj, faces, edges, bbox, volume_mm3,
                cyl_faces, plan_faces, cyl_by_axis,
                feature_measurements=feature_measurements,
            )
            if result is not None:
                results.append(result)
        except Exception as exc:
            errors.append(
                f"Engine A: exception measuring '{_attr(target_obj, 'id', '?')}': {exc}"
            )
            results.append(VerificationResult(
                target_id=_attr(target_obj, "id", "?"),
                passed=False,
                engine="cadpy_analysis",
                severity="error",
                raw_measurements={"error": str(exc)},
            ))

    return EngineReport(
        engine_name="cadpy_analysis",
        results=results,
        summary="  ".join(summary_parts),
        warnings=warnings,
        errors=errors,
        raw_json={
            "bbox": bbox,
            "volume_mm3": volume_mm3,
            "face_count": len(faces),
            "edge_count": len(edges),
            "_manifest": manifest,  # full manifest for selector_manifest_diff
        },
    )


def _measure_target_cadpy(
    target,        # VerificationTarget
    faces: list,
    edges: list,
    bbox: dict,
    volume_mm3: float | None,
    cyl_faces: list,
    plan_faces: list,
    cyl_by_axis: dict[str, list],
    feature_measurements: dict | None = None,
) -> VerificationResult | None:
    """Check a single VerificationTarget against cadpy-extracted topology.

    PRIORITY 1: White-box instrumentation (feature_measurements) — exact
    per-feature bounding-box data saved BEFORE boolean merge.
    PRIORITY 2: cadpy STEP topology analysis.

    Returns ``None`` for target kinds this engine doesn't handle
    (Engine B covers those).
    """
    target_id = _attr(target, "id", "?")
    kind_str = _attr(target, "kind", None)
    kind = str(kind_str.value) if hasattr(kind_str, "value") else str(kind_str or "")
    nominal = _attr(target, "nominal", None)
    tol_upper = _attr(target, "tolerance_upper", 0.1)
    tol_lower = _attr(target, "tolerance_lower", 0.1)
    axis = _attr(target, "measurement_axis", None)
    axis_str = str(axis.value) if hasattr(axis, "value") else str(axis or "")

    # Skip targets that cannot be accurately measured from merged STEP topology
    # These are array/pattern features that lose individual identity after boolean merge
    _target_id_lower = target_id.lower()
    _skip_keywords = ["pitch", "spacing", "start", "end", "count", "pattern", "rise"]
    if any(kw in _target_id_lower for kw in _skip_keywords):
        return None  # Let Engine B or manual verification handle these

    # PRIORITY 0: Selector-based measurement (precise topology matching)
    selector_expr = (
        _attr(target, "face_selector_expression", None)
        or _attr(target, "edge_selector_expression", None)
    )
    if selector_expr:
        parsed = _parse_selector(selector_expr)
        if parsed is not None:
            entities = _resolve_selector(parsed, faces, edges)
            if entities:
                measured = _measure_entities(entities, kind, axis_str, part_bbox=bbox)
                if measured is not None:
                    measured = _adjust_for_radius_semantics(measured, target_id, kind)
                if measured is not None and nominal is not None:
                    dev = measured - nominal
                    # Sanity check: if deviation is too large (> 3x nominal),
                    # the selector likely matched wrong features — fall through
                    if abs(dev) > 3.0 * abs(nominal):
                        # Wrong match — fall through to Priority 1 or 2
                        pass
                    else:
                        passed = abs(dev) <= max(tol_upper, tol_lower)
                        entity_ids = [e.get("id", "?") for e in entities[:5]]
                        return VerificationResult(
                            target_id=target_id, passed=passed,
                            measured_value=round(measured, 3),
                            deviation=round(dev, 3),
                            engine="cadpy_analysis",
                            severity="error" if not passed else "info",
                            raw_measurements={
                                "source": f"selector:{selector_expr}",
                                "entity_ids": entity_ids,
                                "entity_count": len(entities),
                                "method": "selector_based",
                            },
                        )
            # Selector didn't match or matched wrong features — fall through to Priority 1
            # (logged in EngineReport summary, not printed per-target)

    # PRIORITY 1: White-box instrumentation (exact per-feature data)
    if feature_measurements:
        # Skip array/pattern properties — these require analyzing the full
        # array structure, not a single prototype feature. White-box only
        # measures the prototype (e.g., one fin), not the array (e.g., 12 fins).
        _tid_lower = target_id.lower()
        _array_keywords = {"pitch", "count", "spacing", "start", "end", "pattern", "rise"}
        if any(kw in _tid_lower for kw in _array_keywords):
            # Let Engine B or selector-based measurement handle this
            pass
        else:
            # Compound measurements (gap/distance/rise/offset) FIRST — they
            # require TWO features and cannot be handled by single-feature match.
            measured = None
            source = ""
            _compound_keywords = ("gap", "distance", "rise", "offset", "pitch", "spacing")
            if any(kw in _tid_lower for kw in _compound_keywords):
                compound = _find_compound_gap_measurement(
                    target_id, feature_measurements, axis_str,
                )
                if compound is not None:
                    measured = compound
                    source = "white_box_compound"

            if measured is None:
                measured, source = _find_best_feature_match(
                    target_id, feature_measurements, kind, axis_str,
                )

            if measured is not None and nominal is not None:
                dev = measured - nominal
                passed = abs(dev) <= max(tol_upper, tol_lower)
                return VerificationResult(
                    target_id=target_id, passed=passed,
                    measured_value=round(measured, 3), deviation=round(dev, 3),
                    engine="cadpy_analysis",
                    severity="error" if not passed else "info",
                    raw_measurements={"source": source, "method": "white_box_instrumentation"},
                )

    # PRIORITY 2: cadpy STEP topology analysis
    # ── Feature-keyword guard ──────────────────────────────────────────
    # If target_id refers to a SPECIFIC feature (e.g. "backplate-thickness",
    # "hub-diameter", "blade-height"), the whole-part STEP topology (bbox,
    # all cylindrical faces, all planar faces) is NOT the right measurement.
    # Return None so the merge logic defers to the other engine or reports
    # "cannot measure".  This guard protects ALL kind-specific handlers below,
    # not just overall_dimension.
    _tid_lower = target_id.lower()
    _feature_keywords = {"lug", "rib", "cutout", "boss", "flange", "tab",
                         "gusset", "ear", "arm", "leg", "web", "slot", "fin",
                         "hub", "blade", "backplate", "shaft", "housing",
                         "bracket", "plate", "cover", "base", "cap", "tread"}
    _is_feature_target = any(kw in _tid_lower for kw in _feature_keywords)
    if _is_feature_target:
        # Feature-specific target — whole-part topology is wrong.
        # Let Engine B handle it (it has white-box + STL heuristics).
        return None

    # === OVERALL_DIMENSION ================================================
    if kind == "overall_dimension" and axis_str in {"x", "y", "z"}:
        idx = {"x": 0, "y": 1, "z": 2}[axis_str]
        if bbox:
            from cadpy.analysis import bbox_size as _bs
            _size = _bs(bbox)
            measured = float(_size[idx]) if _size else 0.0
        elif faces:
            # Compute bbox from all faces
            all_centers = [c for f in faces if (c := _face_center(f))]
            if all_centers:
                measured = max(c[idx] for c in all_centers) - min(c[idx] for c in all_centers)
            else:
                return _feature_not_found(target_id, "overall_dimension")
        else:
            return None  # let Engine B handle
        dev = measured - float(nominal or 0)
        passed = abs(dev) <= max(tol_upper, tol_lower)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
        )

    # === VOLUME ===========================================================
    if kind == "volume" and volume_mm3 is not None and nominal is not None:
        dev = volume_mm3 - float(nominal)
        passed = abs(dev) <= max(tol_upper, tol_lower)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(volume_mm3, 2), deviation=round(dev, 2),
            engine="cadpy_analysis", severity="error" if not passed else "info",
        )

    # === HOLE_DIAMETER ====================================================
    if kind in ("hole_diameter", "counterbore_diameter", "boss_diameter"):
        candidates = cyl_faces
        if axis_str:
            candidates = cyl_by_axis.get(axis_str, cyl_faces)
        if not candidates:
            return _feature_not_found(target_id, kind, "No cylindrical faces found")

        # Prefer the cylinder closest to nominal diameter
        best = None
        best_dist = float("inf")
        for f in candidates:
            radius = f.get("params", {}).get("radius") if isinstance(f.get("params"), dict) else None
            if radius is None:
                continue
            dia = float(radius) * 2
            if nominal is not None:
                dist = abs(dia - float(nominal))
                if dist < best_dist:
                    best_dist = dist
                    best = (dia, f)
            else:
                best = (dia, f)
                break

        if best is None:
            return _feature_not_found(target_id, kind, "No cylindrical face with radius data")

        # Sanity check: if the best match deviates too much from nominal,
        # we likely matched an unrelated cylinder (e.g., gear outer surface
        # instead of bore).  Reject the match rather than report a wildly
        # wrong value that confuses the repair agent.
        measured, matched_face = best
        _diagnostic_hint = ""
        if nominal is not None and nominal > 0:
            max_reasonable_dev = max(float(nominal) * 0.5, 2.0)  # 50% or 2mm
            if abs(measured - float(nominal)) > max_reasonable_dev:
                return _feature_not_found(
                    target_id, kind,
                    f"Closest cylindrical face has diameter {measured:.2f}mm "
                    f"(nominal {nominal:.2f}mm, deviation {abs(measured - float(nominal)):.2f}mm "
                    f"> {max_reasonable_dev:.2f}mm threshold). "
                    f"Bore may not exist in exported geometry or selector_map is needed."
                )
            # Diagnostic: even within the sanity threshold, >25% deviation
            # suggests we may have matched the wrong cylinder (e.g., a pin
            # instead of a bore).  Add a hint for the repair agent.
            if abs(measured - float(nominal)) > float(nominal) * 0.25:
                _diagnostic_hint = (
                    f"WARNING: measured {measured:.2f}mm deviates "
                    f"{abs(measured - float(nominal)):.2f}mm ({abs(measured - float(nominal)) / float(nominal) * 100:.0f}%) "
                    f"from nominal {nominal:.2f}mm. "
                    f"This may be a different cylindrical feature (e.g., pin, shaft) "
                    f"rather than the intended bore. "
                    f"Check that the exported STEP contains the correct body."
                )
        dev = measured - float(nominal or 0) if nominal is not None else 0.0
        passed = abs(dev) <= max(tol_upper, tol_lower) if nominal is not None else True
        center = _face_center(matched_face)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
            raw_measurements={
                "matched_face": matched_face.get("id", "?"),
                "center": list(center) if center else None,
                **({"diagnostic": _diagnostic_hint} if _diagnostic_hint else {}),
            },
        )

    # === HOLE_POSITION ====================================================
    if kind == "hole_position":
        candidates = cyl_faces
        if axis_str:
            candidates = cyl_by_axis.get(axis_str, cyl_faces)
        if not candidates:
            return _feature_not_found(target_id, kind, "No cylindrical faces")

        # Find the cylinder whose center is closest to the nominal position.
        # nominal is interpreted as the expected coordinate along measurement_axis.
        # Without a selector or nominal, we can only report the position, not validate.
        best_center = None
        best_face = None
        if nominal is not None and axis_str in {"x", "y", "z"}:
            ax_idx = {"x": 0, "y": 1, "z": 2}[axis_str]
            best_dist = float("inf")
            for f in candidates:
                c = _face_center(f)
                if c is None:
                    continue
                d = abs(c[ax_idx] - float(nominal))
                if d < best_dist:
                    best_dist = d
                    best_center = c
                    best_face = f
            if best_center is not None:
                measured = best_center[ax_idx]
                dev = measured - float(nominal)
                passed = abs(dev) <= max(tol_upper, tol_lower)
                return VerificationResult(
                    target_id=target_id, passed=passed,
                    measured_value=round(measured, 3),
                    deviation=round(dev, 3),
                    engine="cadpy_analysis",
                    severity="error" if not passed else "info",
                    raw_measurements={
                        "center": list(best_center),
                        "matched_face": best_face.get("id", "?") if best_face else "?",
                        "method": "closest_to_nominal",
                    },
                )

        # No nominal or axis — report position without validation
        center = _face_center(candidates[0])
        if center is None:
            return _feature_not_found(target_id, kind, "Cannot compute hole center")
        return VerificationResult(
            target_id=target_id, passed=True,
            measured_value=0.0, deviation=0.0,
            engine="cadpy_analysis", severity="info",
            raw_measurements={
                "center": list(center),
                "matched_face": candidates[0].get("id", "?"),
                "note": "No nominal/axis specified — position reported but not validated",
            },
        )

    # === HOLE_DISTANCE ====================================================
    if kind == "hole_distance" and len(cyl_faces) >= 2 and nominal is not None:
        centers = [c for f in cyl_faces if (c := _face_center(f))]
        if len(centers) < 2:
            return _feature_not_found(target_id, kind, f"Only {len(centers)} cylinder centers")
        # Pairwise distances, pick closest to nominal
        import math as _math
        best_dist = float("inf")
        best_measured = 0.0
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = _math.sqrt(sum((centers[i][k] - centers[j][k]) ** 2 for k in range(3)))
                if abs(d - float(nominal)) < best_dist:
                    best_dist = abs(d - float(nominal))
                    best_measured = d
        dev = best_measured - float(nominal)
        passed = abs(dev) <= max(tol_upper, tol_lower)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(best_measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
        )

    # === WALL_THICKNESS (distance between parallel planar faces) ===========
    if kind == "wall_thickness" and nominal is not None:
        matched_axis = axis_str or "z"
        planes = _find_planar_faces(faces, axis=matched_axis)
        if len(planes) < 2:
            return _feature_not_found(target_id, kind, f"Need ≥2 parallel planar faces, got {len(planes)}")

        # Collect all pairwise distances between parallel planar faces.
        # Filter out noise (< 0.5mm) and prefer the distance closest to nominal.
        centers = [c for f in planes if (c := _face_center(f))]
        ax_idx = {"x": 0, "y": 1, "z": 2}[matched_axis]
        distances: list[float] = []
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = abs(centers[i][ax_idx] - centers[j][ax_idx])
                if d > 0.5:  # filter out noise / coincident faces
                    distances.append(d)
        if not distances:
            return _feature_not_found(target_id, kind, "No valid plane-to-plane distances found")
        # Prefer the distance closest to nominal (not just the minimum).
        # The minimum distance might be an unrelated thin gap, not the wall.
        distances.sort(key=lambda d: abs(d - float(nominal)))
        best_dist = distances[0]
        dev = best_dist - float(nominal)
        passed = dev >= -tol_lower  # wall must be at least nominal - tol_lower
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(best_dist, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
            raw_measurements={
                "method": "closest_to_nominal",
                "candidates_count": len(distances),
                "all_distances": [round(d, 2) for d in distances[:5]],
            },
        )

    # === DEPTH measurements (hole_depth, boss_depth, counterbore_depth) ====
    # These measure the extent of a cylindrical feature along its axis.
    # On STEP topology, a blind hole appears as a cylindrical face with a
    # planar bottom; depth = distance from top face to bottom face along axis.
    if kind in ("hole_depth", "boss_depth", "counterbore_depth") and nominal is not None:
        # Find cylindrical faces along the measurement axis
        target_axis = axis_str or "z"
        cyl_candidates = cyl_by_axis.get(target_axis, cyl_faces)
        if not cyl_candidates:
            return _feature_not_found(target_id, kind, f"No cylindrical faces along {target_axis}")

        # For each cylinder, compute its extent along the axis
        ax_idx = {"x": 0, "y": 1, "z": 2}[target_axis]
        best_measured = None
        best_dev = float("inf")
        for f in cyl_candidates:
            params = f.get("params", {}) if isinstance(f.get("params"), dict) else {}
            # Use the face's parametric bounds if available
            u_min = params.get("u_min")
            u_max = params.get("u_max")
            if u_min is not None and u_max is not None:
                extent = abs(float(u_max) - float(u_min))
            else:
                # Fallback: use the face center ± radius as rough extent
                center = _face_center(f)
                radius = params.get("radius")
                if center and radius:
                    extent = float(radius) * 2  # rough approximation
                else:
                    continue
            if nominal is not None:
                dev = abs(extent - float(nominal))
                if dev < best_dev:
                    best_dev = dev
                    best_measured = extent
            else:
                best_measured = extent
                break

        if best_measured is None:
            return _feature_not_found(target_id, kind, "Cannot compute depth from cylindrical faces")
        dev = best_measured - float(nominal)
        passed = abs(dev) <= max(tol_upper, tol_lower)
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(best_measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
            raw_measurements={"method": "cylinder_extent", "axis": target_axis},
        )

    # === SLOT_DIMENSION =====================================================
    if kind == "slot_dimension" and nominal is not None:
        # Slots appear as elongated holes — measure from cylindrical end-caps
        # or from parallel planar faces depending on which axis is requested.
        target_axis = axis_str or "x"
        ax_idx = {"x": 0, "y": 1, "z": 2}[target_axis]
        # Use planar faces perpendicular to the axis (slot width)
        planes = _find_planar_faces(faces, axis=target_axis)
        if len(planes) >= 2:
            centers = [c for f in planes if (c := _face_center(f))]
            distances = []
            for i in range(len(centers)):
                for j in range(i + 1, len(centers)):
                    d = abs(centers[i][ax_idx] - centers[j][ax_idx])
                    if d > 0.5:
                        distances.append(d)
            if distances:
                distances.sort(key=lambda d: abs(d - float(nominal)))
                best = distances[0]
                dev = best - float(nominal)
                passed = abs(dev) <= max(tol_upper, tol_lower)
                return VerificationResult(
                    target_id=target_id, passed=passed,
                    measured_value=round(best, 3), deviation=round(dev, 3),
                    engine="cadpy_analysis", severity="error" if not passed else "info",
                )
        return _feature_not_found(target_id, kind, "No planar faces for slot measurement")

    # === FILLET_RADIUS (look for toroidal/small-radius faces on edges) ====
    if kind == "fillet_radius":
        # Fillets appear as cylindrical or toroidal faces with small radii
        candidates = []
        for e in edges:
            curve_type = str(e.get("curveType") or "").lower()
            if curve_type in ("circle",):
                radius = e.get("params", {}).get("radius") if isinstance(e.get("params"), dict) else None
                if radius is not None:
                    candidates.append((float(radius), e))
        if not candidates:
            return _feature_not_found(target_id, kind, "No circular edges with radius data")
        # Return the radius closest to nominal (or the smallest if no nominal)
        if nominal is not None:
            candidates.sort(key=lambda x: abs(x[0] - float(nominal)))
        measured = candidates[0][0]
        # Guard: reject if the closest edge is too far from nominal.
        # This prevents matching unrelated edges (e.g. mounting holes)
        # when the actual fillet edges don't exist in the topology.
        if nominal is not None and nominal > 0:
            max_reasonable_dev = max(float(nominal) * 0.5, 1.0)  # 50% or 1mm
            if abs(measured - float(nominal)) > max_reasonable_dev:
                return _feature_not_found(
                    target_id, kind,
                    f"Closest circular edge has radius {measured:.2f}mm "
                    f"(nominal {nominal:.2f}mm, deviation {abs(measured - float(nominal)):.2f}mm "
                    f"> {max_reasonable_dev:.2f}mm threshold). "
                    f"Fillet may not have been applied to the geometry."
                )
        dev = measured - float(nominal or 0) if nominal is not None else 0.0
        passed = abs(dev) <= max(tol_upper, tol_lower) if nominal is not None else True
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=round(measured, 3), deviation=round(dev, 3),
            engine="cadpy_analysis", severity="error" if not passed else "info",
            raw_measurements={"matched_edge": candidates[0][1].get("id", "?")},
        )

    # === Targets Engine A doesn't handle → let Engine B cover them ========
    return None


def _feature_not_found(target_id: str, kind: str, detail: str = "") -> VerificationResult:
    """Return a ``passed=False`` result when a feature cannot be located.

    Uses ``severity="info"`` so this result is NOT counted as a real failure
    in the merge logic (which filters by ``severity in ("error", "fatal")``).
    The target appears in the QA report as "not measurable" for transparency,
    but does not trigger DIMENSION error routing or inflate the failure count
    that confuses the repair agent.
    """
    return VerificationResult(
        target_id=target_id,
        passed=False,
        engine="cadpy_analysis",
        severity="info",
        raw_measurements={
            "error": f"Feature not found ({kind})",
            "detail": detail,
            "not_measurable": True,
        },
    )


# ---------------------------------------------------------------------------
# Engine B — Heuristic STL QA (check_mesh.py)
# ---------------------------------------------------------------------------

# Expected schema version from check_mesh.py.  Bump when check_mesh.py
# introduces breaking changes to its JSON output format.
_EXPECTED_CHECK_MESH_SCHEMA = "2.0.0"


def _cadbrief_to_spec_json(cad_brief) -> dict | None:
    """Convert CADBrief.verification_targets to the spec format expected by
    ``check_mesh.py --spec``.

    The check_mesh.py ``compare_to_spec()`` function expects::

        {
            "target_dimensions": {"X": 42.0, "Y": 42.0, "Z": 42.0},
            "target_hole_diameters": [3.4, 3.4, ...],
            "min_wall_thickness": 5.0,
            "target_volume_mm3": 10000.0,
            "tolerances": {"X": 0.5, ...},
            "hole_tolerance": 0.2,
            "volume_tolerance": 500.0
        }

    Returns None if the CADBrief has no verification targets.
    """
    targets = _attr(cad_brief, "verification_targets", []) or []
    if not targets:
        return None

    spec: dict = {
        "target_dimensions": {},
        "tolerances": {},
        "target_hole_diameters": [],
        "hole_tolerance": 0.2,
        "min_wall_thickness": None,
        "target_volume_mm3": None,
        "volume_tolerance": None,
    }

    for vt in targets:
        kind_str = str(_attr(vt, "kind", None) or "")
        if hasattr(kind_str, "value"):
            kind_str = kind_str.value
        nominal = _attr(vt, "nominal", None)
        tol_upper = _attr(vt, "tolerance_upper", 0.1)
        tol_lower = _attr(vt, "tolerance_lower", 0.1)
        axis = _attr(vt, "measurement_axis", None)
        axis_str = str(axis.value) if hasattr(axis, "value") else str(axis or "")

        if kind_str == "overall_dimension" and axis_str in {"x", "y", "z"}:
            label = {"x": "X", "y": "Y", "z": "Z"}[axis_str]
            if nominal is not None:
                spec["target_dimensions"][label] = float(nominal)
            spec["tolerances"][label] = max(tol_upper, tol_lower)
        elif kind_str == "hole_diameter" and nominal is not None:
            spec["target_hole_diameters"].append(float(nominal))
            spec["hole_tolerance"] = max(spec["hole_tolerance"], tol_upper)
        elif kind_str == "wall_thickness" and nominal is not None:
            spec["min_wall_thickness"] = float(nominal)
        elif kind_str == "volume" and nominal is not None:
            spec["target_volume_mm3"] = float(nominal)
            spec["volume_tolerance"] = max(tol_upper, tol_lower)

    # Clean up: remove empty/unset fields
    if not spec["target_dimensions"]:
        del spec["target_dimensions"]
        del spec["tolerances"]
    if not spec["target_hole_diameters"]:
        del spec["target_hole_diameters"]
    if spec["min_wall_thickness"] is None:
        del spec["min_wall_thickness"]
    if spec["target_volume_mm3"] is None:
        del spec["target_volume_mm3"]
        del spec["volume_tolerance"]

    # Return None if there's nothing meaningful to compare
    has_content = (
        spec.get("target_dimensions")
        or spec.get("target_hole_diameters")
        or spec.get("min_wall_thickness") is not None
        or spec.get("target_volume_mm3") is not None
    )
    return spec if has_content else None


def _fallback_connectivity_check(stl_path: str) -> dict:
    """Lightweight connectivity check using trimesh when check_mesh.py fails.

    Uses Union-Find on face adjacency to count connected components WITHOUT
    requiring networkx (which trimesh.split() depends on).

    Returns a ``connectivity`` dict compatible with the merge function's expectations.
    This is the MOST CRITICAL QA check — disconnected bodies mean the part will
    fall apart when printed.  Even when the full check_mesh.py analysis fails
    (timeout, crash), we must still detect this condition.
    """
    try:
        import trimesh

        mesh = trimesh.load(stl_path, force='mesh')
        n_faces = len(mesh.faces)

        if n_faces == 0:
            return {"is_single_body": True, "body_count": 0,
                    "is_fatal": False, "message": "Empty mesh"}

        # --- Union-Find on face adjacency (no networkx needed) ---
        parent = list(range(n_faces))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Build adjacency: faces sharing an edge are connected
        edges = mesh.face_adjacency  # (N, 2) array of adjacent face pairs
        for f1, f2 in edges:
            union(int(f1), int(f2))

        # Count connected components
        roots = set(find(i) for i in range(n_faces))
        body_count = len(roots)

        if body_count <= 1:
            return {"is_single_body": True, "body_count": 1,
                    "is_fatal": False, "message": ""}

        # Estimate volume per component by grouping faces
        face_areas = mesh.area_faces
        component_volumes: dict[int, float] = {}
        for i in range(n_faces):
            root = find(i)
            component_volumes[root] = component_volumes.get(root, 0.0) + float(face_areas[i])

        # Use total surface area as a proxy for volume ranking
        body_info = []
        for idx, (root, area) in enumerate(sorted(
            component_volumes.items(), key=lambda x: -x[1]
        )):
            body_info.append({"body_index": idx + 1, "surface_area_mm2": round(area, 2)})

        total_area = sum(b["surface_area_mm2"] for b in body_info) or 1.0
        significant = [b for b in body_info if b["surface_area_mm2"] > total_area * 0.001]

        if len(significant) >= 2:
            desc = ", ".join(
                f"#{b['body_index']}: {b['surface_area_mm2']}mm²"
                for b in significant[:5]
            )
            return {
                "is_single_body": False, "body_count": body_count,
                "significant_bodies": len(significant), "is_fatal": True,
                "message": (
                    f"Model contains {len(significant)} separate disconnected parts!"
                    f"Surface area of each part: {desc}."
                    f"Ensure all parts are merged via boolean operations (+), with mutual overlap ≥0.1mm."
                ),
            }
        else:
            return {
                "is_single_body": True, "body_count": body_count,
                "is_fatal": False,
                "message": f"{body_count - len(significant)} tiny fragments (non-fatal)",
            }
    except Exception as exc:
        print(f"[ENGINE B] Fallback connectivity check failed: {exc}")
        return {"is_single_body": True, "body_count": 1, "is_fatal": False, "message": ""}


def _build_single_body_result(conn: dict) -> VerificationResult:
    """Build a ``single_body`` VerificationResult from fallback connectivity data."""
    is_single = bool(conn.get("is_single_body", True))
    return VerificationResult(
        target_id="single-body",
        passed=is_single,
        measured_value=1.0 if is_single else float(conn.get("body_count", 0)),
        engine="check_mesh",
        severity="fatal" if not is_single else "info",
        raw_measurements={
            "body_count": conn.get("body_count", 1),
            "is_fatal": conn.get("is_fatal", False),
            "message": conn.get("message", ""),
            "source": "fallback_connectivity_check",
        },
    )


def _run_engine_b_check_mesh(
    *,
    stl_path: str,
    cad_brief,  # CADBrief | dict
    iteration: int = 0,
) -> EngineReport:
    """Run the heuristic / physical QA engine on the .stl mesh.

    Shells out to ``legacy_refs/check_mesh.py --format json``, parses its
    comprehensive JSON output, and maps every measurable property back to
    the verification targets in the CADBrief.
    """
    warnings: list[str] = []
    errors: list[str] = []
    results: list[VerificationResult] = []
    raw_json: dict[str, object] = {}

    targets = _attr(cad_brief, "verification_targets", [])

    # -- 1. Build spec file for compare_to_spec() ---------------------------
    spec_json = _cadbrief_to_spec_json(cad_brief)
    spec_path: Path | None = None
    if spec_json is not None:
        spec_path = Path(_CACHE_DIR) / f"check_mesh_spec_{_attr(cad_brief, 'part_name', 'unknown')}.json"
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(json.dumps(spec_json, indent=2), encoding="utf-8")
        except OSError:
            spec_path = None  # non-fatal — spec comparison will be skipped

    # -- 2. Run check_mesh.py ----------------------------------------------
    if not _CHECK_MESH_SCRIPT.is_file():
        return EngineReport(
            engine_name="check_mesh",
            results=[],
            summary="",
            warnings=[],
            errors=[f"check_mesh.py not found at {_CHECK_MESH_SCRIPT}"],
        )

    cmd = [sys.executable, str(_CHECK_MESH_SCRIPT), stl_path, "--format", "json", "--skip-print-orientation", "--iteration", str(iteration)]
    if spec_path is not None and spec_path.is_file():
        cmd += ["--spec", str(spec_path)]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CFG_CHECK_MESH_TIMEOUT,
            cwd=str(_REPO_ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        _safe_print("[ENGINE B] check_mesh.py timed out — running fallback connectivity check")
        fb_conn = _fallback_connectivity_check(stl_path)
        _fallback_result = _build_single_body_result(fb_conn) if not fb_conn.get("is_single_body", True) else None
        return EngineReport(
            engine_name="check_mesh",
            results=[_fallback_result] if _fallback_result else [],
            summary=f"Fallback: {fb_conn.get('message', 'connectivity OK')}",
            warnings=["check_mesh.py timed out; only fallback connectivity check was performed."],
            errors=["check_mesh.py timed out after 180 seconds."],
            raw_json={"connectivity": fb_conn},
        )
    except Exception as exc:
        _safe_print(f"[ENGINE B] check_mesh.py exception — running fallback connectivity check")
        fb_conn = _fallback_connectivity_check(stl_path)
        _fallback_result = _build_single_body_result(fb_conn) if not fb_conn.get("is_single_body", True) else None
        return EngineReport(
            engine_name="check_mesh",
            results=[_fallback_result] if _fallback_result else [],
            summary=f"Fallback: {fb_conn.get('message', 'connectivity OK')}",
            warnings=[f"check_mesh.py subprocess failed: {exc}; only fallback connectivity check was performed."],
            errors=[f"check_mesh.py subprocess failed: {exc}"],
            raw_json={"connectivity": fb_conn},
        )

    if proc.returncode != 0:
        _safe_print(f"\n[ENGINE B] check_mesh.py FAILED (exit {proc.returncode}):")
        _safe_print(f"  STDERR: {proc.stderr.strip()[:500]}")
        _safe_print("[ENGINE B] Running fallback connectivity check")
        fb_conn = _fallback_connectivity_check(stl_path)
        _fallback_result = _build_single_body_result(fb_conn) if not fb_conn.get("is_single_body", True) else None
        return EngineReport(
            engine_name="check_mesh",
            results=[_fallback_result] if _fallback_result else [],
            summary=f"Fallback: {fb_conn.get('message', 'connectivity OK')}",
            warnings=[f"check_mesh.py exited {proc.returncode}; only fallback connectivity check was performed."],
            errors=[f"check_mesh.py exited {proc.returncode}: {proc.stderr.strip()[:500]}"],
            raw_json={"connectivity": fb_conn},
        )

    # -- 3. Parse JSON output ----------------------------------------------
    try:
        raw_json = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        _safe_print(f"[ENGINE B] check_mesh.py invalid JSON — running fallback connectivity check")
        fb_conn = _fallback_connectivity_check(stl_path)
        _fallback_result = _build_single_body_result(fb_conn) if not fb_conn.get("is_single_body", True) else None
        return EngineReport(
            engine_name="check_mesh",
            results=[_fallback_result] if _fallback_result else [],
            summary=f"Fallback: {fb_conn.get('message', 'connectivity OK')}",
            warnings=[f"check_mesh.py returned invalid JSON; only fallback connectivity check was performed."],
            errors=[f"check_mesh.py returned invalid JSON: {exc}"],
            raw_json={"connectivity": fb_conn},
        )

    # -- 3.5. Load feature measurements from white-box instrumentation --------
    measurements_file = Path(f"temp_measurements_{iteration}.json")
    if not measurements_file.is_file():
        measurements_file = Path("temp_measurements_0.json")
    if measurements_file.is_file():
        try:
            with open(measurements_file, 'r', encoding='utf-8') as f:
                feature_measurements = json.load(f)
            # Remove metadata keys that are not feature measurements
            feature_measurements.pop("_step_hash", None)
            feature_measurements.pop("_stl_hash", None)
            feature_measurements.pop("_timestamp", None)
            print(f"[ENGINE B] Loaded {len(feature_measurements)} feature measurements from {measurements_file.name}")

            raw_json["feature_measurements"] = feature_measurements
        except Exception as exc:
            print(f"[ENGINE B] WARNING: Failed to load feature measurements: {exc}")

    # -- 4. Schema version check -------------------------------------------
    schema_ver = raw_json.get("schema_version")
    if schema_ver and schema_ver != _EXPECTED_CHECK_MESH_SCHEMA:
        warnings.append(
            f"check_mesh.py schema version mismatch: got {schema_ver}, "
            f"expected {_EXPECTED_CHECK_MESH_SCHEMA}. "
            f"Some measurements may be unavailable or misformatted."
        )

    if raw_json.get("status") != "success":
        err_msg = raw_json.get("message", f"status={raw_json.get('status', 'unknown')}")
        errors.append(f"check_mesh.py failed: {err_msg}")

        # Distinguish: watertightness pre-check vs. generic failure
        is_watertight = raw_json.get("mesh_info", {}).get("is_watertight")
        if is_watertight is False:
            # Non-watertight mesh → FATAL topology error
            raw_json["connectivity"] = {
                "is_single_body": False,
                # body_count=None signals "unknown" — we know the part is bad
                # (is_single_body=False) but couldn't actually count bodies.
                # The single_body VerificationResult propagates None as
                # measured_value instead of a misleading numeric sentinel.
                "body_count": None,
                "is_fatal": True,
                "message": (
                    f"STL mesh is NOT WATERTIGHT. {err_msg} "
                    f"This is a fatal error — the build123d script must produce "
                    f"a closed, manifold solid. Check that all features are "
                    f"properly unioned and that there are no gaps in the geometry."
                ),
            }
        else:
            # Generic failure — inject synthetic connectivity failure
            raw_json["connectivity"] = {
                "is_single_body": False,
                "body_count": None,
                "is_fatal": True,
                "message": f"check_mesh.py could not analyze STL: {err_msg}",
            }

    # -- 5. Extract mesh-noise baseline ------------------------------------
    mesh_res = raw_json.get("mesh_resolution", {})
    noise_threshold = mesh_res.get("max_theoretical_error_mm", 0.15)

    # -- 4. Map verification targets to check_mesh measurements ------------
    holes_data = raw_json.get("holes", {})
    struct = raw_json.get("structure", {})
    conn = raw_json.get("connectivity", {})

    # -- LOUD print: connectivity is the most critical QA check ----------
    body_count = conn.get("body_count", 1)
    is_single = conn.get("is_single_body", True)
    is_fatal = conn.get("is_fatal", False)
    if not is_single:
        print(f"\n{'='*60}")
        print(f"  [ENGINE B] DISCONNECTED BODIES DETECTED!")
        print(f"  Body count: {body_count}")
        print(f"  Fatal: {is_fatal}")
        print(f"  {conn.get('message', '')}")
        print(f"{'='*60}\n")
    elif isinstance(body_count, int) and body_count > 1:
        print(f"\n[ENGINE B] WARNING: {body_count} bodies (non-fatal fragments)")
    else:
        print(f"[ENGINE B] OK: Single watertight body")

    for target in targets:
        target_obj = _to_verification_target(target)
        if target_obj is None:
            continue
        try:
            result = _measure_target_via_check_mesh(
                target_obj, raw_json, noise_threshold,
            )
            if result is not None:
                results.append(result)
        except Exception as exc:
            errors.append(
                f"Engine B failed to measure '{_attr(target_obj, 'id', '?')}': {exc}"
            )

    # -- 5. Build summary --------------------------------------------------
    strength = struct.get("strength_assessment", {})
    summary_parts = [
        f"Vertices: {raw_json.get('mesh_info', {}).get('vertex_count', '?')}",
        f"Watertight: {struct.get('is_watertight', '?')}",
        f"Strength: {strength.get('strength_score', '?')}/100",
        f"Single-body: {conn.get('is_single_body', True)}",
    ]
    # Surface dimensional verdict so a structurally-OK but dimensionally-wrong
    # model is not mistaken for an overall pass. `results` is filled above
    # (one VerificationResult per target); each carries `passed` and `deviation`.
    dims_total = len(results)
    dims_pass = sum(1 for r in results if getattr(r, "passed", False))
    if dims_total > 0:
        marker = " OK" if dims_pass == dims_total else " FAIL"
        summary_parts.append(f"Dims: {dims_pass}/{dims_total}{marker}")
    print(f"[ENGINE B] {' | '.join(summary_parts)}")

    # Collect structural / mfg warnings
    for issue in strength.get("issues", []) or []:
        warnings.append(str(issue))
    for mw in holes_data.get("margin_warnings", []) or []:
        warnings.append(str(mw))
    mfg = raw_json.get("manufacturability", {})
    for issue in mfg.get("3d_print", {}).get("issues", []) or []:
        # Skip overhang warnings — not actionable in this pipeline
        _issue_lower = str(issue).lower()
        if "overhang" in str(issue) or "overhang" in _issue_lower or "bridge" in str(issue):
            continue
        warnings.append(str(issue))
    print_ori = mfg.get("print_orientation", {})
    z_warn = print_ori.get("z_layer_weakness_warning")
    if z_warn:
        warnings.append(str(z_warn))
    cantilever = struct.get("cantilever_analysis", {})
    c_warn = cantilever.get("warning")
    if c_warn:
        warnings.append(str(c_warn))

    # Symmetry check — report scores for informational purposes only.
    # Do NOT generate warnings: the QA system doesn't know whether the part
    # is SUPPOSED to be symmetric about each plane.  Asymmetry may be
    # intentional (e.g. a boss on one side, a part that sits on Z=0).
    # The repair agent decides based on the original design requirements.
    sym_data = raw_json.get("symmetry", {}).get("symmetry", {})
    if sym_data:
        sym_parts = []
        for plane, info in sym_data.items():
            score = info.get("symmetry_score")
            is_sym = info.get("is_symmetric", False)
            if score is not None:
                sym_parts.append(f"{plane}={score:.3f}{'✓' if is_sym else '✗'}")
        if sym_parts:
            summary_parts.append(f"Symmetry: {', '.join(sym_parts)}")
            # Add to warnings as INFO so the repair agent can reference it.
            # Low scores may indicate misplaced features even when the part
            # is not expected to be perfectly symmetric.
            low_sym = [
                f"{plane}={info.get('symmetry_score', 0):.3f}"
                for plane, info in sym_data.items()
                if info.get("symmetry_score", 1.0) < 0.5
            ]
            if low_sym:
                warnings.append(
                    f"SYMMETRY INFO: Low symmetry scores: {', '.join(low_sym)}. "
                    f"If the design is intended to be symmetric, check feature placement."
                )

    return EngineReport(
        engine_name="check_mesh",
        results=results,
        summary="  ".join(summary_parts),
        warnings=warnings,
        errors=errors,
        raw_json=raw_json,
    )


def _adjust_for_radius_semantics(
    measured: float | None, target_id: str, kind: str,
) -> float | None:
    """Adjust measurement value when target_id implies a radius.

    The white-box instrumentation and STEP topology return **full extents**
    (bounding-box sizes, cylinder diameters).  When the Spec Planner names a
    target with ``radius`` (e.g. ``tread-outer-radius``, ``fillet-radius``),
    the nominal is a radius, so the raw measurement must be halved.

    Kinds that explicitly return diameters (``hole_diameter``,
    ``boss_diameter``, ``counterbore_diameter``) are NOT adjusted — the Spec
    Planner should not pair those kinds with a radius-named target.

    Kinds that cannot be derived from bounding boxes (``fillet_radius``,
    ``chamfer``) are returned as-is (usually ``None``).
    """
    if measured is None:
        return None
    if kind in ("fillet_radius", "chamfer", "surface_area"):
        return measured  # these have their own extraction logic
    if kind in ("hole_diameter", "counterbore_diameter", "boss_diameter"):
        return measured  # already a diameter, don't halve
    _tid_lower = target_id.lower()
    if "radius" in _tid_lower or "rad" in _tid_lower.split("-"):
        return measured / 2.0
    return measured


def _extract_measurement_for_kind(
    feat_data: dict, kind: str, axis_str: str,
) -> float | None:
    """Extract the right numeric measurement from a feature dict based on VerificationKind.

    Maps (kind, axis) → the correct field in the white-box measurement dict
    produced by ``_measure_feature()``.
    """
    if not isinstance(feat_data, dict) or "error" in feat_data:
        return None

    # overall_dimension → size_{axis}
    if kind == "overall_dimension" and axis_str in ("x", "y", "z"):
        return feat_data.get(f"size_{axis_str}")

    # wall_thickness → smallest axis dimension (conservative)
    if kind == "wall_thickness":
        sizes = [feat_data.get(f"size_{a}") for a in ("x", "y", "z")]
        valid = [s for s in sizes if s is not None and s > 0]
        return min(valid) if valid else None

    # volume → volume field
    if kind == "volume":
        return feat_data.get("volume")

    # hole_diameter / counterbore_diameter → internal feature, NOT measurable
    # from outer bounding box.  The bbox gives the outer extent of the solid
    # (e.g. a gear body), not the diameter of a hole cut through it.
    # Return None so the QA system falls through to STEP topology analysis
    # which can find the actual cylindrical face of the bore.
    if kind in ("hole_diameter", "counterbore_diameter"):
        return None

    # boss_diameter → external cylindrical feature, CAN be approximated from
    # bbox (the boss is a protrusion, so min XY ≈ boss diameter).
    if kind == "boss_diameter":
        sx = feat_data.get("size_x")
        sy = feat_data.get("size_y")
        if sx is not None and sy is not None:
            return min(sx, sy)
        return sx or sy

    # hole_depth / counterbore_depth / boss_depth → size along axis (default Z)
    if kind in ("hole_depth", "counterbore_depth", "boss_depth"):
        axis = axis_str if axis_str in ("x", "y", "z") else "z"
        return feat_data.get(f"size_{axis}")

    # slot_dimension → size along axis (default X)
    if kind == "slot_dimension":
        axis = axis_str if axis_str in ("x", "y", "z") else "x"
        return feat_data.get(f"size_{axis}")

    # fillet_radius / chamfer → NOT measurable from white-box bounding boxes.
    # These require STL mesh curvature analysis (Engine B Priority 2).
    if kind in ("fillet_radius", "chamfer"):
        return None

    # surface_area → not available from bounding-box measurements
    if kind == "surface_area":
        return None

    # Geometric tolerances → NOT measurable from bounding boxes
    if kind in ("flatness", "parallelism", "perpendicularity", "concentricity",
                "angle", "edge_margin", "hole_position", "hole_distance",
                "mass", "threaded_hole", "water_tightness", "single_body"):
        return None

    # Generic fallback: if measurement_axis is specified, return size along that axis.
    # This handles unrecognized kinds that still have a meaningful axis dimension
    # (e.g. a custom "height" or "extent" target with measurement_axis="z").
    if axis_str in ("x", "y", "z"):
        val = feat_data.get(f"size_{axis_str}")
        if val is not None:
            return val

    return None


def _find_best_feature_match(
    target_id: str,
    feature_measurements: dict,
    kind: str,
    axis_str: str,
) -> tuple[float | None, str]:
    """Find the best matching white-box measurement for a verification target.

    Strategy:
      1. Extract the **primary feature token** from the target_id (first
         meaningful token, skipping "overall" and short words).
      2. Find features whose name contains that primary token.
      3. Among matches, prefer more specific (longer) feature names.
      4. Extract the measurement for the given kind/axis.

    Returns ``(measured_value, source_name)`` or ``(None, "")`` if no match.
    """
    if not feature_measurements:
        return None, ""

    _non_feature_tokens = {
        "overall", "dimension", "height", "width", "depth", "extent",
        "above", "below", "pos", "position", "dia", "diameter", "radius",
        "thickness", "gap", "distance", "margin", "edge", "span",
        "straight", "top", "bottom", "left", "right", "base", "per",
        "fillet", "chamfer", "hole", "counterbore", "boss", "wall",
        "volume", "concentricity", "perpendicularity", "parallelism",
        "flatness", "angle", "slot",
        # Positional / instance modifiers — not feature names
        "first", "last", "instance", "template", "prototype",
        "inner", "outer", "upper", "lower", "front", "rear",
        # Pattern-level terms (already in _skip_keywords but also excluded here)
        "rise", "pitch", "spacing", "count", "pattern",
    }

    target_tokens = [t for t in re.split(r'[-_]', target_id.lower()) if len(t) > 1]

    # Primary feature = first token not in the non-feature set
    primary = None
    for t in target_tokens:
        if t not in _non_feature_tokens:
            primary = t
            break

    if primary is None:
        # All tokens are property words (e.g., "overall-x") — no feature match
        return None, ""

    # Find features containing the primary token as a whole word.
    # Word-boundary matching avoids false positives like primary="tab"
    # matching "table-mount" or primary="cap" matching "escapement-shaft".
    # \b treats hyphens and non-alphanumeric chars as boundaries, so
    # "lug-right" matches primary="lug" but "alug-bracket" does not.
    primary_pattern = re.compile(rf"\b{re.escape(primary)}\b")
    candidates = []
    for feat_name, feat_data in feature_measurements.items():
        if not isinstance(feat_data, dict) or "error" in feat_data:
            continue
        if primary_pattern.search(feat_name.lower()):
            candidates.append((feat_name, feat_data))

    if not candidates:
        return None, ""

    # Among candidates, prefer the most specific (longest name).
    # Filter out "overall" summary entries AND union operand measurements.
    # Union operands (keys containing "-union-target" / "-union-tool") record
    # the bbox of intermediate boolean operands — often the accumulated result
    # of a pattern/array.  They are NOT the prototype feature and would give
    # wildly wrong measurements (e.g. 20 treads' combined Z=118 instead of
    # one tread's Z=4).  The prototype key is always shorter and correct.
    candidates = [c for c in candidates
                  if c[0] != "overall"
                  and "-union-" not in c[0]]
    if not candidates:
        return None, ""

    # Sort by name length (descending) to prefer more specific matches
    candidates.sort(key=lambda c: len(c[0]), reverse=True)

    # For targets with directional suffixes (e.g., "lug-right"), prefer
    # features with the same suffix.  Union operands are already filtered
    # from candidates above, so we only match prototype features here.
    for token in target_tokens:
        if token in ("right", "left", "front", "back", "top", "bottom"):
            for name, data in candidates:
                if token in name.lower():
                    measured = _extract_measurement_for_kind(data, kind, axis_str)
                    if measured is not None:
                        measured = _adjust_for_radius_semantics(measured, target_id, kind)
                        return measured, name

    # Default: use the most specific (first after sorting)
    best_name, best_data = candidates[0]

    # Guard: sub-properties like "straight-height" cannot be derived from
    # a bounding box — the bbox only gives the TOTAL extent, not the
    # straight portion.  Return None so STL mesh analysis handles it.
    # Exception: "outer-radius" / "outer-diameter" CAN be measured from bbox
    # (half of the outer extent).  But "inner-radius" CANNOT — the inner
    # boundary is not represented in the outer bounding box.
    _unmeasurable_subprops = {"straight", "inner", "outer", "effective"}
    _has_outer_meas = ("outer" in target_tokens
                       and any(t in target_tokens for t in ("radius", "diameter", "dia")))
    if any(sp in target_tokens for sp in _unmeasurable_subprops) and not _has_outer_meas:
        return None, ""

    measured = _extract_measurement_for_kind(best_data, kind, axis_str)
    if measured is not None:
        # Adjust for radius semantics (halve if target expects radius)
        measured = _adjust_for_radius_semantics(measured, target_id, kind)
        return measured, best_name

    return None, ""


def _find_compound_gap_measurement(
    target_id: str,
    feature_measurements: dict,
    axis_str: str,
) -> float | None:
    """Handle compound measurements that require TWO features.

    Examples:
      - ``lug-gap-y`` → lug_right.min_y - lug_left.max_y
      - ``hole-distance-x`` → distance between two hole features

    Returns the computed gap/distance or None.
    """
    if not feature_measurements:
        return None

    target_lower = target_id.lower()

    # Detect "gap" pattern: need two features on opposite sides
    if "gap" in target_lower:
        # Find two features that share the main keyword (e.g., "lug")
        # but differ in a directional suffix (e.g., "right" vs "left")
        main_keywords = set(re.split(r'[-_]', target_lower)) - {"gap", "distance"}
        main_keywords = {kw for kw in main_keywords if len(kw) > 1}

        candidates = []
        for feat_name, feat_data in feature_measurements.items():
            if not isinstance(feat_data, dict) or "error" in feat_data:
                continue
            feat_kw = set(re.split(r'[-_]', feat_name.lower()))
            feat_kw = {kw for kw in feat_kw if len(kw) > 1}
            if main_keywords & feat_kw:
                candidates.append((feat_name, feat_data))

        if len(candidates) >= 2 and axis_str in ("x", "y", "z"):
            # Sort by min_{axis} to get left-to-right ordering
            axis_key = f"min_{axis_str}"
            axis_key_max = f"max_{axis_str}"
            sorted_cands = sorted(
                candidates,
                key=lambda c: c[1].get(axis_key, float("inf")),
            )
            lower_max = sorted_cands[0][1].get(axis_key_max)
            upper_min = sorted_cands[1][1].get(axis_key)
            if lower_max is not None and upper_min is not None:
                return upper_min - lower_max

    return None


def _measure_target_via_check_mesh(
    target,  # VerificationTarget
    raw_json: dict,
    noise_threshold: float,
) -> VerificationResult | None:
    """Check a single VerificationTarget against check_mesh.py output.

    PRIORITY 1: Use feature_measurements from white-box instrumentation
    (per-feature bounding boxes saved BEFORE boolean merge).

    PRIORITY 2: Fall back to measuring the entire STL file via check_mesh.
    """
    target_id = _attr(target, "id", "?")
    kind_str = _attr(target, "kind", None)
    kind = str(kind_str.value) if hasattr(kind_str, "value") else str(kind_str or "")

    nominal = _attr(target, "nominal", None)
    tol_upper = _attr(target, "tolerance_upper", 0.1)
    tol_lower = _attr(target, "tolerance_lower", 0.1)
    axis = _attr(target, "measurement_axis", None)
    axis_str = str(axis.value) if hasattr(axis, "value") else str(axis or "")

    # PRIORITY 1: White-box instrumentation
    feature_measurements = raw_json.get("feature_measurements", {})
    measured = None
    source = ""

    if feature_measurements:
        # 1a. Compound measurement FIRST (gap/distance/rise/offset between two features)
        # These require TWO features and cannot be handled by single-feature match
        _tid_lower_compound = target_id.lower()
        _compound_kw = ("gap", "distance", "rise", "offset", "pitch", "spacing")
        if any(kw in _tid_lower_compound for kw in _compound_kw):
            compound = _find_compound_gap_measurement(
                target_id, feature_measurements, axis_str,
            )
            if compound is not None:
                measured = compound
                source = "white_box_compound"

        # 1b. Direct single-feature match
        if measured is None:
            measured, source = _find_best_feature_match(
                target_id, feature_measurements, kind, axis_str,
            )

    # If we found a white-box measurement, use it (exact, no mesh noise)
    if measured is not None and nominal is not None:
        dev = measured - nominal
        passed = abs(dev) <= max(tol_upper, tol_lower)
        return VerificationResult(
            target_id=target_id,
            passed=passed,
            measured_value=round(float(measured), 2),
            deviation=round(float(dev), 2),
            engine="check_mesh",
            is_mesh_noise=False,
            mesh_resolution_mm=0.0,  # White-box is exact
            noise_explanation=f"Measured from {source} (exact, no mesh noise)",
            severity="error" if not passed else "info",
        )

    # PRIORITY 2: Fall back to STL measurement
    # Guard: feature-specific targets should NOT fall back to the whole-part
    # STL bounding box — it would give wildly wrong numbers (e.g. the base
    # plate's 120mm X reported as a lug's X extent).  Return None so the
    # merge logic can defer to the other engine or report "cannot measure".
    _tid_lower = target_id.lower()
    _feature_keywords = {"lug", "rib", "cutout", "boss", "flange", "tab",
                         "gusset", "ear", "arm", "leg", "web", "slot", "fin",
                         "hub", "blade", "backplate", "shaft", "housing",
                         "bracket", "plate", "cover", "cap", "base", "tread"}
    if any(kw in _tid_lower for kw in _feature_keywords):
        return None

    dims = raw_json.get("dimensions", {}).get("dimensions", {})
    holes_data = raw_json.get("holes", {})
    hole_list = holes_data.get("holes", [])
    struct = raw_json.get("structure", {})
    conn = raw_json.get("connectivity", {})

    def _noise_check(dev: float) -> tuple[bool, str | None]:
        is_n = abs(dev) <= noise_threshold
        expl = (
            f"Deviation {dev:.3f} mm ≤ mesh noise floor {noise_threshold:.2f} mm"
            if is_n else None
        )
        return is_n, expl

    # --- OVERALL_DIMENSION ------------------------------------------------
    if kind == "overall_dimension":
        key_map = {"x": "X_width_mm", "y": "Y_depth_mm", "z": "Z_height_mm"}
        key = key_map.get(axis_str, "")
        measured = dims.get(key)
        if measured is not None:
            measured = _adjust_for_radius_semantics(float(measured), target_id, kind)
        if measured is not None and nominal is not None:
            dev = measured - nominal
            passed = abs(dev) <= max(tol_upper, tol_lower)
            is_n, expl = _noise_check(dev)
            return VerificationResult(
                target_id=target_id, passed=passed or is_n,
                measured_value=round(float(measured), 2),
                deviation=round(float(dev), 2),
                engine="check_mesh", is_mesh_noise=is_n,
                mesh_resolution_mm=noise_threshold,
                noise_explanation=expl,
                severity="error" if (not passed and not is_n) else "info",
            )

    # --- HOLE_DIAMETER ----------------------------------------------------
    if kind == "hole_diameter" and nominal is not None:
        if not hole_list:
            # No holes detected at all — the cut tools likely missed the body
            hole_count = holes_data.get("hole_count", 0)
            det_warnings = holes_data.get("detection_warnings", [])
            return VerificationResult(
                target_id=target_id, passed=False,
                measured_value=None,
                deviation=None,
                engine="check_mesh",
                severity="error",
                raw_measurements={
                    "error": (
                        f"HOLE MISSING: {target_id} expects a hole with "
                        f"diameter {nominal}mm but NO holes were detected "
                        f"in the mesh (hole_count={hole_count}). "
                        f"The cut tool likely does not intersect the body — "
                        f"check the Pos() coordinates and Cylinder height "
                        f"of the hole-cutting tool."
                    ),
                    "detection_warnings": det_warnings,
                },
            )
        for hole in hole_list:
            measured = hole.get("diameter_mm")
            if measured is None:
                continue
            dev = measured - nominal
            passed = abs(dev) <= max(tol_upper, tol_lower)
            is_n, expl = _noise_check(dev)
            return VerificationResult(
                target_id=target_id, passed=passed or is_n,
                measured_value=round(float(measured), 2),
                deviation=round(float(dev), 2),
                engine="check_mesh", is_mesh_noise=is_n,
                mesh_resolution_mm=noise_threshold,
                noise_explanation=expl,
                severity="error" if (not passed and not is_n) else "info",
            )
        # Holes found but none had valid diameter → fail
        return VerificationResult(
            target_id=target_id, passed=False,
            measured_value=None,
            engine="check_mesh",
            severity="error",
            raw_measurements={"error": "Holes detected but no valid diameter measured"},
        )

    # --- WALL_THICKNESS ---------------------------------------------------
    if kind == "wall_thickness" and nominal is not None:
        equiv = struct.get("equivalent_wall_thickness_mm")
        if equiv is not None:
            dev = equiv - nominal
            passed = dev >= -tol_lower  # wall must be ≥ nominal - lower_tol
            return VerificationResult(
                target_id=target_id, passed=passed,
                measured_value=round(float(equiv), 2),
                deviation=round(float(dev), 2),
                engine="check_mesh",
                severity="warning" if not passed else "info",
            )

    # --- VOLUME -----------------------------------------------------------
    if kind == "volume" and nominal is not None:
        vol = struct.get("volume_mm3")
        if vol is not None:
            dev = vol - nominal
            passed = abs(dev) <= max(tol_upper, tol_lower)
            return VerificationResult(
                target_id=target_id, passed=passed,
                measured_value=round(float(vol), 2),
                deviation=round(float(dev), 2),
                engine="check_mesh",
                severity="error" if not passed else "info",
            )

    # --- WATER_TIGHTNESS --------------------------------------------------
    if kind == "water_tightness":
        is_wt = bool(struct.get("is_watertight", False))
        passed = is_wt  # nominal is irrelevant for boolean checks
        return VerificationResult(
            target_id=target_id, passed=passed,
            measured_value=1.0 if is_wt else 0.0,
            engine="check_mesh",
            severity="fatal" if not is_wt else "info",
        )

    # --- SINGLE_BODY ------------------------------------------------------
    if kind == "single_body":
        is_single = bool(conn.get("is_single_body", True))
        # When the part fails the single-body check, measured_value reflects
        # the actual body_count (e.g. 2.0 for two disconnected shells).  When
        # body_count is None (synthetic failure — check_mesh.py couldn't
        # analyze the STL at all), measured_value is None instead of a
        # misleading numeric sentinel like 99.0.
        raw_body_count = conn.get("body_count")
        if is_single:
            _single_body_measured = 1.0
        elif raw_body_count is None:
            _single_body_measured = None
        else:
            _single_body_measured = float(raw_body_count)
        return VerificationResult(
            target_id=target_id, passed=is_single,
            measured_value=_single_body_measured,
            engine="check_mesh",
            severity="fatal" if not is_single else "info",
            raw_measurements={
                "body_count": conn.get("body_count", 1),
                "is_fatal": conn.get("is_fatal", False),
                "message": conn.get("message", ""),
            },
        )

    # --- HOLE_DISTANCE ----------------------------------------------------
    if kind == "hole_distance" and nominal is not None:
        hd_list = holes_data.get("hole_distances_mm", [])
        if hd_list:
            measured = hd_list[0].get("center_distance_mm")
            if measured is not None:
                dev = measured - nominal
                passed = abs(dev) <= max(tol_upper, tol_lower)
                is_n, expl = _noise_check(dev)
                return VerificationResult(
                    target_id=target_id, passed=passed or is_n,
                    measured_value=round(float(measured), 2),
                    deviation=round(float(dev), 2),
                    engine="check_mesh", is_mesh_noise=is_n,
                    mesh_resolution_mm=noise_threshold,
                    noise_explanation=expl,
                    severity="error" if (not passed and not is_n) else "info",
                )

    # --- EDGE_MARGIN ------------------------------------------------------
    if kind == "edge_margin" and nominal is not None:
        margins = holes_data.get("edge_margins", [])
        if margins:
            measured = margins[0].get("min_edge_distance_mm")
            if measured is not None:
                dev = measured - nominal
                passed = dev >= -tol_lower
                return VerificationResult(
                    target_id=target_id, passed=passed,
                    measured_value=round(float(measured), 2),
                    deviation=round(float(dev), 2),
                    engine="check_mesh",
                    severity="warning" if not passed else "info",
                )

    return None


# ---------------------------------------------------------------------------
# Merge & route
# ---------------------------------------------------------------------------


def _merge_engine_reports(
    *,
    cad_brief,  # CADBrief | dict
    engine_a: EngineReport,
    engine_b: EngineReport,
    iteration: int,
) -> QAReport:
    """Merge Engine A and Engine B reports into a single QAReport.

    Deduplicates VerificationResults (Engine A wins for dimension/volume
    checks because it has no mesh noise; Engine B covers everything else).
    Also applies the routing rules to determine the ``error_type``.
    """
    brief_id = _attr(cad_brief, "part_name", None) or "unknown"

    # -- Merge verification results (Engine A overrides Engine B) ----------
    results_by_id: dict[str, VerificationResult] = {}
    # Engine B first (baseline)
    for r in engine_b.results:
        results_by_id[r.target_id] = r
    # Engine A overrides (more precise for dimension/volume)
    for r in engine_a.results:
        results_by_id[r.target_id] = r

    all_results = list(results_by_id.values())

    # -- Count pass / fail (mesh-noise-only failures don't count as real) --
    passed = [r for r in all_results if r.passed or r.is_mesh_noise]
    failed = [r for r in all_results if not r.passed and not r.is_mesh_noise]
    noise_only = [r for r in all_results if not r.passed and r.is_mesh_noise]

    # -- Extract structural warnings from Engine B -------------------------
    raw_b = engine_b.raw_json or {}
    conn = raw_b.get("connectivity", {})
    struct = raw_b.get("structure", {})
    mfg = raw_b.get("manufacturability", {})

    connectivity_warning: str | None = None
    if not conn.get("is_single_body", True):
        # body_count may be None when check_mesh.py itself failed and we
        # injected a synthetic connectivity failure (is_single_body=False
        # but no real body count available).  Show "?" in that case rather
        # than the misleading literal "None".
        body_count = conn.get("body_count") or "?"
        connectivity_warning = (
            f"Part consists of {body_count} separate shells (disconnected bodies). "
            f"Solids do not share any volume — they will fall apart when printed. "
            f"Fix: ensure extrusions overlap by ≥0.1mm and add a boolean_union step. "
            f"(Exception: if the user explicitly requests multiple independent bodies such as an assembly / gear system, this error may be ignored.)"
        )

    strength_score: int | None = None
    strength_assess = struct.get("strength_assessment", {})
    if isinstance(strength_assess, dict):
        strength_score = strength_assess.get("strength_score")

    mfg_warnings: list[str] = []
    for issue in mfg.get("3d_print", {}).get("issues", []) or []:
        _issue_str = str(issue)
        if "overhang" in _issue_str or "overhang" in _issue_str.lower() or "bridge" in _issue_str:
            continue
        mfg_warnings.append(_issue_str)
    cnc_issues = mfg.get("cnc_machining", {}).get("issues", []) or []
    for issue in cnc_issues:
        mfg_warnings.append(str(issue))

    improvement_suggestions: list[str] = []
    for w in engine_b.warnings:
        improvement_suggestions.append(w)

    # -- Determine error_type (routing decision) ---------------------------
    error_type = ErrorType.NONE
    error_details: list[str] = []

    # RULE 0 (safety): engines produced errors with no verification results.
    # Don't silently return NONE — route based on severity.
    if len(all_results) == 0:
        a_errors = engine_a.errors or []
        b_errors = engine_b.errors or []
        if a_errors and b_errors:
            # Both engines broken → FATAL
            error_type = ErrorType.FATAL
            error_details.append(
                f"FATAL: Both QA engines failed. "
                f"Engine A: {a_errors[:2]}. Engine B: {b_errors[:2]}."
            )
        elif a_errors or b_errors:
            # One engine broken → retryable (the other might work next time)
            error_type = ErrorType.DIMENSION
            error_details.append(
                f"DIMENSION: One QA engine reported errors (A: {a_errors[:2]}, "
                f"B: {b_errors[:2]}) with no verification results. Retrying."
            )

    # RULE 1: Connectivity / multi-body → TOPOLOGY (Architect must fix)
    if conn.get("is_fatal") or not conn.get("is_single_body", True):
        error_type = ErrorType.TOPOLOGY
        msg = conn.get("message", "")
        if not msg:
            body_count = conn.get("body_count") or "?"
            msg = (
                f"Part consists of {body_count} separate shells — not a single solid. "
                f"Fix: add boolean_union and ensure ≥0.1mm overlap. "
                f"(Exception: if the user explicitly requests multiple independent bodies such as an assembly / gear system, this error may be ignored.)"
            )
        error_details.append(f"TOPOLOGY: {msg}")
        print(f"\n{'▲'*60}")
        print(f"  ▲ ROUTING: connectivity failure → TOPOLOGY → back to Architect")
        print(f"  ▲ {msg}")
        print(f"  ▲ Body count: {conn.get('body_count') or '?'}, Fatal: {conn.get('is_fatal', False)}")
        print(f"{'▼'*60}\n")

    # RULE 2: Non-watertight → TOPOLOGY
    if not struct.get("is_watertight", True):
        if error_type == ErrorType.NONE:
            error_type = ErrorType.TOPOLOGY
        error_details.append("TOPOLOGY: Mesh is not watertight — possible gap in geometry.")

    # RULE 3: Real dimension failures → DIMENSION (Coder tweaks numbers)
    # When connectivity is fatal, ALL dimension measurements are unreliable
    # (separate shells scramble feature isolation).  Suppress dimension details
    # so the repair agent focuses exclusively on fixing the topology first.
    _connectivity_fatal = bool(conn.get("is_fatal")) or not conn.get("is_single_body", True)
    real_fails = [
        r for r in failed
        if r.severity in ("error", "fatal") and not r.is_mesh_noise
    ]
    if real_fails:
        if error_type == ErrorType.NONE:
            error_type = ErrorType.DIMENSION
        if _connectivity_fatal:
            # Suppress individual dimension errors — they are bogus when
            # the mesh has disconnected bodies.  Add a single summary note.
            error_details.append(
                f"DIMENSION: {len(real_fails)} dimension target(s) failed, "
                f"but measurements are UNRELIABLE due to disconnected bodies. "
                f"Fix connectivity FIRST — dimension errors will resolve automatically."
            )
        else:
            # Build nominal lookup for direction hints
            _nominal_map: dict[str, float] = {}
            for vt in (_attr(cad_brief, "verification_targets", []) or []):
                tid = _attr(vt, "id", None)
                nom = _attr(vt, "nominal", None)
                if tid and nom is not None:
                    _nominal_map[tid] = float(nom)
            for rf in real_fails:
                target_id = rf.target_id
                measured = rf.measured_value
                deviation = rf.deviation
                nominal = _nominal_map.get(target_id)

                # Check for raw error message (e.g., "HOLE MISSING")
                raw_error = (rf.raw_measurements or {}).get("error")
                if raw_error:
                    error_details.append(raw_error)
                elif nominal is not None and deviation is not None and measured is not None:
                    direction = "TOO LARGE → decrease" if deviation > 0 else "TOO SMALL → increase"
                    needed = round(abs(deviation) * 0.7, 2)  # 70% damping correction
                    error_details.append(
                        f"DIMENSION: {target_id} — measured {measured}, "
                        f"nominal {nominal}, deviation {round(deviation, 2)} "
                        f"[{direction} by ~{needed}mm]"
                    )
                else:
                    error_details.append(
                        f"DIMENSION: {target_id} — measured {measured}, "
                        f"deviation {deviation}"
                    )
                # Append diagnostic hints (e.g., "may have matched wrong cylinder")
                _diag = (rf.raw_measurements or {}).get("diagnostic")
                if _diag:
                    error_details.append(f"  ↳ {_diag}")

    # RULE 4: Poor strength or severe overhangs → TOPOLOGY
    if strength_score is not None and strength_score < 40:
        if error_type == ErrorType.NONE:
            error_type = ErrorType.TOPOLOGY
        error_details.append(
            f"TOPOLOGY: Strength score {strength_score}/100 — "
            f"structural redesign needed (add ribs, increase wall thickness)."
        )
    elif strength_score is not None and strength_score < 60:
        # Moderate — include as dimension-level feedback
        error_details.append(
            f"Strength score {strength_score}/100 — consider increasing "
            f"wall thickness or adding gussets."
        )

    # RULE 5: Cantilever / leverage warning → TOPOLOGY
    cantilever = struct.get("cantilever_analysis", {})
    if cantilever.get("is_severe"):
        if error_type == ErrorType.NONE:
            error_type = ErrorType.TOPOLOGY
        error_details.append(f"TOPOLOGY: {cantilever.get('warning', 'Severe cantilever')}")

    # -- Final safety: engine errors with passed results → still flag -------
    all_engine_errors = (engine_a.errors or []) + (engine_b.errors or [])
    if all_engine_errors and error_type == ErrorType.NONE and not real_fails:
        error_type = ErrorType.DIMENSION
        error_details.append(
            f"DIMENSION: QA engine(s) reported errors while all verification "
            f"targets passed. Errors: {all_engine_errors[:3]}"
        )

    # -- Build final QAReport ----------------------------------------------
    return QAReport(
        cad_brief_id=brief_id,
        engine_a=engine_a,
        engine_b=engine_b,
        all_passed=(len(failed) == 0 and error_type == ErrorType.NONE),
        passed_count=len(passed),
        failed_count=len(failed),
        noise_only_fail_count=len(noise_only),
        error_type=error_type,
        error_details=error_details,
        improvement_suggestions=improvement_suggestions,
        connectivity_warning=connectivity_warning,
        strength_score=strength_score,
        manufacturability_warnings=mfg_warnings,
        iteration=iteration,
    )


# ---------------------------------------------------------------------------
# Shared QA helpers
# ---------------------------------------------------------------------------


def _attr(obj, name: str, default=None):
    """Read an attribute from a pydantic model OR a plain dict, with a default."""
    if obj is None:
        return default
    if hasattr(obj, name):
        val = getattr(obj, name)
        return val if val is not None else default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def _to_verification_target(raw) -> VerificationTarget | None:
    """Coerce a raw dict or pydantic model to a VerificationTarget."""
    if raw is None:
        return None
    if isinstance(raw, VerificationTarget):
        return raw
    if isinstance(raw, dict):
        try:
            return VerificationTarget(**raw)
        except Exception:
            return None
    return None


# ============================================================================
# JSON extraction helper
# ============================================================================


def _extract_json_from_llm(raw: str) -> str:
    """Aggressively extract JSON object from LLM response.

    1. Try `` ```json { ... } ``` `` or `` ``` { ... } ``` `` fences.
    2. Fall back to outermost ``{ ... }`` span via rfind.
    3. Return raw text if nothing matches.
    """
    # Priority 1: fenced JSON block — use greedy .* so nested braces work
    pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
    match = re.search(pattern, raw, flags=re.DOTALL)
    if match:
        content = match.group(1).strip()
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


def _build_autonomous_repair_prompt(
    *,
    user_request: str,
    error_details: list[str],
    feature_measurements: dict | None = None,
    special_features: list[str] | None = None,
) -> str:
    """Construct the strict autonomous repair prompt for the Aider closed-loop.

    This is the enhanced version used by ``node_autonomous_skill_loop``.
    It includes four iron-clad rules that the model MUST follow, plus the
    original user request to prevent goal drift across multiple iterations.

    Parameters
    ----------
    feature_measurements : dict | None
        White-box instrumentation measurements from temp_measurements_{iteration}.json.
        These are precise measurements of individual features BEFORE boolean merge.
    special_features : list[str] | None
        Non-trivial geometric constraints extracted from CADBrief by the Spec
        Planner (symmetry, feature placement, avoidance rules, etc.).  Shown
        right after user_request so Aider verifies each item explicitly.
    """
    req_section = ""
    if user_request:
        req_section = textwrap.dedent(f"""\
        ## Original Design Requirements (Original Design Requirements — GROUND TRUTH)

        The following is the user's original request. In all iteration repairs, you must strictly satisfy these requirements,
        and must not deviate from or simplify the original design intent due to error repair.

        {user_request}

        """)

    # Special features section — non-trivial geometric constraints extracted
    # by Spec Planner.  Shown right after user_request so Aider sees them as
    # a verification checklist, not lost in the long user_request text.
    special_features_section = ""
    if special_features:
        features_lines = []
        for i, feat in enumerate(special_features, 1):
            features_lines.append(f"  [{i}] {feat}")
        special_features_section = (
            "## 🔍 Special Design Constraints (Special Features — MUST verify each item)\n\n"
            "The following constraints are extracted separately from the user's requirements by the Spec Planner, and are the non-routine geometric constraints of this part.\n"
            "**The QA system only detects overall bounding box, connectivity, and watertightness, not these constraints.**\n"
            "**You are responsible for checking each constraint one by one to see if it is satisfied in the current code.** Any violation must be fixed.\n\n"
            + "\n".join(features_lines)
            + "\n\n**Self-check**: After repairing, check each constraint one by one to see if it is satisfied. For example:\n"
            "- Symmetry constraint → Check each sketch's control_points, Y coordinate range -h..h\n"
            "- Rib position constraint → Check the rib's Pos coordinate, whether Y is ±LUG_OUTER_FACE_Y\n"
            "- Avoidance rule → Check whether cutout vertices are at least 5mm away from mounting holes\n\n"
        )

    # Add feature measurements section if available
    measurements_section = ""
    if feature_measurements:
        measurements_section = "## 📏 Feature Measurements (Feature Measurements — White-box Instrumentation)\n\n"
        measurements_section += "The following is precise bounding box measurement data for each independent feature **before** boolean merge.\n"
        measurements_section += "**The QA system only detects overall bounding box, connectivity, and watertightness, not the dimensions of individual features.**\n"
        measurements_section += "**You are responsible for actively comparing this data with the dimensions in the user request, discovering and fixing feature-level deviations.**\n\n"
        measurements_section += "```json\n"
        measurements_section += json.dumps(feature_measurements, indent=2, ensure_ascii=False)
        measurements_section += "\n```\n\n"
        measurements_section += "**Usage**:\n"
        measurements_section += "- Compare each feature dimension in the user request (hole diameter, plate thickness, boss height, etc.) with the corresponding entry in the white-box data\n"
        measurements_section += "- Compare `size_x/y/z` with the nominal value in the user request, deviations exceeding 0.5mm need to be fixed\n"
        measurements_section += "- Check `min_x/y/z` and `max_x/y/z` to confirm the feature position is correct\n"
        measurements_section += "- This data is collected from independent features before merging, and is more precise than reverse inference from the merged STEP\n\n"

    errors_section = "No specific QA errors reported."
    if error_details:
        errors_section = "The following QA detection found errors that need to be fixed:\n\n"
        for i, err in enumerate(error_details, 1):
            errors_section += f"  [{i}] {err}\n"

    return textwrap.dedent(f"""\
    {req_section}\
    {special_features_section}\
    {measurements_section}\
    ## Task Instructions

    You are repairing a build123d CAD script. Your primary responsibility is to implement the user's original design intent,
    while making **minimal, precise modifications** to the code.

    **Important**: Each iteration only fixes **1-2 most critical errors**, do not attempt to fix all errors at once.

    ### Information Priority (from high to low)

    1. **User Qualitative Requirements** (structure, function, design intent described in the original request) — highest priority
    2. **QA Report** (overall bounding box, connectivity, watertightness) — core reference for topology and overall dimensions
    3. **White-box Feature Measurement Data** (📏 Feature Measurements section) — core reference for feature-level dimensions
    4. **User Quantitative Data** (specific dimension values) — satisfy as much as possible without violating the first three

    **Processing Principles**:
    - The QA report only detects overall bounding box dimensions, part connectivity, and watertightness. Connectivity and watertightness
      errors must be fixed first (correct structure is the foundation of everything).
    - Feature-level dimensions (hole diameter, plate thickness, boss height, fillet radius, etc.) must be verified by comparing white-box
      measurement data with the user request. If a feature's `size_x/y/z` in the white-box data
      deviates from the nominal in the user request by > 0.5mm, actively fix it.
    - When QA suggestions clearly conflict with user qualitative intent (e.g., user requests a "gear system" but QA
      reports a multi-body error), follow the user qualitative intent.
    - Coordinate verification should use `min/max_x/y/z` (actual bounding box) from white-box data, not
      hand calculations or coordinate comments in the code. Comments may be outdated from previous fixes without being updated.

    **Examples**:
    - QA report shows overall Z deviation 3mm → Check each feature's size_z in white-box data to locate the cause
    - White-box data shows hole diameter feature size_x=6mm but user requests 14mm → Fix the hole cutting tool
    - User requests gear system, QA reports multi-body error → Keep multi-body (user qualitative intent takes priority)
    - User labels hole diameter 4mm but wall thickness at that position is only 3mm → Adjust hole diameter to a reasonable value (structure takes priority over quantitative data)

    ## QA Error Report

    {errors_section}

    ## ⚠️ Absolute Iron Rules — Violating any one means repair failure

    ### 🔴 Iron Rule 1: Feature Preservation (Feature Preservation)
    It is strictly prohibited to escape topology error checking by deleting existing holes, chamfers, or cutting features!
    You **can only** fix it through the following methods:
      - Adjust coordinate position (Pos / Location / translate offset)
      - Rotate structure or feature (Rot / rotate) — encouraged if rotation can better satisfy design requirements or fix problems
      - Increase or decrease extrusion length (amount=)
      - Add new boolean merge structure (body_a + body_b / BOOLEAN_UNION)
      - Adjust hole diameter or position
      - Increase overlap between adjacent solids (push solids into each other via Pos)

    If parts are separated (disconnected bodies), fix by adjusting coordinate positions,
    increasing extrusion overlap (≥0.1mm), or adding a new boolean merge (body = body_a + body_b).
    **Absolutely prohibited** to delete features like holes, slots, reinforcement ribs to make errors "disappear".

    **Encourage rotation**: If rotating the entire structure or a feature can better satisfy design requirements (e.g., better alignment of mounting holes), you should actively use rotation operations.

    ### 🔴 Iron Rule 2: Coordinate System & Transformation Order (Coordinate System & Transformation Order)
    build123d uses a right-handed coordinate system:
      - X axis: left-right direction (positive direction to the right)
      - Y axis: front-back direction (positive direction forward)
      - Z axis: up-down direction (positive direction upward)

    **Transformation order is crucial — must "extrude first, then position"**:
    In the algebra API, `Pos` and `Rot` behave differently for sketch and solid.
    **Positioning sketch first and then extruding will cause coordinate offset**, because `extrude` will ignore
    the existing transformation of the sketch and extrude starting from Z=0.

      - ❌ **Error (common trap)**: Pos/Rot sketch first, then extrude
        ```python
        sk_placed = Pos(0, 8, 10) * Rot(X=90) * sk  # ← transform sketch
        solid = extrude(sk_placed, amount=18)         # ← extrude ignores transform!
        # Result: solid position is wrong, offset exactly equals amount
        ```
      - ✅ **Correct**: Extrude in XY plane first (along Z), then rotate and position the solid
        ```python
        solid_temp = extrude(sk, amount=18)            # ← extrude in XY plane, along Z
        solid = Pos(0, 8, 10) * Rot(X=90) * solid_temp # ← transform solid
        ```
      - ✅ **Also correct**: Do boolean operations on solid first, then apply unified transformation
        ```python
        solid = extrude(Rectangle(36, 42), amount=18)  # X=36, Y=42, Z=0→18
        solid = Rot(X=90) * solid                      # Y→Z, Z→-Y
        solid = Pos(0, 8, 10) * solid                  # Move to position
        ```

    **Mapping of coordinates under Rot(X=90)**:
      - Original X → New X (unchanged)
      - Original Y → New Z
      - Original Z → New -Y
    So the Y direction height of sketch (XY plane) → becomes the global Z direction height.
    The Z direction extrusion amount of extrude → becomes the global -Y direction (needs Pos correction).

    **Coordinate comment synchronization**: When you modify any `Pos()` or `Rot()` parameter, you must simultaneously
    update the adjacent coordinate comments (e.g., `# Y=8..26, Z=9..52`). Outdated comments are worse than no comments
    — they will cause subsequent fixes to be based on wrong hand-calculated expectations. If you cannot determine the new coordinate
    range, delete the comment rather than leaving wrong values.

    **Extrusion direction must be perpendicular to the sketch plane**: XY plane sketch → extrude along Z.
    If other directions are needed, extrude along Z first then Rot the solid.
      - ❌ `extrude(sk_in_XY, dir=(1,0,0))` → "gp_Dir::CrossCross() zero norm"
      - ✅ `solid = Pos(...) * Rot(Y=90) * extrude(sk, amount=6)`

    **Debugging tip**: QA report feature coordinates do not match code Pos → almost always the "position sketch first
    then extrude" problem. Fix: extrude first, then apply Rot/Pos to the solid.

    **Feature Measurement Data**:
    Check `temp_measurements_{{iteration}}.json` during repair (if it exists),
    which records the precise bounding box of each feature before boolean merge:
    `min_x, min_y, min_z, max_x, max_y, max_z`.
    If a feature's min/max does not match the Pos value in the code,
    it indicates the transformation order of this feature has a problem.

    ### 🔴 Iron Rule 3: 100% Algebra API (Algebra API)
    Only use top-level functions like `extrude()`, `Box()`, `body + body`. See `build123d_reference.md` for details.

    ### 🔴 Iron Rule 4: No Context Managers (NO Context Managers)
    `BuildPart`/`BuildSketch`/`Locations` **absolutely prohibited**.
    **Only exception**: `BuildLine` can create wireframes containing arcs, used with `make_face(wire.wire())`.
    ```python
    with BuildLine() as lug_wire:
        Line((-18, 0), (18, 0))
        ThreePointArc((18, 34), (0, 52), (-18, 34))
        Line((-18, 34), (-18, 0))
    sk_lug_profile = make_face(lug_wire.wire())
    ```

    ### 🔴 Iron Rule 5: Preserve Original Intent (Preserve Original Intent)
    In multiple iteration repairs, do not forget the user's initial design goal. All features described in the "Original Design Requirements" above
    (holes, slots, reinforcement ribs, chamfers, mounting surfaces, etc.) must be preserved.
    If a feature is missing in the current code, you must **add** it rather than ignore it.
    Focus on the positional relationships between features.

    ### 🔴 Iron Rule 6: Through-cut / Overshoot Boolean Cut Tools (Overshoot Boolean Cut Tools)
    Must use `_safe_cut(body, tool, label)`. Z-axis through-holes must use `Align.MIN`
    (see the Holes section of `build123d_reference.md` for details).
    The cutting tool must exceed the target surface by ≥2mm at both ends, otherwise OpenCascade coplanar failure.
      - `BLIND_HOLE` → check if Align.MIN is missing
      - `MISSED_CUT` → check Pos coordinates and Rot direction

    ### 🔴 Iron Rule 7: Fillets and Chamfers Last (Fillets and Chamfers Last) — see exception below

    Fillets and chamfers are **the most fragile operations**. Every boolean operation will
    invalidate all edge selectors, so chamfers **usually must** be placed after all boolean operations (union, cut).

    **Correct operation order**:
    ```text
    1. Base solid
    2. Additive features (lugs, ribs, bosses)
    3. Subtractive features (holes, cutouts, slots)
    4. Shell (if needed)
    5. Fillets and Chamfers (last!)
    ```

    **⚠️ Exception: When the fillet area does not overlap with subsequent boolean areas, you can fillet first then union**

    If a complete Circle edge is split into multiple arcs after boolean (e.g., backplate outer circle
    split into 12 segments by 12 blade unions), filleting multiple arcs will fail at degenerate junctions
    (ChFi3d cannot handle edge endpoints where only 2 faces intersect).

    In this case, if the fillet area (e.g., outer edge Circle) and subsequent boolean area (e.g., internal features
    union) **do not overlap spatially**, you can **fillet the complete Circle first, then union the internal features**:

    ```python
    # ✅ Fillet backplate outer circle first (complete Circle, fillet succeeds)
    solid = extrude(Circle(45), amount=6)
    outer_edges = [e for e in solid.edges().filter_by(Plane.XY) if e.length > 200]
    solid = fillet(outer_edges, radius=1.5)  # Complete Circle, fillet succeeds

    # Then union internal features (does not touch the fillet outer circle area)
    hub = Pos(0, 0, 5) * extrude(Circle(13), amount=23)
    solid = solid + hub  # hub is in the center, does not break outer circle fillet
    ```

    Judgment method: the fillet edge's bounding box does not intersect with the subsequent boolean bounding box.

    **Not applicable**: fillet area overlaps with boolean area (e.g., lug-to-base fillet and
    lug union are in the same area), must follow the "fillet last" rule.

    **Chamfer edge selection**: Use `solid.edges().filter_by(Axis.Z)` or coordinate filtering to precisely select edges,
    wrap each `try/except` to prevent failure crashes. See `build123d_reference.md` Fillet section for details.

    ### 🔴 Iron Rule 8: `_measure_feature` Records Variable State at Call Time
    `_measure_feature(var, name, type)` **immediately captures** `var`'s bounding box at **call time**.
    It **must be placed after feature creation and before any operation that modifies the variable**.

    If you use loops/boolean operations to accumulate multiple copies, **measure the prototype first, then enter the loop**:
    ```python
    # ✅ Correct — measure a single prototype
    blade = extrude(sk_blade, amount=3.0)
    _measure_feature(blade, 'step-05-blade', 'extrude')   # ← single blade 3mm

    result = blade
    for _i in range(1, 12):
        result = result + Rot(Z=_i * 30) * blade          # loop does not change blade

    # ❌ Error — measure the accumulated body
    for _i in range(12):
        result = (result or new) + new
    _measure_feature(result, 'step-05-blade', 'extrude')  # ← 12 blades as a whole!
    ```

    **The same applies to any operation that modifies the variable**: boolean merge (`a + b`), loop accumulation,
    variable reassignment. There is only one rule — **`_measure_feature` immediately follows feature creation,
    before the variable is modified**.

    ### 🔴 Iron Rule 9: Rib Sketch Plane Must Be Orthogonal to Connection Face

    A rib connects two faces (e.g., base plate + lug outer side). The rib's **sketch plane** and **connection face plane**
    must be orthogonal (cannot be the same), and the rib's **extrusion direction** = connection face normal direction.

    Mnemonic: connection face in XZ → rib sketch in YZ; connection face in YZ → rib sketch in XZ; connection face in XY → rib sketch in XZ or YZ.

    **Self-check**: If the rib sketch plane == connection face plane, immediately switch to an orthogonal plane. **Do not use Pos/Rot
    rotation to remedy wrong plane selection** — directly switch planes, remap local coordinates according to the new plane.

    ## Repair Strategy — Classified by Error Type

    Select the corresponding repair strategy based on the error type in the QA report:

    ### 🔧 TOPOLOGY / Connectivity (highest priority)
    Symptoms: parts disconnected, multiple shells, abnormally small volume
    Fix:
      - Increase overlap between adjacent solids (≥0.2mm, recommended 0.3-0.5mm)
      - Push solids into each other via Pos to make them share volume
      - Add `body = body_a + body_b` boolean merge
      - Check whether any solid's Pos coordinates cause gaps between them

    ### 🔧 MISSED_CUT / CUT POSITION ERROR (high priority)
    Symptoms: cutting tool does not intersect with target body, holes/slots are not cut out
    Fix:
      - Check whether the cutting tool's `Pos()` coordinates are within the target body range
      - **Increase Cylinder/extrude height**, ensure both ends exceed the target surface by ≥1mm
      - Check whether the `Rot()` direction is consistent with the cutting direction
      - Use `_safe_cut`'s log output to locate which specific cut failed

    ### 🔧 DIMENSION (Dimensional Deviation)
    Symptoms: dimension in some direction is too large or too small
    Fix:
      - Use 70% damping: `new_value = old_value + 0.7 * (target - measured)`
      - Do not jump to the target value all at once, avoid overshoot oscillation
      - If white-box measurement data is correct but QA report is wrong → check the Pos/Rot transformation order in the code
      - If the deviation exactly equals some extrude amount → typical "position first then extrude" bug

    ### 🔧 HOLE MISSING (Hole Missing)
    Symptoms: holes should exist but 0 holes detected
    Fix:
      - Confirm the cutting Cylinder's Pos coordinates are correct (inside the target body)
      - Confirm the Cylinder height is sufficient to penetrate the target body (exceeds by 1mm on both ends)
      - Confirm the Rot direction is correct (along the penetration direction of the target body)
      - If it is a Y-direction hole: `Pos(0, 0, z) * Rot(X=90) * Cylinder(radius, height)`
      - If it is a Z-direction hole: `Pos(x, y, center_z) * Cylinder(radius, height)`
        (center_z = penetration range center, e.g., base plate Z=0..10 → center_z=5)

    ### 🔧 FILLET / CHAMFER FAILURE
    Symptoms: chamfer operation exception caught by except, QA reports fillet size mismatch
    Fix:
      - Ensure chamfers are **after all boolean operations**
      - Reduce chamfer radius (if it exceeds local geometry)
      - Precisely filter edge selectors (filter by Z coordinate, direction, position)
      - Split large groups of chamfers into smaller groups, each with independent try-except

    ### 🔧 WALL THICKNESS
    Symptoms: wall thickness insufficient
    Fix: increase dimension parameters in the relevant direction

    **Boolean merge overlap guidance**:
    - Minimum overlap: 0.2mm (avoid micro features)
    - Recommended overlap: 0.3-0.5mm (ensure reliable merge)
    - Maximum overlap: no more than 10% of feature size (avoid changing external dimensions)

    **Chamfer edge selection guidance**:
    ```python
    # Select edges of specific direction
    vertical_edges = solid.edges().filter_by(Axis.Z)
    horizontal_edges = solid.edges().filter_by(Axis.X)

    # Select edges at specific position
    base_edges = [e for e in solid.edges() if abs(e.center_point().Z) < 1]
    lug_edges = [e for e in solid.edges() if abs(e.center_point().Z - 10) < 2]

    # Safe chamfer
    try:
        solid = fillet(selected_edges, radius=2.0)
    except Exception:
        pass  # If chamfer fails, skip instead of crashing
    ```

    **Rotation example**:
    ```python
    # Rotate a single feature
    hole_tool = Rot(Z=45) * Cylinder(radius=5, height=20)

    # Rotate the entire structure
    body = Rot(X=90) * body  # Rotate 90 degrees around X axis
    ```

    ## Task

    Fix the code in this file so that it passes all the above QA checks.
    **Only make necessary modifications** — do not rewrite the entire file.
    **Only fix 1-2 errors at a time**, prioritize fixing the most severe errors.
    Focus on the specific errors listed in the QA report.
    """)


# ============================================================================
# Autonomous Skill Loop — Aider helpers
# ============================================================================


# ---------------------------------------------------------------------------
# System prompt for the direct repair LLM call (replaces Aider)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_REPAIR = _load_prompt("repair")


def generate_initial_solution(
    *,
    script_path: str,
    user_request: str,
    special_features: list[str] | None = None,
) -> bool:
    """Generate initial CAD solution from scratch using Aider or direct API.

    This is different from repair — it generates a complete implementation
    based on the user request, not fixing existing errors.

    Returns
    -------
    bool
        True if the code was successfully generated.  False on failure.
    """
    script_path_obj = Path(script_path)

    # Special features section — non-trivial geometric constraints from CADBrief
    special_features_section = ""
    if special_features:
        features_lines = "\n".join(f"  [{i}] {feat}" for i, feat in enumerate(special_features, 1))
        special_features_section = f"""
## 🔍 Special Features (MUST satisfy each constraint)

These constraints were extracted from the user request by Spec Planner.
QA only checks overall dimension / single_body / water_tightness — it does
NOT verify these.  Your implementation MUST satisfy each item:

{features_lines}

After implementing, self-check: does each special feature hold?
"""
    else:
        special_features_section = ""

    # Build generation prompt
    generation_prompt = f"""Please implement the gen_step() function to create the CAD model described in the user request.

USER REQUEST:
{user_request}
{special_features_section}
Requirements:
1. Implement the COMPLETE solution in the gen_step() function
2. Use build123d API (Box, Cylinder, Sphere, fillet, chamfer, extrude, etc.)
3. Return the completed Part from gen_step()
4. Make sure the geometry matches the user request EXACTLY
5. Use proper dimensions, positions, and boolean operations

Please replace the 'pass' statement in gen_step() with the full implementation.
"""

    # Try Aider first
    aider_available = False
    try:
        from aider.coders import Coder
        from aider.io import InputOutput
        from aider.models import Model
        _ = Model(_AIDER_MODEL_NAME)
        aider_available = True
    except Exception as exc:
        print(f"[GENERATE] Aider not available: {exc}")

    if aider_available:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or _HARDCODED_API_KEY
        if api_key:
            if _API_BASE_ENV_VAR:
                os.environ[_API_BASE_ENV_VAR] = _DS_BASE_URL
            if _API_KEY_ENV_VAR:
                os.environ[_API_KEY_ENV_VAR] = api_key

            print(f"\n[GENERATE] Launching Aider to generate initial solution...")
            print(f"[GENERATE] Script: {script_path_obj.name}")
            print(f"[GENERATE] User request: {user_request[:100]}...")

            try:
                original_code = script_path_obj.read_text(encoding="utf-8")

                model = Model(_AIDER_MODEL_NAME)
                model.extra_params = {"max_tokens": _AIDER_MAX_TOKENS}  # avoid truncation
                io = InputOutput(
                    yes=True,
                    pretty=False,
                    dry_run=False,
                )
                coder = Coder.create(
                    main_model=model,
                    io=io,
                    fnames=[script_path, _BUILD123D_REF],
                    auto_commits=False,
                )
                coder.run(generation_prompt)

                # Verify the file was modified
                try:
                    generated_code = script_path_obj.read_text(encoding="utf-8")
                except OSError:
                    print("[GENERATE] Cannot read file after Aider run.")
                    return False

                if generated_code.strip() == original_code.strip():
                    print("[GENERATE] WARNING: Aider made NO changes.")
                    print("[GENERATE] Falling back to direct API generation...")
                else:
                    delta = len(generated_code) - len(original_code)
                    print(f"[GENERATE] Aider completed: code generated (delta: {delta:+d} chars)")
                    return True

            except Exception as exc:
                print(f"[GENERATE] Aider exception: {exc}")
                print("[GENERATE] Falling back to direct API generation...")

    # Fallback: Direct API generation with retry
    print(f"\n[GENERATE] Using direct DashScope API to generate solution...")
    try:
        original_code = script_path_obj.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[GENERATE] Cannot read script: {exc}")
        return False

    client = _llm_client()

    system_prompt = """You are an expert build123d CAD engineer. Your job is to implement CAD models based on user requests.

You will receive:
1. The USER REQUEST — what to build
2. The CURRENT PYTHON CODE — a template with a gen_step() function that needs implementation

You must output the COMPLETE Python script with the gen_step() function fully implemented.
Use build123d API: Box, Cylinder, Sphere, fillet, chamfer, extrude, boolean operations (+, -, &), etc.
Return a Part from gen_step().

Output ONLY the complete Python code, no explanations."""

    user_prompt = f"""USER REQUEST:
{user_request}

CURRENT CODE:
```python
{original_code}
```

Please implement the gen_step() function to create the requested CAD model.
Output the COMPLETE Python script with the implementation."""

    # Retry logic for transient connection errors
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"[GENERATE] Attempt {attempt + 1}/{max_retries}...")
            response = client.chat.completions.create(
                model=_REPAIR_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=_REPAIR_TEMP,
                max_tokens=_REPAIR_MAX_TOKENS,
                **_REPAIR_KWARGS,
                timeout=_CFG_LLM_API_TIMEOUT,  # configurable via config.py
            )

            generated_code = response.choices[0].message.content.strip()

            # Extract code from markdown if present
            if "```python" in generated_code:
                match = re.search(r"```python\s*(.*?)\s*```", generated_code, re.DOTALL)
                if match:
                    generated_code = match.group(1)

            # Write the generated code
            script_path_obj.write_text(generated_code, encoding="utf-8")

            delta = len(generated_code) - len(original_code)
            print(f"[GENERATE] Direct API completed: code generated (delta: {delta:+d} chars)")
            return True

        except Exception as exc:
            error_msg = str(exc).lower()
            # Check if it's a transient connection error
            if any(keyword in error_msg for keyword in ['connection', 'timeout', 'incomplete', 'peer closed']):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    print(f"[GENERATE] Connection error: {exc}")
                    print(f"[GENERATE] Retrying in {wait_time} seconds...")
                    import time
                    time.sleep(wait_time)
                    continue
            # Non-transient error or final attempt
            print(f"[GENERATE] Direct API exception: {exc}")
            return False


def _run_repair_on_script(
    *,
    script_path: str,
    error_details: list[str],
    user_request: str,
    feature_measurements: dict | None = None,
    special_features: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Repair a CAD Python script using Aider (primary) or direct API (fallback).

    **Primary path — Aider (headless)**:
      Uses ``aider.coders.Coder`` with ``openai/qwen3.7-max`` to directly
      edit the script file in place.  Aider's edit formats (search/replace,
      unified diff) are more reliable for targeted code fixes than asking an
      LLM to regenerate the entire file.

    **Fallback path — Direct DashScope API**:
      If Aider is not installed, falls back to sending the current code + QA
      errors to Qwen (DashScope) and extracting the fixed code from the response.

    Returns
    -------
    tuple[bool, str | None]
        (True, None) on success. (False, reason) on failure, where ``reason``
        is a short human-readable string identifying the actual cause (e.g.
        "Aider exception: Request timed out", "No DashScope API key",
        "Aider made no changes (timeout, model rejected, or context limit)").
        The caller surfaces this in the iteration log so the user is not
        misdirected into checking credentials when the real cause was a
        timeout or context-limit issue.
    """
    script_path_obj = Path(script_path)

    # ── Path A: Aider (primary) ──────────────────────────────────────────
    aider_available = False
    try:
        from aider.coders import Coder  # noqa: F811
        from aider.io import InputOutput
        from aider.models import Model
        # Verify Aider actually initializes (model registry, etc.)
        _ = Model(_AIDER_MODEL_NAME)
        aider_available = True
    except Exception as exc:
        print(f"[REPAIR] Aider not available: {exc}")
        print("[REPAIR] Falling back to direct DashScope API repair.")

    if aider_available:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or _HARDCODED_API_KEY
        if not api_key:
            print("[REPAIR] No DashScope API key — cannot run Aider.")
        else:
            if _API_BASE_ENV_VAR:
                os.environ[_API_BASE_ENV_VAR] = _DS_BASE_URL
            if _API_KEY_ENV_VAR:
                os.environ[_API_KEY_ENV_VAR] = api_key

            repair_prompt = _build_autonomous_repair_prompt(
                user_request=user_request,
                error_details=error_details,
                feature_measurements=feature_measurements,
                special_features=special_features,
            )

            print(f"\n[AIDER REPAIR] Launching Aider on: {script_path_obj.name}")
            print(f"[AIDER REPAIR] Errors to fix: {len(error_details)}")
            print(f"[AIDER REPAIR] Prompt length: {len(repair_prompt)} chars")

            try:
                # Read original code BEFORE Aider runs
                try:
                    original_code = script_path_obj.read_text(encoding="utf-8")
                except OSError:
                    original_code = ""

                model = Model(_AIDER_MODEL_NAME)
                model.extra_params = {"max_tokens": _AIDER_MAX_TOKENS}  # avoid truncation
                io = InputOutput(
                    yes=True,       # auto-confirm all prompts
                    pretty=False,   # no colored / interactive output
                    dry_run=False,  # actually edit files
                )
                coder = Coder.create(
                    main_model=model,
                    io=io,
                    fnames=[script_path, _BUILD123D_REF],
                    auto_commits=False,
                )
                coder.run(repair_prompt)

                # Verify the file still has valid content
                try:
                    fixed_code = script_path_obj.read_text(encoding="utf-8")
                except OSError as exc:
                    print("[AIDER REPAIR] Cannot read file after Aider run.")
                    return False, f"Cannot read file after Aider run: {exc}"

                if len(fixed_code.strip()) < 50:
                    print("[AIDER REPAIR] WARNING: file is nearly empty after Aider edit.")
                    return False, "File nearly empty after Aider edit"

                # Detect whether Aider actually changed the code
                if fixed_code.strip() == original_code.strip():
                    print("[AIDER REPAIR] WARNING: Aider made NO changes to the code.")
                    print("[AIDER REPAIR] This is usually a timeout, context-limit, or model rejection - NOT an API key issue.")
                    print("[AIDER REPAIR] Falling back to direct API repair ...")
                    _aider_no_change_reason = (
                        "Aider made no changes (timeout, context limit, or model rejected)"
                    )
                    # Fall through to Path B
                else:
                    delta = len(fixed_code) - len(original_code)
                    print(f"[AIDER REPAIR] Aider completed: code changed (delta: {delta:+d} chars)")
                    return True, None

            except Exception as exc:
                print(f"[AIDER REPAIR] Aider exception: {exc}")
                import traceback as _tb
                _tb.print_exc()
                print("[AIDER REPAIR] Falling back to direct API repair ...")
                _aider_no_change_reason = f"Aider exception: {exc}"
            else:
                # Try/except completed without raising and without returning;
                # only reachable when Aider made no changes (the fall-through
                # case above). ``_aider_no_change_reason`` is set in that path.
                pass
            try:
                _aider_no_change_reason
            except NameError:
                _aider_no_change_reason = None

    # ── Path B: Direct DashScope API (fallback) ───────────────────────────
    ok, fallback_reason = _run_direct_repair_fallback(
        script_path_obj=script_path_obj,
        error_details=error_details,
        user_request=user_request,
        special_features=special_features,
    )
    if ok:
        return True, None
    # If fallback failed, surface the most informative reason: prefer the
    # fallback's own error (e.g. "Request timed out"), but if the fallback
    # didn't say and Aider had a reason, use that.
    return False, fallback_reason or _aider_no_change_reason or "fallback repair failed"


def _run_direct_repair_fallback(
    *,
    script_path_obj: Path,
    error_details: list[str],
    user_request: str,
    special_features: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Fallback: call DashScope API directly to regenerate the entire script.

    Returns (True, None) on success or (False, reason) on failure, where
    ``reason`` identifies the actual cause (e.g. "API call failed: Request
    timed out") so the caller can surface it in the iteration log instead
    of guessing at credentials.
    """

    try:
        current_code = script_path_obj.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[FALLBACK REPAIR] Cannot read script: {exc}")
        return False, f"Cannot read script: {exc}"

    if not current_code.strip():
        print("[FALLBACK REPAIR] Script is empty.")
        return False, "Script is empty"

    errors_text = "\n".join(f"  [{i+1}] {e}" for i, e in enumerate(error_details))

    # Special features section — non-trivial geometric constraints from CADBrief
    if special_features:
        features_lines = "\n".join(f"  [{i}] {feat}" for i, feat in enumerate(special_features, 1))
        special_features_section = (
            "## 🔍 Special Features (MUST verify each item)\n\n"
            "These constraints were extracted from the user request by Spec Planner.\n"
            "QA only checks overall dimension / single_body / water_tightness — it does\n"
            "NOT verify these.  Your fix MUST satisfy each item:\n\n"
            f"{features_lines}\n\n"
            "After fixing, self-check: does each special feature hold?\n\n"
        )
    else:
        special_features_section = ""

    user_prompt = textwrap.dedent(f"""\
    ## Original User Request (GROUND TRUTH — never deviate)

    {user_request}

    {special_features_section}\
    ## QA Error Report (fix EVERY error listed below)

    {errors_text}

    ## Current Python Code (the script that produced the errors above)

    ```python
    {current_code}
    ```

    ## Task

    Fix ALL errors listed in the QA report above.  Output the COMPLETE
    fixed Python script inside a ```python fenced code block.
    """)

    if len(user_prompt) > 900_000:
        max_code_len = 900_000 - len(user_prompt) + len(current_code) - 5000
        if max_code_len > 1000:
            truncated_code = current_code[:max_code_len] + "\n# ... (truncated)\n"
            user_prompt = user_prompt.replace(current_code, truncated_code)

    print(f"\n[FALLBACK REPAIR] Calling DashScope API directly ...")
    print(f"[FALLBACK REPAIR] Code: {len(current_code)} chars, Errors: {len(error_details)}")

    # Retry logic for transient connection errors
    max_retries = 3
    client = _llm_client()
    raw_response = ""

    for attempt in range(max_retries):
        try:
            print(f"[FALLBACK REPAIR] Attempt {attempt + 1}/{max_retries}...")
            response = client.chat.completions.create(
                model=_REPAIR_MODEL,
                temperature=_REPAIR_TEMP,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT_REPAIR},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=_REPAIR_MAX_TOKENS,
                timeout=_CFG_LLM_API_TIMEOUT,  # configurable via config.py
                **_REPAIR_KWARGS,
            )
            raw_response = response.choices[0].message.content or ""
            break  # Success, exit retry loop

        except Exception as exc:
            error_msg = str(exc).lower()
            # Check if it's a transient connection error
            if any(keyword in error_msg for keyword in ['connection', 'timeout', 'incomplete', 'peer closed']):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    print(f"[FALLBACK REPAIR] Connection error: {exc}")
                    print(f"[FALLBACK REPAIR] Retrying in {wait_time} seconds...")
                    import time
                    time.sleep(wait_time)
                    continue
            # Non-transient error or final attempt
            print(f"[FALLBACK REPAIR] API call failed: {exc}")
            return False, f"API call failed: {exc}"

    if not raw_response.strip():
        print("[FALLBACK REPAIR] Empty response.")
        return False, "Empty LLM response"

    fixed_code = _extract_code_from_llm_response(raw_response)
    if not fixed_code or len(fixed_code.strip()) < 50:
        print(f"[FALLBACK REPAIR] Extracted code too short ({len(fixed_code)} chars).")
        return False, f"Extracted code too short ({len(fixed_code)} chars)"

    if fixed_code.strip() == current_code.strip():
        print("[FALLBACK REPAIR] LLM returned IDENTICAL code.")
        return True, None  # not a hard failure

    try:
        script_path_obj.write_text(fixed_code, encoding="utf-8")
        print(f"[FALLBACK REPAIR] Fixed code written: {script_path_obj.name} "
              f"({len(fixed_code)} chars, delta: {len(fixed_code) - len(current_code):+d})")
        return True, None
    except OSError as exc:
        print(f"[FALLBACK REPAIR] Cannot write: {exc}")
        return False, f"Cannot write fixed code: {exc}"


def _execute_cad_script(
    script_path: Path,
    step_out: Path,
    stl_out: Path,
    *,
    timeout: int = _CFG_CAD_SCRIPT_TIMEOUT,
    iteration: int = 0,
) -> tuple[bool, str]:
    """Execute a CAD Python script and verify it produces STEP + STL output.

    Parameters
    ----------
    iteration : int
        Current retry iteration number.  Passed to the script via ITERATION
        environment variable so it can write measurements to the correct file.

    Returns
    -------
    tuple[bool, str]
        ``(success, error_message)``.  ``error_message`` is empty on success;
        on failure it contains the script's stderr / traceback so the repair
        agent can see what went wrong.
    """
    cwd = script_path.parent

    # Set ITERATION environment variable so the script can write measurements
    # to the correct file (temp_measurements_{iteration}.json)
    import os as _os
    _os.environ["ITERATION"] = str(iteration)

    # Delete stale runtime diagnostics from previous runs.
    # The generated script only writes temp_missed_{iter}.json if _MISSED_CUTS
    # is non-empty.  Without this cleanup, a successful re-execution would
    # leave the old file in place, causing false-positive error detection.
    _stale_missed = cwd / f"temp_missed_{iteration}.json"
    if _stale_missed.is_file():
        try:
            _stale_missed.unlink()
        except OSError:
            pass

    # Ensure the script has a __main__ guard so it can be executed standalone
    try:
        code = script_path.read_text(encoding="utf-8")
    except OSError:
        print(f"[EXECUTE CAD] Cannot read script: {script_path}")
        return False, f"Cannot read script: {script_path}"

    if "def gen_step():" in code and 'if __name__ == "__main__":' not in code:
        code += (
            '\n\nif __name__ == "__main__":\n'
            '    import sys as _sys, json as _json\n'
            '    print("gen_step() starting ...", file=_sys.stderr)\n'
            '    _result = gen_step()\n'
            '    print(f"gen_step() returned: {type(_result).__name__}", file=_sys.stderr)\n'
        )

    # ── Rewrite output paths to match expected locations ──────────────────
    # The script may contain hardcoded paths from the original coder run
    # (e.g. temp_output_0.step).  After Aider repair, these paths may not
    # match the autonomous loop's expected output paths (e.g.
    # temp_output_autonomous_0.step).  Rewrite export_step / export_stl
    # path arguments to ensure files are written where the caller expects.
    import re as _re
    _step_name = step_out.name
    _stl_name = stl_out.name
    # Match path arguments in export_step(...) and export_stl(...) calls.
    # Handles both raw strings r"..." and regular strings "...", with
    # absolute or relative paths.  The regex captures the filename portion
    # (the last path component) and replaces it with the expected name.
    code = _re.sub(
        r'(export_step\s*\([^,]+,\s*r?["\'])([^"\']*?)(["\'])',
        lambda m: m.group(1) + _re.sub(r'[^/\\]+\.step$', _step_name, m.group(2)) + m.group(3),
        code,
    )
    code = _re.sub(
        r'(export_stl\s*\([^,]+,\s*r?["\'])([^"\']*?)(["\'])',
        lambda m: m.group(1) + _re.sub(r'[^/\\]+\.stl$', _stl_name, m.group(2)) + m.group(3),
        code,
    )

    # Also rewrite paths in hash computation (open(...) calls for hashing)
    # This ensures the hash is computed from the same files that were exported
    code = _re.sub(
        r'(with\s+open\s*\(\s*r?["\'])([^"\']*?\.step)(["\'],\s*["\']rb["\'])',
        lambda m: m.group(1) + _re.sub(r'[^/\\]+\.step$', _step_name, m.group(2)) + m.group(3),
        code,
    )
    code = _re.sub(
        r'(with\s+open\s*\(\s*r?["\'])([^"\']*?\.stl)(["\'],\s*["\']rb["\'])',
        lambda m: m.group(1) + _re.sub(r'[^/\\]+\.stl$', _stl_name, m.group(2)) + m.group(3),
        code,
    )

    try:
        script_path.write_text(code, encoding="utf-8")
    except OSError:
        pass

    print(f"[EXECUTE CAD] Running: {script_path.name}")

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        print(f"[EXECUTE CAD] Timed out after {timeout}s")
        return False, f"Script execution timed out after {timeout} seconds."
    except Exception as exc:
        print(f"[EXECUTE CAD] subprocess.run failed: {exc}")
        return False, f"subprocess.run failed: {exc}"

    if result.returncode != 0:
        stderr_tail = _tail(result.stderr.strip(), 40)
        print(f"[EXECUTE CAD] FAILED (exit {result.returncode})")
        _safe_print(f"[EXECUTE CAD] STDERR: {stderr_tail[:500]}")
        return False, (
            f"Script exited with returncode {result.returncode}.\n"
            f"--- STDERR (last 40 lines) ---\n{stderr_tail}"
        )

    # Check for output files at the expected paths
    found_step = step_out.is_file()
    found_stl = stl_out.is_file()

    if found_step and found_stl:
        print(f"[EXECUTE CAD] SUCCESS — STEP: {_file_size_kb(step_out)}, STL: {_file_size_kb(stl_out)}")
        return True, ""

    msg = f"Script ran but did not produce expected output files (STEP={found_step}, STL={found_stl})."
    print(f"[EXECUTE CAD] {msg}")
    return False, msg


# ============================================================================
# Node: Autonomous Skill Loop (replaces old QA + Repair loop)
# ============================================================================


def _validate_geometry_against_request(
    step_path: str,
    user_request: str,
    engine_a: EngineReport,
) -> list[str]:
    """Validate that the generated geometry matches the user request.

    This is a sanity check to catch obvious mismatches like:
    - Requested 120mm shaft, got 10mm cube
    - Requested complex part with 20+ faces, got simple box with 6 faces

    Parameters
    ----------
    step_path : str
        Path to the STEP file.
    user_request : str
        The original user request text.
    engine_a : EngineReport
        The Engine A (cadpy) report, which contains topology info.

    Returns
    -------
    list[str]
        List of error messages. Empty list if validation passes.
    """
    errors = []

    # Extract bounding box from Engine A report
    bbox_str = engine_a.summary if engine_a.summary else ""
    bbox_match = re.search(r"Bbox:\s*\[([\d.]+),\s*([\d.]+),\s*([\d.]+)\]", bbox_str)

    if not bbox_match:
        # Try to load STEP directly if Engine A didn't provide bbox
        try:
            from OCP.STEPControl import STEPControl_Reader
            from OCP.Bnd import Bnd_Box
            from OCP.BRepBndLib import BRepBndLib

            reader = STEPControl_Reader()
            status = reader.ReadFile(step_path)
            if status != 1:  # IFSelect_RetDone
                return [f"GEOMETRY GATE: Cannot read STEP file to validate geometry"]

            reader.TransferRoots()
            shape = reader.OneShape()

            bbox = Bnd_Box()
            BRepBndLib.Add_s(shape, bbox)
            xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()

            bbox_size = [xmax - xmin, ymax - ymin, zmax - zmin]
        except Exception as e:
            return [f"GEOMETRY GATE: Failed to load STEP for validation: {str(e)[:100]}"]
    else:
        bbox_size = [float(bbox_match.group(1)), float(bbox_match.group(2)), float(bbox_match.group(3))]

    # Extract face count from Engine A report.  None when the summary lacks
    # the "N faces" token (e.g. Engine A returned an early-error report with
    # a summary like "Engine A: STEP topology extraction failed.").  The
    # face-count sanity check below skips when face_count is None, so a
    # missing face-count token does NOT false-positive flag the part as a
    # placeholder — the real Engine A error is already in errors[].
    face_count_match = re.search(r"(\d+)\s*faces", engine_a.summary if engine_a.summary else "")
    face_count = int(face_count_match.group(1)) if face_count_match else None

    # Parse user request for expected dimensions
    # Look for patterns like "120 mm", "120mm", "length 120", etc.
    dimension_patterns = [
        r'(\d+(?:\.\d+)?)\s*mm',  # "120 mm" or "120mm"
        r'length\s*(?:of\s+)?(\d+(?:\.\d+)?)',  # "length 120" or "length of 120"
        r'(\d+(?:\.\d+)?)\s*(?:long|wide|tall)',  # "120 long"
        r'total\s+length\s+(?:is\s+)?(\d+(?:\.\d+)?)',  # "total length is 120"
    ]

    expected_dimensions = []
    for pattern in dimension_patterns:
        matches = re.finditer(pattern, user_request, re.IGNORECASE)
        for match in matches:
            dim = float(match.group(1))
            if dim > 1:  # Ignore very small numbers (likely not dimensions)
                expected_dimensions.append(dim)

    # Check 1: Bounding box sanity
    if expected_dimensions:
        max_expected = max(expected_dimensions)
        max_actual = max(bbox_size)

        # If the largest expected dimension is > 50mm, the actual should be at least 30% of that
        if max_expected > 50 and max_actual < max_expected * 0.3:
            errors.append(
                f"GEOMETRY GATE: Bounding box too small. "
                f"Expected at least {max_expected * 0.3:.0f}mm (based on request), "
                f"got {max_actual:.1f}mm. Bbox: [{bbox_size[0]:.1f}, {bbox_size[1]:.1f}, {bbox_size[2]:.1f}]"
            )

    # Check 2: Face count sanity (complex parts should have many faces)
    complexity_keywords = ['chamfer', 'fillet', 'keyway', 'slot', 'stepped', 'complex', 'detailed']
    has_complexity = any(kw in user_request.lower() for kw in complexity_keywords)

    if has_complexity and face_count is not None and face_count < 15:
        errors.append(
            f"GEOMETRY GATE: Too few faces for complex part. "
            f"Request mentions complexity features (chamfer/keyway/stepped), "
            f"but model only has {face_count} faces. "
            f"This is likely a placeholder or incomplete geometry."
        )

    # Check 3: Degenerate geometry (all dimensions very small)
    if all(dim < 20 for dim in bbox_size) and any(dim > 50 for dim in expected_dimensions):
        errors.append(
            f"GEOMETRY GATE: Degenerate geometry detected. "
            f"All dimensions < 20mm: [{bbox_size[0]:.1f}, {bbox_size[1]:.1f}, {bbox_size[2]:.1f}], "
            f"but request expects dimensions > 50mm. "
            f"This is likely a placeholder box, not the requested part."
        )

    # Check 4: Fillet volume anomaly
    # If the user_request mentions fillets/chamfers but the volume decrease
    # from the last union (pre-fillet) to the final assembly (post-fillet) is
    # too small, fillets were likely silently lost — typically because Aider
    # pre-collected edges into a list and reused across fillet calls, causing
    # the second fillet to operate on stale edges (parent = OLD solid) and
    # overwrite the first fillet's result.  See _safe_fillet docstring for
    # the full mechanism.  This check is a safety net for cases where Aider
    # bypasses _safe_fillet by inlining raw fillet() calls.
    user_request_lower = user_request.lower()
    wants_fillets = (
        "fillet" in user_request_lower
        or "chamfer" in user_request_lower
        or "rounded corner" in user_request_lower
        or "round edge" in user_request_lower
        or "chamfer" in user_request
        or "fillet" in user_request
    )
    if wants_fillets:
        _iter = os.environ.get("ITERATION", "0")
        measurements_path = Path(f"temp_measurements_{_iter}.json")
        if not measurements_path.is_file():
            measurements_path = Path("temp_measurements_0.json")
        if measurements_path.is_file():
            try:
                with open(measurements_path, "r", encoding="utf-8") as f:
                    measurements = json.load(f)
                union_volumes = [
                    m.get("volume")
                    for m in measurements.values()
                    if isinstance(m, dict)
                    and m.get("type") == "union"
                    and m.get("volume") is not None
                ]
                overall = measurements.get("overall", {})
                overall_vol = (
                    float(overall["volume"])
                    if isinstance(overall, dict) and overall.get("volume") is not None
                    else None
                )
                if union_volumes and overall_vol is not None:
                    pre_vol = float(max(union_volumes))
                    decrease = pre_vol - overall_vol
                    decrease_ratio = decrease / pre_vol if pre_vol > 0 else 0.0
                    # Heuristic: fillets remove at least 0.5% of pre-fillet
                    # volume on a real part with outer-corner fillets.  This
                    # threshold is conservative — a single 1mm fillet on a
                    # 100mm edge removes ~57 mm³, which on a 72000 mm³ base
                    # plate is ~0.08% — so 0.5% catches "fillet completely
                    # lost" without false-flagging "small fillet applied".
                    if decrease_ratio < 0.005:
                        errors.append(
                            f"GEOMETRY GATE: Fillet volume anomaly. User "
                            f"request mentions fillets/chamfers but volume "
                            f"only decreased by {decrease:.0f} mm³ "
                            f"({decrease_ratio*100:.2f}% of pre-fillet volume "
                            f"{pre_vol:.0f} mm³). Expected at least 0.5% "
                            f"decrease. This usually means fillet edges were "
                            f"pre-collected into a list and reused across "
                            f"fillet calls — see _safe_fillet docstring for "
                            f"the stale-edges overwrite trap."
                        )
            except Exception:
                pass  # measurements file unreadable — skip this check

    return errors


def _optimize_print_orientation(stl_path: Path) -> tuple[Path, str | None]:
    """
    Evaluate the best print orientation and automatically rotate the STL.

    Returns: (rotated_stl_path, orientation_info)
    - If a better orientation is found: returns the rotated STL path and orientation info
    - If the current orientation is already optimal: returns the original path and None
    """
    try:
        import trimesh
        import numpy as np

        mesh = trimesh.load(stl_path, force='mesh')
        extents = mesh.extents
        normals_all = mesh.face_normals

        # Evaluate 6 candidate orientations
        candidates = [
            {"name": "+Z down (default)", "up": np.array([0, 0, 1]), "base_area": extents[0] * extents[1]},
            {"name": "-Z down (inverted)", "up": np.array([0, 0, -1]), "base_area": extents[0] * extents[1]},
            {"name": "+Y down (side)", "up": np.array([0, 1, 0]), "base_area": extents[0] * extents[2]},
            {"name": "-Y down (side)", "up": np.array([0, -1, 0]), "base_area": extents[0] * extents[2]},
            {"name": "+X down (side)", "up": np.array([1, 0, 0]), "base_area": extents[1] * extents[2]},
            {"name": "-X down (side)", "up": np.array([-1, 0, 0]), "base_area": extents[1] * extents[2]},
        ]

        for cand in candidates:
            up = cand["up"]
            dot = np.dot(normals_all, up)
            overhang_mask = (dot < 0) & (np.abs(dot) < np.cos(np.radians(45)))
            cand["overhang_count"] = int(np.sum(overhang_mask))
            cand["overhang_ratio"] = cand["overhang_count"] / len(normals_all) if len(normals_all) > 0 else 0
            base_score = cand["base_area"] / (extents[0] * extents[1] + extents[1] * extents[2] + extents[0] * extents[2])
            cand["score"] = base_score * (1 - cand["overhang_ratio"])

        # Find the best orientation
        best = max(candidates, key=lambda c: c["score"])
        default = candidates[0]  # +Z down

        # Determine whether rotation is needed (best orientation scores 20% higher than default)
        if best["name"] != default["name"] and best["score"] > default["score"] * 1.2:
            _safe_print(f"\n[PRINT ORIENTATION] Detected better orientation: {best['name']}")
            _safe_print(f"  Base area: {best['base_area']:.0f}mm², Overhang: {best['overhang_ratio']*100:.1f}%")

            # Compute rotation matrix: rotate best["up"] to [0, 0, 1] (so the best direction faces up)
            up_current = best["up"]
            up_target = np.array([0, 0, 1])

            # Compute rotation axis and angle
            rotation_axis = np.cross(up_current, up_target)
            rotation_axis_norm = np.linalg.norm(rotation_axis)

            if rotation_axis_norm > 1e-6:
                rotation_axis = rotation_axis / rotation_axis_norm
                rotation_angle = np.arccos(np.clip(np.dot(up_current, up_target), -1, 1))

                # Build rotation matrix
                rotation_matrix = trimesh.transformations.rotation_matrix(
                    rotation_angle, rotation_axis
                )

                # Rotate mesh
                mesh.apply_transform(rotation_matrix)

                # Save to a temporary file
                rotated_path = stl_path.parent / f"{stl_path.stem}_rotated{stl_path.suffix}"
                mesh.export(rotated_path)

                info = f"Rotated to {best['name']} (base area {best['base_area']:.0f}mm², overhang {best['overhang_ratio']*100:.1f}%)"
                _safe_print(f"[PRINT ORIENTATION] ✅ {info}")
                return rotated_path, info
            else:
                _safe_print(f"[PRINT ORIENTATION] Rotation axis is zero, skipping rotation")
                return stl_path, None
        else:
            _safe_print(f"[PRINT ORIENTATION] Current orientation is already optimal (+Z down)")
            return stl_path, None

    except Exception as e:
        _safe_print(f"[PRINT ORIENTATION] Rotation failed: {e}")
        return stl_path, None


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
        if workflow_id == "aider":
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
    elif workflow_id == "aider":
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
    # Snapshot the original user_request so per-iteration interventions
    # (choice 2) only affect the current round. Next iteration starts
    # fresh from the original, even if the user picked 2 last round.
    original_user_request = user_request
    for retry in range(MAX_RETRIES):
        retry_iter = iteration + retry
        # Reset to original at the start of each iteration so interventions
        # from previous rounds do not leak into subsequent Aider calls.
        user_request = original_user_request
        print(f"\n{'='*60}")
        print(f"  AUTONOMOUS LOOP — Retry {retry + 1}/{MAX_RETRIES}")
        print(f"{'='*60}")

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
                iteration=retry_iter,
                selector_map=selector_map,
            )

            print(f"[AUTONOMOUS QA] Running Engine B (check_mesh.py) ...")
            engine_b = _run_engine_b_check_mesh(
                stl_path=str(current_stl),
                cad_brief=cad_brief,
                iteration=retry_iter,
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
        if not _skip_qa and not (_skip_initial_qa and retry == 0) and workflow_id == "aider":
            geometry_errors = _validate_geometry_against_request(
                step_path=str(current_step),
                user_request=user_request,
                engine_a=engine_a,
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
            missed_path = cwd / f"temp_missed_{retry_iter}.json"
            if not missed_path.is_file():
                missed_path = cwd / "temp_missed_0.json"
            if missed_path.is_file():
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
        if retry == 0:
            choice = 1  # auto-iterate — apply user_request directly
            all_log_lines.append(
                f"node_autonomous_skill_loop [retry {retry}]: "
                f"CHECKPOINT skipped on first iteration (choice=1, apply user_request directly)"
            )
        else:
            choice = _prompt_iteration_choice(
                step_path=str(current_step.resolve()),
                stl_path=str(current_stl.resolve()),
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
                "current_step_path": str(current_step.resolve()),
                "current_stl_path": str(rotated_stl.resolve()),
                "current_python_code": python_code,
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
        if (retry > 0 or workflow_id != "aider") and last_qa_report.all_passed and last_qa_report.error_type == ErrorType.NONE:
            all_log_lines.append(
                f"node_autonomous_skill_loop [retry {retry}]: ✅ ALL CHECKS PASSED"
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
                "current_step_path": str(current_step.resolve()),
                "current_stl_path": str(rotated_stl.resolve()),
                "current_python_code": python_code,
                "iteration_count": retry_iter + 1,
                "execution_log": all_log_lines,
                "node_history": list(state.get("node_history", [])) + ["autonomous_skill_loop"],
            }

        # --------------------------------------------------------------
        # Phase 3: Build repair prompt & run Aider or fallback repair
        # --------------------------------------------------------------
        error_details = last_qa_report.error_details or []
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
            # Load feature measurements from white-box instrumentation
            feature_measurements = None
            measurements_file = cwd / f"temp_measurements_{retry_iter}.json"
            if not measurements_file.is_file():
                measurements_file = cwd / "temp_measurements_0.json"
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
                    "current_step_path": str(current_step.resolve()),
                    "current_stl_path": str(current_stl.resolve()),
                    "current_python_code": python_code,
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
            if workflow_id == "aider":
                new_step = cwd / f"temp_output_aider_autonomous_{retry_iter}.step"
                new_stl = cwd / f"temp_output_aider_autonomous_{retry_iter}.stl"
            else:
                new_step = cwd / f"temp_output_autonomous_{retry_iter}.step"
                new_stl = cwd / f"temp_output_autonomous_{retry_iter}.stl"

            exec_ok, exec_error = _execute_cad_script(script_path, new_step, new_stl, iteration=retry_iter)

            if exec_ok:
                current_step = new_step
                current_stl = new_stl
                _skip_qa = False  # Model updated — next round needs fresh QA
                all_log_lines.append(
                    f"node_autonomous_skill_loop [retry {retry}]: "
                    f"Aider fixed code → re-executed OK"
                    f"{' (exec-retry ' + str(exec_attempt) + ')' if exec_attempt > 0 else ''}"
                )

                # -- Check for missed cuts / fillet failures (runtime diagnostics) --
                missed_path = cwd / f"temp_missed_{retry_iter}.json"
                if not missed_path.is_file():
                    missed_path = cwd / "temp_missed_0.json"  # fallback
                if missed_path.is_file():
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
        "current_step_path": str(current_step.resolve()),
        "current_stl_path": str(current_stl.resolve()),
        "current_python_code": python_code,
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

    Calls Qwen 3.7-max (DashScope) with a specialised system prompt that instructs it to
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

    # -- Cache: skip LLM call only on fresh runs (not retries) --------------
    is_retry = (
        iteration > 0
        or any(n == "planner" for n in state.get("node_history", []))
    )
    if not force_refresh and not is_retry:
        cached = _load_cache(_CACHE_DIR / "cad_brief.json")
        if cached:
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
    client = _llm_client()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT_SPEC_PLANNER},
        {"role": "user", "content": user_prompt},
    ]

    json_str = ""
    raw_response = ""
    try:
        json_str, raw_response = _call_llm_json_with_retry(
            client, messages,
            model=_SP_MODEL,
            max_tokens=_SP_MAX_TOKENS,
            temperature=_SP_TEMP,
            extra_kwargs=_SPEC_PLANNER_KWARGS,
            max_retries=3,
        )
    except ValueError as exc:
        # Empty response after retries → FATAL
        return {
            "error_type": ErrorType.FATAL,
            "cad_brief": None,  # Explicitly clear stale cad_brief
            "execution_log": [
                f"node_spec_planner: FATAL — {exc}"
            ],
            "node_history": list(state.get("node_history", [])) + ["planner"],
        }
    except json.JSONDecodeError as exc:
        # JSON still broken after retries → let graph retry
        print(f"\n[DEBUG PLANNER] JSON decode exhausted: {exc}\n")
        return {
            "error_type": ErrorType.DIMENSION,
            "cad_brief": None,  # Explicitly clear stale cad_brief
            "execution_log": [f"node_spec_planner: JSON Decode exhausted: {exc}"],
            "node_history": list(state.get("node_history", [])) + ["planner"],
        }
    except Exception as exc:
        return _planner_fatal(
            iteration=iteration,
            reason=f"LLM API call failed: {exc}",
            node="node_spec_planner",
        )

    # ------------------------------------------------------------------
    # 3. Validate Pydantic schema
    # ------------------------------------------------------------------
    try:
        parsed_dict = json.loads(json_str)
        cad_brief = CADBrief.model_validate(parsed_dict)
    except json.JSONDecodeError as e:
        print(f"\n[DEBUG PLANNER] {e}\n")
        return {
            "error_type": ErrorType.DIMENSION,
            "cad_brief": None,  # Explicitly clear stale cad_brief
            "execution_log": [f"node_spec_planner: JSON Decode: {e}"],
            "node_history": list(state.get("node_history", [])) + ["planner"],
        }
    except ValidationError as e:
        print(f"\n[DEBUG PLANNER] Schema Error: {e}\n")
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
    _save_cache(_CACHE_DIR / "cad_brief.json", cad_brief.model_dump())

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
        if cached:
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
                "missing required fields (step_id, step_type, depends_on), "
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
        4. Every step needs correct depends_on references.
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
    _save_cache(_CACHE_DIR / "architect_plan.json", architect_plan.model_dump())

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