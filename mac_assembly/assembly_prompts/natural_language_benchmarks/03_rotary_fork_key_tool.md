# Rotary Fork-Key Tool

Create a compact robot-side tool that engages and turns an external tab or
wing-shaped knob. Use millimetres throughout. The finished mechanism has
exactly four physical parts: a mounting plate, a fixed bearing sleeve, a
rotating spindle, and a fork-shaped key head. Do not include the external
knob, a motor, bearings, screws, a robot arm, or any additional mechanism.

The mounting plate is a 70 by 70 millimetre square plate, 8 millimetres thick,
centred on X and Y zero and occupying Z zero to 8. Add four vertical mounting
holes of radius 3 millimetres at X and Y coordinates plus or minus 26
millimetres, plus a central vertical through-bore of radius 6.2 millimetres.
Keep the plate faces flat around the central opening.

The fixed sleeve is a cylindrical tube of outer radius 12 millimetres, inner
radius 6.2 millimetres, and length 28 millimetres. Mount it coaxially below
the plate, with its upper annular face touching the plate underside at Z zero
and its body extending to Z minus 28. The sleeve and plate are rigidly joined
without overlapping material.
The stated negative height is the sleeve's final assembled position; its
separate-part model may use a different local origin. Whatever local origin
is chosen, the assembled sleeve must span Z minus 28 to zero, with no part
of the sleeve above the mounting plate's underside.

The spindle is a single connected rotating part. Its main vertical shaft has
radius 5.5 millimetres and length 40 millimetres. In the neutral assembled
pose the bare shaft spans Z minus 32 to plus 8. Fuse a flange of radius 9
millimetres and thickness 4 millimetres around each end: the lower flange
spans Z minus 36 to minus 32 and the upper flange spans Z plus 8 to plus 12.
The complete spindle therefore spans Z minus 36 to plus 12. The lower
flange lies below the sleeve and the upper flange lies above the plate, so
neither large flange can enter the guide bore. The shaft has 0.7 millimetres
of radial clearance inside the plate and sleeve.
The lower surface of the upper flange must stay entirely at or above Z plus
8, including any small overlap introduced when fusing it to the shaft; a
visually negligible extension into the plate is still a real collision.

The replaceable key head is rigidly fixed beneath the spindle. It has a
central circular hub of radius 9 millimetres and thickness 6 millimetres, with
two downward rectangular prongs fused into its underside. Each prong is 6
millimetres wide along X, 12 millimetres deep along Y, and 16 millimetres
long along Z. Their inner faces are parallel and separated by a clear
14-millimetre slot that remains open from below. The complete key head spans
approximately 26 by 18 by 22 millimetres and occupies Z minus 58 to minus 36
in the assembled pose. Both prongs must overlap the hub enough to form one
connected solid.

The plate is the root and the sleeve is fixed to it. The spindle rotates
about their common vertical Z axis from minus 180 to plus 180 degrees, with
zero degrees as the neutral pose. This spindle rotation is the only active
degree of freedom. The key head is a rigid child of the spindle and must
rotate with it. The full rotation must remain collision-free, and the
14-millimetre working slot must stay unobstructed.

Deliver separate-part STEP geometry, an assembled STL and GLB, and a URDF
that preserves the single full-range revolute joint.
