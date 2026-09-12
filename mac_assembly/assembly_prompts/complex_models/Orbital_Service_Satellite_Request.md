# Orbital Service Satellite — Assembly Request

Create a detailed orbital servicing satellite: a central avionics bus with a
front docking ring, two large deployed solar wings, a tilted high-gain
antenna on top, and twin service arms. Every part is deliberately COMPLEX --
the bus carries the radiator channels, sensor pods, corner rails, and
recessed panels as fused features -- so the whole assembly is only 7
physical instances built from 5 geometries. This is a static CAD showcase:
only three revolute joints actually articulate (antenna pan + two arm base
yaws); every other former joint is baked into the fused part geometry.

All dimensions are millimetres. The mate graph is a shallow open tree rooted
at the central bus: every child mates directly to the bus, tree depth one,
no closed loops. Do not model wires, individual solar cells as separate
parts, free-floating screws, hinge segments inside a solar wing, or any
second mate for a part that already has one.

## Envelope and orientation

- Central bus about 320 X x 280 Y x 260 Z, centered on the world XY origin,
  bus bottom at world Z=0 (bus local frame = world frame).
- Total deployed solar span about 1480 along Y.
- Docking ring on the bus +X face, ring axis world +X.
- Antenna on the bus top near (30, 0); dish tilted toward +X; pans about Z.
- Twin arms on the bus top rear corners near (-90, +-85), extending -X.
- Overall assembly envelope approximately 580 X x 1480 Y x 500 Z.

## Required part tree — exactly 7 parts

```text
central_bus
  -> docking_ring        rigid
  -> solar_wing_left     rigid
  -> solar_wing_right    rigid (reuses solar_wing_left)
  -> antenna_assembly    revolute Z
  -> service_arm_left    revolute Z
  -> service_arm_right   revolute Z (reuses service_arm_left)
```

- Root link `central_bus` anchors everything; tree depth is one.
- Exactly 3 revolute joints and 3 rigid attachments; 6 mates total.
- `solar_wing_right` and `service_arm_right` reuse the left templates'
  geometry verbatim -- separate physical instances, never regenerated.
- Placements: `solar_wing_right` mounts unrotated on the +Y face;
  `solar_wing_left` (the generated template) is placed with a 180-degree
  yaw about Z so its local +Y maps to world -Y (the wing is X-symmetric, so
  this is visually identical); `service_arm_right` mounts unrotated
  (identity orientation) at the opposite seat with the opposite static yaw.

## Part requirements

### central_bus

One connected watertight premium housing, local X=-160..160, Y=-140..140,
Z=0..260. Prefer simple orthogonal box cuts and fused boxes; keep every wall
at least 5 mm thick.

- Chamfer the four vertical corners by about 18.
- Four raised corner rails (26 x 26 section, full height, proud of the side
  faces by 4) at the vertical edges.
- On each +-Y side face: two recessed panels (4 mm deep), symmetric about
  Z=118. KEEP FLAT rectangular landing regions X=-100..100, Z=100..134 on
  both side faces for the solar wing roots -- no recess inside a landing.
- On the -X rear face: six vertical recessed channels (18 wide, 3 deep)
  spread over Y=-105..105 (radiator look).
- On the +X front face: keep a flat circular landing pad R95 centered at
  (160, 0, 130) for the docking ring; any front-face detail stays outside
  it.
- Top face (Z=260): three circular seat recesses, 4 mm deep, all axes
  exactly Z -- antenna seat bore R30 at (30, 0); arm seat bores R26 at
  (-90, -85) and (-90, +85).
- Two rectangular sensor pods fused on the top face at (110, +-60): each
  about 36 X x 44 Y x 28 Z tall with a recessed front aperture facing +X.
  Rectangular pods only -- no horizontal cylinders anywhere in this part.
- Bottom face flat. Solid block aesthetic; no internal cavities.

### docking_ring

Deterministic builder (do not send this part to the single-part LLM):

```json
{"name":"bushing","params":{"outer_radius":92,"inner_radius":62,"length":28}}
```

Its local cylinder axis is Z. During rigid/coaxial assembly align local Z
to world +X and seat the ring's root face flush on the bus front landing
pad, spanning world X=160..188, centered at (174, 0, 130). The R62 central
opening must remain fully open.

### solar_wing — reusable template

One connected panel, local X=-95..95, Y=0..600, Z=-12..12.

- Root mounting block Y=0..40, full 24 mm thick (Z=-12..12); the flat root
  face at local Y=0 seats on a bus side-face landing.
- Panel Y=40..600, 12 mm thick (Z=-6..6): slightly raised surrounding
  frame (3 mm proud), two longitudinal stiffeners and four transverse ribs
  on the +Z face only (keep the -Z face flat).
- One large recessed cell field on the +Z face between the stiffeners,
  with 3-4 shallow groove grid lines (2 mm deep) cut into the SAME solid
  -- never hundreds of separate cell objects.
- Symmetric about local X=0 so the mirrored instance needs no regeneration.

### antenna_assembly

One connected part, local Z=0 at the base pivot; the dish tilt is baked
into the part (the only joint is the base pan).

- Base pivot cylinder R26, local Z=0..40, axis exactly local Z (seats into
  the bus antenna seat bore R30 with a 4 mm radial gap).
- Slewing flange disc R38, Z=40..48, with six shallow recesses on its rim.
- Mast: tapered post (30 x 24 at the base shrinking to 22 x 18), Z=48..150.
- Tilted yoke bracket at the mast top extending toward +X, holding the dish
  axis 35 degrees from vertical toward +X.
- Dish: shallow stepped conical reflector R90 with rim depth about 18,
  centered near local (45, 0, 185), dish axis along the yoke direction. If
  a smooth revolved cone is unreliable, a faceted octagonal plate with
  concentric ring grooves tilted the same way is an acceptable substitute.
- Small rectangular feed probe on the dish front, along the dish axis.
- The only full cylinder in this part is the Z-axis base pivot; no
  horizontal cylinder bores. Local bbox approximately X=-30..120,
  Y=-90..90, Z=0..237.

### service_arm — reusable template

One connected part, local Z=0 at the base pivot, arm extending local -X;
the elbow bend is baked into the geometry (no elbow joint).

- Base pivot cylinder R22, local Z=0..45, axis exactly local Z (seats into
  a bus arm seat bore R26 with a 4 mm radial gap).
- Slewing flange disc R34, Z=45..52.
- Shoulder: tapered box from X=0 to X=-110, about 60 wide (Y) and 40 tall,
  Z=52..92, with two shallow recessed side panels.
- Elbow boss: rectangular block about 24 (X) x 46 (Y) x 56 (Z) at
  X=-110..-133, Z=60..116, with recessed outer faces (motor look --
  rectangular, not cylindrical).
- Forearm: box 46 wide x 26 thick rising at 40 degrees from the elbow
  toward -X and +Z, length about 160; tip near local (-256, 0, 198).
- Wrist/tool block about 40 x 36 x 30 at the forearm tip, with three short
  rectangular prongs around a recessed central aperture, pointing along
  the forearm direction.
- One watertight solid; the only full cylinder is the Z-axis base pivot.
  Local bbox approximately X=-300..0, Y=-32..32, Z=0..230.

## Mates — exactly 6

All three revolute joints use SELECTOR cylinder anchors on BOTH sides with
explicit axis, radius, and target coordinates: the bus seat bore on the
fixed side, the moving part's base pivot cylinder on the moving side. Do not
substitute axis_point anchors on these joints -- the seats are real
cylinders and must be referenced as such. Write every joint's limits into
the actual limit_lower/limit_upper fields, never only in notes.

1. `docking_ring_mount`: rigid/coaxial, bus front landing pad face to the
   ring root face; align the ring's local Z to world +X; ring center lands
   at world (174, 0, 130).
2. `wing_left_mount`: rigid face_to_face, wing root face (local Y=0) seats
   flush on the bus -Y face landing, centered X=0, wing mid-plane at world
   Z=118. The left instance is placed with a 180-degree yaw about Z (local
   +Y maps to world -Y).
3. `wing_right_mount`: rigid face_to_face, same landing on the +Y face,
   unrotated instance.
4. `antenna_pan`: revolute about Z, bus antenna seat bore R30 at (30, 0) to
   the antenna base pivot cylinder R26. Static angle 0 deg.
   Limits -170..+170 deg.
5. `arm_left_yaw`: revolute about Z, bus arm seat bore R26 at (-90, -85) to
   the arm base pivot cylinder R22. Static angle +15 deg (splayed outward).
   Limits -110..+110 deg.
6. `arm_right_yaw`: revolute about Z, bus arm seat bore R26 at (-90, +85)
   to the arm base pivot cylinder R22. Static angle -15 deg (splayed
   outward). Limits -110..+110 deg.

## Static pose and collision requirements

- Nominal pose: wings fully deployed flat; antenna dish tilted toward +X;
  arms raised with elbows up and tools pointing -X/+Z, splayed +-15 deg.
- Each revolute joint is QA-swept +-30 degrees about its static pose with
  all other joints static: keep at least 1 mm clearance to every other part
  throughout. The layout above already guarantees this when dimensions are
  honored: arms sweep entirely ABOVE the bus top plane (world Z>=260)
  while the wings occupy world Z=106..130, so the two never interact; the
  antenna dish stays at world Z>=390, far above the docking ring
  (world Z=38..222) and the sensor pods (world Z=260..288).
- Intended seat/pivot occupancy and flush face contact are allowed;
  unintended volumetric intersections deeper than 0.3 mm are forbidden.
- The docking ring must look fused to the bus front face, not floating;
  keep its R62 aperture open.

## Acceptance

- assembly_name: `orbital_service_satellite`
- expected_part_count: 7; expected_mate_count: 6; 3 revolute DOF.
- unmistakable satellite silhouette: faceted bus, two large wings, tilted
  dish, twin raised arms, front docking ring with an open center.
- reused instances (`solar_wing_right`, `service_arm_right`) are placed
  with assembly transforms, never regenerated as new geometry.
- separate per-part STEP, assembly STEP/STL/GLB, and URDF with explicit
  limits on all three revolute joints.
- no missing mirrored arm or wing, no floating ring, no closed mate loop,
  no merged adjacent parts.
- cosmetic deviations +-10% allowed; the part tree, joint axes, seat/pivot
  radii, and left-right symmetry are mandatory.
