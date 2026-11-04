"""Motion-joint sweeps and moving-subtree collision checks.

Internal helpers; callers continue to use ``mac_assembly.assembly_qa``.
Resolve shared configuration, caches and replaceable helpers through that
module at call time so existing overrides and monkeypatch hooks stay live.
"""
from __future__ import annotations

import math
from mac_assembly.schemas_assembly import KinematicCheck, MateType


def _moving_subtree(mates, root_id: str) -> set[str]:
    """Parts rigidly carried by ``root_id`` (BFS through mate graph)."""
    subtree = {root_id}
    frontier = [root_id]
    while frontier:
        cur = frontier.pop()
        for m in mates:
            if m.fixed_part_id == cur and m.moving_part_id not in subtree:
                subtree.add(m.moving_part_id)
                frontier.append(m.moving_part_id)
    return subtree


def _check_kinematics(
    placed, mates, meshes_by_label, step_paths=None, locations=None
) -> list[KinematicCheck]:
    """Sweep every motion joint's moving subtree and test collisions
    against the static structure at each sample pose.

    * REVOLUTE     -- rotate the subtree about the joint axis over
      +/- ``KINEMATIC_SWEEP_DEG``.
    * LINEAR -- use explicit joint limits when available; otherwise sweep
      over +/- ``LINEAR_SWEEP_MM``. CYLINDRICAL uses the fallback sweep.

    A part may export as several connected shells (B5): every shell of
    the moving subtree moves together and every shell of the static
    structure is tested. The mesh is already AT the static pose, so each
    sample applies a relative transform on top of it.
    """
    if not _qa.cfg.KINEMATIC_ENABLED:
        return []
    step_paths = step_paths or {}
    locations = locations or {}
    moved_set = {m.moving_part_id for m in mates}
    motion = [
        m for m in mates
        if m.mate_type in (MateType.REVOLUTE, MateType.LINEAR, MateType.CYLINDRICAL)
    ]
    if not motion:
        return []

    import numpy as np
    import trimesh

    bboxes = {label: (bmin, bmax) for label, bmin, bmax in placed}

    checks: list[KinematicCheck] = []
    for mate in motion:
        rotational = mate.mate_type == MateType.REVOLUTE
        if rotational:
            lower, upper = mate.limit_lower, mate.limit_upper
            if lower is None or upper is None:
                lower, upper = -_qa.cfg.KINEMATIC_SWEEP_DEG, _qa.cfg.KINEMATIC_SWEEP_DEG
            samples = np.unique(np.concatenate((
                np.linspace(lower, upper, _qa.cfg.KINEMATIC_SWEEP_SAMPLES),
                np.array([0.0]),
            )))
            unit = "deg"
        else:
            lower = mate.limit_lower if mate.mate_type == MateType.LINEAR else None
            upper = mate.limit_upper if mate.mate_type == MateType.LINEAR else None
            if lower is not None and upper is not None:
                samples = np.unique(np.concatenate((
                    np.linspace(lower, upper, _qa.cfg.LINEAR_SWEEP_SAMPLES),
                    np.array([0.0]),
                )))
            else:
                samples = np.linspace(
                    -_qa.cfg.LINEAR_SWEEP_MM, _qa.cfg.LINEAR_SWEEP_MM,
                    _qa.cfg.LINEAR_SWEEP_SAMPLES,
                )
            unit = "mm"

        chk = KinematicCheck(
            mate_id=mate.mate_id,
            # `sweep_deg` kept (legacy QA JSON compat) but its name is only
            # accurate for revolute; `samples` + `sweep_unit` are the
            # authoritative fields for readers (mm for linear/cylindrical).
            sweep_deg=[float(s) for s in samples],
            samples=[float(s) for s in samples],
            sweep_unit=unit,
        )
        if (mate.fixed_part_id not in bboxes or mate.moving_part_id not in bboxes
                or mate.fixed_part_id not in meshes_by_label
                or mate.moving_part_id not in meshes_by_label):
            chk.passed = False
            chk.detail = "joint parts missing from placed geometry"
            checks.append(chk)
            continue
        # Verifiable-placement gate for the FIXED part: the sweep axis /
        # pivot are derived in its local frame and transformed through its
        # placed Location. A moved fixed part without manifest location
        # data would be swept about a silently guessed world axis -- fail
        # explicitly instead (no approximation is accepted; see
        # _moved_part_location). The moving part's own location is not
        # transformed here (its meshes are swept relative to the static
        # pose), so it is not gated.
        if mate.fixed_part_id in moved_set:
            _loc, _loc_err = _qa._moved_part_location(
                mate.fixed_part_id, locations
            )
            if _loc_err:
                chk.passed = False
                chk.detail = f"UNVERIFIABLE: {_loc_err}"
                checks.append(chk)
                continue

        if mate.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL):
            # slide_axis is the joint axis in the FIXED part's LOCAL frame
            # (codegen emits it as an Axis on the fixed part). When the
            # fixed part was itself moved/rotated by a parent mate, the
            # local principal axis is NOT the world slide direction --
            # transform it through the placed Location (same rotation-only
            # math as the revolute SELECTOR axis) instead of sweeping along
            # an unrotated world axis.
            axis_dir = _qa._AXIS_DIRS[mate.slide_axis or "x"]
            axis_pt = None  # translation sweep needs no pivot point (R4)
            if mate.fixed_part_id in moved_set:
                transformed = _qa._local_to_world_direction(
                    axis_dir, locations.get(mate.fixed_part_id)
                )
                if transformed is None:
                    chk.passed = False
                    chk.detail = (
                        f"slide axis {mate.slide_axis!r} could not be "
                        f"transformed to a world direction (degenerate "
                        f"transform through {mate.fixed_part_id}'s placed "
                        f"location)"
                    )
                    checks.append(chk)
                    continue
                axis_dir = transformed
        else:
            # REVOLUTE / COAXIAL: a SELECTOR fixed anchor carries the axis in
            # selector_query (anchor.axis is None) -- resolve the REAL
            # topology axis point+direction (N2b: sweeping the nominal z
            # instead of the query axis gives wrong collision results), then
            # shift the local point into world coords if the fixed part was
            # itself moved (N1).
            #
            # P1-1: use the 3-state probe so a SELECTOR miss / probe error
            # fails closed (no silent bbox fallback). A Box part with a
            # cylinder SELECTOR query used to fall through to the
            # FACE/AXIS_POINT bbox branch and silently sweep about Z.
            ka_status, ka_result = _qa._resolve_qa_anchor_3state(
                mate.fixed_anchor, step_paths.get(mate.fixed_part_id)
            )
            if ka_status == "found" and ka_result:
                axis_pt = tuple(ka_result["point"])
                ax = tuple(ka_result["axis"])
                axis_dir = ax if sum(abs(v) for v in ax) > 1e-9 else _qa._AXIS_DIRS["z"]
                if mate.fixed_part_id in moved_set:
                    axis_pt = _qa._local_to_world_point(
                        axis_pt,
                        locations.get(mate.fixed_part_id),
                        step_paths.get(mate.fixed_part_id),
                        bboxes[mate.fixed_part_id],
                    )
                    # N1 follow-up: the local axis DIRECTION must be rotated
                    # into world too -- previously only the POINT was
                    # transformed, so a chained joint (parent rotated the
                    # fixed part) swept about the wrong world axis.
                    transformed = _qa._local_to_world_direction(
                        axis_dir, locations.get(mate.fixed_part_id)
                    )
                    if transformed is None:
                        chk.passed = False
                        chk.detail = (
                            f"joint axis direction could not be transformed "
                            f"to a world direction (degenerate transform "
                            f"through {mate.fixed_part_id}'s placed location)"
                        )
                        checks.append(chk)
                        continue
                    axis_dir = transformed
            elif ka_status == "not_found":
                # SELECTOR probe found NO matching face on the fixed part.
                # Fail closed with the selector-miss signature so the
                # router can attribute (spec-promised vs plan
                # hallucinated). Do NOT fall through to bbox algebra --
                # a Box part with a cylinder SELECTOR has no real axis
                # to sweep about.
                sq = mate.fixed_anchor.selector_query
                sq_dict = (
                    sq.model_dump(mode="json", exclude_none=True)
                    if sq is not None else {}
                )
                chk.passed = False
                chk.detail = (
                    f"SELECTOR anchor matched no face on "
                    f"{mate.fixed_part_id}: {sq_dict!r}"
                )
                checks.append(chk)
                continue
            elif ka_status == "error":
                # STEP unreadable / cadpy unavailable / SELECTOR + no
                # step_path / probe exception. UNVERIFIABLE.
                chk.passed = False
                chk.detail = (
                    f"UNVERIFIABLE: fixed SELECTOR probe errored "
                    f"(infrastructure fault) on {mate.fixed_part_id}"
                )
                checks.append(chk)
                continue
            else:
                # no_selector: non-SELECTOR anchor (FACE / AXIS_POINT).
                # Same fallback as the mate-delta check above: FACE anchors
                # carry axis=None, so derive the normal axis via
                # _get_anchor_axis instead of blindly sweeping about Z.
                axis_name = (
                    mate.fixed_anchor.axis
                    or _qa._get_anchor_axis(mate.fixed_anchor)
                    or "z"
                )
                axis_dir = _qa._AXIS_DIRS.get(axis_name, _qa._AXIS_DIRS["z"])
                if mate.fixed_part_id in moved_set:
                    transformed = _qa._local_to_world_direction(
                        axis_dir, locations.get(mate.fixed_part_id)
                    )
                    if transformed is None:
                        chk.passed = False
                        chk.detail = (
                            f"joint axis direction could not be transformed "
                            f"to a world direction (degenerate transform "
                            f"through {mate.fixed_part_id}'s placed location)"
                        )
                        checks.append(chk)
                        continue
                    axis_dir = transformed
                # FACE / AXIS_POINT pivot point: derive on the ORIGINAL bbox
                # and transform through the placed Location for moved parts
                # (P1-6) -- the placed world bbox reads the wrong face off a
                # rotated part.
                axis_pt, _pt_dir, pt_err = _qa._anchor_world_datum(
                    mate.fixed_anchor, mate.fixed_part_id, bboxes,
                    step_paths, locations, moved_set,
                )
                if axis_pt is None:
                    chk.passed = False
                    chk.detail = f"UNVERIFIABLE: {pt_err}"
                    checks.append(chk)
                    continue
        subtree = _qa._moving_subtree(mates, mate.moving_part_id)
        # The moving part itself is checked above, but a chained joint's
        # subtree (A->B->C: sweeping A's joint must carry C too) can contain
        # parts whose placed STL export failed (the codegen footer swallows
        # per-part export errors). meshes_by_label.get(sl, []) would then
        # silently sweep nothing for those parts -- a vacuous pass. Fail
        # the check instead so the router rebuilds the assembly script.
        missing_meshes = [sl for sl in subtree if sl not in meshes_by_label]
        if missing_meshes:
            chk.passed = False
            chk.detail = (
                f"subtree parts missing placed meshes: {missing_meshes} "
                f"(per-part STL export failed) -- refusing to pass a sweep "
                f"that would silently skip them"
            )
            checks.append(chk)
            continue
        statics = [l for l in meshes_by_label if l not in subtree]

        collisions: list[float] = []
        first_detail = ""
        # BUG-003: the joint's own moving/fixed interface must be swept
        # too. The previous blanket skip ("direct mate pair, never a
        # collision") meant a slider ramming its end-stop or a rotating
        # arm sweeping into its own pivot post went undetected. Now we
        # sweep the direct pair, but use baseline comparison.
        #
        # Baseline tracking is per (moving_mesh, static_mesh) PAIR, not
        # per (moving_part, fixed_part) -- one mesh pair's baseline
        # contact must NOT exempt a NEW collision on another mesh pair
        # of the same direct part interface (e.g. a tessellated part
        # exporting as multiple connected components).
        #
        # For each mesh pair we track TWO things:
        #   * baseline_hit (bool): was this pair colliding at sample 0?
        #   * baseline_vol (float): if so, what was the penetration volume?
        #   * baseline_probe_failed (set): the probe at sample 0 crashed.
        #
        # Failure rule (BUG-003 revision):
        #   * If baseline probe failed: UNVERIFIABLE for that mesh pair.
        #   * If baseline_hit is False: ANY hit at ANY sweep sample
        #     (including sample 0 in the sweep, which should be identical
        #     to the baseline probe -- a contradiction indicates either
        #     a probe bug or a real new collision) is a NEW collision
        #     and must FAIL. The volume tolerance does NOT apply here --
        #     a 1.0 mm³ collision in a pair that had no contact at
        #     baseline is a real failure, not "within tolerance".
        #   * If baseline_hit is True: use deepening tolerance. Allow
        #     penetration up to baseline_vol + INTERFERENCE_VOLUME_TOL_MM3;
        #     deeper penetration fails.
        baseline_hit: dict[tuple[int, int], bool] = {}
        baseline_vol: dict[tuple[int, int], float] = {}
        baseline_probe_failed: set[tuple[int, int]] = set()
        # Pre-sample at s=0 to record the designed-contact baseline for
        # the direct pair. Sample 0 is included in the sweep, so we'll
        # also re-test it (idempotent for non-direct pairs).
        try:
            T0 = trimesh.transformations.translation_matrix(
                (0.0, 0.0, 0.0)
            )
        except Exception:  # noqa: BLE001
            T0 = None
        if T0 is not None:
            for moving_mesh in meshes_by_label.get(mate.moving_part_id, []):
                moved0 = moving_mesh.copy()
                # Identity transform -- sample 0 is the initial pose.
                for static_mesh in meshes_by_label.get(
                    mate.fixed_part_id, []
                ):
                    pair_key = (id(moving_mesh), id(static_mesh))
                    try:
                        _hit0, _det0, vol0 = _qa._pair_collides(
                            moved0, static_mesh
                        )
                        if _hit0 is None:
                            # Probe failure at sample 0: cannot establish
                            # baseline. Treat all subsequent collisions as
                            # UNVERIFIABLE rather than baseline-exempt.
                            baseline_probe_failed.add(pair_key)
                        elif _hit0:
                            baseline_hit[pair_key] = True
                            baseline_vol[pair_key] = max(
                                baseline_vol.get(pair_key, 0.0),
                                float(vol0),
                            )
                        else:
                            baseline_hit.setdefault(pair_key, False)
                    except Exception:  # noqa: BLE001
                        baseline_probe_failed.add(pair_key)

        for s in samples:
            if rotational:
                T = trimesh.transformations.rotation_matrix(
                    math.radians(float(s)), axis_dir, axis_pt
                )
            else:
                shift = (
                    axis_dir[0] * float(s),
                    axis_dir[1] * float(s),
                    axis_dir[2] * float(s),
                )
                T = trimesh.transformations.translation_matrix(shift)
            for sl in subtree:
                for moving_mesh in meshes_by_label.get(sl, []):
                    moved = moving_mesh.copy()
                    moved.apply_transform(T)
                    for st in statics:
                        # BUG-003: the joint's own moving/fixed interface
                        # (sl==moving, st==fixed) is NO LONGER skipped.
                        # Designed contact at sample=0 is allowed via the
                        # per-mesh-pair baseline; new or deepened
                        # penetration fails.
                        is_direct_interface = (
                            sl == mate.moving_part_id
                            and st == mate.fixed_part_id
                        )
                        for static_mesh in meshes_by_label[st]:
                            hit, detail, vol = _qa._pair_collides(moved, static_mesh)
                            if hit is None:
                                # BUG-004: probe could not run -- treat as
                                # UNVERIFIABLE failure, not a silent PASS.
                                collisions.append(float(s))
                                first_detail = first_detail or (
                                    f"UNVERIFIABLE at {float(s):+.0f} {unit}: "
                                    f"{sl} vs {st} ({detail})"
                                )
                                break
                            if hit:
                                if is_direct_interface:
                                    pair_key = (
                                        id(moving_mesh), id(static_mesh)
                                    )
                                    if pair_key in baseline_probe_failed:
                                        # Baseline probe failed at sample=0
                                        # for THIS mesh pair -- UNVERIFIABLE.
                                        collisions.append(float(s))
                                        first_detail = first_detail or (
                                            f"UNVERIFIABLE at {float(s):+.0f} "
                                            f"{unit}: {sl} vs {st} "
                                            f"(baseline probe failed for "
                                            f"this mesh pair; {detail})"
                                        )
                                        break
                                    if not baseline_hit.get(pair_key, False):
                                        # No baseline contact for this
                                        # mesh pair at sample=0. ANY hit
                                        # during the sweep is a NEW
                                        # collision -- fail, regardless
                                        # of volume. The volume tolerance
                                        # only applies to deepening of
                                        # existing baseline contact.
                                        collisions.append(float(s))
                                        first_detail = first_detail or (
                                            f"new collision at "
                                            f"{float(s):+.0f} {unit}: "
                                            f"{sl} vs {st} "
                                            f"(vol {vol:.1f}; baseline "
                                            f"had no contact for this "
                                            f"mesh pair; {detail})"
                                        )
                                        break
                                    # baseline_hit is True for this pair:
                                    # allow penetration up to baseline_vol
                                    # + tolerance; deeper = deepened
                                    # collision (fail).
                                    base = baseline_vol.get(pair_key, 0.0)
                                    vol_tol = (
                                        base
                                        + _qa.cfg.INTERFERENCE_VOLUME_TOL_MM3
                                    )
                                    if float(vol) <= vol_tol:
                                        # Within deepening tolerance:
                                        # legitimate bearing/slider
                                        # contact, not a sweep collision.
                                        continue
                                    # Penetration deepened beyond
                                    # baseline + tolerance -- fail.
                                    collisions.append(float(s))
                                    first_detail = first_detail or (
                                        f"deepened collision at "
                                        f"{float(s):+.0f} {unit}: "
                                        f"{sl} vs {st} "
                                        f"(vol {vol:.1f} > baseline "
                                        f"{base:.1f} + tol; {detail})"
                                    )
                                    break
                                # Non-direct pair: any collision fails.
                                collisions.append(float(s))
                                first_detail = first_detail or (
                                    f"collision at {float(s):+.0f} {unit}: "
                                    f"{sl} vs {st} ({detail})"
                                )
                                break
                        if collisions and collisions[-1] == float(s):
                            break
                if collisions and collisions[-1] == float(s):
                    break
        chk.collision_deg = collisions
        chk.passed = not collisions
        chk.detail = first_detail or (
            f"swept {samples[0]:+.0f}..{samples[-1]:+.0f} {unit}, "
            f"no collisions vs statics {statics}"
        )
        checks.append(chk)
    return checks


# Bind the facade after definitions so importing either module first works.
# Helpers are not called during import; shared state remains owned by the facade.
from mac_assembly import assembly_qa as _qa
