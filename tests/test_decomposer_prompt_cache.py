"""Decomposer cache must include the workflow prompt contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mac_assembly.nodes_assembly import node_decomposer


def _state(work_dir: Path):
    return {
        "user_request": "make a simple assembly",
        "work_dir": str(work_dir),
        "assembly_brief": None,
        "part_results": {},
        "decomposer_llm_calls": 0,
        "repair_context": "",
        "node_history": [],
        "execution_log": [],
    }


def _brief():
    return {
        "assembly_name": "cache_test",
        "parts": [{
            "part_id": "base", "part_name": "Base",
            "description": "A plain base",
        }],
        "interfaces": [],
        "user_request_raw": "make a simple assembly",
    }


class TestDecomposerPromptCache(unittest.TestCase):
    def test_prompt_change_invalidates_cached_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with mock.patch(
                "mac_assembly.nodes_assembly._prompt", return_value="prompt-v1"
            ), mock.patch(
                "mac_assembly.nodes_assembly.call_llm_json", return_value=_brief()
            ) as llm:
                node_decomposer(_state(work_dir))
                self.assertEqual(llm.call_count, 1)

            # Same user request and images, but changed workflow contract.
            with mock.patch(
                "mac_assembly.nodes_assembly._prompt", return_value="prompt-v2"
            ), mock.patch(
                "mac_assembly.nodes_assembly.call_llm_json", return_value=_brief()
            ) as llm:
                node_decomposer(_state(work_dir))
                self.assertEqual(llm.call_count, 1)


if __name__ == "__main__":
    unittest.main()
