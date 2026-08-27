"""Parametric builders for common mechanical structures.

These bypass the LLM-driven single-part pipeline for structures the LLM
struggles to generate correctly (e.g. horizontal-axis cylinders, knuckle
ears with through-bores). The Decomposer can specify
`PartSpec.builder = {"name": "knuckle_hinge_ear", "params": {...}}` and
the PartBuilder will call the builder directly instead of running the
Spec Planner -> Architect -> Coder -> Skill Loop.

Each builder returns a build123d Shape (Solid or Compound) with the part
positioned in its local coordinate system (the convention stated in the
part description). The caller (PartBuilder) exports the shape to STEP/STL.

Why this matters: qwen3.8-max reliably writes build123d for default-axis
features (vertical cylinders along Z, plates extruded along Z) but
struggles with non-default-axis features (horizontal cylinders along X,
like a knuckle ear with a horizontal bore). The builder pre-computes the
correct rotation so the geometry is always valid.
"""

from __future__ import annotations

from typing import Any


def knuckle_hinge_ear(
    plate_w: float,
    plate_h: float,
    plate_t: float,
    ear_radius: float,
    ear_length: float,
    bore_radius: float,
    ear_z: float,
    ear_at_plus_x: bool = True,
    **_: Any,
) -> "Compound":
    """A vertical rectangular plate with a horizontal cylindrical ear
    (knuckle) on one side, with a horizontal through-bore for a shaft.

    Local coordinate convention:
      - Plate occupies X=0..plate_w, Y=-plate_h/2..plate_h/2, Z=0..plate_t
        (bottom face at Z=0).
      - Ear (horizontal cylinder, axis along X, length=ear_length) is
        attached to the plate's +X face (if ear_at_plus_x=True) at
        X=plate_w..plate_w+ear_length, or the -X face (if False) at
        X=-ear_length..0. Ear axis midpoint at (ear_center_x, 0, ear_z).
      - Through-bore (axis along X, radius=bore_radius) passes through
        the ear (slightly overshooting each end for a clean boolean cut).

    The ear's Z extent is ear_z +/- ear_radius. Choose ear_radius and
    ear_z so this fits within (or only slightly exceeds) the plate's
    Z=0..plate_t range.
    """
    from build123d import (
        Align,
        Box,
        Cylinder,
        Mode,
        Pos,
        Rotation,
    )

    # Plate: Box with min-X, center-Y, min-Z alignment so the plate sits at
    # X=0..plate_w, Y=-plate_h/2..plate_h/2, Z=0..plate_t.
    plate = Box(
        plate_w, plate_h, plate_t,
        align=(Align.MIN, Align.CENTER, Align.MIN),
    )

    # Ear: horizontal cylinder along X. build123d's default Cylinder is
    # along Z; rotate 90deg around Y so its axis becomes +X. Center the
    # cylinder (Align.CENTER on all 3) so it spans +/- length/2 around the
    # position. Position the rotated cylinder so it sits flush against the
    # plate's +/-X face.
    if ear_at_plus_x:
        ear_center_x = plate_w + ear_length / 2.0
    else:
        ear_center_x = -ear_length / 2.0

    ear = Pos(ear_center_x, 0, ear_z) * (
        Rotation(0, 90, 0) * Cylinder(
            radius=ear_radius, height=ear_length,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
    )

    # Through-bore: same orientation as the ear, slightly longer for a
    # clean boolean cut (overshoot 1mm each side).
    bore_len = ear_length + 2.0
    bore = Pos(ear_center_x, 0, ear_z) * (
        Rotation(0, 90, 0) * Cylinder(
            radius=bore_radius, height=bore_len,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
    )

    # Combine: union plate + ear, then subtract bore. The algebraic API
    # (+, -) on Shapes is equivalent to boolean union/subtract.
    return plate + ear - bore


def shaft_with_arm(
    rod_radius: float,
    rod_length: float,
    arm_w: float,
    arm_h: float,
    arm_d: float,
    arm_offset_x: float = 0.0,
    arm_offset_y: float = 0.0,
    arm_offset_z: float = 0.0,
    **_: Any,
) -> "Compound":
    """A rod (cylinder along LOCAL Z) with a perpendicular rectangular arm
    attached at the rod's midpoint. The arm extends +X from the rod.

    **Local Z is the rod axis** because the revolute mate aligns the
    moving part's local Z with the rotation axis. So when this part is
    mated via revolute to a fixed bore whose axis is world X, the rod
    (along local Z) ends up along world X (the rotation axis), and the arm
    (along local X) ends up perpendicular to the rotation axis (visible
    rotation).

    Local coordinate convention:
      - Rod (cylinder, axis along LOCAL Z, length=rod_length) centered at
        the origin: X=-rod_radius..rod_radius, Y=-rod_radius..rod_radius,
        Z=-rod_length/2..rod_length/2. Rod axis at (0, 0, 0).
      - Arm (rectangular box, arm_h along X, arm_w along Y, arm_d along Z)
        positioned so its -X face touches the rod at X=arm_offset_x, and
        extends +X by arm_h. Centered at Y=arm_offset_y, Z=arm_offset_z.

    Without the arm, a pure cylindrical rod rotating around its own axis
    is visually a no-op (the bbox doesn't change). The arm makes the
    rotation visible and acts as a joint lever.
    """
    from build123d import (
        Align,
        Box,
        Cylinder,
        Pos,
    )

    # Rod: cylinder along LOCAL Z (build123d default -- no rotation needed).
    # Centered at origin. This is critical: the revolute mate aligns the
    # moving part's local Z with the rotation axis, so the rod must be
    # along local Z to end up along the rotation axis after mating.
    rod = Cylinder(
        radius=rod_radius, height=rod_length,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )

    # Arm: rectangular box, extends +X from the rod. Box dimensions are
    # (arm_h along X, arm_w along Y, arm_d along Z). Position so its -X
    # face is at X=arm_offset_x (touching the rod by default), centered on
    # Y=arm_offset_y, Z=arm_offset_z.
    arm_center = (
        arm_offset_x + arm_h / 2.0,
        arm_offset_y,
        arm_offset_z,
    )
    arm = Pos(arm_center) * Box(
        arm_h, arm_w, arm_d,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )

    return rod + arm


def pivot_post(
    plate_w: float,
    plate_d: float,
    plate_t: float,
    post_radius: float,
    post_height: float,
    post_x: float = 0.0,
    post_y: float = 0.0,
    **_: Any,
) -> "Compound":
    """A flat plate with a vertical cylindrical post integral to the top
    face. The post is the revolute pivot for a mating arm with a through-hole.

    Local coordinate convention:
      - Plate occupies X=-plate_w/2..plate_w/2 (centered on X=0),
        Y=-plate_d/2..plate_d/2 (centered on Y=0), Z=0..plate_t (bottom
        face at Z=0).
      - Post (vertical cylinder, axis along Z, radius=post_radius,
        height=post_height) integral to the plate top face, centered at
        (post_x, post_y). Occupies Z=plate_t..plate_t+post_height.
    """
    from build123d import (
        Align,
        Box,
        Cylinder,
        Pos,
    )

    plate = Box(
        plate_w, plate_d, plate_t,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    post = Pos(post_x, post_y, plate_t) * Cylinder(
        radius=post_radius, height=post_height,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    return plate + post


def link_bar(
    length: float,
    width: float,
    thickness: float,
    bore_radius: float,
    hole_offset: float = 0.0,
    **_: Any,
) -> "Compound":
    """A rectangular bar with vertical through-holes at both ends. The
    bar is along LOCAL X; the holes are along LOCAL Z (vertical), so the
    revolute mate can align local Z with the rotation axis (the pin).

    Use this for finger links, crank arms, linkage bars -- any part that
    connects to two other parts via pin joints at its ends.

    Local coordinate convention:
      - Bar (Box) along LOCAL X: X=-length/2..length/2, Y=-width/2..width/2,
        Z=0..thickness (bottom face at Z=0).
      - Two through-holes (vertical, axis along Z, radius=bore_radius) at
        X=-length/2+hole_offset and X=+length/2-hole_offset, Y=0. Holes
        pass through the full thickness (slightly overshooting for clean
        boolean cut). Hole midpoints at (±hole_x, 0, thickness/2).

    If hole_offset=0 (default), holes are at the bar's ends (±length/2).
    Set hole_offset > 0 to move holes inward (e.g., for a fillet margin).
    """
    from build123d import Align, Box, Cylinder, Pos

    bar = Box(
        length, width, thickness,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    hole_x = length / 2.0 - hole_offset
    bore_h = thickness + 2.0  # overshoot 1mm each side
    hole1 = Pos(-hole_x, 0, thickness / 2.0) * Cylinder(
        radius=bore_radius, height=bore_h,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    hole2 = Pos(hole_x, 0, thickness / 2.0) * Cylinder(
        radius=bore_radius, height=bore_h,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    return bar - hole1 - hole2


def fork_end(
    bar_length: float,
    bar_width: float,
    bar_thickness: float,
    ear_length: float,
    ear_spacing: float,
    bore_radius: float,
    bore_axis: str = "z",
    **_: Any,
) -> "Compound":
    """A bar with a fork (two parallel ears) at the +X end, each with a
    coaxial through-bore. The fork receives a single mating ear (from
    another part) between the two ears, forming a knuckle joint.

    Use this for rod ends, clevises, and any forked linkage connection.

    Local coordinate convention:
      - Bar (Box) along LOCAL X: X=-bar_length/2..bar_length/2,
        Y=-bar_width/2..bar_width/2, Z=0..bar_thickness.
      - Two ears (rectangular blocks) at the +X end, separated by a gap
        of width ear_spacing along Y. Each ear: X=bar_length/2..bar_length/2
        +ear_length, Y=ear_spacing/2..ear_spacing/2+ear_width, Z=0..bar_thickness
        (or the mirror for the other ear).
      - Through-bore (cylinder, radius=bore_radius) through BOTH ears,
        coaxial. bore_axis: "z" (vertical bore, default -- for a horizontal
        pin layout) or "y" (horizontal bore along Y -- for a vertical pin
        layout). Bore midpoint at (bar_length/2 + ear_length/2, 0,
        bar_thickness/2).

    The gap between the ears (width=ear_spacing) receives the mating
    single ear (use `knuckle_hinge_ear` with appropriate thickness).
    """
    from build123d import Align, Box, Cylinder, Pos, Rotation

    bar = Box(
        bar_length, bar_width, bar_thickness,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    ear_w = (bar_width - ear_spacing) / 2.0
    ear_x = bar_length / 2.0 + ear_length / 2.0
    ear1 = Pos(ear_x, ear_spacing/2.0 + ear_w/2.0, bar_thickness/2.0) * Box(
        ear_length, ear_w, bar_thickness,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    ear2 = Pos(ear_x, -(ear_spacing/2.0 + ear_w/2.0), bar_thickness/2.0) * Box(
        ear_length, ear_w, bar_thickness,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    # Through-bore through both ears. For bore_axis="z" (vertical), use
    # default Cylinder orientation. For bore_axis="y" (horizontal), rotate
    # 90° around X (turns +Z -> +Y).
    if bore_axis == "y":
        rot = Rotation(90, 0, 0)
    else:  # "z"
        rot = Rotation(0, 0, 0)
    bore_h = bar_thickness + 2.0  # overshoot; passes through both ears + gap
    bore = Pos(ear_x, 0, bar_thickness/2.0) * (rot * Cylinder(
        radius=bore_radius, height=bore_h,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    ))
    return bar + ear1 + ear2 - bore


def mounting_plate(
    width: float,
    depth: float,
    thickness: float,
    hole_radius: float = 0.0,
    hole_dx: float = 0.0,
    hole_dy: float = 0.0,
    central_hole_radius: float = 0.0,
    **_: Any,
) -> "Compound":
    """A flat plate with optional mounting holes. Default pattern: 4
    corner holes at (±hole_dx, ±hole_dy) (skipped when hole_radius=0) plus
    an optional central hole.

    Use this for base plates, palm plates, mounting platforms, fixture
    plates -- any flat plate that other parts mount onto.

    Local coordinate convention:
      - Plate occupies X=-width/2..width/2, Y=-depth/2..depth/2,
        Z=0..thickness (bottom face at Z=0).
      - Optional 4 corner through-holes at (±hole_dx, ±hole_dy), axis
        along Z, radius=hole_radius (only when hole_radius > 0). Hole
        midpoints at Z=thickness/2.
      - Optional central through-hole (central_hole_radius > 0) at (0, 0).
    """
    from build123d import Align, Box, Cylinder, Pos

    plate = Box(
        width, depth, thickness,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    bore_h = thickness + 2.0
    holes = []
    # Skip corner holes when hole_radius=0 (matches `lid` API). Without
    # this guard, Cylinder(radius=0, ...) raises Standard_ConstructionError.
    if hole_radius > 0:
        for sx in (-1, 1):
            for sy in (-1, 1):
                holes.append(Pos(sx*hole_dx, sy*hole_dy, thickness/2.0) * Cylinder(
                    radius=hole_radius, height=bore_h,
                    align=(Align.CENTER, Align.CENTER, Align.CENTER),
                ))
    if central_hole_radius > 0:
        holes.append(Pos(0, 0, thickness/2.0) * Cylinder(
            radius=central_hole_radius, height=bore_h,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        ))
    result = plate
    for h in holes:
        result = result - h
    return result


def hollow_box(
    outer_w: float,
    outer_d: float,
    outer_h: float,
    wall_t: float,
    floor_t: float = 0.0,
    **_: Any,
) -> "Compound":
    """A hollow open-top box (enclosure base). Walls on 4 sides + floor
    (no top). Use with `lid` to close the top.

    Use this for electronics enclosures, gearbox housings, servo bays --
    any open-top container.

    Local coordinate convention:
      - Outer bbox: X=-outer_w/2..outer_w/2, Y=-outer_d/2..outer_d/2,
        Z=0..outer_h (bottom face at Z=0).
      - Walls thickness=wall_t on all 4 sides.
      - Floor thickness=floor_t (if 0, defaults to wall_t). Interior is
        hollow from Z=floor_t to Z=outer_h (open top).
    """
    from build123d import Align, Box, Pos

    if floor_t <= 0:
        floor_t = wall_t
    outer = Box(
        outer_w, outer_d, outer_h,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    inner_w = outer_w - 2.0 * wall_t
    inner_d = outer_d - 2.0 * wall_t
    inner_h = outer_h - floor_t + 2.0  # overshoot through the open top
    inner = Pos(0, 0, floor_t) * Box(
        inner_w, inner_d, inner_h,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    return outer - inner


def lid(
    width: float,
    depth: float,
    thickness: float,
    hole_radius: float = 0.0,
    hole_dx: float = 0.0,
    hole_dy: float = 0.0,
    **_: Any,
) -> "Compound":
    """A flat rectangular lid (for closing a hollow_box). Optional 4
    corner mounting holes.

    Use this for enclosure lids, cover plates, access panels.

    Local coordinate convention:
      - Lid occupies X=-width/2..width/2, Y=-depth/2..depth/2,
        Z=0..thickness (bottom face at Z=0, seats on the box top rim).
      - Optional 4 corner through-holes at (±hole_dx, ±hole_dy).
    """
    from build123d import Align, Box, Cylinder, Pos

    lid_shape = Box(
        width, depth, thickness,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    if hole_radius > 0:
        bore_h = thickness + 2.0
        for sx in (-1, 1):
            for sy in (-1, 1):
                hole = Pos(sx*hole_dx, sy*hole_dy, thickness/2.0) * Cylinder(
                    radius=hole_radius, height=bore_h,
                    align=(Align.CENTER, Align.CENTER, Align.CENTER),
                )
                lid_shape = lid_shape - hole
    return lid_shape


def bracket_L(
    plate_w: float,
    plate_h: float,
    plate_t: float,
    hole_radius: float = 0.0,
    wall_holes: tuple = (),
    foot_holes: tuple = (),
    **_: Any,
) -> "Compound":
    """An L-shaped bracket: two perpendicular rectangular plates joined
    at a 90° corner along one edge.

    Use this for mounting brackets, servo brackets, sensor mounts, corner
    reinforcements.

    Simplified single-sheet assumption: ``plate_w`` is reused for three
    geometric roles that are physically equal on a bent sheet-metal bracket
    -- (a) the wall's Y width, (b) the foot's X length (wall bend depth),
    (c) the foot's Y width. The wall height (``plate_h``) and both plate
    thicknesses (``plate_t``) are independent. If your design needs the
    wall width, foot length, and foot depth to differ (e.g. wall_width=50,
    foot_length=80), do NOT use this builder -- compose a wall ``Box``
    and a foot ``Box`` yourself and union them, then add holes via
    cadpy face selectors.

    Local coordinate convention:
      - Vertical plate (the "wall"): X=0..plate_t, Y=-plate_w/2..plate_w/2,
        Z=0..plate_h (bottom at Z=0).
      - Horizontal plate (the "foot"): X=0..plate_w, Y=-plate_w/2..plate_w/2,
        Z=0..plate_t (extends +X from the wall's bottom).
      - The two plates share the edge at X=0..plate_t, Z=0..plate_t.
      - Optional wall through-holes (axis along Y) at ``(x, z)`` positions
        on the wall plate -- bore centred at ``(x, 0, z)``.
      - Optional foot through-holes (axis along Z) at ``(x, y)`` positions
        on the foot plate -- bore centred at ``(x, y, 0)``.

    wall_holes: list of ``(x, z)`` 2-tuples; bore along Y at ``(x, 0, z)``.
    foot_holes: list of ``(x, y)`` 2-tuples; bore along Z at ``(x, y, 0)``.
    """
    from build123d import Align, Box, Cylinder, Pos, Rotation

    wall = Box(
        plate_t, plate_w, plate_h,
        align=(Align.MIN, Align.CENTER, Align.MIN),
    )
    foot = Box(
        plate_w, plate_w, plate_t,
        align=(Align.MIN, Align.CENTER, Align.MIN),
    )
    bracket = wall + foot
    for x, z in wall_holes:
        bore_h = plate_w + 2.0
        hole = Pos(x, 0, z) * (Rotation(90, 0, 0) * Cylinder(
            radius=hole_radius, height=bore_h,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        ))
        bracket = bracket - hole
    for x, y in foot_holes:
        bore_h = plate_t + 2.0
        hole = Pos(x, y, plate_t/2.0) * Cylinder(
            radius=hole_radius, height=bore_h,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
        bracket = bracket - hole
    return bracket


def standoff(
    radius: float,
    height: float,
    bore_radius: float = 0.0,
    **_: Any,
) -> "Compound":
    """A cylindrical standoff (spacer). Optional coaxial through-bore.

    Use this for spacers between plates, threaded standoffs, hex-standoff
    substitutes (simplified as round), bearing races.

    Local coordinate convention:
      - Cylinder (axis along Z, radius=radius, height=height) centered
        at origin in XY, bottom at Z=0: X=-radius..radius, Y=-radius..radius,
        Z=0..height.
      - Optional coaxial through-bore (axis along Z, radius=bore_radius).
    """
    from build123d import Align, Cylinder, Pos

    body = Cylinder(
        radius=radius, height=height,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    if bore_radius > 0:
        # Position bore at body's Z center (height/2), overshoot each end
        # by 1mm so the boolean cut is clean (no coincident face failures).
        bore = Pos(0, 0, height/2.0) * Cylinder(
            radius=bore_radius, height=height + 2.0,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
        return body - bore
    return body


def bushing(
    outer_radius: float,
    inner_radius: float,
    length: float,
    **_: Any,
) -> "Compound":
    """A sleeve bushing: a short cylinder with a coaxial through-bore.
    The bore axis is along LOCAL Z (so revolute mates can align it).

    Use this for plain bearings, spacer bushings, bearing races. The
    bushing is typically pressed into a housing (use `knuckle_hinge_ear`
    or `hollow_box` as the housing).

    Local coordinate convention:
      - Outer cylinder: axis along LOCAL Z, radius=outer_radius,
        length=length. Centered at origin in XY, bottom at Z=0:
        X=-outer_radius..outer_radius, Y=-outer_radius..outer_radius,
        Z=0..length.
      - Inner bore: axis along Z, radius=inner_radius, through-bore
        (overshooting each end).
    """
    from build123d import Align, Cylinder, Pos

    outer = Cylinder(
        radius=outer_radius, height=length,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    inner = Pos(0, 0, length/2.0) * Cylinder(
        radius=inner_radius, height=length + 2.0,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    return outer - inner


def gusset(
    side_a: float,
    side_b: float,
    thickness: float,
    **_: Any,
) -> "Compound":
    """A triangular gusset (right triangle) for reinforcing a corner.
    The right angle is at the origin; the two legs extend along +X and +Z.

    Use this for corner reinforcement in frames, brackets, and welded
    structures.

    Local coordinate convention:
      - Triangle in the XZ plane: vertices at (0,0,0), (side_a,0,0),
        (0,0,side_b). Extruded along Y by thickness:
        Y=-thickness/2..thickness/2.
    """
    from build123d import Polygon, extrude, Plane

    # Build a right-triangle Face in the XZ plane using algebraic API:
    # `Plane.XZ * Polygon([...])` returns a Face in the XZ plane.
    # `extrude(profile, amount=thickness)` extrudes along the plane's
    # normal (+Y), giving a triangular prism of thickness along Y.
    profile = Plane.XZ * Polygon([(0, 0), (side_a, 0), (0, side_b)])
    return extrude(profile, amount=thickness)


# Stem direction -> (Rotation rpy deg, surface offset from sphere center).
# Cylinder default axis is +Z with align=(CENTER, CENTER, MIN) so its base
# sits at the origin; the rotation reorients the +Z axis to the target
# direction, then Pos translates the base to the sphere surface point.
_STEM_DIRECTIONS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "+z": ((0.0, 0.0, 0.0),         (0.0, 0.0, 1.0)),
    "-z": ((180.0, 0.0, 0.0),       (0.0, 0.0, -1.0)),
    "+x": ((0.0, 90.0, 0.0),        (1.0, 0.0, 0.0)),
    "-x": ((0.0, -90.0, 0.0),       (-1.0, 0.0, 0.0)),
    "+y": ((-90.0, 0.0, 0.0),       (0.0, 1.0, 0.0)),
    "-y": ((90.0, 0.0, 0.0),        (0.0, -1.0, 0.0)),
}


def ball_joint_socket(
    role: str,
    sphere_radius: float,
    sphere_center_mm: list[float] | None = None,
    socket_housing_w: float = 0.0,
    socket_housing_d: float = 0.0,
    socket_housing_h: float = 0.0,
    socket_wall_t: float = 2.0,
    socket_opening_radius: float = 0.0,
    socket_opening_height_mm: float = 0.0,
    ball_stem_radius: float = 0.0,
    ball_stem_length: float = 0.0,
    ball_stem_direction: str = "+z",
    **_: Any,
) -> "Compound":
    """Generate one piece of a 2-DOF ball-and-socket joint.

    ``role="socket"``: box housing with a concave spherical cavity.
      - Housing: ``Box(socket_housing_w, _d, _h)`` centered at XY origin,
        bottom face at Z=0. Defaults to ``(2*sphere_radius + 2*wall_t)^3``
        if any dim is 0.
      - Cavity: ``Sphere(sphere_radius)`` at ``sphere_center_mm``,
        subtracted from the housing.
      - Optional opening: ``Cylinder(socket_opening_radius, ...)`` on
        +Z face so the ball's stem passes through. Subtracted too.

    ``role="ball"``: solid sphere, optionally on a stem.
      - Sphere at ``sphere_center_mm``.
      - Optional stem: ``Cylinder(ball_stem_radius, ball_stem_length)``
        attached to the sphere on the ``ball_stem_direction`` face.

    Both pieces share ``sphere_radius`` + ``sphere_center_mm`` conventions
    so the codegen aligns the socket cavity center with the ball center.
    For a 2-DOF joint, the ball's ``sphere_radius`` should be slightly
    smaller than the socket's cavity radius (~0.2 mm clearance); the
    Decomposer specifies both radii separately on each PartSpec.

    Local coordinate convention:
      - Default sphere center: ``(0, 0, sphere_radius)`` -- sphere sits
        with its south pole at Z=0 (matches the socket housing's bottom).
      - Socket housing: X=-w/2..+w/2, Y=-d/2..+d/2, Z=0..h.
    """
    from build123d import Align, Box, Cylinder, Pos, Rotation, Sphere

    if sphere_center_mm is None:
        cx, cy, cz = 0.0, 0.0, float(sphere_radius)
    else:
        if len(sphere_center_mm) != 3:
            raise ValueError(
                f"sphere_center_mm must have 3 elements [x, y, z], "
                f"got {len(sphere_center_mm)}"
            )
        cx, cy, cz = (float(v) for v in sphere_center_mm)

    if role == "socket":
        w = socket_housing_w or (2.0 * sphere_radius + 2.0 * socket_wall_t)
        d = socket_housing_d or (2.0 * sphere_radius + 2.0 * socket_wall_t)
        h = socket_housing_h or (2.0 * sphere_radius + 2.0 * socket_wall_t)

        housing = Box(
            w, d, h,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        )
        cavity = Pos(cx, cy, cz) * Sphere(radius=sphere_radius)
        body = housing - cavity

        if socket_opening_radius > 0:
            opening_h = socket_opening_height_mm or h
            # Cylinder along Z, base at the top of the housing, extends
            # downward through the cavity roof. align=MAX puts the top
            # face at the position Z.
            opening = Pos(cx, cy, h) * Cylinder(
                radius=socket_opening_radius, height=opening_h + 1.0,
                align=(Align.CENTER, Align.CENTER, Align.MAX),
            )
            body = body - opening

        return body

    if role == "ball":
        body = Pos(cx, cy, cz) * Sphere(radius=sphere_radius)

        if ball_stem_length > 0 and ball_stem_radius > 0:
            orientation = _STEM_DIRECTIONS.get(ball_stem_direction)
            if orientation is None:
                raise ValueError(
                    f"ball_stem_direction {ball_stem_direction!r} not in "
                    f"{sorted(_STEM_DIRECTIONS)}"
                )
            (rpy_deg, dir_offset) = orientation
            ox = dir_offset[0] * sphere_radius
            oy = dir_offset[1] * sphere_radius
            oz = dir_offset[2] * sphere_radius
            stem = (
                Pos(cx + ox, cy + oy, cz + oz)
                * Rotation(rpy_deg[0], rpy_deg[1], rpy_deg[2])
                * Cylinder(
                    radius=ball_stem_radius, height=ball_stem_length,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                )
            )
            body = body + stem

        return body

    raise ValueError(
        f"ball_joint_socket: role must be 'socket' or 'ball', got {role!r}"
    )


def _clevis_validate(
    bar_thickness: float,
    bore_radius: float,
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    clearance_side: float,
    bar_width: float | None = None,
) -> None:
    """Validate clevis joint parameters; raise ValueError with concrete
    numbers on violation. Centralised so clevis_link and clevis_base_with_fork
    enforce the same constraints."""
    R_tip = ear_width / 2.0
    fork_gap_z = tongue_thickness + 2.0 * clearance_side
    ear_z_extent = (bar_thickness - fork_gap_z) / 2.0
    min_wall_z = 0.5

    if tongue_thickness >= bar_thickness - 2.0 * clearance_side - min_wall_z:
        raise ValueError(
            f"clevis: tongue_thickness {tongue_thickness} too large for "
            f"bar_thickness {bar_thickness} (fork_gap_z={fork_gap_z}, "
            f"each ear Z extent={ear_z_extent} < min {min_wall_z}mm)"
        )
    if ear_length < ear_width + clearance_side:
        # Geometry: root-to-bore distance = ear_length - R_tip; net rotation
        # radius clearance = (ear_length - R_tip) - R_tip = ear_length - ear_width.
        # Constraint equivalent to: net rotation clearance >= clearance_side.
        # Conservative for full-semicircle tips (actual safe angle >> +/-30deg
        # sweep), but finger joints may look elongated -- future tightening.
        raise ValueError(
            f"clevis: ear_length {ear_length} < ear_width {ear_width} + "
            f"clearance_side {clearance_side} (net rotation radius "
            f"clearance = ear_length - ear_width = "
            f"{ear_length - ear_width} < {clearance_side}; finger would jam)"
        )
    if ear_length <= R_tip:
        raise ValueError(
            f"clevis: ear_length {ear_length} <= R_tip {R_tip} "
            f"(rectangular attachment length <= 0)"
        )
    if ear_width < 2.0 * (bore_radius + 1.0):
        raise ValueError(
            f"clevis: ear_width {ear_width} < 2*(bore_radius {bore_radius} "
            f"+ 1.0mm wall) = {2.0 * (bore_radius + 1.0)} "
            f"(bore D={2.0 * bore_radius} would cut ear in half)"
        )
    if bar_width is not None and ear_width > bar_width:
        raise ValueError(
            f"clevis: ear_width {ear_width} > bar_width {bar_width} "
            f"(ear cannot be wider than the bar)"
        )


def _fork_local(
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    bore_radius: float,
    bar_thickness: float,
    clearance_side: float = 0.1,
    overshoot_mm: float = 0.0,
) -> "Compound":
    """Build a clevis fork in LOCAL coords with bore at the origin.

    The fork consists of two Z-separated ears (upper + lower) with a
    Z-direction slot between them. Bore center at (0, 0, bar_thickness/2).
    Ear bodies extend in -X direction (length = ear_length - R_tip +
    overshoot_mm) so the body is "behind" the bore (toward the base body
    when the fork protrudes +X). Ear tips are cylinders at origin (radius R_tip).

    The bore overshoots each Z end by 1mm to preserve the cylindrical
    face topology during boolean union with the base body (see
    feature_operators §2h "Boolean operation order"). Self-contained
    Compound -- the v3 clevis_fork operator positions + orients this
    local geometry at attach_point_mm + direction.

    ``overshoot_mm`` extends the body in -X by that amount (default 0).
    Used by v3 operators to push the body's back face 0.2mm INTO the
    base body, avoiding coincident-face boolean union (Bug 5 fix).
    Bore stays at origin (attach_point in the operator) -- the overshoot
    is in the body's protrusion, not in a whole-fork Pos offset. v2
    builders pass overshoot_mm=0 (default), preserving existing behavior.

    Local coord convention:
      - Bore center: (0, 0, bar_thickness/2)
      - Ear body: X from -(ear_length - R_tip + overshoot_mm) to 0
      - Upper ear Z: (bar_thickness + fork_gap_z)/2 to bar_thickness
      - Lower ear Z: 0 to (bar_thickness - fork_gap_z)/2
    """
    from build123d import Align, Box, Cylinder, Pos

    R_tip = ear_width / 2.0
    fork_gap_z = tongue_thickness + 2.0 * clearance_side
    upper_ear_z_extent = (bar_thickness - fork_gap_z) / 2.0
    lower_ear_z_extent = (bar_thickness - fork_gap_z) / 2.0
    upper_ear_z_min = (bar_thickness + fork_gap_z) / 2.0
    lower_ear_z_max = (bar_thickness - fork_gap_z) / 2.0

    body_len = ear_length - R_tip + overshoot_mm
    body_center_x = -body_len / 2.0
    upper_ear_z_center = (upper_ear_z_min + bar_thickness) / 2.0
    lower_ear_z_center = lower_ear_z_max / 2.0

    upper_body = Pos(body_center_x, 0, upper_ear_z_center) * Box(
        body_len, ear_width, upper_ear_z_extent,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    upper_tip = Pos(0, 0, upper_ear_z_center) * Cylinder(
        radius=R_tip, height=upper_ear_z_extent,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    lower_body = Pos(body_center_x, 0, lower_ear_z_center) * Box(
        body_len, ear_width, lower_ear_z_extent,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    lower_tip = Pos(0, 0, lower_ear_z_center) * Cylinder(
        radius=R_tip, height=lower_ear_z_extent,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    bore = Pos(0, 0, bar_thickness / 2.0) * Cylinder(
        radius=bore_radius, height=bar_thickness + 2.0,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    return upper_body + upper_tip + lower_body + lower_tip - bore


def _tongue_local(
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    bore_radius: float,
    bar_thickness: float,
    overshoot_mm: float = 0.0,
) -> "Compound":
    """Build a clevis tongue in LOCAL coords with bore at the origin.

    Single mid-Z slab with a semicircular tip. Bore center at
    (0, 0, bar_thickness/2). Body extends -X (length = ear_length - R_tip +
    overshoot_mm). Bore overshoots each Z end by 1mm. Self-contained
    Compound -- the v3 clevis_tongue operator positions + orients this
    local geometry. ``overshoot_mm`` extends body in -X (Bug 5 fix --
    keeps bore at attach_point, body pushes into base body).
    """
    from build123d import Align, Box, Cylinder, Pos

    R_tip = ear_width / 2.0
    body_len = ear_length - R_tip + overshoot_mm
    body_center_x = -body_len / 2.0
    tongue_z_center = bar_thickness / 2.0

    body = Pos(body_center_x, 0, tongue_z_center) * Box(
        body_len, ear_width, tongue_thickness,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    tip = Pos(0, 0, tongue_z_center) * Cylinder(
        radius=R_tip, height=tongue_thickness,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    bore = Pos(0, 0, bar_thickness / 2.0) * Cylinder(
        radius=bore_radius, height=bar_thickness + 2.0,
        align=(Align.CENTER, Align.CENTER, Align.CENTER),
    )
    return body + tip - bore


def _clevis_end_fork(
    sign: int,
    bar_length: float,
    bar_thickness: float,
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    bore_radius: float,
    clearance_side: float,
) -> "Compound":
    """Build a fork at the +X (sign=+1) or -X (sign=-1) end of a bar.

    Delegates to _fork_local (bore at origin, body extends -X) and
    positions the fork so:
      - Bore ends up at sign * (bar_length/2 + ear_length - R_tip)
      - Body extends from bore back toward the bar
    For sign=+1: body extends -X (toward bar at +bar_length/2). For
    sign=-1: the local fork is rotated 180° about Z so the body
    extends +X (toward bar at -bar_length/2).
    """
    from build123d import Pos, Rotation

    R_tip = ear_width / 2.0
    bore_x = sign * (bar_length / 2.0 + ear_length - R_tip)

    fork = _fork_local(
        ear_length, ear_width, tongue_thickness, bore_radius,
        bar_thickness, clearance_side,
    )
    if sign == -1:
        # Rotate 180° about Z: local -X body direction becomes global +X,
        # matching the bar's -X end (body extends back toward -bar_length/2).
        fork = Rotation(0, 0, 180) * fork
    return Pos(bore_x, 0, 0) * fork


def _clevis_end_tongue(
    sign: int,
    bar_length: float,
    bar_thickness: float,
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    bore_radius: float,
) -> "Compound":
    """Build a tongue at the +X (sign=+1) or -X (sign=-1) end of a bar.

    Delegates to _tongue_local (bore at origin, body extends -X) and
    positions similarly to _clevis_end_fork.
    """
    from build123d import Pos, Rotation

    R_tip = ear_width / 2.0
    bore_x = sign * (bar_length / 2.0 + ear_length - R_tip)

    tongue = _tongue_local(
        ear_length, ear_width, tongue_thickness, bore_radius, bar_thickness,
    )
    if sign == -1:
        tongue = Rotation(0, 0, 180) * tongue
    return Pos(bore_x, 0, 0) * tongue


def clevis_link(
    bar_length: float,
    bar_width: float,
    bar_thickness: float,
    bore_radius: float,
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    clearance_side: float = 0.1,
    minus_x_end: str = "plain",
    plus_x_end: str = "plain",
    **_: Any,
) -> "Compound":
    """A link bar with clevis tongue/fork interfaces at each end.

    Standard end-to-end clevis (tongue & groove) joint for planar revolute
    chains. The pin axis is along Z (vertical); the fork slot is along Z
    (thickness direction): Upper Ear takes the top Z band, Lower Ear takes
    the bottom Z band, and the empty Z slot between them receives the
    mating tongue (a mid-Z slab). This is critical: a Y-direction slot
    would make the tongue collide with the fork ears the instant it
    rotates about Z.

    Local coordinate convention:
      - Bar: X=-bar_length/2..+bar_length/2, Y=-bar_width/2..+bar_width/2,
        Z=0..bar_thickness (bottom at Z=0).
      - Ear/tongue features protrude from the +/-X faces of the bar by
        ear_length (total, including the semicircular tip).
      - Bore (axis along Z, radius=bore_radius) at the ear midpoint:
        X = +/-(bar_length/2 + ear_length - R_tip), Y=0, Z=bar_thickness/2.

    End configuration:
      - "tongue": a single mid-Z slab protruding from the bar end. The
        slab thickness is tongue_thickness (Z), centred on Z=bar_thickness/2.
        Mates with a "fork" end on another part.
      - "fork": two ears (upper Z + lower Z) with a Z slot between them.
        Receives a "tongue" end from another part.
      - "plain": no feature at that end (free end-effector tip).

    Chain topology for base -> lower_arm -> upper_arm:
      - base_plate: use `clevis_base_with_fork` (fork on a plate)
      - lower_arm: clevis_link(minus_x_end="tongue", plus_x_end="fork")
      - upper_arm: clevis_link(minus_x_end="tongue", plus_x_end="plain")

    Implicit kinematic mate: bore coaxiality + Z-slot clearance implement
    the revolute constraint; no physical Pin is modelled.
    """
    _clevis_validate(
        bar_thickness, bore_radius, ear_length, ear_width,
        tongue_thickness, clearance_side, bar_width=bar_width,
    )

    from build123d import Align, Box

    bar = Box(
        bar_length, bar_width, bar_thickness,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )

    for sign, end_type in [(+1, plus_x_end), (-1, minus_x_end)]:
        if end_type == "fork":
            bar = bar + _clevis_end_fork(
                sign, bar_length, bar_thickness, ear_length, ear_width,
                tongue_thickness, bore_radius, clearance_side,
            )
        elif end_type == "tongue":
            bar = bar + _clevis_end_tongue(
                sign, bar_length, bar_thickness, ear_length, ear_width,
                tongue_thickness, bore_radius,
            )
        # "plain": no feature

    return bar


def clevis_base_with_fork(
    plate_w: float,
    plate_d: float,
    plate_t: float,
    fork_x: float,
    fork_y: float,
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    bore_radius: float,
    clearance_side: float = 0.1,
    fork_direction: str = "+x",
    **_: Any,
) -> "Compound":
    """A flat plate with a clevis fork at a specified location (the fixed
    base of a planar revolute chain).

    The fork structure is identical to `clevis_link`'s +X fork (two ears
    in Z with a Z-direction slot), but placed at (fork_x, fork_y) on the
    plate and protruding in `fork_direction`.

    (fork_x, fork_y) is the BORE centre (absolute local coordinate on the
    plate), not the ear attachment point. The ear rectangular body extends
    from the bore backwards along -fork_direction to attach to the plate.
    The Mating Architect can thus use the bore coordinate directly without
    computing rotational offsets.

    Local coordinate convention:
      - Plate: X=-plate_w/2..+plate_w/2, Y=-plate_d/2..+plate_d/2,
        Z=0..plate_t (bottom at Z=0).
      - Fork bore at (fork_x, fork_y, plate_t/2).
      - Fork ears protrude from the plate in `fork_direction`:
        "+x" (default), "-x", "+y", or "-y".

    Use this for the fixed root of a single-chain kinematic chain
    (base_plate). For a multi-finger palm (dexterous hand, multi-chain
    base), use `clevis_palm` instead.

    Implicit kinematic mate: bore coaxiality + Z-slot clearance implement
    the revolute constraint; no physical Pin is modelled.
    """
    _clevis_validate(
        plate_t, bore_radius, ear_length, ear_width,
        tongue_thickness, clearance_side, bar_width=None,
    )

    from build123d import Align, Box, Location, Pos, Rotation

    plate = Box(
        plate_w, plate_d, plate_t,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )

    # Fork built at +X (sign=+1) around its own bar of length 0 (the ear
    # body attaches directly to the plate). We reuse _clevis_end_fork by
    # synthesising a "virtual bar end" at the bore location.
    # Implementation: build the +X fork in a local frame where the bar's
    # +X edge is at X=0 (so attach_x=0, bore_x = ear_length - R_tip),
    # then translate+rotate to (fork_x, fork_y) along fork_direction.
    R_tip = ear_width / 2.0
    # Build in local frame with attach at origin, ear extending +X.
    # Use a zero-length "bar" so _clevis_end_fork's attach_x = +0/2 = 0
    # and bore_x = +(0 + ear_length - R_tip) = ear_length - R_tip.
    fork_local = _clevis_end_fork(
        sign=+1,
        bar_length=0.0,
        bar_thickness=plate_t,
        ear_length=ear_length,
        ear_width=ear_width,
        tongue_thickness=tongue_thickness,
        bore_radius=bore_radius,
        clearance_side=clearance_side,
    )

    # fork_direction -> (rotation about Z, translation offset from attach
    # to bore in the world frame). attach is at (fork_x, fork_y) - unit_vec
    # * (ear_length - R_tip); bore is at (fork_x, fork_y).
    if fork_direction not in _FORK_DIR_VEC:
        raise ValueError(
            f"fork_direction {fork_direction!r} not in {sorted(_FORK_DIR_VEC)}"
        )
    angle_deg, (ux, uy) = _FORK_DIR_VEC[fork_direction]
    attach_x = fork_x - ux * (ear_length - R_tip)
    attach_y = fork_y - uy * (ear_length - R_tip)

    # In fork_local's frame, attach is at (0, 0) and bore at (ear_length - R_tip, 0).
    # Place fork_local so its attach lands at (attach_x, attach_y) and it
    # rotates so its local +X axis aligns with fork_direction.
    fork_placed = (
        Pos(attach_x, attach_y, 0)
        * Rotation(0, 0, angle_deg)
        * fork_local
    )

    return plate + fork_placed


_FORK_DIR_VEC = {
    "+x": (0.0, (1.0, 0.0)),
    "-x": (180.0, (-1.0, 0.0)),
    "+y": (90.0, (0.0, 1.0)),
    "-y": (270.0, (0.0, -1.0)),
}


def _fork_ear_bbox(fork_x, fork_y, ear_length, R_tip, direction):
    """World-frame (X, Y) AABB of a fork's ear body (rectangular slab +
    tip semicircle), used for pairwise overlap validation in
    `clevis_palm`. The tip semicircle is contained in the slab's extent
    (slab spans attach..bore = ear_length - R_tip; tip adds R_tip past
    bore, total = ear_length)."""
    if direction not in _FORK_DIR_VEC:
        raise ValueError(
            f"fork_direction {direction!r} not in {sorted(_FORK_DIR_VEC)}"
        )
    _angle, (ux, uy) = _FORK_DIR_VEC[direction]
    attach_x = fork_x - ux * (ear_length - R_tip)
    attach_y = fork_y - uy * (ear_length - R_tip)
    if direction in ("+x", "-x"):
        x_lo = min(attach_x, attach_x + ux * ear_length)
        x_hi = max(attach_x, attach_x + ux * ear_length)
        y_lo = attach_y - R_tip
        y_hi = attach_y + R_tip
    else:  # +y / -y
        y_lo = min(attach_y, attach_y + uy * ear_length)
        y_hi = max(attach_y, attach_y + uy * ear_length)
        x_lo = attach_x - R_tip
        x_hi = attach_x + R_tip
    return (x_lo, x_hi, y_lo, y_hi)


def _forks_overlap(forks, ear_length, R_tip):
    """Pairwise AABB-overlap test for fork ear bodies. Returns (i, j) of
    the first overlapping pair, or None. Overlap = ANY positive
    intersection area; touching (zero-area contact) is allowed."""
    boxes = [
        _fork_ear_bbox(
            float(f["fork_x"]), float(f["fork_y"]),
            ear_length, R_tip, f.get("fork_direction", "+x"),
        )
        for f in forks
    ]
    n = len(boxes)
    for i in range(n):
        x1lo, x1hi, y1lo, y1hi = boxes[i]
        for j in range(i + 1, n):
            x2lo, x2hi, y2lo, y2hi = boxes[j]
            if x1lo < x2hi and x2lo < x1hi and y1lo < y2hi and y2lo < y1hi:
                return (i, j)
    return None


def clevis_palm(
    plate_w: float,
    plate_d: float,
    plate_t: float,
    ear_length: float,
    ear_width: float,
    tongue_thickness: float,
    bore_radius: float,
    forks: list,
    clearance_side: float = 0.1,
    **_: Any,
) -> "Compound":
    """A flat plate (palm) with multiple clevis forks (finger roots).

    Each fork is structurally identical to `clevis_base_with_fork`'s fork
    (two Z-direction ears with a Z slot), placed at its own (fork_x,
    fork_y) on the plate and protruding in its own fork_direction. Use
    this for the fixed root of a multi-finger kinematic chain (dexterous
    hand palm with multiple finger bases).

    `forks` is a list of dicts, each:
        {"fork_x": <abs bore X>, "fork_y": <abs bore Y>,
         "fork_direction": "+x"|"-x"|"+y"|"-y"}
    (fork_x, fork_y) is the BORE centre (absolute local coord on the
    plate). All forks share the same ear_length / ear_width / bore_radius
    (passed at the top level); per-fork variation is not supported because
    the chain typically uses uniform fingers.

    Local coordinate convention:
      - Plate: X=-plate_w/2..+plate_w/2, Y=-plate_d/2..+plate_d/2,
        Z=0..plate_t (bottom at Z=0).
      - Fork bore at (fork_x, fork_y, plate_t/2).
      - Each fork's ears protrude from the plate in its own fork_direction.

    Validation (in addition to `_clevis_validate`):
      - At least one fork in `forks`.
      - No two forks' ear bodies overlap (AABB intersection). Merging ear
        bodies would also merge their Z slots, breaking the clevis
        kinematic function. Lateral min spacing = ear_width
        (perpendicular to fork_direction); protrusion-direction min
        spacing = ear_length + R_tip.

    For a single fork use `clevis_base_with_fork` (simpler API). Implicit
    kinematic mate: bore coaxiality + Z-slot clearance implement the
    revolute constraint; no physical Pin is modelled.
    """
    _clevis_validate(
        plate_t, bore_radius, ear_length, ear_width,
        tongue_thickness, clearance_side, bar_width=None,
    )
    if not forks:
        raise ValueError("clevis_palm requires at least one fork in `forks`")

    from build123d import Align, Box, Pos, Rotation

    R_tip = ear_width / 2.0
    overlap = _forks_overlap(forks, ear_length, R_tip)
    if overlap is not None:
        i, j = overlap
        f1, f2 = forks[i], forks[j]
        raise ValueError(
            f"clevis_palm forks {i},{j} ear bodies overlap: "
            f"fork{i}=(x={f1.get('fork_x')},y={f1.get('fork_y')},"
            f"dir={f1.get('fork_direction','+x')}) vs "
            f"fork{j}=(x={f2.get('fork_x')},y={f2.get('fork_y')},"
            f"dir={f2.get('fork_direction','+x')}); "
            f"increase lateral spacing (>= ear_width={ear_width}mm) "
            f"or reduce ear_width"
        )

    plate = Box(
        plate_w, plate_d, plate_t,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )

    for f in forks:
        fork_x = float(f["fork_x"])
        fork_y = float(f["fork_y"])
        fork_direction = f.get("fork_direction", "+x")
        angle_deg, (ux, uy) = _FORK_DIR_VEC[fork_direction]
        attach_x = fork_x - ux * (ear_length - R_tip)
        attach_y = fork_y - uy * (ear_length - R_tip)
        fork_local = _clevis_end_fork(
            sign=+1,
            bar_length=0.0,
            bar_thickness=plate_t,
            ear_length=ear_length,
            ear_width=ear_width,
            tongue_thickness=tongue_thickness,
            bore_radius=bore_radius,
            clearance_side=clearance_side,
        )
        fork_placed = (
            Pos(attach_x, attach_y, 0)
            * Rotation(0, 0, angle_deg)
            * fork_local
        )
        plate = plate + fork_placed

    return plate


# Registry: builder name -> function. The PartBuilder looks up builders
# by name from PartSpec.builder["name"].
BUILDERS = {
    "knuckle_hinge_ear": knuckle_hinge_ear,
    "shaft_with_arm": shaft_with_arm,
    "pivot_post": pivot_post,
    "link_bar": link_bar,
    "fork_end": fork_end,
    "mounting_plate": mounting_plate,
    "hollow_box": hollow_box,
    "lid": lid,
    "bracket_L": bracket_L,
    "standoff": standoff,
    "bushing": bushing,
    "gusset": gusset,
    "clevis_link": clevis_link,
    "clevis_base_with_fork": clevis_base_with_fork,
    "clevis_palm": clevis_palm,
    "ball_joint_socket": ball_joint_socket,
}


def build_part(builder_name: str, params: dict) -> "Compound":
    """Call a named builder with the given parameters and return the shape.

    Raises ValueError if the builder name is unknown or the builder raises.
    """
    if builder_name not in BUILDERS:
        raise ValueError(
            f"unknown builder {builder_name!r}; known: {sorted(BUILDERS)}"
        )
    return BUILDERS[builder_name](**params)
