# Guided Linear Plunger End Effector

Create a compact end effector that presses an external button by moving a
rigid plunger straight downward. Use millimetres throughout. The mechanism
must contain exactly three physical parts: a mounting base, a fixed guide
sleeve, and a moving plunger. Do not model the external button, a spring,
motor, screws, wires, or a robot arm.

The mounting base consists of a 60 by 60 millimetre horizontal plate, 8
millimetres thick, centred at X and Y zero and occupying Z zero to 8. Add four
vertical mounting holes of radius 2.5 millimetres at X and Y coordinates plus
or minus 22 millimetres. Fuse a 30 by 30 by 12 millimetre square support boss
to the underside, centred on the main axis and occupying Z minus 12 to zero.
A vertical guide bore of radius 5.2 millimetres passes completely through the
boss and plate. The bore must remain a real open void.

The fixed guide sleeve is a cylindrical tube with outer radius 9
millimetres, inner radius 5.2 millimetres, and length 32 millimetres. Mount it
coaxially beneath the support boss so its top annular face touches the boss at
Z minus 12 and its body extends down to Z minus 44. The sleeve and base form a
rigid connection without volumetric overlap.

The moving plunger is one connected solid made from a central shaft, a lower
contact head, and an upper stop collar. The shaft radius is 4.5 millimetres
and its length is 54 millimetres. In the retracted assembled pose it spans Z
minus 49 to plus 5. Fuse an eight-millimetre-radius contact head, four
millimetres thick, below the shaft so its flat working face is at Z minus 53.
Fuse an eight-millimetre-radius stop collar, three millimetres thick, around
the upper end from Z plus 2 to plus 5. The head and collar must remain outside
the 5.2-millimetre-radius guide bore at every valid position.

The plunger has one prismatic degree of freedom along the common Z axis. Zero
travel is the retracted pose just described. Increasing the commanded travel
moves the plunger downward, in negative Z, through a total stroke of 12
millimetres. At full extension the contact face is therefore at Z minus 65.
The shaft retains 0.7 millimetres of radial clearance inside both guide bores
throughout the stroke.

The mounting base is the root. The sleeve is rigidly attached to it, and the
plunger is the only moving body. Reject any result with a blocked guide bore,
a disconnected collar or head, lateral plunger motion, an extra active joint,
or collision between the large head or collar and the sleeve.

Deliver separate-part STEP geometry, an assembled STL and GLB, and a URDF
whose positive joint command produces the specified downward 12-millimetre
motion.

