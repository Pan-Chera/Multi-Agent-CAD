import unittest
import tempfile
from pathlib import Path

from multi_agent_cad.nodes import (
    _format_missed_cuts_errors,
    _parse_missed_cuts,
    _runtime_diagnostics_path,
)


class TestRuntimeDiagnosticSeverity(unittest.TestCase):
    def test_later_iteration_never_falls_back_to_stale_iteration_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "temp_missed_0.json").write_text(
                '["CUT_ERROR: stale"]', encoding="utf-8"
            )
            self.assertIsNone(_runtime_diagnostics_path(root, 2))
            current = root / "temp_missed_2.json"
            current.write_text('["CUT_ERROR: current"]', encoding="utf-8")
            self.assertEqual(_runtime_diagnostics_path(root, 2), current)

    def test_cosmetic_edge_treatment_degradations_do_not_block(self):
        entries = [
            "CHAMFER_FAILED: trim — no edges matched filter",
            "FILLET_PARTIAL: trim — 1/2 groups OK",
            "FILLET_DEGRADED: trim — radius 2.0 -> 1.0",
        ]
        categories = _parse_missed_cuts(entries)
        errors, _ = _format_missed_cuts_errors(categories)
        self.assertEqual(errors, [])
        self.assertEqual(len(categories["cosmetic_warning"]), 3)

    def test_real_edge_treatment_failure_still_blocks(self):
        categories = _parse_missed_cuts([
            "CHAMFER_FAILED: trim — all lengths failed",
        ])
        errors, label = _format_missed_cuts_errors(categories)
        self.assertTrue(errors)
        self.assertEqual(label, "CHAMFER_FAILED")

    def test_missed_cut_and_execution_error_still_block(self):
        categories = _parse_missed_cuts([
            "MISSED_CUT: bore — tool had NO effect on body",
            "CUT_ERROR: slot — exception during cut",
        ])
        errors, label = _format_missed_cuts_errors(categories)
        self.assertTrue(errors)
        self.assertEqual(label, "CUT_POSITION_ERROR")


if __name__ == "__main__":
    unittest.main()
