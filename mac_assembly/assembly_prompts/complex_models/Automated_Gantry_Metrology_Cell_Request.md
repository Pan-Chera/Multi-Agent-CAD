# Automated Three-Axis Gantry Metrology Cell — Assembly Request

Create a large, visually sophisticated industrial optical metrology gantry.
The machine must look like premium coordinate-measuring equipment: a broad
granite-style base, twin side columns, a high crossbeam, layered linear
carriages, a descending Z ram, a two-axis camera head, four symmetric light
pods, cable-chain support trays, access covers, ribs, and visible mounting
patterns. It is NOT a robot arm or gripper.

All dimensions are millimetres. The assembly must remain an open kinematic
tree. Do not model belts, wires, loose screws, closed-loop rails, or multiple
mates for the same moving part. Linear bearings and drives are represented by
semantic prismatic joints, not physical balls or lead screws.

## Required visual result

- Overall envelope approximately 760 X x 520 Y x 650 Z.
- Wide, low base with two tall towers near X=+-300.
- Crossbeam spans between towers at Z about 500.
- X carriage moves along the crossbeam.
- Y saddle projects forward from the X carriage.
- Z ram descends from the saddle.
- Compact pan/tilt optical head hangs at the ram tip.
- Four small light pods surround the camera symmetrically.
- Use dark recessed panels, raised ribs, bosses, and bolt-hole patterns as
  connected geometry; no disconnected decoration.

## Required part tree — exactly 18 parts

```text
machine_base
  -> left_column                         rigid
  -> right_column                        rigid (reuse left_column)
  -> rear_service_cabinet                rigid
  -> crossbeam                           rigid
      -> x_carriage                      linear X
          -> y_saddle                    linear Y
              -> z_ram                   linear Z
                  -> pan_rotor            revolute Z
                      -> tilt_yoke         rigid
                          -> camera_body   revolute Y
                              -> lens_bezel rigid
                              -> light_top  rigid
                              -> light_bottom rigid (reuse light_top)
                              -> light_left rigid (reuse light_top)
                              -> light_right rigid (reuse light_top)
  -> left_cable_tray                     rigid
  -> right_cable_tray                    rigid (reuse left_cable_tray)
```

Root link is `machine_base`. Exactly three linear joints and two revolute
joints. All other relationships are rigid. `right_column` reuses
`left_column`; three light pods reuse `light_top`; `right_cable_tray` reuses
`left_cable_tray`.

## Part geometry

### machine_base

One connected base/plinth, about 700 x 460 x 55. Use a broad rectangular slab
with chamfered corners, a smaller raised central work deck, two shallow side
rails, and eight real mounting holes. Top remains largely flat. Prefer v3 base
body for a premium layered shape; avoid a plain featureless box.

### left_column / right_column

Reusable tapered tower, about 95 x 125 x 445, local bottom at Z=0. Make one
connected hollow-looking housing with a wide foot, narrower upper body, two
vertical recessed channels, an outer raised rail, and triangular side ribs.
The copied right tower may be placed with a 180-degree yaw if needed, but its
geometry must not be regenerated.

### crossbeam

One connected trussed beam spanning about 640 along X, 100 along Y, and 110
high. Use upper and lower rails joined by at least five diagonal webs so large
side windows remain open. Add a continuous front linear-guide ridge and a
rear cable shelf. It must be rigidly supported by both visual towers, but the
mate graph remains a tree: give it one primary rigid mate to `left_column`;
the contact with `right_column` is geometric only and must not create a second
mate.

### x_carriage

Layered plate about 125 x 125 x 150, wrapping visually around the front of the
crossbeam without enclosing it. Include four symmetric bearing bosses and a
central reinforced pad. Static pose near world X=-80. Linear axis +X, limits
-190..190 mm.

### y_saddle

Short orthogonal slide about 105 x 190 x 80, projecting toward world -Y.
Include twin rails and a faceted central housing. Static position 0, linear
axis Y, limits -90..70 mm.

### z_ram

Tall narrow beam about 70 x 70 x 300 with four corner rails and two recessed
faces. Local long axis Z. It hangs downward from the saddle. Linear axis Z,
limits -180..40 mm. Use a placement convention that keeps its body clear of
the crossbeam throughout the sampled range.

### pan_rotor and tilt_yoke

`pan_rotor` is a thick annular or cylindrical turntable about R34 x 22 high,
with a central bore and layered outer flange. It rotates around Z with limits
-170..170 degrees. `tilt_yoke` MUST use the deterministic
`y_axis_tilt_yoke` builder with
`width=100, depth=124, height=70, top_thickness=10, cheek_thickness=18,
bore_radius=8, bore_z=-58, root_overlap=2`. It is a connected U-shaped fork
below the rotor. Its cheeks are separated along Y (not X), so a Y-axis bore
at X=0, Z=-58 passes coaxially through both. The 124 mm depth clears the
82 mm-wide camera body and its pivot stubs.

### camera_body and lens_bezel

Use `symmetric_sensor_pod` for camera_body if compatible:

```json
{"name":"symmetric_sensor_pod","params":{"body_length":92,"body_width":82,"body_height":68,"lens_radius":18,"boss_radius":27,"boss_depth":8,"pivot_radius":7.7,"pivot_length":10}}
```

The two optional pivot parameters are mandatory here: they create the real
coaxial Y-axis shaft faces consumed by the camera/yoke revolute mate. Do not
replace them with prose-only shaft requirements or a virtual axis-point mate.

Rotate/place it so the lens points generally toward world -Z in the nominal
pose. Its tilt joint axis is Y, limits -65..35 degrees. The lens bezel is an
additional thin annular fixed part, concentric with the camera aperture.

### light pods

Generate one compact rectangular/cylindrical light module, about 38 x 22 x
16, with a shallow front aperture and cooling ribs. Reuse it four times around
the camera. They are visual fixed children and must remain symmetric.

### cable trays

Generate one long shallow U-channel about 250 x 28 x 18. Reuse it on both
sides of the base/columns. Place their centres near X=+-200, Y=-200 so the
250 mm trays remain inside the 700 mm-wide base envelope. Keep them connected
and do not model flexible cable.

## Joint conventions

- X/Y/Z linear axes are expressed in the fixed parent's local frame.
- Pan axis is Z; camera tilt axis is Y.
- Write explicit lower and upper limits in the actual mate fields.
- FACE anchors are acceptable only on unrotated root-level rigid parts;
  moving-chain anchors should use SELECTOR or AXIS_POINT.
- Do not create closed-loop mates for the second column or rail contacts.
- Static pose must be collision-free and visually centered over the work deck.

## Required outputs and acceptance

- assembly_name: `automated_gantry_metrology_cell`
- expected_part_count: 18
- exactly 5 active joints: X/Y/Z linear, pan, tilt
- separate-part STEP, STL, GLB, and URDF
- no missing towers, no duplicate camera, no blocked open truss windows
- no disconnected decorative solids
- all copied parts remain independent assembly instances
- acceptable cosmetic deviation: +-10%; mandatory topology and joint axes
  must remain as specified.
