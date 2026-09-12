"""BUG-001: main() must return non-zero on runtime exceptions so CI/CD and
orchestrators can detect failure. Previously the except-Exception branch
printed the error then fell through to `return 0`, making crash and success
indistinguishable from the exit code.

No external LLM calls; the LangGraph app, preflight, and handoff are
monkeypatched. The work_dir is isolated to a TemporaryDirectory so the
test never creates real assembly_jobs/job_* directories.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _patched_env(work_dir: Path):
    """Yield a (stdout_buf, stderr_buf, patches) stack for calling main().
    The work_dir is set via MAC_ASSEMBLY_WORK_DIR env so get_initial_state
    picks it up -- no real assembly_jobs/job_* directory is created."""
    buf_out, buf_err = io.StringIO(), io.StringIO()
    patches = [
        mock.patch(
            "mac_assembly.geometry_utils.mesh_containment_available",
            return_value=True,
        ),
        mock.patch.dict(
            "os.environ",
            {
                "DASHSCOPE_API_KEY": "sk-test",
                "MAC_ASSEMBLY_WORK_DIR": str(work_dir),
            },
        ),
        contextlib.redirect_stdout(buf_out),
        contextlib.redirect_stderr(buf_err),
    ]
    return buf_out, buf_err, patches


class _FakeStream:
    """Minimal stand-in for a LangGraph CompiledGraph: yields events."""

    def __init__(self, behavior: str = "normal"):
        self.behavior = behavior

    def stream(self, initial, config):  # noqa: ARG002
        if self.behavior == "normal":
            yield {"node_a": {"work_dir": initial["work_dir"]}}
        elif self.behavior == "raise_exc":
            raise RuntimeError("simulated pipeline crash")
        elif self.behavior == "raise_kbi":
            raise KeyboardInterrupt()
        else:
            yield {}


class TestMainExitCodes(unittest.TestCase):
    def _run_main(self, fake_app):
        from mac_assembly import graph_assembly as graph

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "job_test"
            work_dir.mkdir()
            buf_out, buf_err, patches = _patched_env(work_dir)
            # Mock _final_report and handoff so the normal-completion
            # path doesn't read/write real artifacts in the temp dir
            # (keeps the test fast and side-effect-free).
            with mock.patch.object(graph, "build_assembly_graph",
                                   return_value=fake_app), \
                 mock.patch.object(graph, "_final_report"), \
                 mock.patch("mac_assembly.handoff.build_handoff",
                            return_value={}), \
                 mock.patch("mac_assembly.handoff.print_handoff"):
                with contextlib.ExitStack() as stack:
                    for p in patches:
                        stack.enter_context(p)
                    rc = graph.main()
            # Verify no real assembly_jobs/job_* was created outside tmp.
            jobs_root = Path.cwd() / "assembly_jobs"
            leaked = [
                p for p in jobs_root.glob("job_test_*")
                if p.exists()
            ] if jobs_root.is_dir() else []
            self.assertEqual(
                leaked, [],
                f"test leaked assembly_jobs/job_test_* directories: {leaked}"
            )
        return rc, buf_out, buf_err

    def test_normal_completion_returns_zero(self):
        rc, _, _ = self._run_main(_FakeStream("normal"))
        self.assertEqual(rc, 0)

    def test_runtime_exception_returns_one(self):
        rc, out, _ = self._run_main(_FakeStream("raise_exc"))
        self.assertEqual(rc, 1)
        self.assertIn("Pipeline error", out.getvalue())

    def test_keyboard_interrupt_returns_130(self):
        rc, _, _ = self._run_main(_FakeStream("raise_kbi"))
        self.assertEqual(rc, 130)

    def test_exception_path_does_not_claim_success(self):
        rc, out, _ = self._run_main(_FakeStream("raise_exc"))
        self.assertNotEqual(rc, 0)
        text = out.getvalue()
        if "Handoff packet" in text and "GLB" in text:
            self.fail(
                "Crash path produced a successful handoff report; main() must "
                "not present a runtime exception as a clean delivery."
            )


if __name__ == "__main__":
    unittest.main()
