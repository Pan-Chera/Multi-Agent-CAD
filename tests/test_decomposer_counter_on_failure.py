"""BUG-007: node_decomposer's failure path did not increment
decomposer_llm_calls, so the DECOMPOSER_MAX_RUNS=2 budget was effectively
unbounded for failed calls. Failed LLM calls still consume budget.

No external LLM calls; the LLM is monkeypatched to raise.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mac_assembly.nodes_assembly import node_decomposer


def _state(work_dir: Path):
    return {
        "user_request": "test request",
        "work_dir": str(work_dir),
        "assembly_brief": None,
        "decomposer_llm_calls": 0,
        "node_history": [],
        "execution_log": [],
    }


class TestDecomposerCounterOnFailure(unittest.TestCase):
    def test_failed_call_increments_counter(self):
        """A failed LLM call must still +1 decomposer_llm_calls so the
        budget binds failed runs, not just successful ones."""
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "assembly_cache").mkdir(exist_ok=True)
            # Ensure cache file does NOT exist (force LLM call path).
            with mock.patch(
                "mac_assembly.nodes_assembly.call_llm_json",
                side_effect=RuntimeError("simulated LLM crash"),
            ):
                out = node_decomposer(_state(work_dir))
        self.assertEqual(
            out.get("decomposer_llm_calls"),
            1,
            f"failed decomposer call must increment counter, got: {out}",
        )


if __name__ == "__main__":
    unittest.main()
