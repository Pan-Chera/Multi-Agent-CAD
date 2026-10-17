"""Preserve thinking-model timeouts, env overrides, and config-reset behavior.

Both defaults were deliberately raised to 1800s in the v2 release candidate.
Planning and code generation still have independent settings and call sites.
"""
from __future__ import annotations

import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TIMEOUT_ENV = ("MAC_LLM_API_TIMEOUT", "MAC_LLM_CODEGEN_API_TIMEOUT")


def _timeout_values(path, overrides=None):
    """Load a config file without importing the pipeline or user job config."""
    with patch.dict(os.environ):
        for key in _TIMEOUT_ENV:
            os.environ.pop(key, None)
        os.environ.update(overrides or {})
        config = runpy.run_path(str(path))
    return config["LLM_API_TIMEOUT"], config["LLM_CODEGEN_API_TIMEOUT"]


class TestTimeoutValues(unittest.TestCase):
    def test_config_values(self):
        self.assertEqual(
            _timeout_values(_REPO_ROOT / "multi_agent_cad" / "config.py"),
            (1800, 1800),
        )

    def test_defaults_template_in_sync(self):
        """Exercise the reset utility on a temporary file, preserving config.py."""
        from multi_agent_cad import _config_defaults as defaults

        with tempfile.TemporaryDirectory() as directory:
            restored = Path(directory) / "config.py"
            with patch.object(defaults, "CONFIG_PATH", restored):
                self.assertEqual(defaults.reset_config(), restored)
            self.assertEqual(_timeout_values(restored), (1800, 1800))

    def test_paths_bind_configured_timeouts(self):
        """Distinct overrides must reach single-part and assembly bindings."""
        code = (
            "from multi_agent_cad import config, nodes\n"
            "from mac_assembly import nodes_assembly\n"
            "assert config.LLM_API_TIMEOUT == 345\n"
            "assert config.LLM_CODEGEN_API_TIMEOUT == 1234\n"
            "assert nodes._CFG_LLM_API_TIMEOUT == 345\n"
            "assert nodes._CFG_LLM_CODEGEN_API_TIMEOUT == 1234\n"
            "assert nodes_assembly._LLM_API_TIMEOUT == 345\n"
        )
        env = dict(os.environ)
        env.pop("MAC_CONFIG_FILE", None)
        env.update(MAC_LLM_API_TIMEOUT="345", MAC_LLM_CODEGEN_API_TIMEOUT="1234")
        res = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=str(_REPO_ROOT),
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(res.returncode, 0, msg=f"stderr: {res.stderr}\nstdout: {res.stdout}")

    def test_codegen_call_sites_use_codegen_timeout(self):
        """Source-level guard: no call site may pass the planning timeout
        into a codegen/repair path, and the Aider litellm extra_params must
        carry the codegen timeout (regression against silent 120s revert).
        """
        src = (_REPO_ROOT / "multi_agent_cad" / "nodes.py").read_text(
            encoding="utf-8"
        )
        # Planning and codegen must use their own settings even when the
        # current defaults happen to be equal.
        self.assertEqual(
            src.count("timeout=_CFG_LLM_API_TIMEOUT,"), 1,
            "planning timeout must be confined to _call_llm_json_with_retry",
        )
        self.assertGreaterEqual(
            src.count("timeout=_CFG_LLM_CODEGEN_API_TIMEOUT"), 3,
            "coder + generate + repair fallback must use the codegen timeout",
        )
        self.assertGreaterEqual(
            src.count('"timeout": _CFG_LLM_CODEGEN_API_TIMEOUT'), 3,
            "all three Aider Model.extra_params must set a litellm timeout",
        )

    def test_env_override_still_works(self):
        cases = (
            ({"MAC_LLM_API_TIMEOUT": "345"}, (345, 1800)),
            ({"MAC_LLM_CODEGEN_API_TIMEOUT": "1234"}, (1800, 1234)),
            ({"MAC_LLM_API_TIMEOUT": "345", "MAC_LLM_CODEGEN_API_TIMEOUT": "1234"},
             (345, 1234)),
            ({key: "" for key in _TIMEOUT_ENV}, (1800, 1800)),
        )
        path = _REPO_ROOT / "multi_agent_cad" / "config.py"
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(_timeout_values(path, overrides), expected)

    def test_reset_preserves_env_overrides(self):
        """Resetting config must retain the supported environment interface."""
        from multi_agent_cad import _config_defaults as defaults

        with tempfile.TemporaryDirectory() as directory:
            restored = Path(directory) / "config.py"
            with patch.object(defaults, "CONFIG_PATH", restored):
                defaults.reset_config()
            for overrides in (
                {"MAC_LLM_API_TIMEOUT": "345"},
                {"MAC_LLM_CODEGEN_API_TIMEOUT": "1234"},
                {"MAC_LLM_API_TIMEOUT": "345", "MAC_LLM_CODEGEN_API_TIMEOUT": "1234"},
                {key: "" for key in _TIMEOUT_ENV},
            ):
                with self.subTest(overrides=overrides):
                    self.assertEqual(
                        _timeout_values(restored, overrides),
                        _timeout_values(_REPO_ROOT / "multi_agent_cad" / "config.py", overrides),
                    )


if __name__ == "__main__":
    unittest.main()
