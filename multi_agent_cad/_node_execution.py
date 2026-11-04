"""Generated-script execution, child environments and print orientation.

Internal implementation module; callers continue to use ``multi_agent_cad.nodes``.
Shared settings and replaceable helpers are resolved through that facade at call
time, preserving per-job overrides and existing monkeypatch hooks. No mutable
configuration or cache state is copied into this module.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from multi_agent_cad import nodes as _nodes


def _build_child_env(iteration: int) -> dict[str, str]:
    """Build an allowlisted env dict for the generated-Python subprocess.

    ITERATION is set explicitly (the previous code mutated ``os.environ`` at
    process level, which leaked the value into every other subprocess in the
    web_runner; now it lives only in this child's env).

    Operators who need additional vars (e.g. ``HTTP_PROXY`` for a corporate
    network) set ``MAC_CHILD_ENV_ALLOW=VAR1,VAR2``; those names are added to
    the allowlist on top of the defaults above.
    """
    extra = os.environ.get(_nodes._CHILD_ENV_ALLOW_OVERRIDE, "")
    extra_names = {n.strip() for n in extra.split(",") if n.strip()}
    allow = _nodes._CHILD_ENV_ALLOWLIST | extra_names
    child_env = {k: v for k, v in os.environ.items() if k in allow}
    child_env["ITERATION"] = str(iteration)
    child_env["PYTHONIOENCODING"] = "utf-8"
    return child_env


def _remove_execution_outputs(step: Path, stl: Path, iteration: int) -> None:
    """A successful execution must create fresh, nonempty artifacts."""
    for path in (step, stl, step.parent / f"temp_measurements_{iteration}.json",
                 step.parent / f"temp_missed_{iteration}.json"):
        path.unlink(missing_ok=True)


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


def _execute_cad_script(
    script_path: Path,
    step_out: Path,
    stl_out: Path,
    *,
    timeout: int = _nodes._CFG_CAD_SCRIPT_TIMEOUT,
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

    # Build the child env using the C3 allowlist. ITERATION is set explicitly
    # here; the previous code mutated ``os.environ`` at process level, which
    # leaked the value into every other subprocess in the web_runner.
    child_env = _nodes._build_child_env(iteration)

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

    if re.search(r"(?m)^def gen_step\s*\(", code) and not re.search(
        r"(?m)^if __name__\s*==\s*(['\"])__main__\1\s*:", code,
    ):
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
    _step_name = step_out.resolve().as_posix()
    _stl_name = stl_out.resolve().as_posix()
    def _redirect_path(match, target):
        prefix = _re.sub(r'''r?["']$''', "", match.group(1))
        return prefix + repr(target) + match.group(3)[1:]
    # Match path arguments in export_step(...) and export_stl(...) calls.
    # Handles both raw strings r"..." and regular strings "...", with
    # absolute or relative paths. Replace the complete literal so a copied
    # source cannot keep exporting into the previous job's directory.
    code = _re.sub(
        r'(export_step\s*\([^,]+,\s*r?["\'])([^"\']*?)(["\'])',
        lambda m: _redirect_path(m, _step_name) if m.group(2).lower().endswith(".step") else m.group(0),
        code,
    )
    code = _re.sub(
        r'(export_stl\s*\([^,]+,\s*r?["\'])([^"\']*?)(["\'])',
        lambda m: _redirect_path(m, _stl_name) if m.group(2).lower().endswith(".stl") else m.group(0),
        code,
    )

    # Also rewrite paths in hash computation (open(...) calls for hashing)
    # This ensures the hash is computed from the same files that were exported
    code = _re.sub(
        r'(with\s+open\s*\(\s*r?["\'])([^"\']*?\.step)(["\'],\s*["\']rb["\'])',
        lambda m: _redirect_path(m, _step_name),
        code,
    )
    code = _re.sub(
        r'(with\s+open\s*\(\s*r?["\'])([^"\']*?\.stl)(["\'],\s*["\']rb["\'])',
        lambda m: _redirect_path(m, _stl_name),
        code,
    )

    try:
        script_path.write_text(code, encoding="utf-8")
    except OSError:
        pass

    print(f"[EXECUTE CAD] Running: {script_path.name}")

    try:
        _nodes._remove_execution_outputs(step_out, stl_out, iteration)
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        print(f"[EXECUTE CAD] Timed out after {timeout}s")
        return False, f"Script execution timed out after {timeout} seconds."
    except Exception as exc:
        print(f"[EXECUTE CAD] subprocess.run failed: {exc}")
        return False, f"subprocess.run failed: {exc}"

    if result.returncode != 0:
        stderr_tail = _nodes._tail(result.stderr.strip(), 40)
        print(f"[EXECUTE CAD] FAILED (exit {result.returncode})")
        _nodes._safe_print(f"[EXECUTE CAD] STDERR: {stderr_tail[:500]}")
        return False, (
            f"Script exited with returncode {result.returncode}.\n"
            f"--- STDERR (last 40 lines) ---\n{stderr_tail}"
        )

    # Check for output files at the expected paths
    found_step = step_out.is_file() and step_out.stat().st_size > 0
    found_stl = stl_out.is_file() and stl_out.stat().st_size > 0

    if found_step and found_stl:
        print(f"[EXECUTE CAD] SUCCESS — STEP: {_nodes._file_size_kb(step_out)}, STL: {_nodes._file_size_kb(stl_out)}")
        return True, ""

    msg = f"Script ran but did not produce expected output files (STEP={found_step}, STL={found_stl})."
    print(f"[EXECUTE CAD] {msg}")
    return False, msg


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
            _nodes._safe_print(f"\n[PRINT ORIENTATION] Detected better orientation: {best['name']}")
            _nodes._safe_print(f"  Base area: {best['base_area']:.0f}mm², Overhang: {best['overhang_ratio']*100:.1f}%")

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
                rotated_path = stl_path.parent / f"print_{stl_path.stem}{stl_path.suffix}"
                mesh.export(rotated_path)

                info = f"Rotated to {best['name']} (base area {best['base_area']:.0f}mm², overhang {best['overhang_ratio']*100:.1f}%)"
                _nodes._safe_print(f"[PRINT ORIENTATION] ✅ {info}")
                return rotated_path, info
            else:
                _nodes._safe_print(f"[PRINT ORIENTATION] Rotation axis is zero, skipping rotation")
                return stl_path, None
        else:
            _nodes._safe_print(f"[PRINT ORIENTATION] Current orientation is already optimal (+Z down)")
            return stl_path, None

    except Exception as e:
        _nodes._safe_print(f"[PRINT ORIENTATION] Rotation failed: {e}")
        return stl_path, None
