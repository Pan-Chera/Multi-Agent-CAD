"""Anchor resolution, original-geometry caching and world transforms.

Internal helpers; callers continue to use ``mac_assembly.assembly_qa``.
Resolve shared configuration, caches and replaceable helpers through that
module at call time so existing overrides and monkeypatch hooks stay live.
"""
from __future__ import annotations

import math
from mac_assembly.schemas_assembly import Anchor, AnchorKind


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
        # Face centre = bbox centre + outward normal * half-extent
        # (normals from geometry_utils, single source of truth).
        n = _qa._FACE_NORMALS[anchor.face]
        return (
            cx + n[0] * (bb_max[0] - bb_min[0]) / 2.0,
            cy + n[1] * (bb_max[1] - bb_min[1]) / 2.0,
            cz + n[2] * (bb_max[2] - bb_min[2]) / 2.0,
        )
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
    if anchor.point_mm is not None:
        return tuple(float(v) for v in anchor.point_mm)
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

    BUG-002 (P0 revision): callers that need to distinguish "selector
    miss" (definitive -- geometry absent) from "probe error"
    (infrastructure fault) from "non-SELECTOR anchor" (use bbox algebra)
    must call :func:`_resolve_qa_point_3state` instead. The legacy
    two-tuple collapses those three states into (None, False), which
    caused silent bbox-fallback when a SELECTOR cylinder query matched
    no face on the part (e.g. a Box part with a cylinder SELECTOR --
    the fixed datum became the Box bbox centre, then the moving-side
    target verification passed, silently false-PASSing the mate).
    """
    resolved = _qa._resolve_qa_anchor(anchor, step_path)
    if resolved:
        return tuple(resolved["point"]), True
    return None, False


def _resolve_qa_point_3state(anchor, step_path: str | None):
    """Three-state resolution of a SELECTOR anchor.

    Returns ``(status, point)`` where ``status`` is one of:

      * ``"found"``      -- ``point`` is the resolved part-local point.
      * ``"not_found"``  -- SELECTOR probe found NO matching face
        (geometry absent). Callers MUST fail with the selector-miss
        signature; do NOT fall back to bbox algebra.
      * ``"error"``      -- STEP unreadable / cadpy unavailable /
        SelectorIndex build failed, OR anchor is SELECTOR but
        ``step_path`` is missing/empty, OR selector_query is None.
        UNVERIFIABLE.
      * ``"no_selector"``-- anchor is not SELECTOR; caller uses
        FACE/AXIS_POINT/SPHERE bbox algebra (existing behavior).

    Replaces the legacy two-tuple ``_resolve_qa_point`` for call sites
    that must avoid the silent bbox-fallback (BUG-002 P0 revision).

    P1-1 revision: SELECTOR + missing step_path / missing selector_query
    is now ``"error"`` (UNVERIFIABLE), not ``"no_selector"``. Treating a
    SELECTOR with no STEP as "no_selector" let _check_kinematics fall
    through to bbox algebra and silently sweep about a guessed axis.
    """
    status, result = _qa._resolve_qa_anchor_3state(anchor, step_path)
    if status == "found" and result:
        return "found", tuple(result["point"])
    return status, None


def _resolve_qa_anchor_3state(anchor, step_path: str | None):
    """Three-state resolution returning the FULL resolver result dict.

    Shared by ``_check_mates`` (via _resolve_qa_point_3state) and
    ``_check_kinematics`` (directly), so both paths fail closed
    identically on SELECTOR miss / probe error (P1-1 requirement 4:
    avoid one path fail-closed + another path fallback).

    Returns ``(status, result)`` where ``status`` is one of:

      * ``"found"``      -- ``result`` is the resolver dict
        (point/axis/selector/...).
      * ``"not_found"``  -- SELECTOR probe found NO matching face
        (geometry absent). Callers MUST fail with the selector-miss
        signature.
      * ``"error"``      -- STEP unreadable / cadpy unavailable /
        SelectorIndex build failed, OR SELECTOR + missing step_path,
        OR SELECTOR + missing selector_query. UNVERIFIABLE.
      * ``"no_selector"``-- anchor is not SELECTOR; caller uses
        FACE/AXIS_POINT/SPHERE bbox algebra.

    Result dict (status ``"found"``)::

        {"point": (x, y, z),
         "axis": (dx, dy, dz),
         "selector": "f5",
         "surface": "plane"|"cylinder",
         "radius": float | None,
         "coordinate": float | None,
         "candidate_count": int,
         "role": str | None (cylinder only)}
    """
    if anchor.kind != AnchorKind.SELECTOR:
        return "no_selector", None
    sq = anchor.selector_query
    if sq is None:
        return "error", None
    if not step_path:
        return "error", None
    try:
        from mac_assembly.selector_resolver import probe_selector_anchor

        status, result = probe_selector_anchor(
            step_path,
            sq.model_dump(mode="json", exclude_none=True),
        )
    except Exception:  # noqa: BLE001
        return "error", None
    if status == "found" and result:
        return "found", result
    return status, None


def _probe_selector_anchor(step_path: str, query: dict) -> tuple[str, dict | None]:
    """Three-state probe wrapper for the rigid-no-target uniqueness
    check (BUG-043). Delegates to selector_resolver.probe_selector_anchor
    so QA can read the candidate_count without modifying the public
    resolver API.
    """
    try:
        from mac_assembly.selector_resolver import probe_selector_anchor

        return probe_selector_anchor(step_path, query)
    except Exception:  # noqa: BLE001
        return "error", None


def _validate_selector_anchor(
    anchor,
    step_path: str | None,
    part_id: str,
    anchor_label: str,
    tolerance_mm: float,
) -> tuple[bool, str, dict | None]:
    """Fail-closed validation shared by every mate type.

    A selector must resolve, identify exactly one semantic candidate, and
    satisfy every explicit target coordinate in the part-local frame.  This
    runs before mate-specific early returns so motion mates cannot bypass
    moving-side validation.
    """
    if anchor.kind != AnchorKind.SELECTOR:
        return True, "", None

    sq = anchor.selector_query
    sq_dict = (
        sq.model_dump(mode="json", exclude_none=True)
        if sq is not None else {}
    )
    status, result = _qa._resolve_qa_anchor_3state(anchor, step_path)
    if status == "not_found":
        return False, (
            f"SELECTOR anchor matched no face on {part_id}: {sq_dict!r}"
        ), None
    if status == "error":
        return False, (
            f"UNVERIFIABLE: {anchor_label} SELECTOR probe errored "
            f"(infrastructure fault) on {part_id}"
        ), None
    if status != "found" or result is None:
        return False, (
            f"UNVERIFIABLE: {anchor_label} SELECTOR returned "
            f"unexpected status {status!r} on {part_id}"
        ), None

    targets = []
    if sq is not None:
        for name, idx in (("x", 0), ("y", 1), ("z", 2)):
            value = getattr(sq, f"target_{name}_mm", None)
            if value is not None:
                targets.append((name, idx, float(value)))

    candidate_count = int(result.get("candidate_count") or 0)
    if candidate_count == 0:
        if targets:
            return False, (
                f"UNVERIFIABLE: {anchor_label} SELECTOR target matches no "
                f"candidate on {part_id}: {sq_dict!r}"
            ), result
        return False, (
            f"UNVERIFIABLE: {anchor_label} SELECTOR resolved without a "
            f"semantic candidate on {part_id}: {sq_dict!r}"
        ), result
    if candidate_count != 1:
        return False, (
            f"UNVERIFIABLE: {anchor_label} SELECTOR has "
            f"{candidate_count} candidate faces on {part_id}; target does "
            f"not uniquely disambiguate: {sq_dict!r}"
        ), result

    if targets:
        point = tuple(float(v) for v in result["point"])
        tol = max(float(tolerance_mm), 0.5)
        deltas = [
            (name, abs(point[idx] - target), target)
            for name, idx, target in targets
        ]
        worst_name, worst_delta, worst_target = max(
            deltas, key=lambda item: item[1]
        )
        if worst_delta > tol:
            return False, (
                f"UNVERIFIABLE: {anchor_label} SELECTOR target mismatch on "
                f"{part_id}: resolved {point}, target_{worst_name}_mm="
                f"{worst_target:.3f}, delta={worst_delta:.3f}mm "
                f"(tol {tol:.3f}mm)"
            ), result

    return True, "", result


def _orig_bbox_center(step_path: str) -> tuple | None:
    """Cached original (pre-placement) bbox centre of a part STEP.

    Cache key includes mtime+size (reuses selector_resolver._cache_key) so
    an in-place Aider remodel never shifts the N1 datum against stale
    original geometry.
    """
    bb = _qa._orig_bbox(step_path)
    if bb is None:
        return None
    (bmin, bmax) = bb
    return tuple((bmin[k] + bmax[k]) / 2.0 for k in range(3))


def _orig_bbox(step_path: str) -> tuple[tuple, tuple] | None:
    """Cached ORIGINAL (pre-placement) bbox of a part STEP as
    ((xmin, ymin, zmin), (xmax, ymax, zmax)).

    Needed to re-derive a FACE / AXIS_POINT anchor's part-LOCAL datum
    (face centre / axis-offset point) on the un-rotated geometry, which is
    then transformed into world coords via the placed Location -- using the
    PLACED world bbox instead would silently read the wrong face off any
    part that a parent mate has rotated (P1-6).
    """
    from mac_assembly.selector_resolver import _cache_key

    key = _cache_key(step_path)
    if key in _qa._orig_bbox_cache:
        _qa._orig_bbox_cache.move_to_end(key)
        return _qa._orig_bbox_cache[key]
    try:
        from build123d import import_step

        bb = import_step(step_path).bounding_box()
        v = (
            (bb.min.X, bb.min.Y, bb.min.Z),
            (bb.max.X, bb.max.Y, bb.max.Z),
        )
    except Exception:  # noqa: BLE001
        v = None
    _qa._orig_bbox_cache[key] = v
    _qa._orig_bbox_cache.move_to_end(key)
    while len(_qa._orig_bbox_cache) > _qa._ORIG_CENTER_CACHE_MAX:
        _qa._orig_bbox_cache.popitem(last=False)
    return v


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
    when no parent rotation is involved. This fallback only serves parts
    NOT subject to the ``_moved_part_location`` gate (roots/unmoved
    parts, whose legacy poses never carried rotation): a MOVED part
    without manifest location is failed UNVERIFIABLE by that gate before
    it can reach this code.
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
        orig = _qa._orig_bbox_center(step_path)
        if orig is not None:
            shift = tuple(
                (placed_bbox[0][k] + placed_bbox[1][k]) / 2.0 - orig[k]
                for k in range(3)
            )
            return tuple(point[k] + shift[k] for k in range(3))
    return tuple(point)


def _local_to_world_direction(local_direction, location_data) -> tuple | None:
    """Transform a part-LOCAL direction into WORLD coords (rotation only,
    no translation).

    Reuses the "transform the endpoint, transform the origin, subtract"
    logic of ``_check_mates``' selector-axis path: the shared Location
    multiplication cancels the translation, leaving the rotated direction.

    When ``location_data`` is None (legacy manifest without the
    ``location`` field) there is NO rotation information; the unchanged
    local direction is returned -- exact for pure-translation assemblies,
    and the same approximation ``_local_to_world_point`` makes for
    points. Only reachable for ungated (root/unmoved) parts: callers
    transforming a MOVED part's direction must first pass
    ``_moved_part_location``, which fails UNVERIFIABLE when the manifest
    location is missing, so a possibly-rotated legacy pose never
    silently sweeps about an unrotated axis.

    Returns None when the transform produces a degenerate (near-zero)
    vector -- callers must fail their check explicitly instead of silently
    sweeping about a garbage axis.
    """
    if location_data is None:
        return tuple(local_direction)
    translation, rotation_euler = location_data
    from build123d import Location, Vector

    if any(abs(a) > 1e-9 for a in rotation_euler):
        loc = Location(Vector(*translation), Vector(*rotation_euler))
    else:
        loc = Location(Vector(*translation))
    origin = loc * Location((0.0, 0.0, 0.0))
    end = loc * Location(tuple(local_direction))
    d = (
        end.position.X - origin.position.X,
        end.position.Y - origin.position.Y,
        end.position.Z - origin.position.Z,
    )
    mag = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
    if mag < 1e-9:
        return None
    return (d[0] / mag, d[1] / mag, d[2] / mag)


def _moved_part_location(part_id, locations):
    """Placement data for a MOVED part: manifest location or bust.

    A moved part (placed by a parent mate) MUST carry a manifest
    ``location``. Legacy manifests without the ``location`` field get NO
    approximation: AABB comparison was deliberately rejected as
    "no-rotation" evidence because it proves nothing -- a 180 deg turn
    about ANY principal axis leaves the ordered AABB dimensions of EVERY
    part unchanged (and a dimension-symmetric part additionally hides 90
    deg turns), while the local face/axis datums and SELECTOR axis
    directions silently point elsewhere after such a turn. Restoring a
    pose must use real transform data (the current codegen manifest
    records every part's Location), never AABB inference.

    Returns ``(location_data | None, error | None)``:
    - ``(loc, None)`` -- manifest location present; transform exactly.
    - ``(None, err)`` -- moved part without a location; callers must
      fail their check UNVERIFIABLE instead of guessing.
    """
    loc = locations.get(part_id)
    if loc is not None:
        return loc, None
    return None, (
        f"{part_id} was moved (placed by a parent mate) but the assembly "
        f"manifest carries no location for it: the world pose cannot be "
        f"re-derived. AABB dimensions are NOT accepted as no-rotation "
        f"evidence -- a 180 deg turn about any principal axis leaves the "
        f"ordered AABB dimensions of every part unchanged -- so the "
        f"pure-translation approximation is refused outright for moved "
        f"parts. Re-run the current assembly codegen (its manifest "
        f"records every part's location) instead of guessing the pose."
    )


def _anchor_local_datum(anchor: Anchor, orig_bbox) -> tuple[tuple, tuple]:
    """Part-LOCAL (point, direction) of a FACE / AXIS_POINT anchor,
    computed from the part's ORIGINAL (unplaced) bbox -- the same algebra
    ``_anchor_world_point`` applies to the placed bbox, which is only
    correct when the part was never rotated (P1-6).
    """
    bmin, bmax = orig_bbox
    cx = (bmin[0] + bmax[0]) / 2.0
    cy = (bmin[1] + bmax[1]) / 2.0
    cz = (bmin[2] + bmax[2]) / 2.0
    if anchor.kind == AnchorKind.FACE:
        n = _qa._FACE_NORMALS[anchor.face]
        return (
            (
                cx + n[0] * (bmax[0] - bmin[0]) / 2.0,
                cy + n[1] * (bmax[1] - bmin[1]) / 2.0,
                cz + n[2] * (bmax[2] - bmin[2]) / 2.0,
            ),
            tuple(n),
        )
    axis = anchor.axis or "z"
    if anchor.point_mm is not None:
        return tuple(float(v) for v in anchor.point_mm), _qa._AXIS_DIRS[axis]
    off = anchor.offset_mm or 0.0
    pt = {
        "x": (cx + off, cy, cz),
        "y": (cx, cy + off, cz),
        "z": (cx, cy, cz + off),
    }[axis]
    return pt, _qa._AXIS_DIRS[axis]


def _anchor_world_datum(
    anchor: Anchor,
    part_id: str,
    bboxes,
    step_paths,
    locations,
    moved_set,
) -> tuple[tuple | None, tuple | None, str | None]:
    """Resolve a FACE / AXIS_POINT anchor to (world_point, world_dir).

    For a part that was NEVER moved (not in moved_set) the placed bbox
    equals the original bbox and the local direction IS the world
    direction -- the legacy bbox algebra is exact, kept for compatibility.

    For a MOVED part the datum is computed on the part's ORIGINAL bbox
    (local frame) and then pushed through the placed Location (point via
    ``_local_to_world_point``, direction via ``_local_to_world_direction``
    -- rotation only). Using the PLACED world bbox instead would read the
    wrong face off a rotated part (a part rolled 90 deg has a different
    physical face at the world "top").

    Returns (None, None, error_detail) when the transform is impossible
    (no STEP to measure the original bbox from, or a degenerate world
    direction). Callers must fail their check with the error detail --
    silently falling back to the rotated-part bbox algebra would measure
    the wrong datum.
    """
    if part_id not in moved_set:
        return (
            _qa._anchor_world_point(anchor, *bboxes[part_id]),
            None,  # direction unused by unmoved callers' legacy paths
            None,
        )
    # Placement gate FIRST: a moved part needs the manifest location,
    # full stop (see _moved_part_location -- AABB comparison is not
    # accepted as no-rotation evidence). Without it, the transforms
    # below would silently guess the world datum.
    loc, loc_err = _qa._moved_part_location(part_id, locations)
    if loc_err:
        return None, None, loc_err
    step_path = step_paths.get(part_id)
    if not step_path:
        return None, None, (
            f"{part_id} anchor {anchor.kind.value} datum cannot be "
            f"re-derived: part is moved (placed by a parent mate) but no "
            f"part STEP is available to measure the original bbox"
        )
    orig = _qa._orig_bbox(step_path)
    if orig is None:
        return None, None, (
            f"{part_id} anchor {anchor.kind.value} datum cannot be "
            f"re-derived: original bbox of {step_path} failed to load"
        )
    local_pt, local_dir = _qa._anchor_local_datum(anchor, orig)
    world_pt = _qa._local_to_world_point(
        local_pt, loc, step_path, bboxes[part_id]
    )
    world_dir = _qa._local_to_world_direction(local_dir, loc)
    if world_dir is None:
        return None, None, (
            f"{part_id} anchor {anchor.kind.value} world direction "
            f"degenerate after transform through the placed location"
        )
    return world_pt, world_dir, None


# Bind the facade after definitions so importing either module first works.
# Helpers are not called during import; shared state remains owned by the facade.
from mac_assembly import assembly_qa as _qa
