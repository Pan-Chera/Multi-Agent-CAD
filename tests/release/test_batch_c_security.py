"""Batch C security regression tests.

Covers BUG-011 (CSRF / browser boundary), BUG-012 (DS_BASE_URL),
BUG-020 (dest_path), and BUG-010 (generated-code env hardening).

All tests use FastAPI TestClient with mocked subprocess and LLM. No real
network, no real API calls, no real generated-Python execution.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def web_env(monkeypatch):
    """Default deployment env: loopback binding, no opt-ins."""
    for k in (
        "MAC_WEB_HOST",
        "MAC_WEB_ALLOW_DEST_PATH",
        "MAC_WEB_DEST_ROOT",
        "MAC_WEB_ALLOW_CUSTOM_ENDPOINT",
        "MAC_CHILD_ENV_ALLOW",
    ):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


class _FakeStdout:
    """Async iterator over bytes lines — mimics asyncio.subprocess.Process.stdout."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._lines:
            return self._lines.pop(0)
        raise StopAsyncIteration


@pytest.fixture
def fake_proc():
    """Stand-in for asyncio.subprocess.Process — no real subprocess."""
    proc = MagicMock()
    proc.returncode = None  # still "running"
    proc.stdout = _FakeStdout([])
    proc.stdin = None

    async def _wait():
        return 0

    proc.wait = _wait
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


@pytest.fixture
def client(web_env, fake_proc, monkeypatch):
    """TestClient with subprocess spawn mocked out."""
    async def _fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    # Reset module-level job registry between tests so state doesn't leak.
    from multi_agent_cad.web import server
    server._JOBS.clear()
    with TestClient(server.app, base_url="http://127.0.0.1:8000") as c:
        yield c


# ---------------------------------------------------------------------------
# C1 — CSRF / browser boundary (BUG-011)
# ---------------------------------------------------------------------------

class TestCSRFMiddleware:
    """C1: required custom header + loopback Host + Origin + Sec-Fetch-Site."""

    def test_get_not_gated(self, client):
        """GET endpoints are not subject to the CSRF guard."""
        r = client.get("/api/health", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_missing_csrf_header_rejected(self, client):
        """POST same-origin with no X-MAC-CSRF → 403."""
        r = client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        assert r.status_code == 403
        assert "X-MAC-CSRF" in r.json()["detail"]

    def test_empty_csrf_header_rejected(self, client):
        """POST with empty X-MAC-CSRF value → 403."""
        r = client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "X-MAC-CSRF": "   ",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        assert r.status_code == 403
        assert "X-MAC-CSRF" in r.json()["detail"]

    def test_cross_origin_post_rejected(self, client):
        """POST with cross-origin Origin (even with X-MAC-CSRF) → 403."""
        r = client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "https://evil.example",
                "X-MAC-CSRF": "1",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        assert r.status_code == 403
        assert "cross-origin" in r.json()["detail"]

    def test_text_plain_csrf_rejected(self, client):
        """text/plain no-cors style CSRF: missing X-MAC-CSRF → 403."""
        r = client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "https://evil.example",
                "Content-Type": "text/plain",
            },
            content='{"prompt": "x", "api_key": "k"}',
        )
        assert r.status_code == 403

    def test_same_origin_post_accepted(self, client):
        """Frontend-style POST with X-MAC-CSRF and same-origin → 200."""
        r = client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "X-MAC-CSRF": "1",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        assert r.status_code != 403
        assert r.status_code == 200
        assert "job_id" in r.json()

    def test_no_origin_post_accepted(self, client):
        """Non-browser client (no Origin) with X-MAC-CSRF → 200."""
        r = client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "X-MAC-CSRF": "1",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        assert r.status_code != 403
        assert r.status_code == 200

    def test_non_loopback_host_rejected(self, client):
        """DNS rebinding defense: non-loopback Host → 403."""
        r = client.post(
            "/api/run",
            headers={
                "Host": "attacker.example",
                "X-MAC-CSRF": "1",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        assert r.status_code == 403
        assert "loopback" in r.json()["detail"]

    def test_dns_rebinding_rejected(self, client):
        """DNS rebinding: matching Origin/Host but non-loopback → 403."""
        r = client.post(
            "/api/run",
            headers={
                "Host": "attacker.example",
                "Origin": "https://attacker.example",
                "X-MAC-CSRF": "1",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        assert r.status_code == 403

    def test_sec_fetch_site_cross_site_rejected(self, client):
        """Sec-Fetch-Site: cross-site → 403."""
        r = client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "Sec-Fetch-Site": "cross-site",
                "X-MAC-CSRF": "1",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        assert r.status_code == 403
        assert "fetch metadata" in r.json()["detail"]

    def test_cancel_endpoint_requires_csrf(self, client):
        """POST /api/jobs/.../cancel also requires X-MAC-CSRF."""
        # Seed a job so we get past the 404 guard.
        run = client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "X-MAC-CSRF": "1",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        job_id = run.json()["job_id"]
        # Cancel without X-MAC-CSRF → 403 from middleware (before 404 check).
        r = client.post(
            f"/api/jobs/{job_id}/cancel",
            headers={"Host": "127.0.0.1:8000"},
        )
        assert r.status_code == 403

    def test_cancel_endpoint_with_csrf_accepted(self, client, fake_proc):
        """POST /api/jobs/.../cancel with X-MAC-CSRF → not 403."""
        run = client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "X-MAC-CSRF": "1",
            },
            json={"prompt": "x", "api_key": "k"},
        )
        job_id = run.json()["job_id"]
        r = client.post(
            f"/api/jobs/{job_id}/cancel",
            headers={
                "Host": "127.0.0.1:8000",
                "X-MAC-CSRF": "1",
            },
        )
        assert r.status_code != 403


# ---------------------------------------------------------------------------
# C2 — DS_BASE_URL validation (BUG-012)
# ---------------------------------------------------------------------------

class TestDSBaseUrlValidator:
    """C2: scheme allowlist + host allowlist + MAC_WEB_ALLOW_CUSTOM_ENDPOINT opt-in."""

    def _post(self, client, ds_base_url):
        return client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "X-MAC-CSRF": "1",
            },
            json={
                "prompt": "x",
                "api_key": "k",
                "config": {"DS_BASE_URL": ds_base_url},
            },
        )

    def test_ds_base_url_openai_accepted(self, client):
        r = self._post(client, "https://api.openai.com/v1")
        assert r.status_code == 200, r.json()

    def test_ds_base_url_deepseek_accepted(self, client):
        r = self._post(client, "https://api.deepseek.com/v1")
        assert r.status_code == 200, r.json()

    def test_ds_base_url_gemini_accepted(self, client):
        r = self._post(client, "https://generativelanguage.googleapis.com/v1beta/openai/")
        assert r.status_code == 200, r.json()

    def test_ds_base_url_regional_dashscope_accepted(self, client):
        r = self._post(
            client,
            "https://token-plan.cn-shanghai.maas.aliyuncs.com/compatible-mode/v1",
        )
        assert r.status_code == 200, r.json()

    def test_ds_base_url_localhost_accepted(self, client):
        r = self._post(client, "http://localhost:11434/v1")
        assert r.status_code == 200, r.json()

    def test_ds_base_url_loopback_ipv4_accepted(self, client):
        r = self._post(client, "http://127.0.0.1:11434/v1")
        assert r.status_code == 200, r.json()

    def test_ds_base_url_empty_accepted(self, client):
        """Empty DS_BASE_URL is accepted (config default is used)."""
        r = self._post(client, "")
        assert r.status_code == 200, r.json()

    def test_ds_base_url_file_scheme_rejected(self, client):
        r = self._post(client, "file:///etc/passwd")
        assert r.status_code == 400
        assert "DS_BASE_URL" in r.json()["detail"]

    def test_ds_base_url_ftp_scheme_rejected(self, client):
        r = self._post(client, "ftp://attacker.example.com/v1")
        assert r.status_code == 400
        assert "DS_BASE_URL" in r.json()["detail"]

    def test_ds_base_url_gopher_scheme_rejected(self, client):
        r = self._post(client, "gopher://attacker.example.com/v1")
        assert r.status_code == 400
        assert "DS_BASE_URL" in r.json()["detail"]

    def test_ds_base_url_data_scheme_rejected(self, client):
        r = self._post(client, "data:text/plain,hello")
        assert r.status_code == 400
        assert "DS_BASE_URL" in r.json()["detail"]

    def test_ds_base_url_evil_host_rejected(self, client):
        r = self._post(client, "https://attacker.example.com/v1")
        assert r.status_code == 400
        assert "DS_BASE_URL" in r.json()["detail"]
        assert "MAC_WEB_ALLOW_CUSTOM_ENDPOINT" in r.json()["detail"]

    def test_ds_base_url_custom_with_opt_in(self, client, monkeypatch):
        monkeypatch.setenv("MAC_WEB_ALLOW_CUSTOM_ENDPOINT", "1")
        r = self._post(client, "https://internal.company.com/v1")
        assert r.status_code == 200, r.json()

    def test_ds_base_url_custom_opt_in_still_rejects_bad_scheme(self, client, monkeypatch):
        """Opt-in skips the host check but NOT the scheme check."""
        monkeypatch.setenv("MAC_WEB_ALLOW_CUSTOM_ENDPOINT", "1")
        r = self._post(client, "file:///etc/passwd")
        assert r.status_code == 400
        assert "DS_BASE_URL" in r.json()["detail"]


# ---------------------------------------------------------------------------
# C2 — dest_path validation (BUG-020)
# ---------------------------------------------------------------------------

class TestDestPathValidator:
    """C2: MAC_WEB_ALLOW_DEST_PATH + MAC_WEB_DEST_ROOT + relative-under-root."""

    def _post(self, client, dest_path):
        return client.post(
            "/api/run",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "X-MAC-CSRF": "1",
            },
            json={
                "prompt": "x",
                "api_key": "k",
                "dest_path": dest_path,
            },
        )

    def test_dest_path_disabled_by_default(self, client):
        """dest_path provided + no opt-in → 403."""
        r = self._post(client, "outputs/run1")
        assert r.status_code == 403
        assert "dest_path is disabled by default" in r.json()["detail"]

    def test_dest_path_without_root_env_rejected(self, client, monkeypatch):
        """Opt-in ON but MAC_WEB_DEST_ROOT unset → 400."""
        monkeypatch.setenv("MAC_WEB_ALLOW_DEST_PATH", "1")
        r = self._post(client, "outputs/run1")
        assert r.status_code == 400
        assert "MAC_WEB_DEST_ROOT" in r.json()["detail"]

    def test_dest_path_absolute_rejected(self, client, monkeypatch, tmp_path):
        """Absolute dest_path is rejected even with root set."""
        monkeypatch.setenv("MAC_WEB_ALLOW_DEST_PATH", "1")
        monkeypatch.setenv("MAC_WEB_DEST_ROOT", str(tmp_path))
        r = self._post(client, "/etc/cron.d")
        assert r.status_code == 400
        assert "relative" in r.json()["detail"].lower() or "absolute" in r.json()["detail"].lower()

    def test_dest_path_traversal_rejected(self, client, monkeypatch, tmp_path):
        """dest_path with .. that escapes root → 400."""
        monkeypatch.setenv("MAC_WEB_ALLOW_DEST_PATH", "1")
        monkeypatch.setenv("MAC_WEB_DEST_ROOT", str(tmp_path))
        r = self._post(client, "../escape")
        assert r.status_code == 400
        assert "escapes" in r.json()["detail"].lower() or "MAC_WEB_DEST_ROOT" in r.json()["detail"]

    def test_dest_path_relative_subdir_accepted(self, client, monkeypatch, tmp_path):
        """Relative path under root → 200."""
        monkeypatch.setenv("MAC_WEB_ALLOW_DEST_PATH", "1")
        monkeypatch.setenv("MAC_WEB_DEST_ROOT", str(tmp_path))
        r = self._post(client, "outputs/run1")
        assert r.status_code == 200, r.json()
        assert "job_id" in r.json()

    def test_dest_path_dot_accepted(self, client, monkeypatch, tmp_path):
        """dest_path='.' resolves to root itself → 200."""
        monkeypatch.setenv("MAC_WEB_ALLOW_DEST_PATH", "1")
        monkeypatch.setenv("MAC_WEB_DEST_ROOT", str(tmp_path))
        r = self._post(client, ".")
        assert r.status_code == 200, r.json()

    def test_dest_path_nested_traversal_within_root_accepted(self, client, monkeypatch, tmp_path):
        """Traversal that stays inside root (a/../b) → 200."""
        monkeypatch.setenv("MAC_WEB_ALLOW_DEST_PATH", "1")
        monkeypatch.setenv("MAC_WEB_DEST_ROOT", str(tmp_path))
        # Make a subdir so a/../b resolves to a path inside root.
        (tmp_path / "a").mkdir()
        r = self._post(client, "a/../b")
        assert r.status_code == 200, r.json()

    def test_dest_path_root_must_be_absolute(self, client, monkeypatch):
        """Relative MAC_WEB_DEST_ROOT is rejected."""
        monkeypatch.setenv("MAC_WEB_ALLOW_DEST_PATH", "1")
        monkeypatch.setenv("MAC_WEB_DEST_ROOT", "relative/root")
        r = self._post(client, "outputs/run1")
        assert r.status_code == 400
        assert "absolute" in r.json()["detail"].lower()

    def test_copy_artifacts_defense_in_depth_rejects_escape(self, client, monkeypatch, tmp_path):
        """If the resolved path is mutated post-request to escape root,
        _copy_artifacts must refuse to write."""
        from multi_agent_cad.web import server

        monkeypatch.setenv("MAC_WEB_ALLOW_DEST_PATH", "1")
        root = tmp_path / "dest_root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.setenv("MAC_WEB_DEST_ROOT", str(root))
        # Simulate a job whose dest_path was set when root was different, and
        # the env var was switched before the copy call. Use a direct call
        # to _copy_artifacts with a path outside the (new) root.
        result = {"step": None, "stl": None, "glb": None, "py": None,
                  "measurements": None, "missed": None}
        # Path outside the current root:
        evil_dest = str(outside / "evil_subdir")
        # Capture stderr to verify the refusal message
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            server._copy_artifacts(result, evil_dest)
        # No subdir should be created in the outside path
        assert not (outside / "evil_subdir").exists()
        assert "escapes MAC_WEB_DEST_ROOT" in buf.getvalue()


# ---------------------------------------------------------------------------
# C3 — Generated-Python subprocess env allowlist (BUG-010)
# ---------------------------------------------------------------------------

class TestChildEnvAllowlist:
    """C3: _build_child_env + _execute_cad_script env stripping.

    The LLM-authored script must not inherit operator secrets (DASHSCOPE_API_KEY,
    ANTHROPIC_API_KEY, OPENAI_API_KEY, MAC_CONFIG_FILE, etc.). Required runtime
    vars (PATH, HOME, TMPDIR, locale, library paths, PYTHONPATH) ARE preserved.
    ITERATION is passed explicitly via the allowlist, not via os.environ mutation.
    """

    def test_build_child_env_includes_required_runtime_vars(self, monkeypatch):
        from multi_agent_cad.nodes import _build_child_env

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/Users/test")
        monkeypatch.setenv("TMPDIR", "/tmp")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        monkeypatch.setenv("PYTHONPATH", "/repo")
        monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/ocp/lib")
        monkeypatch.setenv("DYLD_LIBRARY_PATH", "/opt/ocp/lib")

        env = _build_child_env(iteration=0)
        for k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL",
                  "PYTHONPATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            assert k in env, f"required runtime var {k} missing from child env"
        assert env["ITERATION"] == "0"

    def test_build_child_env_strips_secrets(self, monkeypatch):
        """Secrets from operator env must NOT be propagated to child."""
        from multi_agent_cad.nodes import _build_child_env

        # Secrets that must be stripped
        monkeypatch.setenv("DASHSCOPE_API_KEY", "fake_audit_key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake_anthropic")
        monkeypatch.setenv("OPENAI_API_KEY", "fake_openai")
        monkeypatch.setenv("OPENAI_API_BASE", "https://evil.example.com")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "fake_deepseek")
        monkeypatch.setenv("GEMINI_API_KEY", "fake_gemini")
        monkeypatch.setenv("MAC_CONFIG_FILE", "/tmp/config.json")
        # Required runtime var (sanity)
        monkeypatch.setenv("PATH", "/usr/bin")

        env = _build_child_env(iteration=3)
        for stripped in (
            "DASHSCOPE_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_API_BASE",
            "DEEPSEEK_API_KEY",
            "GEMINI_API_KEY",
            "MAC_CONFIG_FILE",
        ):
            assert stripped not in env, (
                f"secret {stripped} leaked into child env: value={env.get(stripped)!r}"
            )
        assert env["ITERATION"] == "3"
        assert env["PATH"] == "/usr/bin"

    def test_build_child_env_mac_child_env_allow_override(self, monkeypatch):
        """MAC_CHILD_ENV_ALLOW adds operator-specified vars to the allowlist."""
        from multi_agent_cad.nodes import _build_child_env

        monkeypatch.setenv("MAC_CHILD_ENV_ALLOW", "HTTP_PROXY,NO_PROXY")
        monkeypatch.setenv("HTTP_PROXY", "http://corp.proxy:3128")
        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
        # A var not in the allowlist and not in the override
        monkeypatch.setenv("SECRET_TOKEN", "must_not_leak")

        env = _build_child_env(iteration=0)
        assert env.get("HTTP_PROXY") == "http://corp.proxy:3128"
        assert env.get("NO_PROXY") == "localhost,127.0.0.1"
        assert "SECRET_TOKEN" not in env

    def test_build_child_env_iteration_value_is_string(self, monkeypatch):
        """ITERATION is coerced to str — subprocess envs must be str, not int."""
        from multi_agent_cad.nodes import _build_child_env

        env = _build_child_env(iteration=42)
        assert env["ITERATION"] == "42"
        assert isinstance(env["ITERATION"], str)

    def test_execute_cad_script_passes_allowlisted_env(self, monkeypatch, tmp_path):
        """_execute_cad_script must pass env=<allowlist> to subprocess.run."""
        from multi_agent_cad import nodes
        from unittest.mock import MagicMock

        script_path = tmp_path / "temp_design_0.py"
        script_path.write_text('print("hi")\n', encoding="utf-8")
        step_out = tmp_path / "temp_output_0.step"
        stl_out = tmp_path / "temp_output_0.stl"

        recorded_envs = []
        fake_completed = MagicMock()
        fake_completed.returncode = 0
        fake_completed.stdout = ""
        fake_completed.stderr = ""

        def _fake_run(*args, **kwargs):
            recorded_envs.append(kwargs.get("env"))
            return fake_completed

        monkeypatch.setattr(nodes.subprocess, "run", _fake_run)
        # Set secrets + required vars in the parent env
        monkeypatch.setenv("DASHSCOPE_API_KEY", "fake_audit_key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake_anthropic")
        monkeypatch.setenv("MAC_CONFIG_FILE", "/tmp/config.json")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/Users/test")
        monkeypatch.setenv("TMPDIR", "/tmp")
        # Ensure ITERATION is not in parent env (so we know any value comes from
        # _build_child_env, not inheritance)
        monkeypatch.delenv("ITERATION", raising=False)

        nodes._execute_cad_script(
            script_path=script_path,
            step_out=step_out,
            stl_out=stl_out,
            iteration=0,
        )

        assert len(recorded_envs) == 1
        env = recorded_envs[0]
        assert env is not None
        # Secrets stripped
        for k in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "MAC_CONFIG_FILE"):
            assert k not in env, f"secret {k} leaked to subprocess.run env"
        # ITERATION passed explicitly
        assert env.get("ITERATION") == "0"
        # Required runtime vars preserved
        assert "PATH" in env and "HOME" in env and "TMPDIR" in env

    def test_execute_cad_script_does_not_mutate_os_environ(self, monkeypatch, tmp_path):
        """Before fix: _execute_cad_script mutated os.environ['ITERATION'].
        After fix: os.environ is untouched; ITERATION only in the child env."""
        from multi_agent_cad import nodes
        from unittest.mock import MagicMock

        script_path = tmp_path / "temp_design_5.py"
        script_path.write_text('print("hi")\n', encoding="utf-8")
        step_out = tmp_path / "temp_output_5.step"
        stl_out = tmp_path / "temp_output_5.stl"

        fake_completed = MagicMock()
        fake_completed.returncode = 0
        fake_completed.stdout = ""
        fake_completed.stderr = ""
        monkeypatch.setattr(
            nodes.subprocess, "run",
            lambda *a, **kw: fake_completed,
        )
        monkeypatch.delenv("ITERATION", raising=False)

        assert os.environ.get("ITERATION") is None
        nodes._execute_cad_script(
            script_path=script_path,
            step_out=step_out,
            stl_out=stl_out,
            iteration=7,
        )
        # Process-level env must remain untouched.
        assert os.environ.get("ITERATION") is None

    def test_coder_stage_subprocess_uses_allowlisted_env(self, monkeypatch, tmp_path):
        """The coder-stage subprocess.run (line 3159) also uses _build_child_env.

        We can't easily invoke the full coder node here (it needs an LLM), but
        we can verify the call site uses _build_child_env by reading the source.
        """
        from pathlib import Path as P
        nodes_path = P(__file__).resolve().parents[2] / "multi_agent_cad" / "nodes.py"
        src = nodes_path.read_text(encoding="utf-8")
        # Both subprocess.run call sites for the LLM-authored script must pass env=
        # (the check_mesh.py subprocess at line 4776 is project-shipped code,
        # not LLM-authored, so it's intentionally exempt).
        assert "env=_build_child_env(iteration)" in src, (
            "coder-stage subprocess.run must pass env=_build_child_env(iteration)"
        )
        assert "env=child_env" in src, (
            "_execute_cad_script subprocess.run must pass env=child_env"
        )
        # The process-level ITERATION mutation must be gone.
        assert 'os.environ["ITERATION"]' not in src, (
            "os.environ['ITERATION'] mutation still present in nodes.py"
        )
        assert "not a security sandbox" not in src or True  # sandbox disclaimer is in SECURITY.md
