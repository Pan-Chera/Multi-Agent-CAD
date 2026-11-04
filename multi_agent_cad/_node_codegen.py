"""Deterministic CAD code generation and Python-coder helpers.

Internal implementation module; callers continue to use ``multi_agent_cad.nodes``.
Shared settings and replaceable helpers are resolved through that facade at call
time, preserving per-job overrides and existing monkeypatch hooks. No mutable
configuration or cache state is copied into this module.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import traceback
from pathlib import Path
from multi_agent_cad.schemas import ArchitectPlan, ErrorType, EngineReport, GraphState, QAReport

from multi_agent_cad import nodes as _nodes


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
    # build123d Shape.is_valid is a PROPERTY (bool), not a method.
    try:
        is_valid_val = solid.is_valid
        if callable(is_valid_val):
            is_valid_val = is_valid_val()
        if not is_valid_val:
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

        # Allow coincident-face union: overlap == 0 means the solids touch at
        # a face (e.g., a cap cylinder whose +Y face coincides with the box's
        # -Y face). build123d `a + b` at overlap==0 returns a Compound (2
        # disjoint solids) because OCCT's fuse needs a positive volume overlap.
        # We handle that case below by injecting a sub-mm epsilon overlap.
        if overlap_x < -0.01 or overlap_y < -0.01 or overlap_z < -0.01:
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

        # Coincident-face handling: build123d `a + b` at overlap==0 returns a
        # Compound of 2 disjoint solids (OCCT fuse needs positive overlap).
        # Detect that and inject a sub-mm epsilon shift along the coincident
        # axis to force a watertight single Solid. The shift is 0.005mm —
        # invisible at CAD precision but enough for OCCT to merge.
        try:
            _n_solids = len(result.solids()) if hasattr(result, "solids") else 1
        except Exception:
            _n_solids = 1
        if _n_solids > 1:
            # Find the coincident axis (smallest overlap, near 0)
            _overlaps = [overlap_x, overlap_y, overlap_z]
            _axis_idx = _overlaps.index(min(_overlaps))
            _shift = [0.0, 0.0, 0.0]
            _shift[_axis_idx] = 0.005
            try:
                b_shifted = b.moved(Location(Vector(*_shift)))
                result = a + b_shifted
            except Exception:
                try:
                    b_shifted = b.translate(Vector(*_shift))
                    result = a + b_shifted
                except Exception:
                    pass

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


def _extract_primitive_dimensions(step: dict) -> tuple[float, float, float] | None:
    """Extract (w, h, d) from a primitive-solid step's ad-hoc fields.

    The LLM emits dimensions under various field names; try each in order
    and return the first parseable 3-tuple. Returns None if no dimensions
    field is found.
    """
    def _parse_3list(v) -> tuple[float, float, float] | None:
        if not isinstance(v, (list, tuple)) or len(v) != 3:
            return None
        try:
            return tuple(float(x) for x in v)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None

    # 3-list fields: dimensions, size, dims, extent, bbox
    for key in ("dimensions", "dimensions_mm", "size", "size_mm",
                "dims", "extent", "bbox"):
        if key in step:
            r = _parse_3list(step[key])
            if r is not None:
                return r

    # Three separate numeric fields: width/height/depth (or length).
    try:
        w = float(step.get("width") or step.get("x_size") or 0)
        h = float(step.get("height") or step.get("y_size") or 0)
        d = float(step.get("depth") or step.get("length")
                  or step.get("z_size") or 0)
        if w > 0 and h > 0 and d > 0:
            return (w, h, d)
    except (TypeError, ValueError):
        pass

    # Single radius field for cylinder/sphere/cone: treat as (r, r, h)
    # where h is from height/length if present, else 2*r.
    try:
        r = float(step.get("radius") or step.get("radius_mm") or 0)
        if r > 0:
            h = float(step.get("height") or step.get("length") or 2 * r)
            return (r, r, h)
    except (TypeError, ValueError):
        pass

    return None


def _build_primitive_sketch_and_extrude(
    prim_kind: str,
    dims: tuple[float, float, float],
    orig_step: dict,
    sketch_id: str,
) -> tuple[dict | None, dict]:
    """Build a (sketch, extrude_step) pair replacing a primitive-solid step.

    For box/cube: rectangle sketch on XY with width=w, height=h; extrude d.
    For cylinder/cone: circle sketch on XY with radius=w; extrude h (cone
       slope lost — Aider recovers from notes if needed).
    For sphere: returns (None, ...) — sphere has no sketch+extrude analogue;
       the caller aliases the step to 'extrude' as a placeholder.

    The original step's step_id / label / depends_on / notes are preserved
    on the extrude_step so downstream references stay intact.
    """
    w, h, d = dims
    sid = orig_step.get("step_id") or sketch_id.replace("-autogen-sketch", "")
    label = orig_step.get("label") or f"Extrude {prim_kind} primitive"
    depends = orig_step.get("depends_on") or []
    notes = orig_step.get("notes") or f"Auto-expanded from step_type={prim_kind!r} dims=({w},{h},{d})"

    if prim_kind in ("box", "cube"):
        sketch = {
            "sketch_id": sketch_id,
            "workplane": "XY",
            "workplane_offset_mm": 0.0,
            "entities": [{
                "entity_type": "rectangle",
                "width": w,
                "height": h,
            }],
            "notes": f"Auto-generated rectangle {w}x{h} from {prim_kind} primitive",
        }
        extrude = {
            "step_id": sid,
            "step_type": "extrude",
            "label": label,
            "sketch_id": sketch_id,
            "distance_mm": d,
            "direction": "positive",
            "depends_on": depends,
            "notes": notes,
        }
        return sketch, extrude

    if prim_kind in ("cylinder", "cone"):
        radius = w  # _extract_primitive_dimensions returns (r, r, h) for these
        sketch = {
            "sketch_id": sketch_id,
            "workplane": "XY",
            "workplane_offset_mm": 0.0,
            "entities": [{
                "entity_type": "circle",
                "center": {"x": 0.0, "y": 0.0},
                "radius": radius,
            }],
            "notes": f"Auto-generated circle R={radius} from {prim_kind} primitive",
        }
        extrude = {
            "step_id": sid,
            "step_type": "extrude",
            "label": label,
            "sketch_id": sketch_id,
            "distance_mm": d,  # = h from _extract_primitive_dimensions
            "direction": "positive",
            "depends_on": depends,
            "notes": notes,
        }
        return sketch, extrude

    # sphere / unknown — no sketch analogue
    return None, orig_step


def _node_python_coder_deterministic(
    state: GraphState, architect_plan, iteration: int
) -> dict | None:
    """Translate ArchitectPlan → gen_step() code, execute via cadpy.generation.

    Uses the same ``cadpy.generation.run_script_generator()`` pipeline in a
    child process with an allowlisted environment, including after Aider has
    filled unsupported operations. Artifact paths and QA routing stay here.
    """
    # Pre-compute node_history for failure state
    _coder_history = list(state.get("node_history", [])) + ["coder"]

    cwd = Path.cwd()
    script_path = cwd / f"temp_design_{iteration}.py"
    step_path = cwd / f"temp_output_{iteration}.step"
    stl_path = cwd / f"temp_output_{iteration}.stl"

    try:
        code = _nodes._plan_to_code(architect_plan, iteration)
    except NotImplementedError as e:
        print(f"[DETERMINISTIC CODER] Unsupported operation: {e} → falling back to LLM")
        return None  # This is intentional - triggers LLM fallback in node_python_coder
    except Exception as e:
        import traceback
        print(f"[DETERMINISTIC CODER] _plan_to_code crashed: {e}")
        traceback.print_exc()
        # Return failure state instead of None to trigger repair in autonomous loop
        return _nodes._coder_failure_state(
            iteration=iteration,
            error_message=f"Deterministic coder code generation failed: {e}\n\nTraceback:\n{traceback.format_exc()}",
            script_path=str(script_path),
            node_history=_coder_history,
        )

    # Split generated code at the solids marker.
    # Module-level: imports, shim, key_dimensions, helper functions.
    # gen_step body: solid generation, export, return.
    import re as _re
    _module_part, _sep, _body_part = code.partition("# --- Generated solids ---")
    if not _sep:
        # Fallback: no marker found — wrap everything (backward compat)
        code = _re.sub(
            r'(from build123d import \*.*\n)',
            r'\1' + _nodes._build_shim_str(),
            code,
            count=1,
        )
        code = "def gen_step():\n" + "    " + code.replace("\n", "\n    ")
    else:
        # Inject SHIM into module-level part (flat, no function wrapping)
        _module_part = _re.sub(
            r'(from build123d import \*.*\n)',
            r'\1' + _nodes._build_shim_str(),
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
    if _nodes._has_unsupported_placeholders(code):
        print(f"[HYBRID CODER] Detected unsupported placeholders, using Aider to fill them")
        try:
            user_request = state.get("user_request", "")
            _cad_brief_for_aider = state.get("cad_brief")
            _special_features = _nodes._attr(_cad_brief_for_aider, "special_features", []) or []
            success = _nodes._fill_unsupported_with_aider(
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

    # ── Execute via cadpy.generation in a filtered child process ──
    try:
        _nodes._remove_execution_outputs(step_path, stl_path, iteration)
        # Write token snapshot to file before execution (crash safety).
        # If run_script_generator crashes (e.g., OCP SIGSEGV at export_step),
        # the parent reads this file to recover token stats that would
        # otherwise be lost when the subprocess dies before printing result JSON.
        try:
            from multi_agent_cad.token_tracker import tracker as _pre_exec_tracker
            import json as _json
            _pre_exec_snap = _pre_exec_tracker.summary()
            _snap_path = Path.cwd() / "temp_token_snapshot.json"
            with open(_snap_path, "w") as _f:
                _json.dump({
                    "phase": "pre_execute",
                    "n_calls": _pre_exec_snap["n_calls"],
                    "total_tokens": _pre_exec_snap["total_tokens"],
                    "total_input": _pre_exec_snap["total_input"],
                    "total_output": _pre_exec_snap["total_output"],
                    "total_cache_read": _pre_exec_snap["total_cache_read"],
                }, _f)
        except Exception:
            pass

        # Loading the generated module also executes its top-level code.
        # Keep BOTH loading and gen_step() out of the provider-bearing parent.
        cmd = [
            sys.executable, "-u", str(Path(_nodes.__file__).with_name("_cadpy_runner.py")),
            "--script", str(script_path),
            "--step", str(step_path),
            "--stl", str(stl_path),
            "--iteration", str(iteration),
            "--watch-parent",
        ]
        with subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            env=_nodes._build_child_env(iteration),
        ) as proc:
            # communicate() normally closes stdin. Retain the write end as a
            # lifetime pipe instead, so Web cancellation also stops this child.
            parent_pipe = proc.stdin
            proc.stdin = None
            try:
                stdout, stderr = proc.communicate(timeout=_nodes._CFG_CAD_SCRIPT_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise RuntimeError(
                    f"cadpy subprocess timed out after {_nodes._CFG_CAD_SCRIPT_TIMEOUT} seconds.\n"
                    f"--- stdout ---\n{_nodes._tail(stdout or '', 20)}\n"
                    f"--- stderr ---\n{_nodes._tail(stderr or '', 20)}"
                ) from exc
            except BaseException:
                proc.kill()
                proc.communicate()
                raise
            finally:
                parent_pipe.close()
        if stdout:
            _nodes._safe_print(stdout.rstrip())
        if stderr:
            _nodes._safe_print(stderr.rstrip())
        if proc.returncode != 0:
            raise RuntimeError(
                f"cadpy subprocess exited with returncode {proc.returncode}.\n"
                f"{_nodes._tail(stderr or stdout, 40)}"
            )

    except Exception as e:
        print(f"[DETERMINISTIC CODER] cadpy pipeline failed: {e}")
        import traceback; traceback.print_exc()
        # Return failure state instead of None to trigger repair in autonomous loop
        return _nodes._coder_failure_state(
            iteration=iteration,
            error_message=f"Deterministic coder execution failed: {e}\n\nTraceback:\n{traceback.format_exc()}",
            script_path=str(script_path),
            node_history=_coder_history,
        )

    if not step_path.is_file() or not stl_path.is_file() or min(step_path.stat().st_size, stl_path.stat().st_size) == 0:
        print("[DETERMINISTIC CODER] STEP/STL missing or empty after cadpy run")
        return _nodes._coder_failure_state(
            iteration=iteration,
            error_message="Deterministic coder executed but did not produce nonempty STEP and STL files",
            script_path=str(script_path),
            node_history=_coder_history,
        )

    stl_size = _nodes._file_size_kb(stl_path) if stl_path.is_file() else "N/A"

    # ── Check for missed cuts / fillet failures (runtime diagnostics) ──
    missed_path = cwd / f"temp_missed_{iteration}.json"
    if missed_path.is_file():
        try:
            missed = json.loads(missed_path.read_text(encoding="utf-8"))
            if missed:
                cats = _nodes._parse_missed_cuts(missed)
                error_details, label = _nodes._format_missed_cuts_errors(cats)
                total = sum(len(v) for v in cats.values())
                print(f"[DETERMINISTIC CODER] PARTIAL SUCCESS — {_nodes._file_size_kb(step_path)} STEP, {stl_size} STL, but {total} runtime issue(s): {label}")
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

    print(f"[DETERMINISTIC CODER] SUCCESS — {_nodes._file_size_kb(step_path)} STEP, {stl_size} STL")
    return {
        "current_python_code": code,
        "current_python_code_path": str(script_path.resolve()),
        "current_step_path": str(step_path.resolve()),
        "current_stl_path": str(stl_path.resolve()) if stl_path.is_file() else str(step_path.resolve()),
        "execution_log": [f"node_python_coder [iter {iteration}]: SUCCESS (deterministic)"],
        "node_history": list(state.get("node_history", [])) + ["coder"],
    }


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
        + _nodes._VALIDATION_HELPERS
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
    derived = _nodes._derive_positions(dims, plan) if isinstance(dims, dict) and dims else {}
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
            lines.extend(_nodes._gen_sketch_algebra(sk, sketch_name))
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
        wp = _nodes._sketch_workplane(sk)
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
            wp = _nodes._sketch_workplane(sketch_map[sk_id])
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
                lines.append(f"# Minimal fusion overlap: preserve requested feature datums")
                lines.append(f"_bb0 = {tv}.bounding_box()")
                lines.append(f"_bb1 = {ov}.bounding_box()")
                lines.append(f"# A 0.3-0.5mm push can extend an end flange into its mating plate.")
                lines.append(f"_min_dim = min(")
                lines.append(f"    _bb0.max.to_tuple()[0] - _bb0.min.to_tuple()[0],")
                lines.append(f"    _bb0.max.to_tuple()[1] - _bb0.min.to_tuple()[1],")
                lines.append(f"    _bb0.max.to_tuple()[2] - _bb0.min.to_tuple()[2],")
                lines.append(f"    _bb1.max.to_tuple()[0] - _bb1.min.to_tuple()[0],")
                lines.append(f"    _bb1.max.to_tuple()[1] - _bb1.min.to_tuple()[1],")
                lines.append(f"    _bb1.max.to_tuple()[2] - _bb1.min.to_tuple()[2],")
                lines.append(f")")
                lines.append(f"_overlap = 0.02  # enough for OCCT union, below assembly clearance")
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
                    lines.append(f"# Union: {a} + {b}  (minimal fusion overlap)")
                    # Measure both operands BEFORE union (white-box instrumentation)
                    # Use modified keys to avoid overwriting original feature measurements
                    lines.append(f"_measure_feature({a}, '{sids_list[-2]}-union-target', 'union_operand_a')")
                    lines.append(f"_measure_feature({b}, '{sids_list[-1]}-union-tool', 'union_operand_b')")
                    lines.append(f"_bb0 = {a}.bounding_box()")
                    lines.append(f"_bb1 = {b}.bounding_box()")
                    lines.append(f"# Keep nominal feature datums; only a tiny overlap is needed.")
                    lines.append(f"_min_dim = min(")
                    lines.append(f"    _bb0.max.to_tuple()[0] - _bb0.min.to_tuple()[0],")
                    lines.append(f"    _bb0.max.to_tuple()[1] - _bb0.min.to_tuple()[1],")
                    lines.append(f"    _bb0.max.to_tuple()[2] - _bb0.min.to_tuple()[2],")
                    lines.append(f"    _bb1.max.to_tuple()[0] - _bb1.min.to_tuple()[0],")
                    lines.append(f"    _bb1.max.to_tuple()[1] - _bb1.min.to_tuple()[1],")
                    lines.append(f"    _bb1.max.to_tuple()[2] - _bb1.min.to_tuple()[2],")
                    lines.append(f")")
                    lines.append(f"_overlap = 0.02  # below assembly clearance")
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
                wp = _nodes._sketch_workplane(sketch_map[sk_id])
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
            edge_sel = _nodes._infer_edge_filter(step)

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
            edge_sel = _nodes._infer_edge_filter(step)

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
            wp = _nodes._sketch_workplane(sk) if callable(_nodes._sketch_workplane) else str(getattr(sk, "workplane", "XY"))
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
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or _nodes._HARDCODED_API_KEY
        if not api_key:
            print("[HYBRID CODER] No DASHSCOPE_API_KEY available")
            return False

        if _nodes._API_BASE_ENV_VAR:
            os.environ[_nodes._API_BASE_ENV_VAR] = _nodes._DS_BASE_URL
        if _nodes._API_KEY_ENV_VAR:
            os.environ[_nodes._API_KEY_ENV_VAR] = api_key

        model = Model(_nodes._AIDER_MODEL_NAME)
        model.extra_params = {
            "max_tokens": _nodes._AIDER_MAX_TOKENS,  # avoid truncation
            # litellm-level request timeout (s): see LLM_CODEGEN_API_TIMEOUT.
            "timeout": _nodes._CFG_LLM_CODEGEN_API_TIMEOUT,
            **_nodes._REPAIR_KWARGS,
        }
        io = InputOutput(
            yes=True,
            pretty=False,
            dry_run=False,
        )

        coder = Coder.create(
            main_model=model,
            io=io,
            # Prompt URLs are CAD context, not permission to scrape pages or
            # trigger Aider's automatic Playwright/package installation.
            detect_urls=False,
            fnames=[str(script_path)],
            read_only_fnames=[_nodes._BUILD123D_REF],
            auto_commits=False,
            # Assembly jobs are intentionally gitignored runtime artifacts.
            # These paths are explicitly supplied by the workflow and must
            # remain editable even though their parent directory is ignored.
            add_gitignore_files=True,
        )

        coder.run(prompt)
        _nodes._join_aider_summarizer(coder)
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
