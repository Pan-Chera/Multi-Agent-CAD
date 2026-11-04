"""QA report merging and geometry verification orchestration.

Internal implementation module; callers continue to use ``multi_agent_cad.nodes``.
Shared settings and replaceable helpers are resolved through that facade at call
time, preserving per-job overrides and existing monkeypatch hooks. No mutable
configuration or cache state is copied into this module.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from multi_agent_cad.schemas import ErrorType, EngineReport, QAReport, VerificationResult, VerificationTarget

from multi_agent_cad import nodes as _nodes


def _qa_report_or_dict(error_details: list[str] | None) -> str:
    """Format QA error details for injection into the Coder's user prompt."""
    if not error_details:
        return "No previous QA feedback."
    lines = ["Previous QA feedback (fix these issues):"]
    for i, detail in enumerate(error_details, 1):
        lines.append(f"  [{i}] {detail}")
    return "\n".join(lines)


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
    brief_id = _nodes._attr(cad_brief, "part_name", None) or "unknown"

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
            for vt in (_nodes._attr(cad_brief, "verification_targets", []) or []):
                tid = _nodes._attr(vt, "id", None)
                nom = _nodes._attr(vt, "nominal", None)
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


def _validate_geometry_against_request(
    step_path: str,
    user_request: str,
    engine_a: EngineReport,
    measurement_iteration: int = 0,
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
        measurements_path = Path(f"temp_measurements_{measurement_iteration}.json")
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
