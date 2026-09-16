# Angular-Jaw Pneumatic Gripper — Assembly Request Prompt

Build a compact angular-jaw pneumatic gripper assembly (SMC MHC2-style
form): a rectangular base block; two complex multi-segment jaws pivoting
on horizontal pins near the bottom of the front (-Y) face; each jaw
carries a thin linkage rod running back toward a lower anchor point on
the same front face. The central single-acting pneumatic cylinder is
IMPLICIT (inside the base, not modeled as a part) and conceptually
drives the rotation of the two main jaw pivots. All parts use the v3
feature-based architecture (LLM-generated base body + deterministic
kinematic feature operators). All dimensions in millimeters.

**Joint hardware restriction: ONLY regular clevis hinges (1-DOF
revolute).** The actuated DOF is the closing angle of the two main jaw
pivots (one physical cylinder drives both; in the exported kinematics
they appear as two revolute joints).

## Target DOF: 1 actuated (pair of main pivots) + 5 passive = 6 clevis hinges total

| joint | axis / limits | this design |
|---|---|---|
| left_jaw_main_pivot | about X, ±25 deg | gripper_base <-> left_jaw_proximal clevis hinge, pin X (bore selector) |
| right_jaw_main_pivot | about X, ±25 deg | gripper_base <-> right_jaw_proximal clevis hinge, pin X (bore selector) |
| left_jaw_knuckle | about X, -15..45 deg | left_jaw_proximal <-> left_jaw_distal clevis hinge, pin X (bore selector) |
| right_jaw_knuckle | about X, -15..45 deg | right_jaw_proximal <-> right_jaw_distal clevis hinge, pin X (bore selector) |
| left_link_jaw_pivot | about X, free | left_jaw_proximal <-> left_linkage clevis hinge, pin X (bore selector) |
| right_link_jaw_pivot | about X, free | right_jaw_proximal <-> right_linkage clevis hinge, pin X (bore selector) |

The two main pivots are the actuated closing DOF. The jaw knuckles give
the multi-segment jaw its articulated look (like the multi-part jaws of
a real angular gripper). The link rods swing on the jaw as the jaws
rotate.

## Clevis axis configuration (critical)

- All hinge pins are parallel to world +X: `pin_axis: "x"` on every
  clevis feature. All mechanism motion occurs in the YZ plane.
- The linkage rear tongues land EXACTLY in the base's lower Set-L forks
  at the static pose (geometric loop closure), but the mate graph MUST
  stay a single tree rooted at `gripper_base` — do NOT emit a mate
  between a linkage and the base (the assembly engine places each moving
  part exactly once; a second placement silently breaks the first).

## Convention notes (critical for v3 feature operators)

These conventions are NOT optional — getting them wrong produces
geometry that is physically inside another part or floating in air.

- **For `pin_axis: "x"` (and "y"), `attach_point_mm.z` IS the bore
  center Z.** E.g. attach_z = 10 puts the bore at Z = 10; the ear/tongue
  body then spans Z = 10 ± ear_width/2.
- **`clevis_fork` / `clevis_tongue` bore position**: bore center =
  `attach_point_mm + direction * (ear_length - ear_width/2)` — i.e.
  SET F bores sit **15 mm** outside the attach face, SET L bores **7 mm**
  (NOT 11 / 6 — those numbers belong to a different ear_width).
- **`direction` controls the protrusion direction of the ear/tongue
  body in the plane perpendicular to `pin_axis`.** For pin=x, valid
  directions are ±y / ±z only (pin parallel to direction is invalid).
  A tongue and the fork it inserts into protrude TOWARD each other
  (e.g. base fork `"-y"` receives a jaw tongue `"+y"`).
- **`ear_length` must be strictly greater than `ear_width`** (hinge
  rectangle rule: the rectangular ear body must be longer than the tip
  circle radius). Do not invent clevis parameters outside the two sets
  below.
- **Attach points sit ON flat faces of the base body** (snap-to-surface
  reverts on tilted normals; put every attach point exactly on a flat
  region of the body, as specified below).

## Standard clevis parameter sets (use EXACTLY these)

- **Set F (finger / main pivots & jaw knuckles)**:
  `{ear_length: 25, ear_width: 20, bore_radius: 5.0,
    tongue_thickness: 12, bar_thickness: 20, clearance_side: 0.1,
    pin_axis: "x"}`
  Bore sits 15 mm outside the attach face (ear_length - R_tip = 25 - 10).
  Fork slot gap = 12.2 mm (tongue 12 + 2×0.1), each ear 3.9 mm thick.
- **Set L (linkage rods)**:
  `{ear_length: 12, ear_width: 10, bore_radius: 2.5,
    tongue_thickness: 6, bar_thickness: 10, clearance_side: 0.05,
    pin_axis: "x"}`
  Bore sits 7 mm outside the attach face (ear_length - R_tip = 12 - 5).
  Fork slot gap = 6.1 mm (tongue 6 + 2×0.05), each ear 1.95 mm thick.

Do NOT alter these parameters. Both satisfy the hinge rectangle rule
(25 > 20, 12 > 10) and `_clevis_validate`.

## Base body construction (MANDATORY for the LLM Coder)

**CRITICAL — READ THIS BEFORE WRITING ANY `gen_step()` CODE.**

Every rectangular prism base body MUST be constructed via:
```python
Pos(x, y, z) * Box(dx, dy, dz, align=(Align.CENTER, Align.CENTER, Align.CENTER))
```
where `(x, y, z)` is the body CENTER (NOT the corner, NOT the origin).

**FORBIDDEN — DO NOT USE `Rectangle(w, h)` + `extrude(amount)`:** a
Sketch profile is centered on the origin, which shifts Y (and Z) so
every `attach_point_mm` lands at the wrong place. Always use the
Pos*Box form with the center coords.

**MANDATORY construction stubs (copy literally, change only the dims):**
```python
from build123d import Box, Cylinder, Pos, Align

# gripper_base: 100 x 90 x 50 centered at (0, 0, 25):
#   X=-50..50, Y=-45..45, Z=0..50; R5 fillet on the 4 VERTICAL corners
gripper_base = Pos(0, 0, 25) * Box(100, 90, 50, align=(Align.CENTER,)*3)

# left_jaw_proximal arm: 25 x 40 x 30 centered at (0, -20, 15):
#   X=-12.5..12.5, Y=-40..0, Z=0..30
arm = Pos(0, -20, 15) * Box(25, 40, 30, align=(Align.CENTER,)*3)
# linkage prong: 21 x 12 x 20 centered at (-15.5, -34, 10):
#   X=-26..-5, Y=-40..-28, Z=0..20 (fused to the arm's -X side)
prong = Pos(-15.5, -34, 10) * Box(21, 12, 20, align=(Align.CENTER,)*3)
left_jaw_proximal_body = arm + prong

# left_jaw_distal body: 25 x 30 x 30 centered at (0, -15, 15):
#   X=-12.5..12.5, Y=-30..0, Z=0..30
body = Pos(0, -15, 15) * Box(25, 30, 30, align=(Align.CENTER,)*3)
# rounded tip: Z-axis Cylinder half-embedded at the -Y end (do NOT rotate)
tip = Pos(0, -30, 15) * Cylinder(radius=12.5, height=30, align=(Align.CENTER,)*3)
left_jaw_distal_body = body + tip   # cylinder center ON the box -Y face

# left_linkage bar: 10 x 30 x 8 centered at (0, -15, 0):
#   X=-5..5, Y=-30..0, Z=-4..4
left_linkage_body = Pos(0, -15, 0) * Box(10, 30, 8, align=(Align.CENTER,)*3)
```

**CRITICAL — Cylinder axis MUST be Z (do NOT rotate the cylinder).**
`Cylinder(radius=R, height=H)` defaults to axis +Z. FORBIDDEN:
`Rot(X=90) * Cylinder(...)`.

## Gripper Base (base_body + 4 features)

- base_body: **100 x 90 x 50 mm** simplified block. R5 fillets on all
  4 VERTICAL corner edges. Local: X = -50..+50, Y = -45..+45, Z = 0..50
  (bottom face at Z=0, front face at Y=-45). Keep the front face FLAT
  over X in [-45, 45] (the R5 fillets only curve within 5 mm of the
  side edges).
- features (4 total — all `clevis_fork`, pin_axis "x", attached on the
  front face Y=-45, attach_z=10 so the bores sit at Z=10):
  1. clevis_fork at [34, -45, 10], direction="-y", SET F.
     (LEFT main jaw pivot; bore at (34, -60, 10).)
  2. clevis_fork at [-34, -45, 10], direction="-y", SET F.
     (RIGHT main jaw pivot; bore at (-34, -60, 10).)
  3. clevis_fork at [14, -45, 10], direction="-y", SET L.
     (LEFT linkage anchor; bore at (14, -52, 10).)
  4. clevis_fork at [-14, -45, 10], direction="-y", SET L.
     (RIGHT linkage anchor; bore at (-14, -52, 10).)

The Set-F forks (X extents 24..44 / -44..-24) and Set-L forks
(X extents 9..19 / -19..-9) occupy distinct X bands on the same front
edge, 5 mm apart. The linkage tongues plug into the Set-L forks at the
static pose (geometric loop closure — see Kinematic Mates).

## Jaws — left_jaw_proximal / right_jaw_proximal (multi-element, NOT straight sticks)

Each jaw proximal segment is a 2-element fusion (arm + linkage prong)
plus 3 kinematic features. The prong sticks out on the INNER side
(toward the assembly centre plane X=0): -X for the left jaw, +X for the
right jaw (mirror). The jaws extend AWAY from the base (-Y).

- base_body (left_jaw_proximal):
  - Arm: `Pos(0, -20, 15) * Box(25, 40, 30)` → local X=-12.5..12.5,
    Y=-40..0, Z=0..30. Rear face (Y=0) faces the base.
  - Prong: `Pos(-15.5, -34, 10) * Box(21, 12, 20)` → local X=-26..-5,
    Y=-40..-28, Z=0..20, fused to the arm's -X side.
- features (3 total):
  1. clevis_tongue at [0, 0, 10], direction="+y", pin_axis="x", SET F.
     (Bore at local (0, 15, 10). Protrudes +Y toward the base's main
     fork; the tongue rect spans local Y=0..15 and nests between the
     fork ears. Mates with gripper_base feature 1.)
  2. clevis_fork at [-20, -28, 10], direction="+y", pin_axis="x", SET L.
     (Bore at local (-20, -21, 10). Mounted on the prong's rear face
     Y=-28, protrudes +Y. Mates with the linkage's front tongue.)
  3. clevis_fork at [0, -40, 10], direction="-y", pin_axis="x", SET F.
     (Bore at local (0, -55, 10). Mounted on the arm's front face
     Y=-40, protrudes -Y. Receives the jaw distal segment's tongue.)
- right_jaw_proximal: identical EXCEPT the prong is mirrored:
  `Pos(15.5, -34, 10) * Box(21, 12, 20)` (X=5..26) and feature 2
  becomes clevis_fork at [20, -28, 10], direction="+y", SET L
  (bore at local (20, -21, 10)). Features 1 and 3 are identical to the
  left jaw's. NOT a `reuses_part_id` reuse — the prong/fork side is
  mirrored, so it is a separate generated part.

## Jaws — left_jaw_distal (template) / right_jaw_distal (reuse)

- base_body: 2-element fusion, X-symmetric:
  - Body: `Pos(0, -15, 15) * Box(25, 30, 30)` → local X=-12.5..12.5,
    Y=-30..0, Z=0..30.
  - Rounded tip: `Pos(0, -30, 15) * Cylinder(radius=12.5, height=30)`
    (Z-axis, center ON the box -Y face, half embedded, NO rotation).
    Local bbox Y reaches -42.5.
- features (1):
  1. clevis_tongue at [0, 0, 10], direction="+y", pin_axis="x", SET F.
     (Bore at local (0, 15, 10); tongue protrudes +Y back toward the
     proximal segment's knuckle fork.)
- right_jaw_distal: `reuses_part_id = "left_jaw_distal"` (the segment is
  X-symmetric — identical geometry, different placement). Its
  description states: placed on the right jaw, tongue mating the right
  knuckle fork.

## Linkages — left_linkage (template) / right_linkage (reuse)

- base_body: `Pos(0, -15, 0) * Box(10, 30, 8)` → local X=-5..5,
  Y=-30..0, Z=-4..4. Flat faces at Y=0 and Y=-30.
- features (2 total):
  1. clevis_tongue at [0, 0, 0], direction="+y", pin_axis="x", SET L.
     (Bore at local (0, 7, 0). REAR end — geometrically nests in the
     base's Set-L fork at the static pose, bore coinciding with the
     base fork bore; NOT mated.)
  2. clevis_tongue at [0, -30, 0], direction="-y", pin_axis="x", SET L.
     (Bore at local (0, -37, 0). FRONT end — mates with the jaw
     proximal's linkage fork.)
- right_linkage: `reuses_part_id = "left_linkage"` (X-symmetric bar with
  centred tongues — identical geometry, different placement).

## Kinematic Mates (6 total — single tree rooted at gripper_base)

Revolute (6) — ALL anchors are cylinder bore SELECTORS with
axis="x", `select: "closest_to"`, the matching `value_mm` (5.0 for SET F,
2.5 for SET L), and exact target coordinates (part-LOCAL, the bore
centers listed above). `angle_deg: 0`, `axial_offset_mm: 0` for all six.

Left jaw main pivot (right side is the mirror — flip the X signs):
```json
{"mate_id": "left_jaw_main_pivot", "mate_type": "revolute",
 "fixed_part_id": "gripper_base", "moving_part_id": "left_jaw_proximal",
 "fixed_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 5.0, "target_x_mm": 34, "target_y_mm": -60, "target_z_mm": 10}},
 "moving_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 5.0, "target_x_mm": 0, "target_y_mm": 15, "target_z_mm": 10}},
 "angle_deg": 0, "axial_offset_mm": 0,
 "limit_lower": -25, "limit_upper": 25,
 "notes": "main jaw pivot: base fork bore (34,-60,10) <-> jaw tongue bore local (0,15,10)"}
```

Left jaw knuckle:
```json
{"mate_id": "left_jaw_knuckle", "mate_type": "revolute",
 "fixed_part_id": "left_jaw_proximal", "moving_part_id": "left_jaw_distal",
 "fixed_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 5.0, "target_x_mm": 0, "target_y_mm": -55, "target_z_mm": 10}},
 "moving_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 5.0, "target_x_mm": 0, "target_y_mm": 15, "target_z_mm": 10}},
 "angle_deg": 0, "axial_offset_mm": 0,
 "limit_lower": -15, "limit_upper": 45,
 "notes": "jaw knuckle: proximal fork bore local (0,-55,10) <-> distal tongue bore local (0,15,10)"}
```

Left link pivot (jaw's linkage fork receives the linkage's FRONT tongue):
```json
{"mate_id": "left_link_jaw_pivot", "mate_type": "revolute",
 "fixed_part_id": "left_jaw_proximal", "moving_part_id": "left_linkage",
 "fixed_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 2.5, "target_x_mm": -20, "target_y_mm": -21, "target_z_mm": 10}},
 "moving_anchor": {"kind": "selector", "selector_query": {"surface": "cylinder", "axis": "x", "select": "closest_to", "value_mm": 2.5, "target_x_mm": 0, "target_y_mm": -37, "target_z_mm": 0}},
 "angle_deg": 0, "axial_offset_mm": 0,
 "notes": "linkage front tongue bore local (0,-37,0) seats in the jaw prong fork bore local (-20,-21,10); passive hinge, no limits"}
```

The remaining three mates are the X-mirrors:
- right_jaw_main_pivot: base target (-34, -60, 10), moving
  right_jaw_proximal target (0, 15, 10); limits -25..25.
- right_jaw_knuckle: right_jaw_proximal target (0, -55, 10),
  moving right_jaw_distal target (0, 15, 10); limits -15..45.
- right_link_jaw_pivot: right_jaw_proximal fork target (20, -21, 10),
  moving right_linkage target (0, -37, 0); no limits.

**Static-pose world layout** (so the Judge/QA can verify): base at
identity (X=-50..50, Y=-45..45, Z=0..50). Left jaw proximal local origin
lands at world (34, -75, 0); left jaw distal at (34, -145, 0); left
linkage at (14, -59, 10). With these placements the linkage REAR tongue
bore lands EXACTLY on the base's Set-L fork bore (14, -52, 10) —
geometric loop closure without a mate. Right side mirrored.

## v3 SELECTOR Disambiguation (MANDATORY)

- The base has FOUR axis-X cylinder bores (two R5.0 at Y=-60, two R2.5
  at Y=-52): every base-side SELECTOR must set axis="x", the correct
  value_mm (5.0 vs 2.5), AND full target coords — X (+34/-34/+14/-14)
  picks the right fork.
- left_jaw_proximal has THREE axis-X bores: tongue R5.0 at local
  (0, 15, 10), knuckle fork R5.0 at local (0, -55, 10), linkage fork
  R2.5 at local (-20, -21, 10). Two same-radius bores (R5.0) — every
  selector on a jaw proximal MUST carry target_y_mm (15 vs -55) plus
  target_z_mm; the R2.5 fork is additionally separated by value_mm.
- left_jaw_distal has a single R5.0 bore at (0, 15, 10) — include the
  target anyway (gate compliance).
- left_linkage has TWO identical R2.5 bores at local (0, 7, 0) and
  (0, -37, 0) — target_y_mm (7 vs -37) disambiguates.
- For pin axis X, target_x_mm is along the bore axis (irrelevant for
  matching) but set it anyway; target_y_mm + target_z_mm are the
  disambiguating fields.

## Overall

- assembly_name: "angular_pneumatic_gripper"
- overall_envelope_mm: {x: 100, y: 235, z: 50}
  (base X=-50..50, Y=-45..45, Z=0..50; jaw tips reach world Y≈-187.5 at
  the static pose; z=50 is the base height, which dominates the Z extent
  at the static pose.)
- expected_part_count: 7
  (1 gripper_base + left/right_jaw_proximal + left_jaw_distal +
  right_jaw_distal(reuse) + left_linkage + right_linkage(reuse).)
- special_features:
  - "1 actuated DOF: the implicit internal pneumatic cylinder drives the
    two main jaw pivots (±25 deg) simultaneously; 5 passive clevis
    hinges (2 jaw knuckles + 2 link pivots on the jaws). All joint
    hardware is clevis hinges (pin axis X, motion in the YZ plane)."
  - "The base's two lower Set-L forks and the linkage rear tongues
    coincide exactly at the static pose (geometric loop closure) but
    carry NO mate — the mate graph is a single tree rooted at
    gripper_base (assembly engine + URDF export are tree-shaped)."
  - "Jaws are complex multi-element forms (arm + side prong + distal
    segment with Z-axis cylinder tip), NOT straight sticks."
  - "right_jaw_distal and right_linkage reuse their left counterparts'
    geometry via reuses_part_id (X-symmetric parts); right_jaw_proximal
    is generated separately (mirrored prong side)."
  - "all clevis params come from the fixed Sets F and L — they satisfy
    the hinge rectangle rule ear_length > ear_width and
    _clevis_validate; do not modify them."

## Deviations from the reference mechanism (documented, deliberate)

1. The 4-bar linkage loop of a real angular gripper (base -> linkage ->
   jaw -> base) is NOT mated as a closed loop: the assembly engine and
   URDF export are tree-shaped, and a closed mate chain is rejected.
   Instead each linkage hangs on its jaw's prong fork, and the linkage
   rear tongue geometrically plugs into the base's lower fork at the
   static pose (bores coincide exactly). The mechanism LOOKS closed
   while remaining a valid tree.
2. The 7th hinge of the reference mechanism (inside the complex base)
   is simplified away; the actuated main pivots are driven directly.
3. The pneumatic cylinder is implicit (base block only), not modeled.
4. Pivot-to-jaw-tip reach is ~127 mm (reference class ~100 mm): the
   knuckle fork and distal segment add length. Segment proportions
   stay in the compact-gripper class.
5. Jaw rotation about X means the jaws open in the vertical (YZ) plane
   (V-opening), matching the pin-axis-X convention used throughout
   this pipeline.
