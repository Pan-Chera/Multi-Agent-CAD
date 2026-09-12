"""BUG-046 (revised): URDF failure warning used to point at
handoff_error.log even when no log existed (export_urdf swallows its own
exceptions and may not write a log). The fix: only mention the log file
when it actually exists; otherwise say "no detailed exporter error was
recorded". print_handoff should also surface the warnings on stdout.

No external LLM calls; temp work_dir, no real STEP/URDF needed.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mac_assembly.handoff import (
    _collect_handoff_warnings,
    build_handoff,
    print_handoff,
)


class TestHandoffWarningAccuracy(unittest.TestCase):
    def test_urdf_failure_without_log_file_does_not_mention_log(self):
        """When URDF fails and handoff_error.log does NOT exist, the
        warning must NOT point at a non-existent log file."""
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "parts").mkdir()
            (work_dir / "assembly_output.stl").write_text("stub")
            warnings = _collect_handoff_warnings(work_dir, None, None)
        self.assertTrue(warnings)
        urdf_warning = next(
            (w for w in warnings if "URDF" in w), None
        )
        self.assertIsNotNone(urdf_warning)
        self.assertNotIn(
            "see handoff_error.log", urdf_warning,
            f"warning must not point at non-existent log file: {urdf_warning}"
        )
        self.assertIn(
            "no detailed", urdf_warning.lower(),
            f"warning should say 'no detailed error recorded': "
            f"{urdf_warning}"
        )

    def test_urdf_failure_with_log_file_mentions_log(self):
        """When handoff_error.log exists, the warning can point at it."""
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "parts").mkdir()
            (work_dir / "assembly_output.stl").write_text("stub")
            (work_dir / "handoff_error.log").write_text("traceback")
            warnings = _collect_handoff_warnings(work_dir, None, None)
        urdf_warning = next(
            (w for w in warnings if "URDF" in w), None
        )
        self.assertIsNotNone(urdf_warning)
        self.assertIn("handoff_error.log", urdf_warning)

    def test_print_handoff_prints_warnings(self):
        """print_handoff must surface handoff_warnings on stdout so the
        operator doesn't need to open the manifest to see them."""
        manifest = {
            "assembly_step": "assembly_output.step",
            "handoff_warnings": [
                "URDF export skipped or failed; no detailed exporter error",
            ],
        }
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(buf):
                print_handoff(manifest, Path(tmp))
        output = buf.getvalue()
        self.assertIn(
            "URDF export skipped or failed",
            output,
            f"print_handoff must print the warning, got: {output}",
        )


if __name__ == "__main__":
    unittest.main()
