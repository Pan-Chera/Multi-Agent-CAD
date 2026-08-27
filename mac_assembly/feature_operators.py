"""v3 Feature operators: kinematic features attach to an LLM-generated base body.

Each operator has signature ``(base: Compound, feature: Feature) -> Compound``.
The base body is loaded via build123d.import_step from the LLM-generated
STEP. The operator builds feature geometry in local coords (bore at
origin), positions + orients it at ``feature.attachment.attach_point_mm``
+ ``feature.attachment.direction``, applies a 0.2mm overshoot along
-direction (into the base) so the boolean union happens between
intersecting volumes (not at a coincident-face tangent), and fuses.

Subtractive operators (through_bore, ball_cavity) overshoot 1mm
already (preserves bore/cavity face topology -- see plan §2e/§2h).

Snap-to-surface (plan §2g): if attach_point is >0.5mm from the base
surface, snap along -direction to the nearest surface. Then verify
the surface normal at the snap point is within 15° of -direction
(planar kinematics protection); if not, revert to original attach_point
+ log warning, let QA catch the bad geometry.

Multi-solid Compound fallback (plan §2f): if direct ``base + feature``
raises BRep_API, try fusing against each base solid individually; if
all fail, emit the feature as a disjoint solid in the Compound (the
part doesn't crash the pipeline; QA catches the disjoint geometry).
"""
from __future__ import annotations

import math
from typing import Any, Callable

from mac_assembly.schemas_assembly import Feature


# Direction -> (rotation rpy deg to orient local +X protrusion along direction,
#               unit vector of protrusion direction)
# Local feature geometry has bore at origin, body extending -X (the "back").
# For direction=+x (protrudes +X): no rotation needed; body in -X is "back".
# For direction=+y: rotate +X to +Y -> rotation 90° about Z. Body local -X
#   becomes global -Y (back away from +Y protrusion).
# For direction=-x: rotate 180° about Z. Body local -X becomes +X (back).
# For direction=-y: rotate -90° about Z (or 270°).
# For direction=+z / -z: not supported for clevis_fork/tongue (planar only).
_PROTRUSION_ROTATIONS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "+x": ((0.0, 0.0, 0.0),     (1.0, 0.0, 0.0)),
    "-x": ((0.0, 0.0, 180.0),   (-1.0, 0.0, 0.0)),
    "+y": ((0.0, 0.0, 90.0),    (0.0, 1.0, 0.0)),
    "-y": ((0.0, 0.0, -90.0),   (0.0, -1.0, 0.0)),
    "+z": ((0.0, -90.0, 0.0),   (0.0, 0.0, 1.0)),
    "-z": ((0.0, 90.0, 0.0),    (0.0, 0.0, -1.0)),
}

# Pin-axis rotation table for clevis_fork / clevis_tongue.
# Builds on _PROTRUSION_ROTATIONS by adding an extra rotation to re-orient
# the bore axis from +Z (default) to +X / +Y / +Z. This enables non-planar
# kinematics: pin=X means the clevis rotates about the world X axis (motion
# in YZ plane, e.g. fingers curling toward the palm); pin=Y means rotation
# about Y (motion in XZ plane); pin=Z (default) is the legacy planar mode
# (motion in XY plane).
#
# Local fork/tongue frame: body extends -X, bore along +Z at local origin
# (0, 0, bar_thickness/2). For pin_axis != "z", we pre-shift the fork by
# (0, 0, -bar_thickness/2) so the bore sits at the local origin *before*
# rotation. After rotation + Pos(attach), the bore ends up exactly at the
# attach_point. This means for pin_axis != "z", attach_z IS the bore center
# Z (not attach_z + bar_thickness/2 as in pin_axis="z" legacy mode).
#
# Convention: `direction` always describes the body protrusion axis (the
# direction the tip points). For pin="z" direction must be ±x/±y (planar
# XY); for pin="x" direction must be ±y/±z (planar YZ); for pin="y"
# direction must be ±x/±z (planar XZ). pin parallel to direction is invalid.
#
# RPY values were found by brute-force search using actual build123d
# Rotation(R, P, Y) = R_z(Y) * R_x(R) * R_y(P) semantics, choosing the
# candidate with smallest |R|+|P|+|Y|.
_FORK_ROTATIONS: dict[tuple[str, str], tuple[float, float, float]] = {
    # pin=Z: bore +Z, body in -direction (XY plane) -- legacy planar mode
    ("z", "+x"): (0.0, 0.0, 0.0),
    ("z", "-x"): (0.0, 0.0, -180.0),
    ("z", "+y"): (0.0, 0.0, 90.0),
    ("z", "-y"): (0.0, 0.0, -90.0),
    # pin=X: bore +X, body in -direction (YZ plane) -- fingers curl toward palm
    ("x", "+y"): (0.0, 90.0, 90.0),
    ("x", "-y"): (-90.0, 90.0, 0.0),
    ("x", "+z"): (-180.0, 90.0, 0.0),
    ("x", "-z"): (0.0, 90.0, 0.0),
    # pin=Y: bore +Y, body in -direction (XZ plane)
    ("y", "+x"): (-90.0, 0.0, 0.0),
    ("y", "-x"): (-90.0, 0.0, -180.0),
    ("y", "+z"): (-90.0, 0.0, -90.0),
    ("y", "-z"): (-90.0, 0.0, 90.0),
}

# Per-axis rotation to align a +Z cylinder with the chosen pin axis.
# Used by bore-through-base cut: subtract a cylinder along pin_axis at
# attach_point so the pin passes cleanly through both the fork/tongue and
# the base body.
_PIN_BORE_ROTATIONS: dict[str, tuple[float, float, float]] = {
    "x": (0.0, 90.0, 0.0),    # +Z → +X
    "y": (-90.0, 0.0, 0.0),   # +Z → +Y
    "z": (0.0, 0.0, 0.0),     # +Z unchanged
}

# Rotation table for Z-axis features (cylinder along +Z by default).
# build123d Cylinder default axis is +Z. To make the cylinder protrude in
# `direction`, rotate +Z -> direction:
#   +x: Y rotation +90° (right-hand rule: +Z -> +X)
#   -x: Y rotation -90° (+Z -> -X)
#   +y: X rotation -90° (+Z -> +Y)
#   -y: X rotation +90° (+Z -> -Y)
#   +z: no rotation (cylinder already along +Z)
#   -z: X rotation 180° (+Z -> -Z)
# Used by: through_bore, ball_cavity opening, ball_stem stem, knuckle_ear.
# (clevis_fork / clevis_tongue use _PROTRUSION_ROTATIONS -- their local body
# axis is X, not Z.)
_Z_AXIS_ROTATIONS: dict[str, tuple[float, float, float]] = {
    "+x": (0.0, 90.0, 0.0),
    "-x": (0.0, -90.0, 0.0),
    "+y": (-90.0, 0.0, 0.0),
    "-y": (90.0, 0.0, 0.0),
    "+z": (0.0, 0.0, 0.0),
    "-z": (180.0, 0.0, 0.0),
}

# Overshoot into the base along -direction (additive ops). 0.2mm matches
# existing clevis clearance_side default; well above OpenCASCADE 1e-6 tolerance.
_ADDITIVE_OVERSHOOT_MM = 0.2

# Snap-to-surface tolerance + normal angle threshold.
_SNAP_TOLERANCE_MM = 0.5
_MAX_NORMAL_ANGLE_DEG = 15.0


def _snap_to_surface(base: Any, attach_point_mm: list[float], direction: str) -> tuple[list[float], str | None]:
    """If attach_point is >tolerance from base surface, snap along -direction.

    Returns (final_point, warning_msg). final_point is snapped_point if
    snap succeeds + normal aligned, else original attach_point. warning_msg
    is non-None if we reverted due to tilted normal.
    """
    try:
        import trimesh
        import numpy as np
    except ImportError:
        return list(attach_point_mm), None

    protrusion_vec = _PROTRUSION_ROTATIONS.get(direction, ((0, 0, 0), (1, 0, 0)))[1]
    anti_dir = [-v for v in protrusion_vec]

    # Probe the base surface: find the nearest surface point to attach_point.
    try:
        # Extract mesh from base (build123d Compound -> trimesh via STL export)
        import io
        from build123d import export_stl
        buf = io.BytesIO()
        # build123d export_stl needs a file path; use NamedTemporaryFile
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            export_stl(base, tmp_path, tolerance=0.1, angular_tolerance=0.5)
            mesh = trimesh.load(tmp_path, force="mesh")
        finally:
            import os
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if mesh.is_empty:
            return list(attach_point_mm), None
    except Exception:  # noqa: BLE001
        return list(attach_point_mm), None

    # closest_point: nearest surface point to attach_point
    closest, _dist, _triangle_id = trimesh.proximity.closest_point(
        mesh, [attach_point_mm]
    )
    closest = closest[0]
    snap_dist = float(np.linalg.norm(np.asarray(closest) - np.asarray(attach_point_mm)))

    if snap_dist <= _SNAP_TOLERANCE_MM:
        return list(attach_point_mm), None  # within tolerance, no snap

    # Snap: check surface normal at the snap point
    # Find the face containing `closest`, get its normal
    face_normal = _face_normal_at(mesh, closest)
    if face_normal is None:
        # Couldn't get normal; snap silently without normal check
        return list(closest), None

    # Angle between face_normal and anti_dir
    fn = np.asarray(face_normal, dtype=float)
    ad = np.asarray(anti_dir, dtype=float)
    fn_norm = np.linalg.norm(fn)
    ad_norm = np.linalg.norm(ad)
    if fn_norm < 1e-9 or ad_norm < 1e-9:
        return list(closest), None
    cos_angle = float(np.dot(fn, ad) / (fn_norm * ad_norm))
    cos_angle = max(-1.0, min(1.0, cos_angle))
    angle_deg = math.degrees(math.acos(cos_angle))

    if angle_deg > _MAX_NORMAL_ANGLE_DEG:
        # Normal too tilted: snap would tilt the feature on a curved surface.
        # Revert to original attach_point + warn.
        return list(attach_point_mm), (
            f"snap surface normal tilted {angle_deg:.1f}° from -{direction} "
            f"at {list(closest)} -- feature would be tilted on curved surface, "
            f"breaking planar kinematics. Reverting to original attach_point "
            f"{list(attach_point_mm)}; QA will catch disjoint/tilted geometry."
        )

    # Snap OK
    return list(closest), None


def _face_normal_at(mesh: Any, point) -> list[float] | None:
    """Get the face normal at the given point on the mesh."""
    try:
        import trimesh  # N1: was missing -- NameError -> except -> None -> tilted-normal check dead
        # trimesh: find which face contains the point, return its normal
        # closest_point returns triangle_id; re-query to get the face
        _, _dist, tri_id = trimesh.proximity.closest_point(mesh, [point])
        tri_id = int(tri_id[0])
        face_normal = mesh.face_normals[tri_id]
        return [float(face_normal[0]), float(face_normal[1]), float(face_normal[2])]
    except Exception:  # noqa: BLE001
        return None


def _build_feature_solid(feature: Feature) -> Any:
    """Build just the feature geometry in local coords (no base, no overshoot
    applied yet). Used as last-ditch disjoint-solid fallback.
    """
    from build123d import Compound
    # This is operator-specific; dispatch via FEATURE_OPERATORS_BUILD_ONLY
    builder = FEATURE_OPERATORS_BUILD_ONLY.get(feature.name)
    if builder is None:
        raise KeyError(f"unknown feature operator: {feature.name!r}")
    return builder(feature)


def apply_feature(base: Any, feature: Feature) -> Any:
    """Dispatch a feature operator by name. Multi-solid fallback (plan §2f):
    if direct op raises, try per-solid fuse; if all fail, emit as disjoint solid.
    """
    op = FEATURE_OPERATORS[feature.name]
    try:
        return op(base, feature)
    except Exception as exc:  # noqa: BLE001
        # Multi-solid Compound base: try fusing against each base solid.
        import build123d
        base_solids = list(base.solids()) if hasattr(base, "solids") else [base]
        result_solids = []
        feature_applied = False
        for s in base_solids:
            if not feature_applied:
                try:
                    fused = op(s, feature)
                    if hasattr(fused, "solids"):
                        result_solids.extend(fused.solids())
                    else:
                        result_solids.append(fused)
                    feature_applied = True
                    continue
                except Exception:  # noqa: BLE001
                    pass
            result_solids.append(s)
        if not feature_applied:
            # Last-ditch: emit the feature as a SEPARATE solid in the Compound.
            print(f"  [feature_operator] WARNING: {feature.name} at "
                  f"{feature.attachment.attach_point_mm} could not fuse with "
                  f"any base solid; emitting as disjoint solid. "
                  f"Underlying error: {exc}")
            try:
                feat_solid = _build_feature_solid(feature)
                result_solids.append(feat_solid)
            except Exception:  # noqa: BLE001
                pass  # give up entirely; base only
        return build123d.Compound(children=result_solids)


# ---------------------------------------------------------------------------
# Operator implementations
# ---------------------------------------------------------------------------


def _clevis_pin_axis(params: dict) -> str:
    """Read pin_axis from feature params, defaulting to "z" (legacy planar)."""
    pin_axis = str(params.get("pin_axis", "z")).lower()
    if pin_axis not in ("x", "y", "z"):
        raise ValueError(
            f"pin_axis must be 'x', 'y', or 'z', got {pin_axis!r}"
        )
    return pin_axis


def _clevis_placement_and_bore_cut(
    attach: list[float],
    direction: str,
    pin_axis: str,
    bar_thickness: float,
    bore_radius: float,
    feature_kind: str,
):
    """Return (placement_lambda, bore_cut_lambda) for clevis_fork/tongue.

    placement_lambda(local_geom) returns the positioned compound:
        Pos(attach) * Rotation(rpy) * Pos(pre_shift) * local_geom

    bore_cut_lambda(assembly_bb) is preserved for API compatibility but
    NO LONGER CALLED by _op_clevis_fork / _op_clevis_tongue. The previous
    design subtracted a cylinder through the base body at attach_point
    ("bore-through-base cut") so the pin could pass through; this left
    half the bore embedded inside the base body when attach_point sat on
    the base body's surface. The v3 fork/tongue geometry now places bore
    entirely on the clevis ear (at local X=body_len, far from attach),
    so no base-body cut is needed.

    Convention (placement):
      - pin_axis="z" (legacy): bore world = (attach_x + body_len,
        attach_y, attach_z + bar_thickness/2). Prompt uses attach_z =
        bore_z - bar_thickness/2 (e.g. attach_z=1 -> bore at Z=5 with
        bar=8).
      - pin_axis="x"/"y": bore world = attach + direction * body_len.
        Prompt uses attach_z = bore_z directly (e.g. attach_z=5 -> bore
        at Z=5). Pre-shift of (0, 0, -bar_thickness/2) on the local
        geometry centers the bore at local Z=0 before rotation, so
        after placement bore Z = attach_z exactly.
    """
    from build123d import Pos, Rotation

    rpy = _FORK_ROTATIONS.get((pin_axis, direction))
    if rpy is None:
        raise ValueError(
            f"clevis {feature_kind}: pin_axis={pin_axis!r} direction={direction!r} "
            f"is invalid (pin parallel to direction, or unsupported combo). "
            f"Valid: pin=z -> dir=±x/±y; pin=x -> dir=±y/±z; pin=y -> dir=±x/±z."
        )

    if pin_axis == "z":
        pre_shift = (0.0, 0.0, 0.0)
    else:
        pre_shift = (0.0, 0.0, -bar_thickness / 2.0)

    def placement(local_geom):
        return (
            Pos(attach[0], attach[1], attach[2])
            * Rotation(rpy[0], rpy[1], rpy[2])
            * Pos(pre_shift[0], pre_shift[1], pre_shift[2])
            * local_geom
        )

    bore_rot_rpy = _PIN_BORE_ROTATIONS[pin_axis]

    def bore_cut(assembly):
        # Preserved for API compatibility; no longer called by v3
        # operators (bore now lives entirely on the clevis ear, so no
        # base-body cut is needed). Kept so legacy callers don't break.
        from build123d import Align, Cylinder, Pos, Rotation
        bb = assembly.bounding_box()
        if pin_axis == "x":
            extent = bb.max.X - bb.min.X
            center_along = (bb.min.X + bb.max.X) / 2.0
            pos = (center_along, attach[1], attach[2])
        elif pin_axis == "y":
            extent = bb.max.Y - bb.min.Y
            center_along = (bb.min.Y + bb.max.Y) / 2.0
            pos = (attach[0], center_along, attach[2])
        else:  # z
            extent = bb.max.Z - bb.min.Z
            center_along = (bb.min.Z + bb.max.Z) / 2.0
            pos = (attach[0], attach[1], center_along)
        return (
            Pos(pos[0], pos[1], pos[2])
            * Rotation(bore_rot_rpy[0], bore_rot_rpy[1], bore_rot_rpy[2])
            * Cylinder(
                radius=bore_radius, height=extent + 2.0,
                align=(Align.CENTER, Align.CENTER, Align.CENTER),
            )
        )

    return placement, bore_cut


def _v3_fork_local(
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    bore_radius: float,
    bar_thickness: float,
    clearance_side: float = 0.1,
    overshoot_mm: float = 0.0,
):
    """Build a v3 clevis fork in LOCAL coords with bore at the ear TIP
    (local X=body_len), NOT at the local origin.

    Ear body extends in +X direction from local origin (attach_point)
    to local X=body_len. Tip + bore sit at local X=body_len. After
    placement by ``_clevis_placement_and_bore_cut``, the ear body sits
    OUTSIDE the base body (in +direction from attach_point), and the
    bore sits at ``attach + direction * body_len`` -- far from the base
    body surface. Bore's circular cross-section is entirely contained
    in the ear tip (R_tip > bore_radius), so no half-bore is embedded
    inside the base body.

    Local coord convention:
      - Bore center: (body_len, 0, bar_thickness/2)
        where body_len = ear_length - R_tip
      - Ear body (rectangular slab): X from -overshoot_mm to body_len
        (overshoot extends into -X to overlap the base body for a clean
        boolean union -- avoids coincident-face union failure)
      - Upper ear Z: (bar_thickness + fork_gap_z)/2 .. bar_thickness
      - Lower ear Z: 0 .. (bar_thickness - fork_gap_z)/2
    """
    from build123d import Align, Box, Cylinder, Pos

    R_tip = ear_width / 2.0
    fork_gap_z = tongue_thickness + 2.0 * clearance_side
    upper_ear_z_extent = (bar_thickness - fork_gap_z) / 2.0
    lower_ear_z_extent = (bar_thickness - fork_gap_z) / 2.0
    upper_ear_z_min = (bar_thickness + fork_gap_z) / 2.0
    lower_ear_z_max = (bar_thickness - fork_gap_z) / 2.0

    body_len = ear_length - R_tip
    body_x_min = -overshoot_mm
    body_x_max = body_len
    body_len_total = body_x_max - body_x_min
    body_center_x = (body_x_min + body_x_max) / 2.0
    upper_ear_z_center = (upper_ear_z_min + bar_thickness) / 2.0
    lower_ear_z_center = lower_ear_z_max / 2.0

    upper_body = Pos(body_center_x, 0, upper_ear_z_center) * Box(
        body_len_total, ear_width, upper_ear_z_extent,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    upper_tip = Pos(body_len, 0, upper_ear_z_center) * Cylinder(
        radius=R_tip, height=upper_ear_z_extent,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    lower_body = Pos(body_center_x, 0, lower_ear_z_center) * Box(
        body_len_total, ear_width, lower_ear_z_extent,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    lower_tip = Pos(body_len, 0, lower_ear_z_center) * Cylinder(
        radius=R_tip, height=lower_ear_z_extent,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    bore = Pos(body_len, 0, bar_thickness / 2.0) * Cylinder(
        radius=bore_radius, height=bar_thickness + 2.0,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    return upper_body + upper_tip + lower_body + lower_tip - bore


def _v3_tongue_local(
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    bore_radius: float,
    bar_thickness: float,
    overshoot_mm: float = 0.0,
):
    """Build a v3 clevis tongue in LOCAL coords with bore at the ear TIP
    (local X=body_len), NOT at the local origin.

    Single mid-Z slab with a semicircular tip at +X end. Bore center at
    (body_len, 0, bar_thickness/2). Body extends +X (with overshoot_mm
    extending into -X to overlap the base body for clean boolean union).
    """
    from build123d import Align, Box, Cylinder, Pos

    R_tip = ear_width / 2.0
    body_len = ear_length - R_tip
    body_x_min = -overshoot_mm
    body_x_max = body_len
    body_len_total = body_x_max - body_x_min
    body_center_x = (body_x_min + body_x_max) / 2.0
    tongue_z_center = bar_thickness / 2.0

    body = Pos(body_center_x, 0, tongue_z_center) * Box(
        body_len_total, ear_width, tongue_thickness,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    tip = Pos(body_len, 0, tongue_z_center) * Cylinder(
        radius=R_tip, height=tongue_thickness,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    bore = Pos(body_len, 0, bar_thickness / 2.0) * Cylinder(
        radius=bore_radius, height=bar_thickness + 2.0,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    return body + tip - bore


def _op_clevis_fork(base: Any, feature: Feature) -> Any:
    """Attach a clevis fork at attach_point in direction with pin_axis.

    v3 geometry: ear body extends in +direction (OUTSIDE the base body);
    bore sits at the ear tip (local X=body_len = attach + direction *
    body_len in world), entirely on the clevis. The previous design placed
    bore at attach_point (= base body surface) and cut a cylinder through
    the base body, leaving half the bore embedded inside the base body
    (unusable for assembly). The new design keeps the entire bore on the
    added clevis, so the base body has no hole.
    """
    from mac_assembly.builders import _clevis_validate

    params = dict(feature.params)
    ear_length = float(params["ear_length"])
    ear_width = float(params["ear_width"])
    tongue_thickness = float(params["tongue_thickness"])
    bore_radius = float(params["bore_radius"])
    bar_thickness = float(params.get("bar_thickness", tongue_thickness + 2 * 0.1 + 2.0))
    clearance_side = float(params.get("clearance_side", 0.1))

    _clevis_validate(bar_thickness, bore_radius, ear_length, ear_width,
                     tongue_thickness, clearance_side, bar_width=None)

    direction = feature.attachment.direction
    pin_axis = _clevis_pin_axis(params)
    attach = feature.attachment.attach_point_mm

    snapped, warning = _snap_to_surface(base, attach, direction)
    if warning:
        print(f"  [feature_operator] WARNING: {warning}")
    attach = snapped

    placement, _bore_cut_unused = _clevis_placement_and_bore_cut(
        attach, direction, pin_axis, bar_thickness, bore_radius, "clevis_fork"
    )

    fork = _v3_fork_local(ear_length, ear_width, tongue_thickness, bore_radius,
                          bar_thickness, clearance_side,
                          overshoot_mm=_ADDITIVE_OVERSHOOT_MM)
    return base + placement(fork)


def _op_clevis_tongue(base: Any, feature: Feature) -> Any:
    """Attach a clevis tongue at attach_point in direction with pin_axis.

    v3 geometry: ear body extends in +direction (OUTSIDE the base body);
    bore sits at the ear tip, entirely on the clevis. No base-body cut.
    """
    from mac_assembly.builders import _clevis_validate

    params = dict(feature.params)
    ear_length = float(params["ear_length"])
    ear_width = float(params["ear_width"])
    tongue_thickness = float(params["tongue_thickness"])
    bore_radius = float(params["bore_radius"])
    bar_thickness = float(params.get("bar_thickness", tongue_thickness + 2 * 0.1 + 2.0))
    clearance_side = float(params.get("clearance_side", 0.1))

    _clevis_validate(bar_thickness, bore_radius, ear_length, ear_width,
                     tongue_thickness, clearance_side, bar_width=None)

    direction = feature.attachment.direction
    pin_axis = _clevis_pin_axis(params)
    attach = feature.attachment.attach_point_mm

    snapped, warning = _snap_to_surface(base, attach, direction)
    if warning:
        print(f"  [feature_operator] WARNING: {warning}")
    attach = snapped

    placement, _bore_cut_unused = _clevis_placement_and_bore_cut(
        attach, direction, pin_axis, bar_thickness, bore_radius, "clevis_tongue"
    )

    tongue = _v3_tongue_local(ear_length, ear_width, tongue_thickness,
                              bore_radius, bar_thickness,
                              overshoot_mm=_ADDITIVE_OVERSHOOT_MM)
    return base + placement(tongue)


def _op_through_bore(base: Any, feature: Feature) -> Any:
    """Subtract a cylinder bore along an axis at attach_point.

    Overshoots each end by 1mm to preserve bore cylindrical face topology
    (plan §2h). The bore axis is `direction` (±x/±y/±z).
    """
    from build123d import Align, Cylinder, Pos, Rotation

    params = dict(feature.params)
    radius = float(params["radius"])
    height = float(params.get("height", 0.0))  # default: auto-overshoot

    direction = feature.attachment.direction
    # Z-axis feature (cylinder default axis +Z): use _Z_AXIS_ROTATIONS,
    # NOT _PROTRUSION_ROTATIONS (which is for X-axis clevis body).
    rot_rpy = _Z_AXIS_ROTATIONS.get(direction, (0.0, 0.0, 0.0))

    attach = feature.attachment.attach_point_mm
    # Probe base bbox to determine bore length (overshoot each end by 1mm).
    # Pick the extent along the bore axis (direction), not the global max --
    # a thin plate (e.g. 100x100x10) with direction=+z would otherwise
    # generate a 100mm bore instead of the needed 10mm.
    bb = base.bounding_box()
    if height <= 0:
        # Auto-length: span = bbox extent along the bore axis. The
        # Cylinder's +2.0 below adds the 1mm-each-end overshoot.
        extents = {
            "+x": bb.max.X - bb.min.X, "-x": bb.max.X - bb.min.X,
            "+y": bb.max.Y - bb.min.Y, "-y": bb.max.Y - bb.min.Y,
            "+z": bb.max.Z - bb.min.Z, "-z": bb.max.Z - bb.min.Z,
        }
        height = extents.get(direction) or max(
            bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, bb.max.Z - bb.min.Z
        )
    # Cylinder default axis is +Z; rotate via _Z_AXIS_ROTATIONS to align
    # with protrusion direction.
    bore = Cylinder(
        radius=radius, height=height + 2.0,  # +1mm each end overshoot
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    placed = (
        Pos(attach[0], attach[1], attach[2])
        * Rotation(rot_rpy[0], rot_rpy[1], rot_rpy[2])
        * bore
    )
    return base - placed


def _op_ball_cavity(base: Any, feature: Feature) -> Any:
    """Subtract a sphere cavity at attach_point + optional opening cylinder.

    The cavity center is at attach_point. If opening_radius is set,
    subtract a cylinder along +direction from the cavity outward (so
    a ball stem can pass through the housing).
    """
    from build123d import Align, Cylinder, Pos, Rotation, Sphere

    params = dict(feature.params)
    sphere_radius = float(params["sphere_radius"])
    opening_radius = float(params.get("opening_radius", 0.0))

    attach = feature.attachment.attach_point_mm
    cavity = Pos(attach[0], attach[1], attach[2]) * Sphere(radius=sphere_radius)
    body = base - cavity

    if opening_radius > 0:
        direction = feature.attachment.direction
        # Z-axis feature: use _Z_AXIS_ROTATIONS (cylinder default axis +Z).
        rot_rpy = _Z_AXIS_ROTATIONS.get(direction, (0.0, 0.0, 0.0))
        # Opening cylinder: along +direction, from cavity center outward
        # (NOT extending in -direction, which would凿穿 the base body
        # on the other side). Use Align.MIN so the cylinder base sits at
        # the cavity center and extends +direction by cyl_height.
        bb = body.bounding_box()
        max_extent = max(bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, bb.max.Z - bb.min.Z)
        cyl_height = sphere_radius + max_extent + 1.0
        opening = (
            Pos(attach[0], attach[1], attach[2])
            * Rotation(rot_rpy[0], rot_rpy[1], rot_rpy[2])
            * Cylinder(
                radius=opening_radius, height=cyl_height,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
            )
        )
        body = body - opening
    return body


def _op_ball_stem(base: Any, feature: Feature) -> Any:
    """Attach a ball-joint sphere COMPLETELY OUTSIDE the base body,
    connected by a small stem cylinder from the base body surface to
    the ball.

    Geometry (per user design — "small cylinder protrudes the ball so
    it can move freely"):

      - Ball center at ``attach_point + (-direction) * (stem_length +
        sphere_radius)``. The ball surface's nearest point to the base
        body is at ``attach + (-direction) * stem_length`` -- exactly
        ``stem_length`` away from the base body surface along
        ``-direction``. The ball is thus FULLY OUTSIDE the base body,
        free to rotate without clipping the base body's outer surface.

      - Stem cylinder: starts at ``attach`` (base body surface, with
        0.2mm overshoot INTO the base body for clean boolean union),
        extends along ``-direction`` for ``stem_length + 2*overshoot``
        so its far end pierces the ball surface by 0.2mm (avoids
        coincident-face boolean union failure).

    The previous implementation placed the ball at ``attach_point``
    (sphere center on the base body surface) -- the ball's -direction
    hemisphere was embedded inside the base body, leaving only the
    +direction hemisphere protruding (typically 6mm of ball radius
    visible). This is insufficient for a real ball joint: the moving
    part cannot rotate freely because the base body clips the ball.
    """
    from build123d import Align, Cylinder, Pos, Rotation, Sphere

    params = dict(feature.params)
    sphere_radius = float(params["sphere_radius"])
    stem_radius = float(params.get("stem_radius", 0.0))
    stem_length = float(params.get("stem_length", 0.0))

    attach = feature.attachment.attach_point_mm
    direction = feature.attachment.direction
    # Sphere has no axis (symmetric). Use _PROTRUSION_ROTATIONS just for
    # the protrusion unit vector (the rotation isn't applied to the
    # sphere — sphere is rotation-invariant). `protrusion` is the unit
    # vector along `direction` (e.g. direction="-x" -> protrusion=(-1,0,0)).
    _rot_rpy_unused, protrusion = _PROTRUSION_ROTATIONS.get(direction, ((0, 0, 0), (0, 0, 1)))
    # Ball offset direction: OPPOSITE of `direction` (the ball is on
    # the -direction side of attach_point). E.g. direction="-x" means
    # the ball is in the +x direction from attach_point.
    anti = (-protrusion[0], -protrusion[1], -protrusion[2])

    # Ball center: stem_length + sphere_radius from attach along -direction.
    # Ball surface's nearest point to base body is at attach + anti*stem_length
    # (= stem_length from the base body surface along -direction).
    ball_center = (
        attach[0] + anti[0] * (stem_length + sphere_radius),
        attach[1] + anti[1] * (stem_length + sphere_radius),
        attach[2] + anti[2] * (stem_length + sphere_radius),
    )
    sphere = Pos(ball_center[0], ball_center[1], ball_center[2]) * Sphere(radius=sphere_radius)

    body = base + sphere

    if stem_length > 0 and stem_radius > 0:
        # Stem direction string: opposite of `direction` (stem extends
        # from attach along -direction to reach the ball). Lookup in
        # _Z_AXIS_ROTATIONS which maps direction string -> rotation that
        # takes +Z to that direction string.
        _OPPOSITE = {
            "+x": "-x", "-x": "+x",
            "+y": "-y", "-y": "+y",
            "+z": "-z", "-z": "+z",
        }
        stem_dir = _OPPOSITE.get(direction, direction)
        # stem_base: 0.2mm INTO the base body (along +protrusion, opposite
        # of stem_dir) for clean boolean union with the base.
        stem_base = (
            attach[0] + protrusion[0] * _ADDITIVE_OVERSHOOT_MM,
            attach[1] + protrusion[1] * _ADDITIVE_OVERSHOOT_MM,
            attach[2] + protrusion[2] * _ADDITIVE_OVERSHOOT_MM,
        )
        # Cylinder height: stem_length + 2*overshoot (overshoot on both
        # ends -- into the base body AND into the ball surface -- so
        # both boolean unions succeed cleanly).
        stem_height = stem_length + 2.0 * _ADDITIVE_OVERSHOOT_MM
        stem_rot_rpy = _Z_AXIS_ROTATIONS.get(stem_dir, (0.0, 0.0, 0.0))
        stem = (
            Pos(stem_base[0], stem_base[1], stem_base[2])
            * Rotation(stem_rot_rpy[0], stem_rot_rpy[1], stem_rot_rpy[2])
            * Cylinder(
                radius=stem_radius, height=stem_height,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
            )
        )
        body = body + stem

    return body


def _op_knuckle_ear(base: Any, feature: Feature) -> Any:
    """Attach a horizontal-axis cylinder ear with through-bore.

    The ear is a cylinder oriented along `direction` (the ear's axis
    is `direction`). The bore goes through the ear along the same axis.
    Used for knuckle hinges / cross-bores where the LLM struggles with
    horizontal cylinders.
    """
    from build123d import Align, Cylinder, Pos, Rotation

    params = dict(feature.params)
    ear_radius = float(params["ear_radius"])
    ear_length = float(params["ear_length"])
    bore_radius = float(params["bore_radius"])

    attach = feature.attachment.attach_point_mm
    direction = feature.attachment.direction
    # Z-axis feature: ear is a cylinder along +Z by default; rotate via
    # _Z_AXIS_ROTATIONS to align with protrusion direction. The protrusion
    # vector (for overshoot direction) is the second tuple element.
    ear_rot_rpy = _Z_AXIS_ROTATIONS.get(direction, (0.0, 0.0, 0.0))
    _rot_rpy_unused, protrusion = _PROTRUSION_ROTATIONS.get(direction, ((0, 0, 0), (1, 0, 0)))

    # The ear is a cylinder along the protrusion direction (axis = direction)
    # centered at attach_point, length = ear_length.
    # Bore overshoots each end by 1mm.
    ear = Cylinder(
        radius=ear_radius, height=ear_length,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    bore = Cylinder(
        radius=bore_radius, height=ear_length + 2.0,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    ear_with_bore = ear - bore

    # 0.2mm overshoot along -direction
    ox = -protrusion[0] * _ADDITIVE_OVERSHOOT_MM
    oy = -protrusion[1] * _ADDITIVE_OVERSHOOT_MM
    oz = -protrusion[2] * _ADDITIVE_OVERSHOOT_MM
    placed = (
        Pos(attach[0] + ox, attach[1] + oy, attach[2] + oz)
        * Rotation(ear_rot_rpy[0], ear_rot_rpy[1], ear_rot_rpy[2])
        * ear_with_bore
    )
    return base + placed


# ---------------------------------------------------------------------------
# Registry + build-only fallback (for disjoint-solid last-ditch)
# ---------------------------------------------------------------------------

FEATURE_OPERATORS: dict[str, Callable[[Any, Feature], Any]] = {
    "clevis_fork": _op_clevis_fork,
    "clevis_tongue": _op_clevis_tongue,
    "through_bore": _op_through_bore,
    "ball_cavity": _op_ball_cavity,
    "ball_stem": _op_ball_stem,
    "knuckle_ear": _op_knuckle_ear,
}


def _build_only_clevis_fork(feature: Feature) -> Any:
    """Build the feature geometry alone (no base) for the disjoint fallback."""
    params = dict(feature.params)
    bar_thickness = float(params.get("bar_thickness", params["tongue_thickness"] + 2.2))
    fork = _v3_fork_local(
        float(params["ear_length"]), float(params["ear_width"]),
        float(params["tongue_thickness"]), float(params["bore_radius"]),
        bar_thickness,
        float(params.get("clearance_side", 0.1)),
    )
    attach = feature.attachment.attach_point_mm
    pin_axis = _clevis_pin_axis(params)
    placement, _ = _clevis_placement_and_bore_cut(
        attach, feature.attachment.direction, pin_axis, bar_thickness,
        float(params["bore_radius"]), "clevis_fork"
    )
    return placement(fork)


def _build_only_clevis_tongue(feature: Feature) -> Any:
    params = dict(feature.params)
    bar_thickness = float(params.get("bar_thickness", params["tongue_thickness"] + 2 * 0.1 + 2.0))
    tongue = _v3_tongue_local(
        float(params["ear_length"]), float(params["ear_width"]),
        float(params["tongue_thickness"]), float(params["bore_radius"]),
        bar_thickness,
    )
    attach = feature.attachment.attach_point_mm
    pin_axis = _clevis_pin_axis(params)
    placement, _ = _clevis_placement_and_bore_cut(
        attach, feature.attachment.direction, pin_axis, bar_thickness,
        float(params["bore_radius"]), "clevis_tongue"
    )
    return placement(tongue)


def _build_only_through_bore(feature: Feature) -> Any:
    from build123d import Align, Cylinder, Pos, Rotation
    params = dict(feature.params)
    radius = float(params["radius"])
    height = float(params.get("height", 10.0)) + 2.0
    direction = feature.attachment.direction
    # Z-axis feature (Cylinder default +Z): use _Z_AXIS_ROTATIONS to match
    # _op_through_bore. Previous code used _PROTRUSION_ROTATIONS (for +X
    # protrusions), leaving the bore along +Z instead of `direction`.
    rot_rpy = _Z_AXIS_ROTATIONS.get(direction, (0.0, 0.0, 0.0))
    attach = feature.attachment.attach_point_mm
    bore = Cylinder(radius=radius, height=height, align=(Align.CENTER, Align.CENTER, Align.CENTER))
    return Pos(attach[0], attach[1], attach[2]) * Rotation(*rot_rpy) * bore


def _build_only_ball_cavity(feature: Feature) -> Any:
    # Cavity is subtractive; "build only" doesn't really make sense --
    # return an empty sphere that won't fuse with anything.
    from build123d import Pos, Sphere
    params = dict(feature.params)
    attach = feature.attachment.attach_point_mm
    return Pos(attach[0], attach[1], attach[2]) * Sphere(radius=float(params["sphere_radius"]))


def _build_only_ball_stem(feature: Feature) -> Any:
    from build123d import Align, Cylinder, Pos, Rotation, Sphere
    params = dict(feature.params)
    sphere_radius = float(params["sphere_radius"])
    stem_radius = float(params.get("stem_radius", 0.0))
    stem_length = float(params.get("stem_length", 0.0))
    attach = feature.attachment.attach_point_mm
    direction = feature.attachment.direction
    # Mirror _op_ball_stem: protrusion from _PROTRUSION_ROTATIONS (positions
    # stem_base on the sphere surface); stem_rot_rpy from _Z_AXIS_ROTATIONS
    # (cylinder default axis +Z, rotate to `direction`). The previous code
    # unpacked both from _PROTRUSION_ROTATIONS, so for direction=+x the
    # rot_rpy was (0,0,0) -> stem cylinder stayed along +Z instead of +X.
    _rot_rpy_unused, protrusion = _PROTRUSION_ROTATIONS.get(direction, ((0, 0, 0), (0, 0, 1)))
    stem_rot_rpy = _Z_AXIS_ROTATIONS.get(direction, (0.0, 0.0, 0.0))
    sphere = Pos(attach[0], attach[1], attach[2]) * Sphere(radius=sphere_radius)
    if stem_length > 0 and stem_radius > 0:
        stem_base = (
            attach[0] + protrusion[0] * sphere_radius,
            attach[1] + protrusion[1] * sphere_radius,
            attach[2] + protrusion[2] * sphere_radius,
        )
        stem = (
            Pos(*stem_base) * Rotation(*stem_rot_rpy)
            * Cylinder(radius=stem_radius, height=stem_length, align=(Align.CENTER, Align.CENTER, Align.MIN))
        )
        return sphere + stem
    return sphere


def _build_only_knuckle_ear(feature: Feature) -> Any:
    from build123d import Align, Cylinder, Pos, Rotation
    params = dict(feature.params)
    ear_radius = float(params["ear_radius"])
    ear_length = float(params["ear_length"])
    bore_radius = float(params["bore_radius"])
    ear = Cylinder(radius=ear_radius, height=ear_length, align=(Align.CENTER, Align.CENTER, Align.CENTER))
    bore = Cylinder(radius=bore_radius, height=ear_length + 2.0, align=(Align.CENTER, Align.CENTER, Align.CENTER))
    ear_with_bore = ear - bore
    attach = feature.attachment.attach_point_mm
    # Z-axis feature (Cylinder default +Z): use _Z_AXIS_ROTATIONS to match
    # _op_knuckle_ear. Previous code used _PROTRUSION_ROTATIONS (for +X
    # protrusions), leaving the ear along +Z instead of `direction`.
    rot_rpy = _Z_AXIS_ROTATIONS.get(feature.attachment.direction, (0.0, 0.0, 0.0))
    return Pos(attach[0], attach[1], attach[2]) * Rotation(*rot_rpy) * ear_with_bore


FEATURE_OPERATORS_BUILD_ONLY: dict[str, Callable[[Feature], Any]] = {
    "clevis_fork": _build_only_clevis_fork,
    "clevis_tongue": _build_only_clevis_tongue,
    "through_bore": _build_only_through_bore,
    "ball_cavity": _build_only_ball_cavity,
    "ball_stem": _build_only_ball_stem,
    "knuckle_ear": _build_only_knuckle_ear,
}
