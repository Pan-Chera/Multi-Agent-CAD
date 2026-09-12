# Guided Plunger End Effector — Assembly Request Prompt

Build a compact, simulation-ready guided linear plunger end effector for
pressing an external spring-loaded slider or push button. The assembly must
convert one commanded prismatic joint displacement into a straight axial
motion of a rigid plunger. It is a task-specific robot end effector, not the
external button/slider fixture itself. All dimensions are in millimeters.

The design must be robust for URDF export and MuJoCo simulation. Prefer simple,
clearly separated solids and one open kinematic chain. Do not use threads,
gears, bearings, flexible parts, hidden pins, tendons, or closed-loop mates.

## Functional objective

- The end effector mounts by its rear plate to a robot/tool root.
- Its working axis is world/local **-Z**: the plunger extends downward from the
  underside of the assembly toward a horizontal work surface.
- One linear joint commands an extension stroke from 0 mm (retracted) to 12 mm
  (fully extended).
- The circular contact head remains centred on the guide axis throughout the
  stroke.
- The head presses an independent environment slider through real contact. The
  external slider is not part of this generated assembly.
- Retraction must leave at least 3 mm axial clearance between the head and a
  button that was just pressed 5 mm.

## Required parts — exactly 3

### 1. `mounting_base`

A rigid rectangular mounting plate and central support block treated as one
solid part.

- Plate envelope: 60 x 60 x 8 mm, centred at X=0, Y=0, Z=0..8.
- Four vertical mounting holes, radius 2.5 mm, centred at
  (X,Y)=(+/-22,+/-22), through Z.
- Add a central square support boss, 30 x 30 x 12 mm, on the underside of the
  plate, occupying X,Y=-15..15 and Z=-12..0. The boss and plate must be fused
  into one solid.
- Add one coaxial vertical guide bore through the boss and plate, radius
  5.2 mm, centred at X=Y=0, extending through the complete Z range.
- The guide bore is a real void. Do not fill it with a decorative cylinder.
- The bottom of the boss at Z=-12 is the rigid mounting surface for the guide
  sleeve.

Use either a reliable parameterized builder if it can express this geometry,
or a simple feature-based body. Do not add cosmetic fillets that risk changing
the guide datum.

### 2. `guide_sleeve`

A rigid cylindrical guide sleeve fixed to the underside of `mounting_base`.

- Outer radius: 9 mm.
- Inner through-bore radius: 5.2 mm.
- Length: 32 mm along Z.
- In assembled coordinates it occupies Z=-44..-12, with its axis at X=Y=0.
- The top annular face at Z=-12 sits flush against the mounting-base boss.
- The bore must remain open and coaxial with the base guide bore.
- Add no lateral holes or moving pieces.

Prefer the deterministic `bushing` builder when available. Its local bore axis
must be aligned with the assembly Z axis.

### 3. `plunger`

A single rigid moving part consisting of a guide shaft, a lower contact head,
and an upper hard-stop collar, all fused into one solid.

- Main shaft: radius 4.5 mm, total axial length 54 mm.
- In the retracted assembled pose, the shaft axis is X=Y=0 and the shaft spans
  Z=-49..+5.
- Circular contact head fused to the lower shaft end:
  radius 8 mm, thickness 4 mm, spanning Z=-53..-49.
- Contact face is the flat bottom face at Z=-53. Keep it planar and
  perpendicular to the Z axis; small edge fillets are optional.
- Upper hard-stop collar fused near the top:
  radius 8 mm, thickness 3 mm, spanning Z=+2..+5.
- The shaft radius is 0.7 mm smaller than the guide bore radius, providing
  0.7 mm radial clearance.
- The contact head remains outside and below the guide sleeve at all commanded
  positions. The upper collar remains above the guide and cannot pass through
  the 5.2 mm-radius bore.
- This is one connected solid. Do not create separate shaft, head, or collar
  parts.

Use straightforward Z-axis cylinders. The shaft itself is the linear-joint
axis datum; do not rotate it onto X or Y.

## Mates and kinematics — exactly 2

### `sleeve_mount`

- Type: `rigid`.
- Fixed part: `mounting_base`.
- Moving part: `guide_sleeve`.
- Align both Z axes coaxially at X=Y=0.
- Place the sleeve top annular face flush with the boss bottom face at Z=-12.
- The resulting sleeve range must be Z=-44..-12.

### `plunger_slide`

- Type: `linear`.
- Fixed part: `guide_sleeve`.
- Moving part: `plunger`.
- Translation axis: assembly Z axis.
- Static/retracted position: plunger shaft spans Z=-49..+5 and contact head
  spans Z=-53..-49.
- Command convention: joint position 0 mm is retracted; increasing joint
  position moves the plunger in **-Z**, toward the workpiece.
- Limits: lower=0 mm, upper=12 mm.
- At 12 mm extension, the contact face is at Z=-65.
- In this workflow, the cylindrical SELECTOR frame resolves at the selected
  cylinder's local axial datum rather than at the descriptive midpoint used
  in hand calculations. With the generated sleeve and plunger frames, the
  linear mate's verified static `position_mm` is **exactly -5.0 mm**. This
  places the complete plunger at assembly Z=-53..+5. Do not infer the offset
  from bounding-box midpoints and do not use -17 or +4. Preserve
  `limit_lower=0` and `limit_upper=12` in the mating-plan JSON. The assembly
  mate uses geometric `slide_axis="z"`; final URDF export must encode the
  commanded positive extension direction as `<axis xyz="0 0 -1"/>`.

For cylindrical SELECTOR anchors, explicitly request axis=`z`, radius values,
and target X=0, target Y=0 to avoid selecting a mounting hole. If the mating
system represents the slide axis with an `axis_point`, state the desired world
axis and signed motion unambiguously. Do not add a revolute mate.

## Collision and assembly requirements

- The guide sleeve and mounting boss may touch only at their intended annular
  mounting faces; they must not overlap volumetrically.
- The plunger shaft intentionally occupies the guide bores with radial
  clearance. This is a valid sliding fit, not interference.
- The plunger head and upper collar must never enter the guide bore over the
  0..12 mm stroke.
- There must be no disconnected decorative solids in any part.
- Do not model the external slider, return spring, robot arm, screws, wires, or
  actuator housing.
- Do not claim a physical spring return inside this assembly. Retraction is
  commanded by the prismatic actuator in simulation.

## Static pose and coordinate summary

- `mounting_base`: overall Z=-12..+8.
- `guide_sleeve`: Z=-44..-12, coaxial at X=Y=0.
- `plunger` at q=0: shaft Z=-49..+5, head Z=-53..-49, collar Z=+2..+5.
- Plunger at q=12 mm: all plunger coordinates shifted by -12 mm.
- Tool contact direction: -Z.
- The mounting plate remains the root link.

## Overall

- assembly_name: `guided_plunger_end_effector`
- expected_part_count: 3
- expected_mate_count: 2
- active_DOF: exactly 1 prismatic joint
- nominal overall envelope at q=0: approximately 60 x 60 x 61 mm
- required stroke: 12 mm
- required output: separate-part STEP assembly, STL/GLB handoff, and URDF with
  `mounting_base` as root and `plunger_slide` as the only active joint.

The design is accepted when its coarse external form and functional geometry
match these requirements. Small dimensional deviations are acceptable if the
guide remains coaxial, the slider has useful clearance, the head stays outside
the sleeve, the 12 mm stroke is collision-free, and the generated URDF retains
one valid prismatic DOF.
