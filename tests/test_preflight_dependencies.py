"""Missing rtree is FATAL, not a warning: without trimesh ray-containment
the interference / kinematic collision pairs are silently SKIPPED (false
PASSes), so the pipeline refuses to start and deliver a PASS it cannot
verify. run_assembly_qa's generation_warnings stamp stays as
defense-in-depth for DIRECT calls (re-QA scripts).

No external LLM calls; the containment probe is monkeypatched.
"""
from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock


class TestPreflightDependencies(unittest.TestCase):
    def test_containment_probe_failure_is_fatal(self):
        from mac_assembly import graph_assembly as graph

        with mock.patch(
            "mac_assembly.geometry_utils.mesh_containment_available",
            return_value=False,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                graph._preflight_dependencies()
        self.assertIn("rtree", str(ctx.exception))
        self.assertIn("pip install rtree", str(ctx.exception))

    def test_preflight_passes_when_containment_available(self):
        from mac_assembly import graph_assembly as graph

        with mock.patch(
            "mac_assembly.geometry_utils.mesh_containment_available",
            return_value=True,
        ):
            self.assertIsNone(graph._preflight_dependencies())

    def test_main_terminates_when_preflight_fails(self):
        # Exit code 2 BEFORE any state is built or any node runs: a
        # broken environment must not produce a half-finished job whose
        # QA report looks green. (An API key is supplied so the run gets
        # past the key preflight and reaches the dependency check.)
        from mac_assembly import graph_assembly as graph

        buf = io.StringIO()
        with mock.patch(
            "mac_assembly.geometry_utils.mesh_containment_available",
            return_value=False,
        ), mock.patch.dict(
            "os.environ", {"DASHSCOPE_API_KEY": "sk-test-preflight"}
        ), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = graph.main()
        self.assertEqual(rc, 2)
        self.assertIn("FATAL", buf.getvalue())
        self.assertIn("pip install rtree", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
