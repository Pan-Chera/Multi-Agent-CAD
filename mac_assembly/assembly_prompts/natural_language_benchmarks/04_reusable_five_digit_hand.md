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

The palm-to-proximal and proximal-to-middle connections are ordinary
single-axis hinges with coaxial bores of radius about 3.5 millimetres and
approximately 0.2 millimetres total side clearance. Every hinge axis runs
along X. Each proximal finger joint curls from zero to approximately 100
degrees, and each middle joint curls from zero to approximately 110 degrees.
The middle and distal segments are rigidly joined, preserving three visible
phalanges while limiting each finger to two active degrees of freedom. Fork
ears and central tongues must alternate correctly at every hinge, remain
connected to their owning segment, and never occupy the same material volume.

The thumb consists of three additional physical segments extending generally
toward negative Y from the positive-X side of the palm. Its root is centred
near X plus 32, Y minus 45, and Z 12. Give the thumb root two rotational
degrees of freedom: one curls it toward the palm and one swings it across the
palm toward the fingertips. A compact spherical coupling with a small radial
clearance is acceptable, provided the ball remains captured and the opening
allows the required motion. Connect the proximal and middle thumb segments
with one additional hinge whose axis runs along X and whose useful curl range
is about zero to 100 degrees. Join the middle and rounded distal thumb segment
rigidly.

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

The palm is the root. The mechanism has eleven active degrees of freedom:
two for each of the four forward fingers, three for the thumb, and no active
joint at the five distal rigid connections. Keep the neutral pose open and
roughly mirror-symmetric across the four forward fingers. Reject crossed
fingers, stacked duplicate instances, floating hinge ears, blocked joint
bores, or a thumb that cannot reach the finger workspace.

Deliver separate-part STEP geometry, an assembled STL and GLB, and a URDF
that preserves the repeated geometry instances and all eleven active degrees
of freedom.

