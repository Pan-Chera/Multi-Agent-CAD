# Hinged Twin-Claw Gripper

Create a compact symmetrical two-finger gripper intended for simple pick and
place demonstrations. Use millimetres throughout. The completed mechanism
must contain exactly three physical parts: one mounting plate, one left jaw,
and one right jaw. Do not include pins, motors, fasteners, a robot arm, or a
workpiece as additional parts.

The mounting plate is a horizontal rectangular plate, 140 millimetres wide
along X, 100 millimetres deep along Y, and 22 millimetres thick along Z. Its
centre lies on the Z axis and its bottom is at Z zero. Add four vertical
mounting holes of radius 3.2 millimetres, positioned 55 millimetres to either
side in X and 35 millimetres to either side in Y.

Two downward-facing forked hinge supports are integral with the underside of
the plate. Their hinge centres are at X minus 60 and plus 60 millimetres,
Y zero, and Z minus 15 millimetres. Both hinge axes run horizontally along Y.
The complete supports must remain within the plate width when viewed from the
front. Each support has two ears separated along Y, a five-millimetre-radius
coaxial bore, and enough material around the bore for a robust hinge.

The two jaws are exact mirror images about X zero. Each jaw hangs below its
plate hinge and consists of a 40-millimetre upper rectangular segment joined
to a 50-millimetre lower segment. The visible interior angle at this bend is
120 degrees. The two segments must overlap sufficiently at the bend to form a
broad continuous solid bridge; a point contact, seam, or visible gap is not
acceptable. The lower segment points inward toward the centre of the gripper.

At the top of each jaw, include a central hinge tongue that fits between the
two ears of the corresponding plate support. Its five-millimetre-radius bore
is coaxial with the plate bore along Y. Allow approximately 0.1 millimetres of
clearance on each side. The tongue stem must continue in the same direction
as the upper jaw segment instead of protruding sideways.

Fuse a flat gripping pad into the distal end of each lower segment. Each pad
is approximately 6 millimetres thick along X, 24 millimetres deep along Y,
and 18 millimetres high. Its flat inner face points toward X zero. The pads
must oppose one another and provide broad parallel contact surfaces for a
small box.

Connect each jaw to the plate with one revolute joint about its horizontal Y
axis. Both joints have a nominal angle of zero and a usable range from minus
25 to plus 25 degrees. There is no joint directly between the jaws. The
delivered neutral pose must show the plate horizontal, both jaws below it,
the lower segments pointing inward, and no volumetric collision between the
hinge tongues and fork ears.

Deliver separate-part STEP geometry, an assembled STL and GLB, and a URDF in
which the plate is the root and the two jaw hinges are the only active joints.

