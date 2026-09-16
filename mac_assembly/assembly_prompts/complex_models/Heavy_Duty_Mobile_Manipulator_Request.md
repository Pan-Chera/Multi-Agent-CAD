# Consolidated Heavy-Duty Mobile Manipulator — Assembly Request

Create a visually advanced autonomous mobile manipulation platform with an
armoured wheeled chassis, layered electronics deck, rotating shoulder turret,
two articulated open-frame arm links, a compact roll wrist, an integrated
tool-changing inspection head, and a removable lidar module.

This is a showcase of **complex parts assembled into a compact real
mechanism**. Do not inflate the assembly by turning every cover, wheel, camera,
motor cap, bolt, rib, or decorative panel into a separate part. A rigid visual
subassembly must be fused into its owning part. Only bodies that must move
relative to one another, plus two meaningful removable rigid modules, are
separate parts.

All dimensions are millimetres. The assembly is an open kinematic tree. It is
not intended to simulate vehicle locomotion: the four visible wheel forms are
fixed, fused chassis geometry. Do not create steering, suspension, gear,
belt, cable, spring, or closed-loop linkage parts.

## Target silhouette and envelope

- Nominal envelope in the delivered sideways-arm pose: approximately
  620 X x 760 Y x 850 Z.
- The vehicle faces world +X. World +Z is up and Y is left/right.
- Chassis is low, wide, centred, and approximately 560 x 430 x 165.
- The turret is centred on the electronics deck, never side-mounted.
- The arm uses a compact elevated S-fold: shoulder rises forward, elbow folds
  back slightly, and the inspection tool faces generally forward.
- Four large fixed wheel forms, open arm windows, layered armour, bearing
  bosses, recessed service panels, and sensor apertures must make the model
  look substantially more detailed than a collection of primitive blocks.
- Complexity must come from geometry within the seven parts, not from a high
  part count.

## Required part tree — exactly 7 parts

```text
chassis_body
  -> electronics_deck                 rigid
      -> turret_shoulder_module       revolute Z
          -> upper_arm                revolute Y
              -> forearm              revolute Y
                  -> wrist_tool_head  revolute X
      -> lidar_sensor_module          rigid
```

Exactly four active joints:

1. deck to turret: azimuth about Z;
2. turret to upper arm: shoulder pitch about Y;
3. upper arm to forearm: elbow pitch about Y;
4. forearm to wrist/tool head: roll about X.

Do not add moving wheels, a separate tool-lock ring, separate cameras, separate
motor pods, or hidden joints. Do not merge any of the four moving interfaces.

## Complexity allocation

The following operation counts are guidance for the single-part workflow, not
additional assembly parts. Prefer repeated and symmetric features over many
unrelated free-form details.

| Part | Complexity | Intended feature budget |
|---|---|---|
| `chassis_body` | high but repetitive | 18–26 meaningful operations |
| `electronics_deck` | medium | 10–16 operations |
| `turret_shoulder_module` | medium-high | 12–18 operations |
| `upper_arm` | medium-high | 10–16 operations |
| `forearm` | medium-high | 10–16 operations |
| `wrist_tool_head` | high but compact | 16–24 operations |
| `lidar_sensor_module` | medium | 8–12 operations |

Use boxes, cylinders, extrusions, through-cuts, mirrors, and linear/circular
patterns wherever possible. Avoid helical threads, organic lofts, arbitrary
splines, thin sheet-metal folds, and decorative solids that touch only at an
exact coplanar face. Every additive feature must overlap its owning body by at
least 0.5 mm so each exported part is one connected watertight solid.

## Joint-generation rule — mandatory

Do not ask the free-form single-part coder to invent hinge hardware. Generate
the complex base bodies with the normal single-part workflow, then use the
assembly workflow's existing deterministic feature operators for kinematic
interfaces:

- `clevis_fork` and `clevis_tongue` for the shoulder and elbow Y-axis joints;
- `knuckle_ear` or another existing coaxial cylindrical feature for the X-axis
  wrist roll;
- selector anchors on the actual cylindrical bore or spindle faces;
- one fork and one tongue at every clevis joint, never fork/fork or
  tongue/tongue;
- the pin is implicit under the workflow's standard clevis convention; do not
  create a decorative floating pin part.

`builder + features` and `base_body + features` are both valid. Prefer
`base_body + features` for visually distinctive arm bodies so the single-part
workflow controls their appearance while deterministic operators guarantee
the joint geometry.

Use this shared shoulder/elbow clevis parameter set unless geometry requires a
documented change:

```json
{
  "ear_length": 34,
  "ear_width": 26,
  "bore_radius": 7.5,
  "tongue_thickness": 12,
  "bar_thickness": 24,
  "clearance_side": 0.3,
  "pin_axis": "y"
}
```

These are feature-operator parameters, not instructions to hand-code rings or
forks inside generated part scripts.

## Part 1 — chassis_body

Generate one high-detail connected chassis body, approximately 560 X x 430 Y x
165 Z, centred in X/Y with bottom at Z=0.

Construction is restricted to deterministic, sketch-primitive operations the
coder handles reliably. The chassis must be composed of:

- a **rectangular central tub** (520 x 350 x 95) built from boxes — its
  "armoured" look comes from **four fused stepped corner armour blocks**
  (stacked boxes of decreasing footprint at each corner), NOT from chamfered
  or octagonal cross-sections;
- only boxes, cylinders, standard through-cuts/pocket cuts, mirror, and
  linear/circular patterns for every other feature.

**Do NOT use arbitrary polygon sketches, custom-profile polylines, or free
splines anywhere in this part.** "Chamfered or stepped corners" means stepped
(fused rectangular blocks), never an octagon or other N-gon outline. This is
the single most important constraint for this part: a non-rectangular tub
profile is the known trigger for an unsupported placeholder that stalls the
Aider repair loop.

The single part must integrate:

- the rectangular central tub with four fused stepped corner armour blocks;
- a thick underside skid plate and exactly two fused front/rear bumper
  volumes. Each bumper is an axis-aligned `Box(30, 300, 45)` (X thickness,
  Y width, Z height), centred at `(265, 0, 42.5)` for the front and
  `(-265, 0, 42.5)` for the rear. They must be flush with the X end faces,
  centred on Y=0, and must never be interpreted as 480-mm-long plates,
  rotated fins, side outriggers, or diagonal panels;
- two recessed service channels on each side, mirrored about Y=0;
- four wheel-shaped side drums, approximately R68 x 38 wide, axes along Y,
  centred near X=+-195, Y=+-215, Z=70;
- short axle bosses that overlap both chassis and wheel drums, ensuring the
  wheel forms are fused rather than floating;
- stepped hubs and eight or twelve shallow tread grooves per wheel, created by
  repeated cuts or repeated fused pads rather than separate assembly parts;
- four **recessed tie-down pockets cut into the top deck itself**, centred near
  X=+-190, Y=+-125 within the 430 x 300 deck footprint. These are negative
  pocket cuts, not separate positive blocks, cylinders, rings, or lifting-eye
  solids. They must not create additional disconnected bodies;
- a flat, centred deck seat at the top.

All four wheels are visually present but rigid. Use symmetry and patterning;
do not independently improvise four different wheels. The chassis must remain
one solid after all grooves and recesses.

Recommended local bounds: X=-280..280, Y=-235..235, Z=0..165. The rigid deck
seat is a flat XY region approximately 430 x 300 at Z=155..165.

## Part 2 — electronics_deck

Generate a separate rigid electronics housing approximately 460 x 320 x 95,
centred in X/Y with bottom Z=0.

Include in this one connected part:

- a stepped lower plinth that seats fully on the chassis deck seat;
- sloped front corner armour and a shallow rear service recess;
- three vent banks made as repeated shallow slots, not loose grille bars;
- two longitudinal top rails and four fused corner protection bosses;
- a central annular turret seat, outer radius about 82, inner guide bore about
  R28, with enough surrounding material for a Z-axis revolute interface;
- a small flat mounting pad near the rear for the lidar module.

Keep the central turret axis at local X=0, Y=0. Avoid a tall solid box: layered
steps, recesses, rails, and vents should communicate internal equipment while
remaining manufacturable as one solid.

## Part 3 — turret_shoulder_module

Generate one connected moving module combining the turret rotor, shoulder
support structure, fixed-side motor housing, and shoulder clevis support.

Required geometry:

- lower Z-axis rotor: layered disks around R72–78, total height about 30, with
  a central R27 guide boss or bore matching the deck seat;
- two tapered vertical cheek supports rising to a shoulder-axis height of
  approximately 165 above the rotor bottom;
- a lower bridge and two diagonal gussets joining both cheeks to the rotor;
- integrated symmetric motor/bearing boss forms on the outer cheek faces;
- a flat region suitable for the deterministic shoulder `clevis_fork` feature;
- clear central opening around the moving upper-arm root.

The shoulder hinge axis is local/world Y in the nominal zero pose. The
deterministic fork must be centred above the turret, not attached to one side.
The whole turret/yoke/motor appearance belongs to this single part.

Recommended local bounds: approximately X=-90..90, Y=-90..90, Z=0..210.

## Part 4 — upper_arm

Generate a visually rich but robust single-piece arm base body extending along
local +X from its shoulder end to its elbow end, approximately 270 long, 66
wide in Y, and 86 high in Z.

The base body should contain:

- upper and lower longitudinal rails with 12–16 mm structural depth;
- three alternating diagonal webs, producing at least three genuine open
  triangular or trapezoidal windows through Y;
- locally thickened root and distal end blocks for attaching standard joint
  features;
- shallow longitudinal cable-channel recesses on both side faces;
- two or three small inspection holes or weight-relief pockets, mirrored where
  practical;
- no full solid slab filling the truss windows.

Apply a standard `clevis_tongue` at the local -X/root end for the shoulder and
a matching-family `clevis_fork` at the local +X/distal end for the elbow, both
with pin_axis Y. The body and each feature must fuse into one solid.

## Part 5 — forearm

Generate a distinct forearm rather than reusing `upper_arm`. It extends along
local +X, approximately 235 long, tapering visually from about 64 high at the
elbow to 48 high near the wrist while retaining broad flat attachment zones.

Include:

- two principal rails joined by two diagonal webs and one central vertical
  web, leaving at least two real open windows;
- an elbow-end reinforcement collar integrated into the rails;
- paired shallow service grooves and a small rectangular inspection hatch
  recess on both side faces;
- a reinforced cylindrical/box transition at the distal end for the X-axis
  roll bearing;
- no unsupported decorative ring.

Apply a standard Y-axis `clevis_tongue` at the -X/root end to mate with the
upper-arm fork. At the +X end apply the workflow's deterministic `knuckle_ear`
or another existing coaxial cylindrical feature suitable for an X-axis roll
joint. Do not hand-code an imitation hinge in the base-body script.

## Part 6 — wrist_tool_head

Generate one compact but high-detail connected part combining the moving wrist
spindle, tool-change flange, lock-lug appearance, protective cage, stereo
inspection cameras, and work light. Approximate total size 175 X x 155 Y x
125 Z; local +X is the tool-facing direction.

Integrate all of the following into one solid:

- an X-axis spindle/boss that mates physically with the forearm roll bearing;
- two stepped coaxial flange disks, outer radius about 48;
- six bolt holes on a circular pattern and three fused bayonet-style radial
  lugs spaced 120 degrees apart;
- a U-shaped protective bridge around the upper half of the flange;
- a transverse sensor bar fused to the bridge;
- two symmetric camera housings fused at Y=+-52, each with a recessed circular
  front aperture;
- a central rectangular work-light recess and two small protective ribs;
- an open central tool aperture that remains visibly unobstructed.

The cameras and locking details are geometry within this part, not separate
parts. Use mirror and circular-pattern operations extensively. The wrist's
root spindle is the only moving interface on this part.

## Part 7 — lidar_sensor_module

Generate one removable but rigidly mounted sensor module approximately 95 X x
95 Y x 105 Z. It consists of a chamfered lower electronics pod, a short neck,
and a layered cylindrical lidar head with four recessed horizontal scan-window
segments. Add four base mounting holes and two protective side ribs. Everything
must be fused into one solid; the scan windows are recesses, not detached dark
panels. The module is rigidly attached to the rear deck pad and has no joint.

## Mates, limits, and nominal pose

Use these six mates exactly:

| Mate | Type / axis | Limits | Nominal |
|---|---|---:|---:|
| chassis_body → electronics_deck | rigid, centred top seat | — | seated |
| electronics_deck → turret_shoulder_module | revolute Z | -160..160° | +100° |
| turret_shoulder_module → upper_arm | revolute Y | -70..55° | -55° |
| upper_arm → forearm | revolute Y | 0..135° | +35° |
| forearm → wrist_tool_head | revolute X | -180..180° | +8° |
| electronics_deck → lidar_sensor_module | rigid | — | rear-centred |

Use selector anchors for all cylindrical bores/spindles. The nominal arm pose
must be elevated and compact without intersecting the chassis, lidar, or deck.
Sample at least -25°, 0°, and +25° around each revolute joint during QA. Every
moving part has exactly one parent.

For this assembly, use the following deterministic seating contracts rather
than estimating absolute heights:

- `chassis_body -> electronics_deck`: align the largest upward chassis top
  plane with the deck bottom; no extra Z translation or axial offset.
- `electronics_deck -> lidar_sensor_module`: use the centred deck datum
  `axis_point(axis="z", offset_mm=47.5)` (the deck bbox is Z=0..95), align it
  with the lidar bottom face, then apply only `translation_mm=[-180,0,0]`.
  Do not use the centroid of a small subdivided top face as the fixed datum.
- `electronics_deck -> turret_shoulder_module`: for the R28 deck bore centred
  near local Z=89.5 and the R27 turret rotor centred near local Z=-5, use
  `axial_offset_mm=10.5`. This seats the turret's local bottom Z=-10 on the
  deck top Z=95; `85` or `95` is a repeated absolute height and is forbidden.
- Use shoulder `-55°` and elbow `+35°` for the delivered nominal pose. The old
  elbow value `+92°` folds the forearm and wrist down through the electronics
  deck and chassis and is forbidden. The full arm chain must remain above the
  deck.
- Use turret angle `+100°`: this is the previous +10° pose rotated by a
  further +90° around the vertical Z axis, as required for the final display.

## Single-part generation and token policy

This prompt deliberately places more detail inside each part, but avoids the
failure patterns that caused previous 18–20-part assemblies to consume very
large token budgets:

1. Generate and cache each part independently. Never restart successful parts
   because a later part or mate fails.
2. First attempt medium parts with thinking disabled. Their repeated/prismatic
   geometry should remain within the deterministic Architect/Coder path.
3. For `chassis_body` and `wrist_tool_head`, or for a medium part that fails
   twice for spatial/boolean-planning reasons, enable Qwen thinking for the
   **single-part Geometric Architect and Coder/Repair call only**. Do not enable
   thinking for the Judge or rerun the entire assembly with thinking.
4. If a complex cosmetic feature repeatedly fails, simplify that local feature
   while preserving silhouette and interfaces; do not split it into extra
   parts merely to make generation easier.
5. Kinematic features remain deterministic feature operators regardless of
   whether thinking is enabled for the base body.
6. Cache the mating plan after deterministic validation. A selector or pose
   failure must remate or rebuild only the attributed part, never regenerate
   all seven parts.

## Acceptance criteria

- `assembly_name`: `heavy_duty_mobile_manipulator_consolidated`
- `expected_part_count`: exactly 7
- exactly 4 active revolute joints and 2 rigid mates
- unmistakable heavy mobile manipulator silhouette despite the low part count
- all four fused wheel forms visible and symmetric
- chassis, turret, both links, wrist tool head, and lidar each export as one
  connected watertight STEP part
- turret centred on the deck
- shoulder/elbow endpoints use the workflow's standard fork/tongue geometry
- wrist roll has a real coaxial bearing/spindle interface
- arm truss windows remain open
- tool aperture and camera apertures remain open and visible
- nominal pose free of severe intersections or disconnected gaps
- separate per-part STEP/STL, assembly STEP/STL/GLB, and URDF are produced
- cosmetic simplification is acceptable; missing motion bodies, invented
  decorative parts, floating features, blocked apertures, or fake hinges are
  not acceptable.
