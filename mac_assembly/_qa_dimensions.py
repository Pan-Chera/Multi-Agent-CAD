"""Envelope checks, assembled ranges and dimension reconciliation.

Internal helpers; callers continue to use ``mac_assembly.assembly_qa``.
Resolve shared configuration, caches and replaceable helpers through that
module at call time so existing overrides and monkeypatch hooks stay live.
"""
from __future__ import annotations

from mac_assembly.schemas_assembly import AnchorKind, AssemblyBrief, MateType


def _check_envelope(brief: AssemblyBrief, placed):
    errors: list[str] = []
    measured: dict[str, float] = {}
    if not brief.overall_envelope_mm or not placed:
        return measured, errors
    # Exclude unmatched shells (tessellation artifacts of a single part
    # exporting as multiple connected components) -- they sit inside the
    # part's real bbox and would otherwise inflate the assembly envelope.
    labeled = [p for p in placed if not p[0].startswith("unmatched_")]
    if not labeled:
        return measured, errors
    xs = [p[1][0] for p in labeled] + [p[2][0] for p in labeled]
    ys = [p[1][1] for p in labeled] + [p[2][1] for p in labeled]
    zs = [p[1][2] for p in labeled] + [p[2][2] for p in labeled]
    measured = {
        "x": max(xs) - min(xs),
        "y": max(ys) - min(ys),
        "z": max(zs) - min(zs),
    }
    for axis in ("x", "y", "z"):
        expected = brief.overall_envelope_mm.get(axis)
        if not expected:
            continue
        tol = expected * _qa.cfg.ENVELOPE_TOLERANCE_PCT
        if abs(measured[axis] - expected) > tol:
            errors.append(
                f"envelope {axis}: measured {measured[axis]:.1f}mm vs "
                f"expected {expected:.1f}mm (tol ±{tol:.1f}mm)"
            )
    return measured, errors


def _check_assembled_axis_ranges(brief: AssemblyBrief, placed) -> list[str]:
    """Check optional part world-space bbox targets emitted by Decomposer."""
    errors: list[str] = []
    placed_by_label = {label: (bmin, bmax) for label, bmin, bmax in placed}
    for part in brief.parts:
        dims = part.key_dimensions if isinstance(part.key_dimensions, dict) else {}
        target = dims.get("assembled_axis_ranges")
        if not isinstance(target, dict) or part.part_id not in placed_by_label:
            continue
        bmin, bmax = placed_by_label[part.part_id]
        for i, axis in enumerate(("x", "y", "z")):
            pair = target.get(axis)
            if not (
                isinstance(pair, (list, tuple)) and len(pair) == 2
                and all(isinstance(v, (int, float)) for v in pair)
            ):
                continue
            delta = (float(bmin[i]) - float(pair[0]), float(bmax[i]) - float(pair[1]))
            if max(abs(delta[0]), abs(delta[1])) > 0.5:
                errors.append(
                    f"assembled pose {part.part_id} {axis.upper()} mismatch: "
                    f"measured [{bmin[i]:.2f},{bmax[i]:.2f}] vs target "
                    f"[{float(pair[0]):.2f},{float(pair[1]):.2f}] mm; "
                    f"endpoint deltas [{delta[0]:+.2f},{delta[1]:+.2f}] mm"
                )
    return errors


def reconcile_dimensions(brief: AssemblyBrief, mates, part_results: dict) -> ReconcileResult:
    """Re-check the mating plan against MEASURED part geometry.

    The Mating Architect designs anchors from the dimensions the
    Decomposer *claimed*; after PartBuilder the actual bounding boxes
    are free to measure. This gate catches stacking-height drift before
    a doomed assembly+QA cycle is spent (the assembly-layer analogue of
    MAC's stage-boundary normalization: deterministic checks first,
    LLM retries only when they pass).

    Returns a ReconcileResult: the error strings (measured numbers
    embedded so the remate feedback carries ground truth) plus a
    structured attribution the router uses to route part_geometry
    failures to part_builder instead of always remating.
    """
    measured: dict[str, tuple[tuple, tuple]] = {}
    for spec in brief.parts:
        result = part_results.get(spec.part_id)
        if not result or not result.ok or not result.step_path:
            continue
        # _orig_bbox is cached by path+mtime+size, so a re-run reconcile
        # (every assembler iteration) does not re-import the STEP files.
        bb = _qa._orig_bbox(result.step_path)
        if bb is None:
            return ReconcileResult(
                [f"reconcile: cannot load {spec.part_id} STEP "
                 f"({result.step_path})"],
                attribution="part_geometry",
                part_ids=[spec.part_id],
            )
        measured[spec.part_id] = bb

    if len(measured) != len(brief.parts):
        return ReconcileResult([])  # missing parts handled by the part_missing path
    if not mates:
        return ReconcileResult([])

    errors: list[str] = []
    attribution = "ambiguous"
    attribution_ids: list[str] = []

    # --- R6: shaft/bore radius compatibility for axis joints ----------------
    # For each revolute/coaxial/cylindrical mate, collect the real cylinder
    # radii along the joint axis on both parts (cadpy topology) and verify
    # the bore/shaft pairing is mechanically valid. Either orientation is
    # accepted:
    #   (a) fixed=bore + moving=shaft  -- bearing housing + rotating shaft.
    #   (b) fixed=shaft + moving=bore  -- base with pivot post + arm with bore
    #       (the most common finger-palm joint arrangement).
    #   (c) both sides equal radii      -- two bores joined by an implicit pin
    #       (not modeled as a part; kinematically valid even though the pin's
    #       own clearance can't be verified here).
    # Reject exact fit (clearance = 0 -> no rotation room). The previous
    # directional check (mv < f only) rejected (b) and (c), blocking valid
    # multi-joint chains where the unmoved base carries the pivot post. The
    # placed-geometry interference check (trimesh penetration) is the safety
    # net for the rare false-positive (R6 shaft in R5 bore would pass this
    # check as |6-5|=1, but the trimesh check catches the actual collision).
    try:
        from mac_assembly.selector_resolver import (
            cylinder_radii_with_role_along,
            probe_selector_anchor,
        )

        def _selector_cyl_role(anchor, step_path, side_label):
            """Resolve a SELECTOR cylinder anchor to its specific
            (status, role, radius). The previous reconcile_dimensions
            compared ALL cylinders along the joint axis on each part --
            a part with the joint's R5 bore plus an unrelated R6 bore
            would pair the R6 with an R5.5 shaft and false-pass the
            interference. Now: if the anchor has a cylinder SELECTOR
            query, resolve THAT specific cylinder and classify its role.

            Returns (status, role, radius):
              * status="found"     -- role/radius are the resolved
                cylinder's. Compare these specifically.
              * status="not_found" -- the query matched no face on this
                part. This is a SELECTOR miss (geometry absent); the
                caller must FAIL (blocking) -- do NOT fall back to the
                any-pair loop, which would silently PASS.
              * status="error"     -- infrastructure fault. UNVERIFIABLE.
              * status="no_selector" -- this anchor is not a cylinder
                SELECTOR. The caller falls back to the any-pair loop
                for this side (existing behavior).
            """
            if anchor.kind != AnchorKind.SELECTOR:
                return "no_selector", None, None
            sq = anchor.selector_query
            if sq is None or sq.surface != "cylinder":
                return "no_selector", None, None
            if not step_path:
                return "error", None, None
            query = sq.model_dump(mode="json", exclude_none=True)
            try:
                status, result = probe_selector_anchor(step_path, query)
            except Exception:  # noqa: BLE001
                return "error", None, None
            if status == "error":
                return "error", None, None
            if status == "not_found" or result is None:
                return "not_found", None, None
            role = str(result.get("role") or "unknown")
            radius = result.get("radius")
            if radius is None:
                return "error", None, None
            return "found", role, float(radius)

        for m in mates:
            if m.mate_type not in (MateType.REVOLUTE, MateType.COAXIAL,
                                   MateType.CYLINDRICAL):
                continue
            # SELECTOR cylinder's axis lives in selector_query.axis, not
            # anchor.axis (which is None for SELECTOR). Use _get_anchor_axis
            # (handles SELECTOR/AXIS_POINT/FACE) so the R6 radius check
            # queries the real joint axis; falls back to "z" only when
            # neither anchor defines a principal axis.
            axis = (
                m.slide_axis
                or _qa._get_anchor_axis(m.fixed_anchor)
                or _qa._get_anchor_axis(m.moving_anchor)
                or "z"
            )
            fixed_step = part_results[m.fixed_part_id].step_path
            moving_step = part_results[m.moving_part_id].step_path
            # Per-side SELECTOR resolution. If EITHER side has a cylinder
            # SELECTOR, we use the resolved specific cylinder for that
            # side and do NOT fall back to the any-pair loop for it.
            f_status, f_role, f_r = _selector_cyl_role(
                m.fixed_anchor, fixed_step, "fixed"
            )
            mv_status, mv_role, mv_r = _selector_cyl_role(
                m.moving_anchor, moving_step, "moving"
            )
            # SELECTOR-miss / probe-error handling: only fail if THAT
            # side actually had a cylinder SELECTOR. A non-SELECTOR
            # anchor (no_selector) falls through to the any-pair loop
            # below, preserving the existing behavior for AXIS_POINT /
            # FACE anchors on revolute joints.
            if f_status == "error" or mv_status == "error":
                errors.append(
                    f"reconcile: axis joint {m.mate_id!r} radii "
                    f"UNVERIFIABLE -- SELECTOR probe errored "
                    f"(infrastructure fault) for "
                    f"{('fixed' if f_status == 'error' else 'moving')} "
                    f"side; collision/kinematic QA will arbitrate"
                )
                continue
            if f_status == "not_found":
                # P1-2: do NOT pre-set attribution="part_geometry" here.
                # The selector miss may be a plan hallucination (spec
                # never promised this cylinder) OR a part build defect
                # (spec promised it but the STEP is missing it). The
                # feedback_router's _attribute_selector_miss decides
                # based on the PartSpec text. Emit the canonical
                # selector-miss format so the router's regex can parse
                # part_id + query.
                _f_sq = m.fixed_anchor.selector_query
                _f_sq_dict = (
                    _f_sq.model_dump(mode="json", exclude_none=True)
                    if _f_sq is not None else {}
                )
                errors.append(
                    f"SELECTOR anchor matched no face on "
                    f"{m.fixed_part_id}: {_f_sq_dict!r}"
                )
                continue
            if mv_status == "not_found":
                _mv_sq = m.moving_anchor.selector_query
                _mv_sq_dict = (
                    _mv_sq.model_dump(mode="json", exclude_none=True)
                    if _mv_sq is not None else {}
                )
                errors.append(
                    f"SELECTOR anchor matched no face on "
                    f"{m.moving_part_id}: {_mv_sq_dict!r}"
                )
                continue
            # Build the comparison pairs. If a side has a SELECTOR-resolved
            # cylinder, use ONLY that. Otherwise fall back to ALL
            # cylinders along the axis on that part (existing behavior).
            if f_status == "found":
                fixed_pairs = [(f_r, f_role)]
            else:
                fixed_pairs = cylinder_radii_with_role_along(
                    fixed_step, axis)
            if mv_status == "found":
                moving_pairs = [(mv_r, mv_role)]
            else:
                moving_pairs = cylinder_radii_with_role_along(
                    moving_step, axis)
            if not fixed_pairs or not moving_pairs:
                continue  # no cylinders along that axis on some side: skip
            # BUG-002: role-aware compatibility check. The previous
            # `abs(f - mv) <= 2.0` test was directionless -- an
            # interference fit (shaft larger than bore by 0-2mm) passed
            # as a valid clearance fit. Now we use topology role (inner
            # bore vs outer shaft) to determine direction.
            #
            # Rules per (fixed_r, fixed_role, moving_r, moving_role) pair:
            #   outer+inner (one shaft + one bore): clearance fit requires
            #     bore_r > shaft_r and 0 < bore_r - shaft_r <= 2.0mm.
            #     Exact fit (diff=0) is interference, no rotation room.
            #   inner+inner (both bores): implicit-pin case. Without an
            #     explicit implicit-pin marker on the mate, we cannot
            #     verify this is the intended semantics -- emit
            #     UNVERIFIABLE warning (degraded continue), do NOT
            #     silently PASS.
            #   outer+outer (both shafts): not a valid bearing arrangement
            #     for revolute/coaxial/cylindrical -- DEFINITE incompatible.
            #   any unknown role: UNVERIFIABLE warning (degraded).
            fits = False
            unverifiable = False
            for f_r2, f_role2 in fixed_pairs:
                for mv_r2, mv_role2 in moving_pairs:
                    if f_role2 == "unknown" or mv_role2 == "unknown":
                        unverifiable = True
                        continue
                    if f_role2 == "inner" and mv_role2 == "inner":
                        # Two bores: implicit-pin semantics not encoded on
                        # the mate -- UNVERIFIABLE, not a silent PASS.
                        unverifiable = True
                        continue
                    if f_role2 == "outer" and mv_role2 == "outer":
                        # Two shafts -- never a valid bearing pair.
                        continue
                    # One outer (shaft) + one inner (bore).
                    bore_r = f_r2 if f_role2 == "inner" else mv_r2
                    shaft_r = mv_r2 if mv_role2 == "outer" else f_r2
                    if 0 < (bore_r - shaft_r) <= 2.0:
                        fits = True
                        break
                if fits:
                    break
            if fits:
                continue
            fixed_summary = sorted(
                f"{round(r, 2)}/{role}" for r, role in fixed_pairs
            )
            moving_summary = sorted(
                f"{round(r, 2)}/{role}" for r, role in moving_pairs
            )
            if unverifiable:
                # Role-unknown or implicit-pin-without-explicit-marker:
                # degraded warning, NOT a blocking failure. Downstream
                # collision/kinematic QA is still authoritative.
                errors.append(
                    f"reconcile: axis joint {m.mate_id!r} radii UNVERIFIABLE -- "
                    f"{m.fixed_part_id} R={fixed_summary} "
                    f"vs {m.moving_part_id} R={moving_summary} "
                    f"along {axis}: role unknown or implicit-pin semantics; "
                    f"collision/kinematic QA will arbitrate"
                )
            else:
                errors.append(
                    f"reconcile: axis joint {m.mate_id!r} radii incompatible -- "
                    f"{m.fixed_part_id} R={fixed_summary} "
                    f"vs {m.moving_part_id} R={moving_summary} "
                    f"along {axis}: no compatible bore/shaft pair "
                    f"(need bore > shaft, 0..2mm clearance); check for "
                    f"interference or exact-fit (no rotation clearance)"
                )
                # Definite incompatibility is a PART-GEOMETRY defect
                # (the bores the parts actually carry don't fit), not a
                # mate-placement defect -- remating cannot change
                # measured radii. Only set attribution for the BLOCKING
                # path; UNVERIFIABLE stays on the warning path.
                attribution = "part_geometry"
                for pid in (m.fixed_part_id, m.moving_part_id):
                    if pid not in attribution_ids:
                        attribution_ids.append(pid)
    except Exception:  # noqa: BLE001 - radius check is best-effort
        pass

    # --- vertical stacking height vs envelope z ------------------------------
    env_z = brief.overall_envelope_mm.get("z")
    if not env_z:
        return ReconcileResult(errors, attribution, attribution_ids)
    # Only a purely vertical stacking tree has a predictable z height.
    if any(m.mate_type != MateType.FACE_TO_FACE for m in mates):
        return ReconcileResult(errors, attribution, attribution_ids)

    moved = {m.moving_part_id for m in mates}
    roots = [p.part_id for p in brief.parts if p.part_id not in moved]
    if len(roots) != 1:
        return ReconcileResult(errors, attribution, attribution_ids)

    children: dict[str, list] = {}
    for m in mates:
        children.setdefault(m.fixed_part_id, []).append(m)

    def stack_height(pid: str, seen: set[str]) -> float:
        if pid in seen:
            return 0.0
        seen.add(pid)
        bmin, bmax = measured[pid]
        own = bmax[2] - bmin[2]
        above = 0.0
        for m in children.get(pid, []):
            above = max(above, (m.offset_mm or 0.0) + stack_height(m.moving_part_id, seen))
        return own + above

    predicted_z = stack_height(roots[0], set())
    tol = env_z * _qa.cfg.ENVELOPE_TOLERANCE_PCT
    if abs(predicted_z - env_z) > tol:
        breakdown = " + ".join(
            f"{p.part_id}={measured[p.part_id][1][2] - measured[p.part_id][0][2]:.1f}"
            for p in brief.parts
        )
        gaps = sum(m.offset_mm or 0.0 for m in mates)
        errors.append(
            f"reconcile: measured stacking height {predicted_z:.1f}mm "
            f"({breakdown} + gaps {gaps:.1f}mm) vs envelope z {env_z:.1f}mm "
            f"(tol ±{tol:.1f}mm) -- adjust mates or envelope from MEASURED dims"
        )
    return ReconcileResult(errors, attribution, attribution_ids)


# Bind the facade after definitions so importing either module first works.
# Helpers are not called during import; shared state remains owned by the facade.
from mac_assembly import assembly_qa as _qa
from mac_assembly.assembly_qa import ReconcileResult
