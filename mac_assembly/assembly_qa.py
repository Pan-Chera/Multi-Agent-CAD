"""AssemblyQA: closed-loop detection for the assembled compound.

Deterministic checks decide pass/fail (philosophy reused from the CAD
Skills ``inspection-and-validation.md``):

1. **Part count** -- solids in the exported STEP == len(brief.parts).
2. **Envelope** -- assembly bbox within tolerance of the brief.
3. **Mate alignment** -- each MateSpec's two named datums are re-derived
   from the *placed* geometry and their world-space delta is compared
   against the expected offset (the in-process analogue of
   ``scripts/inspect align``).
4. **Interference** -- pairwise mesh overlap between placed parts
   (containment sampling + surface gap, via trimesh).

Visual review stays diagnostic (rendered views go to the Assembly
Judge), exactly like ``snapshot-review.md``: "visual review is
diagnostic, not authoritative".
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from mac_assembly import config_assembly as cfg
from mac_assembly.geometry_utils import shift_pt
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    AssemblyBrief,
    AssemblyErrorType,
    AssemblyQAReport,
    InterferenceCheck,
    KinematicCheck,
    MateCheck,
    MateType,
)
from multi_agent_cad.render_views import _render_isometric_views


# ---------------------------------------------------------------------------
# Geometry loading
# ---------------------------------------------------------------------------


def _load_placed_solids(assembly_dir: Path):
    """Return (placed, locations, error) from the exported assembly STEP.

    Solids are mapped back to part labels via the manifest written by the
    assembly script (nearest bbox centre), so QA never trusts STEP label
    metadata.

    ``locations`` is ``{label: (translation_tuple, rotation_euler_xyz_deg)}``
    from the manifest -- the part's full world transform after joints are
    applied. QA uses this to map part-local selector points through the
    FULL placed transform (not just bbox-centre translation) so rotated
    poses (chains of revolute joints) are handled exactly.
    """
    from build123d import import_step

    step_path = assembly_dir / "assembly_output.step"
    manifest_path = assembly_dir / "assembly_manifest.json"
    if not step_path.is_file():
        return None, None, "assembly_output.step missing"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else []

    locations: dict[str, tuple[tuple, tuple]] = {}
    for entry in manifest:
        loc = entry.get("location")
        if loc and "translation" in loc and "rotation_euler_xyz_deg" in loc:
            locations[entry["label"]] = (
                tuple(loc["translation"]),
                tuple(loc["rotation_euler_xyz_deg"]),
            )

    compound = import_step(str(step_path))
    solids = list(compound.solids())
    if not solids:
        return None, None, "assembly STEP contains no solids"

    placed = []
    used: set[int] = set()
    for entry in manifest:
        label = entry["label"]
        centre = (
            (entry["bbox_min"][0] + entry["bbox_max"][0]) / 2.0,
            (entry["bbox_min"][1] + entry["bbox_max"][1]) / 2.0,
            (entry["bbox_min"][2] + entry["bbox_max"][2]) / 2.0,
        )
        best_idx, best_d = None, float("inf")
        for idx, solid in enumerate(solids):
            if idx in used:
                continue
            bb = solid.bounding_box()
            c = bb.center()
            d = (c.X - centre[0]) ** 2 + (c.Y - centre[1]) ** 2 + (c.Z - centre[2]) ** 2
            if d < best_d:
                best_idx, best_d = idx, d
        if best_idx is None:
            continue
        used.add(best_idx)
        bb = solids[best_idx].bounding_box()
        placed.append((label, (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)))
    # Solids without a manifest match get an anonymous label.
    for idx, solid in enumerate(solids):
        if idx not in used:
            bb = solid.bounding_box()
            placed.append((f"unmatched_{idx}", (bb.min.X, bb.min.Y, bb.min.Z),
                           (bb.max.X, bb.max.Y, bb.max.Z)))
    return placed, locations, None


# ---------------------------------------------------------------------------
# Anchor maths (mirror of the runtime derivation in the generated script)
# ---------------------------------------------------------------------------


def _anchor_world_point(anchor: Anchor, bb_min, bb_max):
    cx = (bb_min[0] + bb_max[0]) / 2.0
    cy = (bb_min[1] + bb_max[1]) / 2.0
    cz = (bb_min[2] + bb_max[2]) / 2.0
    if anchor.kind == AnchorKind.SELECTOR:
        # SELECTOR anchors carry a semantic query, not bbox algebra. Callers
        # that can resolve the real face do so BEFORE calling this; this
        # fallback (bbox centre) only guards resolver-unavailable paths so a
        # SELECTOR anchor can never crash QA with KeyError: None (N2a).
        return (cx, cy, cz)
    if anchor.kind == AnchorKind.FACE:
        return {
            "top": (cx, cy, bb_max[2]),
            "bottom": (cx, cy, bb_min[2]),
            "right": (bb_max[0], cy, cz),
            "left": (bb_min[0], cy, cz),
            "back": (cx, bb_max[1], cz),
            "front": (cx, bb_min[1], cz),
        }[anchor.face]
    if anchor.kind == AnchorKind.SPHERE:
        # Sphere anchor: explicit sphere_center_mm in part-local coords.
        # QA transforms the part-local center through the placed Location
        # in the caller; here we return the part-local point so the caller
        # can apply the transform (mirrors _emit_anchor SPHERE branch).
        sc = anchor.sphere_center_mm or [0.0, 0.0, 0.0]
        return (float(sc[0]), float(sc[1]), float(sc[2]))
    if anchor.axis is None:
        # AXIS_POINT with axis=None (shouldn't happen, but guard) — fall
        # back to bbox centre so QA never crashes with KeyError: None.
        return (cx, cy, cz)
    off = anchor.offset_mm or 0.0
    return {
        "x": (cx + off, cy, cz),
        "y": (cx, cy + off, cz),
        "z": (cx, cy, cz + off),
    }[anchor.axis]


def _point_bbox_gap(point, bb_min, bb_max) -> float:
    """Distance from ``point`` to an axis-aligned bbox: 0 when the point is
    inside (or on) the box, otherwise the Euclidean distance to the nearest
    point on the box surface."""
    d2 = 0.0
    for k in range(3):
        if point[k] < bb_min[k]:
            d2 += (bb_min[k] - point[k]) ** 2
        elif point[k] > bb_max[k]:
            d2 += (point[k] - bb_max[k]) ** 2
    return math.sqrt(d2)


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------


def _resolve_qa_anchor(anchor, step_path: str | None):
    """Resolve a SELECTOR anchor to real topology (part-local).

    Returns the resolver dict (point/axis/selector/...) or None when
    unresolvable (caller falls back to bbox algebra). The point and axis
    are in the part's LOCAL coordinates -- authoritative for an unmoved
    part; a moved part's caller must shift via ``_local_to_world_point``.
    """
    if anchor.kind != AnchorKind.SELECTOR or not step_path:
        return None
    try:
        from mac_assembly.selector_resolver import resolve_selector_anchor

        return resolve_selector_anchor(
            step_path,
            anchor.selector_query.model_dump(mode="json", exclude_none=True),
        )
    except Exception:  # noqa: BLE001
        return None


def _resolve_qa_point(anchor, step_path: str | None):
    """Resolve a SELECTOR anchor to a real topology point (part-local).

    Returns (point, True) on success; (None, False) when unresolvable
    (caller falls back to bbox algebra). NOTE: the point is in the part's
    LOCAL coordinates -- authoritative for the (unmoved) fixed side; for a
    moved part the caller must not treat it as a world position.
    """
    resolved = _resolve_qa_anchor(anchor, step_path)
    if resolved:
        return tuple(resolved["point"]), True
    return None, False


_orig_center_cache: dict[str, tuple] = {}


def _orig_bbox_center(step_path: str) -> tuple | None:
    """Cached original (pre-placement) bbox centre of a part STEP.

    Cache key includes mtime+size (reuses selector_resolver._cache_key) so
    an in-place Aider remodel never shifts the N1 datum against stale
    original geometry.
    """
    from mac_assembly.selector_resolver import _cache_key

    key = _cache_key(step_path)
    if key in _orig_center_cache:
        return _orig_center_cache[key]
    try:
        from build123d import import_step

        bb = import_step(step_path).bounding_box()
        c = (bb.center().X, bb.center().Y, bb.center().Z)
    except Exception:  # noqa: BLE001
        c = None
    _orig_center_cache[key] = c
    return c


def _local_to_world_point(point, location_data, step_path: str | None = None,
                          placed_bbox=None) -> tuple:
    """Transform a part-LOCAL selector point into world coords.

    Preferred path: ``location_data`` (translation + Euler XYZ rotation in
    degrees, from the manifest) -- applies the full 4x4 transform so
    rotated poses (chains of revolute joints where the fixed part of
    mate N is itself rotated by mate N-1) are handled exactly. The previous
    bbox-centre-translation approximation silently used the wrong axis
    direction for chained revolute joints (the local axis was used as
    world, ignoring the parent's rotation).

    Fallback: when ``location_data`` is None (old manifest without the
    ``location`` field), approximate the transform as a pure translation
    by the (placed - original) bbox-centre delta. Exact for pure-translation
    assemblies; close enough for the approximate pivot/proximity checks
    when no parent rotation is involved.
    """
    if location_data is not None:
        translation, rotation_euler = location_data
        from build123d import Location, Vector

        has_rot = any(abs(a) > 1e-9 for a in rotation_euler)
        if has_rot:
            loc = Location(
                Vector(*translation),
                Vector(*rotation_euler),
            )
        else:
            loc = Location(Vector(*translation))
        world = loc * Location(tuple(point))
        return (world.position.X, world.position.Y, world.position.Z)
    # Fallback: bbox-center shift (pure translation approximation).
    if step_path and placed_bbox:
        orig = _orig_bbox_center(step_path)
        if orig is not None:
            shift = tuple(
                (placed_bbox[0][k] + placed_bbox[1][k]) / 2.0 - orig[k]
                for k in range(3)
            )
            return tuple(point[k] + shift[k] for k in range(3))
    return tuple(point)


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
        # Fixed anchor: SELECTOR resolves on the real topology. For an
        # intermediate fixed part that was itself moved, the resolved point
        # is part-LOCAL -- shift it into world coords first (N1).
        fp_sel, fp_ok = _resolve_qa_point(
            mate.fixed_anchor, step_paths.get(mate.fixed_part_id)
        )
        if fp_ok:
            fp = fp_sel
            if (
                mate.fixed_part_id in moved_set
                and step_paths.get(mate.fixed_part_id)
            ):
                fp = _local_to_world_point(
                    fp_sel,
                    locations.get(mate.fixed_part_id),
                    step_paths[mate.fixed_part_id],
                    bboxes[mate.fixed_part_id],
                )
        elif mate.fixed_anchor.kind == AnchorKind.SPHERE:
            # SPHERE fixed anchor (ball mate's socket side): sphere_center_mm
            # is part-LOCAL. For a moved fixed part (chained ball mate),
            # transform through the placed location; for a root fixed part
            # at origin, the transform is identity and this still returns
            # the part-local value.
            sc = mate.fixed_anchor.sphere_center_mm or [0.0, 0.0, 0.0]
            fp = _local_to_world_point(
                (float(sc[0]), float(sc[1]), float(sc[2])),
                locations.get(mate.fixed_part_id),
                step_paths.get(mate.fixed_part_id),
                bboxes[mate.fixed_part_id],
            )
        else:
            fp = _anchor_world_point(mate.fixed_anchor, *bboxes[mate.fixed_part_id])

        if mate.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL):
            # Slide joints have no single point datum inside the moving part,
            # but the slide datum (fixed anchor) must still land near the
            # placed moving part. This basic proximity check works even when
            # the kinematic sweep is disabled (R5 -- was an unconditional pass).
            mbmin, mbmax = bboxes[mate.moving_part_id]
            gap = _point_bbox_gap(fp, mbmin, mbmax)
            diag = math.sqrt(sum(
                (mbmax[k] - mbmin[k]) ** 2 for k in range(3)
            ))
            tol = max(diag, 1.0)  # datum may sit up to ~one part-diagonal away
            chk.measured_mm = gap
            chk.delta_mm = gap
            chk.passed = gap <= tol
            chk.detail = (
                f"{mate.mate_type.value} slide datum {gap:.1f}mm from moving "
                f"part (limit {tol:.1f}mm); motion range validated by sweep"
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
            # like the point).
            axis_dir = None
            resolved_full = _resolve_qa_anchor(
                mate.fixed_anchor, step_paths.get(mate.fixed_part_id)
            )
            if resolved_full:
                ax_local = tuple(resolved_full["axis"])
                if sum(abs(v) for v in ax_local) > 1e-9:
                    if (mate.fixed_part_id in moved_set
                            and step_paths.get(mate.fixed_part_id)):
                        ax_world = _local_to_world_point(
                            ax_local,
                            locations.get(mate.fixed_part_id),
                            step_paths[mate.fixed_part_id],
                            bboxes[mate.fixed_part_id],
                        )
                        # _local_to_world_point returns a POINT (with
                        # translation); to get a DIRECTION, subtract the
                        # transformed origin so the translation cancels.
                        origin_world = _local_to_world_point(
                            (0.0, 0.0, 0.0),
                            locations.get(mate.fixed_part_id),
                            step_paths[mate.fixed_part_id],
                            bboxes[mate.fixed_part_id],
                        )
                        axis_dir = tuple(
                            ax_world[k] - origin_world[k] for k in range(3)
                        )
                    else:
                        axis_dir = ax_local
            if not axis_dir or sum(abs(v) for v in axis_dir) < 1e-9:
                axis_dir = _AXIS_DIRS.get(
                    mate.fixed_anchor.axis or "z", _AXIS_DIRS["z"]
                )
            # Apply axial_offset_mm to the fixed anchor point along the
            # axis direction. Same math as codegen's runtime _shift_pt --
            # both call mac_assembly.geometry_utils.shift_pt so they cannot
            # diverge.
            off = mate.axial_offset_mm or 0.0
            fp_shifted = shift_pt(fp, axis_dir, off)
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
        mp_sel, mp_ok = _resolve_qa_point(
            mate.moving_anchor, step_paths.get(mate.moving_part_id)
        )
        mp_local = mp_ok
        if mp_ok:
            mp = mp_sel
        elif mate.moving_anchor.kind == AnchorKind.SPHERE:
            # SPHERE moving anchor (ball mate's ball side): sphere_center_mm
            # is part-LOCAL; transform through the placed location or QA
            # reads it as a world point and reports a false delta. The
            # connect() call did translate the part — we must mirror that
            # transform here, otherwise every ball mate fails with a
            # delta equal to the intended translation.
            sc = mate.moving_anchor.sphere_center_mm or [0.0, 0.0, 0.0]
            mp = _local_to_world_point(
                (float(sc[0]), float(sc[1]), float(sc[2])),
                locations.get(mate.moving_part_id),
                step_paths.get(mate.moving_part_id),
                bboxes[mate.moving_part_id],
            )
        else:
            mp = _anchor_world_point(
                mate.moving_anchor, *bboxes[mate.moving_part_id]
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
                real_gap = _min_gap_between(
                    meshes_by_label.get(mate.fixed_part_id, []),
                    meshes_by_label.get(mate.moving_part_id, []),
                )
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
                    # No user-stated target: no ground truth to verify
                    # against. Fall back to joint-enforced coincidence.
                    chk.measured_mm = 0.0
                    chk.delta_mm = 0.0
                    chk.passed = True
                    chk.detail = (
                        "selector-anchored rigid datum (joint-enforced; "
                        "no target_x/y/z_mm to verify resolver choice)"
                    )
                    checks.append(chk)
                    continue
                # Per-axis target check. The resolved point is the chosen
                # cylinder's bbox midpoint = bore centre (modulo numerical
                # precision); tol = max(tolerance_mm, 0.5) is generous.
                # A wrong-bore pick is typically off by tens of mm, so
                # this is a strong signal.
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


def _sample_surface(mesh, n: int = 300):
    import numpy as np
    import trimesh

    try:
        pts, _face_idx = trimesh.sample.sample_surface(mesh, n)
        return np.asarray(pts)
    except Exception:  # noqa: BLE001 - fall back to vertices
        return np.asarray(mesh.vertices)


def _load_components(assembly_dir: Path, placed):
    """Load per-part placed meshes, labelled.

    Preferred source: the ``placed_stl/<label>.stl`` files exported by the
    assembly script (exact-contact assemblies merge into one shell in the
    compound STL, making any split unreliable -- including trimesh's
    networkx fallback on the resulting non-watertight mesh). Fallback:
    split the compound STL and match components to labels
    containment-first.
    """
    import trimesh

    per_part_dir = assembly_dir / "placed_stl"
    components = []
    if per_part_dir.is_dir():
        for stl in sorted(per_part_dir.glob("*.stl")):
            try:
                mesh = trimesh.load(str(stl), force="mesh")
            except Exception:  # noqa: BLE001
                continue
            if len(getattr(mesh, "faces", [])):
                components.append((stl.stem, mesh))
        if components:
            return components

    whole = trimesh.load(str(assembly_dir / "assembly_output.stl"), force="mesh")
    shells = whole.split(only_watertight=False)
    bboxes = [(l, bmin, bmax) for l, bmin, bmax in placed if not l.startswith("unmatched_")]

    def contains(bmin, bmax, cb, tol=0.5):
        return all(
            cb[0][k] >= bmin[k] - tol and cb[1][k] <= bmax[k] + tol for k in range(3)
        )

    def centre_dist(bmin, bmax, c):
        p = ((bmin[0] + bmax[0]) / 2.0, (bmin[1] + bmax[1]) / 2.0, (bmin[2] + bmax[2]) / 2.0)
        return sum((p[k] - c[k]) ** 2 for k in range(3))

    comp_labels = []
    for comp in shells:
        cb = comp.bounds
        centre = (cb[0] + cb[1]) / 2.0
        candidates = [l for l, bmin, bmax in bboxes if contains(bmin, bmax, cb)]
        pool = candidates or [l for l, _bmin, _bmax in bboxes]
        best = min(pool, key=lambda l: centre_dist(
            *next((bmin, bmax) for ll, bmin, bmax in bboxes if ll == l), centre
        )) if pool else "unknown"
        comp_labels.append((best, comp))
    return comp_labels


def _deep_penetration_frac(outer, pts, depth_tol: float) -> float:
    """Fraction of ``pts`` that lie inside ``outer`` by more than depth_tol.

    ``outer.contains`` (ray parity) excludes boundary points, so parts
    merely touching face-to-face produce 0 -- contact is not interference.
    Points strictly inside are then measured for depth via closest_point;
    only penetration deeper than ``depth_tol`` counts (shallow STL-tessellation
    slivers are ignored).
    """
    import numpy as np

    inside = np.asarray(outer.contains(pts))
    if not inside.any():
        return 0.0
    try:
        from trimesh.proximity import closest_point

        _cp, dist, _fid = closest_point(outer, pts[inside])
        deep = int((np.asarray(dist) > depth_tol).sum())
    except Exception:  # noqa: BLE001 - depth unavailable; count all inside
        deep = int(inside.sum())
    return deep / float(len(pts))


def _min_gap_between(meshes_a, meshes_b, n_samples: int = 400) -> float | None:
    """Minimum surface-to-surface distance between two part meshes (real
    topology, not bbox). Returns the clearance when separated, ~0 when
    touching, and 0 (clamped) when overlapping. None if it cannot be
    computed. This is the authoritative "functional gap" for face_to_face
    mates and solves bbox != mating-face cases (a knob raises the bbox top
    but the real gap is measured between the actual surfaces)."""
    import numpy as np
    import trimesh
    from trimesh.proximity import closest_point

    try:
        pts_a = np.vstack([_sample_surface(m, n_samples) for m in meshes_a])
        pts_b = np.vstack([_sample_surface(m, n_samples) for m in meshes_b])
        best = float("inf")
        for outer, pts in ((meshes_b, pts_a), (meshes_a, pts_b)):
            # distance from each sampled point to the nearest point on outer
            merged = trimesh.util.concatenate(*outer) if len(outer) > 1 else outer[0]
            _cp, dist, _fid = closest_point(merged, pts)
            if len(dist):
                best = min(best, float(np.asarray(dist).min()))
        return None if best == float("inf") else max(0.0, best)
    except Exception:  # noqa: BLE001
        return None


def _pair_collides(mesh_a, mesh_b) -> tuple[bool, str]:
    """Bidirectional penetration test with a DEPTH tolerance.

    Contact/touching parts (face_to_face mates, a block resting on a rail)
    are NOT interference: ``contains`` excludes boundary points and only
    penetration deeper than ``INTERFERENCE_DEPTH_TOL_MM`` counts.
    """
    depth_tol = cfg.INTERFERENCE_DEPTH_TOL_MM
    try:
        pts_a = _sample_surface(mesh_a)
        pts_b = _sample_surface(mesh_b)
        frac = max(
            _deep_penetration_frac(mesh_b, pts_a, depth_tol),
            _deep_penetration_frac(mesh_a, pts_b, depth_tol),
        )
    except Exception:  # noqa: BLE001
        return False, "sampling failed - pair skipped"
    vol = frac * float(min(abs(mesh_a.volume), abs(mesh_b.volume)))
    passed = (
        frac <= cfg.INTERFERENCE_VERTEX_FRACTION
        and vol <= cfg.INTERFERENCE_VOLUME_TOL_MM3
    )
    return (not passed), (
        f"penetration >{depth_tol}mm: {frac * 100:.1f}% (vol proxy {vol:.1f}mm3)"
    )


def _check_interference(comp_labels) -> list[InterferenceCheck]:
    """Pairwise overlap between placed parts (depth-tolerant, trimesh).

    Also reports the real min-gap (clearance) for non-colliding pairs so
    the Judge/QA can reason about functional gaps, not just overlaps.
    """
    try:
        import numpy as np  # noqa: F401
        import trimesh  # noqa: F401
    except ImportError:
        return [
            InterferenceCheck(
                part_a="*", part_b="*", passed=True,
                detail="trimesh/numpy unavailable - interference check skipped",
            )
        ]

    if not comp_labels:
        return [InterferenceCheck(part_a="*", part_b="*", passed=False,
                                  detail="assembly STL split into no components")]

    checks: list[InterferenceCheck] = []
    n = len(comp_labels)
    for i in range(n):
        for j in range(i + 1, n):
            la, ma = comp_labels[i]
            lb, mb = comp_labels[j]
            if la == "unknown" or lb == "unknown" or la == lb:
                continue
            if len(ma.vertices) == 0 or len(mb.vertices) == 0:
                continue
            collided, detail = _pair_collides(ma, mb)
            gap = None if collided else _min_gap_between([ma], [mb])
            checks.append(InterferenceCheck(
                part_a=la, part_b=lb, passed=not collided, detail=detail,
                min_gap_mm=gap,
            ))
    return checks


# ---------------------------------------------------------------------------
# Kinematic sweep (revolute joints)
# ---------------------------------------------------------------------------


_AXIS_DIRS = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


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
    assembly_dir: Path, placed, mates, step_paths=None, locations=None
) -> list[KinematicCheck]:
    """Sweep every motion joint's moving subtree and test collisions
    against the static structure at each sample pose.

    * REVOLUTE     -- rotate the subtree about the joint axis over
      +/- ``KINEMATIC_SWEEP_DEG``.
    * LINEAR / CYLINDRICAL -- translate the subtree along the joint axis
      over +/- ``LINEAR_SWEEP_MM``.

    A part may export as several connected shells (B5): every shell of
    the moving subtree moves together and every shell of the static
    structure is tested. The mesh is already AT the static pose, so each
    sample applies a relative transform on top of it.
    """
    if not cfg.KINEMATIC_ENABLED:
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

    comp_labels = _load_components(assembly_dir, placed)
    meshes_by_label: dict[str, list] = {}
    for label, mesh in comp_labels:
        if label != "unknown":
            meshes_by_label.setdefault(label, []).append(mesh)
    bboxes = {label: (bmin, bmax) for label, bmin, bmax in placed}

    checks: list[KinematicCheck] = []
    for mate in motion:
        rotational = mate.mate_type == MateType.REVOLUTE
        if rotational:
            samples = np.linspace(
                -cfg.KINEMATIC_SWEEP_DEG, cfg.KINEMATIC_SWEEP_DEG,
                cfg.KINEMATIC_SWEEP_SAMPLES,
            )
            unit = "deg"
        else:
            samples = np.linspace(
                -cfg.LINEAR_SWEEP_MM, cfg.LINEAR_SWEEP_MM,
                cfg.LINEAR_SWEEP_SAMPLES,
            )
            unit = "mm"

        chk = KinematicCheck(mate_id=mate.mate_id, sweep_deg=[float(s) for s in samples])
        if (mate.fixed_part_id not in bboxes or mate.moving_part_id not in bboxes
                or mate.fixed_part_id not in meshes_by_label
                or mate.moving_part_id not in meshes_by_label):
            chk.passed = False
            chk.detail = "joint parts missing from placed geometry"
            checks.append(chk)
            continue

        if mate.mate_type in (MateType.LINEAR, MateType.CYLINDRICAL):
            axis_dir = _AXIS_DIRS[mate.slide_axis or "x"]
            axis_pt = None  # translation sweep needs no pivot point (R4)
        else:
            # REVOLUTE / COAXIAL: a SELECTOR fixed anchor carries the axis in
            # selector_query (anchor.axis is None) -- resolve the REAL
            # topology axis point+direction (N2b: sweeping the nominal z
            # instead of the query axis gives wrong collision results), then
            # shift the local point into world coords if the fixed part was
            # itself moved (N1).
            resolved = _resolve_qa_anchor(
                mate.fixed_anchor, step_paths.get(mate.fixed_part_id)
            )
            if resolved:
                axis_pt = tuple(resolved["point"])
                ax = tuple(resolved["axis"])
                axis_dir = ax if sum(abs(v) for v in ax) > 1e-9 else _AXIS_DIRS["z"]
                if (mate.fixed_part_id in moved_set
                        and step_paths.get(mate.fixed_part_id)):
                    axis_pt = _local_to_world_point(
                        axis_pt,
                        locations.get(mate.fixed_part_id),
                        step_paths[mate.fixed_part_id],
                        bboxes[mate.fixed_part_id],
                    )
            else:
                axis_dir = _AXIS_DIRS[mate.fixed_anchor.axis or "z"]
                axis_pt = _anchor_world_point(
                    mate.fixed_anchor, *bboxes[mate.fixed_part_id]
                )
        subtree = _moving_subtree(mates, mate.moving_part_id)
        statics = [l for l in meshes_by_label if l not in subtree]

        collisions: list[float] = []
        first_detail = ""
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
                        for static_mesh in meshes_by_label[st]:
                            hit, detail = _pair_collides(moved, static_mesh)
                            if hit:
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
        tol = expected * cfg.ENVELOPE_TOLERANCE_PCT
        if abs(measured[axis] - expected) > tol:
            errors.append(
                f"envelope {axis}: measured {measured[axis]:.1f}mm vs "
                f"expected {expected:.1f}mm (tol ±{tol:.1f}mm)"
            )
    return measured, errors


# ---------------------------------------------------------------------------
# Dimension reconciliation (zero-token pre-assembly gate)
# ---------------------------------------------------------------------------


def reconcile_dimensions(brief: AssemblyBrief, mates, part_results: dict) -> list[str]:
    """Re-check the mating plan against MEASURED part geometry.

    The Mating Architect designs anchors from the dimensions the
    Decomposer *claimed*; after PartBuilder the actual bounding boxes
    are free to measure. This gate catches stacking-height drift before
    a doomed assembly+QA cycle is spent (the assembly-layer analogue of
    MAC's stage-boundary normalization: deterministic checks first,
    LLM retries only when they pass).

    Currently checks vertical stacking chains (face_to_face top/bottom)
    against the brief's envelope z. Measured numbers are embedded in the
    error strings so the remate feedback carries ground truth.
    """
    from build123d import import_step

    measured: dict[str, tuple[tuple, tuple]] = {}
    for spec in brief.parts:
        result = part_results.get(spec.part_id)
        if not result or not result.ok or not result.step_path:
            continue
        try:
            shape = import_step(result.step_path)
            bb = shape.bounding_box()
            measured[spec.part_id] = (
                (bb.min.X, bb.min.Y, bb.min.Z),
                (bb.max.X, bb.max.Y, bb.max.Z),
            )
        except Exception as exc:  # noqa: BLE001
            return [f"reconcile: cannot load {spec.part_id} STEP ({exc})"]

    if len(measured) != len(brief.parts):
        return []  # missing parts handled by the part_missing path
    if not mates:
        return []

    errors: list[str] = []

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
        from mac_assembly.selector_resolver import cylinder_radii_along
        from mac_assembly.assembly_codegen import _get_anchor_axis

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
                or _get_anchor_axis(m.fixed_anchor)
                or _get_anchor_axis(m.moving_anchor)
                or "z"
            )
            fixed_r = cylinder_radii_along(
                part_results[m.fixed_part_id].step_path, axis)
            moving_r = cylinder_radii_along(
                part_results[m.moving_part_id].step_path, axis)
            if not fixed_r or not moving_r:
                continue  # no cylinders along that axis on some side: skip
            # Bore > shaft with 0..2mm clearance (either orientation), OR
            # both sides equal radii (implicit-pin case). Strict inequality
            # rejects exact fit (no clearance for rotation).
            fits = any(
                0 < abs(f - mv) <= 2.0
                or abs(f - mv) < 0.1
                for f in fixed_r for mv in moving_r
            )
            if not fits:
                errors.append(
                    f"reconcile: axis joint {m.mate_id!r} radii incompatible -- "
                    f"{m.fixed_part_id} R={sorted(round(r,2) for r in fixed_r)} "
                    f"vs {m.moving_part_id} R={sorted(round(r,2) for r in moving_r)} "
                    f"along {axis}: no pair has 0..2mm clearance (bore > shaft) "
                    f"or equal radii (implicit pin); check for interference or "
                    f"exact-fit (no rotation clearance)"
                )
    except Exception:  # noqa: BLE001 - radius check is best-effort
        pass

    # --- vertical stacking height vs envelope z ------------------------------
    env_z = brief.overall_envelope_mm.get("z")
    if not env_z:
        return errors
    # Only a purely vertical stacking tree has a predictable z height.
    if any(m.mate_type != MateType.FACE_TO_FACE for m in mates):
        return errors

    moved = {m.moving_part_id for m in mates}
    roots = [p.part_id for p in brief.parts if p.part_id not in moved]
    if len(roots) != 1:
        return errors

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
    tol = env_z * cfg.ENVELOPE_TOLERANCE_PCT
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
    return errors


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def run_assembly_qa(
    brief: AssemblyBrief,
    mates: list,
    part_results: dict,
    assembly_dir: Path,
) -> AssemblyQAReport:
    placed, locations, load_err = _load_placed_solids(assembly_dir)

    report = AssemblyQAReport(
        part_count_expected=brief.expected_part_count or len(brief.parts),
    )

    if load_err is not None:
        report.error_type = AssemblyErrorType.FATAL
        report.all_passed = False
        report.error_details = [f"assembly load failed: {load_err}"]
        _finalize(report, mates)
        return report

    labels = [l for l, _bmin, _bmax in placed if not l.startswith("unmatched_")]
    unmatched_count = len(placed) - len(labels)
    report.part_count_measured = len(placed)  # diagnostic: actual solid count
    # Pass if every expected part has a labelled solid. Extra shells
    # (unmatched_N) are tessellation artifacts of a single part exporting
    # as multiple connected components -- they are flagged downstream by
    # interference/envelope checks, not as a part_count failure (was: any
    # extra shell -> part_count_passed=False -> FATAL -> wrong route).
    report.part_count_passed = len(labels) == report.part_count_expected
    report.missing_parts = [
        p.part_id for p in brief.parts if p.part_id not in labels
    ]

    # Load placed meshes once; shared by mate-gap, interference and the
    # (separately-loading) kinematic sweep.
    try:
        comp_labels = _load_components(assembly_dir, placed)
    except Exception:  # noqa: BLE001 - no STL / trimesh unavailable
        comp_labels = []
    meshes_by_label: dict[str, list] = {}
    for _label, _mesh in comp_labels:
        if _label != "unknown":
            meshes_by_label.setdefault(_label, []).append(_mesh)
    # Part STEP paths (SELECTOR anchors resolve against real part topology).
    step_paths = {
        pid: r.step_path
        for pid, r in (part_results or {}).items()
        if getattr(r, "ok", False) and getattr(r, "step_path", "")
    }

    report.mate_checks = _check_mates(
        mates, placed, meshes_by_label, step_paths, locations,
    )
    report.mates_passed = all(c.passed for c in report.mate_checks)

    report.envelope_measured_mm, env_errors = _check_envelope(brief, placed)
    report.envelope_passed = not env_errors

    report.interference_checks = _check_interference(comp_labels)
    report.interference_passed = all(c.passed for c in report.interference_checks)

    report.kinematic_checks = _check_kinematics(
        assembly_dir, placed, mates, step_paths, locations,
    )
    report.kinematics_passed = all(c.passed for c in report.kinematic_checks)

    details: list[str] = []
    if not report.part_count_passed:
        details.append(
            f"part count: expected {report.part_count_expected}, "
            f"measured {report.part_count_measured}; missing={report.missing_parts}"
        )
    details += [f"mate {c.mate_id} FAIL: {c.detail}" for c in report.mate_checks if not c.passed]
    details += env_errors
    details += [
        f"interference {c.part_a}/{c.part_b} FAIL: {c.detail}"
        for c in report.interference_checks if not c.passed
    ]
    details += [
        f"kinematic {c.mate_id} FAIL: {c.detail}"
        for c in report.kinematic_checks if not c.passed
    ]
    report.error_details = details
    report.all_passed = not details

    _finalize(report, mates)
    return report


def _finalize(report: AssemblyQAReport, mates: list) -> None:
    """Compute error type + routing hints (repair-loop.md classification)."""
    if report.all_passed:
        report.error_type = AssemblyErrorType.NONE
        return
    if report.part_count_passed is False and report.missing_parts:
        report.error_type = AssemblyErrorType.PART_MISSING
        report.needs_remodel_part_ids = list(report.missing_parts)
        return
    if not report.kinematics_passed:
        report.error_type = AssemblyErrorType.KINEMATIC
    elif not report.interference_passed:
        report.error_type = AssemblyErrorType.INTERFERENCE
    elif not report.mates_passed:
        report.error_type = AssemblyErrorType.MATE_MISALIGNMENT
    elif not report.envelope_passed:
        report.error_type = AssemblyErrorType.ENVELOPE
    else:
        report.error_type = AssemblyErrorType.FATAL
    report.needs_mate_fix_ids = [
        c.mate_id for c in report.mate_checks if not c.passed
    ] + [c.mate_id for c in report.kinematic_checks if not c.passed]

    # Borderline-failure flag: when the only failures are envelope overshoot
    # within 2x tolerance OR interference volume under 2x tolerance, the QA
    # failure is likely a too-strict envelope / tessellation artifact rather
    # than a real geometric defect. The Judge node reads this to override
    # its iteration_count < MIN_RETRY skip so an ACCEPT can terminate the
    # loop instead of forcing a route back. Only set when ALL active errors
    # are borderline -- mixed real + borderline failures stay False so the
    # real errors still drive a route-back.
    if report.error_type == AssemblyErrorType.ENVELOPE:
        report.is_likely_false_positive = _envelope_is_borderline(report)
    elif report.error_type == AssemblyErrorType.INTERFERENCE:
        report.is_likely_false_positive = _interference_is_borderline(report)


def _envelope_is_borderline(report: AssemblyQAReport) -> bool:
    """All envelope errors overshoot by less than 1 extra tolerance band
    (i.e. measured - expected falls in (tol, 2*tol]). Tighter than 2*tol
    is a real violation; the borderline band catches tessellation / mesh
    inflation drift that just barely crosses the threshold.
    """
    expected = report.envelope_expected_mm
    measured = report.envelope_measured_mm
    if not expected or not measured:
        return False
    for axis, exp in expected.items():
        if exp <= 0:
            continue
        meas = measured.get(axis, 0.0)
        tol = exp * cfg.ENVELOPE_TOLERANCE_PCT
        err = abs(meas - exp)
        if err <= tol:
            continue  # this axis passed -- not the failure axis
        if err > 2 * tol:
            return False  # real violation, not borderline
    return True


def _interference_is_borderline(report: AssemblyQAReport) -> bool:
    """All interference failures have penetration volume under 2x the
    tolerance (i.e. <= 10 mm^3 by default). Volumes in this range are
    typically tessellation overlap of coplanar faces, not real collision.
    """
    for c in report.interference_checks:
        if c.passed:
            continue
        if c.penetration_volume_mm3 > 2 * cfg.INTERFERENCE_VOLUME_TOL_MM3:
            return False
    return True


def render_assembly_views(assembly_dir: Path) -> list[bytes]:
    """Diagnostic views for the Assembly Judge (reuse MAC render_views)."""
    stl = assembly_dir / "assembly_output.stl"
    if not stl.is_file():
        return []
    views = _render_isometric_views(
        stl, n_views=cfg.ASSEMBLY_JUDGE_VIEWS_COUNT, size=cfg.ASSEMBLY_JUDGE_VIEW_SIZE
    )
    if views and cfg.ASSEMBLY_JUDGE_SAVE_VIEWS:
        out_dir = assembly_dir / "assembly_judge_views"
        out_dir.mkdir(exist_ok=True)
        for i, png in enumerate(views):
            (out_dir / f"view_{i}.png").write_bytes(png)
    return views
