# Corrected Hinged Twin-Claw Gripper

Generate a simple gripper with exactly 3 parts and 2 revolute joints:
`mount_plate`, `left_jaw`, and `right_jaw`. Units are millimetres. Required
outputs: STEP, STL, GLB, and URDF. Do not model pins, actuators, a robot arm,
or a workpiece.

## Critical geometry

1. The mounting plate is horizontal in XY, thickness along Z.
2. BOTH hinge pin/bore axes are horizontal **Y axes**. Never use pin axis Z.
   The jaws rotate in the XZ plane and hang below the plate. Do not use the
   `clevis_palm` builder because it makes legacy Z-axis forks.
3. Each jaw consists of exactly two rectangular bars. The visible interior
   angle at the elbow is **120 degrees**, not 60 degrees.
4. The jaws are exact X mirrors. Their lower bars point inward toward X=0.
5. Each plate fork points downward from the plate bottom, and each jaw
   tongue points upward from the jaw top. The clevis stem and the jaw's
   vertical top segment are collinear, never perpendicular.

## Shared clevis dimensions

For every `clevis_fork` and `clevis_tongue` feature use:

- ear_length 25
- ear_width 20
- bore_radius 5.0
- tongue_thickness 12
- bar_thickness 20
- clearance_side 0.1
- pin_axis `y`

With this feature operator, body length is 15 mm and the bore centre is the
attachment point plus 15 mm along the feature direction.

## Part: mount_plate

MANDATORY: use the deterministic `mounting_plate` builder, then add two
features. Do not use a free-form base_body:

```text
builder.name = mounting_plate
builder.params = {width:140, depth:100, thickness:22,
                  hole_radius:3.2, hole_dx:55, hole_dy:35,
                  central_hole_radius:0}
base_body = null
```

The builder produces X=-70..70, Y=-50..50, Z=0..22 and the four required
Z-axis mounting holes. Do not add duplicate `through_bore` features.

Features:

- `clevis_fork` at attachment (-60,0,0), direction `-z`, pin_axis `y`.
  Its Y-axis R5 bore centre must be (-60,0,-15).
- `clevis_fork` at attachment (60,0,0), direction `-z`, pin_axis `y`.
  Its Y-axis R5 bore centre must be (60,0,-15).
Each fork is 20 mm wide along X, so these centres keep its complete
X=-70..-50 or X=50..70 footprint inside the widened plate. The wider hinge
spacing also gives the gripping pads clearance throughout the joint sweep.
The two fork ears are separated along Y and hang below the plate. In front
view along Y, each hinge
bore must appear as a circle. This is the required 90-degree correction from
the previous incorrect Z-axis design.

## Part: left_jaw

MANDATORY: use the deterministic builder below, not a free-form base_body:

```text
builder.name = bent_jaw_xz
builder.params = {side:left, root_length:40, root_width:18, depth_y:12,
                  tip_length:50, tip_height:18,
                  tip_down_angle_deg:30, elbow_overlap:6.0,
                  pad_thickness_x:6, pad_depth_y:24, pad_height_z:18}
base_body = null
```

The builder uses two centred Box primitives in the XZ plane and deliberately
overlaps them by 6 mm at the elbow, so the result has a broad, visibly
continuous material bridge rather than a V-shaped seam or corner-only
connection. Its segment directions define a visible 120-degree interior
angle. Do not send this simple geometry through the LLM single-part Coder.
The builder must fuse a 24 mm wide by 18 mm high rectangular gripping pad
to the distal end. Its 6 mm-thick flat inner face is normal to X and faces
toward the centre between the jaws. The pad is integral with the jaw and
must not be emitted as a separate part or left with a gap.

Attach one `clevis_tongue` to the jaw top at (0,0,0), direction `+z`,
pin_axis `y`, using the shared dimensions. Its bore centre is local
(0,0,15), axis Y. The tongue stem continues along the same vertical axis as
the top segment; it must not protrude sideways.

## Part: right_jaw

MANDATORY: use the same deterministic builder with `side:right`, not a
free-form base_body:

```text
builder.name = bent_jaw_xz
builder.params = {side:right, root_length:40, root_width:18, depth_y:12,
                  tip_length:50, tip_height:18,
                  tip_down_angle_deg:30, elbow_overlap:6.0,
                  pad_thickness_x:6, pad_depth_y:24, pad_height_z:18}
base_body = null
```

This is the exact X mirror and has the same visible 120-degree interior
angle. Attach one `clevis_tongue` to the top at (0,0,0), direction `+z`,
pin_axis `y`; its bore centre is local (0,0,15), axis Y and the stem is
collinear with the vertical top segment.

## Mate plan

The mate tree is rooted at `mount_plate` and has exactly two edges. Both are
revolute mates with angle_deg=0, axial_offset_mm=0, and limits -25..25 deg.
Use cylinder SELECTOR anchors with axis `y`, radius 5.0, `closest_to`, and all
three target coordinates:

- `left_jaw_pivot`: fixed mount_plate bore (-60,0,-15), moving left_jaw bore
  (0,0,15). Expected jaw translation approximately (-60,0,-30).
- `right_jaw_pivot`: fixed mount_plate bore (60,0,-15), moving right_jaw bore
  (0,0,15). Expected jaw translation approximately (60,0,-30).

Do not add a mate between the jaws. Do not use axis_point, bbox, or face
anchors. Do not omit joint limits.

## Acceptance criteria

- exactly 3 connected parts and exactly 2 revolute mates;
- all four mating bores have axis Y, never Z;
- the plate remains horizontal while both jaws hang below it;
- each jaw visibly has a 120-degree interior elbow angle;
- each elbow has a broad solid connection with no visible gap;
- each tongue/fork stem is collinear with the jaw top segment;
- lower jaw segments point inward and the result is mirror symmetric;
- no disconnected decorative solids or intersecting clevis tongue/fork ears.
