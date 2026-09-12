"""Fix: MAC's three aider call sites finished coder.run() without joining
aider's background chat-summarizer thread (spawned whenever the conversation
outgrows the model's chat-history budget).

At interpreter shutdown Python runs the concurrent.futures atexit handler
(shutting litellm's global executor) BEFORE joining non-daemon threads, so an
in-flight summary call died with "cannot schedule new futures after
shutdown": a full-conversation API call was billed but discarded, and the
three noise lines it printed landed at the tail of subprocess stdout -- the
exact spot _tail_error reads for the REAL failure reason (on the 2026-09-08
gantry metrology run they masked the actual "AUTONOMOUS LOOP -- MAX RETRIES
EXHAUSTED" fatal).

The fix: _join_aider_summarizer(coder) right after every coder.run(). No
external LLM calls here: aider's Coder/Model/InputOutput are mocked.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from multi_agent_cad import nodes as mac_nodes

try:
    import aider  # noqa: F401
    _HAS_AIDER = True
except ImportError:
    _HAS_AIDER = False


ORIGINAL_CODE = (
    "import build123d as bd\n"
    "\n"
    "def gen_step():\n"
    "    body = bd.Box(60, 40, 12)\n"
    "    body = bd.Pos(0, 0, 6) * body\n"
    "    return {'shape': body}\n"
)


CODE_WITH_PLACEHOLDER = (
    "import build123d as bd\n"
    "\n"
    "def gen_step():\n"
    "    body = bd.Box(60, 40, 12)\n"
    "    # TODO_AIDER: add fillet on top edges\n"
    "    # Feature data: {\"fillet_radius\": 3.0}\n"
    "    return {'shape': body}\n"
)


class TestJoinAiderSummarizerHelper(unittest.TestCase):
    """The helper itself: call summarize_end, swallow any bookkeeping error."""

    def test_calls_summarize_end(self):
        calls = []

        class _Coder:
            def summarize_end(self):
                calls.append("joined")

        mac_nodes._join_aider_summarizer(_Coder())
        self.assertEqual(calls, ["joined"])

    def test_swallows_exceptions(self):
        class _Coder:
            def summarize_end(self):
                raise RuntimeError("boom")

        # Must not raise: summary bookkeeping must never fail the pipeline.
        mac_nodes._join_aider_summarizer(_Coder())

    def test_tolerates_missing_attr(self):
        # A coder-like object without summarize_end (e.g. an older aider
        # version) must not crash the pipeline.
        mac_nodes._join_aider_summarizer(object())


@unittest.skipUnless(_HAS_AIDER, "aider not installed in this environment")
class TestAiderSitesJoinSummarizer(unittest.TestCase):
    """Each of the three coder.run() sites joins the summarizer afterwards."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.script = Path(self._tmp.name) / "temp_design_0.py"
        self.script.write_text(ORIGINAL_CODE, encoding="utf-8")
        env = mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key-123"})
        env.start()
        self.addCleanup(env.stop)

    def _patch_aider(self):
        coder = mock.MagicMock(name="coder")

        def _fake_run(prompt):
            self.script.write_text(
                self.script.read_text(encoding="utf-8") + "\n# aider edit\n",
                encoding="utf-8",
            )

        coder.run.side_effect = _fake_run

        coder_cls = mock.patch("aider.coders.Coder")
        model_cls = mock.patch("aider.models.Model")
        io_cls = mock.patch("aider.io.InputOutput")
        c, m, i = coder_cls.start(), model_cls.start(), io_cls.start()
        for p in (coder_cls, model_cls, io_cls):
            self.addCleanup(p.stop)
        c.create.return_value = coder
        coder.create_mock = c
        return coder

    def test_fill_unsupported_with_aider_joins(self):
        coder = self._patch_aider()
        ok = mac_nodes._fill_unsupported_with_aider(
            self.script, CODE_WITH_PLACEHOLDER, "a test part")
        self.assertTrue(ok)
        coder.run.assert_called_once()
        coder.summarize_end.assert_called_once()
        self.assertTrue(coder.create_mock.create.call_args.kwargs["add_gitignore_files"])

    def test_generate_initial_solution_joins(self):
        coder = self._patch_aider()
        ok = mac_nodes.generate_initial_solution(
            script_path=str(self.script), user_request="a test part")
        self.assertTrue(ok)
        coder.run.assert_called_once()
        coder.summarize_end.assert_called_once()
        self.assertTrue(coder.create_mock.create.call_args.kwargs["add_gitignore_files"])

    def test_run_repair_on_script_joins(self):
        coder = self._patch_aider()
        ok = mac_nodes._run_repair_on_script(
            script_path=str(self.script),
            error_details=["MISSED_CUT: step-06-bore — tool had NO effect"],
            user_request="a test part",
        )
        self.assertTrue(ok)
        coder.run.assert_called_once()
        coder.summarize_end.assert_called_once()
        self.assertTrue(coder.create_mock.create.call_args.kwargs["add_gitignore_files"])


if __name__ == "__main__":
    unittest.main()
