"""P0-1: startup API-key resolution, stable subprocess injection, preflight.

No external LLM calls; the key values used here are synthetic markers.
"""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

_ENV_KEY = "env-key-aaaaaaaa"
_CONFIG_KEY = "config-key-bbbbbbb"
_SNAPSHOT_KEY = "snapshot-key-cccccc"


def _env_without_key() -> dict:
    return {k: v for k, v in os.environ.items() if k != "DASHSCOPE_API_KEY"}


class TestApiKeyResolution(unittest.TestCase):
    def test_env_key_takes_priority(self):
        from mac_assembly import graph_assembly as graph

        with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": _ENV_KEY}):
            with mock.patch("multi_agent_cad.config.DS_API_KEY", _CONFIG_KEY):
                self.assertEqual(graph._resolve_api_key(), _ENV_KEY)

    def test_config_key_used_when_env_missing(self):
        from mac_assembly import graph_assembly as graph

        with mock.patch.dict(os.environ, _env_without_key(), clear=True):
            with mock.patch("multi_agent_cad.config.DS_API_KEY", _CONFIG_KEY):
                self.assertEqual(graph._resolve_api_key(), _CONFIG_KEY)

    def test_both_missing_resolves_empty(self):
        from mac_assembly import graph_assembly as graph

        with mock.patch.dict(os.environ, _env_without_key(), clear=True):
            with mock.patch("multi_agent_cad.config.DS_API_KEY", ""):
                self.assertEqual(graph._resolve_api_key(), "")


class TestMainPreflight(unittest.TestCase):
    def test_main_fatal_when_no_key_and_workflow_not_built(self):
        """Both key sources empty: main() exits non-zero BEFORE building the
        graph or the initial state -- no tokens can be spent."""
        from mac_assembly import graph_assembly as graph

        with mock.patch.dict(os.environ, _env_without_key(), clear=True), \
                mock.patch("multi_agent_cad.config.DS_API_KEY", ""), \
                mock.patch("mac_assembly.graph_assembly._preflight_dependencies"), \
                mock.patch(
                    "mac_assembly.graph_assembly.build_assembly_graph",
                    side_effect=AssertionError("workflow must not be built"),
                ), \
                mock.patch(
                    "mac_assembly.graph_assembly.get_initial_state",
                    side_effect=AssertionError("state must not be built"),
                ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(buf):
                rc = graph.main()
        self.assertEqual(rc, 2)
        combined = buf.getvalue()
        self.assertIn("FATAL", combined)
        self.assertIn("DASHSCOPE_API_KEY", combined)

    def test_main_proceeds_when_key_present(self):
        """Sanity counterpart: with a key, main() runs the normal path
        (graph built and immediately short-circuited by a mock)."""
        from mac_assembly import graph_assembly as graph

        fake_events = [{"decomposer": {"assembly_brief": None, "execution_log": []}}]
        with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": _ENV_KEY}), \
                mock.patch("mac_assembly.graph_assembly._preflight_dependencies"), \
                mock.patch(
                    "mac_assembly.graph_assembly.get_initial_state",
                    return_value={
                        "user_request": "req", "work_dir": "/tmp/mac_test_unused",
                        "execution_log": [], "node_history": [],
                    },
                ), \
                mock.patch(
                    "mac_assembly.graph_assembly.build_assembly_graph",
                ) as build, \
                mock.patch.object(
                    graph, "_final_report",
                ), \
                mock.patch.object(
                    graph, "_print_end_to_end_tokens",
                ), \
                mock.patch.object(
                    graph, "_token_tracker",
                ):
            build.return_value.stream.return_value = iter(fake_events)
            rc = graph.main()
        self.assertEqual(rc, 0)
        from mac_assembly import part_generator as _pg
        _pg.set_api_key_snapshot(None)  # main() installed one; reset for other tests

    def test_key_value_never_printed_on_normal_path(self):
        """A config-sourced key must never appear in any startup output."""
        from mac_assembly import graph_assembly as graph

        marker_key = "NEVER-PRINT-ME-123456"
        fake_events = [{"decomposer": {"assembly_brief": None, "execution_log": []}}]
        with mock.patch.dict(os.environ, _env_without_key(), clear=True), \
                mock.patch("multi_agent_cad.config.DS_API_KEY", marker_key), \
                mock.patch("mac_assembly.graph_assembly._preflight_dependencies"), \
                mock.patch(
                    "mac_assembly.graph_assembly.get_initial_state",
                    return_value={
                        "user_request": "req", "work_dir": "/tmp/mac_test_unused",
                        "execution_log": [], "node_history": [],
                    },
                ), \
                mock.patch("mac_assembly.graph_assembly.build_assembly_graph") as build, \
                mock.patch.object(graph, "_final_report"), \
                mock.patch.object(graph, "_print_end_to_end_tokens"), \
                mock.patch.object(graph, "_token_tracker"):
            build.return_value.stream.return_value = iter(fake_events)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(buf):
                rc = graph.main()
        self.assertEqual(rc, 0)
        self.assertNotIn(marker_key, buf.getvalue())
        from mac_assembly import part_generator as _pg
        _pg.set_api_key_snapshot(None)  # main() installed one; reset for other tests


class TestSubprocessEnvInjection(unittest.TestCase):
    def test_snapshot_injected_when_env_lacks_key(self):
        from mac_assembly import part_generator as pg

        with mock.patch.dict(os.environ, _env_without_key(), clear=True):
            with mock.patch.object(pg, "_API_KEY_SNAPSHOT", _SNAPSHOT_KEY):
                env = pg._make_subprocess_env()
        self.assertEqual(env["DASHSCOPE_API_KEY"], _SNAPSHOT_KEY)

    def test_env_key_wins_over_snapshot(self):
        from mac_assembly import part_generator as pg

        with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": _ENV_KEY}):
            with mock.patch.object(pg, "_API_KEY_SNAPSHOT", _SNAPSHOT_KEY):
                env = pg._make_subprocess_env()
        self.assertEqual(env["DASHSCOPE_API_KEY"], _ENV_KEY)

    def test_no_key_forwarded_when_neither_source_has_one(self):
        from mac_assembly import part_generator as pg

        with mock.patch.dict(os.environ, _env_without_key(), clear=True):
            with mock.patch.object(pg, "_API_KEY_SNAPSHOT", None):
                env = pg._make_subprocess_env()
        self.assertNotIn("DASHSCOPE_API_KEY", env)

    def test_snapshot_survives_midrun_config_blank(self):
        """The subprocess env is built from the STARTUP snapshot, so a
        config.py blanked mid-run cannot strip the key from later parts."""
        from mac_assembly import graph_assembly as graph
        from mac_assembly import part_generator as pg

        with mock.patch.dict(os.environ, _env_without_key(), clear=True), \
                mock.patch("multi_agent_cad.config.DS_API_KEY", _CONFIG_KEY):
            graph.set_api_key_snapshot(graph._resolve_api_key())
            try:
                # config.py gets blanked mid-run
                with mock.patch("multi_agent_cad.config.DS_API_KEY", ""):
                    env = pg._make_subprocess_env()
                    self.assertEqual(env["DASHSCOPE_API_KEY"], _CONFIG_KEY)
            finally:
                pg.set_api_key_snapshot(None)


if __name__ == "__main__":
    unittest.main()
