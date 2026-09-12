# Quadruped Industrial Inspection Robot — Assembly Request

Create a visually sophisticated four-legged industrial inspection robot with
a low armoured torso, four articulated three-joint legs, compact feet, twelve
visible motor housings, a rotating sensor mast, and a pan/tilt stereo camera
head. The robot should resemble a rugged inspection platform used in power
plants or factories, with layered panels, protective corner rails, open-truss
limbs, symmetric joints, and a premium engineered appearance.

All dimensions are millimetres. This is an open-tree articulated assembly.
Do not model parallel linkages, tendons, cables, springs, loose bolts, or a
closed-loop suspension. Each moving part has exactly one parent mate.

## Overall form

- Torso approximately 420 X x 220 Y x 120 Z.
- Nominal standing height approximately 430.
- Four legs at front/rear and left/right corners.
- Hip yaw joints rotate about Z; hip pitch and knee pitch rotate about Y in
  each leg's local chain.
- Feet remain visually below the torso and do not intersect each other.
- Sensor mast centered on the torso, not offset to one side.

## Required part tree — exactly 27 parts

```text
torso_base
  -> torso_top                          rigid
      -> sensor_mast                    rigid
          -> sensor_pan                 revolute Z
              -> sensor_tilt_yoke       rigid
                  -> sensor_head        revolute Y
  -> hip_fl_yaw                         revolute Z
      -> thigh_fl                       revolute Y
          -> shin_fl                    revolute Y
              -> foot_fl                rigid
  -> hip_fr_yaw                         revolute Z (reuse hip_fl_yaw)
      -> thigh_fr                       revolute Y (reuse thigh_fl)
          -> shin_fr                    revolute Y (reuse shin_fl)
              -> foot_fr                rigid (reuse foot_fl)
  -> hip_rl_yaw                         revolute Z (reuse hip_fl_yaw)
      -> thigh_rl                       revolute Y (reuse thigh_fl)
          -> shin_rl                    revolute Y (reuse shin_fl)
              -> foot_rl                rigid (reuse foot_fl)
  -> hip_rr_yaw                         revolute Z (reuse hip_fl_yaw)
      -> thigh_rr                       revolute Y (reuse thigh_fl)
          -> shin_rr                    revolute Y (reuse shin_fl)
              -> foot_rr                rigid (reuse foot_fl)
  -> front_bumper                       rigid
  -> rear_bumper                        rigid (reuse front_bumper)
  -> side_guard_left                    rigid
  -> side_guard_right                   rigid (reuse side_guard_left)
  -> belly_guard                        rigid
```

If schema counting shows a discrepancy, preserve this explicit tree and make
`expected_part_count` equal its actual number of physical instances; do not
drop a leg or add decorative free parts merely to hit a number.

## Reusable geometry strategy

Only generate one template for each repeated family:

- one hip yaw carrier, reused four times;
- one thigh, reused four times;
- one shin, reused four times;
- one foot, reused four times;
- one bumper, reused twice;
- one side guard, reused twice.

Mirroring must be performed through assembly placement, not by asking the
part generator to produce four subtly different copies. Every reused instance
remains an independent link.

## Part requirements

### torso_base

One connected armoured chassis about 420 x 220 x 95, centered in XY with
bottom Z=0. Use beveled corners, recessed side service panels, four reinforced
hip mounting towers, a hollow-looking underside recess, and longitudinal top
ribs. The four hip attachment axes must be symmetric at approximately
X=+-155, Y=+-100. Keep the root body connected.

### torso_top

Separate rigid top shell about 350 x 170 x 35, with a central sensor mast
seat, six cooling slots and four corner bosses. It must sit centrally on the
torso and not cover hip joint clearances.

### belly_guard

A separate rigid shallow skid plate mounted under the torso, approximately
300 x 130 x 12, with a faceted nose, two long recessed channels, and six
mounting holes. It must be centered, remain above the feet, and touch the
torso only at its intended mounting pads.

### hip yaw carrier

Compact layered rotary module R32 and about 38 high around local Z. Include a
lower annular flange, central shaft/bore geometry, and a lateral Y-axis clevis
for the thigh. It must be one connected component; avoid floating annular
rings. Hip yaw limits -35..35 degrees.

### thigh template

Open-truss link length about 150 between joint centres, with a mature central
tongue at the hip and distal fork at the knee. Prefer a scaled
`y_axis_truss_clevis_link`:

```json
{"name":"y_axis_truss_clevis_link","params":{"length":150,"joint_radius":17,"fork_ear_thickness":7,"fork_ear_center_y":15,"tongue_thickness":20,"fork_bore_radius":5.5,"pin_radius":5.0,"pin_length":40,"rail_width_y":20,"rail_height":7,"rail_center_z":14}}
```

Keep large truss windows. Hip pitch limits -70..75 degrees.

### shin template

Use the same thigh geometry by reuse if proportions remain acceptable. If
necessary for a more animal-like silhouette, use a separate 165 mm template,
but still reuse it across all four legs. Knee limits 10..135 degrees.

### foot template

Compact connected foot about 105 x 62 x 28 with a rounded toe, raised ankle
pad, two side ribs, and a shallow sole tread pattern cut into one solid. It is
rigid to the shin for the Beta showcase; do not add an unsupported ankle DOF.

### motor housings

Motor-like cylindrical bosses should be integrated into hip/thigh/shin parts
or represented as rigid fused features. Do not create twelve separate motor
pod parts beyond the declared tree. Every visible cylinder must align with an
actual joint axis.

### sensor mast and head

Mast is a centered tapered post, about 130 tall, with two side ribs. Sensor
pan is a Z-axis turntable. Tilt yoke is a small U-frame with Y-axis bore.
Sensor head is a symmetric rectangular pod with two front apertures and a
central depth-camera opening. Pan limits +-170; tilt limits -55..45 degrees.

## Static pose

- front and rear thighs angle slightly outward/downward;
- knees bend so shins descend toward four separated feet;
- left/right placements mirror visually but reuse identical part geometry;
- all feet are below the body and approximately coplanar;
- no leg passes through the torso at nominal pose;
- sensor head faces +X.

## Acceptance

- assembly_name: `quadruped_industrial_inspection_robot`
- four complete legs, each with yaw + hip pitch + knee pitch
- centered torso and sensor mast
- explicit limits on every revolute joint
- no missing/misaligned reused instances, no floating motor rings, no closed
  mate loops, and no merged adjacent links
- separate-part STEP, assembly STEP/STL/GLB, and URDF
- cosmetic deviations +-10% allowed; symmetry, link tree, axis directions,
  and recognizable quadruped form are mandatory.
