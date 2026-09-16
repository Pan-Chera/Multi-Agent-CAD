# Advanced Four-Axis Optical Inspection Robot — Assembly Request

Create a visually sophisticated desktop industrial optical-inspection robot.
It is NOT a gripper and has no fingers or jaws. It consists of a heavy
electronics pedestal, azimuth turntable, tall shoulder yoke, two articulated
lattice booms, a compact wrist carrier, and a camera/sensor pod. The visual
language should resemble a premium industrial robot: layered housings,
prominent round motor pods, forked joint cheeks, open truss windows, access
covers, mounting-hole patterns, and a large front lens bezel.

All dimensions are millimetres. Small cosmetic deviations are acceptable,
but the open kinematic tree, joint locations, principal dimensions, reusable
parts, connected solids, and collision-free static pose are mandatory. Do
not create a gripper, hand, claws, gears, belts, cables, springs, closed-loop
linkages, loose screws, or disconnected decorative bodies.

## Intended appearance and static pose

The root stands upright on a broad rectangular base. A square electronics
pedestal supports a round azimuth bearing. Above it, a U-shaped shoulder yoke
holds an upper boom that rises rear-to-front. The forearm folds downward from
the elbow, and the wrist projects nearly horizontally, giving a compact
inverted-Z robot silhouette. A faceted camera pod with a circular lens points
along the wrist direction. Cylindrical motor pods sit outside the shoulder and
elbow joints. A smaller bearing end cover opposite the elbow motor creates a
layered, engineered joint stack.

The nominal static joint angles are:

- azimuth: +18 degrees about world/local Z;
- shoulder pitch: -55 degrees about the yoke's Y axis;
- elbow pitch: +100 degrees relative to the upper boom;
- wrist pitch: -45 degrees relative to the forearm.

## Required part tree — exactly 13 parts

```text
mounting_base
  -> pedestal_housing                         rigid
      -> pedestal_top                         rigid
          -> azimuth_rotor                    revolute Z
              -> shoulder_yoke                rigid
                  -> upper_boom               revolute Y
                      -> forearm_boom          revolute Y
                          -> wrist_carrier     revolute Y
                              -> sensor_pod    rigid
                                  -> lens_bezel rigid/coaxial
                      -> upper_cover           rigid
                      -> elbow_motor_pod       rigid/coaxial
                  -> shoulder_motor_pod        rigid/coaxial
```

- `forearm_boom` MUST reuse `upper_boom` geometry using
  `reuses_part_id="upper_boom"`.
- `elbow_motor_pod` MUST reuse `shoulder_motor_pod` geometry.
- Exactly 4 revolute joints and 8 rigid/fixed attachments.
- Root link is `mounting_base`.
- The mate graph must be a tree. Motor pods and covers are visual/mechanical
  children only and must not create a second mate to another link.

## Shared coordinate conventions

- Root Z is upward.
- The articulated arm lies primarily in the XZ plane.
- Shoulder, elbow, and wrist pitch axes are +Y.
- Every pitch-link local long axis is +X.
- For booms, root hinge centre is local `(0,0,0)` and distal hinge centre is
  local `(180,0,0)`.
- Fixed/fork through-bores at pitch joints are cylinders along local Y with
  radius 6 mm. Each moving central tongue carries an integrated Y-axis pin
  of radius 5.5 mm, giving 0.5 mm radial running clearance.
- All explicit joint limits must be written to `limit_lower` and
  `limit_upper`, not merely mentioned in notes.

## Part specifications

### 1. `mounting_base`

Use deterministic builder:

```json
{"name":"mounting_plate","params":{"width":160,"depth":140,"thickness":10,"hole_radius":4,"hole_dx":65,"hole_dy":55,"central_hole_radius":0}}
```

Local bbox X=-80..80, Y=-70..70, Z=0..10. Four real corner mounting
holes are visible. Its top face supports the pedestal.

### 2. `pedestal_housing`

Use deterministic builder:

```json
{"name":"hollow_box","params":{"outer_w":104,"outer_d":94,"outer_h":72,"wall_t":5,"floor_t":6}}
```

Local bbox X=-52..52, Y=-47..47, Z=0..72. It is an open-top electronics
enclosure. In assembly its bottom sits on mounting_base top, so world
Z=10..82.

### 3. `pedestal_top`

Use deterministic `mounting_plate` builder:

```json
{"name":"mounting_plate","params":{"width":108,"depth":98,"thickness":6,"hole_radius":3,"hole_dx":45,"hole_dy":40,"central_hole_radius":10.4}}
```

Local bbox X=-54..54, Y=-49..49, Z=0..6. It closes the pedestal at world
Z=82..88 and exposes a central Z-axis bore R10.4 plus four bolt holes.

### 4. `azimuth_rotor`

Use deterministic `bushing` builder:

```json
{"name":"bushing","params":{"outer_radius":38,"inner_radius":10.4,"length":12}}
```

Local bbox X=-38..38, Y=-38..38, Z=0..12. This thick annular turntable
rests on pedestal_top and rotates about +Z. Its top is world Z=100.

### 5. `shoulder_yoke`

Generate one connected, watertight U-shaped structural solid. Use simple
orthogonal boxes and horizontal bores; do not generate separate bodies.

- Bottom bridge: X=-42..42, Y=-30..30, Z=0..14.
- Two upright cheeks rise from the bridge:
  - rear/left-Y cheek: X=-22..22, Y=-30..-16, Z=14..86;
  - front/right-Y cheek: X=-22..22, Y=16..30, Z=14..86.
- Connect each cheek to the bridge with a triangular gusset or sloped rib;
  ribs must be fused into the bridge and cheeks.
- Shoulder bore: radius 6 mm, axis along Y, centred at local `(0,0,74)`,
  cutting through both cheeks.
- Add a larger circular external boss R14 around that bore on the outer face
  of each cheek if convenient; bosses must be fused.
- Leave the central gap Y=-16..16 open for the boom.
- Local overall bbox approximately X=-42..42, Y=-30..30, Z=0..88.
- Bottom face Z=0 mounts rigidly to azimuth_rotor top.

Prefer v3 `base_body` mode so the U-frame remains visually detailed. The
horizontal shoulder bore may be part of the base body or a deterministic
`through_bore` feature with direction `+y` at `(0,0,74)`.

### 6. `upper_boom` — reusable lattice-boom template

Use this deterministic builder exactly (do not send this part to the
single-part LLM pipeline):

```json
{"name":"y_axis_truss_clevis_link","params":{"length":180,"joint_radius":18,"fork_ear_thickness":8,"fork_ear_center_y":16,"tongue_thickness":22,"fork_bore_radius":6,"pin_radius":5.5,"pin_length":44,"rail_width_y":22,"rail_height":8,"rail_center_z":14}}
```

It produces a single connected, watertight trussed beam extending along +X.

- Root hinge is a central tongue carrying an integrated R5.5 pin, 44 mm long
  along Y, centred at local `(0,0,0)`. It is not an empty annular ring.
- Distal hinge is a mature two-ear clevis at `(180,0,0)`: two radius-18 ears,
  each 8 mm thick, centred at Y=-16 and Y=+16. Keep the central Y=-12..12
  gap clear so the next link's 22 mm tongue fits with 1 mm clearance per side.
- Upper rail: X=16..164, Y=-13..13, Z=+10..+18.
- Lower rail: X=16..164, Y=-13..13, Z=-18..-10.
- At least three diagonal or vertical webs connect upper and lower rails,
  leaving two or three large open window cut-outs visible from the side.
- Add shallow raised longitudinal ribs on the Y outer faces, but all material
  must remain connected to the main beam.
- Overall local bbox approximately X=-18..198, Y=-22..22 (including pin),
  Z=-18..18.
- Root and distal bore centres must remain exactly `(0,0,0)` and
  `(180,0,0)`.
- Do not fill the truss windows with a solid slab.

Do not substitute `clevis_link`: that existing builder has a Z-axis hinge,
whereas this robot requires Y-axis hinges. Geometry must be reused without
mirroring.

### 7. `forearm_boom`

Exact geometry reuse of `upper_boom`; do not regenerate it. It is a separate
assembly instance with its own joint pose.

### 8. `wrist_carrier`

Use this deterministic builder exactly:

```json
{"name":"y_axis_wrist_carrier","params":{"joint_radius":18,"tongue_thickness":22,"pin_radius":5.5,"pin_length":44,"payload_x":78}}
```

- Root hinge is a central tongue centred at local `(0,0,0)`, radius 18 in XZ,
  width 22 along Y, with an integrated R5.5 Y-axis pin. It fits inside the
  forearm clevis R6 bores.
- Tapered/stepped body extends along +X to X=78.
- Body should transition from 30 mm high near the hinge to 46 mm high at the
  payload flange, with chamfered or faceted outer corners.
- Rectangular payload mounting face at X=78, approximately 46 high x 54 wide.
- Include four small visible through-holes R2.5 near the payload-face corners
  if robust, but keep the body connected.
- Local bbox approximately X=-18..78, Y=-27..27, Z=-24..24.
- The sensor pod mounts to the +X payload face.

### 9. `sensor_pod`

Use this deterministic builder exactly:

```json
{"name":"symmetric_sensor_pod","params":{"body_length":68,"body_width":68,"body_height":60,"lens_radius":15,"boss_radius":22,"boss_depth":6}}
```

It generates a premium camera/sensor enclosure as one connected solid.

- Main housing X=0..68, Y=-34..34, Z=-30..30.
- Rear mounting face at local X=0.
- Front face at X=68 carries a centred circular lens opening/boss axis +X.
- Make the enclosure symmetric about both Y=0 and Z=0: matching top/bottom
  rails, matching left/right side ribs, and four symmetrically placed front
  fastener holes. No disconnected details.
- Main lens opening radius 15 mm centred at `(68,0,0)`, axis X. A shallow
  annular boss around it may extend to X=74.
- Keep enough material around the aperture; one connected shell or solid.
- Local bbox approximately X=0..74, Y=-37..37, Z=-33..33.

### 10. `lens_bezel`

Use deterministic `bushing` builder:

```json
{"name":"bushing","params":{"outer_radius":22,"inner_radius":15,"length":10}}
```

Its local cylinder axis is Z. During rigid/coaxial assembly align local Z to
the sensor pod's +X lens axis. Seat the bezel against the front face, outside
the housing, without inserting the R22 body into the R15 opening.

### 11. `shoulder_motor_pod` — reusable motor-pod template

Use deterministic Y-axis solid motor-cover builder:

```json
{"name":"y_axis_motor_pod","params":{"radius":18,"thickness":12,"boss_radius":11,"boss_thickness":3}}
```

Its local axis is already Y. Never use a default-Z `bushing` here: a rigid
mate does not rotate that geometry and would place an apparent ring above or
below the hinge. This is a solid servo cover with a shallow axle recess.

### 12. `elbow_motor_pod`

Exact reuse of `shoulder_motor_pod`. Mount coaxially outside the upper
boom's positive-Y face at its distal joint centre `(180,0,0)`.

### 13. `upper_cover` — elbow bearing end cover

Keep the legacy part ID `upper_cover`, but model it as a thin solid bearing
cap opposite the elbow motor pod. Use this deterministic builder:

```json
{"name":"y_axis_bearing_cap","params":{"radius":14,"thickness":4,"axle_radius":6,"axle_height":2}}
```

Its local axis is already Y. Mount it flush outside the negative-Y fork ear;
it must look like a thin axle cap, not a second empty hinge ring. Together
with the larger positive-Y motor pod it creates an asymmetric industrial
joint stack. The authoritative 13
parts are: mounting_base, pedestal_housing, pedestal_top, azimuth_rotor,
shoulder_yoke, upper_boom, forearm_boom, wrist_carrier, sensor_pod,
lens_bezel, shoulder_motor_pod, elbow_motor_pod, upper_cover.

## Authoritative mates — exactly 12

1. `pedestal_mount`: rigid/face_to_face, mounting_base top Z=10 to
   pedestal_housing bottom Z=0, centred XY.
2. `top_cover_mount`: rigid/face_to_face, pedestal_housing top Z=72 to
   pedestal_top bottom Z=0, centred XY.
3. `azimuth_joint`: revolute about Z, pedestal_top central bore R10.4 to
   azimuth_rotor inner bore R10.4. Static angle +18 deg. Place rotor bottom
   on pedestal_top top. Limits -170..+170 deg.
4. `yoke_mount`: rigid/face_to_face, azimuth_rotor top Z=12 to shoulder_yoke
   bottom Z=0, centred.
5. `shoulder_pitch`: revolute about Y. The yoke selector resolves one bore at
   Y=-30; use axial_offset +30 mm to centre the upper-boom tongue at Y=0
   between the yoke cheeks. Static angle -55 deg.
   Limits -75..+55 deg.
6. `elbow_pitch`: revolute about Y. The upper-boom fork selector resolves its
   negative-Y R6 ear bore at Y=-15; align the forearm root R5.5 pin and use
   axial_offset +15 mm to centre it at Y=0. Static angle +100 deg.
   Limits -120..+120 deg.
7. `wrist_pitch`: revolute about Y. The forearm fork selector resolves its
   negative-Y R6 ear bore at Y=-15; align the wrist root R5.5 pin and use
   axial_offset +15 mm to centre it at Y=0. Static angle -45 deg.
   Limits -90..+90 deg.
8. `sensor_mount`: rigid, wrist_carrier payload face local X=78 to sensor_pod
   rear face local X=0. Preserve orientation so sensor points along wrist +X.
9. `lens_mount`: rigid/coaxial, sensor_pod front lens axis at `(68,0,0)` to
   lens_bezel axis. Align bezel axis to +X and place bezel outside the front
   face, spanning approximately sensor-local X=68..78.
10. `shoulder_motor_mount`: rigid/coaxial, shoulder_yoke selected bore axis at
    Y=-30 to the motor pod's Y-axis cylindrical surface. Use axial_offset
    +73 mm so the 12 mm housing sits flush outside the positive-Y cheek.
11. `elbow_motor_mount`: rigid/coaxial. The selected negative fork-ear bore is
    at Y=-15; use axial_offset +41 mm so the Y-axis motor housing centre is
    Y=+26 and its inner face is flush with the fork's Y=+20 outer face.
12. `upper_cover_mount`: rigid/coaxial. From the selected fork bore at Y=-15,
    use axial_offset -7 mm so the thin Y-axis bearing cap centre is Y=-22 and
    its inner face is flush with the fork's Y=-20 outer face.

For all bore mates use SELECTOR cylinder anchors with explicit axis, radius,
and target coordinates to disambiguate. For face mates use explicit face or
plane selectors. Preserve the exact parent/child order above. All moving
subtrees must inherit their parent's rotations.

## Collision and quality requirements

- Every part is one connected watertight solid; reused parts remain separate
  labelled instances.
- Keep at least 1 mm clearance at every clevis/tongue hinge and centre the
  upper_boom between the shoulder-yoke cheeks.
- Intended shaft/bore occupancy and flush mounting contact are allowed;
  unintended volumetric intersections deeper than 0.3 mm are forbidden.
- Motor pods sit outside the positive-Y faces and must not overlap the moving
  booms or yoke cheeks.
- In an oblique view each fork has two ears around one continuous pin. Reject
  empty, vertically oriented, or apparently floating annular rings.
- Lens bezel sits outside the sensor front face and leaves its R15 aperture
  open.
- Joint sweep QA must remain collision-free for at least +/-30 degrees around
  each static pose. If the requested static fold would cause a collision,
  preserve topology and adjust only a cosmetic housing dimension by a few mm.
- Preserve all four active revolute joints in URDF with explicit limits.
- Do not merge the complete robot into a single solid.

## Expected scale and output

- Expected static envelope approximately 470 x 150 x 340 mm. This is an
  estimate; accept +/-20 percent in X and Z and let the 160 x 140 mounting
  base dominate minimum X/Y extents.
- assembly_name: `advanced_optical_inspection_robot`
- expected_part_count: 13
- expected_mate_count: 12
- active_DOF: 4 revolute joints
- required outputs: STEP, STL, GLB, URDF, manifests, and four saved views.

Reject any result that resembles a gripper, omits the U-yoke or sensor pod,
turns the lattice booms into plain rods, loses a hinge, uses continuous joints
without limits, creates a closed mate loop, or stacks all parts in one plane.
