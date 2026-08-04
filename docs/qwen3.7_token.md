# 3D Modeling Token Consumption Statistics

## Unit Price Reference

Model: `qwen3.7-max`

| Item | Unit Price (CNY / million tokens) |
|---|---:|
| input | 6 |
| cache_creation | 7.5 |
| cache_read | 0.6 |
| output | 18 |

## Prompt 1 
> Create a single solid STEP model in millimeters. The part is a rectangular block, 100 mm long in X, 60 mm wide in Y, and 20 mm tall in Z. Center the block on the XY origin, with the bottom face at Z = 0. Add four vertical through-holes, each 8 mm in diameter, located at X = +/-35 mm and Y = +/-20 mm. Add a 2 mm chamfer to the top perimeter edges only. Do not chamfer the holes.

**Geometric Features**:
- Single solid body (not an assembly; all features merged into one STEP solid)
- Main body is a rectangular block
- Four vertical through-holes (running the full block height along Z)
- Through-holes arranged in a 2×2 symmetric array
- Hole positions symmetric about both X and Y axes
- Chamfer applied only to the top perimeter outer edges
- No chamfer on hole inner walls (hole openings stay sharp), bottom face, or side faces

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 7/7 | 212,689 | 0 | 5,413,760 | 55,878 | 5,682,327 | 112 | **5.53** |
| multi agent | 7/7 | 23,453 | 0 | 0 | 9,156 | 32,609 | 3 | **0.31** |

## Prompt 2 
> Create a single solid circular flange as a STEP model in millimeters. The flange is a cylinder with an outside diameter of 80 mm and a thickness of 10 mm. Its axis is vertical along Z, with the bottom face at Z = 0 and the center at X = 0, Y = 0. Add a central vertical through-bore with diameter 30 mm. Add six equally spaced vertical through-holes, each 6 mm in diameter, on a 60 mm bolt-circle diameter. Add a 1.5 mm fillet to the top and bottom outside circular edges.

**Geometric Features**:
- Single solid body
- Overall shape is a flat cylindrical flange
- Rotationally symmetric about the Z axis
- Central vertical through-bore coaxial with the outer circle
- Central bore runs through the full thickness
- Six vertical through-holes equally spaced around the circumference (6-fold rotational symmetry)
- Six holes run through the full thickness
- Top outer circular edge has a fillet
- Bottom outer circular edge has a fillet
- Central bore edges have no fillet

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 10/10 | 412,097 | 0 | 6,625,536 | 89,885 | 7,127,518 | 126 | **8.07** |
| multi agent | 10/10 | 24,179 | 0 | 0 | 10,648 | 34,827 | 3 | **0.34** |

## Prompt 3
> Create a single solid L-bracket STEP model in millimeters. The bracket has a horizontal base plate 80 mm long in X, 50 mm wide in Y, and 8 mm thick in Z. Center the base plate on the XY origin, with its bottom at Z = 0. Add a vertical back plate along the rear long edge of the base. The back plate is 80 mm long in X, 8 mm thick in Y, and 50 mm tall in Z, rising from the top of the base plate. The back plate should sit along the rear edge at positive Y. Add two vertical through-holes in the base plate, each 6 mm in diameter, located at X = +/-25 mm and Y = -10 mm. Add two horizontal through-holes in the vertical plate, each 6 mm in diameter, located at X = +/-25 mm and Z = 30 mm, passing through the 8 mm thickness of the vertical plate. Add two triangular gussets, each 8 mm thick in X, located at X = +/-20 mm. Each gusset should connect the base plate to the back plate with a right-triangle side profile 30 mm tall and 30 mm deep. Add 2 mm fillets to the outside corner where the base and back plate meet.

**Geometric Features**:
- Single solid body
- Overall L-shape (side view shows an L outline)
- Composed of a horizontal base plate + vertical back plate
- Back plate stands vertically on the rear long edge of the base (positive Y side)
- Back plate and base plate share the same X length
- Back plate rises from the top of the base plate
- Two vertical through-holes in the base plate (along Z)
- Base-plate hole positions symmetric about the X axis
- Two horizontal through-holes in the back plate (along Y)
- Back-plate hole positions symmetric about the X axis
- Two triangular reinforcement gussets symmetrically distributed
- Gussets connect the base plate to the back plate
- Gusset side profile is a right triangle
- Fillet at the outer corner where base and back plate meet

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 14/14 | 457,731 | 0 | 8,393,600 | 293,629 | 9,144,960 | 113 | **13.07** |
| multi agent | 14/14 | 51,886 | 0 | 0 | 42,618 | 94,504 | 5 | **1.08** |

## Prompt 4
> Create a single solid stepped shaft STEP model in millimeters. The shaft axis runs along X. The total length is 120 mm. The left end center is at X = 0, Y = 0, Z = 0. From X = 0 to X = 30, the shaft diameter is 20 mm. From X = 30 to X = 90, the shaft diameter is 30 mm. From X = 90 to X = 120, the shaft diameter is 20 mm. Add a 1 mm chamfer to both end edges. Add a rectangular keyway slot on the top of the 30 mm diameter middle section. The keyway is 6 mm wide in Y, 3 mm deep in Z, and runs from X = 40 to X = 80. Export as a STEP file.

**Geometric Features**:
- Single solid body
- Overall shape is a stepped shaft
- Axis runs along X
- Three coaxial cylindrical sections (left, middle, right)
- Middle section has the largest diameter; the two end sections are smaller (thick in the middle, thin at the ends)
- Left and right end sections have equal diameters (symmetric about the middle section)
- Chamfer on both end face edges
- Rectangular keyway on top of the middle section (the thickest part)
- Keyway runs along X (does not span the full length)
- Keyway is on the top (does not cut through the diameter)
- Keyway has a fixed Y-direction width and Z-direction depth

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 11/11 | 206,212 | 0 | 6,293,248 | 84,450 | 6,583,910 | 106 | **6.53** |
| multi agent | 11/11 | 27,069 | 0 | 0 | 22,538 | 49,607 | 3 | **0.57** |

## Prompt 5
> The outer shape is a rectangular box 100 mm long in X, 70 mm wide in Y, and 30 mm tall in Z. Center it on the XY origin, with the bottom face at Z = 0. The enclosure is open at the top. The wall thickness is 3 mm and the bottom floor thickness is 3 mm. Add four internal cylindrical standoffs rising from the inside floor. Each standoff has an outside diameter of 10 mm and a height of 12 mm above the inside floor. Place the standoffs at X = +/-35 mm and Y = +/-25 mm. Add a centered blind hole in each standoff, 3 mm in diameter and 8 mm deep from the top of the standoff. Add 2 mm radius fillets to the four outside vertical corners of the enclosure.

**Geometric Features**:
- Single solid body
- Overall shape is a rectangular box (enclosure)
- Open at the top (no top lid / no ceiling face)
- Uniform wall thickness (all four walls equally thick)
- Uniform bottom thickness (bottom plate evenly thick)
- Four internal cylindrical standoffs
- Standoffs rise from the inside floor (not connected to the outer walls)
- Standoff positions symmetric about both X and Y axes
- Each standoff has a coaxial blind hole at its center
- Blind holes do not pierce the bottom plate (depth is less than standoff + bottom thickness)
- Fillets on the four outer vertical corners of the box
- No fillets on inner vertical corners, top edges, or standoffs

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 11/12 (item 11 not satisfied) | 199,344 | 0 | 1,961,344 | 27,957 | 2,188,645 | 53 | **2.88** |
| multi agent | 12/12 | 25,927 | 0 | 0 | 11,430 | 37,357  | 3 | **0.36** |

## Prompt 6
> Create a single solid aerospace-style clevis bracket as a STEP model in millimeters. The part is symmetric about the XZ plane. Start with a base plate 120 mm long in X, 60 mm wide in Y, and 10 mm thick in Z, centered on the XY origin, with bottom face at Z = 0. Add two vertical clevis lugs rising from the top of the base near the center. Each lug is 18 mm thick in Y, 42 mm tall above the base, and extends 36 mm along X. The two lugs are separated by a 16 mm central gap in Y. The top of each lug has a semicircular rounded profile with radius 18 mm when viewed from the side. Add a horizontal through-hole of diameter 14 mm through both lugs along the Y direction, centered at X = 0 and Z = 34 mm. Add four base mounting holes, diameter 7 mm, through the base plate, located at X = +/-45 mm and Y = +/-20 mm. Add two triangular lightening cutouts through the base web, one on each side of the clevis, each with rounded corners of radius 3 mm. Add two diagonal reinforcing ribs from the base to the outer faces of the lugs, one on each side, thickness 6 mm. Add 3 mm fillets to the base perimeter and 2 mm fillets at lug-to-base transitions.

**Geometric Features**:
- Single solid body
- Overall shape is a U-shaped clevis bracket
- Symmetric about the XZ plane (mirror symmetry in Y)
- Two vertical clevis lugs standing on top of the base plate
- Two lugs located near the center of the base (not at the edges)
- Y-direction gap between the two lugs
- Top of each lug has a semicircular profile (side view)
- Horizontal through-hole pierces both lugs (along Y)
- Through-hole coaxial across both lugs
- Four base mounting holes pierce the base plate
- Base mounting holes in a 2×2 symmetric array
- Two triangular lightening cutouts pierce the base web
- Lightening cutout inner corners have fillets
- Two diagonal reinforcing ribs connect the base to the outer faces of the lugs
- Ribs symmetrically distributed (one on +Y, one on -Y)
- Larger fillet on the base perimeter edges
- Smaller fillet at the lug-to-base junction
- No fillets elsewhere

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 16/18 (items 16 and 18 not satisfied) | 677,783 | 0 | 12,261,248 | 210,211 | 13,149,242 | 171 | **15.21** |
| multi agent | 18/18 | 157,815 | 0 | 0 | 119,799 | 277,614 | 13 | **3.10** |

## Prompt 7
> Create a single solid radial-engine-style cylinder as a STEP model in millimeters. The main cylinder axis is vertical along Z and centered at the origin.Create a central barrel with diameter 36 mm and height 70 mm, bottom at Z = 0.Around the barrel, add 12 horizontal circular cooling fins. Each fin is 2 mm thick in Z, has outside diameter 62 mm, and is spaced every 5 mm from Z = 10 mm to Z = 65 mm. Add a thicker base flange at the bottom, outside diameter 70 mm and thickness 8 mm, with six vertical mounting holes of diameter 5 mm on a 56 mm bolt circle. Add a top cap cylinder, diameter 44 mm and height 8 mm, from Z = 70 mm to Z = 78 mm. Add an angled spark-plug boss protruding from the side of the top cap. The boss is a cylinder of diameter 12 mm and length 24 mm, angled upward at 35 degrees from horizontal, with its axis pointing outward in the positive X direction. Add a 5 mm diameter hole through the boss along its own axis.Add small 1 mm fillets to the outer fin edges and base flange edges.

**Geometric Features**:
- Single solid body
- Overall shape is a radial-engine-style cylinder
- Main cylinder axis vertical along Z, centered at the origin
- 12 horizontal circular cooling fins around the barrel
- Fins equally spaced along Z
- Fin outer diameter larger than barrel outer diameter
- Each fin is a thin disk
- Thicker base flange at the bottom, coaxial with the barrel
- Base flange outer diameter larger than fin outer diameter
- Six vertical mounting holes pierce the base flange
- Mounting holes equally spaced around the circumference (6-fold rotational symmetry)
- Top cap cylinder at the top, diameter larger than barrel diameter but smaller than fin outer diameter
- Spark-plug boss protrudes from the side of the top cap
- Boss axis angled upward (35° from horizontal)
- Boss through-hole pierces the full length of the boss
- Small fillet on the outer edges of the fins
- Small fillet on the outer edge of the base flange

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 17/17 | 809,241 | 0 | 13,545,088 | 246,802 | 14,601,131 | 150 | **17.42** |
| multi agent | 17/17 | 30,679 | 0 | 0 | 19,334 | 50,013 | 3 | **0.53** |

## Prompt 8
> Create a single solid centrifugal impeller as a STEP model in millimeters. The impeller axis is vertical along Z and centered at the origin. Add a circular backplate disk with outside diameter 90 mm and thickness 6 mm, with its bottom face at Z = 0. Add a central hub cylinder on top of the backplate, diameter 26 mm and height 22 mm above the backplate. Add a vertical through-bore of diameter 8 mm through the entire part. Add 12 identical backward-curved blades on top of the backplate, equally spaced around the hub. Each blade begins at radius 18 mm and ends at radius 43 mm. Each blade is 3 mm thick, 16 mm tall above the backplate, and curves backward by approximately 45 degrees from root to tip. The blade tips should lean opposite the direction of rotation when viewed from above.Add 1 mm fillets at the blade roots where they meet the backplate and hub. Add a 1.5 mm fillet to the top and bottom outer circular edges of the backplate.

**Geometric Features**:
- Single solid body
- Overall shape is a centrifugal impeller
- Flat cylindrical backplate disk as the base
- Central hub cylinder stands on top of the backplate
- Hub coaxial with the backplate
- Vertical through-bore pierces the entire part (from backplate bottom to hub top)
- 12 identical backward-curved blades
- Blades stand on top of the backplate
- Blades equally spaced around the hub (12-fold rotational symmetry)
- Each blade extends from an inner radius (near the hub) to an outer radius
- Blade tips lean backward (opposite the direction of rotation)
- Blades have fixed thickness and height
- Fillet at the blade roots (where they meet the backplate and hub)
- Fillet on the top outer circular edge of the backplate
- Fillet on the bottom outer circular edge of the backplate

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 15/15 | 2,089,788 | 0 | 21,905,920 | 392,906 | 24,388,614 | 211 | **32.75** |
| multi agent | 14/15 (item 13 not satisfied) | 80,922 | 0 | 0 | 39,733 | 120,655 | 7 | **1.20** |

## Prompt 9
> Create a single STEP model of a miniature spiral staircase in millimeters. The staircase is centered on the origin and rises along Z. Add a central vertical column, diameter 14 mm and height 140 mm, with its bottom at Z = 0. Add 20 identical wedge-shaped stair treads arranged helically around the column. Each tread is 4 mm thick, has an inner radius of 10 mm, an outer radius of 62 mm, and subtends 24 degrees in plan view. The first tread is at Z = 4 mm, and each subsequent tread rises by 6 mm and rotates by 18 degrees around Z. Add a helical outer handrail tube of diameter 5 mm following radius 66 mm, starting at Z = 14 mm and ending at Z = 130 mm, making one full revolution around the staircase. Add 20 vertical balusters, each diameter 3 mm, connecting the outer end of each tread to the handrail. Add a circular base disk, diameter 90 mm and thickness 5 mm.

**Geometric Features**:
- Single solid body
- Overall shape is a spiral staircase
- Rises along Z
- Central vertical column
- 20 identical wedge-shaped stair treads
- Treads rise in equal Z increments
- Treads rotate in equal angular increments around Z
- Tread inner ends approach the column (this feature is physically infeasible and would cause the model to collapse and fracture)
- Each tread subtends a fixed sector angle in plan view
- Helical handrail tube spirals up around the outer circumference
- Handrail tube positioned above the outer ends of the treads
- 20 vertical balusters
- Each baluster at the outer end of a corresponding tread
- Each baluster connects a tread's outer end to the handrail tube
- Circular base disk
- Base disk has a fixed thickness and a diameter larger than the column diameter

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 16/16 | 487,907 | 0 | 10,189,056 | 208,818 | 10,885,781 | 135 | **12.80** |
| multi agent | 17/16 (fixed item 8) | 71,493 | 0 | 10,496 | 56,503 | 138,492 | 6 | **1.45** |

## Prompt 10
> Create a visually clear simplified planetary gear assembly as a STEP model in millimeters. The assembly lies flat in the XY plane with gear axes along Z. Use separate solid bodies for the sun gear, three planet gears, ring gear, carrier plate, and three planet pins. All gears are 8 mm thick. Use simplified straight-sided trapezoidal teeth rather than true involute teeth. The sun gear has 24 external teeth, pitch diameter 48 mm, root diameter 42 mm, and outside diameter 54 mm. The three planet gears each have 18 external teeth, pitch diameter 36 mm, root diameter 31 mm, and outside diameter 41 mm. Place the planet gear centers on a 42 mm radius circle, equally spaced every 120 degrees. The ring gear is concentric with the sun gear, has 60 internal teeth, internal pitch diameter 120 mm, internal root diameter 126 mm, internal tooth-tip diameter 114 mm, and outside diameter 140 mm. Add a thin circular carrier plate below the gears, diameter 105 mm and thickness 4 mm, located from Z = -5 mm to Z = -1 mm. Add three vertical planet pins, each diameter 6 mm and height 14 mm, centered under the planet gears. Add a central sun bore of diameter 10 mm.

**Geometric Features**:
- Multiple independent solid bodies (not a single body; 5 part types each independent)
- Overall shape is a planetary gear assembly
- Lies flat in the XY plane
- Contains 5 part types: sun gear, 3 planet gears, ring gear, carrier plate, 3 planet pins
- Sun gear at the center
- Sun gear has external teeth (teeth on the outer circumference)
- 3 planet gears
- Planet gears have external teeth
- Planet gears equally spaced around the circumference (3-fold rotational symmetry, 120° apart)
- Planet gears located between the sun gear and the ring gear
- Ring gear coaxial with the sun gear
- Ring gear has internal teeth (teeth on the inner circumference)
- Ring gear at the outermost circumference
- Carrier plate below the gears
- Carrier plate is a flat disk
- 3 planet pins below the planet gears
- Pins at the same positions as the planet gear axes
- All gears equally thick (same Z-direction thickness)
- Simplified straight-sided trapezoidal teeth (not involute tooth profile)
- Central sun bore pierces the sun gear
- Gears mesh radially (tooth counts and positions ensure meshing)

| Category | Pass Rate | input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cad skill | 21/21 | 418,774 | 0 | 9,604,096 | 175,191 | 10,198,061 | 130 | **11.43** |
| multi agent | 21/21 | 30,501 | 0 | 0 | 30,161 | 60,662 | 4 | **0.73** |

## MAC
## Prompt test1

![test1 result](assets/show1.gif)
> Create a single solid desktop organizer in millimeters. The main body is a hexagon with a flat-to-flat distance of 80 mm and a height of 40 mm, centered on the origin with its bottom at Z = 0. Create a honeycomb pattern of vertical hexagonal pockets descending from the top face. Each pocket has a flat-to-flat distance of 14 mm and a depth of 25 mm, ending at Z = 15. The pockets should be arranged in a hexagonal grid with a 2 mm wall thickness between them. Leave a 5 mm solid border around the outer edge of the main body where no pockets are cut. On all 6 vertical sides of the main body, add a rectangular cutout for paperclips near the bottom of each side, 35 mm wide along the side, 10 mm deep into the body, and 8 mm tall in Z, starting from Z = 3 and extending to Z = 11, leaving a solid bottom base. Add a 1.5 mm fillet to all top perimeter edges of the main body."

| input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---:|---:|---:|---:|---:|---:|---:|
| 67,329 | 0 | 0 | 43,865 | 111,194 | 6 | **1.19** |

## Prompt test2

![test2 result](assets/show2.gif)
> Create a single solid gyroscope-style desk ornament as a STEP model in millimeters. It consists of three concentric rings and a central sphere, all centered at the origin (0,0,37). The outer ring has an outer radius of 30 mm, an inner radius of 25 mm, and a width of 8 mm in Z. The middle ring has an outer radius of 23 mm, an inner radius of 19 mm, and a width of 8 mm, but is rotated 45 degrees around the X axis. The inner ring has an outer radius of 17 mm, an inner radius of 14 mm, and a width of 8 mm, rotated 90 degrees around the Y axis. Add a solid central sphere of radius 11 mm. Connect the inner ring to the central sphere with two 4 mm diameter cylindrical pegs, embedded at least 3 mm into both bodies. Connect the middle ring to the inner ring with two 4 mm pegs, embedded at least 3 mm into both bodies. Add a cylindrical base stand at the bottom, diameter 45 mm and height 8 mm, resting on the XY plane at Z=0. Connect the base to the outer ring with a central vertical trunk of diameter 14 mm, adding 3 mm fillets at the intersections with the base and the outer ring. Add a 1.5 mm chamfer to the top edge of the base.

| input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---:|---:|---:|---:|---:|---:|---:|
| 66,683 | 0 | 4,224 | 63,870 | 134,777 | 6 | **1.55** |

## Prompt test3

![test3 result](assets/show4.gif)
> Create a single solid STEP model of a smartphone stand in millimeters. BASE: Create a rectangular base centered at X=0, Y=0, with dimensions 100 mm in X, 80 mm in Y, and 6 mm in Z, with bottom face at Z=0. Apply 8 mm fillets to the four vertical corner edges of the base. PANEL: Create a sketch in the YZ plane (perpendicular to the X axis) using control_points forming a parallelogram with vertices (Y=15.00, Z=6.00), (Y=69.94, Z=123.82), (Y=74.47, Z=121.71), (Y=19.53, Z=3.89). Extrude this sketch 75 mm along the X axis, centered on X=0 (X spans -37.5 to +37.5). The resulting panel is tilted backward at 65 degrees to horizontal, with its front-bottom edge at (Y=15, Z=6) and front face normal pointing toward -Y and +Z. LIP BLOCKS: Add two rectangular lip blocks on the base top surface, directly in front of the panel bottom. Each block is 20 mm wide in X, 5 mm deep in Y, 4 mm high in Z. Right lip block: X=10 to X=30, Y=10 to Y=15, Z=6 to Z=10. Left lip block: X=-30 to X=-10, Y=10 to Y=15, Z=6 to Z=10. The two blocks are symmetric about X=0 with a 20 mm central gap. RIBS: Add two triangular reinforcement ribs on the +Y (rear) side of the panel, one on the left and one on the right, symmetric about X=0. Each rib is a right triangle in the YZ plane with vertices (Y=20, Z=6), (Y=65, Z=6), (Y=20, Z=41) — horizontal leg 45 mm along the base top, vertical leg 35 mm. Right rib: place the YZ sketch at X=30 and extrude 4 mm in the -X direction (so the rib spans X=26 to X=30). Left rib: place the YZ sketch at X=-30 and extrude 4 mm in the +X direction (so the rib spans X=-30 to X=-26). FINISHING: Apply 2 mm fillets to all exposed outer edges. Apply 1 mm chamfer to the bottom perimeter edge of the base.

## Prompt test4

![test4 result](assets/show3.gif)
> Create a single solid STEP model in millimeters. The model is a decorative lighthouse ornament consisting of a circular base, a tapered tower, an observation platform, a lantern room, a conical roof, and embossed architectural details including a doorway and windows. The overall height of the model is 180 mm, with a maximum diameter of 70 mm. The bottom face of the base is located at Z = 0, and the entire model is centered on and symmetric about the Z axis. Create a circular base with a diameter of 70 mm and a height of 12 mm. The bottom face is completely flat, and the top face is horizontal. Apply R3 fillets to the outer edge of the base. Create the lighthouse tower at the center of the base. The tower is a frustum of a cone with a height of 110 mm, a bottom diameter of 42 mm, and a top diameter of 30 mm. The tower is concentric with the base and smoothly connected to it. Create a circular observation platform at the top of the tower. The platform is a cylinder with a diameter of 46 mm and a height of 6 mm. It is concentric with the tower and extends outward to form an overhanging circular balcony. Below the observation platform, create eight equally spaced rectangular support brackets. Each bracket measures 6 mm wide, 4 mm thick, and 12 mm high. Arrange the brackets uniformly around the tower at 45-degree intervals, connecting the upper portion of the tower to the underside of the observation platform. At the center of the observation platform, create the lantern room as a cylinder with a diameter of 26 mm and a height of 24 mm. Cut eight identical vertical rectangular windows into the lantern room. Each window measures 6 mm wide, 14 mm high, and 2 mm deep. Distribute the windows evenly around the circumference at 45-degree intervals. Create a conical roof on top of the lantern room. The roof has a height of 22 mm and a base diameter of 30 mm, tapering continuously to a single point. The base of the roof is flush with the top surface of the lantern room. Create an embossed entrance on the front side of the tower near its base. The entrance is recessed 2 mm into the tower wall and measures 12 mm wide and 22 mm high. The bottom of the entrance is located 8 mm above the top surface of the base. The top of the doorway is a semicircular arch with a radius of 6 mm. Create three horizontal rows of recessed windows on the tower wall. The center heights of the rows are 40 mm, 65 mm, and 90 mm above the top surface of the base. Each row contains four identical rectangular windows evenly spaced at 90-degree intervals around the tower. Each window measures 5 mm wide, 10 mm high, and 2 mm deep. Create five horizontal decorative bands around the outer surface of the tower. Each band is 3 mm wide and protrudes 1 mm from the tower surface. Position the bands at heights of 20 mm, 42 mm, 64 mm, 86 mm, and 108 mm above the top surface of the base. Apply R1 fillets to all exposed sharp edges unless otherwise specified. Apply R3 fillets to the outer edge of the base and R2 fillets to the bottom edge of the conical roof. Do not apply additional chamfers or fillets to the recessed doorway or window edges. The final model must be a single watertight solid suitable for FDM or SLA 3D printing. The finished object should resemble a traditional coastal lighthouse ornament with a tapered tower, circular observation platform, lantern room, and conical roof.

## Prompt test5

![test5 result](assets/show5.gif)
> Create a print-in-place ball-in-cage fidget toy consisting of two separate solid bodies. Body 1 (the Cage): A cube of 40x40x40 mm centered at the origin. Cut a spherical hollow inside the cube with a radius of 16 mm, centered at the origin. To make the ball visible and touchable, cut circular through-holes of radius 12 mm on all six faces of the cube (extrude cuts along X, Y, and Z axes through the entire cube). Add a 2 mm fillet to all outer straight edges of the cube. Body 2 (the Ball): A solid sphere with a radius of 15 mm centered at the origin. Note that the ball is physically separated from the cage by a 1 mm clearance everywhere. Ensure the final result is a multi-body part containing both bodies. 

| input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---:|---:|---:|---:|---:|---:|---:|
| 29,444 | 0 | 4,224 | 19,059 | 52,727 | 4 | **0.52** |

## Prompt test6

![test6 result](assets/show6.gif)
> Create a print-in-place articulable gyroscope toy consisting of two separate bodies. Body 1 (Outer Ring): A cylindrical ring with an outer radius of 30 mm, an inner radius of 23 mm, and a height of 10 mm, centered at the origin on the XY plane. Cut 12 evenly spaced gear-like notches (2 mm deep, 3 mm wide) along the outer perimeter of the outer ring. Cut two circular holes of radius 2.4 mm entirely through the outer ring along the X-axis. Body 2 (Inner Spinner): A cylindrical ring with an outer radius of 22 mm, an inner radius of 15 mm, and a height of 10 mm, centered at the origin. Cut 8 evenly spaced gear-like notches (2 mm deep, 3 mm wide) along the inner perimeter of the inner ring. Add two cylindrical pivot pins protruding outward along the X-axis from the outer surface of the inner ring. The pins should have a radius of 2.0 mm and extend 6 mm outward, reaching into the holes of the outer ring. The 0.4 mm difference in radii ensures a clearance gap so the inner ring can spin freely. Add a 1 mm chamfer to the top and bottom outer edges of both rings. Ensure the final result is a multi-body part containing both bodies.

| input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---:|---:|---:|---:|---:|---:|---:|
| 78,352 | 0 | 4,224 | 44,003 | 126,579 | 7 | **1.26** |

## Prompt test7

![test7 result](assets/show7.gif)
> Create a print-in-place articulable multi-link chain consisting of 5 separate interlocking bodies. Body 1 (Link 1): A solid torus with a major radius of 10 mm and a minor radius of 2.5 mm, lying flat on the XY plane, centered at the origin. Body 2 (Link 2): An identical solid torus (major radius 10 mm, minor radius 2.5 mm). Rotate this torus 90 degrees around the X-axis, and translate it 13 mm along the positive X-axis to interlock with Body 1. Body 3 (Link 3): An identical torus with the same orientation as Body 1 (flat on the XY plane), translated 26 mm along the positive X-axis to interlock with Body 2. Body 4 (Link 4): An identical torus with the same orientation as Body 2 (rotated 90 degrees around the X-axis), translated 39 mm along the positive X-axis to interlock with Body 3. Body 5 (Link 5): An identical torus with the same orientation as Body 1 (flat on the XY plane), translated 52 mm along the positive X-axis to interlock with Body 4. Note: A clearance gap of approximately 0.5 mm is maintained between adjacent links so they remain physically separated and move freely after printing. Keep the result as a multi-body STEP file containing all 5 bodies.

| input | cache_w | cache_r | output | total | API calls | Cost (CNY) |
|---:|---:|---:|---:|---:|---:|---:|
| 30,825 | 0 | 4,224 | 33,063 | 68,112 | 7 | **0.78** |

## Prompt test8

![test8 result](assets/show8.gif)
> Create a classic Geneva mechanism drive wheel. Start with a solid base cylinder of radius 30 mm and height 6 mm centered at the origin on the XY plane. Step 1: Cut a central through-hole of radius 5 mm. Step 2: Create 4 straight slots arrayed at 90-degree intervals. Each slot should be 6 mm wide and extend from a radius of 12 mm all the way to the outer edge. Step 3: Cut 4 circular arcs to create the classic scalloped edges. These 4 cutting cylinders should have a radius of 12 mm, with their centers located at a distance of 35 mm from the origin, arrayed at 45, 135, 225, and 315 degrees. Add a 1 mm chamfer to the top outer edges of the resulting shape.

## Prompt test9

![test9 result](assets/show9.gif)
> Create a sci-fi plasma reactor core as a single solid body. Step 1: Start with a central cylindrical core of radius 15 mm and height 25 mm centered on the XY plane. Cut a 10 mm diameter vertical through-hole down its center. Step 2: Create a regular hexagonal outer ring centered on the XY plane, with an inner circumscribed radius of 40 mm, an outer circumscribed radius of 50 mm, and an extrusion height of 10 mm. Step 3: Connect the central core to the hexagonal outer ring using 6 horizontal rectangular spokes. Each spoke should be 5 mm wide and 10 mm high, evenly arrayed around the Z-axis. Step 4: Create a thin vertical cooling fin (2 mm thick, 25 mm high, extending 5 mm outward from the central cylinder's surface). Array this fin circularly 24 times around the Z-axis and boolean union them with the central core. Step 5: Apply a 1 mm chamfer to the top and bottom outer edges of the hexagonal ring. Finally, boolean union all parts into a single watertight model.

## Prompt test10

![test10 result](assets/show10.gif)
> Create a high-performance automotive ventilated brake disc with internal cooling vanes and cross-drilled holes. Step 1: Create the bottom friction plate as a solid cylinder with a radius of 160 mm and a thickness of 8 mm, centered on the XY plane. Step 2: Create a central mounting hat as a cylinder with a radius of 75 mm and a height of 25 mm, resting directly on top of the bottom plate's center. Boolean union it with the bottom plate. Step 3: Cut a central through-bore with a radius of 35 mm entirely through the center of the assembly. Step 4: Cut 5 vertical bolt holes, each with a radius of 7 mm, arranged on a 52 mm radius bolt circle around the center of the mounting hat. Step 5: Create a single cooling vane. Sketch a rectangle on the XY plane with a length of 80 mm and a width of 5 mm, centered at X=120 mm, Y=0 mm. Extrude this sketch upwards by 12 mm, starting from the top surface of the bottom plate. Step 6: Circularly pattern this cooling vane 36 times around the Z-axis. Boolean union all 36 vanes with the assembly. Step 7: Create the top friction plate. Sketch a ring on the XY plane with an outer radius of 160 mm and an inner radius of 80 mm. Extrude it by 8 mm and place it exactly on top of the cooling vanes. Boolean union it with the rest of the body. Step 8: Add cross-drilled cooling holes. Cut a vertical through-hole of radius 3 mm located at X=110 mm, Y=0. Circularly pattern this cut 18 times around the Z-axis. Cut another vertical through-hole of radius 3 mm at X=140 mm, Y=0, and circularly pattern it 18 times around the Z-axis. Step 9: Apply a 2 mm chamfer to the outer top and bottom circular edges of the complete brake disc assembly.