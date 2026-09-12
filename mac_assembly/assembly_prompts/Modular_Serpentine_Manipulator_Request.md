# Large Modular Serpentine Manipulator — Assembly Request Prompt

Build a large, visually complex modular serpentine robotic manipulator as a
simulation-ready articulated assembly. It is an eight-joint planar inspection
arm made from a heavy mounting base, seven identical reusable intermediate
links, and one terminal tool link. All dimensions are in millimeters.

The purpose is to demonstrate that the assembly workflow can generate a large
multi-joint mechanism with high part reuse and a valid URDF tree. Prioritize a
clean, mechanically legible silhouette: repeated rectangular arm beams,
clevis ears, visible joint bores, and an alternating shallow S-shaped static
pose. Do not use gears, belts, cables, springs, closed loops, ball joints, or
decorative disconnected solids.

Small cosmetic and sub-millimetre deviations are acceptable. The part count,
joint count, serial topology, repeated geometry, connected solids, and
collision-free joint neighbourhoods are mandatory.

## Architecture

Kinematic chain:

```text
pedestal_base
  -> arm_link_1
  -> arm_link_2
  -> arm_link_3
  -> arm_link_4
  -> arm_link_5
  -> arm_link_6
  -> arm_link_7
  -> terminal_link
```

- Exactly 9 parts.
- Exactly 8 revolute mates.
- `pedestal_base` is the root.
- `arm_link_1` is the sole intermediate-link geometry template.
- `arm_link_2` through `arm_link_7` use `reuses_part_id="arm_link_1"`.
- All joint axes are local/world +Z.
- The complete arm moves in the XY plane.
- All parts occupy the same nominal Z layer, Z=0..18 mm.
- No fixed mates and no unconnected visual pieces.

## Shared clevis dimensions

Use these exact values for every joint interface:

```text
bar_thickness = 18
bore_radius = 4
ear_length = 24
ear_width = 20
tongue_thickness = 10
clearance_side = 0.2
```

These values satisfy the deterministic clevis builder constraints. Every
joint is a fork-and-tongue pair with an implicit pin axis along Z. Do not add
physical pin parts.

## Part 1: `pedestal_base`

Use the deterministic `clevis_base_with_fork` builder:

```json
{
  "name": "clevis_base_with_fork",
  "params": {
    "plate_w": 120,
    "plate_d": 100,
    "plate_t": 18,
    "fork_x": 70,
    "fork_y": 0,
    "ear_length": 24,
    "ear_width": 20,
    "tongue_thickness": 10,
    "bore_radius": 4,
    "clearance_side": 0.2,
    "fork_direction": "+x"
  }
}
```

Coordinate convention:

- Base plate X=-60..60, Y=-50..50, Z=0..18.
- Fork projects from the +X side.
- Fork bore centre is exactly (70, 0, 9).
- The base and fork must be one connected solid.

## Part 2: `arm_link_1` — reusable template

Use the deterministic `clevis_link` builder:

```json
{
  "name": "clevis_link",
  "params": {
    "bar_length": 60,
    "bar_width": 24,
    "bar_thickness": 18,
    "bore_radius": 4,
    "ear_length": 24,
    "ear_width": 20,
    "tongue_thickness": 10,
    "clearance_side": 0.2,
    "minus_x_end": "tongue",
    "plus_x_end": "fork"
  }
}
```

Local geometry:

- Main beam X=-30..30, Y=-12..12, Z=0..18.
- Minus-X tongue bore centre is (-44, 0, 9).
- Plus-X fork bore centre is (+44, 0, 9).
- Joint-centre spacing is exactly 88 mm.
- The link and both clevis interfaces form one connected solid.

## Parts 3–8: repeated intermediate links

Create six independent assembly instances:

- `arm_link_2` reuses `arm_link_1`.
- `arm_link_3` reuses `arm_link_1`.
- `arm_link_4` reuses `arm_link_1`.
- `arm_link_5` reuses `arm_link_1`.
- `arm_link_6` reuses `arm_link_1`.
- `arm_link_7` reuses `arm_link_1`.

Each instance must remain a separate labelled part with its own pose and
joint, but no LLM should regenerate its geometry.

## Part 9: `terminal_link`

Use the deterministic `clevis_link` builder with the same dimensions, except
the positive end is plain:

```json
{
  "name": "clevis_link",
  "params": {
    "bar_length": 60,
    "bar_width": 32,
    "bar_thickness": 18,
    "bore_radius": 4,
    "ear_length": 24,
    "ear_width": 20,
    "tongue_thickness": 10,
    "clearance_side": 0.2,
    "minus_x_end": "tongue",
    "plus_x_end": "plain"
  }
}
```

- Minus-X tongue bore centre is (-44, 0, 9).
- The wider 32 mm beam visually reads as a terminal sensor/tool housing.
- Its free +X face is the payload mounting face.
- Do not add another joint or payload part.

## Revolute mates

Every mate uses cylindrical SELECTOR anchors:

- surface=`cylinder`
- axis=`z`
- value_mm=`4.0`
- target_y_mm=`0`
- target_z_mm=`9`
- fixed fork target_x_mm=`+44` for an arm link, or `+70` on the base
- moving tongue target_x_mm=`-44`
- axial_offset_mm=`0`
- limits lower=`-45` degrees, upper=`+45` degrees
- tolerance_mm=`0.2`

Create these eight mates in this exact tree order:

1. `joint_1`: pedestal_base fork at (70,0,9) -> arm_link_1 tongue at
   (-44,0,9), static angle +15 degrees.
2. `joint_2`: arm_link_1 fork (+44,0,9) -> arm_link_2 tongue (-44,0,9),
   static angle -30 degrees.
3. `joint_3`: arm_link_2 -> arm_link_3, static angle +30 degrees.
4. `joint_4`: arm_link_3 -> arm_link_4, static angle -30 degrees.
5. `joint_5`: arm_link_4 -> arm_link_5, static angle +30 degrees.
6. `joint_6`: arm_link_5 -> arm_link_6, static angle -30 degrees.
7. `joint_7`: arm_link_6 -> arm_link_7, static angle +30 degrees.
8. `joint_8`: arm_link_7 fork -> terminal_link tongue, static angle
   -15 degrees.

All fixed anchors are forks and all moving anchors are tongues. Do not reverse
them. All moving subtrees remain in the same Z layer. Do not use
`face_to_face`, `rigid`, `coaxial`, or `ball` mates.

The alternating angles produce a shallow S-shaped arm while keeping the chain
mostly extended along +X. Angles are relative joint angles, not cumulative
world angles. Preserve them in the actual mating-plan fields.

## Geometry and collision requirements

- Every part is one connected watertight solid.
- Reused parts must be exact geometric copies of `arm_link_1`.
- Fork ears receive the neighbouring tongue in the central Z slot.
- Adjacent links may touch at intended joint interfaces but must not have
  unintended volumetric overlap deeper than 0.3 mm.
- The complete serial chain must remain connected for joint sweeps of at least
  +/-30 degrees around each static pose.
- Non-adjacent links must not intersect in the requested static S pose.
- Do not place multiple links on the same joint or stack links vertically.
- Do not collapse the repeated links into a single long beam.

## Expected scale

- Base envelope: approximately 120 x 100 x 18 mm plus its fork.
- Each intermediate joint-centre spacing: 88 mm.
- Eight-link reach in the mostly extended pose: approximately 700 mm from the
  base fork to the terminal region.
- Expected complete static envelope: approximately 800 x 100 x 18 mm.

The X and Y envelope estimates are intentionally approximate because the
alternating static angles change the projected reach. Accept +/-15 percent in
X and let the base width dominate Y. Z must remain approximately 18 mm.

## Overall

- assembly_name: `modular_serpentine_manipulator`
- expected_part_count: 9
- expected_mate_count: 8
- root_link: `pedestal_base`
- active_DOF: exactly 8 revolute joints
- unique geometry count: exactly 3
- reused part instances: exactly 6
- required output: STEP assembly, STL, GLB, URDF, manifests, and saved views

Reject a result if links are missing, duplicated at one pivot, disconnected,
vertically stacked, exported as fixed joints, given the wrong axis, or if
reuse is replaced by six independent LLM generations.
