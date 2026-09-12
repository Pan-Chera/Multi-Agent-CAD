"""P1-2: LLM response timeouts must stay at the agreed values.

- JSON planning calls (Spec Planner / Architect / Decomposer / Mating /
  Judges): 300s (LLM_API_TIMEOUT)
- Large code generation / Aider paths: 900s (LLM_CODEGEN_API_TIMEOUT)

The 2026-09-10 regression: LLM_API_TIMEOUT had silently dropped to 120s and
Aider code-gen calls were truncated mid-file, timing out every round.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestTimeoutValues(unittest.TestCase):
    def test_config_values(self):
        from multi_agent_cad import config

        self.assertEqual(config.LLM_API_TIMEOUT, 300)
        self.assertEqual(config.LLM_CODEGEN_API_TIMEOUT, 900)

    def test_defaults_template_in_sync(self):
        """`mac-config-reset` must not restore the old 120s default (the
        values live in the CONFIG_TEMPLATE string, not module attrs)."""
        from multi_agent_cad import _config_defaults as defaults

        template = defaults.CONFIG_TEMPLATE
        self.assertIn("LLM_API_TIMEOUT = 300", template)
        self.assertIn("LLM_CODEGEN_API_TIMEOUT = 900", template)
        self.assertNotIn('or "120"', template)

    def test_codegen_paths_bind_900(self):
        """The nodes module's codegen-facing bindings must be the 900s
        value (the coder / direct-API generation / repair call sites)."""
        from multi_agent_cad import nodes

        self.assertEqual(nodes._CFG_LLM_CODEGEN_API_TIMEOUT, 900)
        self.assertEqual(nodes._CFG_LLM_API_TIMEOUT, 300)

    def test_codegen_call_sites_use_codegen_timeout(self):
        """Source-level guard: no call site may pass the planning timeout
        into a codegen/repair path, and the Aider litellm extra_params must
        carry the codegen timeout (regression against silent 120s revert).
        """
        src = (_REPO_ROOT / "multi_agent_cad" / "nodes.py").read_text(
            encoding="utf-8"
        )
        # the JSON-planning retry helper is the ONLY remaining user of the
        # 300s planning timeout inside a chat.completions.create call
        self.assertEqual(
            src.count("timeout=_CFG_LLM_API_TIMEOUT,"), 1,
            "planning timeout must be confined to _call_llm_json_with_retry",
        )
        self.assertGreaterEqual(
            src.count("timeout=_CFG_LLM_CODEGEN_API_TIMEOUT"), 3,
            "coder + generate + repair fallback must use the 900s timeout",
        )
        self.assertGreaterEqual(
            src.count('"timeout": _CFG_LLM_CODEGEN_API_TIMEOUT'), 3,
            "all three Aider Model.extra_params must set a litellm timeout",
        )

    def test_env_override_still_works(self):
        """MAC_LLM_CODEGEN_API_TIMEOUT env override is honoured (checked
        in a subprocess so the module is freshly imported with the env)."""
        code = (
            "import os\n"
            "assert os.environ['MAC_LLM_CODEGEN_API_TIMEOUT'] == '1234'\n"
            "from multi_agent_cad import config\n"
            "assert config.LLM_CODEGEN_API_TIMEOUT == 1234\n"
            "assert config.LLM_API_TIMEOUT == 300\n"
        )
        env = dict(os.environ)
        env["MAC_LLM_CODEGEN_API_TIMEOUT"] = "1234"
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            res.returncode, 0, msg=f"stderr: {res.stderr}\nstdout: {res.stdout}"
        )


if __name__ == "__main__":
    unittest.main()
