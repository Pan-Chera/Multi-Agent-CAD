"""Shared geometry math used by both the generated assembly script
(``assembly_codegen``) and the QA engine (``assembly_qa``).

Single source of truth for the axial-offset shift math so the codegen
runtime and the QA mirror cannot diverge -- and for the direction /
rotation / face-normal tables so builders, feature operators, codegen
and QA cannot drift apart (R1).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Direction / rotation tables (single source of truth)
# ---------------------------------------------------------------------------

# Principal axis name -> unit vector / component index.
AXIS_DIRS = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
AXIS_INDEX = {"x": 0, "y": 1, "z": 2}

# Signed direction string ("+x" .. "-z") -> unit vector.
DIR_VECTORS = {
    "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
}

# Bbox face name -> outward unit normal / principal axis of that normal.
FACE_NORMALS = {
    "top": (0.0, 0.0, 1.0), "bottom": (0.0, 0.0, -1.0),
    "right": (1.0, 0.0, 0.0), "left": (-1.0, 0.0, 0.0),
    "back": (0.0, 1.0, 0.0), "front": (0.0, -1.0, 0.0),
}
FACE_AXIS = {
    "top": "z", "bottom": "z", "left": "x", "right": "x",
    "front": "y", "back": "y",
}

# RPY (deg) orienting local +Z along each signed direction. build123d
# Cylinder's default axis is +Z, so Z-axis features (through_bore,
# ball_cavity opening, ball_stem stem, knuckle_ear) rotate via this table.
#   +x: Y rotation +90 deg (+Z -> +X)   -x: Y rotation -90 deg
#   +y: X rotation -90 deg (+Z -> +Y)   -y: X rotation +90 deg
#   +z: identity                        -z: X rotation 180 deg
Z_AXIS_ROTATIONS = {
    "+x": (0.0, 90.0, 0.0), "-x": (0.0, -90.0, 0.0),
    "+y": (-90.0, 0.0, 0.0), "-y": (90.0, 0.0, 0.0),
    "+z": (0.0, 0.0, 0.0), "-z": (180.0, 0.0, 0.0),
}

# RPY (deg) orienting local +X along each signed direction. clevis
# fork/tongue local geometry has its body axis along +X, so those features
# rotate via this table (NOT Z_AXIS_ROTATIONS).
#   +y: rotate +X to +Y -> 90 deg about Z;  -x: 180 deg about Z;
#   -y: -90 deg about Z (== 270).
X_AXIS_ROTATIONS = {
    "+x": (0.0, 0.0, 0.0), "-x": (0.0, 0.0, 180.0),
    "+y": (0.0, 0.0, 90.0), "-y": (0.0, 0.0, -90.0),
    "+z": (0.0, -90.0, 0.0), "-z": (0.0, 90.0, 0.0),
}

# direction string -> its opposite.
OPPOSITE_DIRECTION = {
    "+x": "-x", "-x": "+x",
    "+y": "-y", "-y": "+y",
    "+z": "-z", "-z": "+z",
}


def snap_axis(vec, tol=1e-6):
    """Snap a near-principal direction to the exact principal axis.

    A SELECTOR anchor's world axis is derived by transforming the resolved
    cylinder axis through the fixed part's placed Location. That Location
    carries ~1e-16 float noise (e.g. the Rz(360) identity round-trip inside
    RevoluteJoint.connect_to), so an axis-aligned bore yields e.g.
    (1, 2.8e-32, 0) instead of exactly (1, 0, 0). build123d's Axis.location
    rebuilds a Plane via OCP gp_Ax3, whose DEFAULT x_dir selection branches
    on which direction component is smallest -- exact zeros tie-break into a
    different branch than tiny non-zeros, flipping the plane's x_dir by 90
    degrees and silently rolling the mated part about the joint axis.
    Snapping the world axis to the exact principal axis (when within tol)
    keeps the branch deterministic. Tolerance is far below any real design
    angle, so genuinely tilted axes pass through unchanged.
    """
    v = (float(vec[0]), float(vec[1]), float(vec[2]))
    mag = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
    if mag < 1e-9:
        return v
    dominant = max(range(3), key=lambda i: abs(v[i]))
    if abs(abs(v[dominant]) - mag) > tol * mag:
        return v
    out = [0.0, 0.0, 0.0]
    out[dominant] = 1.0 if v[dominant] > 0.0 else -1.0
    return tuple(out)


def shift_pt(pt, direction, distance):
    """Shift a point along a direction by a signed distance.

    Used to apply ``axial_offset_mm`` to a revolute/coaxial fixed frame:
    shift the fixed anchor along the rotation axis so the moving part
    seats at the desired axial position (not the cylinder midpoint both
    anchors resolve to by default).

    Direction is normalized defensively -- callers pass a unit vector
    (face normal / axis / resolved SELECTOR axis), but we normalize again
    so a non-unit input cannot amplify the offset. A zero-length direction
    is treated as no-shift (returns ``pt`` unchanged) to avoid amplifying
    garbage into the part location.
    """
    mag = (direction[0] ** 2 + direction[1] ** 2 + direction[2] ** 2) ** 0.5
    if mag < 1e-9:
        return (pt[0], pt[1], pt[2])
    inv = 1.0 / mag
    return (pt[0] + distance * direction[0] * inv,
            pt[1] + distance * direction[1] * inv,
            pt[2] + distance * direction[2] * inv)


_MESH_CONTAINMENT_PROBE: bool | None = None


def mesh_containment_available() -> bool:
    """Functional probe (cached) for trimesh's ray-parity containment.

    The optional ``rtree`` package backs BOTH
    ``trimesh.proximity.closest_point`` (raises at call time when missing)
    AND ``Mesh.contains`` (``triangles_tree`` -> ``bounds_tree`` ->
    ``import rtree``). Proximity has a naive fallback
    (``closest_point_robust``: slow but exact); containment has NONE --
    without it every interference / kinematic collision pair silently
    degrades to "sampling failed - pair skipped", i.e. false PASSes. An
    import check is not enough (an alternative ray backend such as
    pyembree could serve containment without rtree), so probe the real
    query once: a point inside a unit box must report contained.

    Callers (QA report warnings, pipeline preflight) surface a False
    result loudly so an environment gap is never misread as clean
    geometry.
    """
    global _MESH_CONTAINMENT_PROBE
    if _MESH_CONTAINMENT_PROBE is None:
        try:
            import trimesh

            probe = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
            _MESH_CONTAINMENT_PROBE = bool(
                probe.contains([[0.0, 0.0, 0.0]])[0]
            )
        except Exception:  # noqa: BLE001 - missing rtree raises here
            _MESH_CONTAINMENT_PROBE = False
    return _MESH_CONTAINMENT_PROBE


def closest_point_robust(mesh, points):
    """``trimesh.proximity.closest_point`` with a no-rtree fallback.

    The accelerated query needs the optional ``rtree`` package and RAISES
    at call time when it is missing (an environment gap that previously
    surfaced mid-workflow as a "geometry failure": snap-to-surface died,
    min-gap returned None, penetration depth degraded to counting every
    inside point). ``closest_point_naive`` is the identical query without
    the spatial index -- O(triangles) per point instead of O(log n), same
    results -- so a missing rtree costs time, never correctness.

    Single source of truth for all proximity queries (feature_operators
    snap, assembly_qa gap/depth) so the fallback cannot drift.
    """
    from trimesh.proximity import closest_point, closest_point_naive

    try:
        return closest_point(mesh, points)
    except Exception:  # noqa: BLE001 - missing rtree raises at call time
        return closest_point_naive(mesh, points)
