# Rack-and-Pinion Parallel Gripper (Underslung) — Assembly Request Prompt

Build a rack-and-pinion electric parallel gripper assembly with an
UNDERSLUNG finger layout: a flat rectangular base block (the "top
cuboid") with a snug guide slot cut into its BOTTOM face — the slot is
exactly wide enough for the two rack bars plus the pinion, so each
bar's outer flank rides directly against the slot side wall; two rack
bars sliding in opposing directions along the X axis inside that slot
(occupying the upper (+Y) and lower (-Y) tracks, point-symmetric about
the slot centre) — each bar's inner tooth flank TANGENT to the pinion
tip circle (zero gap), its bottom face resting FLUSH on the slot
floor, its outer end at X = ∓45 and its inner end extended INWARD past
the slot centre to X = ±37 (single-direction inward extension, so the
inner end reaches the FAR slot end wall X = ±66 exactly at full jaw
close); two L-shaped gripping fingers hanging below the base — each
finger is a compact horizontal swing arm exactly as long as the jaw's
short leg (16 mm, no separate mount block: the arm's top face contacts
the bar's bottom face directly, outer edge flush with the slot side
wall at Y = ∓19, no overhang past the base outline) fused to a
RIGHT-TRIANGULAR grip pad directly below it, centred on the world Y = 0
plane (8 mm thick), over the bar's OUTER end, for parallel clamping
along X; the arm carries a small R2 locating tongue on its top face
seating in a matching R2 groove cut into the bar's bottom face
(tongue-and-groove mount); and a central drive pinion rotating about a
vertical (Z) axis on a pivot pin hanging down from the slot ceiling at
the exact centre of the slot.

**Layout & View from Below (Looking +Z into the under-slot):**

- Top (+Y edge): Top rack bar (`right_rack_bar`), sliding along X.
- Center: Central drive pinion rotating on the (0, 0) pivot pin.
- Bottom (-Y edge): Bottom rack bar (`left_rack_bar`), sliding along X.
- Jaw regions: BOTH triangular grip pads hang down over the bars'
  OUTER ends (left pad X = -45..-29, right pad X = 29..45), both centred
  on the Y = 0 plane (world Y = -4..+4, 8 mm thick, coplanar grasp
  patches); the two grasp faces oppose each other along X.

**Rack Contact (zero-gap requirement):** each bar spans from its outer
end (X = ∓45, keeping the 1 mm sweep margin to the slot end wall) across
the slot centre to X = ±37, so its inner Y-facing flank passes the
pinion's closest point and lies tangent to the tip circle R9 (bar inner
flanks at Y = ∓9); the bar bottom face rests directly on the slot floor
(Z = 0); the bar's outer Y flank (Y = ∓19) rides directly against the
slot side wall (the slot is Y = -19..+19 — exactly bar + pinion + bar,
zero side clearance). The 29 mm closing stroke takes each inner end
from ±37 to ±66, flush against the FAR slot end wall — that wall
contact is the full-close hard stop, and it coincides exactly with the
jaws closing to zero gap.

**Swing-Arm Finger Connection:** Each finger hangs rigidly under its
rack bar via a compact L-shaped swing arm running BELOW the base
bottom face (Z < 0, where no slot walls constrain it); the arm's top
face touches the bar's bottom face inside the slot (there is NO
separate mount stub block — the arm alone is the mount interface). The
arm is 16 x 15 x 4 (exactly the jaw's short-leg width in X), spanning
world X = -45..-29 and world Y = -19..-4 (left arm) — outer edge flush
with the slot side wall, nothing overhanging the base outline. The
triangular grip pad hangs directly below the arm (world Y = -4..+4,
centred on Y = 0), over the bar's outer end, its long vertical grasp
leg facing the machine centre. The arm's top face carries an R2
locating tongue (a half-cylinder rising 2 mm into world Z = 0..2)
seating in a matching R2 groove cut into the bar's bottom face — the
tongue-and-groove pair is the rigid mount datum (both SELECTOR anchors
resolve to the tongue/groove cylindrical-face midpoint).

All parts use the v3 feature-based architecture (LLM-generated base
body + deterministic kinematic feature operators). All dimensions in
millimeters.

**Joint hardware restriction: ONLY one revolute joint about Z (1-DOF),
two linear prismatic joints along X (1-DOF each), and two rigid mounts
(0-DOF).** No angled linkage bars, oblique pivot pins, or tilted
coordinate frames.

## Target DOF: 1 active rotation + 2 linear prismatic slides

| joint | axis / motion | limits | this design |
|---|---|---|---|
| pinion_pivot | revolute about Z | -210..210 deg | gripper_base <-> drive_pinion (ceiling pivot pin / bore, Z-axis revolute, axial_offset -2.0 seats Z=0..8 flush on the slot floor; +/-210 deg covers the 208 deg full-close rotation of the 29 mm rack stroke at pitch radius 8) |
| left_bar_slide_joint | linear along X | 0..29 mm (closing = +X) | gripper_base <-> left_rack_bar (end-wall datum bore / bbox centre) |
| right_bar_slide_joint | linear along X | -29..0 mm (closing = -X) | gripper_base <-> right_rack_bar (end-wall datum bore / bbox centre) |
| left_finger_mount | rigid | 0 DOF | left_rack_bar <-> left_finger (swing arm top / bar bottom face) |
| right_finger_mount | rigid | 0 DOF | right_rack_bar <-> right_finger (swing arm top / bar bottom face) |

The pinion is the conceptual rotational input; the two bars translate
synchronously in opposing directions along X; each finger hangs rigidly
under its bar via its swing arm running below the base, so the grasp
faces move in pure translation (parallel gripper — no angular jaw
motion). The 29 mm + 29 mm closing strokes close the 58 mm static gap
to 0: at full close each bar's inner end abuts the far slot end wall
(X = ±66), so the wall contact doubles as the mechanical hard stop.

## Orthogonal alignment and topology rules (critical)

- **Strict orthogonal frame alignment**: every part's local frame aligns
  with the world axes at the static pose (zero rotation). Pinion axis
  is world +Z; bars slide along world X; finger pads extend along
  world -Z.
- **Tree topology**: the mate graph MUST remain a single tree rooted at
  `gripper_base`. Pinion and both bars mate independently to
  gripper_base; each finger mates to its own bar. The pinion is coupled
  to the bars ONLY conceptually (implicit rack mesh) — do NOT create
  mates between the pinion and the bars (closed loops are rejected).
- **Emit the mates in the order listed below** (revolute, linear,
  linear, rigid, rigid): parents must be placed before their children.
- **QA sweep margins (do not shrink)**: the QA engine sweeps ONE joint
  at a time over +/-20 mm (linear) or +/-30 deg (revolute) around the
  static pose while ALL other parts stay static. The static positions
  below keep every part clear at every sweep extreme, except the
  intentional zero-gap contacts (bar flanks tangent to the pinion tip
  circle, bar bottoms on the slot floor, bar outer flanks on the slot
  side walls, arm tops on the bar bottoms) — face contact is not
  interference, and the deepest tooth-corner excursion past a tangent
  flank is ~0.03 mm, far below the 0.3 mm penetration threshold.

## Detailed Pinion Specifications & Generation Method

The central drive pinion is an involute spur gear generated
deterministically via circular/polar pattern arrays to ensure precise
visual and kinematic tooth definition:

- Module (m): 1.0 mm
- Number of Teeth (z): 16
- Standard Pressure Angle (α): 20°
- Pitch Diameter (d = m·z): 16.0 mm (Pitch radius Rp = 8.0 mm)
- Addendum (ha = 1.0·m): 1.0 mm -> Tip Diameter (da): 18.0 mm
  (Ra = 9.0 mm)
- Dedendum (hf = 1.25·m): 1.25 mm -> Root Diameter (df): 13.5 mm
  (Rf = 6.75 mm)
- Face Width (Gear Height): 8.0 mm (along Z, local Z = 2 to 10 mm;
  the revolute mate's axial_offset_mm = -2.0 seats it at world Z = 0..8,
  flush on the slot floor)
- Central Pivot Bore: Ø 6.2 mm (Radius 3.1 mm for 0.1 mm clearance on
  the Ø 6.0 mm base pivot pin)
- Tooth Profile Generation: The tooth base cylinder of radius
  Rf = 6.75 mm is fused with 16 radial wedge/trapezoidal tooth
  projections spaced uniformly at intervals of Δθ = 22.5° (360° / 16),
  each tooth extruded along Z for 8 mm, followed by subtraction of the
  central Ø 6.2 mm pivot bore.

## Base body construction (MANDATORY for the LLM Coder)

**CRITICAL — READ THIS BEFORE WRITING ANY `gen_step()` CODE.**

Every rectangular prism base body MUST be constructed via:
```python
Pos(x, y, z) * Box(dx, dy, dz, align=(Align.CENTER, Align.CENTER, Align.CENTER))
```
where `(x, y, z)` is the body CENTER (NOT the corner, NOT the origin).

**CRITICAL — Cylinder axis MUST be Z (do NOT rotate the cylinder).**
`Cylinder(radius=R, height=H)` defaults to axis +Z. The pivot pin uses
axis Z. FORBIDDEN: `Rot(X=90) * Cylinder(...)`.

**Rotation restriction:** the ONLY rotations allowed anywhere are
`Rot(0, 0, angle)` for the pinion teeth, the single literal
`Rot(90, 0, 0)` that appears inside the finger-pad stubs below, and the
literal `Rot(0, 90, 0) * Cylinder` for the bar locating grooves and the
finger locating tongues (X-axis features). Do not invent any other
rotation.

**Finger Geometry Construction (L-shaped: Swing Arm + Triangular Pad,
NO stub):** Each finger is designed so its local coordinates EQUAL the
world static pose (the mount places it at identity). Each finger is
THREE fused sub-solids: (1) a compact horizontal swing arm (16 x 15 x 4,
exactly the jaw's short-leg width in X) running below the base bottom
(world Z = -4..0), spanning world X = -45..-29 and world Y = -19..-4
(left arm) — outer edge flush with the slot side wall at Y = -19
(nothing overhanging the base outline), inner edge at Y = -4; the arm's
TOP face (Z = 0) is the mount interface contacting the bar's bottom
face — there is NO separate mount stub; (2) a RIGHT-TRIANGULAR grip
pad directly below the arm, centred on the world Y = 0 plane: a prism
whose XZ profile (front view, looking along Y) is a right triangle with
the right angle at the TOP-INNER corner — the SHORT leg is the top
horizontal edge (16 mm, facing up, exactly the arm's X footprint), the
LONG leg is the inner vertical edge (60 mm, the grasp face contacting
the workpiece), and the hypotenuse slopes the outer side, tapering the
jaw to a knife edge at the bottom — extruded 8 mm thick in Y (world
Y = -4..+4); (3) an R2 locating TONGUE fused on the arm's top face: a
full cylinder along X (radius 2, length 16) centered at world
(Y = -14, Z = 0), X = -45..-29 — its upper half rises into world
Z = 0..2 and seats in the bar's matching bottom-face groove. The
tongue's exposed half-cylinder face (midpoint at world (-37, -14, +1))
is the rigid-mount SELECTOR datum, so the finger's bbox centre is
irrelevant to the mount.

**MANDATORY construction stubs (copy literally, change only the dims;
the triangle vertex ORDER controls the extrusion direction — do NOT
reorder the vertices):**
```python
import math
from build123d import Box, Cylinder, Pos, Rot, Align, Plane, Polygon, extrude

# 1. gripper_base: 140 x 46 x 16 centered at (0, 0, 8):
#    X=-70..70, Y=-23..23, Z=0..16 (top face Z=16, bottom face Z=0)
base = Pos(0, 0, 8) * Box(140, 46, 16, align=(Align.CENTER,)*3)
# central under-slot: pocket in bottom face, X=-66..66, Y=-19..19, Z=0..10
# (snug: exactly two 10-wide bar tracks + the R9 pinion between them)
slot = Pos(0, 0, 5) * Box(132, 38, 10, align=(Align.CENTER,)*3)
# pivot pin (Z-axis) hanging down from slot ceiling at (0, 0):
pin = Pos(0, 0, 7) * Cylinder(radius=3, height=10, align=(Align.CENTER,)*3)
gripper_base = base - slot + pin

# 2. drive_pinion: Module m=1.0, z=16, Root R=6.75, Tip R=9.0, Height=8 (local Z=2..10)
root_core = Pos(0, 0, 6) * Cylinder(radius=6.75, height=8, align=(Align.CENTER,)*3)
teeth = []
for i in range(16):
    angle = i * (360.0 / 16)
    # individual tooth wedge centered radially at radius ~7.85 mm
    rad = math.radians(angle)
    tx = 7.85 * math.cos(rad)
    ty = 7.85 * math.sin(rad)
    t = Pos(tx, ty, 6) * Rot(0, 0, angle) * Box(2.3, 1.5, 8, align=(Align.CENTER,)*3)
    teeth.append(t)

pinion_solid = root_core
for t in teeth:
    pinion_solid += t

pinion_bore = Pos(0, 0, 6) * Cylinder(radius=3.1, height=8, align=(Align.CENTER,)*3)
drive_pinion = pinion_solid - pinion_bore
# local bbox: X=-9..9, Y=-9..9, Z=2..10

# 3. left_rack_bar (Bottom rack, -Y track): 82 x 10 x 8 with ONE R2
# locating groove in its bottom face (X-axis cylinder centered at
# local (Y=0, Z=-4), spanning local X=-41..-25 — the OUTER end):
left_rack_bar = Pos(0, 0, 0) * Box(82, 10, 8, align=(Align.CENTER,)*3)
left_rack_bar -= Pos(-33, 0, -4) * Rot(0, 90, 0) * Cylinder(radius=2, height=16, align=(Align.CENTER,)*3)
# local bbox: X=-41..41, Y=-5..5, Z=-4..4; groove face midpoint local (-33, 0, -3)

# 4. left_finger (L-shaped: 16mm arm + triangular centre grip pad +
# locating tongue, NO stub). Local coordinates EQUAL the world static
# pose. Arm top face (Z=0) contacts the bar bottom; arm spans world
# X=-45..-29, Y=-19..-4 (outer edge flush with the slot side wall):
swing_arm = Pos(-37, -11.5, -2) * Box(16, 15, 4, align=(Align.CENTER,)*3)
# triangular grip pad directly below the arm, centred on world Y=0:
# right angle at TOP-INNER (X=-29, Z=0); short leg = top edge 16 mm
# (X=-45..-29, matching the arm); long leg = inner vertical grasp face
# 60 mm (X=-29); hypotenuse tapers the outer side to a knife edge at
# (-29, -60); extruded 8 mm thick, Y=-4..+4
grip_pad = Pos(0, 4, 0) * Rot(90, 0, 0) * extrude(
    Plane.XY * Polygon([(-45, 0), (-29, -60), (-29, 0)]), 8)
# R2 locating tongue on the arm top: full X-axis cylinder at
# (Y=-14, Z=0), X=-45..-29; upper half rises into Z=0..2 and seats in
# the bar groove; exposed face midpoint (-37, -14, +1) = mount datum
tongue = Pos(-37, -14, 0) * Rot(0, 90, 0) * Cylinder(radius=2, height=16, align=(Align.CENTER,)*3)
left_finger = swing_arm + grip_pad + tongue
# local bbox: X=-45..-29, Y=-19..4, Z=-60..2; local == world

# 3b. right_rack_bar (Top rack, +Y track): SAME box but the groove at
# the MIRrored end (local X=+25..+41, its own geometry — NOT a reuse
# of the left bar STEP, because the groove end differs):
right_rack_bar = Pos(0, 0, 0) * Box(82, 10, 8, align=(Align.CENTER,)*3)
right_rack_bar -= Pos(33, 0, -4) * Rot(0, 90, 0) * Cylinder(radius=2, height=16, align=(Align.CENTER,)*3)
# local bbox: X=-41..41, Y=-5..5, Z=-4..4; groove face midpoint local (+33, 0, -3)

# 5. right_finger (mirror of the left finger about BOTH world X=0 and
# world Y=0; local == world): arm X=29..45, Y=+4..+19; pad X=29..45,
# Y=-4..+4, grasp face at X=+29, knife edge at (+29, -60); tongue at
# (Y=+14, Z=0), X=29..45, datum (+37, +14, +1)
swing_arm_r = Pos(37, 11.5, -2) * Box(16, 15, 4, align=(Align.CENTER,)*3)
grip_pad_r = Pos(0, 4, 0) * Rot(90, 0, 0) * extrude(
    Plane.XY * Polygon([(45, 0), (29, 0), (29, -60)]), 8)
tongue_r = Pos(37, 14, 0) * Rot(0, 90, 0) * Cylinder(radius=2, height=16, align=(Align.CENTER,)*3)
right_finger = swing_arm_r + grip_pad_r + tongue_r
# local bbox: X=29..45, Y=-4..19, Z=-60..2; local == world
```

Then apply the 2 through_bore FEATURES listed in the gripper_base
section below (X-axis datum bores); do NOT try to cut them in the stub.

## Gripper Base (base_body + 2 features)

- base_body: **140 x 46 x 16 mm** block. Local: X = -70..+70,
  Y = -23..+23, Z = 0..+16. One central under-slot pocket cut into the
  BOTTOM face: X = -66..+66, Y = -19..+19, Z = 0..10 (10 mm deep) —
  the slot exactly fits the two 10-wide bar tracks plus the R9 pinion
  between them (zero side clearance: the bars' outer flanks ride on the
  slot side walls). Pivot pin (Z-axis):
  `Pos(0, 0, 7) * Cylinder(radius=3, height=10)` -> Z=2..12 at (0, 0),
  protruding down to Z=2 at the exact centre.
- features (2 total, subtractive datum bores):
  1. through_bore, params `{radius: 3.0}`, attachment
     `{attach_point_mm: [0, -14, 4], direction: "+x"}`.
  2. through_bore, params `{radius: 3.0}`, attachment
     `{attach_point_mm: [0, 14, 4], direction: "+x"}`.
     Datum bores run along X through the end walls at Y = -14 (bottom
     track) and Y = +14 (top track), serving as fixed datums for the
     linear mates; their Z = 4 pins the bar centre so the bar bottom
     rests flush on the slot floor (Z = 0) and their Y = ∓14 pins the
     bar centre so the bar inner flank sits tangent to the pinion tip
     circle (Y = ∓9) and its outer flank touches the slot side wall
     (Y = ∓19).
- key_dimensions: `{"width": 140, "depth": 46, "height": 16,
  "axis_ranges": {"x": [-70, 70], "y": [-23, 23], "z": [0, 16]}}`

## Drive Pinion (base_body only, no features)

- base_body: 16-tooth involute spur gear (m=1.0, Rf=6.75, Ra=9.0,
  thickness 8 mm, center bore R=3.1 along Z). Local: X = -9..9,
  Y = -9..9, Z = 2..10. In the static pose it sits at world Z = 0..8
  (flush on the slot floor; the revolute mate's axial_offset_mm = -2.0),
  tangent to both rack flanks.
- key_dimensions: `{"module": 1.0, "teeth_count": 16,
  "pitch_radius": 8.0, "tip_radius": 9.0, "bore_radius": 3.1,
  "face_width": 8.0, "axis_ranges": {"x": [-9, 9], "y": [-9, 9], "z": [2, 10]}}`

## Rack Bars — left_rack_bar (template) / right_rack_bar (reuse)

- base_body (left_rack_bar): Sliding rack bar
  `Pos(0, 0, 0) * Box(82, 10, 8)` with ONE R2 locating groove cut into
  its bottom face at the OUTER end (X-axis cylinder centered at local
  (Y=0, Z=-4), spanning local X = -41..-25). Local: X = -41..41,
  Y = -5..5, Z = -4..4. Placed in the -Y track (bottom edge when viewed
  from below), spanning world X = -45..+37 (outer end at -45 keeps the
  1 mm sweep margin to the slot end wall; the inner end EXTENDS INWARD
  past the slot centre to +37, so the 29 mm closing stroke brings it
  flush against the FAR slot end wall at +66 exactly at full jaw close).
  Its inner flank (Y = -9) rides tangent to the pinion tip circle, its
  bottom rests flush on the slot floor (Z = 0..8), and its outer flank
  (Y = -19) rides directly on the slot side wall. The world X = -45..-29
  groove seats the left finger's locating tongue (mount datum).
- base_body (right_rack_bar): SAME 82 x 10 x 8 box but the groove at
  the MIRRORED end (local X = +25..+41) — its own geometry, NOT a
  `reuses_part_id` copy of the left bar (the groove end differs).
  Placed in the +Y track (top edge when viewed from below), spanning
  world X = -37..+45, sliding symmetrically along X. Its world
  X = 29..45 groove seats the right finger's tongue.
- key_dimensions: `{"length": 82, "width": 10, "height": 8,
  "groove_radius": 2.0, "groove_length": 16,
  "axis_ranges": {"x": [-41, 41], "y": [-5, 5], "z": [-4, 4]}}`

## Gripping Fingers — left_finger / right_finger

Three-part L-shaped fingers (16 mm swing arm + triangular pad +
locating tongue, NO stub) rigidly mounted under the rack bars (local
coordinates == world):

- base_body (left_finger): (1) swing arm 16 x 15 x 4 at world
  Z = -4..0 (entirely below the base bottom, free of the slot walls)
  spanning world X = -45..-29 (exactly the jaw short-leg width) and
  world Y = -19..-4 — outer edge flush with the slot side wall, so
  nothing overhangs the base outline; the arm's TOP face at Z = 0
  directly contacts the bar's bottom face over the full arm footprint
  X = -45..-29 x Y = -19..-9; (2) right-triangular grip pad directly
  below the arm, centred on Y = 0: XZ profile with right angle at the
  TOP-INNER corner (world X = -29, Z = 0) — short leg = top edge from
  (-45, 0) to (-29, 0) (16 mm, matching the arm), long leg = inner
  vertical grasp face at X = -29 from Z = 0 to Z = -60 (60 mm), outer
  hypotenuse from (-45, 0) tapering to the knife-edge bottom vertex at
  (-29, -60) — extruded 8 mm thick at world Y = -4..+4; (3) R2 locating
  tongue fused on the arm's top: full X-axis cylinder (radius 2,
  length 16) centered at world (Y = -14, Z = 0), X = -45..-29 — the
  upper half rises into world Z = 0..2 seating in the bar groove; the
  exposed half-cylinder face midpoint (-37, -14, +1) is the mount
  datum.
- base_body (right_finger): mirror image under the +Y bar: arm
  X = 29..45, Y = +4..+19; triangular grip pad X = 29..45, Y = -4..+4
  with the grasp face at X = +29 (knife-edge bottom vertex at
  (+29, -60)); tongue at (Y = +14, Z = 0), X = 29..45, datum
  (+37, +14, +1).
- key_dimensions: `{"arm_size": [16, 15, 4], "pad_top_width": 16,
  "pad_height": 60, "pad_thickness": 8,
  "tongue_radius": 2.0, "tongue_length": 16,
  "axis_ranges": {"x": [-45, -29], "y": [-19, 4], "z": [-60, 2]}}`

## Kinematic Mates (5 total — single tree rooted at gripper_base)

Copy these JSON specs VERBATIM (the numbers encode the static pose and
the QA sweep margins).

**Anchor-kind rules for THIS assembly (validation is strict):**
- The two LINEAR mates' moving anchors MUST be exactly
  `{"kind": "axis_point", "axis": "x", "offset_mm": 0}` (the bar's
  bbox centre). Do NOT substitute a plane selector, a non-zero offset,
  or any other anchor kind — plan validation rejects anchors that do
  not define a principal axis.
- The LINEAR fixed anchors are cylinder selectors on the base's two
  R3.0 X-axis datum bores — copy them exactly, including value_mm=3.0
  and the full target coords (target_y_mm=-14 for the LEFT bar,
  target_y_mm=+14 for the RIGHT bar; the target is what disambiguates
  the two bores).
- The two finger mounts are `mate_type: "rigid"` — do NOT change them
  to `face_to_face` (that type seats the moving part ABOVE the fixed
  part along +Z; these fingers hang BELOW their bars). BOTH anchors of
  each finger mount are SELECTORS on the tongue-and-groove pair
  (cylinder, axis x, value_mm 2.0, with all three target coords stated
  explicitly — a NULL target component is treated as 0 and corrupts
  the resolved datum): the fixed anchor targets the bar's locating
  GROOVE face midpoint in BAR-LOCAL coords (target_x_mm=∓33,
  target_y_mm=0, target_z_mm=-3), the moving anchor targets the
  finger's TONGUE face midpoint in finger-local coords (which equal
  world: target_x_mm=∓37, target_y_mm=∓14, target_z_mm=+1). Keep
  `axial_offset_mm: 0` on both finger mounts. Do NOT substitute other
  faces/anchors, drop the targets, or change the mate type.
- **Keep the `limit_lower` / `limit_upper` fields on the revolute and
  both linear mates EXACTLY as written below** (-210..210 deg, 0..29 mm,
  -29..0 mm). They are the URDF joint travel limits; dropping them
  silently degrades the export to placeholder +/-20 mm sweep values.
- **Keep the revolute mate's `axial_offset_mm: -2.0`** — it seats the
  pinion at world Z = 0..8 (flush on the slot floor, tangent to both
  bar flanks over their full height); with axial_offset 0 the pinion
  would sit 2 mm higher and float clear of the racks.

Revolute (1):
```json
{"mate_id": "pinion_pivot", "mate_type": "revolute",
 "fixed_part_id": "gripper_base", "moving_part_id": "drive_pinion",
 "fixed_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "z", "select": "closest_to", "value_mm": 3.0, "target_x_mm": 0, "target_y_mm": 0, "target_z_mm": 6}},
 "moving_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "z", "select": "closest_to", "value_mm": 3.1, "target_x_mm": 0, "target_y_mm": 0, "target_z_mm": 6}},
 "angle_deg": 0, "axial_offset_mm": -2.0,
 "limit_lower": -210, "limit_upper": 210,
 "notes": "Pinion gear bore (R3.1) mounted on base pivot pin (R3.0) at the slot centre; axial_offset -2.0 seats the pinion at Z=0..8, flush on the slot floor, flanks tangent to both rack bars. Limits +/-210 deg cover the 208 deg full-close rotation (29 mm rack stroke at pitch radius 8)."}
```

Prismatic (2):
```json
{"mate_id": "left_bar_slide_joint", "mate_type": "linear",
 "fixed_part_id": "gripper_base", "moving_part_id": "left_rack_bar",
 "fixed_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 3.0, "target_x_mm": 0, "target_y_mm": -14, "target_z_mm": 4}},
 "moving_anchor": {"kind": "axis_point", "axis": "x", "offset_mm": 0},
 "slide_axis": "x", "position_mm": -4, "angle_deg": 0,
 "limit_lower": 0, "limit_upper": 29,
 "notes": "Left rack bar sliding in the bottom (-Y) track, resting on the slot floor, outer flank on the slot side wall, inner flank tangent to the pinion tip circle. Closing stroke +X (0..29 mm): the inner end travels from +37 to +66, flush against the far slot end wall exactly at full jaw close."}
```
```json
{"mate_id": "right_bar_slide_joint", "mate_type": "linear",
 "fixed_part_id": "gripper_base", "moving_part_id": "right_rack_bar",
 "fixed_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 3.0, "target_x_mm": 0, "target_y_mm": 14, "target_z_mm": 4}},
 "moving_anchor": {"kind": "axis_point", "axis": "x", "offset_mm": 0},
 "slide_axis": "x", "position_mm": 4, "angle_deg": 0,
 "limit_lower": -29, "limit_upper": 0,
 "notes": "Right rack bar sliding in the top (+Y) track, resting on the slot floor, outer flank on the slot side wall, inner flank tangent to the pinion tip circle. Closing stroke -X (-29..0 mm): the inner end travels from -37 to -66, flush against the far slot end wall exactly at full jaw close."}
```

Rigid (2) — **`mate_type` MUST stay `"rigid"`**: these fingers hang
BELOW their bars; face_to_face seats the moving part ABOVE (+Z) and
always fails validation. Do not rewrite the type, the faces, or the
anchors:
```json
{"mate_id": "left_finger_mount", "mate_type": "rigid",
 "fixed_part_id": "left_rack_bar", "moving_part_id": "left_finger",
 "fixed_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 2.0, "target_x_mm": -33, "target_y_mm": 0, "target_z_mm": -3}},
 "moving_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 2.0, "target_x_mm": -37, "target_y_mm": -14, "target_z_mm": 1}},
 "axial_offset_mm": 0,
 "notes": "Left finger mounted under the bottom rack bar via tongue-and-groove: the 16x15x4 arm (world X=-45..-29, Y=-19..-4, Z=-4..0) contacts the bar bottom, and the arm's R2 locating tongue (Z=0..2) seats in the bar's matching bottom-face groove. Both selectors resolve to the tongue/groove face midpoint world (-37,-14,1) -> identity placement: pad at X=-45..-29, Y=-4..+4 (centred on Y=0), grasp face at X=-29."}
```
```json
{"mate_id": "right_finger_mount", "mate_type": "rigid",
 "fixed_part_id": "right_rack_bar", "moving_part_id": "right_finger",
 "fixed_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 2.0, "target_x_mm": 33, "target_y_mm": 0, "target_z_mm": -3}},
 "moving_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 2.0, "target_x_mm": 37, "target_y_mm": 14, "target_z_mm": 1}},
 "axial_offset_mm": 0,
 "notes": "Right finger (mirror of left) mounted under the top rack bar: tongue-and-groove at the bar's +X end; identity placement: pad at X=29..45, Y=-4..+4, grasp face at X=+29."}
```

## Static-pose world layout (QA Verification)

- gripper_base: Envelope X = -70..70, Y = -23..23, Z = 0..16;
  under-slot pocket X = -66..66, Y = -19..19, Z = 0..10 (snug: exactly
  the two 10-wide bar tracks plus the R9 pinion); central pivot pin at
  (0, 0, 7), radius 3.0 mm.
- drive_pinion: Mounted at (0, 0, 4), Z = 0..8 (flush on the slot
  floor), outer tip radius 9.0 mm, centered between the tracks.
- left_rack_bar (Bottom rack): Positioned at Y = -14 (bar spans
  Y = -19..-9: inner face Y = -9 TANGENT to the pinion tip circle R9 —
  zero gap; outer face Y = -19 riding directly on the slot side wall),
  Z = 0..8 (bottom face resting FLUSH on the slot floor), static range
  X = -45..+37 (outer end 1 mm clear of the sweep margin to the slot
  end wall at X = -66; inner end extended inward to +37 — the 29 mm
  closing stroke brings it flush to the far wall at +66 exactly at
  full jaw close).
- right_rack_bar (Top rack): Positioned at Y = +14 (bar spans
  Y = 9..19, inner face Y = +9 tangent to the tip circle, outer face on
  the side wall), Z = 0..8, static range X = -37..+45.
- left_finger: mounted under the bottom bar at IDENTITY (local ==
  world, mount datum = the R2 tongue face at (-37,-14,+1) seating in
  the bar groove): swing arm at X = -45..-29, Y = -19..-4, Z = -4..0
  (arm top face contacts the bar bottom over the full footprint
  X = -45..-29 x Y = -19..-9; outer edge flush with the slot side
  wall, no overhang past the base outline; the R2 tongue rises to
  Z = 0..2 inside the bar groove); triangular grip pad at
  X = -45..-29, Y = -4..+4, Z = -60..0 with the long vertical grasp
  leg at X = -29 (full height Z = -60..0, 8 mm thick, knife-edge
  bottom vertex at (-29, -60)) — the pad sits over the bar's OUTER
  end, centred on the Y = 0 plane.
- right_finger: mirror image under the top bar: swing arm at
  X = 29..45, Y = +4..+19, Z = -4..0 (tongue at (37,14,Z=0..2));
  triangular grip pad at X = 29..45, Y = -4..+4, Z = -60..0 with the
  long vertical grasp leg at X = +29 (knife-edge bottom vertex at
  (+29, -60)).
- Central Gripping Zone: BOTH triangular grip pads are centred on the
  world Y = 0 plane (Y = -4..+4, 8 mm thick, coplanar grasp patches).
  The opposed grasp faces are the pads' inner X-facing vertical legs
  (left X = -29, right X = +29), static gap 58 mm, closed to 0 by the
  two 29 mm strokes — each bar's inner end lands flush on the far slot
  end wall (X = ±66) at exactly the same moment the grasp faces meet
  on the X = 0 plane. The two ARMS live in separate Y lanes (left
  Y = -19..-4, right Y = +4..+19, an 8 mm gap between them), so the
  arms can never collide in any swept or closed pose; the pads share
  the centre lane but never overlap in X.
- Sweep margins: the bars' outer ends sit at X = ∓45, so the -20 mm
  sweep extreme leaves 1 mm to the slot end wall at X = ∓66 (the inner
  ends at ±37 leave 9 mm at the +20 extreme); the bar
  flanks graze the pinion tip circle tangentially during the pinion's
  +/-30 deg sweep with at most ~0.03 mm tooth-corner excursion (well
  under the 0.3 mm penetration threshold); the bar outer flanks slide
  along the slot side walls (coplanar contact, zero penetration); the
  fingers hang at Z <= 0 with the pinion at Z = 0..8, so they never
  interact.

## v3 SELECTOR Disambiguation (MANDATORY)

- gripper_base Z-axis cylinders: ONLY the pivot pin R3.0 at (0, 0, 6)
  (no other Z-axis cylinders on this base) — the revolute fixed
  selector uses value_mm=3.0, target (0, 0, 6).
- gripper_base X-axis cylinders: the two datum bores, both R3.0 —
  disambiguate by target_y_mm (-14 left lane, +14 right lane); each
  bore's two end-wall hole segments merge to an axis midpoint at
  (0, target_y, 4).
- drive_pinion Z-axis cylinders: bore R3.1 vs root cylinder R6.75 —
  value_mm=3.1 picks the bore (they differ by far more than the
  0.5 mm tolerance).
- The bars carry TWO X-axis cylinders each (the R2 locating grooves,
  co-axial at local (Y=0, Z=-4), face midpoints local (∓33, 0, -3)) —
  the finger mounts' FIXED selectors use value_mm=2.0 with target_x_mm
  = ∓33 (bar-local) to pick the groove at the finger's own end; the
  linear moving anchors are axis_points.
- Each FINGER carries exactly ONE X-axis cylinder: its R2 locating
  tongue (face midpoint (∓37, ∓14, +1), local == world) — the finger
  mounts' MOVING selectors use value_mm=2.0 with target_x_mm=∓37,
  target_y_mm=∓14, target_z_mm=+1. All three target coords must be
  stated explicitly on every selector (a NULL target component is
  treated as 0 and corrupts the resolved datum).

## Overall

- assembly_name: "rack_and_pinion_parallel_gripper"
- overall_envelope_mm: {x: 140, y: 46, z: 76}
- expected_part_count: 6
  (1 gripper_base + 1 drive_pinion + 1 left_rack_bar +
  1 right_rack_bar + 1 left_finger + 1 right_finger)
- special_features:
  - "L-shaped swing-arm finger architecture: Bottom (-Y) and Top (+Y) sliding racks each carry a compact finger (16 x 15 x 4 arm exactly as long as the jaw's short leg, mounted on the bar bottom via an R2 tongue-and-groove locator — no stub, outer edge flush with the slot side wall, no overhang past the base outline — plus a right-triangular jaw directly below the arm, centred on Y=0 over the bar's outer end), positioning the grasp faces at X=∓29 for parallel clamping along the rack axis with coplanar contact patches."
  - "Snug zero-clearance housing: the slot (Y=-19..19) exactly fits the two 10-wide bar tracks plus the R9 pinion, so each bar rides on the slot side wall while its inner flank sits tangent to the pinion tip circle and its bottom rests on the slot floor."
  - "Single-direction inward rack extension with wall-abutment closure: each 82 mm bar's outer end stays at X=∓45 (1 mm QA sweep margin to the slot end wall) while its inner end extends past the slot centre to X=±37 — the 29 mm closing stroke lands the inner end flush on the far slot end wall (X=±66) at exactly the moment the jaws close, so the wall doubles as the full-close hard stop."
  - "Right-triangular finger jaws (front view): short leg up (16 mm), long inner vertical leg = full-height grasp face (60 mm) at the pad's inner edge, outer hypotenuse tapering to a knife-edge tip; 8 mm thick, centred on its own bar track; static grasp gap 58 mm closing to 0."
  - "Explicit 16-tooth involute spur pinion (m=1.0, z=16) modeled with discrete gear teeth radially patterned around a central mounting bore."
  - "Underslung layout with 100% axis-orthogonal linear guidance ensuring parallel jaw motion without angular backlash."
