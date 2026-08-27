"""Resolve a semantic SELECTOR anchor to real part topology via cadpy.

The Mating Architect cannot know numeric topology ids at plan time (parts
are not built yet), so a SELECTOR anchor carries a *semantic face query*
(surface type + normal/cylinder axis + selection criterion). After a part is
generated, this module builds the part's cadpy ``SelectorIndex`` and resolves
the query to an actual face, returning its real center / axis plus the cadpy
numeric selector (``f5``, ``o1.f5`` ...) for audit/traceability -- the
"semantic query + numeric audit" hybrid.

This is what closes the bbox != functional-datum loop on the ANCHOR side:
a skirted lid's mating face (the plate bottom, not the bbox bottom under the
skirt) or a knobbed lid's seat is located by real topology, so the mate can
actually be placed correctly, not just measured as wrong.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CADPY_SRC = _REPO_ROOT / "packages" / "cadpy" / "src"

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}

_lock = threading.Lock()
_index_cache: dict[str, Any] = {}


def _cache_key(step_path: str) -> str:
    """Cache key includes mtime+size so an in-place regenerated part STEP
    (e.g. after an Aider remodel) never resolves against stale topology."""
    import os

    try:
        st = os.stat(step_path)
        return f"{step_path}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return str(step_path)


def _ensure_cadpy_path() -> None:
    import sys

    if str(_CADPY_SRC) not in sys.path:
        sys.path.insert(0, str(_CADPY_SRC))


def _selector_index(step_path: str):
    """Build (and cache) a cadpy SelectorIndex for a part STEP."""
    key = _cache_key(step_path)
    with _lock:
        if key in _index_cache:
            return _index_cache[key]
    _ensure_cadpy_path()
    from cadpy import lookup
    from cadpy.selector_types import SelectorProfile
    from cadpy.step_scene import extract_selectors_from_scene, load_step_scene

    scene = load_step_scene(Path(step_path))
    bundle = extract_selectors_from_scene(scene, profile=SelectorProfile.REFS)
    index = lookup.build_selector_index(bundle.manifest, buffers=bundle.buffers)
    with _lock:
        _index_cache[key] = (index, lookup)
    return index, lookup


def _dominant_axis(vec) -> int:
    ax = [abs(float(v)) for v in vec]
    return ax.index(max(ax))


def resolve_selector_anchor(step_path: str, query: dict) -> dict | None:
    """Resolve a semantic face query to real topology on ``step_path``.

    Returns a dict::

        {"point": (x, y, z),        # face centre (plane) / point on axis (cyl)
         "axis": (dx, dy, dz),      # plane normal (signed) / cylinder axis
         "selector": "f5",          # cadpy numeric selector (audit)
         "surface": "plane"|"cylinder",
         "radius": float | None,    # cylinder only
         "coordinate": float}       # plane offset along axis / None for cyl

    Returns None when nothing matches (caller decides how to surface it).
    """
    try:
        index, lookup = _selector_index(step_path)
    except Exception:  # noqa: BLE001 - cadpy unavailable / bad STEP
        return None

    surface = str(query.get("surface", "plane")).lower()
    axis_name = str(query.get("axis", "z")).lower()
    axis_index = _AXIS_INDEX.get(axis_name, 2)
    select = str(query.get("select", "largest")).lower()
    value_mm = query.get("value_mm")
    normal_sign = query.get("normal_sign")

    chosen = None
    if surface == "plane":
        chosen = _pick_plane(index, axis_index, normal_sign, select, value_mm)
    elif surface == "cylinder":
        target = (
            query.get("target_x_mm"),
            query.get("target_y_mm"),
            query.get("target_z_mm"),
        )
        chosen = _pick_cylinder(
            index, axis_index, select, value_mm,
            target=target if any(t is not None for t in target) else None,
        )

    if chosen is None:
        return None

    row, point, axis_vec, extra = chosen
    selector_id = str(row.get("id") or "")
    try:
        selector = lookup.display_selector(selector_id, index) or selector_id
    except Exception:  # noqa: BLE001
        selector = selector_id
    return {
        "point": tuple(float(v) for v in point),
        "axis": tuple(float(v) for v in axis_vec),
        "selector": selector,
        "surface": surface,
        "radius": extra.get("radius"),
        "coordinate": extra.get("coordinate"),
    }


def _pick_plane(index, axis_index, normal_sign, select, value_mm):
    candidates = []
    for row in index.faces:
        if str(row.get("surfaceType") or "").lower() != "plane":
            continue
        normal = row.get("normal")
        center = row.get("center")
        if not normal or not center:
            continue
        if _dominant_axis(normal) != axis_index:
            continue
        sign = 1 if float(normal[axis_index]) > 0 else -1
        if normal_sign is not None and sign != int(normal_sign):
            continue
        coord = float(center[axis_index])
        area = float(row.get("area") or 0.0)
        candidates.append((row, center, normal, coord, area))
    if not candidates:
        return None
    if select == "closest_to" and value_mm is not None:
        best = min(candidates, key=lambda c: abs(c[3] - float(value_mm)))
    else:  # largest area
        best = max(candidates, key=lambda c: c[4])
    row, center, normal, coord, _area = best
    return row, center, normal, {"coordinate": coord}


def _pick_cylinder(index, axis_index, select, value_mm, target=None):
    """Pick the best-matching cylinder face from the topology index.

    ``target`` (optional) is a (x, y, z) tuple (part-local) used to
    disambiguate when multiple cylinders have the same (or close) radius
    -- e.g. a link_bar with two through-bores of the same R. Without a
    target, the resolver picks the first matching candidate (Python's
    min/max is stable); with a target, it prefers the cylinder whose axis
    midpoint is nearest the target position.
    """
    candidates = []
    for row in index.faces:
        if str(row.get("surfaceType") or "").lower() != "cylinder":
            continue
        params = row.get("params") or {}
        origin = params.get("origin")
        axis = params.get("axis")
        radius = params.get("radius")
        if origin is None or axis is None:
            continue
        if _dominant_axis(axis) != axis_index:
            continue
        area = float(row.get("area") or 0.0)
        # Datum = the cylinder face's AXIS MIDPOINT. For a 360-degree
        # cylinder the bbox centre lies ON the axis (the cylinder is
        # symmetric around its axis), so the bbox midpoint is the correct
        # axis midpoint regardless of the parametric axis direction sign.
        # The previous formula `origin + axis * (extent/2)` walked in the
        # parametric axis direction from `origin`; when OCCT/build123d
        # stores the parametric axis flipped relative to the face's actual
        # extent (e.g. a post built axis-down has axis=-Z but the face
        # extends upward from Z=10 to Z=30), the computed midpoint landed
        # *outside* the face -- producing a wildly wrong joint datum and
        # inverting the derived frame orientation.
        bbox = row.get("bbox") or {}
        try:
            mn = [float(v) for v in bbox.get("min", [0, 0, 0])]
            mx = [float(v) for v in bbox.get("max", [0, 0, 0])]
            point = [(mn[k] + mx[k]) / 2.0 for k in range(3)]
        except Exception:  # noqa: BLE001
            point = [float(v) for v in origin]
        # Normalize the axis direction sign so the dominant component is
        # positive -- aligning with the face's actual bbox extent
        # direction. OCCT/build123d stores the cylinder's *parametric* axis
        # which can be flipped from the geometric axis direction (a post
        # built axis-down has axis=-Z but extends upward in bbox). The
        # frame's Z direction matters for the moving part's static pose
        # orientation, so we want the geometric direction (the direction
        # the face actually extends, low-bbox to high-bbox).
        axis_list = [float(v) for v in axis]
        dom = _dominant_axis(axis_list)
        if axis_list[dom] < 0:
            axis_list = [-v for v in axis_list]
        axis_norm = tuple(axis_list)
        candidates.append((row, point, axis_norm, float(radius or 0.0), area))
    if not candidates:
        return None
    # If target is given, filter to cylinders within a reasonable radius
    # tolerance first (so target_x_mm doesn't pick a wildly different
    # radius), then pick by target distance.
    if target is not None:
        tx, ty, tz = target
        tx = float(tx) if tx is not None else None
        ty = float(ty) if ty is not None else None
        tz = float(tz) if tz is not None else None
        # If value_mm (radius target) is also given, restrict to cylinders
        # within 0.5mm of the target radius.
        if select == "closest_to" and value_mm is not None:
            pool = [c for c in candidates if abs(c[3] - float(value_mm)) <= 0.5]
            if not pool:
                pool = candidates
        else:
            pool = candidates

        # Distance from target to cylinder's axis LINE (not just bbox
        # centre). This handles two cases the bbox-centre metric misses:
        #
        # (1) Merged cylinder faces: when OCCT unions multiple parallel
        # bores of the same radius into a single cylindrical face (e.g. a
        # palm plate with 4 clevis-fork bores at X=-30/-10/+10/+30 all
        # R=2.5 axis=+X), the merged face's bbox spans all 4 bores
        # (X=-47..+47) and its centre lands at X=0 — but no actual bore
        # is at X=0. The target_x_mm hint is the user's intended bore
        # location; distance-to-axis-line is 0 for all targets on the
        # axis line, so we use axis projection (below) to extract the
        # right point.
        #
        # (2) Single bore with origin outside the part: OCCT stores
        # cylinder origin as the parametric start point, which can be
        # outside the face's actual extent. bbox centre is the right
        # point for a single bore, but if we want to honour target_x_mm
        # we should snap the resolved point to the target's projection
        # on the axis.
        def axis_line_dist(c):
            """Perpendicular distance from target to cylinder's axis line,
            PLUS along-axis penalty when target's along coord is outside
            the face's bbox.

            For a cylinder with origin O and axis direction A (unit), the
            closest point on the axis line to target T is:
                P = O + A * dot(T - O, A)
            The perpendicular distance is |T - P| (only lateral components).

            When the cylinder face is split (e.g. clevis fork bore split
            into upper ear + lower ear by the slot between them), each
            split face has a small bbox. Multiple split faces share the
            same axis line (same lateral position), so perpendicular
            distance alone can't disambiguate which split face is the
            target's intended bore. The along-axis penalty (distance from
            target's along coord to the face's bbox boundary) breaks the
            tie, so a palm with 4 forks at X=-18/-6/+6/+18 correctly
            resolves each target_x to its own fork's split face.
            """
            _row, point, axis, _r, _area = c
            ox, oy, oz = point  # use bbox centre as origin proxy
            ax, ay, az = axis
            bbox = c[0].get("bbox") or {}
            mn = bbox.get("min", [float("-inf")] * 3)
            mx = bbox.get("max", [float("inf")] * 3)
            # Compute dot(T - O, A) using only the non-None target components
            # If a target component is None, skip it (treat as 0 contribution
            # to the projection parameter t, AND exclude from the perpendicular
            # distance computation).
            txv = tx if tx is not None else 0.0
            tyv = ty if ty is not None else 0.0
            tzv = tz if tz is not None else 0.0
            # t = dot(T - O, A) / dot(A, A) — A is unit, so denominator = 1
            t = (txv - ox) * ax + (tyv - oy) * ay + (tzv - oz) * az
            # Closest point on axis line: P = O + A * t
            px = ox + ax * t
            py = oy + ay * t
            pz = oz + az * t
            # Lateral distance² from T to P (only lateral components, i.e.
            # perpendicular to axis). The along component (tx - px) is 0
            # by construction (P is target's projection on axis line).
            # So (tx-px)² is always 0; we exclude it explicitly to make
            # the lateral-only semantics clear.
            d2 = 0.0
            # Identify the along-axis index (dominant axis component)
            along_idx = max(range(3), key=lambda i: abs(axis[i]))
            for i in range(3):
                if i == along_idx:
                    continue  # skip along component (always 0)
                t_i = (tx, ty, tz)[i]
                p_i = (px, py, pz)[i]
                if t_i is not None:
                    d2 += (t_i - p_i) ** 2
            # Along-axis penalty: if target's along coord is outside the
            # face's bbox, add the squared distance to the nearest bbox
            # boundary. This breaks ties between split faces sharing the
            # same axis line (e.g. 4 palm forks at different X).
            t_along = (tx, ty, tz)[along_idx]
            if t_along is not None:
                bbox_min_along = float(mn[along_idx]) if along_idx < len(mn) else float("-inf")
                bbox_max_along = float(mx[along_idx]) if along_idx < len(mx) else float("inf")
                if t_along < bbox_min_along - 0.5:
                    d2 += (bbox_min_along - t_along) ** 2
                elif t_along > bbox_max_along + 0.5:
                    d2 += (t_along - bbox_max_along) ** 2
            return d2

        best = min(pool, key=axis_line_dist)

        # Override the resolved point with the target's projection onto
        # the chosen cylinder's axis line. For a merged face, this gives
        # the correct per-bore attach point (e.g. (-30, 30, 5) instead of
        # the merged face's bbox centre (0, 30, 5)). For a single bore,
        # the projection is the target itself, so this is a no-op when
        # the target lies on the axis (the typical case).
        _row, point, axis, _r, _area = best
        ox, oy, oz = point
        ax, ay, az = axis
        txv = tx if tx is not None else 0.0
        tyv = ty if ty is not None else 0.0
        tzv = tz if tz is not None else 0.0
        t = (txv - ox) * ax + (tyv - oy) * ay + (tzv - oz) * az
        proj_point = (ox + ax * t, oy + ay * t, oz + az * t)
        # Sanity check: projection must lie within the face's bbox (+
        # small tolerance). If outside, the target is not on this face's
        # axis — fall back to bbox centre (preserves legacy behavior).
        bbox = best[0].get("bbox") or {}
        mn = bbox.get("min", [float("-inf")] * 3)
        mx = bbox.get("max", [float("inf")] * 3)
        tol = 0.5
        if (mn[0] - tol <= proj_point[0] <= mx[0] + tol
                and mn[1] - tol <= proj_point[1] <= mx[1] + tol
                and mn[2] - tol <= proj_point[2] <= mx[2] + tol):
            point = proj_point
        # Re-pack best with the updated point (otherwise the unpack below
        # would clobber proj_point with the original bbox centre).
        best = (best[0], point, best[2], best[3], best[4])
    elif select == "closest_to" and value_mm is not None:
        best = min(candidates, key=lambda c: abs(c[3] - float(value_mm)))
    else:  # largest lateral area
        best = max(candidates, key=lambda c: c[4])
    row, point, axis, radius, _area = best
    # Clevis-fork midpoint: when the chosen cylinder has one or more
    # "twins" at the same lateral position (perpendicular to the cylinder
    # axis) but a different along-axis position, the bore has been split
    # by a slot (e.g. upper ear + lower ear of a clevis fork, with the
    # slot between them being air). The pin axis is at the MIDPOINT of
    # the twin group, not at either ear's midpoint.
    #
    # The previous logic was hardcoded for pin_axis=z (twins at same
    # X/Y, different Z). With pin_axis=x (v3 dexterous hand design),
    # the slot is along X, so twins are at same Y/Z, different X. The
    # logic now uses the cylinder's axis direction to determine which
    # coordinate is "along" vs "lateral".
    #
    # Without this, the resolver returns one ear's bbox centre (e.g.
    # X=-3.05 for the lower ear of a pin_axis=x fork), the revolute
    # frame lands there, and the mating part gets shifted by 3.05mm in
    # X to align -- producing a misaligned chain (fingers not centered
    # on palm forks, thumb shifted off the cavity axis).
    ax_dominant = max(range(3), key=lambda i: abs(axis[i]))
    lateral_indices = [i for i in range(3) if i != ax_dominant]
    along_index = ax_dominant
    twin_lateral_tolerance = 0.5  # mm, same axis location in lateral plane
    twin_along_min_delta = 0.1  # mm, distinct along-axis (avoid picking up same face)
    # Twins are split faces of the SAME bore (upper ear + lower ear of one
    # clevis fork). Along-axis distance between split faces ~= bar_thickness
    # - fork_gap_z (e.g. 8 - 4.2 = 3.8mm bbox extent, with bbox-centre delta
    # of ~6mm). Cap at 8mm so adjacent forks (spacing 12mm, nearest split
    # face at ~5.9mm — too close to split distance 6.1mm to distinguish
    # by distance alone) don't get picked up blindly.
    twin_along_max_delta = 8.0  # mm
    # Disambiguate split-face twins from adjacent-fork near-twins by
    # picking the twin whose MIDPOINT with the selected is closest to
    # the target's along coord. The target X (e.g. -18 for palm fork 1)
    # is the user's intended bore centre; the correct twin is the one
    # whose midpoint with the selected matches the target.
    target_along = (tx, ty, tz)[along_index] if target is not None else None
    same_axis_candidates = []
    for c in candidates:
        _r2, p2, _ax2, rad2, _area2 = c
        if abs(rad2 - radius) > 0.5:
            continue
        along_diff = abs(p2[along_index] - point[along_index])
        if (abs(p2[lateral_indices[0]] - point[lateral_indices[0]]) <= twin_lateral_tolerance
                and abs(p2[lateral_indices[1]] - point[lateral_indices[1]]) <= twin_lateral_tolerance
                and twin_along_min_delta < along_diff <= twin_along_max_delta):
            same_axis_candidates.append(p2)
    if same_axis_candidates and target_along is not None:
        # Pick twin whose midpoint with selected is closest to target_along
        best_twin = None
        best_mid_dist = float("inf")
        for p2 in same_axis_candidates:
            mid = (p2[along_index] + point[along_index]) / 2.0
            dist = abs(mid - target_along)
            if dist < best_mid_dist:
                best_mid_dist = dist
                best_twin = p2
        if best_twin is not None:
            mid_along = (best_twin[along_index] + point[along_index]) / 2.0
            point_list = list(point)
            point_list[along_index] = mid_along
            point = tuple(point_list)
    elif same_axis_candidates:
        # No target along coord: average all twins (legacy behavior)
        all_alongs = [point[along_index]] + [t[along_index] for t in same_axis_candidates]
        mid_along = (min(all_alongs) + max(all_alongs)) / 2.0
        point_list = list(point)
        point_list[along_index] = mid_along
        point = tuple(point_list)
    return row, point, axis, {"radius": radius}


def cylinder_radii_along(step_path: str, axis: str) -> list[float]:
    """All cylindrical-face radii on ``step_path`` whose axis is ``axis``.

    Used by the reconcile gate to check shaft/bore radius compatibility
    before an assembly cycle is spent (R6). Empty when cadpy is
    unavailable or the part has no cylinders along that axis.
    """
    try:
        index, _lookup = _selector_index(step_path)
    except Exception:  # noqa: BLE001
        return []
    axis_index = _AXIS_INDEX.get(str(axis).lower(), 2)
    radii: list[float] = []
    for row in index.faces:
        if str(row.get("surfaceType") or "").lower() != "cylinder":
            continue
        params = row.get("params") or {}
        origin = params.get("origin")
        cyl_axis = params.get("axis")
        radius = params.get("radius")
        if origin is None or cyl_axis is None or radius is None:
            continue
        if _dominant_axis(cyl_axis) != axis_index:
            continue
        radii.append(float(radius))
    return radii


def query_key(query: dict) -> str:
    """Stable string key for a face query (used in audit logs)."""
    return json.dumps(query, sort_keys=True)
