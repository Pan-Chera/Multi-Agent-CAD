"""P0-2 follow-up: _PART_STEP_OVERRIDES resolution in the generated script.

An override recorded for a part is AUTHORITATIVE: when its file is missing
the script must hard-fail (FileNotFoundError) instead of falling back to
the mtime glob, which could resurrect a stale STEP and masquerade as this
round's geometry. The glob fallback remains only in legacy compatibility
mode (no override recorded for the part at all).

Deterministic: real build123d STEP export + real script execution in a
subprocess. No external LLM calls.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from build123d import Box, export_step

from mac_assembly.assembly_codegen import run_assembly_script, write_assembly_script
from mac_assembly.schemas_assembly import AssemblyBrief, PartSpec

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _brief():
    return AssemblyBrief(
        assembly_name="ovr",
        parts=[PartSpec(part_id="block", part_name="block", description="d")],
        user_request_raw="r",
        expected_part_count=1,
    )


class TestResolvePartStepOverrides(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.part_dir = self.work / "parts" / "block"
        self.part_dir.mkdir(parents=True)

    def _run(self, overrides):
        script = write_assembly_script(
            _brief(), [], self.work, _REPO_ROOT, 0,
            part_step_overrides=overrides,
        )
        return run_assembly_script(
            script, self.work, timeout=300, python_bin=sys.executable
        )

    def test_missing_override_hard_fails_despite_valid_glob(self):
        # A perfectly valid (newer) STEP sits in the part dir -- the old
        # behaviour would silently glob-fall-back to it and assemble a
        # geometry that is NOT this round's recorded result.
        export_step(Box(4, 4, 4), str(self.part_dir / "temp_output_1.step"))
        ok, tail = self._run({"block": "parts/block/missing.step"})
        self.assertFalse(ok)
        self.assertIn("FileNotFoundError", tail)
        self.assertIn("authoritative STEP", tail)
        self.assertFalse((self.work / "assembly_output.step").exists())

    def test_valid_override_wins_over_newer_decoy(self):
        # Decoy: newer mtime + higher iteration number, but garbage bytes.
        # If the glob were consulted at all, the script would die on it.
        export_step(Box(4, 4, 4), str(self.part_dir / "temp_output_features.step"))
        decoy = self.part_dir / "temp_output_9.step"
        decoy.write_bytes(b"not a step file")
        future = time.time() + 3600
        os.utime(decoy, (future, future))
        ok, tail = self._run(
            {"block": "parts/block/temp_output_features.step"}
        )
        self.assertTrue(ok, f"override script failed: {tail}")
        self.assertTrue((self.work / "assembly_output.step").is_file())

    def test_legacy_glob_still_works_without_overrides(self):
        # Compatibility mode: no override recorded -> newest temp_output_*
        # by mtime, exactly like before the override mechanism existed.
        export_step(Box(4, 4, 4), str(self.part_dir / "temp_output_1.step"))
        ok, tail = self._run(None)
        self.assertTrue(ok, f"legacy glob script failed: {tail}")
        self.assertTrue((self.work / "assembly_output.step").is_file())


if __name__ == "__main__":
    unittest.main()
