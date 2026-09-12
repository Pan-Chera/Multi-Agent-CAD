# Telescopic Cinema Robot Crane with Stabilized Camera — Assembly Request

Create a large premium robotic cinema crane that looks mechanically advanced
from every angle. It must have a heavy wheeled pedestal, azimuth turntable,
forked elevation joint, two long open-truss boom stages, rear counterweight
pack, telescoping nose carrier, and a three-axis stabilized camera gimbal.
The result should resemble professional motion-control equipment rather than
a generic industrial arm.

All dimensions are millimetres. Use an open tree. Do not model cables, chains,
hydraulic hoses, loose screws, or closed-loop bracing. Decorative motor pods
are rigid children, never extra joints.

## Target envelope and pose

- Nominal static-pose envelope approximately 1050 X x 210 Y x 790 Z.
- The larger swept workspace may approach 1100 X x 560 Y x 720 Z; do not
  write this swept workspace into `overall_envelope_mm`, which QA compares
  against one static assembly pose.
- Base centered at origin, Z upward.
- Main crane operates primarily in the XZ plane.
- Nominal azimuth +15 degrees about Z.
- Main elevation visually rises about +10 degrees from the base toward +X. Under
  build123d's current Y-rotation convention this must be encoded as
  `angle_deg=-10` (positive code angle slopes a +X boom downward).
- Elbow/nose pitch -12 degrees about Y.
- Gimbal pan/roll/tilt angles near zero for a level camera.
- Long open trusses and a compact camera head create a strong asymmetric
  silhouette, balanced by a layered rear counterweight.

## Required part tree — exactly 20 parts

```text
floor_plate
  -> pedestal_housing                    rigid
      -> azimuth_rotor                   revolute Z
          -> elevation_yoke              rigid
              -> primary_boom            revolute Y
                  -> secondary_boom       revolute Y
                      -> telescope_carrier linear X
                          -> gimbal_pan   revolute Z
                              -> gimbal_roll_frame revolute X
                                  -> camera_tilt_yoke rigid
                                      -> camera_body revolute Y
                                          -> lens_bezel rigid
                  -> counterweight_frame rigid
                      -> counterweight_left rigid
                      -> counterweight_right rigid (reuse left)
                  -> elevation_motor_pod rigid
                  -> bearing_cap rigid
          -> azimuth_service_cover rigid
  -> wheel_left rigid
  -> wheel_right rigid (reuse left)
```

Exactly seven active joints: azimuth, main elevation, secondary pitch,
telescope extension, gimbal pan, roll, and camera tilt.

## Geometry requirements

### floor_plate and pedestal_housing

`floor_plate`: use `mounting_plate`, about 220 x 180 x 14, with four mounting
holes. Add two fixed wheel-like transport rollers only for visual complexity;
they do not move in the exported mechanism.

`pedestal_housing`: one connected faceted cabinet about 150 x 140 x 180,
narrower at the top, with symmetric recessed side panels, horizontal cooling
slots, corner ribs, and a removable-looking but fused front service panel.

### azimuth_rotor and elevation_yoke

The azimuth rotor is a layered annular platform R65, total height about 28,
with a central guide bore and bolt pattern. The elevation yoke is a mature
U-shaped structure with two thick cheeks, a clear central gap of at least
48 mm, and a coaxial Y-axis bore at height about 120 above the rotor (for a
yoke spanning local Z=-140..0, use bore_z=-20, not -115). Both
cheeks must be connected by a bottom bridge and gussets.
Set `bridge_at_bottom=true` for this elevation yoke so the opening faces
upward and the counterweight can sweep behind the pivot without striking a
top crossbar. Camera tilt yokes may keep the default inverted-U orientation.

### primary_boom / secondary_boom

Use this deterministic template for `primary_boom`:

```json
{"name":"y_axis_truss_clevis_link","params":{"length":300,"joint_radius":25,"fork_ear_thickness":10,"fork_ear_center_y":22,"tongue_thickness":30,"fork_bore_radius":8,"pin_radius":7.4,"pin_length":86,"rail_width_y":30,"rail_height":10,"rail_center_z":24}}
```

Do **not** reuse the complete `primary_boom` for `secondary_boom`: its 86 mm
root pin is sized for the wide elevation yoke and is incorrect at the much
narrower boom-to-boom clevis. Generate the secondary with the same template
and 300 mm truss dimensions, but use `pin_length=56`, `pin_cap_radius=10`, and
`pin_cap_thickness=3`. This produces a central tongue between the primary
boom's two distal ears, a pin through both R8 bores, and visible retained axle
heads immediately outside the fork. The secondary pitch mate must select the
primary distal R8 bore at local X=300 and the secondary root R7.4 pin at local
X=0. Preserve open truss windows; never replace either boom by a solid slab.

### telescope_carrier

A 260 mm long nested-looking beam sliding along local +X. It is one connected
part with a central rectangular tube, two raised rails, three inspection
windows, and a reinforced gimbal flange. A thin perforated flange alone is
**not** a valid final revolute joint. Use the workflow's existing standard
feature operators on top of the builders: add a `clevis_fork` to the flat
carrier +X end at `[260,0,10]`, direction `+x`, pin_axis `z`; add the matching
`clevis_tongue` to the gimbal pan at `[-22,0,2]`, direction `-x`, pin_axis
`z`. Both use ear_length=30, ear_width=20, bore_radius=6,
tongue_thickness=8, bar_thickness=16, clearance_side=0.2. The Mating
Architect must select the two R6 bores. The pin remains implicit according
to the common clevis convention. Static extension 80 mm, limits 0..180 mm.
The carrier must not pass through a closed solid end cap.

### counterweight assembly

`counterweight_frame` uses the deterministic `crane_counterweight_frame`
builder: it projects about 170 mm behind the primary pivot in local -X and
has an integral 50 mm tongue overlapping the primary boom. It carries two reused
counterweight blocks on opposite Y sides. Each block should look layered,
with chamfered edges and a recessed lifting slot, but remain one connected
solid.
Place the frame's forward end at least 80 mm behind the elevation pivot so it
clears the yoke throughout the sampled elevation range.

### gimbal

Use three visibly distinct nested frames:

- pan rotor: compact vertical Z-axis ring/platform with a real horizontal
  bridge and two bearing towers supporting the roll axis; use the deterministic
  `gimbal_pan_yoke` builder rather than a bare standoff disk; its roll axis is
  elevated about 60 mm above the pan disk to clear the telescope carrier;
- roll frame: rectangular open cage rotating around X; use the deterministic
  `gimbal_roll_cage` builder at about 100 X x 80 Y x 70 Z so neither end hoop
  cuts through the camera body or lens;
- camera tilt yoke: U-shaped fork with Y-axis bore; use `y_axis_tilt_yoke`
  with about 70 X x 50 Y x 50 Z, 10 mm cheeks, and a bore at its mid-height so the camera
  and lens clear the bridge during tilt;
- camera body: symmetric optical pod with large lens bezel.

Each moving frame has exactly one primary mate. Keep at least 4 mm clearance
between nested frames over +-25 degrees sampled motion. Do not merge the
camera with any gimbal frame.

### motor pod, bearing cap, covers and wheels

Use `y_axis_motor_pod` and `y_axis_bearing_cap` where appropriate for the
main elevation joint. They are fixed to the yoke or boom side and must not
appear as redundant annular rings floating above the real hinge. The two
transport wheels reuse one template and remain rigid for this showcase.
Place the azimuth service cover on a lateral side of the rotor, outside the
primary boom's XZ sweep corridor.

## Joint requirements

- azimuth: Z, limits -170..170 deg
- primary elevation: Y, limits -35..70 deg
- secondary pitch: Y, limits -95..95 deg
- telescope extension: local X, limits 0..180 mm
- gimbal pan: Z, limits -170..170 deg
- gimbal roll: X, limits -45..45 deg
- camera tilt: Y, limits -75..55 deg

Use SELECTOR cylindrical anchors for bores and shafts. Write limits in actual
fields. Moving subtrees must follow parent rotations. No second mate is allowed
for visual contacts or counterweights.

## Required outputs and rejection rules

- assembly_name: `telescopic_cinema_robot_crane`
- expected_part_count: 20
- separate STEP parts, assembly STEP/STL/GLB, and URDF
- reject solid-filled trusses, blocked gimbal frames, duplicated camera,
  offset base attachment, floating motor rings, wrong joint axes, or merged
  moving parts
- cosmetic dimensions may vary +-10%, but the recognizable crane silhouette,
  seven-joint tree, and independent camera gimbal are mandatory.
