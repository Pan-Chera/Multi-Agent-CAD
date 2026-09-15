# Reusable Five-Digit Dexterous Hand

Create a simplified left robotic hand that demonstrates repeated-part reuse
and articulated kinematics without reproducing every anatomical detail. Use
millimetres throughout. The hand has a broad palm, four parallel fingers on
its forward edge, and one opposed thumb on the radial side near the wrist.
It should look recognisably like a mature robotic hand and be suitable for a
basic simulated grasp.

The palm is one connected rounded rectangular housing approximately 96
millimetres wide along X, 110 millimetres long along Y, and 24 millimetres
thick along Z. It occupies X minus 48 to plus 48, Y minus 50 to plus 60, and
Z zero to 24. Keep broad flat regions at the forward edge for four finger
hinges. Place their centres at X minus 30, minus 10, plus 10, and plus 30
millimetres, near Y plus 71 and Z 12. All four root hinge axes run along X so
the fingers curl in the YZ plane.

Each of the four forward fingers contains three visible physical segments.
All four fingers use identical geometry for corresponding segments; generate
one proximal design, one middle design, and one distal design, then reuse
those shapes for the other fingers. The proximal segment is approximately 18
millimetres wide, 16 millimetres thick, and 34 millimetres long. The middle
segment is 18 by 16 by 22 millimetres. The distal segment is about 18 by 16
by 24 millimetres and ends in a smoothly rounded pad suitable for contacting
a box.

The palm-to-proximal, proximal-to-middle, and middle-to-distal connections are ordinary
single-axis hinges with coaxial bores of radius about 3.5 millimetres and
approximately 0.2 millimetres total side clearance. Every hinge axis runs
along X. Each proximal finger joint curls from zero to approximately 100
degrees, and each of the two subsequent joints curls from zero to approximately
110 degrees. At the final finger joint, the middle segment must have two
connected fork ears with coaxial bores; the distal segment must have a real
central tongue with a through-bore received between those ears. Do not use a
flat butt joint or omit this distal hinge. Fork ears and central tongues
must alternate correctly at every hinge, remain
connected to their owning segment, and never occupy the same material volume.
At each palm-to-finger root hinge, the proximal finger segment must also
carry a connected central tongue with a through-bore; a bare rectangular
finger end inside the palm fork is not a hinge.

The thumb consists of three additional physical segments mounted on the
positive-X side of the palm. Its root is centred near the positive-X side
face, Y minus 35 and Z 12. In the neutral pose the thumb points outward
sideways toward positive X, approximately perpendicular to the four fingers.
It must not point toward positive Y or appear as a fifth upright finger.
Place the spherical socket centre a few millimetres inside the positive-X
palm wall, with its opening on that wall; do not place the cavity wholly
outside the palm. The mating ball and its short stem must be connected to
the proximal thumb body, which starts outside the palm. All three thumb
segments should continue outward in the same approximate sideways direction
in the open neutral pose, while remaining able to curl inward when actuated.
Give the thumb root two rotational
degrees of freedom: one curls it toward the palm and one swings it across the
palm toward the fingertips. A compact spherical coupling with a small radial
clearance is acceptable, provided the ball remains captured and the opening
allows the required motion. Connect the proximal and middle thumb segments
with one additional hinge whose axis runs along X and whose useful curl range
is about zero to 100 degrees. Connect the middle and rounded distal thumb
segments with a further ordinary hinge using a real fork and tongue.

The thumb proximal body is approximately 18 by 28 by 16 millimetres, its
middle body 18 by 15 by 16 millimetres, and its distal body about 18 by 24 by
16 millimetres including the rounded contact pad. Arrange its neutral pose so
the thumb is clearly separated from the palm and can oppose the index and
middle fingertips when curled. No thumb component may begin embedded inside
the palm.

The finished assembly contains sixteen physical parts: one palm, twelve
forward-finger segments, and three thumb segments. Despite those sixteen
instances, only seven distinct geometries should be generated: the palm,
three shared forward-finger segment shapes, and three thumb segment shapes.
The other nine finger segments must reuse the corresponding geometry exactly.
The seven source geometries are embodied by seven of those sixteen installed
parts; they are not seven extra physical template pieces. Nothing may be
added to the assembly solely to serve as a reusable source model.

The palm is the root. The mechanism has sixteen active degrees of freedom:
three for each of the four forward fingers and four for the thumb. Keep the neutral pose open and
roughly mirror-symmetric across the four forward fingers. Reject crossed
fingers, stacked duplicate instances, floating hinge ears, blocked joint
bores, or a thumb that cannot reach the finger workspace.

Deliver separate-part STEP geometry, an assembled STL and GLB, and a URDF
that preserves the repeated geometry instances and all sixteen active degrees
of freedom.
