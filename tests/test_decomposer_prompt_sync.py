"""P0-4: static consistency guard between decomposer.md and the real v3
feature-operator implementation (no LLM calls -- pure text checks).

Every assertion mirrors a fact in feature_operators.py / builders.py /
schemas_assembly.py so prompt drift re-breaks the build.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from mac_assembly.builders import BUILDERS
from mac_assembly.feature_operators import _FORK_ROTATIONS

_PROMPT = (
    Path(__file__).resolve().parent.parent
    / "mac_assembly" / "prompts" / "decomposer.md"
).read_text(encoding="utf-8")

_README = (
    Path(__file__).resolve().parent.parent
    / "mac_assembly" / "README.md"
).read_text(encoding="utf-8")


class TestV3ConventionsMatchImplementation(unittest.TestCase):
    def test_direction_vs_pin_axis_documented(self):
        self.assertIn("pin_axis", _PROMPT)
        self.assertIn("surface_axis", _PROMPT)
        # orthogonal combos stated exactly as _FORK_ROTATIONS supports
        self.assertIn("pin_axis=z", _PROMPT.replace("`", ""))
        self.assertIn("pin_axis=x", _PROMPT.replace("`", ""))
        self.assertIn("pin_axis=y", _PROMPT.replace("`", ""))

    def test_rotation_table_combos_all_documented(self):
        text = _PROMPT.replace("`", "")
        for (pin, direction) in _FORK_ROTATIONS:
            # every legal (pin_axis, direction) combo must be expressible
            # from the documented rules: pin=z -> ±x/±y; pin=x -> ±y/±z;
            # pin=y -> ±x/±z
            allowed = {
                "z": ("+x", "-x", "+y", "-y"),
                "x": ("+y", "-y", "+z", "-z"),
                "y": ("+x", "-x", "+z", "-z"),
            }[pin]
            self.assertIn(direction, allowed,
                          f"impl has undocumented combo pin={pin} dir={direction}")
        # and the prompt must state direction=±z IS legal for pin x/y
        self.assertIn("IS legal", text)

    def test_stale_claims_removed(self):
        for stale in (
            "the bore is ALWAYS along Z",
            "the validator rejects it",
            "planar kinematics only",
            "extends INWARD from the edge",
            "only Z-axis pins exist",
        ):
            self.assertNotIn(stale, _PROMPT, f"stale prompt text: {stale!r}")

    def test_v3_bore_at_tip_documented(self):
        text = _PROMPT.replace("`", "")
        self.assertIn("ear_length - ear_width/2", text)
        self.assertIn("OUTWARD", text)
        self.assertIn("ear_length > ear_width", text)

    def test_knuckle_ear_axis_is_direction(self):
        self.assertIn("axis IS `direction`", _PROMPT)

    def test_interface_type_list_includes_ball(self):
        self.assertIn("seat / axis / hinge / slide / fixed / ball", _PROMPT)

    def test_fewshot_palm_geometry_is_consistent(self):
        # plate Y=-40..40; +y forks must attach AT the +Y edge (40), not
        # embedded at y=25/35 as in the stale example
        self.assertIn('"attach_point_mm": [-36, 40, 0]', _PROMPT)
        self.assertIn('"attach_point_mm": [36, 40, 0]', _PROMPT)
        self.assertNotIn('"attach_point_mm": [30, 25, 3]', _PROMPT)
        # ear_length > ear_width in the example params
        self.assertIn('"ear_length": 20, "ear_width": 16', _PROMPT)
        self.assertNotIn('"ear_length": 10, "ear_width": 16', _PROMPT)


class TestReadmeSync(unittest.TestCase):
    def test_builder_count_not_hardcoded_stale(self):
        self.assertNotIn("16 个参数化 builder", _README)
        self.assertIn(str(len(BUILDERS)), _README)

    def test_readme_documents_pin_axis_and_degraded(self):
        self.assertIn("pin_axis", _README)
        self.assertIn("degraded", _README)
        self.assertIn("surface_axis", _README)
        self.assertIn("spec_fingerprint", _README)


if __name__ == "__main__":
    unittest.main()
