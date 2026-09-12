# Rotary Fork-Key End Effector — Assembly Request Prompt

Build a compact, simulation-ready rotary fork-key end effector for engaging
and turning an external T-tab, wing knob, or matching keyed cap. The generated
assembly is the robot-side tool only; do not include the external knob, bottle,
fixture, motor, robot arm, or work surface. All dimensions are millimeters.

The assembly must expose exactly one active revolute joint. A robot controller
holds the mounting plate pose while commanding the spindle angle. Rotation is
transmitted rigidly to a two-prong fork key, which contacts and turns the
external task object through real MuJoCo collision. Use a simple open
kinematic tree. Do not use gears, belts, threads, springs, tendons, flexible
parts, or closed-loop mates.

Small dimensional deviations are acceptable if the coarse external form,
coaxial shaft guidance, collision-free rotation, usable fork slot, and single
rotational DOF are preserved.

## Coordinate and task convention

- Mounting plate is the root part.
- Main rotation axis is local/world Z through X=0, Y=0.
- The tool projects downward in -Z toward a horizontal work surface.
- Positive spindle command rotates about +Z by the right-hand rule.
- Nominal static angle is 0 degrees.
- Required joint range is -180 to +180 degrees.
- The fork prongs point downward and straddle an external tab.

## Required parts — exactly 4

### 1. `mounting_plate`

A rigid square robot mounting plate.

- Plate: 70 x 70 x 8 mm, centred at X=0, Y=0, occupying Z=0..8.
- Four vertical mounting holes, radius 3 mm, through the plate at
  (X,Y)=(+/-26,+/-26).
- One central vertical through-bore, radius 6.2 mm, at X=Y=0.
- Bottom face Z=0 remains flat around the central bore for mounting the
  bearing sleeve.
- Top face Z=8 remains flat.
- Mild R4 corner fillets are optional; do not fillet or distort bore edges.
- Produce one connected solid with five real through-holes.

Prefer the deterministic `mounting_plate` builder if it supports the central
hole and four corner holes. Otherwise use a simple LLM-generated solid. Local
coordinates and bore axis must remain exactly as described.

### 2. `bearing_sleeve`

A rigid cylindrical bushing fixed under the mounting plate.

- Outer radius: 12 mm.
- Inner through-bore radius: 6.2 mm.
- Length: 28 mm along local Z.
- Local geometry occupies Z=0..28.
- In the assembled pose it occupies world Z=-28..0.
- Its top annular face at world Z=0 sits flush against the mounting-plate
  bottom face.
- Its bore is coaxial with the plate bore at X=Y=0.

Use the deterministic `bushing` builder with
`outer_radius=12`, `inner_radius=6.2`, `length=28`.

### 3. `drive_spindle`

A single rigid moving component made from a guide shaft, lower drive flange,
and upper retention collar. All cylinders share the local Z axis and must be
boolean-unioned into one connected solid.

- Main shaft: radius 5.5 mm, spanning local Z=0..48.
- Lower drive flange: radius 9 mm, thickness 4 mm, spanning local Z=0..4,
  fused around the lower shaft end.
- Upper retention collar: radius 9 mm, thickness 4 mm, spanning local
  Z=44..48, fused around the upper shaft end.
- Overall local bounds approximately 18 x 18 x 48 mm.
- The shaft has 0.7 mm radial clearance inside the R6.2 bores.
- In the assembled static pose, translate this part by world Z=-36 mm:
  shaft and complete spindle occupy world Z=-36..+12.
- The lower flange therefore occupies world Z=-36..-32, below the sleeve
  bottom at Z=-28 with a 4 mm exposed-shaft gap.
- The upper collar occupies world Z=+8..+12, above the plate top at Z=8.
- Neither R9 collar may enter the R6.2 guide bore.
- The flat lower face at local Z=0 is the fixed mounting interface for the
  fork-key head.

Use straightforward Z-axis cylinders. Do not rotate the spindle onto X or Y,
and do not output print-orientation-rotated geometry for assembly use.

### 4. `fork_key_head`

A rigid replaceable two-prong key head, generated as one connected solid.

- Local top mounting plane is Z=0; all material extends in -Z.
- Central circular hub: radius 9 mm, thickness 6 mm, occupying Z=-6..0.
- Add two rectangular prongs fused to the hub underside.
- Left prong: X=-13..-7, Y=-6..+6, Z=-22..-6.
- Right prong: X=+7..+13, Y=-6..+6, Z=-22..-6.
- Each prong is 6 mm wide in X, 12 mm deep in Y, and 16 mm long in Z.
- Clear slot between the inner faces is exactly 14 mm wide in X, open from
  below and open through Z=-22..-6.
- Overall local bounds approximately 26 x 18 x 22 mm.
- The two prongs and hub must form one connected solid; embed each prong into
  the hub by 0.2 mm if necessary for a reliable boolean union.
- Keep the prong inner faces planar and parallel. Small R1 edge fillets are
  optional, but do not close or narrow the 14 mm slot.
- Do not add a decorative external knob or task object between the prongs.

In the assembled pose, the hub top at local Z=0 attaches to the spindle lower
flange bottom at world Z=-36. The complete key therefore occupies world
Z=-58..-36 and rotates rigidly with the spindle.

## Required mates — exactly 3

### `sleeve_mount`

- Type: rigid.
- Fixed part: `mounting_plate`.
- Moving part: `bearing_sleeve`.
- Align Z axes at X=Y=0.
- Sleeve top face local Z=28 sits flush with mounting plate bottom Z=0.
- Resulting sleeve world range must be Z=-28..0.

### `spindle_revolute`

- Type: revolute.
- Fixed part: `bearing_sleeve`.
- Moving part: `drive_spindle`.
- Both anchors select coaxial Z-axis cylinders at X=Y=0:
  sleeve bore radius 6.2 mm and spindle shaft radius 5.5 mm.
- Static spindle translation must place spindle local Z=0 at world Z=-36
  and local Z=48 at world Z=+12.
- Static angle: 0 degrees.
- Rotation axis: +Z.
- Limits: lower=-180 degrees, upper=+180 degrees.
- This is the only active joint in the assembly.
- The required axial placement is a translation only; do not rotate the
  spindle body away from its Z axis.

Because cylindrical SELECTOR implementations may return an endpoint rather
than a bounding-box midpoint, compute the signed axial offset from the actual
resolved selector points. Verify the final placed spindle bounds
Z=-36..+12 instead of trusting a midpoint-only calculation.

For this exact geometry, the verified value is
`axial_offset_mm=+2.0`: the placed sleeve bore axis datum and spindle shaft
datum require a positive 2 mm shift. Write `2.0` in the actual JSON field,
not merely in explanatory notes, and preserve `limit_lower=-180` and
`limit_upper=180`.

### `key_mount`

- Type: rigid.
- Fixed part: `drive_spindle`.
- Moving part: `fork_key_head`.
- Align both Z axes and X/Y orientation without twist.
- Key-head top face local Z=0 sits flush with spindle lower-flange bottom
  local Z=0 at world Z=-36.
- The key must inherit spindle rotation as a rigid child.
- Resulting key bounds must be world Z=-58..-36.

## Collision and functional requirements

- The sleeve touches the plate only at its intended top annular mounting
  face; no volumetric overlap.
- The spindle shaft intentionally occupies the plate and sleeve bores with
  0.7 mm radial clearance. This is a valid rotating fit.
- Upper and lower R9 collars remain outside the R6.2 bore for all angles.
- The fork head must not collide with the stationary sleeve or mounting plate
  during a full -180..+180 degree rotation.
- The fork slot remains open and physically usable by an external 10–12 mm
  wide tab, with at least 1 mm total lateral clearance.
- Do not hide collisions, merge moving and fixed parts, or add a weld across
  `spindle_revolute`.
- Do not model material strength, a motor, bearings balls, screws, wires, or
  a return mechanism. The revolute joint is position-driven in simulation.

## Static coordinate summary

- `mounting_plate`: world Z=0..8.
- `bearing_sleeve`: world Z=-28..0.
- `drive_spindle`: world Z=-36..+12.
- `fork_key_head`: world Z=-58..-36.
- Overall static envelope: approximately 70 x 70 x 70 mm.
- Working contact region is the 14 mm slot between the two prongs.

## Overall

- assembly_name: `rotary_fork_key_end_effector`
- expected_part_count: 4
- expected_mate_count: 3
- active_DOF: exactly 1 revolute joint
- required joint: `spindle_revolute`, axis +Z, limits -180..+180 degrees
- root_link: `mounting_plate`
- required outputs: separate-part STEP assembly, STL, GLB, and URDF.

Accept small cosmetic or sub-millimetre deviations, but reject wrong global
axis, a blocked fork slot, disconnected prongs, a spindle not passing through
the guide, collars inside the guide bore, incorrect part count, extra active
joints, missing joint limits, or a fork head that is not rigidly driven by the
spindle.
