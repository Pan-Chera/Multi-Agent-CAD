"""SinglePartMAC: the existing 4-agent MAC pipeline wrapped as a module.

This is the "encapsulate MAC as a single structural part generator"
step of the assembly refactor. The wrapper adds exactly two things on
top of ``python -m multi_agent_cad.graph``:

1. **cwd isolation** -- each part runs in its own directory so the
   ``temp_*`` artifacts and ``pipeline_cache`` never collide.
2. **result harvesting** -- locates the newest STEP/STL/Python outputs
   and returns a structured ``PartResult``.

No MAC internals are modified; the pipeline (Spec Planner ->
Geometric Architect -> deterministic Coder -> Autonomous Skill Loop)
is reused verbatim.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mac_assembly import config_assembly as cfg
from mac_assembly.schemas_assembly import PartResult, PartSpec

_ITER_RE = re.compile(r"_(\d+)$")

# Project root on sys.path for the part subprocess (whose cwd is the part dir,
# not the project root, so `import mac_assembly` would fail without this).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_token_summary(part_dir: Path) -> dict[str, int]:
    """Read ``token_summary.json`` from a part dir, or return {} on any error.

    Best-effort accounting: any malformed/missing file just contributes zero
    tokens instead of failing the part pipeline.
    """
    token_file = part_dir / "token_summary.json"
    if not token_file.is_file():
        return {}
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception:  # noqa: BLE001 - accounting is best-effort
        return {}


def _newest(work_dir: Path, pattern: str) -> Path | None:
    """Newest non-empty file matching pattern, by mtime then highest iter.

    mtime is the primary key: a fresh re-run writes new files (newer mtime)
    even at iter=0, while stale files from prior runs (higher iter numbers
    but older mtime) must NOT shadow them. Within the same run, iter breaks
    ties when mtimes are equal.
    """
    candidates = [
        p for p in work_dir.glob(pattern) if p.is_file() and p.stat().st_size > 0
    ]
    if not candidates:
        return None

    def sort_key(p: Path) -> tuple[float, int]:
        m = _ITER_RE.search(p.stem)
        return (p.stat().st_mtime, int(m.group(1)) if m else -1)

    return max(candidates, key=sort_key)


def run_part(
    spec: PartSpec,
    parts_root: Path,
    *,
    feedback: str = "",
    force_refresh: bool = False,
) -> PartResult:
    """Generate one part via the single-part MAC pipeline.

    Parameters
    ----------
    spec:
        PartSpec whose ``description`` is the entire single-part prompt.
    parts_root:
        ``<work_dir>/parts`` -- the part gets ``parts_root/<part_id>/``.
    feedback:
        Optional QA feedback appended to the prompt on remodel rounds.
    force_refresh:
        Skip the per-part plan cache (used when the previous plan failed).
    """
    part_dir = parts_root / spec.part_id
    part_dir.mkdir(parents=True, exist_ok=True)

    request = spec.description
    if feedback:
        request = (
            f"{spec.description}\n\n"
            f"IMPORTANT correction from assembly-level QA of the previous "
            f"attempt (the part geometry was wrong):\n{feedback}"
        )

    python_bin = cfg.PYTHON_BIN or sys.executable
    env = _make_subprocess_env()
    env.update(
        MAC_PART_REQUEST=request,
        MAC_PART_DIR=str(part_dir.resolve()),
        MAC_FORCE_REFRESH="1" if force_refresh else "",
    )

    log_path = part_dir / "part_log.txt"
    attempt = 0
    ok = False
    error: str | None = None
    with log_path.open("w", encoding="utf-8") as log_file:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed module invocation
                [python_bin, "-m", "mac_assembly._part_runner"],
                cwd=str(part_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=cfg.PART_PIPELINE_TIMEOUT,
            )
            log_file.write(proc.stdout or "")
            attempt = 1
            ok = proc.returncode == 0
            if not ok:
                error = _tail_error(proc.stdout or "", proc.returncode)
        except subprocess.TimeoutExpired as exc:
            log_file.write(exc.stdout or "" if isinstance(exc.stdout, str) else "")
            error = f"single-part pipeline timed out after {cfg.PART_PIPELINE_TIMEOUT}s"
        except Exception as exc:  # noqa: BLE001
            error = f"part subprocess crashed: {exc}"

    step = _newest(part_dir, "temp_output_*.step")
    stl = _newest(part_dir, "temp_output_*.stl")
    design = _newest(part_dir, "temp_design_*.py")

    if ok and (step is None or stl is None):
        ok = False
        error = error or "pipeline reported success but STEP/STL artifacts missing"

    token_usage = _load_token_summary(part_dir)

    return PartResult(
        part_id=spec.part_id,
        step_path=str(step) if step else "",
        stl_path=str(stl) if stl else "",
        py_path=str(design) if design else "",
        part_dir=str(part_dir),
        ok=ok,
        attempts=attempt,
        error=None if ok else (error or "unknown failure"),
        token_usage=token_usage,
    )


def run_part_remodel(
    spec: PartSpec,
    parts_root: Path,
    feedback: str,
) -> PartResult | None:
    """Patch an already-generated part with Aider instead of regenerating.

    Reuses MAC's ``graph_aider`` (modify-existing workflow) so verified
    features are preserved and fewer tokens are spent. Returns None when
    there is nothing to patch or the patch run fails outright -- the caller
    then falls back to full regeneration.
    """
    part_dir = parts_root / spec.part_id
    if not list(part_dir.glob("temp_design*.py")):
        return None  # nothing to patch

    # R3: give Aider the part's identity + intent, not just the bare QA error
    # list -- otherwise it patches blind, not knowing which part it is fixing.
    request = (
        f"You are modifying the existing part '{spec.part_id}' "
        f"({spec.part_name}). Original design intent:\n{spec.description}\n\n"
        f"Apply this assembly-level correction to the existing model "
        f"(do not redesign unrelated features):\n{feedback}"
    )

    python_bin = cfg.PYTHON_BIN or sys.executable
    env = _make_subprocess_env()
    env.update(
        MAC_PART_REQUEST=request,
        MAC_PART_DIR=str(part_dir.resolve()),
    )

    log_path = part_dir / "part_remodel_log.txt"
    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            proc = subprocess.run(  # noqa: S603
                [python_bin, "-m", "mac_assembly._part_runner", "--mode", "aider"],
                cwd=str(part_dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=cfg.PART_PIPELINE_TIMEOUT,
            )
            log_file.write(proc.stdout or "")
        if proc.returncode != 0:
            return None
    except Exception:  # noqa: BLE001
        return None

    return _harvest(spec, part_dir, ok=True, attempt=1, error=None)


def _harvest(spec: PartSpec, part_dir: Path, *, ok: bool, attempt: int,
             error: str | None) -> PartResult:
    step = _newest(part_dir, "temp_output_*.step")
    stl = _newest(part_dir, "temp_output_*.stl")
    design = _newest(part_dir, "temp_design_*.py")
    if ok and (step is None or stl is None):
        ok = False
        error = error or "remodel reported success but STEP/STL missing"
    token_usage = _load_token_summary(part_dir)
    return PartResult(
        part_id=spec.part_id,
        step_path=str(step) if step else "",
        stl_path=str(stl) if stl else "",
        py_path=str(design) if design else "",
        part_dir=str(part_dir),
        ok=ok, attempts=attempt,
        error=None if ok else (error or "unknown failure"),
        token_usage=token_usage,
    )


# Env vars forwarded to part subprocesses: API keys + proxy settings. Shared
# by the full-generation and the aider-remodel paths so both behave the same
# behind a proxy (R2).
_ENV_FORWARD_KEYS = (
    "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE",
    "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
)


def _make_subprocess_env() -> dict:
    """Build the env dict for a part subprocess: PYTHONPATH + PATH + the
    forwarded API/proxy vars. Single source for both the full-generation and
    aider-remodel subprocess paths.
    """
    env = dict(
        PATH=os.environ.get("PATH", "/usr/bin:/bin"),
        PYTHONPATH=str(_PROJECT_ROOT),
    )
    for key in _ENV_FORWARD_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def run_part_builder(spec: PartSpec, parts_root: Path) -> PartResult:
    """Generate a part by calling a named builder from mac_assembly.builders.

    Bypasses the LLM-driven single-part pipeline entirely (no Spec Planner,
    Architect, Coder, Skill Loop). The builder returns a build123d Shape
    which is exported to STEP/STL directly. Zero tokens spent on LLM.

    Use this when PartSpec.builder is set -- e.g. for structures the LLM
    struggles to generate correctly (horizontal-axis cylinders, knuckle
    ears with horizontal bores).
    """
    part_dir = parts_root / spec.part_id
    part_dir.mkdir(parents=True, exist_ok=True)

    builder_spec = spec.builder or {}
    name = builder_spec.get("name", "")
    params = builder_spec.get("params") or {}

    from mac_assembly.builders import build_part
    from build123d import export_step, export_stl

    step_path = part_dir / "temp_output_builder.step"
    stl_path = part_dir / "temp_output_builder.stl"
    py_path = part_dir / "temp_design_builder.py"

    # Write a tiny source file for audit trail (which builder + params).
    py_path.write_text(
        f'"""Auto-generated source for {spec.part_id} (builder: {name}).\n'
        f'Params: {params}\n'
        f'This file is not executable -- the actual geometry was generated '
        f'by calling builders.{name}(**{params!r}) at runtime.\n'
        f'"""\n',
        encoding="utf-8",
    )

    ok = True
    error: str | None = None
    try:
        shape = build_part(name, params)
        export_step(shape, str(step_path))
        try:
            export_stl(shape, str(stl_path), tolerance=0.05, angular_tolerance=0.3)
        except Exception as exc:  # noqa: BLE001
            stl_path = None  # type: ignore
        if not step_path.is_file() or step_path.stat().st_size == 0:
            ok = False
            error = f"builder {name!r} produced empty STEP"
    except Exception as exc:  # noqa: BLE001
        ok = False
        error = f"builder {name!r} crashed: {exc}"

    return PartResult(
        part_id=spec.part_id,
        step_path=str(step_path) if step_path.is_file() else "",
        stl_path=str(stl_path) if stl_path and stl_path.is_file() else "",
        py_path=str(py_path),
        part_dir=str(part_dir),
        ok=ok,
        attempts=1,
        error=error,
        token_usage={},  # zero tokens (no LLM call)
    )


def run_part_reuse(
    spec: PartSpec,
    parts_root: Path,
    template_result: PartResult | None,
) -> PartResult:
    """Copy a template part's STEP/STL/py into this instance's directory.

    Zero LLM tokens. The instance shares the template's geometry but
    gets its own part directory (so the codegen + QA + selector
    resolver all see it as an independent labeled solid). Mates place
    it independently -- only the geometry is shared.

    Returns a PartResult with ok=False (and a clear error) when the
    template's result is missing or its STEP file is gone -- the caller
    (node_part_builder) should then surface this as a PART_MISSING QA
    signal pointing at the template.
    """
    import shutil

    part_dir = parts_root / spec.part_id
    part_dir.mkdir(parents=True, exist_ok=True)

    template_id = spec.reuses_part_id
    if template_result is None or not template_result.ok:
        return PartResult(
            part_id=spec.part_id,
            part_dir=str(part_dir),
            ok=False,
            attempts=0,
            error=(
                f"reuse template {template_id!r} not yet built or failed "
                f"-- template must appear before this instance in "
                f"brief.parts (node_part_builder sorts templates first)"
            ),
            token_usage={},
        )

    src_step = Path(template_result.step_path)
    src_stl = Path(template_result.stl_path) if template_result.stl_path else None
    src_py = Path(template_result.py_path) if template_result.py_path else None
    if not src_step.is_file():
        return PartResult(
            part_id=spec.part_id,
            part_dir=str(part_dir),
            ok=False,
            attempts=0,
            error=f"reuse template {template_id!r} STEP missing at {src_step}",
            token_usage={},
        )

    dst_step = part_dir / "temp_output_reuse.step"
    dst_stl = part_dir / "temp_output_reuse.stl"
    dst_py = part_dir / "temp_design_reuse.py"
    shutil.copy2(src_step, dst_step)
    if src_stl and src_stl.is_file():
        shutil.copy2(src_stl, dst_stl)
    if src_py and src_py.is_file():
        # Prepend an audit header to the template's actual source code.
        # Do NOT overwrite with just the header -- downstream Aider
        # repair and the audit trail both need the real source body.
        # Prepending is safe even if the template's source opens with
        # its own module docstring: Python treats the prepended string
        # as the new module docstring and the original as a no-op
        # string expression -- valid syntax, no semantic loss.
        original_code = src_py.read_text(encoding="utf-8")
        header = (
            f'"""Reused geometry from template {template_id!r} '
            f'(copied at runtime by run_part_reuse).\n'
            f'Template source: {src_py}\n'
            f'This file is a copy for audit trail -- the actual geometry '
            f'was generated by the template\'s pipeline (builder or MAC).\n'
            f'"""\n\n'
        )
        dst_py.write_text(header + original_code, encoding="utf-8")

    return PartResult(
        part_id=spec.part_id,
        step_path=str(dst_step),
        stl_path=str(dst_stl) if dst_stl.is_file() else "",
        py_path=str(dst_py),
        part_dir=str(part_dir),
        ok=True,
        attempts=0,  # zero LLM calls
        error=None,
        token_usage={},  # zero tokens (no LLM call)
    )


def run_part_builder_remodel(
    spec: PartSpec,
    parts_root: Path,
    feedback: str,
) -> PartResult | None:
    """Re-call a builder with LLM-adjusted params to fix a QA failure.

    Builder parts cannot be Aider-patched (``temp_design_builder.py`` is a
    non-executable audit file), so the previous fallback path tried Aider
    on it (failed), then ran the full LLM single-part pipeline on
    ``spec.description`` -- losing the zero-token benefit of the builder
    and ignoring the original builder params. This path instead asks the
    LLM to read the QA feedback and emit adjusted builder params, then
    re-calls the builder directly. One small LLM call instead of the full
    MAC pipeline; preserves the deterministic builder geometry.

    Returns None when the LLM call fails or the adjusted params don't
    build -- caller falls back to full regeneration.
    """
    from mac_assembly import config_assembly as cfg
    from mac_assembly.llm_utils import call_llm_json

    builder_spec = spec.builder or {}
    name = builder_spec.get("name", "")
    params = builder_spec.get("params") or {}

    system_prompt = (
        "You adjust the parameters of a parametric CAD builder to fix an "
        "assembly-QA failure. Output ONE JSON object with the adjusted "
        "params. Keep the builder name unchanged. Only change params that "
        "need to change to address the feedback. Return JSON like: "
        '{"name": "<builder_name>", "params": {...}}'
    )
    user_prompt = (
        f"Builder: {name}\n"
        f"Current params: {params}\n"
        f"Part description (intent + local coordinate convention):\n"
        f"{spec.description}\n\n"
        f"Assembly-QA feedback (fix these by adjusting params only -- do "
        f"not switch to a different builder):\n{feedback}"
    )

    try:
        raw = call_llm_json(
            system_prompt,
            user_prompt,
            model=cfg.MATING_MODEL,  # cheap structured-output model
            temperature=cfg.MATING_TEMPERATURE,
            max_tokens=4096,
            extra_kwargs=cfg.MATING_KWARGS,
        )
        new_name = raw.get("name", name) if isinstance(raw, dict) else name
        new_params = raw.get("params", params) if isinstance(raw, dict) else params
        if not isinstance(new_params, dict) or not new_name:
            return None
        # Sanity: the LLM should not switch builders (different geometry
        # family); allow it only if the new name is a known builder.
        from mac_assembly.builders import BUILDERS
        if new_name not in BUILDERS:
            return None
        # Validate param keys against the builder's actual signature.
        # The LLM often renames params (plate_w -> plate_width); without
        # this check, build_part(**new_params) raises TypeError and the
        # failure surfaces as a generic builder crash rather than a
        # param-key mismatch. We fail fast here so the caller falls back
        # to full regeneration cleanly. Builders that declare `**_`
        # (e.g. clevis_palm) intentionally absorb extra keys -- only
        # enforce the extra-key rejection when no VAR_KEYWORD sink
        # exists. Missing required params (no default) always fail.
        import inspect
        sig = inspect.signature(BUILDERS[new_name])
        params = sig.parameters
        has_var_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        extra = set(new_params) - set(params)
        missing_required = {
            n for n, p in params.items()
            if p.default is inspect.Parameter.empty and n not in new_params
        }
        if (extra and not has_var_kw) or missing_required:
            return None
    except Exception:  # noqa: BLE001
        return None

    modified = spec.model_copy(update={
        "builder": {"name": new_name, "params": new_params},
    })
    result = run_part_builder(modified, parts_root)
    result.attempts = 1  # single remodel attempt
    if not result.ok:
        # Adjusted params didn't build -- let the caller fall back to
        # full regeneration rather than retry the builder.
        return None
    return result


def _tail_error(stdout: str, returncode: int) -> str:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    tail = " | ".join(lines[-4:])[:400]
    return f"returncode={returncode}; tail: {tail}"


def _build_synthetic_spec(spec: PartSpec) -> PartSpec:
    """Construct a PartSpec for the MAC subprocess: base_body.description
    + attach-point hints so the Coder agent leaves flat surfaces where
    features will attach.

    The synthetic spec's `description` is base_body.description + a
    "## Attach-point constraints" section listing each feature's
    attach_point + direction + name. The `builder` / `base_body` /
    `features` fields are cleared so the subprocess treats this as a
    normal single-part MAC run.
    """
    if spec.base_body is None:
        raise ValueError(f"_build_synthetic_spec: part {spec.part_id!r} has no base_body")
    hints = ["## Attach-point constraints (leave flat surfaces here for v3 features):"]
    for i, f in enumerate(spec.features):
        ap = f.attachment.attach_point_mm
        hints.append(
            f"  {i+1}. {f.name} at ({ap[0]}, {ap[1]}, {ap[2]}) direction={f.attachment.direction} "
            f"-- leave a flat region here for feature attachment"
        )
    synthetic_desc = spec.base_body.description + "\n\n" + "\n".join(hints)
    return spec.model_copy(update={
        "description": synthetic_desc,
        "builder": None,
        "base_body": None,
        "features": [],
    })


_LOCAL_BOUNDS_RE = re.compile(
    r"Local\b[^.\n]*?\bX\s*=\s*([+-]?\d+\.?\d*)\s*\.\.\s*([+-]?\d+\.?\d*)\s*,\s*"
    r"Y\s*=\s*([+-]?\d+\.?\d*)\s*\.\.\s*([+-]?\d+\.?\d*)\s*,\s*"
    r"Z\s*=\s*([+-]?\d+\.?\d*)\s*\.\.\s*([+-]?\d+\.?\d*)",
    re.IGNORECASE,
)


def _parse_expected_bounds(description: str) -> tuple[
    float, float, float, float, float, float
] | None:
    """Parse 'Local [coords:] X=min..max, Y=min..max, Z=min..max' from v3 spec.

    Tolerates variants like:
      - "Local X=-5..5, Y=0..30, Z=0..10"
      - "Local coordinates: X = -20..0, Y = -5..+5, Z = 0..10"
      - "Local coords: X=-5..+5, Y=0..30, Z=0..10"

    Returns (xmin, xmax, ymin, ymax, zmin, zmax) or None if not found.
    """
    m = _LOCAL_BOUNDS_RE.search(description)
    if not m:
        return None
    return tuple(float(g) for g in m.groups())  # type: ignore[return-value]


def _maybe_recenter_base_body(base_body: Any, spec: PartSpec) -> Any:
    """Auto-correct base body position when LLM Coder used Sketch+extrude
    (default-centered on origin) instead of Pos(x,y,z)*Box(...) (positioned
    at spec's center).

    Detection (priority order):
      1. Structured ``base_body.local_bounds`` dict {xmin, xmax, ymin, ymax,
         zmin, zmax} -- set by Decomposer when it knows the prism bounds.
         This is the reliable path; no free-text parsing.
      2. Regex fallback: parse 'Local X=.., Y=.., Z=..' from
         ``base_body.description``. Less reliable -- description format
         varies across prompts, but covers existing Decomposer output
         that hasn't been migrated to local_bounds yet.

    After getting expected bounds, compare with actual base body bounds.
    If the SIZES match (within 0.5mm tolerance, or 5mm on axes where the
    body has fillets / half-cylinder tips that may extend the bbox) but
    the POSITIONS differ, translate the base body so its min corner
    aligns with the expected min corner.

    This is a deterministic post-hoc fix -- does not rely on LLM honoring
    prompt instructions about Pos()*Box() vs Sketch+extrude. Single-part
    pipeline prompts (python_coder.md, repair.md) actively teach Sketch+
    extrude as the canonical pattern, so v3 spec instructions to use
    Pos()*Box() are routinely overridden by the LLM's training prior.

    If sizes don't match (e.g. LLM generated wrong dims), no auto-fix --
    the shape itself is wrong, return as-is so QA can flag it.
    """
    if spec.base_body is None:
        return base_body
    # Priority 1: structured local_bounds
    expected = None
    lb = spec.base_body.local_bounds
    if lb is not None:
        try:
            expected = (
                float(lb["xmin"]), float(lb["xmax"]),
                float(lb["ymin"]), float(lb["ymax"]),
                float(lb["zmin"]), float(lb["zmax"]),
            )
        except (KeyError, TypeError, ValueError):  # noqa: BLE001
            expected = None
    # Priority 2: regex fallback on description
    if expected is None:
        expected = _parse_expected_bounds(spec.base_body.description)
    if expected is None:
        return base_body  # no bounds info, can't verify
    ex_min_x, ex_max_x, ex_min_y, ex_max_y, ex_min_z, ex_max_z = expected
    try:
        bb = base_body.bounding_box()
    except Exception:  # noqa: BLE001
        return base_body
    # Check size match along each axis (within 0.5mm tolerance)
    size_x = bb.max.X - bb.min.X
    size_y = bb.max.Y - bb.min.Y
    size_z = bb.max.Z - bb.min.Z
    ex_size_x = ex_max_x - ex_min_x
    ex_size_y = ex_max_y - ex_min_y
    ex_size_z = ex_max_z - ex_min_z
    # Allow some slack for fillets / half-cylinder tips that may extend
    # the bbox beyond the prism's local bounds (e.g. finger_distal has
    # a half-cylinder tip making Y extend past 20 to 25). Use a generous
    # 5mm tolerance on the long axis, 0.5mm on others.
    if abs(size_x - ex_size_x) > 0.5 and abs(size_x - ex_size_x) > 5.0 + 1e-9:
        return base_body  # X size mismatch — wrong shape, no auto-fix
    if abs(size_y - ex_size_y) > 0.5 and abs(size_y - ex_size_y) > 5.0 + 1e-9:
        return base_body  # Y size mismatch
    if abs(size_z - ex_size_z) > 0.5 and abs(size_z - ex_size_z) > 5.0 + 1e-9:
        return base_body  # Z size mismatch
    # Compute translation: shift actual min to expected min along each axis
    dx = ex_min_x - bb.min.X
    dy = ex_min_y - bb.min.Y
    dz = ex_min_z - bb.min.Z
    if abs(dx) < 0.01 and abs(dy) < 0.01 and abs(dz) < 0.01:
        return base_body  # already at expected position
    src = "local_bounds" if lb is not None else "description regex"
    print(f"[assembly] part {spec.part_id}: auto-recentering base body by "
          f"({dx:.2f}, {dy:.2f}, {dz:.2f}) -- LLM used Sketch+extrude "
          f"(centered at origin) instead of Pos()*Box(); applying "
          f"deterministic post-hoc fix from v3 spec {src}.")
    from build123d import Pos
    return Pos(dx, dy, dz) * base_body


def run_part_with_features(
    spec: PartSpec,
    parts_root: Path,
    feedback: str = "",
) -> PartResult:
    """v3 path: LLM-generated base body + deterministic feature operators.

    1. Build a synthetic description (base_body.description + attach-point
       hints) for the MAC subprocess.
    2. If we have feedback + cached base STEP + cached spec, and the
       base_body.description is byte-identical to the cached version,
       skip MAC Coder (feature-only remodel fast path -- saves 1x LLM
       call + tens of seconds).
    3. Otherwise: run MAC Coder on the synthetic spec. If that fails
       AND we have feedback, do ONE Aider-remodel retry (run_part_remodel)
       on the base body before giving up.
    4. If still failing, return PartResult(ok=False) with diagnostic --
       NO fall-through to LLM Full Regen (would re-introduce the
       kinematic-feature failures v3 was designed to avoid).
    5. On success: load base STEP via build123d.import_step, apply each
       feature via apply_feature(), export final STEP/STL + audit .py
       + spec cache (temp_v3_spec.json).
    """
    import json
    import shutil

    part_dir = parts_root / spec.part_id
    part_dir.mkdir(parents=True, exist_ok=True)

    base_step_path = part_dir / "temp_output_base.step"
    spec_cache_path = part_dir / "temp_v3_spec.json"

    # ----- Step 2: feature-only remodel fast path detection -----
    reuse_base = False
    if feedback and base_step_path.exists() and spec_cache_path.exists():
        try:
            cached = json.loads(spec_cache_path.read_text(encoding="utf-8"))
            cached_desc = cached.get("base_body_description", "")
            current_desc = spec.base_body.description if spec.base_body else ""
            if cached_desc == current_desc and current_desc:
                reuse_base = True
                print(f"[assembly] part {spec.part_id}: v3 feature-only "
                      f"remodel -- reusing cached base STEP, skipping "
                      f"MAC Coder (~1x LLM call + tens of seconds saved)")
        except Exception:  # noqa: BLE001
            pass  # cache corrupted, fall through to full base regen

    # ----- Steps 3-4: generate (or reuse) the base body -----
    if reuse_base:
        try:
            from build123d import import_step
            loaded = import_step(str(base_step_path))
            # import_step returns a Solid (or Compound of solids); wrap
            # bare Solid in a Compound so export_step works on the final
            # body (export_step requires Compound, not bare Solid).
            from build123d import Compound
            if isinstance(loaded, Compound):
                base_body = loaded
            else:
                base_body = Compound(children=[loaded])
        except Exception as exc:  # noqa: BLE001
            return PartResult(
                part_id=spec.part_id, part_dir=str(part_dir), ok=False,
                error=f"v3 cached base STEP load failed: {exc}",
                token_usage={},
            )
        # Cached base body may also need recentering (if the cache was
        # populated before _maybe_recenter_base_body existed).
        base_body = _maybe_recenter_base_body(base_body, spec)
    else:
        spec_synthetic = _build_synthetic_spec(spec)
        base_result = run_part(spec_synthetic, parts_root, feedback=feedback)
        if not base_result.ok and feedback:
            # Aider retry on base body (single retry -- see plan §3a)
            remodeled = run_part_remodel(spec_synthetic, parts_root, feedback)
            if remodeled is not None and remodeled.ok:
                base_result = remodeled
        base_warning: str | None = None
        if not base_result.ok:
            # Continue-on-failure: the LLM Coder / Aider retry failed (e.g.,
            # non-watertight mesh, MISSED_CUT on hemisphere trim, topology
            # errors) but the subprocess produced a STEP file from the last
            # attempt. The user explicitly wants the assembly to proceed
            # with the failed geometry so they can inspect the assembled
            # result; the downstream QA will catch any remaining issues.
            # Truly fatal cases (no STEP file at all) still return ok=False.
            if base_result.step_path and Path(base_result.step_path).is_file():
                print(f"[assembly] part {spec.part_id}: v3 base body "
                      f"generation FAILED ({base_result.error}); continuing "
                      f"with last attempt's STEP -- the assembly will use "
                      f"this geometry as-is.")
                base_warning = (
                    f"base body generation failed ({base_result.error}); "
                    f"using last attempt's STEP"
                )
            else:
                return PartResult(
                    part_id=spec.part_id, part_dir=str(part_dir), ok=False,
                    error=(
                        f"v3 base body generation failed after Aider retry "
                        f"AND no STEP file produced. Base error: "
                        f"{base_result.error}. Features: "
                        f"{[f.name for f in spec.features]}. Consider "
                        f"RECOMPOSE (Decomposer switch to v2 builder) or "
                        f"HALT."
                    ),
                    token_usage=base_result.token_usage,
                )
        # Cache the base body STEP for future feature-only remodels
        try:
            shutil.copy2(base_result.step_path, base_step_path)
        except Exception:  # noqa: BLE001
            pass
        try:
            from build123d import import_step, Compound
            loaded = import_step(str(base_step_path))
            if isinstance(loaded, Compound):
                base_body = loaded
            else:
                base_body = Compound(children=[loaded])
        except Exception as exc:  # noqa: BLE001
            return PartResult(
                part_id=spec.part_id, part_dir=str(part_dir), ok=False,
                error=f"v3 base STEP load failed after generation: {exc}",
                token_usage=base_result.token_usage,
            )

    # Auto-correct base body position if LLM Coder used Sketch+extrude
    # (centered at origin) instead of Pos()*Box() (positioned per spec).
    base_body = _maybe_recenter_base_body(base_body, spec)

    # ----- Step 5: apply feature chain -----
    from mac_assembly.feature_operators import apply_feature
    final_body = base_body
    feature_log = []
    for i, feature in enumerate(spec.features):
        ap = feature.attachment.attach_point_mm
        try:
            final_body = apply_feature(final_body, feature)
            feature_log.append(f"  {i+1}. {feature.name} at {ap} "
                               f"direction={feature.attachment.direction} -- OK")
        except Exception as exc:  # noqa: BLE001
            # apply_feature already does multi-solid + disjoint fallback;
            # if we get here, the dispatcher itself crashed (shouldn't happen).
            feature_log.append(f"  {i+1}. {feature.name} at {ap} -- FAILED: {exc}")

    # ----- Step 6: export final STEP/STL + audit + spec cache -----
    from build123d import export_step, export_stl
    final_step = part_dir / "temp_output_features.step"
    final_stl = part_dir / "temp_output_features.stl"
    audit_py = part_dir / "temp_design_features.py"
    try:
        export_step(final_body, str(final_step))
        try:
            export_stl(final_body, str(final_stl), tolerance=0.05, angular_tolerance=0.3)
        except Exception:  # noqa: BLE001
            final_stl = None  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return PartResult(
            part_id=spec.part_id, part_dir=str(part_dir), ok=False,
            error=f"v3 final export failed: {exc}",
            token_usage={},
        )

    # Audit .py (non-executable; records base + feature chain)
    audit_py.write_text(
        f'"""v3 part: base_body (LLM-generated) + {len(spec.features)} feature operators.\n\n'
        f'Base body STEP:   parts/{spec.part_id}/temp_output_base.step\n'
        f'Final STEP:       parts/{spec.part_id}/temp_output_features.step\n\n'
        f'Feature chain (applied in order):\n'
        + "\n".join(feature_log)
        + f'\n\nThis file is a copy for audit trail. The actual geometry was built\n'
        f'by run_part_with_features at runtime, calling import_step on the base\n'
        f'STEP then applying each feature operator via apply_feature().\n'
        f'"""\n',
        encoding="utf-8",
    )

    # Spec cache (for future feature-only-remodel fast path)
    spec_cache_data = {
        "base_body_description": spec.base_body.description if spec.base_body else "",
        "features": [f.model_dump(mode="json") for f in spec.features],
    }
    try:
        spec_cache_path.write_text(json.dumps(spec_cache_data, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    return PartResult(
        part_id=spec.part_id,
        step_path=str(final_step) if final_step.is_file() else "",
        stl_path=str(final_stl) if final_stl and final_stl.is_file() else "",
        py_path=str(audit_py),
        part_dir=str(part_dir),
        ok=True,
        attempts=0,
        error=base_warning,  # None on clean base; warning string on continue-on-failure
        token_usage={},  # base body's tokens tracked separately in base_result
    )
