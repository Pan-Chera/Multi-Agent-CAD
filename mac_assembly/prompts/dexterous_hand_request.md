# Dexterous Hand — Assembly Request Prompt

Build a dexterous hand assembly: rounded-rectangle palm + 4 identical
fingers (3 segments each) on the top edge + 1 thumb (3 segments) on the
left edge with a ball-joint root. All parts use v3 feature-based
architecture (LLM-generated base body + deterministic kinematic feature
operators). All dimensions in millimeters.

## Convention notes (critical for v3 feature operators)

These conventions are NOT optional — getting them wrong produces
geometry that is physically inside another part or floating in air.

- **All clevis joints use `pin_axis: "x"`** (parameter on the feature).
  This makes the bore axis parallel to world +X, so the joint rotates
  about X — i.e. fingers curl in the YZ plane (toward the palm at
  -Z, toward the back of the hand at +Z). This is the human-hand curl
  motion, NOT a left-right sideways sweep.
- **For `pin_axis: "x"`, `attach_point_mm.z` IS the bore center Z.**
  Set `attach_z = 5` for a bore at Z=5 (palm mid-thickness). No
  bar_thickness/2 offset. (For `pin_axis: "z"` legacy mode,
  `bore_z = attach_z + bar_thickness/2`, so attach_z=1 gives bore at
  Z=5; we are NOT using legacy mode in this assembly.)
- **`clevis_fork` / `clevis_tongue` `direction` controls the protrusion
  direction of the ear/tongue BODY in the plane PERPENDICULAR to
  `pin_axis`.** For `pin_axis: "x"`, direction must be ±y or ±z (NOT
  ±x — pin parallel to direction is invalid). Out-of-plane motion
  (e.g. thumb opposition at the root) comes from the ball joint (2 DOF:
  pitch + yaw), not from clevis orientation.
- **Local frame alignment**: by default (ball mate with yaw=0, pitch=0,
  or rigid/revolute mate with no rotation), a part's local +X axis
  aligns with world +X. If a part should extend in world -X from its
  parent (e.g. thumb on the -X edge of the palm), write the body as
  `X = -L..0` (extending -X in local) — do NOT rely on yaw=180.
- **Palm-side `attach_point` is at the plate edge.** The clevis_fork
  body extends OUTWARD from the edge (entirely OUTSIDE the plate,
  length = `ear_length - R_tip`); the rounded tip sits at the ear end
  (further out by `R_tip`); the bore is at the ear tip — entirely on
  the clevis, NOT cutting into the plate. This is the v3 design: the
  whole bore sits on the new clevis so the base body has no hole (the
  previous "anchored in plate" design left half the bore embedded
  inside the plate, unusable for assembly). The same applies to
  finger/thumb clevis: `attach_point_mm` is at the segment end face,
  the clevis ear extends OUTWARD from the segment, bore sits on the
  ear tip.

## Base body construction (MANDATORY for the LLM Coder)

**CRITICAL — READ THIS BEFORE WRITING ANY `gen_step()` CODE.**

Every rectangular prism base body MUST be constructed via:
```python
Pos(x, y, z) * Box(dx, dy, dz, align=(Align.CENTER, Align.CENTER, Align.CENTER))
```
where `(x, y, z)` is the body CENTER (NOT the corner, NOT the origin).

**FORBIDDEN — DO NOT USE `Rectangle(w, h)` + `extrude(amount)`:**
```python
# WRONG — Sketch profile is centered on the origin, breaking attach_points
sk = Rectangle(10, 25)           # X=-5..5, Y=-12.5..12.5 (WRONG position)
solid = extrude(sk, amount=10)  # Z=0..10, but Y is wrong
# All attach_point_mm=[0, 0, 5] and [0, 25, 5] now land at WRONG Y
# attach_point Y=0 falls in the body CENTER (feature embeds in body)
# attach_point Y=25 falls OUTSIDE the body (feature floats in air)
```

**Why this matters**: feature operators compute attach_point positions in
the body's LOCAL frame. If `Box(10, 25, 10)` is centered on the origin
(Y=-12.5..12.5) instead of (0, 12.5, 5) (Y=0..25), then:
- `attach_point_mm=[0, 0, 5]` lands in the body CENTER (tongue body
  embeds 7mm into the body interior — physically wrong, the tongue
  should protrude -Y past the body's proximal end)
- `attach_point_mm=[0, 25, 5]` lands OUTSIDE the body (fork floats in
  air at Y=25, with no connection to the body — fork falls off)

**MANDATORY construction stubs (copy literally, change only the dims):**

```python
# finger_proximal (10x30x10 centered at (0, 15, 5)):
solid = Pos(0, 15, 5) * Box(10, 30, 10, align=(Align.CENTER,)*3)
# X=-5..+5, Y=0..30, Z=0..10

# finger_middle (10x25x10 centered at (0, 12.5, 5)):
solid = Pos(0, 12.5, 5) * Box(10, 25, 10, align=(Align.CENTER,)*3)
# X=-5..+5, Y=0..25, Z=0..10

# finger_distal Box portion (10x20x10 centered at (0, 10, 5)):
solid_box = Pos(0, 10, 5) * Box(10, 20, 10, align=(Align.CENTER,)*3)
# Then add the half-cylinder tip (see finger_distal section below).

# thumb_proximal (10x20x10 centered at (0, -10, 5)) — note Y dimension is 20:
solid = Pos(0, -10, 5) * Box(10, 20, 10, align=(Align.CENTER,)*3)
# X=-5..+5, Y=-20..0, Z=0..10 (palm-side at Y=0, distal at Y=-20)

# thumb_middle (10x20x10 centered at (0, -10, 5)):
solid = Pos(0, -10, 5) * Box(10, 20, 10, align=(Align.CENTER,)*3)
# X=-5..+5, Y=-20..0, Z=0..10

# thumb_distal Box portion (10x15x10 centered at (0, -7.5, 5)):
solid_box = Pos(0, -7.5, 5) * Box(10, 15, 10, align=(Align.CENTER,)*3)
# Then add the half-cylinder tip at the -Y end.
```

For a body with a half-cylinder cap (e.g. finger_distal), fuse the Box
and the Z-axis Cylinder with the SAME `Pos(x_c, y_c, 5)` so they sit
at the correct position:
```python
from build123d import Box, Cylinder, Pos, Align
body = Pos(0, 10, 5) * Box(10, 20, 10, align=(Align.CENTER,)*3)
tip = Pos(0, 20, 5) * Cylinder(radius=5, height=10, align=(Align.CENTER,)*3)
finger_distal = body + tip  # half-cylinder tip protrudes +Y past the box
```

**CRITICAL — Cylinder axis MUST be Z (do NOT rotate the cylinder).**
`Cylinder(radius=R, height=H)` defaults to axis +Z. The half-cylinder
cap is a HORIZONTAL cylinder (axis along Z, parallel to the clevis
pin axis X — both horizontal, perpendicular to the finger long axis Y).
Its circular cross-section is in the XY plane; the cylinder extends
the full Z=0..10 height of the segment.

**FORBIDDEN — DO NOT USE `Rot(X=90)` or any rotation on the Cylinder:**
```python
# WRONG — rotates the cylinder so its axis becomes +Y instead of +Z.
# The cylinder becomes a "round rod" pointing along Y, NOT a
# half-cylinder cap. The cap's +Y end face becomes a full 360° circle
# (R=5 disc) instead of a single line (X=0, Z=0..10) at the cap tip.
# This breaks the rounded-fingertip shape.
cap = Pos(0, 20, 5) * Rot(X=90) * Cylinder(radius=5, height=10)  # WRONG

# CORRECT — no rotation; cylinder stays axis-aligned +Z.
tip = Pos(0, 20, 5) * Cylinder(radius=5, height=10, align=(Align.CENTER,)*3)
```

Why this matters: with axis +Z, the cylinder surface equation is
`X² + (Y-20)² = R²`, Z in [0,10]. At the cap's +Y extreme (Y=25),
the cross-section collapses to a single line (X=0, Z=0..10) — the
cylinder's outermost +Y point. With axis +Y (Rot(X=90)), the surface
is `X² + (Z-5)² = R²`, Y in [15,25] — at Y=25 the cross-section is a
full circle (X²+Z²=R²), making the cap a round rod end, not a flat
half-cylinder tip.

## Palm (v3: base_body + 5 features)

- base_body: rounded rectangle plate, 60 x 40 x 10mm, R6 fillets on all
  4 corners. Centered on XY origin, bottom face at Z=0, top face at Z=10.
  Local coords: X = -30..+30, Y = -20..+20, Z = 0..10. Leave flat 12x10mm
  regions at the +Y edge top face (4 spots at X=-18/-6/+6/+18, Y=+20,
  Z=10) for clevis_fork feature attachments protruding +Y. Spacing
  between fork centers is 12mm (ear_width=10mm + 2mm gap). Leave a flat
  14x10mm region at the -Y edge (X=-15, Y=-20, Z=5) for a ball_cavity
  feature opening -Y.
- features (5 total, subtractive first):
  1. ball_cavity at attach_point_mm=[-15, -20, 5], direction="-y",
     params: {sphere_radius: 6.5, opening_radius: 3.5}
     (attach_z=5 is the sphere center; cavity opens -Y through the
     -Y face of the palm at the lower-left corner. Thumb mates here,
     extending downward from the palm's bottom-left.)
  2. clevis_fork at attach_point_mm=[-18, 20, 5], direction="+y",
     params: {ear_length: 12, ear_width: 10, bore_radius: 2.5,
              tongue_thickness: 4, bar_thickness: 8, clearance_side: 0.1,
              pin_axis: "x"}
     (attach_z=5 = bore center Z; pin along +X; fork body extends +Y
     OUTSIDE the palm plate (plate edge at Y=20, ear body at Y=20..27);
     bore at the ear tip = world Y=27; tip protrudes +Y to Y=32. NO
     portion of the bore cuts into the palm plate — the entire bore
     sits on the clevis ear, NOT on the original part.)
  3. clevis_fork at attach_point_mm=[-6, 20, 5], direction="+y", same params
  4. clevis_fork at attach_point_mm=[+6, 20, 5], direction="+y", same params
  5. clevis_fork at attach_point_mm=[+18, 20, 5], direction="+y", same params

## 4 Identical Fingers (reuses_part_id for fingers 2/3/4)

Each finger is a 3-segment clevis chain along Y (palm side at Y=0,
distal at Y=max). All segments: rectangular prism 10 x 10 mm
cross-section (X width x Z height), centered on X=0, Z = 0..10
(Z=5 center). The long axis is Y. **Avoid horizontal-axis cylinders /
hemispheres** — the LLM Coder mishandles them (disconnected bodies,
MISSED_CUT). Use Box() + (for distal) a Z-axis Cylinder half-embedded
for the rounded tip. All clevis params identical to palm forks:
ear_length=12, ear_width=10, bore_radius=2.5, tongue_thickness=4,
bar_thickness=8, clearance_side=0.1, **pin_axis: "x"**. All clevis
attach_z=5 so the bore sits at Z=5 (prism mid-height).

### finger_proximal (template, reused 4x as finger_proximal_2/3/4)
- base_body: rectangular prism 10 x 30 x 10 mm (X x Y x Z). Construct as
  `Pos(0, 15, 5) * Box(10, 30, 10, align=(CENTER,)*3)` — DO NOT use
  `Rectangle(10, 30) + extrude(10)` (centers at origin, wrong position).
  Local: X=-5..+5, Y=0..30, Z=0..10. Flat rectangular faces at Y=0 and
  Y=30 for clevis feature attachment.
- features (2):
  1. clevis_tongue at [0, 0, 5], direction="-y",
     params: {ear_length: 12, ear_width: 10, bore_radius: 2.5,
              tongue_thickness: 4, bar_thickness: 8, clearance_side: 0.1,
              pin_axis: "x"}
     (mates with palm fork; tongue body extends -Y OUTSIDE the proximal
     body (proximal body ends at Y=0, tongue body at Y=-7..0, bore at
     the tongue tip = local Y=-7). NO portion of the bore cuts into
     the proximal body — the entire bore sits on the tongue.)
  2. clevis_fork at [0, 30, 5], direction="+y", same params (mates with
     middle tongue; fork body extends +Y OUTSIDE the proximal body
     (proximal body ends at Y=30, fork body at Y=30..37, bore at the
     fork tip = local Y=37). NO portion of the bore cuts into the
     proximal body.)

### finger_middle (template, reused 4x as finger_middle_2/3/4)
- base_body: rectangular prism 10 x 25 x 10 mm (X x Y x Z). Construct as
  `Pos(0, 12.5, 5) * Box(10, 25, 10, align=(CENTER,)*3)`. Local:
  X=-5..+5, Y=0..25, Z=0..10.
- features (2):
  1. clevis_tongue at [0, 0, 5], direction="-y", same params as proximal
  2. clevis_fork at [0, 25, 5], direction="+y", same params

### finger_distal (template, reused 4x as finger_distal_2/3/4)
- base_body: rectangular prism 10 x 20 x 10 mm + a Z-axis
  Cylinder(radius=5, height=10) half-embedded at the +Y end. Construct as:
  `body = Pos(0, 10, 5) * Box(10, 20, 10, align=(CENTER,)*3)`
  `tip_cyl = Pos(0, 20, 5) * Cylinder(radius=5, height=10, align=(CENTER,)*3)`
  `finger_distal = body + tip_cyl`  (no trim/intersect needed).
  The cylinder's -Y half (Y=15..20) is embedded inside the box; the +Y
  half (Y=20..25) protrudes +Y as the rounded fingertip. **The cylinder
  axis is Z (parallel to the clevis pin axis X — both are horizontal,
  perpendicular to the finger's long axis Y)** — this is a half-cylinder
  cap, NOT a hemisphere. **DO NOT use `Rot(X=90)` on the Cylinder** —
  see "FORBIDDEN" note in Base body construction section above. Local:
  X=-5..+5, Y=0..25, Z=0..10.
- features (1):
  1. clevis_tongue at [0, 0, 5], direction="-y", same params as proximal

## Thumb (bottom-left, 3 segments, ball-joint root)

All thumb segments: rectangular prism 10 x 10 mm cross-section
(X width x Z height), centered on X=0, Z=0..10 (Z=5 center). The long
axis is Y. **Thumb extends -Y in local coords** (palm side at Y=0,
distal at Y=-L). With the ball mate aligning thumb-local ball center
[0,11,5] to palm cavity world [-15,-20,5] at pitch=0 yaw=0, this puts
the thumb body at world Y < -20 (hanging down from the palm's
bottom-left corner — the correct direction). Thumb
clevis params: ear_length=12, ear_width=10, bore_radius=2.5,
tongue_thickness=4, bar_thickness=8, clearance_side=0.1,
**pin_axis: "x"** (same as fingers — pin parallel to world X, so
thumb rotates about X and curls in the YZ plane toward -Z (palm)).
All clevis attach_z=5 -> bore at Z=5.

### thumb_proximal
- base_body: rectangular prism 10 x 20 x 10 mm (X x Y x Z). Construct as
  `Pos(0, -10, 5) * Box(10, 20, 10, align=(CENTER,)*3)` — 10mm along X,
  20mm along Y, 10mm along Z. Local: X=-5..+5, Y=-20..0, Z=0..10.
  (Palm-side end at Y=0, distal end at Y=-20 — body extends -Y away
  from the palm, hanging down from the palm's bottom-left.)
  DO NOT use `Rectangle(10, 20) + extrude(10)` — that
  centers on the origin (Y=-10..+10), wrong position.
- features (2):
  1. ball_stem at [0, 0, 5], direction="-y",
     params: {sphere_radius: 6.0, stem_radius: 3.0, stem_length: 5.0}
     (ball sits COMPLETELY OUTSIDE the prism on the +Y side, connected
     by a small stem cylinder. Ball center is at part-local
     (0, stem_length + sphere_radius, 5) = (0, 11, 5) -- i.e. the ball
     is `stem_length` away from the prism's +Y face along +Y. The stem
     goes from the prism's +Y face (attach_point) along +Y for
     stem_length=5mm to reach the ball's -Y surface. This gives the
     ball room to rotate freely without clipping the prism body. The
     ball mates with palm.ball_cavity at palm-local [-15, -20, 5].)
     **Mate spec MUST use sphere_center_mm = [0, 11, 5]** (the actual
     ball center, not attach_point). Using attach_point [0,0,5] mis-
     aligns the joint by 11mm.
  2. clevis_fork at [0, -20, 5], direction="-y",
     params: {ear_length: 12, ear_width: 10, bore_radius: 2.5,
              tongue_thickness: 4, bar_thickness: 8, clearance_side: 0.1,
              pin_axis: "x"}
     (mates with thumb_middle tongue; fork at distal end of body.
     direction="-y" means the fork body extends -Y OUTSIDE the
     thumb_proximal body (proximal body ends at Y=-20, fork body at
     Y=-27..-20, bore at the fork tip = local Y=-27). NO portion of
     the bore cuts into the thumb_proximal body. pin_axis="x" is valid
     with direction="±y" since pin (X) is perpendicular to direction (Y).
     Thumb rotates about X → curl in YZ plane → thumb tip moves
     toward -Z (palm).)

### thumb_middle
- base_body: rectangular prism 10 x 20 x 10 mm (X x Y x Z). Construct as
  `Pos(0, -10, 5) * Box(10, 20, 10, align=(CENTER,)*3)`. Local:
  X=-5..+5, Y=-20..0, Z=0..10. (Proximal end at Y=0 mates with
  thumb_proximal fork; distal end at Y=-20 carries the next fork.)
- features (2):
  1. clevis_tongue at [0, 0, 5], direction="+y", pin_axis="x",
     same other params (mates with thumb_proximal fork; tongue body
     extends +Y OUTSIDE thumb_middle body (body ends at Y=0, tongue
     body at Y=0..7, bore at the tongue tip = local Y=7). NO portion
     of the bore cuts into thumb_middle. pin=X perpendicular to
     direction=+y ✓)
  2. clevis_fork at [0, -20, 5], direction="-y", pin_axis="x",
     same other params (mates with thumb_distal tongue; fork body
     extends -Y OUTSIDE thumb_middle body (body ends at Y=-20, fork
     body at Y=-27..-20, bore at the fork tip = local Y=-27). NO
     portion of the bore cuts into thumb_middle.)

### thumb_distal
- base_body: rectangular prism 10 x 15 x 10 mm (X x Y x Z) + a Z-axis
  Cylinder(radius=5, height=10) half-embedded at the -Y end.
  Construct as:
  `body = Pos(0, -7.5, 5) * Box(10, 15, 10, align=(CENTER,)*3)`
  `tip_cyl = Pos(0, -15, 5) * Cylinder(radius=5, height=10, align=(CENTER,)*3)`
  `thumb_distal = body + tip_cyl`
  The cylinder center is ON the box's -Y face (Y=-15) so the +Y half
  (Y=-15..-10) is embedded INSIDE the box (5mm overlap, merged by union)
  and the -Y half (Y=-20..-15) protrudes -Y as the rounded half-cylinder
  thumb tip. Cylinder axis is Z (perpendicular to the thumb long axis Y).
  **DO NOT use `Rot(X=90)` on the Cylinder** — see "FORBIDDEN" note in Base
  body construction section. **DO NOT position the cylinder center at
  Y=-20** (5mm away from the box face) — that leaves the ENTIRE cylinder
  outside the box (Y=-25..-15, only touching at Y=-15), producing a FULL
  cylinder protruding -Y instead of a half-cylinder cap half-embedded in
  the box. **DO NOT shift the cylinder center Y to "ensure overlap"** —
  the center MUST be exactly (0, -15, 5); build123d's `body + tip_cyl`
  boolean union handles the 5mm overlap correctly. Local: X=-5..+5,
  Y=-20..0, Z=0..10.
- features (1):
  1. clevis_tongue at [0, 0, 5], direction="+y", pin_axis="x",
     same other params (mates with thumb_middle fork; tongue TIP
     protrudes +Y toward thumb_middle; tongue BODY extends -Y into
     thumb_distal)

## Kinematic Mates (15 total)

Clevis chain (revolute):
- 4x revolute: palm.clevis_fork[i] <-> finger_proximal[i].clevis_tongue
  (i=1..4, bore coaxial X-axis, axial_offset_mm=0)
- 4x revolute: finger_proximal[i].clevis_fork <-> finger_middle[i].clevis_tongue
- 4x revolute: finger_middle[i].clevis_fork <-> finger_distal[i].clevis_tongue
- 1x revolute: thumb_proximal.clevis_fork <-> thumb_middle.clevis_tongue
  (X-axis pin — thumb long axis is Y, so pin=X is perpendicular to
  allow curl (not twist). Thumb tip moves in YZ plane, curling
  toward -Z (palm).)
- 1x revolute: thumb_middle.clevis_fork <-> thumb_distal.clevis_tongue
  (X-axis pin)

Ball joint (2-DOF spherical, provides thumb opposition / out-of-plane
motion at the thumb root):
- 1x ball: palm.ball_cavity <-> thumb_proximal.ball_stem
  (sphere_center aligns at world [-15, -20, 5]; pitch=0 deg, yaw=0 deg
  in static pose)

## v3 SELECTOR Disambiguation (MANDATORY)

The palm has 4 identical clevis_fork features (same bore_radius=2.5).
Every revolute mate on the palm side MUST include target_x_mm +
target_y_mm on the palm-side SELECTOR anchor pointing to the
corresponding fork's attach_point_mm. **For pin_axis="x", the bore
cylinder axis is X, so selector_query.axis must be "x" (NOT "z").**
Example for finger 2 (palm fork at [-6, 20, 5]):

  fixed_anchor: {kind: "selector", selector_query: {surface: "cylinder",
    axis: "x", select: "closest_to", value_mm: 2.5,
    target_x_mm: -6, target_y_mm: 20}}

Without target_x/y_mm, all 4 fingers stack at the first fork (X=+18).

**Finger clevis joints (pin_axis="x")**: all finger-side SELECTOR
anchors use axis="x" with target_x_mm=0, target_y_mm=0 (tongue at
proximal end) or target_x_mm=0, target_y_mm=30/25 (fork at distal
end). The mating fixed-side anchor on the previous segment uses the
fork's attach_point coords.

**Thumb clevis joints (pin_axis="x")**: thumb_middle has 2 cylinder
features (tongue at local [0, 0, 5] + fork at local [0, -20, 5], both
bore_radius=2.5, both pin_axis="x" → bore axis X). Both thumb
revolute mates on thumb_middle MUST include ALL THREE target fields
on the thumb_middle-side SELECTOR anchor (axis="x"). The validation
gate requires target_x_mm + target_y_mm to be non-null (target_z_mm
is optional for the gate, but include it for correct disambiguation
since pin=X means the bores are along X and disambiguation is in the
YZ plane):

- thumb_proximal <-> thumb_middle mate: thumb_middle-side anchor
  points to the tongue (local [0, 0, 5]):
  target_x_mm=0, target_y_mm=0, target_z_mm=5
- thumb_middle <-> thumb_distal mate: thumb_middle-side anchor points
  to the fork (local [0, -20, 5]):
  target_x_mm=0, target_y_mm=-20, target_z_mm=5

thumb_proximal also has 2 cylinder-producing features: ball_stem (sphere
+ stem, no cylinder face — selector uses surface="sphere") and
clevis_fork (bore along -Y). The clevis_fork SELECTOR uses axis="x"
with target_x_mm=0, target_y_mm=-20, target_z_mm=5.

## Overall

- assembly_name: "dexterous_hand"
- overall_envelope_mm: {x: 80, y: 130, z: 50}
  (palm 60x40 at world X=-30..+30, Y=-20..+20, Z=0..10; 4 fingers
  extend +Y from Y=20 to fingertip Y=100; thumb extends -Y from
  cavity center world [-15,-20,5] through thumb_distal tip at world
  Y=-75. Fingers curl in YZ plane (pin=X) so when curled they sweep
  through Z=0..50 — envelope z=50 covers the curled finger sweep.
  Envelope x=80 covers world X=-30..+50; y=130 covers Y=-75..+55.)
- expected_part_count: 16
  (1 palm + 4x3 finger segments + 3 thumb segments)
- special_features:
  - "4 identical fingers share geometry via reuses_part_id (3 templates,
    12 instances with their own placement)"
  - "thumb root is 2-DOF ball joint (pitch=0, yaw=0 in static pose);
    opposition motion comes from the ball joint at the root"
  - "all finger + thumb clevis joints are revolute about the X-axis
    (rotation in YZ plane, fingers curl toward the palm at -Z). The
    thumb clevis joints also rotate about X; the ball joint at the
    thumb root provides the additional out-of-plane opposition DOF."
  - "fingertips are half-cylinders (R5, axis Z -- perpendicular to the
    finger long axis Y, and perpendicular to the clevis pin axis X);
    fingers + thumb bodies are rectangular prisms (10x10 mm
    cross-section). The LLM Coder mishandles horizontal-axis cylinders
    and hemisphere trims, so half-cylinder caps (Box + Z-axis Cylinder
    half-embedded) are used instead."
  - "palm has rounded corners (R6 fillets) -- LLM free-form base body"
  - "thumb body extends -Y in local coords (palm side at Y=0, distal at
    Y=-L) so it points away from the palm in world space — local +Y
    aligns with world +Y by default, so writing the body as -Y..0 puts
    it at world Y<-20, below the palm"
