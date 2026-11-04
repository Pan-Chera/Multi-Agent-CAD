"""Engine B: STL mesh checks and feature measurement matching.

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
from pathlib import Path
from multi_agent_cad.schemas import EngineReport, VerificationResult

from multi_agent_cad import nodes as _nodes


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
    targets = _nodes._attr(cad_brief, "verification_targets", []) or []
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
        kind_str = str(_nodes._attr(vt, "kind", None) or "")
        if hasattr(kind_str, "value"):
            kind_str = kind_str.value
        nominal = _nodes._attr(vt, "nominal", None)
        tol_upper = _nodes._attr(vt, "tolerance_upper", 0.1)
        tol_lower = _nodes._attr(vt, "tolerance_lower", 0.1)
        axis = _nodes._attr(vt, "measurement_axis", None)
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

    targets = _nodes._attr(cad_brief, "verification_targets", [])

    # -- 1. Build spec file for compare_to_spec() ---------------------------
    spec_json = _nodes._cadbrief_to_spec_json(cad_brief)
    spec_path: Path | None = None
    if spec_json is not None:
        spec_path = Path(_nodes._CACHE_DIR) / f"check_mesh_spec_{_nodes._attr(cad_brief, 'part_name', 'unknown')}.json"
        try:
            _nodes._CACHE_DIR.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(json.dumps(spec_json, indent=2), encoding="utf-8")
        except OSError:
            spec_path = None  # non-fatal — spec comparison will be skipped

    # -- 2. Run check_mesh.py ----------------------------------------------
    if not _nodes._CHECK_MESH_SCRIPT.is_file():
        return EngineReport(
            engine_name="check_mesh",
            results=[],
            summary="",
            warnings=[],
            errors=[f"check_mesh.py not found at {_nodes._CHECK_MESH_SCRIPT}"],
        )

    cmd = [sys.executable, str(_nodes._CHECK_MESH_SCRIPT), str(Path(stl_path).resolve()), "--format", "json", "--skip-print-orientation", "--iteration", str(iteration)]
    if spec_path is not None and spec_path.is_file():
        cmd += ["--spec", str(spec_path)]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_nodes._CFG_CHECK_MESH_TIMEOUT,
            cwd=str(Path(stl_path).resolve().parent),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        _nodes._safe_print("[ENGINE B] check_mesh.py timed out — running fallback connectivity check")
        fb_conn = _nodes._fallback_connectivity_check(stl_path)
        _fallback_result = _nodes._build_single_body_result(fb_conn) if not fb_conn.get("is_single_body", True) else None
        return EngineReport(
            engine_name="check_mesh",
            results=[_fallback_result] if _fallback_result else [],
            summary=f"Fallback: {fb_conn.get('message', 'connectivity OK')}",
            warnings=["check_mesh.py timed out; only fallback connectivity check was performed."],
            errors=["check_mesh.py timed out after 180 seconds."],
            raw_json={"connectivity": fb_conn},
        )
    except Exception as exc:
        _nodes._safe_print(f"[ENGINE B] check_mesh.py exception — running fallback connectivity check")
        fb_conn = _nodes._fallback_connectivity_check(stl_path)
        _fallback_result = _nodes._build_single_body_result(fb_conn) if not fb_conn.get("is_single_body", True) else None
        return EngineReport(
            engine_name="check_mesh",
            results=[_fallback_result] if _fallback_result else [],
            summary=f"Fallback: {fb_conn.get('message', 'connectivity OK')}",
            warnings=[f"check_mesh.py subprocess failed: {exc}; only fallback connectivity check was performed."],
            errors=[f"check_mesh.py subprocess failed: {exc}"],
            raw_json={"connectivity": fb_conn},
        )

    if proc.returncode != 0:
        _nodes._safe_print(f"\n[ENGINE B] check_mesh.py FAILED (exit {proc.returncode}):")
        _nodes._safe_print(f"  STDERR: {proc.stderr.strip()[:500]}")
        _nodes._safe_print("[ENGINE B] Running fallback connectivity check")
        fb_conn = _nodes._fallback_connectivity_check(stl_path)
        _fallback_result = _nodes._build_single_body_result(fb_conn) if not fb_conn.get("is_single_body", True) else None
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
        _nodes._safe_print(f"[ENGINE B] check_mesh.py invalid JSON — running fallback connectivity check")
        fb_conn = _nodes._fallback_connectivity_check(stl_path)
        _fallback_result = _nodes._build_single_body_result(fb_conn) if not fb_conn.get("is_single_body", True) else None
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
    if schema_ver and schema_ver != _nodes._EXPECTED_CHECK_MESH_SCHEMA:
        warnings.append(
            f"check_mesh.py schema version mismatch: got {schema_ver}, "
            f"expected {_nodes._EXPECTED_CHECK_MESH_SCHEMA}. "
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
        target_obj = _nodes._to_verification_target(target)
        if target_obj is None:
            continue
        try:
            result = _nodes._measure_target_via_check_mesh(
                target_obj, raw_json, noise_threshold,
            )
            if result is not None:
                results.append(result)
        except Exception as exc:
            errors.append(
                f"Engine B failed to measure '{_nodes._attr(target_obj, 'id', '?')}': {exc}"
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
                    measured = _nodes._extract_measurement_for_kind(data, kind, axis_str)
                    if measured is not None:
                        measured = _nodes._adjust_for_radius_semantics(measured, target_id, kind)
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

    measured = _nodes._extract_measurement_for_kind(best_data, kind, axis_str)
    if measured is not None:
        # Adjust for radius semantics (halve if target expects radius)
        measured = _nodes._adjust_for_radius_semantics(measured, target_id, kind)
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
    target_id = _nodes._attr(target, "id", "?")
    kind_str = _nodes._attr(target, "kind", None)
    kind = str(kind_str.value) if hasattr(kind_str, "value") else str(kind_str or "")

    nominal = _nodes._attr(target, "nominal", None)
    tol_upper = _nodes._attr(target, "tolerance_upper", 0.1)
    tol_lower = _nodes._attr(target, "tolerance_lower", 0.1)
    axis = _nodes._attr(target, "measurement_axis", None)
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
            compound = _nodes._find_compound_gap_measurement(
                target_id, feature_measurements, axis_str,
            )
            if compound is not None:
                measured = compound
                source = "white_box_compound"

        # 1b. Direct single-feature match
        if measured is None:
            measured, source = _nodes._find_best_feature_match(
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
            measured = _nodes._adjust_for_radius_semantics(float(measured), target_id, kind)
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
