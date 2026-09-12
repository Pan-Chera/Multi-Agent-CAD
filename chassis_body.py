"""Armoured mobile chassis body – build123d algebra API."""

from build123d import (
    Align,
    Axis,
    Box,
    Cylinder,
    Plane,
    Pos,
    Rot,
    extrude,
    export_step,
    export_stl,
    fillet,
    mirror,
    chamfer,
)
import math
import sys


def _safe_cut(body, tool, label):
    """Subtract *tool* from *body*, logging if the cut had no effect."""
    try:
        result = body - tool
        vol_before = body.volume
        vol_after = result.volume
        if abs(vol_before - vol_after) < 1e-3:
            print(f"[WARN] _safe_cut '{label}': no volume change (missed cut?)")
        return result
    except Exception as exc:
        print(f"[ERROR] _safe_cut '{label}' failed: {exc}")
        return body


def _measure_feature(var, name, ftype):
    """Print bounding box of a feature for debugging."""
    try:
        bb = var.bounding_box()
        print(
            f"[MEASURE] {name} ({ftype}): "
            f"X={bb.min.X:.1f}..{bb.max.X:.1f} "
            f"Y={bb.min.Y:.1f}..{bb.max.Y:.1f} "
            f"Z={bb.min.Z:.1f}..{bb.max.Z:.1f}"
        )
    except Exception:
        pass


def gen_step():
    # ------------------------------------------------------------------ #
    # 1. BASE STRUCTURE
    # ------------------------------------------------------------------ #

    # Skid plate: X=-270..270, Y=-200..200, Z=0..15
    skid_plate = Box(
        540, 400, 15,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    _measure_feature(skid_plate, "skid_plate", "box")

    # Central tub: X=-260..260, Y=-175..175, Z=10..105  (height 95)
    central_tub = Pos(0, 0, 10) * Box(
        520, 350, 95,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    _measure_feature(central_tub, "central_tub", "box")

    # Front bumper: extends to X=+280
    front_bumper = Pos(270, 0, 10) * Box(
        20, 350, 80,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    _measure_feature(front_bumper, "front_bumper", "box")

    # Rear bumper: extends to X=-280
    rear_bumper = Pos(-270, 0, 10) * Box(
        20, 350, 80,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    _measure_feature(rear_bumper, "rear_bumper", "box")

    body = skid_plate + central_tub + front_bumper + rear_bumper

    # Service channels (recessed pockets on each side Y=+/-175..+/-210)
    # Right channel (+Y side)
    ch_sketch_r = Pos(0, 192.5, 0) * Box(300, 35, 60, align=(Align.CENTER, Align.CENTER, Align.MIN))
    channel_r = Pos(0, 0, 20) * ch_sketch_r
    _measure_feature(channel_r, "channel_r", "box")
    body = _safe_cut(body, channel_r, "service_channel_right")

    # Left channel (-Y side) via mirror
    channel_l = mirror(channel_r, Plane.XZ)
    body = _safe_cut(body, channel_l, "service_channel_left")

    # Second set of recessed channels (narrower, higher)
    ch2_r = Pos(0, 0, 55) * Box(200, 35, 30, align=(Align.CENTER, Align.CENTER, Align.MIN))
    ch2_r_placed = Pos(0, 192.5, 0) * ch2_r
    body = _safe_cut(body, ch2_r_placed, "service_channel_2_right")
    ch2_l = mirror(ch2_r_placed, Plane.XZ)
    body = _safe_cut(body, ch2_l, "service_channel_2_left")

    # ------------------------------------------------------------------ #
    # 2. UPPER STRUCTURE / DECK SEAT REGION
    # ------------------------------------------------------------------ #

    # Transition volume from tub top (Z=105) up to deck seat (Z=155)
    upper_transition = Pos(0, 0, 105) * Box(
        460, 320, 50,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    _measure_feature(upper_transition, "upper_transition", "box")
    body = body + upper_transition

    # Deck seat region: ~430x300 at Z=155..165
    deck_seat = Pos(0, 0, 155) * Box(
        430, 300, 10,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    _measure_feature(deck_seat, "deck_seat", "box")
    body = body + deck_seat

    # ------------------------------------------------------------------ #
    # 3. WHEEL DRUMS (right-front prototype, then mirror)
    # ------------------------------------------------------------------ #
    # Each drum: R68 x 38 wide, axis along Y, centred near (195, 215, 70)

    def make_wheel_drums():
        # Drum cylinder along Y: rotate Z-axis cylinder by Rot(X=90) → axis along Y
        drum_solid = Rot(X=90) * Cylinder(
            radius=68, height=38,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
        drum_placed = Pos(195, 215, 70) * drum_solid
        _measure_feature(drum_placed, "drum_rf", "cylinder")

        # Axle boss overlapping chassis and drum (along Y)
        axle_solid = Rot(X=90) * Cylinder(
            radius=18, height=60,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
        axle_placed = Pos(195, 200, 70) * axle_solid
        _measure_feature(axle_placed, "axle_rf", "cylinder")

        # Stepped hub (outer ring on drum face)
        hub_solid = Rot(X=90) * Cylinder(
            radius=35, height=6,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
        hub_placed = Pos(195, 235, 70) * hub_solid
        _measure_feature(hub_placed, "hub_rf", "cylinder")

        # Inner hub step
        hub_inner_solid = Rot(X=90) * Cylinder(
            radius=22, height=8,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )
        hub_inner_placed = Pos(195, 236, 70) * hub_inner_solid
        _measure_feature(hub_inner_placed, "hub_inner_rf", "cylinder")

        wheel_assembly = drum_placed + axle_placed + hub_placed + hub_inner_placed

        # Tread grooves: 10 shallow cuts around the drum circumference
        groove_tools = None
        for i in range(10):
            angle_deg = i * 36.0
            angle_rad = math.radians(angle_deg)
            # Groove position on drum surface (R=68, centered at drum center)
            gx = 195 + 66 * math.cos(angle_rad)
            gz = 70 + 66 * math.sin(angle_rad)
            # Groove is a thin box cutting into the drum radially
            groove = Rot(X=90) * Box(
                6, 40, 4,
                align=(Align.CENTER, Align.CENTER, Align.CENTER),
            )
            # Position and orient groove radially
            groove_placed = Pos(gx, 215, gz) * Rot(Y=angle_deg) * groove
            if groove_tools is None:
                groove_tools = groove_placed
            else:
                groove_tools = groove_tools + groove_placed

        return wheel_assembly, groove_tools

    wheel_rf, grooves_rf = make_wheel_drums()
    body = body + wheel_rf
    if grooves_rf is not None:
        body = _safe_cut(body, grooves_rf, "tread_grooves_rf")

    # Mirror right-front → left-front (mirror across XZ plane: Y → -Y)
    wheel_lf = mirror(wheel_rf, Plane.XZ)
    body = body + wheel_lf
    if grooves_rf is not None:
        grooves_lf = mirror(grooves_rf, Plane.XZ)
        body = _safe_cut(body, grooves_lf, "tread_grooves_lf")

    # Mirror right-front → right-rear (mirror across YZ plane: X → -X)
    wheel_rr = mirror(wheel_rf, Plane.YZ)
    body = body + wheel_rr
    if grooves_rf is not None:
        grooves_rr = mirror(grooves_rf, Plane.YZ)
        body = _safe_cut(body, grooves_rr, "tread_grooves_rr")

    # Mirror right-front → left-rear (mirror across both planes)
    wheel_lr = mirror(mirror(wheel_rf, Plane.XZ), Plane.YZ)
    body = body + wheel_lr
    if grooves_rf is not None:
        grooves_lr = mirror(mirror(grooves_rf, Plane.XZ), Plane.YZ)
        body = _safe_cut(body, grooves_lr, "tread_grooves_lr")

    # ------------------------------------------------------------------ #
    # 4. LIFTING EYES / TIE-DOWN POCKETS (four corners)
    # ------------------------------------------------------------------ #
    # Near X=+/-250, Y=+/-200, Z=140..165 — recessed pockets

    eye_pocket = Pos(0, 0, 140) * Box(
        30, 30, 25,
        align=(Align.CENTER, Align.CENTER, Align.MIN),
    )
    eye_rf = Pos(250, 200, 0) * eye_pocket
    _measure_feature(eye_rf, "eye_pocket_rf", "box")
    body = _safe_cut(body, eye_rf, "eye_pocket_rf")

    eye_lf = mirror(eye_rf, Plane.XZ)
    body = _safe_cut(body, eye_lf, "eye_pocket_lf")

    eye_rr = mirror(eye_rf, Plane.YZ)
    body = _safe_cut(body, eye_rr, "eye_pocket_rr")

    eye_lr = mirror(mirror(eye_rf, Plane.XZ), Plane.YZ)
    body = _safe_cut(body, eye_lr, "eye_pocket_lr")

    # Add small torus-like lifting eye rings inside each pocket
    # Approximated as a small cylinder ring (outer - inner)
    def make_eye_ring(x_sign, y_sign):
        outer = Pos(250 * x_sign, 200 * y_sign, 148) * (
            Rot(X=90) * Cylinder(radius=10, height=6, align=(Align.CENTER, Align.CENTER, Align.CENTER))
        )
        inner = Pos(250 * x_sign, 200 * y_sign, 148) * (
            Rot(X=90) * Cylinder(radius=6, height=8, align=(Align.CENTER, Align.CENTER, Align.CENTER))
        )
        ring = outer - inner
        return ring

    body = body + make_eye_ring(1, 1)
    body = body + make_eye_ring(-1, 1)
    body = body + make_eye_ring(1, -1)
    body = body + make_eye_ring(-1, -1)

    # ------------------------------------------------------------------ #
    # 5. CHAMFERS ON SKID PLATE CORNERS (after all booleans)
    # ------------------------------------------------------------------ #
    try:
        # Chamfer bottom edges of skid plate
        bottom_edges = [
            e for e in body.edges().filter_by(Plane.XY)
            if abs(e.center_point().Z) < 1.0 and e.length > 100
        ]
        if bottom_edges:
            body = chamfer(bottom_edges, length=5.0)
    except Exception as exc:
        print(f"[WARN] Chamfer failed: {exc}")

    try:
        # Chamfer top edges of deck seat
        top_edges = [
            e for e in body.edges().filter_by(Plane.XY)
            if abs(e.center_point().Z - 165.0) < 1.0 and e.length > 100
        ]
        if top_edges:
            body = chamfer(top_edges, length=3.0)
    except Exception as exc:
        print(f"[WARN] Top chamfer failed: {exc}")

    # ------------------------------------------------------------------ #
    # 6. EXPORT
    # ------------------------------------------------------------------ #
    export_step(body, "chassis_body.step")
    export_stl(body, "chassis_body.stl", tolerance=0.01, angular_tolerance=0.1)
    print("[OK] chassis_body.step and chassis_body.stl written.")

    return {"shape": body}


if __name__ == "__main__":
    gen_step()
