"""BUG-020: run_part_builder used to silently swallow STL export failures
(`stl_path = None`) and still return ok=True with no degraded flag, no
warning. Downstream mesh-based QA (interference, kinematic sweep) silently
degraded. Fix: keep ok=True (STEP is still valid -- assembly can proceed),
but mark degraded=True and record an STL export warning in PartResult.warnings.

No external LLM calls; STL export is monkeypatched to raise.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mac_assembly.part_generator import run_part_builder
from mac_assembly.schemas_assembly import PartSpec


def _spec(part_id="p1", name="mounting_plate"):
    return PartSpec(
        part_id=part_id,
        part_name=part_id,
        description="plate",
        builder={"name": name, "params": {
            "width": 30.0, "depth": 30.0, "thickness": 5.0,
        }},
    )


class TestBuilderStlFailureDegraded(unittest.TestCase):
    def test_step_ok_stl_fail_marks_degraded_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            part_dir = Path(tmp) / "p1"
            part_dir.mkdir()
            spec = _spec()
            # Monkeypatch export_stl to raise; export_step must still work
            # so the STEP file is produced (ok=True path).
            with mock.patch(
                "build123d.export_stl",
                side_effect=RuntimeError("simulated STL export crash"),
            ):
                result = run_part_builder(spec, part_dir)
        # STEP succeeded -> ok=True (assembly can still proceed).
        self.assertTrue(result.ok, "STEP ok means part is still usable")
        # But the part is degraded -- mesh-based QA will skip.
        self.assertTrue(
            result.degraded,
            "STL export failure must set degraded=True (was silently ok=True)",
        )
        self.assertTrue(
            any("STL" in w or "stl" in w.lower() for w in result.warnings),
            f"warnings must mention STL failure, got: {result.warnings}",
        )
        # STEP path is recorded; STL path is empty (no file produced).
        self.assertTrue(result.step_path)
        self.assertEqual(result.stl_path, "")


if __name__ == "__main__":
    unittest.main()
