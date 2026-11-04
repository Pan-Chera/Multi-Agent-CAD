"""Assembly mate alignment and interface checks.

Internal helpers; callers continue to use ``mac_assembly.assembly_qa``.
Resolve shared configuration, caches and replaceable helpers through that
module at call time so existing overrides and monkeypatch hooks stay live.
"""
from __future__ import annotations

import math
from mac_assembly.schemas_assembly import AnchorKind, MateCheck, MateType


def _check_mates(mates, placed, meshes_by_label=None, step_paths=None,
                 locations=None) -> list[MateCheck]:
    step_paths = step_paths or {}
    locations = locations or {}
    bboxes = {label: (bmin, bmax) for label, bmin, bmax in placed}
    moved_set = {m.moving_part_id for m in mates}
    checks: list[MateCheck] = []
    for mate in mates:
        # LINEAR/CYLINDRICAL are parametrised by position_mm, not offset_mm;
        # reporting the wrong "expected" makes the QA report incomparable.
        expected_mm = (
            mate.position_mm if mate.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL)
            else mate.offset_mm
        )
        chk = MateCheck(
            mate_id=mate.mate_id,
            mate_type=mate.mate_type,
            expected_mm=expected_mm,
            tolerance_mm=mate.tolerance_mm,
        )
        if mate.fixed_part_id not in bboxes or mate.moving_part_id not in bboxes:
            chk.passed = False
            chk.detail = "part bbox missing (part not placed?)"
            checks.append(chk)
            continue
        # Verifiable-placement gate: every MOVED part this mate references
        # must carry manifest location data, full stop (see
        # _moved_part_location -- a missing location has NO acceptable
        # approximation, because AABB equality cannot certify "unrotated").
        # Without it, every local->world transform below (SELECTOR point,
        # SPHERE centre, FACE/AXIS_POINT datum, axis direction) would be
        # a silent guess on a possibly rotated pose.
        _unverifiable = None
        for _pid in (mate.fixed_part_id, mate.moving_part_id):
            if _pid in moved_set:
                _loc, _loc_err = _qa._moved_part_location(_pid, locations)
                if _loc_err:
                    _unverifiable = _loc_err
                    break
        if _unverifiable:
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = f"UNVERIFIABLE: {_unverifiable}"
            checks.append(chk)
            continue
        # Fixed anchor: SELECTOR resolves on the real topology. For an
        # intermediate fixed part that was itself moved, the resolved point
        # is part-LOCAL -- shift it into world coords first (N1).
        fp_status, fp_sel = _qa._resolve_qa_point_3state(
            mate.fixed_anchor, step_paths.get(mate.fixed_part_id)
        )
        if fp_status == "found":
            fp = fp_sel
            if (
                mate.fixed_part_id in moved_set
                and step_paths.get(mate.fixed_part_id)
            ):
                fp = _qa._local_to_world_point(
                    fp_sel,
                    locations.get(mate.fixed_part_id),
                    step_paths[mate.fixed_part_id],
                    bboxes[mate.fixed_part_id],
                )
        elif fp_status == "not_found":
            # SELECTOR probe found NO matching face on the fixed part.
            # The previous code fell through to the FACE/AXIS_POINT
            # bbox fallback, treating the SELECTOR as if it named a
            # face -- the fixed datum became the part's bbox centre,
            # then the moving-side target verification passed,
            # silently false-PASSing the mate (BUG-002 P0 revision:
            # "SELECTOR 解析失败后的 bbox 中心不能当成有效基准").
            #
            # P1-2: canonical format so the router's
            # _SELECTOR_MISS_RE can parse part_id + query and
            # attribute via _attribute_selector_miss (spec-promised
            # -> remodel, plan hallucinated -> remate).
            _fp_sq = mate.fixed_anchor.selector_query
            _fp_sq_dict = (
                _fp_sq.model_dump(mode="json", exclude_none=True)
                if _fp_sq is not None else {}
            )
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = (
                f"SELECTOR anchor matched no face on "
                f"{mate.fixed_part_id}: {_fp_sq_dict!r}"
            )
            checks.append(chk)
            continue
        elif fp_status == "error":
            # Infrastructure fault -- UNVERIFIABLE, not a silent PASS.
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = (
                f"UNVERIFIABLE: fixed SELECTOR probe errored "
                f"(infrastructure fault) on {mate.fixed_part_id}"
            )
            checks.append(chk)
            continue
        elif mate.fixed_anchor.kind == AnchorKind.SPHERE:
            # SPHERE fixed anchor (ball mate's socket side): sphere_center_mm
            # is part-LOCAL. For a moved fixed part (chained ball mate),
            # transform through the placed location; for a root fixed part
            # at origin, the transform is identity and this still returns
            # the part-local value.
            sc = mate.fixed_anchor.sphere_center_mm or [0.0, 0.0, 0.0]
            fp = _qa._local_to_world_point(
                (float(sc[0]), float(sc[1]), float(sc[2])),
                locations.get(mate.fixed_part_id),
                step_paths.get(mate.fixed_part_id),
                bboxes[mate.fixed_part_id],
            )
        else:
            # FACE / AXIS_POINT fixed anchor. For an unmoved fixed part the
            # placed-bbox algebra is exact. For a MOVED fixed part (placed
            # by a parent mate) the datum must be derived on the ORIGINAL
            # bbox and pushed through the placed Location -- a rotated
            # part's world "top" bbox face is a different physical face
            # than its local top (P1-6).
            fp, _fp_dir, fp_err = _qa._anchor_world_datum(
                mate.fixed_anchor, mate.fixed_part_id, bboxes,
                step_paths, locations, moved_set,
            )
            if fp is None:
                chk.passed = False
                chk.delta_mm = mate.tolerance_mm * 2
                chk.detail = f"UNVERIFIABLE: {fp_err}"
                checks.append(chk)
                continue

        # Validate both SELECTOR anchors before any mate-type branch can
        # return early.  Previously LINEAR/CYLINDRICAL and
        # REVOLUTE/COAXIAL only checked the fixed datum and could PASS even
        # when the moving selector named geometry that did not exist.
        selector_failed = False
        for anchor_label, part_id, anchor in (
            ("fixed", mate.fixed_part_id, mate.fixed_anchor),
            ("moving", mate.moving_part_id, mate.moving_anchor),
        ):
            ok, detail, _result = _qa._validate_selector_anchor(
                anchor,
                step_paths.get(part_id),
                part_id,
                anchor_label,
                mate.tolerance_mm,
            )
            if not ok:
                chk.passed = False
                chk.delta_mm = mate.tolerance_mm * 2
                chk.detail = detail
                checks.append(chk)
                selector_failed = True
                break
        if selector_failed:
            continue

        if mate.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL):
            # Slide joints have no single point datum inside the moving part,
            # but the slide datum (fixed anchor) must still land near the
            # placed moving part. This basic proximity check works even when
            # the kinematic sweep is disabled (R5 -- was an unconditional pass).
            mbmin, mbmax = bboxes[mate.moving_part_id]
            gap = _qa._point_bbox_gap(fp, mbmin, mbmax)
            diag = math.sqrt(sum(
                (mbmax[k] - mbmin[k]) ** 2 for k in range(3)
            ))
            tol = max(diag, 1.0)  # datum may sit up to ~one part-diagonal away
            chk.measured_mm = gap
            chk.delta_mm = gap
            chk.passed = gap <= tol
            chk.detail = (
                f"{mate.mate_type.value} datum-proximity sanity check: "
                f"slide datum {gap:.1f}mm from moving part (limit "
                f"{tol:.1f}mm). This does NOT verify the static "
                f"position_mm={mate.position_mm} (joint-enforced at "
                f"placement, not independently re-derived); motion range "
                f"validated by sweep"
            )
            checks.append(chk)
            continue

        if mate.mate_type in (MateType.REVOLUTE, MateType.COAXIAL):
            # Motion joints rotate the moving part by their static pose, so
            # the moving bbox centre no longer marks the datum. Robust check
            # instead: the joint pivot (fixed-side anchor SHIFTED by
            # axial_offset_mm along the rotation axis -- the actual location
            # the moving part's bore aligns to) must land inside (or near)
            # the placed moving part's bbox. Without the axial_offset shift
            # the check uses the unshifted cylinder midpoint, which for a
            # stacked joint (post midpoint Z=14, axial_offset=-3 -> actual
            # pivot Z=11; arm bbox Z=8..14 -> unshifted Z=14 fails on
            # boundary, shifted Z=11 passes inside) gives false FAILs.
            mbmin, mbmax = bboxes[mate.moving_part_id]
            tol = max(mate.tolerance_mm, 1.0)
            # Resolve the fixed anchor's axis direction (for SELECTOR, the
            # resolver returns it in part-local coords; transform to world
            # like the point -- rotation only, via the shared helper).
            axis_dir = None
            resolved_full = _qa._resolve_qa_anchor(
                mate.fixed_anchor, step_paths.get(mate.fixed_part_id)
            )
            if resolved_full:
                ax_local = tuple(resolved_full["axis"])
                if sum(abs(v) for v in ax_local) > 1e-9:
                    if mate.fixed_part_id in moved_set:
                        axis_dir = _qa._local_to_world_direction(
                            ax_local, locations.get(mate.fixed_part_id)
                        )
                        if axis_dir is None:
                            # Degenerate transform: fail explicitly instead
                            # of silently sweeping about a fallback axis.
                            chk.passed = False
                            chk.delta_mm = tol * 2
                            chk.detail = (
                                "UNVERIFIABLE: selector axis direction "
                                "degenerate after transform through "
                                f"{mate.fixed_part_id}'s placed location"
                            )
                            checks.append(chk)
                            continue
                    else:
                        axis_dir = ax_local
            if not axis_dir or sum(abs(v) for v in axis_dir) < 1e-9:
                # Mirror codegen's axis derivation (_get_anchor_axis maps a
                # FACE anchor to its normal axis, e.g. left/right -> x).
                # anchor.axis first so AXIS_POINT keeps its explicit axis;
                # raw `anchor.axis or "z"` would sweep every FACE-anchored
                # revolute about Z regardless of the face normal.
                axis_name = (
                    mate.fixed_anchor.axis
                    or _qa._get_anchor_axis(mate.fixed_anchor)
                    or "z"
                )
                axis_dir = _qa._AXIS_DIRS.get(axis_name, _qa._AXIS_DIRS["z"])
                if mate.fixed_part_id in moved_set:
                    # A moved fixed part's LOCAL principal axis is not a
                    # world axis (parent mate rotated it): transform it too
                    # instead of sweeping about an unrotated direction.
                    # A degenerate transform fails the check explicitly --
                    # silently keeping the unrotated axis would sweep about
                    # a direction the placed part no longer has.
                    transformed = _qa._local_to_world_direction(
                        axis_dir, locations.get(mate.fixed_part_id)
                    )
                    if transformed is None:
                        chk.passed = False
                        chk.delta_mm = tol * 2
                        chk.detail = (
                            "UNVERIFIABLE: fixed-part axis "
                            f"{axis_name!r} degenerate after transform "
                            f"through {mate.fixed_part_id}'s placed location"
                        )
                        checks.append(chk)
                        continue
                    axis_dir = transformed
            # Apply axial_offset_mm to the fixed anchor point along the
            # axis direction. Same math as codegen's runtime _shift_pt --
            # both call mac_assembly.geometry_utils.shift_pt so they cannot
            # diverge.
            off = mate.axial_offset_mm or 0.0
            fp_shifted = _qa.shift_pt(fp, axis_dir, off)
            inside = all(
                mbmin[k] - tol <= fp_shifted[k] <= mbmax[k] + tol
                for k in range(3)
            )
            chk.measured_mm = 0.0
            chk.delta_mm = 0.0 if inside else tol * 2
            chk.passed = inside
            chk.detail = (
                f"pivot ({fp_shifted[0]:.1f},{fp_shifted[1]:.1f},"
                f"{fp_shifted[2]:.1f}) [fixed {fp[0]:.1f},{fp[1]:.1f},"
                f"{fp[2]:.1f} + axial_offset {off:.1f}] "
                f"{'inside' if inside else 'OUTSIDE'} moving bbox"
            )
            checks.append(chk)
            continue

        # Moving anchor: SELECTOR resolves in the part's LOCAL coords, which
        # are stale after placement -- only usable for geometry-shape checks,
        # not world-position deltas.
        mp_status, mp_sel = _qa._resolve_qa_point_3state(
            mate.moving_anchor, step_paths.get(mate.moving_part_id)
        )
        mp_local = (mp_status == "found")
        if mp_status == "found":
            mp = mp_sel
        elif mp_status == "not_found":
            # SELECTOR probe found NO matching face on the moving part.
            # Same fail-closed rule as the fixed side (BUG-002 P0
            # revision): do NOT fall through to bbox algebra.
            #
            # P1-2: canonical format so the router's
            # _SELECTOR_MISS_RE can parse part_id + query.
            _mp_sq = mate.moving_anchor.selector_query
            _mp_sq_dict = (
                _mp_sq.model_dump(mode="json", exclude_none=True)
                if _mp_sq is not None else {}
            )
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = (
                f"SELECTOR anchor matched no face on "
                f"{mate.moving_part_id}: {_mp_sq_dict!r}"
            )
            checks.append(chk)
            continue
        elif mp_status == "error":
            chk.passed = False
            chk.delta_mm = mate.tolerance_mm * 2
            chk.detail = (
                f"UNVERIFIABLE: moving SELECTOR probe errored "
                f"(infrastructure fault) on {mate.moving_part_id}"
            )
            checks.append(chk)
            continue
        elif mate.moving_anchor.kind == AnchorKind.SPHERE:
            # SPHERE moving anchor (ball mate's ball side): sphere_center_mm
            # is part-LOCAL; transform through the placed location or QA
            # reads it as a world point and reports a false delta. The
            # connect() call did translate the part — we must mirror that
            # transform here, otherwise every ball mate fails with a
            # delta equal to the intended translation.
            sc = mate.moving_anchor.sphere_center_mm or [0.0, 0.0, 0.0]
            mp = _qa._local_to_world_point(
                (float(sc[0]), float(sc[1]), float(sc[2])),
                locations.get(mate.moving_part_id),
                step_paths.get(mate.moving_part_id),
                bboxes[mate.moving_part_id],
            )
        else:
            # FACE / AXIS_POINT moving anchor. The moving part is always in
            # moved_set (it was just placed by this mate), so derive the
            # datum on the ORIGINAL bbox and transform through the placed
            # Location -- the placed-bbox face of a rotated part is a
            # different physical face than the anchor names (P1-6).
            mp, _mp_dir, mp_err = _qa._anchor_world_datum(
                mate.moving_anchor, mate.moving_part_id, bboxes,
                step_paths, locations, moved_set,
            )
            if mp is None:
                chk.passed = False
                chk.delta_mm = mate.tolerance_mm * 2
                chk.detail = f"UNVERIFIABLE: {mp_err}"
                checks.append(chk)
                continue
        # Rigid translation_mm is expressed in the fixed part's LOCAL
        # frame. Mirror codegen by rotating that basis through the fixed
        # part's placed transform before comparing world-space datums.
        if mate.mate_type == MateType.RIGID and any(
            abs(float(v)) > 1e-12 for v in mate.translation_mm
        ):
            basis = []
            for axis in (
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, 1.0),
            ):
                world_axis = axis
                if mate.fixed_part_id in moved_set:
                    world_axis = _qa._local_to_world_direction(
                        axis, locations.get(mate.fixed_part_id)
                    )
                if world_axis is None:
                    # BUG-042: the previous code fell back to the
                    # UNROTATED local axis here -- silently mis-measuring
                    # the world translation on a rotated part and
                    # potentially false-passing. Fail closed instead:
                    # mark the mate UNVERIFIABLE so the router can remate.
                    chk.passed = False
                    chk.delta_mm = mate.tolerance_mm * 2
                    chk.detail = (
                        f"UNVERIFIABLE: rigid translation_mm basis axis "
                        f"{axis} could not be transformed into a world "
                        f"direction (degenerate transform through "
                        f"{mate.fixed_part_id}'s placed location) -- "
                        f"refusing to fall back to the unrotated local "
                        f"axis and risk a false PASS"
                    )
                    checks.append(chk)
                    # Skip the rest of this mate's checks.
                    basis = None
                    break
                basis.append(world_axis)
            if basis is None:
                continue
            tv = tuple(float(v) for v in mate.translation_mm)
            fp = tuple(
                fp[k] + sum(tv[j] * basis[j][k] for j in range(3))
                for k in range(3)
            )
        dx, dy, dz = mp[0] - fp[0], mp[1] - fp[1], mp[2] - fp[2]

        if mate.mate_type == MateType.FACE_TO_FACE:
            # Lateral offset is only re-derivable from world anchors; a
            # SELECTOR moving anchor is local (post-placement stale).
            lateral = 0.0 if mp_local else math.hypot(dx, dy)
            lat_src = "n/a (selector anchor)" if mp_local else f"{lateral:.3f}mm"
            # Prefer the REAL topology gap (min surface distance) over the
            # bbox-derived dz: it stays correct when a part's bbox extreme is
            # not the mating face (knob on a lid, skirt below a cap).
            real_gap = None
            if meshes_by_label is not None:
                real_gap = _qa._min_gap_between(
                    meshes_by_label.get(mate.fixed_part_id, []),
                    meshes_by_label.get(mate.moving_part_id, []),
                )
            if real_gap is None and mp_local:
                # Unverifiable combination (D8): the only remaining gap
                # source is abs(dz), but dz mixes a part-LOCAL selector
                # point (mp, stale after placement -- see above) with a
                # WORLD fixed point (fp), a physically meaningless number
                # that would pass/fail at random. Refuse to fabricate a
                # measurement; fail loudly with an explicit reason. The
                # synthetic delta follows the pivot check's tol*2
                # convention for "failed without a real measurement".
                chk.measured_mm = 0.0
                chk.delta_mm = mate.tolerance_mm * 2
                chk.passed = False
                chk.detail = (
                    "UNVERIFIABLE: face_to_face gap needs the topology "
                    "measurement, but no meshes are available for these "
                    "parts, and the SELECTOR moving anchor resolves to a "
                    "part-local point that cannot be compared against the "
                    "world fixed anchor (the bbox dz fallback would be a "
                    "meaningless local-vs-world difference)"
                )
                checks.append(chk)
                continue
            gap_meas = real_gap if real_gap is not None else abs(dz)
            gap_src = "topology" if real_gap is not None else "bbox"
            axial_delta = gap_meas - mate.offset_mm
            chk.measured_mm = gap_meas
            chk.delta_mm = max(abs(axial_delta), lateral)
            chk.passed = chk.delta_mm <= mate.tolerance_mm
            chk.detail = (
                f"gap={gap_meas:.3f}mm [{gap_src}] (expected {mate.offset_mm:.3f}), "
                f"lateral={lat_src}"
            )
        else:
            if mp_local:
                # Rigid datum with a SELECTOR moving anchor. The joint
                # (connect aligns the resolved real datum) enforces world
                # coincidence, so the local point cannot be re-derived as
                # a world position. But we CAN verify the resolver picked
                # the intended datum: if the user stated target_x/y/z_mm in
                # the selector_query (disambiguating multi-bore parts --
                # e.g. a link_bar's +X vs -X bore, or a clevis twin), the
                # resolved part-local point must match the stated target
                # within tolerance. Without this, a wrong-bore pick
                # (target=+26, resolver returns -26) goes undetected --
                # the joint still aligns SOMETHING, just not the intended
                # datum, and the moving part lands in the wrong place.
                #
                # BUG-002 (P0 revision): probe BOTH fixed and moving
                # SELECTOR anchors with their OWN selector_query. The
                # previous code re-used the MOVING anchor's query for
                # BOTH probes -- so when the moving query had a target
                # (entering the per-axis target check branch below),
                # the fixed-side uniqueness was never checked, silently
                # passing a multi-bore fixed part. Now: each side is
                # probed independently, regardless of the other side's
                # target state.
                fixed_step = step_paths.get(mate.fixed_part_id)
                moving_step = step_paths.get(mate.moving_part_id)

                def _probe_anchor_uniqueness(
                    anchor, step_path: str | None,
                    anchor_label: str, part_id: str,
                ) -> tuple[bool, str]:
                    """Probe a SELECTOR anchor with its OWN query.

                    Returns (passed, detail).

                    P0-1 requirement 6: even with a target, candidate_count
                    must be 1 for the mate to pass. A target_z_mm alone
                    on two parallel Z-axis bores cannot disambiguate
                    (both clusters' along extents contain z) -- the count
                    stays >= 2 -> fail.

                    P1-2 requirement 7: selector-miss detail uses the
                    canonical ``SELECTOR anchor matched no face on
                    <part_id>: <query_dict_repr>`` format so the router's
                    ``_SELECTOR_MISS_RE`` can parse part_id + query.
                    """
                    if anchor.kind != AnchorKind.SELECTOR:
                        return True, (
                            f"{anchor_label} non-SELECTOR anchor "
                            f"(no uniqueness probe)"
                        )
                    sq = anchor.selector_query
                    if sq is None:
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"has no selector_query on {part_id}"
                        )
                    if not step_path:
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"anchor has no step_path on {part_id}"
                        )
                    sq_dict = sq.model_dump(
                        mode="json", exclude_none=True
                    )
                    try:
                        status, result = _qa._probe_selector_anchor(
                            step_path, sq_dict
                        )
                    except Exception:  # noqa: BLE001
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"probe errored (infrastructure fault) "
                            f"on {part_id}"
                        )
                    if status == "error":
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"probe errored (infrastructure fault) "
                            f"on {part_id}"
                        )
                    if status == "not_found" or result is None:
                        # Selector-miss: canonical format so the router's
                        # _SELECTOR_MISS_RE can parse part_id + query and
                        # attribute via _attribute_selector_miss (spec-
                        # promised -> remodel, plan hallucinated -> remate).
                        return False, (
                            f"SELECTOR anchor matched no face on "
                            f"{part_id}: {sq_dict!r}"
                        )
                    if status != "found":
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"probe returned {status!r} on {part_id}"
                        )
                    cand = int(result.get("candidate_count") or 0)
                    if cand == 0:
                        # Defensive: status=found but count=0.
                        return False, (
                            f"SELECTOR anchor matched no face on "
                            f"{part_id}: {sq_dict!r}"
                        )
                    has_target = (
                        sq.target_x_mm is not None
                        or sq.target_y_mm is not None
                        or sq.target_z_mm is not None
                    )
                    if cand == 1:
                        if has_target:
                            return True, (
                                f"{anchor_label} selector-anchored "
                                f"(target-disambiguated, unique match) "
                                f"on {part_id}"
                            )
                        return True, (
                            f"{anchor_label} selector-anchored rigid "
                            f"datum (unique candidate, no target to "
                            f"verify) on {part_id}"
                        )
                    # cand > 1: target did not actually disambiguate
                    # (e.g. target_z_mm on two parallel Z-axis bores)
                    # OR no target at all. Either way, ambiguous.
                    if has_target:
                        return False, (
                            f"UNVERIFIABLE: {anchor_label} SELECTOR "
                            f"has {cand} candidate faces on {part_id} "
                            f"despite target -- target does not "
                            f"disambiguate (lateral vs along-axis "
                            f"mismatch); router should remate with a "
                            f"lateral target"
                        )
                    return False, (
                        f"UNVERIFIABLE: {anchor_label} SELECTOR has "
                        f"{cand} candidate faces on {part_id} but no "
                        f"target_x/y/z_mm to disambiguate -- "
                        f"router should remate with a target"
                    )

                # Probe BOTH fixed and moving anchors with their OWN
                # queries -- independent of whether either has a target.
                fix_pass, fix_detail = _probe_anchor_uniqueness(
                    mate.fixed_anchor, fixed_step,
                    "fixed", mate.fixed_part_id,
                )
                if not fix_pass:
                    chk.measured_mm = mate.tolerance_mm * 2
                    chk.delta_mm = mate.tolerance_mm * 2
                    chk.passed = False
                    chk.detail = fix_detail
                    checks.append(chk)
                    continue
                mov_pass, mov_detail = _probe_anchor_uniqueness(
                    mate.moving_anchor, moving_step,
                    "moving", mate.moving_part_id,
                )
                if not mov_pass:
                    chk.measured_mm = mate.tolerance_mm * 2
                    chk.delta_mm = mate.tolerance_mm * 2
                    chk.passed = False
                    chk.detail = mov_detail
                    checks.append(chk)
                    continue

                # Per-axis target check on the moving anchor (if it
                # specified target_x/y/z_mm). The resolved point is the
                # chosen cylinder's bbox midpoint = bore centre (modulo
                # numerical precision); tol = max(tolerance_mm, 0.5) is
                # generous. A wrong-bore pick is typically off by tens
                # of mm, so this is a strong signal.
                sq = mate.moving_anchor.selector_query
                targets: list[tuple[str, int, float]] = []
                if sq is not None:
                    if sq.target_x_mm is not None:
                        targets.append(("x", 0, float(sq.target_x_mm)))
                    if sq.target_y_mm is not None:
                        targets.append(("y", 1, float(sq.target_y_mm)))
                    if sq.target_z_mm is not None:
                        targets.append(("z", 2, float(sq.target_z_mm)))
                if not targets:
                    # Both unique, no target to verify -> joint-enforced
                    # PASS.
                    chk.measured_mm = 0.0
                    chk.delta_mm = 0.0
                    chk.passed = True
                    chk.detail = (
                        "selector-anchored rigid datum (joint-enforced; "
                        "unique candidates on both fixed and moving, "
                        "no target to verify)"
                    )
                    checks.append(chk)
                    continue
                tol = max(mate.tolerance_mm, 0.5)
                worst_axis, worst_delta = "", 0.0
                for axis_name, idx, target_val in targets:
                    delta = abs(mp_sel[idx] - target_val)
                    if delta > worst_delta:
                        worst_axis, worst_delta = axis_name, delta
                chk.measured_mm = worst_delta
                chk.delta_mm = worst_delta
                chk.passed = worst_delta <= tol
                target_str = ", ".join(f"{n}={v}" for n, _, v in targets)
                chk.detail = (
                    f"selector-anchored rigid datum target check: "
                    f"resolved ({mp_sel[0]:.2f},{mp_sel[1]:.2f},"
                    f"{mp_sel[2]:.2f}) vs target [{target_str}]; "
                    f"worst axis {worst_axis} delta={worst_delta:.3f}mm "
                    f"(tol {tol:.3f}mm)"
                )
                checks.append(chk)
                continue
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            chk.measured_mm = dist
            chk.delta_mm = dist
            chk.passed = dist <= mate.tolerance_mm
            chk.detail = (
                f"datum coincidence dist={dist:.3f}mm "
                f"(delta=({dx:.2f},{dy:.2f},{dz:.2f}))"
            )
        checks.append(chk)
    return checks


# Bind the facade after definitions so importing either module first works.
# Helpers are not called during import; shared state remains owned by the facade.
from mac_assembly import assembly_qa as _qa
