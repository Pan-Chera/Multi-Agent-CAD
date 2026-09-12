"""BUG-018: apply_feature's multi-solid fallback only applied a subtractive
feature (through_bore, ball_cavity) to the FIRST base solid that accepted it,
leaving other solids uncut. A bore that should pass through two stacked
plates would only cut the first plate; the second plate would be missing
the hole. The fix separates subtractive from additive fallback: subtractive
tries the cut on EVERY base solid and keeps the cut result for each solid
whose volume actually changed.

No external LLM calls; pure geometry with build123d.
"""
from __future__ import annotations

import unittest
from unittest import mock

from build123d import Align, Box, Compound, Cylinder, Pos

from mac_assembly.feature_operators import apply_feature
from mac_assembly.schemas_assembly import (
    Feature,
    FeatureAttachment,
)


def _two_stacked_plates() -> Compound:
    """Compound of two stacked plates (top z=10..20, bottom z=0..10),
    each 30x30x10 = 9000 mm^3."""
    top = Pos(0, 0, 15) * Box(30, 30, 10, align=(Align.CENTER,) * 3)
    bottom = Pos(0, 0, 5) * Box(30, 30, 10, align=(Align.CENTER,) * 3)
    return Compound(children=[top, bottom])


def _bore_feature(radius=3.0, direction="+z", attach=(0, 0, 10)):
    return Feature(
        name="through_bore",
        params={"radius": radius},
        attachment=FeatureAttachment(
            attach_point_mm=list(attach),
            direction=direction,
        ),
    )


class _RaisingOnCompoundOp:
    """Wrapper that raises on the first call (Compound base) then succeeds
    on subsequent calls (per-solid). Forces the fallback path."""

    def __init__(self, real_op):
        self._real_op = real_op
        self._calls = 0

    def __call__(self, base, feature):
        self._calls += 1
        if self._calls == 1:
            # First call is the whole-compound attempt -- raise to force
            # the multi-solid fallback path.
            raise RuntimeError("simulated BRep_API on compound cut")
        return self._real_op(base, feature)


class TestMultiSolidSubtractiveFallback(unittest.TestCase):
    def test_through_bore_cuts_all_intersecting_solids_primary_path(self):
        """Bore along +Z through two stacked plates -- primary path
        (whole-compound cut) already handles this correctly."""
        base = _two_stacked_plates()
        vols_before = sorted(round(s.volume, 1) for s in base.solids())
        self.assertEqual(vols_before, [9000.0, 9000.0])

        feat = _bore_feature(radius=3.0, direction="+z", attach=(0, 0, 10))
        result = apply_feature(base, feat)

        vols_after = sorted(round(s.volume, 1) for s in result.solids())
        self.assertEqual(len(vols_after), 2)
        for v in vols_after:
            self.assertLess(v, 9000.0 - 200.0,
                             f"each solid must be cut, got {v}")

    def test_through_bore_cuts_all_intersecting_solids_fallback_path(self):
        """BUG-018 core: when the whole-compound op fails (BRep_API),
        the multi-solid fallback must still cut EVERY intersecting solid,
        not just the first. Force the fallback by wrapping the operator
        to raise on the first (compound) call."""
        base = _two_stacked_plates()
        feat = _bore_feature(radius=3.0, direction="+z", attach=(0, 0, 10))

        from mac_assembly import feature_operators as fops
        real_op = fops.FEATURE_OPERATORS["through_bore"]
        wrapper = _RaisingOnCompoundOp(real_op)
        with mock.patch.dict(
            fops.FEATURE_OPERATORS, {"through_bore": wrapper}
        ):
            result = apply_feature(base, feat)

        vols_after = sorted(round(s.volume, 1) for s in result.solids())
        self.assertEqual(len(vols_after), 2,
                         "two-solid Compound must remain two solids")
        # Each plate must lose SOME volume to the bore. The bore only
        # overlaps ~6mm of each 10mm-thick plate, so the cut is ~170 mm^3
        # per plate (not the full pi*r^2*10 ~= 282.7). The bug behavior
        # is that the SECOND solid is left unchanged at 9000.0.
        for v in vols_after:
            self.assertLess(
                v, 9000.0 - 10.0,
                f"fallback path: each intersecting solid must be cut, "
                f"got volume {v} (uncut plate is 9000). BUG-018: second "
                f"solid was left uncut."
            )

    def test_through_bore_disjoint_from_all_solids_raises(self):
        """A bore that doesn't intersect ANY base solid must raise
        removed-no-material (no silent empty cut)."""
        base = _two_stacked_plates()
        feat = _bore_feature(radius=1.0, direction="+z",
                              attach=(100, 100, 10))
        with self.assertRaises(RuntimeError) as ctx:
            apply_feature(base, feat)
        msg = str(ctx.exception).lower()
        self.assertTrue(
            "removed no material" in msg or "no base solid" in msg,
            f"disjoint bore must raise removed-no-material, got: {msg}",
        )


if __name__ == "__main__":
    unittest.main()
