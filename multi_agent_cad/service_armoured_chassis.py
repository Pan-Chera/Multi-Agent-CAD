"""Armoured mobile chassis body generator – build123d Algebra API."""

from __future__ import annotations

import math
import logging

from build123d import (
    Align,
    Axis,
    Box,
    BuildLine,
    Cylinder,
    Line,
    Plane,
    Pos,
    Rot,
    Torus,
    extrude,
    make_face,
    mirror,
)

logger = logging.getLogger(__name__)


def _safe_cut(body, tool, label):
    """Subtract *tool* from *body*, logging a warning if volume is unchanged."""
    try:
        vol_before = body.volume
        result = body - tool
        vol_after = result.volume
        if abs(vol_after - vol_before) < 1e-3:
            logger.warning("Missed cut '%s': no volume change", label)
        return result
    except Exception as exc:
        logger.warning("Cut '%s' failed: %s", label, exc)
        return body


def gen_step():
    """Generate the armoured mobile chassis body."""

    # ------------------------------------------------------------------
    # Parameters extracted from the specification
    # ------------------------------------------------------------------
    DRUM_CENTER_X = 195.0
    DRUM_CENTER_Y = 215.0
    DRUM_CENTER_Z = 70.0
    DRUM_RADIUS = 68.0
    DRUM_WIDTH = 38.0

    TREAD_GROOVE_COUNT = 10  # within 8-12 range

    SERVICE_CHANNEL_MIN_Y = 175.0
    SERVICE_CHANNEL_MAX_Y = 210.0

    BUMPER_EXTENT_X = 280.0

    DECK_MIN_X = -215.0
    DECK_MAX_X = 215.0
    DECK_MIN_Y = -150.0
    DECK_MAX_Y = 150.0
    DECK_MAX_Z = 165.0

    LIFTING_EYE_X = 250.0
    LIFTING_EYE_Y = 200.0
    LIFTING_EYE_MIN_Z = 140.0
    LIFTING_EYE_MAX_Z = 165.0

    SKID_PLATE_MIN_X = -270.0
    SKID_PLATE_MAX_X = 270.0
    SKID_PLATE_MIN_Y = -200.0
    SKID_PLATE_MAX_Y = 200.0
    SKID_PLATE_HEIGHT_Z = 15.0

    # ==================================================================
    # 1. Central tub – octagonal cross-section (chamfered corners) [9]
    # ==================================================================
    # Octagonal profile with chamfered corners (non-regular polygon)
    with BuildLine() as tub_wire:
        Line((-230.0, -175.0), (230.0, -175.0))
        Line((230.0, -175.0), (260.0, -145.0))
        Line((260.0, -145.0), (260.0, 145.0))
        Line((260.0, 145.0), (230.0, 175.0))
        Line((230.0, 175.0), (-230.0, 175.0))
        Line((-230.0, 175.0), (-260.0, 145.0))
        Line((-260.0, 145.0), (-260.0, -145.0))
        Line((-260.0, -145.0), (-230.0, -175.0))

    tub_sketch = make_face(tub_wire.wire())
    # Tub spans Z=10..105 → height=95, bottom at Z=10
    tub = Pos(0, 0, 10) * extrude(
        tub_sketch, amount=95, align=(Align.CENTER, Align.CENTER, Align.MIN)
    )

    # ==================================================================
    # 2. Underside skid plate [12]
    # ==================================================================
    skid_plate = Pos(0, 0, 0) * Box(
        SKID_PLATE_MAX_X - SKID_PLATE_MIN_X,  # 540
        SKID_PLATE_MAX_Y - SKID_PLATE_MIN_Y,  # 400
        SKID_PLATE_HEIGHT_Z,                   # 15
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )

    body = tub + skid_plate

    # ==================================================================
    # 3. Front / rear bumper volumes [8]
    # ==================================================================
    # Bumpers extend from tub face (X=±260) to X=±280, overlapping by 5 mm
    bumper_length = (BUMPER_EXTENT_X - 260.0) + 5.0  # 25 mm total, overlaps 5 mm
    bumper_width = 300.0
    bumper_height = 50.0

    front_bumper = Pos(260.0 - 5.0 + bumper_length / 2.0, 0, 10) * Box(
        bumper_length, bumper_width, bumper_height,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    rear_bumper = mirror(front_bumper, Plane.YZ)

    body = body + front_bumper + rear_bumper

    # ==================================================================
    # 4. Side skirts to bridge tub (Y=±175) to service channel outer edge
    # ==================================================================
    skirt_depth = SERVICE_CHANNEL_MAX_Y - SERVICE_CHANNEL_MIN_Y  # 35
    skirt_length = 400.0
    skirt_height = 80.0

    right_skirt = Pos(0, SERVICE_CHANNEL_MIN_Y + skirt_depth / 2.0, 10) * Box(
        skirt_length, skirt_depth, skirt_height,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    left_skirt = mirror(right_skirt, Plane.XZ)

    body = body + right_skirt + left_skirt

    # ==================================================================
    # 5. Recessed service channels (two per side) [7]
    # ==================================================================
    # Two pockets per side, cut into the skirts. Overshoot by 1 mm in Z.
    ch_length = 120.0
    ch_depth = 20.0
    ch_height = 40.0
    ch_spacing = 160.0  # distance between channel centres along X

    # Cut tool: centered in Y within the skirt depth, overshooting outward by 1mm
    ch_tool = Pos(0, SERVICE_CHANNEL_MAX_Y - ch_depth / 2.0 + 1.0, 20) * Box(
        ch_length, ch_depth + 2.0, ch_height + 2.0,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )

    for idx, x_off in enumerate([-ch_spacing / 2.0, ch_spacing / 2.0]):
        # Right side (Y > 0)
        tool_r = Pos(x_off, 0, 0) * ch_tool
        body = _safe_cut(body, tool_r, f"service_channel_R{idx}")
        # Left side (Y < 0) via mirror
        tool_l = mirror(tool_r, Plane.XZ)
        body = _safe_cut(body, tool_l, f"service_channel_L{idx}")

    # ==================================================================
    # 6. Upper superstructure pillars to support deck at Z=155
    # ==================================================================
    pillar_w = 40.0
    pillar_d = 40.0
    pillar_h = 50.0  # Z=105..155

    pillar_rf = Pos(200, 130, 105) * Box(
        pillar_w, pillar_d, pillar_h,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    # Mirror to all four corners correctly
    pillar_lf = mirror(pillar_rf, Plane.XZ)   # Y -> -Y
    pillar_rb = mirror(pillar_rf, Plane.YZ)   # X -> -X
    pillar_lb = mirror(pillar_rf, Plane.XZ)   # start from RF
    pillar_lb = mirror(pillar_lb, Plane.YZ)   # then X -> -X

    body = body + pillar_rf + pillar_lf + pillar_rb + pillar_lb

    # ==================================================================
    # 7. Deck seat region [10]
    # ==================================================================
    deck_thickness = DECK_MAX_Z - 155.0  # 10 mm
    deck = Pos(0, 0, 155) * Box(
        DECK_MAX_X - DECK_MIN_X,  # 430
        DECK_MAX_Y - DECK_MIN_Y,  # 300
        deck_thickness,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    body = body + deck

    # ==================================================================
    # 8. Lifting eyes / tie-down pockets [11]
    # ==================================================================
    eye_z = (LIFTING_EYE_MIN_Z + LIFTING_EYE_MAX_Z) / 2.0  # 152.5
    eye_base_pad = Pos(LIFTING_EYE_X, LIFTING_EYE_Y, 155) * Box(
        30, 30, 10, align=(Align.CENTER, Align.CENTER, Align.MIN)
    )
    eye_ring = Pos(LIFTING_EYE_X, LIFTING_EYE_Y, eye_z + 5) * Torus(
        major_radius=12.0, minor_radius=3.0
    )
    eye_rf = eye_base_pad + eye_ring

    # Mirror to all four corners correctly
    eye_lf = mirror(eye_rf, Plane.XZ)    # Y -> -Y
    eye_rb = mirror(eye_rf, Plane.YZ)    # X -> -X
    eye_lb = mirror(eye_lf, Plane.YZ)    # X -> -X from left-front

    body = body + eye_rf + eye_lf + eye_rb + eye_lb

    # ==================================================================
    # 9. Wheel drums, axle bosses, hubs, tread grooves [3][4][5][6]
    # ==================================================================
    half_width = DRUM_WIDTH / 2.0  # 19

    # --- Drum body (axis along Y) [4] ---
    # Cylinder defaults to Z axis; Rot(X=90) rotates it to Y axis.
    drum = Pos(DRUM_CENTER_X, DRUM_CENTER_Y, DRUM_CENTER_Z) * (
        Rot(X=90) * Cylinder(
            radius=DRUM_RADIUS,
            height=DRUM_WIDTH,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
    )

    # --- Stepped hub (outer) ---
    hub_outer = Pos(DRUM_CENTER_X, DRUM_CENTER_Y + half_width + 3.0, DRUM_CENTER_Z) * (
        Rot(X=90) * Cylinder(
            radius=30.0,
            height=6.0,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
    )
    # --- Stepped hub (inner step) ---
    hub_inner = Pos(DRUM_CENTER_X, DRUM_CENTER_Y + half_width + 7.0, DRUM_CENTER_Z) * (
        Rot(X=90) * Cylinder(
            radius=18.0,
            height=4.0,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
    )

    # --- Axle boss [6] ---
    # Must overlap both the chassis body (skirt ends ~Y=210) and the drum
    # (inner face at Y=DRUM_CENTER_Y - half_width = 196).
    # Boss spans Y=160..216 → length=56, centre Y=188.
    boss_start_y = 160.0
    boss_end_y = DRUM_CENTER_Y - half_width + 20.0  # 196 + 20 = 216
    boss_length = boss_end_y - boss_start_y  # 56
    boss_cy = (boss_start_y + boss_end_y) / 2.0  # 188

    axle_boss = Pos(DRUM_CENTER_X, boss_cy, DRUM_CENTER_Z) * (
        Rot(X=90) * Cylinder(
            radius=15.0,
            height=boss_length,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
    )

    wheel_assembly = drum + hub_outer + hub_inner + axle_boss

    # --- Tread grooves [5] ---
    # 10 shallow grooves cut around the circumference of the drum.
    # Each groove is a thin box positioned radially outward from the drum
    # surface, then rotated around the drum's Y-axis.
    groove_depth = 4.0
    groove_width = 6.0
    groove_length = DRUM_WIDTH + 4.0  # overshoot ±2 mm past drum faces

    for i in range(TREAD_GROOVE_COUNT):
        angle_deg = i * (360.0 / TREAD_GROOVE_COUNT)

        # Create the groove box at the "top" of the drum (positive Z offset
        # from drum center), slightly overshooting outward for a clean cut.
        r_cut = DRUM_RADIUS - groove_depth / 2.0 + 1.0
        groove_box = Pos(
            DRUM_CENTER_X,
            DRUM_CENTER_Y,
            DRUM_CENTER_Z + r_cut,
        ) * Box(
            groove_length, groove_width, groove_depth + 2.0,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )

        # Rotate the groove box around the drum axis (Y axis through drum centre)
        groove_rotated = (
            Pos(DRUM_CENTER_X, DRUM_CENTER_Y, DRUM_CENTER_Z)
            * Rot(Y=angle_deg)
            * Pos(-DRUM_CENTER_X, -DRUM_CENTER_Y, -DRUM_CENTER_Z)
            * groove_box
        )
        wheel_assembly = _safe_cut(wheel_assembly, groove_rotated, f"tread_R_pos_{i}")

    # --- Pattern to all four positions via symmetry [3] ---
    # Right-front is the base. Mirror across XZ (Y→-Y) for left-front.
    wheel_left = mirror(wheel_assembly, Plane.XZ)
    # Mirror both across YZ (X→-X) for rear pair.
    wheel_rear_right = mirror(wheel_assembly, Plane.YZ)
    wheel_rear_left = mirror(wheel_left, Plane.YZ)

    body = body + wheel_assembly + wheel_left + wheel_rear_right + wheel_rear_left

    # ==================================================================
    # 10. Fillets / chamfers would go here (after ALL booleans) [Rule 5]
    #     Omitted intentionally to avoid edge-selector fragility on this
    #     complex multi-feature solid. Corners are already chamfered via
    #     the octagonal tub profile [9].
    # ==================================================================

    return {"shape": body}
