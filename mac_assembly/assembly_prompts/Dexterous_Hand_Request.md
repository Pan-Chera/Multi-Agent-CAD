# Dexterous Hand (XHand-matched) — Assembly Request Prompt

Build a dexterous hand assembly matched to the XHand (left hand)
specification as closely as the v3 part architecture allows: a
rounded-rectangle palm plate (~96 x 110 mm), 4 fingers on the distal
(+Y) edge, and a thumb on the wrist (-Y) edge on the radial (+X,
index-finger) side. All parts use v3 feature-based architecture
(LLM-generated base body + deterministic kinematic feature operators).
All dimensions in millimeters.

**Joint hardware restriction: ONLY ball joints (2-DOF) and regular
clevis hinges (1-DOF revolute).** XHand's fixed distal joints (finger
joint3 / thumb IP) are modeled as `rigid` mates between separate
middle/distal segment parts, so the fingers still LOOK like three
phalanges ("visually 3 segments, driven 2" — same as xhand).

## Target DOF: exactly 12, mapped from xhand

| xhand joint | axis / limits | this design |
|---|---|---|
| thumb_bend (side swing) | about Z, 0~105 deg | thumb root BALL `ball_axis_2="z"` (yaw) |
| thumb_rota1 (pitch 1) | perp. to thumb, -40~100 deg, -15 deg pretension | thumb root BALL `ball_axis_1="x"` (pitch; static 0 — pretension not modeled) |
| thumb_rota2 (pitch 2) | 0~100 deg | thumb clevis hinge, pin X (`limit_lower=0, limit_upper=100`) |
| thumb rota3 | fixed | RIGID mate thumb_middle <-> thumb_distal |
| index_bend (side swing) | ±10 deg | index root BALL `ball_axis_2="z"` (yaw) |
| index_joint1 (curl) | 0~110 deg | index root BALL `ball_axis_1="x"` (pitch) |
| index_joint2 (curl) | 0~110 deg | finger_proximal_1 <-> finger_middle_1 clevis hinge, pin X (`0..110`) |
| mid/ring/pinky joint1 (curl) | 0~110 deg each | palm fork <-> finger_proximal_i clevis hinge, pin X |
| mid/ring/pinky joint2 (curl) | 0~110 deg each | finger_proximal_i <-> finger_middle_i clevis hinge, pin X |
| 4-finger joint3 | fixed | RIGID mate finger_middle_i <-> finger_distal_i |

Active DOF = 2 (thumb ball) + 2 (index ball) + 8 (clevis hinges) = **12**.

## Ball joint axis configuration (critical, new pipeline feature)

A ball mate has TWO configurable rotation axes via `ball_axis_1` (the
axis `ball_pitch_deg` turns about) and `ball_axis_2` (the axis
`ball_yaw_deg` turns about, applied in the post-pitch frame). Each is
one of `"x"`/`"y"`/`"z"`, they MUST differ, and the third axis is
constrained to 0 (2-DOF only). Codegen composes
`Rotation(axis_1, pitch)` then `Rotation(axis_2, yaw)` and emits the
equivalent intrinsic-XYZ Euler triple to build123d's `BallJoint`; URDF
export decomposes the ball into `socket -> [revolute about axis_1] ->
dummy -> [revolute about axis_2] -> ball`.

**CHOOSE THE TWO AXES PERPENDICULAR TO THE LIMB/STEM DIRECTION.**
Rotation about the limb's own long axis is pure twist — it cannot bend
the limb, so a DOF spent on it is wasted. Both ball joints in this hand
sit on limbs that extend along ±Y (thumb extends -Y, index extends +Y),
so BOTH use:
```
ball_axis_1: "x"   # pitch = curl (Y -> Z), maps to xhand joint1 / rota1
ball_axis_2: "z"   # yaw   = in-XY-plane side swing (Y -> X), maps to xhand bend
ball_pitch_deg: 0
ball_yaw_deg: 0
```
(The pipeline default `y`+`z` is for limbs along ±X — do NOT use it
here: pitch about Y on a ±Y limb is axial twist, useless for curl.)

Ball-mate DOF travel limits (xhand's ±10 deg / 0~110 deg / 0~105 deg /
-40~100 deg) are NOT encodable — `limit_lower`/`limit_upper` apply to
revolute/linear mates only, and a ball's real limit is a 2-DOF cone.
The URDF ball decomposition emits unlimited revolutes. Limits are
noted in this table for reference only.

## Convention notes (critical for v3 feature operators)

These conventions are NOT optional — getting them wrong produces
geometry that is physically inside another part or floating in air.

- **Finger/thumb curl clevis joints all use `pin_axis: "x"`** so the
  bore axis is parallel to world +X: fingers curl in the YZ plane
  (toward the palm side), the human-hand curl motion, NOT a
  left-right sideways sweep. The in-plane side-swing DOFs (thumb_bend,
  index_bend) come from the BALL joints at those roots (yaw about Z),
  NOT from any clevis.
- **For `pin_axis: "x"` (and "y"), `attach_point_mm.z` IS the bore
  center Z.** E.g. attach_z = 8 puts the bore at Z = 8.
- **`clevis_fork` / `clevis_tongue` `direction` controls the
  protrusion direction of the ear/tongue BODY in the plane
  PERPENDICULAR to `pin_axis`.** For pin=x, direction must be ±y or
  ±z (pin parallel to direction is invalid).
- **Palm-side `attach_point` is at the plate edge.** The clevis body
  extends OUTWARD from the edge (length = `ear_length - R_tip`); the
  rounded tip sits at the ear end; the bore sits entirely on the
  clevis ear, NOT cutting into the plate. Same for finger/thumb
  clevis: `attach_point_mm` is at the segment end face, the ear
  extends OUTWARD, bore sits at `attach + direction*(ear_length -
  R_tip)` = 11 mm outside the face for SET H below.
- **Ball joint geometry**: `ball_cavity` sphere center IS
  `attach_point_mm` (on the palm edge face), opening bored outward
  along `direction`. `ball_stem` puts the ball COMPLETELY OUTSIDE the
  part: ball center = attach_point + (-direction) * (stem_length +
  sphere_radius). The stem (`stem_radius`) passes through the cavity
  opening (`opening_radius` >= stem_radius + 0.5).
- **Ball mate anchors MUST use `sphere_center_mm` = the actual ball /
  cavity center** (on a ball_stem part that is attach_point +
  (-direction)*(stem_length+sphere_radius), NOT the attach_point).
- **Rigid mates (fixed xhand joints) use `kind=face` bbox-face
  anchors**: `face: "back"` = +Y bbox face (distal face of a middle
  segment), `face: "front"` = -Y bbox face (proximal face of a distal
  segment). The two face centers are made coincident with aligned
  frames; both segments share the same cross-section orientation
  (all segments are X-centered boxes, Z = 0..16).
- **Local frame alignment**: by default a part's local axes align with
  world axes. The thumb extends -Y from the palm, so write thumb
  bodies as Y = -L..0 (palm side at Y = 0) — do NOT rely on rotations.
- **All finger/thumb segments share one cross-section**: 18 mm wide
  (X) x 16 mm tall (Z), centered on X = 0, Z = 0..16 (mid-height
  Z = 8). All pin-x clevis `attach_z = 8`. Cross-section ~ xhand
  proximal phalanx bbox 16.9 x 18.6 mm.

## Standard clevis parameter set (use EXACTLY this)

- **Set H (all clevis hinges in this hand)**:
  `{ear_length: 20, ear_width: 18, bore_radius: 3.5,
    tongue_thickness: 8, bar_thickness: 14, clearance_side: 0.1,
    pin_axis: "x"}`
  Bore sits 11 mm outside the attach face (ear_length - R_tip =
  20 - 9). Joint-to-joint distance through a segment = 11 + body + 11.
  Satisfies the hinge rectangle rule (body length ear_length - R_tip
  = 11 > R_tip = 9) and `_clevis_validate`. Do NOT invent other
  clevis params.

## Base body construction (MANDATORY for the LLM Coder)

**CRITICAL — READ THIS BEFORE WRITING ANY `gen_step()` CODE.**

Every rectangular prism base body MUST be constructed via:
```python
Pos(x, y, z) * Box(dx, dy, dz, align=(Align.CENTER, Align.CENTER, Align.CENTER))
```
where `(x, y, z)` is the body CENTER (NOT the corner, NOT the origin).

**FORBIDDEN — DO NOT USE `Rectangle(w, h)` + `extrude(amount)`:**
a Sketch profile is centered on the origin, which shifts Y (and Z) so
every `attach_point_mm` lands at the wrong place (inside the body or
outside in air). Always use the Pos*Box form with the center coords.

**MANDATORY construction stubs (copy literally, change only the dims):**

```python
from build123d import Box, Cylinder, Pos, Align

# palm: 96 x 110 x 24 centered at (0, 5, 12): X=-48..48, Y=-50..60, Z=0..24
palm_plate = Pos(0, 5, 12) * Box(96, 110, 24, align=(Align.CENTER,)*3)
# then fillet the 4 VERTICAL corner edges with radius 6 (see palm section)

# index_proximal (18 x 34 x 16 centered at (0, 17, 8)): X=-9..9, Y=0..34, Z=0..16
solid = Pos(0, 17, 8) * Box(18, 34, 16, align=(Align.CENTER,)*3)

# finger_proximal (18 x 34 x 16 centered at (0, 17, 8)): X=-9..9, Y=0..34, Z=0..16
solid = Pos(0, 17, 8) * Box(18, 34, 16, align=(Align.CENTER,)*3)

# finger_middle (18 x 11 x 16 centered at (0, 5.5, 8)): Y=0..11
solid = Pos(0, 5.5, 8) * Box(18, 11, 16, align=(Align.CENTER,)*3)

# finger_distal Box portion (18 x 11 x 16 centered at (0, 5.5, 8)):
solid_box = Pos(0, 5.5, 8) * Box(18, 11, 16, align=(Align.CENTER,)*3)
# Then add the half-cylinder tip (see finger_distal section below).

# thumb_proximal (18 x 28 x 16 centered at (0, -14, 8)): Y=-28..0
solid = Pos(0, -14, 8) * Box(18, 28, 16, align=(Align.CENTER,)*3)

# thumb_middle (18 x 15 x 16 centered at (0, -7.5, 8)): Y=-15..0
solid = Pos(0, -7.5, 8) * Box(18, 15, 16, align=(Align.CENTER,)*3)

# thumb_distal Box portion (18 x 15 x 16 centered at (0, -7.5, 8)):
solid_box = Pos(0, -7.5, 8) * Box(18, 15, 16, align=(Align.CENTER,)*3)
# Then add the half-cylinder tip at the -Y end.
```

For a body with a half-cylinder cap (finger_distal / thumb_distal),
fuse the Box and the **Z-axis** Cylinder with the SAME `Pos(x_c, y_c, 8)`:
```python
# finger_distal: cap protrudes +Y past the box
body = Pos(0, 5.5, 8) * Box(18, 11, 16, align=(Align.CENTER,)*3)
tip  = Pos(0, 11, 8) * Cylinder(radius=9, height=16, align=(Align.CENTER,)*3)
finger_distal = body + tip   # cylinder center ON the box +Y face
```
The cylinder center sits ON the distal box face; the inner half is
embedded in the box (boolean union), the outer half protrudes as the
rounded fingertip. Cap radius 9 = half the 18 mm width, so the
silhouette stays smooth.

**CRITICAL — Cylinder axis MUST be Z (do NOT rotate the cylinder).**
`Cylinder(radius=R, height=H)` defaults to axis +Z. The cap is a
HORIZONTAL cylinder (axis along Z, parallel to the clevis pin axis X,
perpendicular to the finger long axis Y). FORBIDDEN: `Rot(X=90) *
Cylinder(...)` — that turns the cap into a round rod end (full circle
at the tip) instead of a half-cylinder cap.

## Palm (base_body + 5 features)

- base_body: rounded-rectangle plate **96 x 110 x 24 mm**, R6 fillets
  on all 4 VERTICAL corners. Centered on XY origin: local X = -48..+48,
  Y = -50..+60, Z = 0..24. (xhand palm bbox 93.8 x 117.6 x 50 mm:
  width matched, length within 7%, thickness reduced to a plate.)
  Leave the following edge regions FLAT (outside the R6 corner
  fillets, which only curve |X| > 42 near the edges):
  - +Y edge (Y = 60): X in [0, 18] (middle fork), [-20, -2] (ring
    fork), [-40, -22] (pinky fork), [19.5, 40.5] (index ball cavity).
  - -Y edge (Y = -50): X in [19.5, 40.5] (thumb ball cavity).
- features (5 total):
  1. ball_cavity at attach_point_mm=[30, -50, 12], direction="-y",
     params: {sphere_radius: 10.5, opening_radius: 5.5}
     (THUMB socket. Sphere center on the -Y edge face at palm
     mid-thickness Z=12, radial (+X) side — xhand thumb base is on
     the radial side, near the wrist. Opening bored -Y outward.)
  2. ball_cavity at attach_point_mm=[30, 60, 12], direction="+y",
     params: {sphere_radius: 10.5, opening_radius: 5.5}
     (INDEX socket. Sphere center on the +Y edge face at palm
     mid-thickness Z=12, at X=+30 (index finger position). The index
     ball_stem mates here; the ball gives index_bend (yaw about Z,
     ±10 deg) + index_joint1 (pitch about X, 0~110 deg). Opening
     bored +Y outward.)
  3. clevis_fork at attach_point_mm=[9, 60, 12], direction="+y",
     pin_axis="x", params SET H (MIDDLE finger MCP; bore at (9, 71, 12))
  4. clevis_fork at attach_point_mm=[-11, 60, 12], direction="+y",
     pin_axis="x", params SET H (RING finger MCP; bore at (-11, 71, 12))
  5. clevis_fork at attach_point_mm=[-31, 60, 12], direction="+y",
     pin_axis="x", params SET H (PINKY MCP; bore at (-31, 71, 12))

  Finger lateral spacing 21/20/20 mm (xhand: 22.5/20/20 mm). Index
  finger at X=+30 (same side as the thumb, radial side — xhand thumb
  and index are both on +x).

## index_proximal (unique part, base_body + 2 features)

The index proximal phalanx. Body identical to finger_proximal (xhand:
all 4 fingers share proximal 55.8 mm), but the ROOT feature is a
ball_stem (mates with palm feature 2) instead of a clevis_tongue.

- base_body: 18 x 34 x 16 mm. `Pos(0, 17, 8) * Box(18, 34, 16,
  align=(CENTER,)*3)`. Local: X=-9..+9, Y=0..34, Z=0..16. Palm-side
  face at Y=0, distal face at Y=34.
- features (2):
  1. ball_stem at [0, 0, 8], direction="+y",
     params: {sphere_radius: 10, stem_radius: 5, stem_length: 6}.
     Ball sits COMPLETELY OUTSIDE the prism on the palm (-Y) side:
     ball center = attach + (-Y)*(stem_length + sphere_radius) =
     part-local **(0, -16, 8)**. The stem (R5) passes through the
     palm cavity opening (R5.5). **Mate spec MUST use
     sphere_center_mm = [0, -16, 8]** and `ball_axis_1="x",
     ball_axis_2="z"` (pitch=curl=index_joint1, yaw=side-swing=
     index_bend).
  2. clevis_fork at [0, 34, 8], direction="+y", pin_axis="x", SET H.
     Bore at local (0, 45, 8). (PIP joint, mates with
     finger_middle_1's tongue.)
- Ball center (palm-side, local Y=-16) -> PIP bore (local Y=45) =
  16 + 34 - 0... actually MCP(ball)->PIP = 16 (ball offset) + 34 (body)
  + 11 (fork ext) - but the ball center is on the -Y side, so
  ball-center-to-PIP-bore = 16 + 34 + 11 = 61 mm. The ball merges
  xhand's index_bend (at MCP) + index_joint1 (17.8 mm distal) into
  one joint center, so xhand's 17.8 mm root offset is absorbed here
  (see Deviations).

## Fingers — 3-segment chains (templates shared across 4 fingers)

Finger 1 = INDEX, 2 = MIDDLE, 3 = RING, 4 = PINKY. Middle/ring/pinky
share ALL three segment templates; index shares finger_middle and
finger_distal but has its own index_proximal (different root
feature). xhand: all 4 fingers share proximal 55.8 / distal-part 42.2
mm, so the segment geometry IS shared.

Each segment: 18 mm (X) x 16 mm (Z) cross-section, centered X=0,
Z=0..16 (mid-height Z=8), long axis Y (palm side Y=0, distal +Y).
**Avoid horizontal-axis cylinders / hemispheres** — use Box() + (for
distal) a Z-axis Cylinder half-embedded for the rounded tip. All
clevis features use SET H with attach_z=8.

### finger_proximal_i (template = finger_proximal_2, reused for 3/4)
- base_body: 18 x 34 x 16 mm. `Pos(0, 17, 8) * Box(18, 34, 16,
  align=(CENTER,)*3)`. Local: X=-9..+9, Y=0..34, Z=0..16. Flat faces
  at Y=0 and Y=34.
- features (2):
  1. clevis_tongue at [0, 0, 8], direction="-y", pin_axis="x", SET H.
     Bore at local (0, -11, 8). (MCP joint, mates with a palm fork.)
  2. clevis_fork at [0, 34, 8], direction="+y", pin_axis="x", SET H.
     Bore at local (0, 45, 8). (PIP joint, mates with
     finger_middle_i's tongue.)
- Joint-to-joint (MCP->PIP) = 11 + 34 + 11 = 56 mm ≈ xhand proximal
  phalanx 55.8 mm.

### finger_middle_i (template = finger_middle_1, reused 3x)
- base_body: 18 x 11 x 16 mm. `Pos(0, 5.5, 8) * Box(18, 11, 16,
  align=(CENTER,)*3)`. Local: Y=0..11.
- features (1):
  1. clevis_tongue at [0, 0, 8], direction="-y", pin_axis="x", SET H.
     Bore at local (0, -11, 8).
- NO distal feature: the distal face (Y=11, bbox face "back") is the
  rigid-mate surface for finger_distal_i (xhand joint3 is fixed).

### finger_distal_i (template = finger_distal_1, reused 3x)
- base_body: 18 x 11 x 16 mm + Z-axis Cylinder(radius=9, height=16)
  half-embedded at the +Y end:
  `body = Pos(0, 5.5, 8) * Box(18, 11, 16, align=(CENTER,)*3)`
  `tip  = Pos(0, 11, 8) * Cylinder(radius=9, height=16, align=(CENTER,)*3)`
  `finger_distal = body + tip`. Cylinder center ON the box +Y face
  (Y=11); axis Z; NO rotation. Local bbox: Y=0..20.
- features: NONE. Proximal face (Y=0, bbox face "front") rigid-mates
  to finger_middle_i. Rounded tip = half-cylinder cap.
- PIP->tip through the chain = 11 (middle tongue) + 11 (middle body)
  + 11 (distal body) + 9 (cap) = 42 mm ≈ xhand distal part 42.2 mm.

## Thumb (3 unique parts, ball root, extends -Y)

All thumb segments: 18 x 16 mm cross-section, centered X=0, Z=0..16.
The thumb extends **-Y** in local coords (palm side at Y=0, distal at
Y=-L). Curl clevis joints use SET H, pin_axis="x", attach_z=8.

### thumb_proximal
- base_body: 18 x 28 x 16 mm. `Pos(0, -14, 8) * Box(18, 28, 16,
  align=(CENTER,)*3)`. Local: X=-9..+9, Y=-28..0, Z=0..16.
- features (2):
  1. ball_stem at [0, 0, 8], direction="-y",
     params: {sphere_radius: 10, stem_radius: 5, stem_length: 6}.
     Ball sits COMPLETELY OUTSIDE the prism on the palm (+Y) side:
     ball center = attach + (+Y)*(stem_length + sphere_radius) =
     part-local **(0, 16, 8)**. **Mate spec MUST use
     sphere_center_mm = [0, 16, 8]** and `ball_axis_1="x",
     ball_axis_2="z"` (pitch=rota1, yaw=bend).
  2. clevis_fork at [0, -28, 8], direction="-y", pin_axis="x", SET H.
     Bore at local (0, -39, 8). (thumb_rota2 joint.)
- Ball center -> rota2 bore = 16 + 28 + 11 = 55 mm ≈ xhand
  rota1->rota2 55.3 mm.

### thumb_middle
- base_body: 18 x 15 x 16 mm. `Pos(0, -7.5, 8) * Box(18, 15, 16,
  align=(CENTER,)*3)`. Local: Y=-15..0.
- features (1):
  1. clevis_tongue at [0, 0, 8], direction="+y", pin_axis="x", SET H.
     Bore at local (0, 11, 8). (Tongue protrudes +Y toward
     thumb_proximal's fork; the body continues -Y.)
- NO distal feature: distal face (Y=-15, bbox face "front") is the
  rigid-mate surface for thumb_distal (xhand thumb rota3/IP fixed).

### thumb_distal
- base_body: 18 x 15 x 16 mm + Z-axis Cylinder(radius=9, height=16)
  half-embedded at the -Y end:
  `body = Pos(0, -7.5, 8) * Box(18, 15, 16, align=(CENTER,)*3)`
  `tip  = Pos(0, -15, 8) * Cylinder(radius=9, height=16, align=(CENTER,)*3)`
  `thumb_distal = body + tip`. Cylinder center ON the box -Y face
  (Y=-15); axis Z; NO rotation. Local bbox: Y=-24..0.
- features: NONE. Proximal face (Y=0, bbox face "back") rigid-mates
  to thumb_middle.
- rota2->tip through the chain = 11 (middle tongue) + 15 (middle) +
  15 (distal) + 9 (cap) = 50 mm = xhand rota2->tip 50.0 mm.

## Kinematic Mates (15 total)

Ball (2) — both with `ball_axis_1: "x"`, `ball_axis_2: "z"`,
`ball_pitch_deg: 0`, `ball_yaw_deg: 0`:
- thumb_root: `ball` — palm.ball_cavity (feat 1) <-> thumb_proximal.ball_stem.
  Fixed (palm) anchor: kind=sphere, sphere_center_mm=[30, -50, 12],
  sphere_radius_mm=10.5. Moving anchor: kind=sphere,
  sphere_center_mm=[0, 16, 8], sphere_radius_mm=10.
  (Pitch about X = xhand thumb_rota1; yaw about Z = xhand thumb_bend.)
- index_root: `ball` — palm.ball_cavity (feat 2) <-> index_proximal.ball_stem.
  Fixed (palm) anchor: kind=sphere, sphere_center_mm=[30, 60, 12],
  sphere_radius_mm=10.5. Moving anchor: kind=sphere,
  sphere_center_mm=[0, -16, 8], sphere_radius_mm=10.
  (Pitch about X = xhand index_joint1 curl; yaw about Z = xhand
  index_bend ±10 deg.)

Revolute (8) — all with SET-H bore selectors; every SELECTOR anchor
includes target_x_mm + target_y_mm (+ target_z_mm):
- middle_mcp: palm(fork feat 3) <-> finger_proximal_2(tongue).
  axis="x", value_mm=3.5. Palm side: target (9, 60, 12). Moving side:
  target (0, -11, 8). Limits 0..110.
- ring_mcp: palm(fork feat 4) <-> finger_proximal_3(tongue).
  Palm side target (-11, 60, 12). Otherwise same as middle_mcp.
- pinky_mcp: palm(fork feat 5) <-> finger_proximal_4(tongue).
  Palm side target (-31, 60, 12). Otherwise same as middle_mcp.
- index_pip: finger_proximal_1(fork) <-> finger_middle_1(tongue).
  axis="x", value_mm=3.5. Fixed side target (0, 45, 8); moving side
  target (0, -11, 8). Limits 0..110.
- middle_pip: finger_proximal_2(fork) <-> finger_middle_2(tongue).
  Same anchor pattern as index_pip. Limits 0..110.
- ring_pip: finger_proximal_3(fork) <-> finger_middle_3(tongue).
  Limits 0..110.
- pinky_pip: finger_proximal_4(fork) <-> finger_middle_4(tongue).
  Limits 0..110.
- thumb_rota2: thumb_proximal(fork) <-> thumb_middle(tongue).
  axis="x", value_mm=3.5. Fixed side target (0, -39, 8); moving side
  target (0, 11, 8). limit_lower=0, limit_upper=100.

Rigid (5) — xhand fixed distal joints; kind=face bbox-face anchors:
- index_dip: finger_middle_1 (face="back", distal face) <->
  finger_distal_1 (face="front", proximal face).
- middle_dip: finger_middle_2 <-> finger_distal_2 (same pattern).
- ring_dip: finger_middle_3 <-> finger_distal_3.
- pinky_dip: finger_middle_4 <-> finger_distal_4.
- thumb_tip: thumb_middle (face="front", distal face at Y=-15) <->
  thumb_distal (face="back", proximal face at Y=0).

## v3 SELECTOR Disambiguation (MANDATORY)

- The palm has 3 identical SET-H fork bores (axis X, R3.5) plus two
  ball-cavity opening cylinders (axis Y, R5.5, excluded by the
  axis+radius filter). Every palm-side revolute SELECTOR must set
  axis="x", value_mm=3.5, AND full target coords:
  - middle/ring/pinky MCP forks: target_x_mm = 9 / -11 / -31,
    target_y_mm=60, target_z_mm=12.
  Without targets, all fingers stack on the first fork.
- finger_proximal_i has TWO axis-X R3.5 bores (tongue at local
  (0,-11,8), fork at local (0,45,8)): every mate touching a
  finger_proximal MUST carry the right target_y_mm (-11 for the
  tongue side, 45 for the fork side).
- thumb_proximal has one axis-X R3.5 bore ((0,-39,8)); its ball stem
  cylinder is axis-Y R5, so it never matches the R3.5/X query. Include
  the target anyway.
- thumb_middle and finger_middle_i each have a single bore; include
  the targets for gate compliance.
- Ball mates use kind=sphere anchors (no selectors): cavity side
  sphere_center_mm = palm-local attach_point of the ball_cavity
  feature; ball side sphere_center_mm = the ball_stem's actual ball
  center (attach + (-direction)*(stem_length+sphere_radius)).
- Rigid DIP mates use kind=face anchors (no selectors): middle segment
  `face: "back"` (+Y distal face), distal segment `face: "front"`
  (-Y proximal face); thumb uses thumb_middle `face: "front"` and
  thumb_distal `face: "back"` (thumb extends -Y).

## Overall

- assembly_name: "dexterous_hand_xhand"
- overall_envelope_mm: {x: 100, y: 360, z: 70}
  (palm X=-48..+48, Y=-50..+60; thumb tip ≈ world Y=-155; index tip
  ≈ world Y=+163, other fingertips ≈ Y=+169; curled fingers sweep
  upward in Z — z=70 covers the sweep.)
- expected_part_count: 16
  (1 palm + 1 index_proximal + 3 thumb segments + 11 finger segments:
  finger_proximal_2/3/4, finger_middle_1..4, finger_distal_1..4.
  finger_middle and finger_distal templates are shared by ALL 4
  fingers including index.)
- special_features:
  - "12 active DOF matching xhand: thumb 3 (ball root with
    ball_axis_1='x' = rota1 pitch, ball_axis_2='z' = bend yaw, plus
    a clevis rota2) + index 3 (ball root with the SAME x/z axes =
    joint1 curl + bend yaw, plus a PIP clevis) + middle/ring/pinky 2
    each (two clevis hinges). Joint hardware restricted to ball
    joints and clevis hinges; xhand's fixed distal joints are rigid
    mates between separate middle/distal segment parts so all digits
    still look like three phalanges."
  - "Both ball joints use ball_axis_1='x' + ball_axis_2='z' (the two
    axes perpendicular to the ±Y limb direction): pitch about X
    curls the limb in the YZ plane, yaw about Z swings it in the XY
    plane. The pipeline default y+z is for ±X limbs and must NOT be
    used here (pitch about Y on a ±Y limb is axial twist)."
  - "finger_middle / finger_distal templates are shared by ALL 4
    fingers via reuses_part_id (xhand segment lengths are identical
    across the 4 fingers). The index differs only in its proximal
    segment (index_proximal carries a ball_stem root instead of a
    clevis_tongue)."
  - "all clevis params come from the single fixed Set H — they
    satisfy the hinge rectangle rule ear_length - R_tip > R_tip and
    _clevis_validate; do not modify them."
  - "fingertips are half-cylinders (R9, axis Z) built as Box +
    Z-axis Cylinder half-embedded; bodies are 18x16 mm rectangular
    prisms; palm has R6 corner fillets."

## Deviations from xhand (documented, deliberate)

1. Palm is a 24 mm plate (xhand palm volume is ~50 mm thick); width
   96 ≈ 93.8 mm, length 110 vs 117.6 mm.
2. Fingers attach at the palm distal EDGE, so wrist->MCP ≈ 126 mm
   (xhand: palm origin -> MCP ≈ 90-108 mm inside the palm volume).
   Fingertips land ~7% farther out; phalanx LENGTHS themselves match
   xhand (56 ≈ 55.8, 42 ≈ 42.2).
3. The index ball socket is at the palm edge: the index proximal body
   starts 6 mm MORE proximal than the other fingers' proximal bodies
   (xhand aligns them after the 17.8 mm root offset). The direction
   (index root more proximal) matches xhand; the magnitude is smaller
   because the ball merges xhand's bend+joint1 into one joint center,
   absorbing the 17.8 mm offset.
4. Ball-mate DOF travel limits (xhand's ±10 deg / 0~110 deg /
   0~105 deg / -40~100 deg on the ball DOFs) are NOT encodable in the
   MateSpec — `limit_lower`/`limit_upper` apply to revolute/linear
   mates only, and a ball's real limit is a 2-DOF cone. URDF ball
   decomposition emits unlimited revolutes. The limits are listed in
   the DOF table for reference only.
5. xhand's -15 deg thumb_rota1 pretension is not modeled (ball static
   pose pitch=0, yaw=0).
6. xhand's 29.8 mm thumb bend->rota1 offset is merged into the ball
   center (one ball replaces two coaxial joints).
7. Mass / collision meshes / inertials are out of scope for this
   geometry prompt (STEP parts + URDF export handled downstream).
