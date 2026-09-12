"""SinglePartMAC: the existing 4-agent MAC pipeline wrapped as a module.

This is the "encapsulate MAC as a single structural part generator"
step of the assembly refactor. The wrapper adds exactly two things on
top of ``python -m multi_agent_cad.graph``:

1. **cwd isolation** -- each part runs in its own directory so the
   ``temp_*`` artifacts and ``pipeline_cache`` never collide.
2. **result harvesting** -- locates the newest STEP/STL/Python outputs
   and returns a structured ``PartResult``.

No MAC internals are modified; the pipeline (Spec Planner ->
Geometric Architect -> deterministic Coder -> Autonomous Skill Loop)
is reused verbatim.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from mac_assembly import config_assembly as cfg
from mac_assembly.file_utils import newest_file
from mac_assembly.schemas_assembly import (
    PartResult,
    PartSpec,
    base_body_fingerprint,
    part_spec_fingerprint,
)

# Project root on sys.path for the part subprocess (whose cwd is the part dir,
# not the project root, so `import mac_assembly` would fail without this).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# P0-1: startup API-key snapshot
# ---------------------------------------------------------------------------
# Resolved ONCE by the assembly main() before any node runs, and injected
# into every part subprocess env. A mid-run edit of multi_agent_cad/config.py
# (e.g. DS_API_KEY blanked by a reset) can no longer strip the key from
# later subprocesses -- each one gets the startup snapshot. The value is
# never printed, logged, or written to any artifact.

_API_KEY_SNAPSHOT: str | None = None


def set_api_key_snapshot(key: str | None) -> None:
    """Install the startup-resolved DashScope key (see graph_assembly.main)."""
    global _API_KEY_SNAPSHOT
    _API_KEY_SNAPSHOT = (str(key).strip()) if key else None


# ---------------------------------------------------------------------------
# P0-2: non-retryable (configuration / infrastructure) error classification
# ---------------------------------------------------------------------------

# Substrings matched (case-insensitively) against a PartResult error / a
# subprocess output tail. Deliberately NARROW: a false positive ends the run
# on a recoverable error, so only unambiguous authentication / endpoint /
# model-configuration / missing-dependency signatures qualify. Ordinary
# timeouts, JSON parse errors, model content errors and CAD build errors
# match none of these and stay on the normal retry paths.
_NON_RETRYABLE_ERROR_PATTERNS: tuple[str, ...] = (
    # missing / rejected credentials
    "dashscope_api_key is not set",
    "error code: 401",
    "error code: 403",
    "authenticationerror",
    "permissiondeniederror",
    "invalid api key",
    "invalid api-key",  # DashScope phrasing: "Invalid API-key provided"
    "incorrect api key",
    "unauthorized",
    # endpoint / model misconfiguration
    "model not found",
    "model not exist",  # DashScope phrasing for an unknown model id
    "invalid model",
    "invalid url",
    # missing required dependency (ModuleNotFoundError only: a generated
    # script's own bad import is a plain ImportError and stays retryable)
    "modulenotfounderror",
)


def is_non_retryable_error(error: str | None) -> bool:
    """True for configuration/infrastructure failures that retrying cannot
    fix (auth rejection, bad endpoint/model, missing dependency)."""
    if not error:
        return False
    haystack = str(error).lower()
    return any(p in haystack for p in _NON_RETRYABLE_ERROR_PATTERNS)


# ---------------------------------------------------------------------------
# P1-3: cross-attempt token accounting
# ---------------------------------------------------------------------------


def _parse_token_summary_text(raw: str) -> dict[str, int]:
    """Parse a token_summary.json body, or return {} on any error."""
    try:
        data = json.loads(raw)
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception:  # noqa: BLE001 - accounting is best-effort
        return {}


def _load_token_summary(part_dir: Path) -> dict[str, int]:
    """Read ``token_summary.json`` from a part dir, or return {} on any error.

    Best-effort accounting: any malformed/missing file just contributes zero
    tokens instead of failing the part pipeline.
    """
    token_file = part_dir / "token_summary.json"
    if not token_file.is_file():
        return {}
    try:
        return _parse_token_summary_text(token_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - accounting is best-effort
        return {}


def _load_cumulative_tokens(part_dir: Path) -> dict[str, int]:
    cumulative = part_dir / "token_summary_cumulative.json"
    if not cumulative.is_file():
        return {}
    try:
        data = json.loads(cumulative.read_text(encoding="utf-8"))
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception:  # noqa: BLE001
        return {}


def load_cumulative_token_usage(part_dir: Path) -> dict[str, int]:
    """Public read of a part's cumulative token ledger (all attempts)."""
    return _load_cumulative_tokens(part_dir)


def _next_attempt_number(part_dir: Path, prefix: str, suffix: str) -> int:
    """First free attempt index for ``<prefix>NN<suffix>`` -- never reuses a
    number, so multiple attempts never overwrite each other's files."""
    n = 1
    while (part_dir / f"{prefix}{n:02d}{suffix}").exists():
        n += 1
    return n


def _seed_cumulative_tokens(part_dir: Path) -> None:
    """Fold a PRE-EXISTING token_summary.json into the cumulative ledger.

    Runs BEFORE a new subprocess launches. On a resumed job dir the runner
    overwrites token_summary.json per subprocess, so without this seed the
    previous session's spend would be silently dropped from end-to-end
    accounting. No-op once the cumulative ledger exists (fresh attempts
    merge into it via _archive_attempt_tokens instead).
    """
    cumulative = part_dir / "token_summary_cumulative.json"
    src = part_dir / "token_summary.json"
    if cumulative.exists() or not src.is_file():
        return
    seed = _load_token_summary(part_dir)
    if seed:
        try:
            cumulative.write_text(json.dumps(seed, indent=2), encoding="utf-8")
        except OSError:
            pass


def _archive_attempt_tokens(part_dir: Path, started: float) -> dict[str, int]:
    """Snapshot this attempt's token_summary.json into the cumulative ledger.

    The runner (``_part_runner``) writes token_summary.json in a finally
    block -- one file per subprocess, overwritten each run. Archiving moves
    it to ``token_summary_attempt_NN.json`` (attempt-level snapshot) and adds
    it into ``token_summary_cumulative.json``. The mtime guard means a file
    predating this attempt (subprocess SIGKILLed at timeout before its
    finally could run) is NOT re-counted. Returns the cumulative totals.
    """
    src = part_dir / "token_summary.json"
    if not src.is_file():
        return _load_cumulative_tokens(part_dir)
    try:
        if src.stat().st_mtime < started - 1.0:
            # Stale summary from a previous attempt (this subprocess was
            # killed before writing its own) -- already counted.
            return _load_cumulative_tokens(part_dir)
        raw = src.read_text(encoding="utf-8")
    except OSError:
        return _load_cumulative_tokens(part_dir)
    attempt_usage = _parse_token_summary_text(raw)
    n = _next_attempt_number(part_dir, "token_summary_attempt_", ".json")
    try:
        (part_dir / f"token_summary_attempt_{n:02d}.json").write_text(
            raw, encoding="utf-8"
        )
        src.unlink(missing_ok=True)
    except OSError:
        pass  # snapshot best-effort; the mtime guard prevents double-count
    if not attempt_usage:
        return _load_cumulative_tokens(part_dir)
    cumulative = _load_cumulative_tokens(part_dir)
    merged = {
        k: int(cumulative.get(k, 0)) + int(attempt_usage.get(k, 0))
        for k in set(cumulative) | set(attempt_usage)
    }
    try:
        (part_dir / "token_summary_cumulative.json").write_text(
            json.dumps(merged, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    return merged


# ---------------------------------------------------------------------------
# P1-1: live-streaming subprocess runner (echo + per-attempt log)
# ---------------------------------------------------------------------------


def _redact_secrets(line: str, secrets: list[str]) -> str:
    """Replace API-key values with ``<redacted>`` before echoing/logging."""
    for s in secrets:
        if len(s) >= 8:
            line = line.replace(s, "<redacted>")
    return line


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a subprocess AND its descendants. The child runs in its own
    session/process group (start_new_session=True), so killpg reaches aider /
    CAD grandchildren that a bare proc.kill() would orphan."""
    try:
        if os.name == "posix" and hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - non-POSIX fallback
            proc.kill()
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - already gone
            pass


def _stream_subprocess(
    cmd: list[str],
    *,
    cwd: Path | str,
    env: dict,
    timeout: float,
    log_path: Path,
) -> tuple[int | None, str, bool]:
    """Run a part subprocess with line-streamed output.

    Every stdout/stderr line is (a) echoed to the parent terminal and
    (b) appended to ``log_path`` as it arrives -- the parent is no longer
    silent for the (up to 1h) duration of a part generation.

    Returns ``(returncode, combined_output, timed_out)``. On timeout the
    child's whole process group is SIGKILLed (no orphans) and returncode is
    the kill status (typically -9).
    """
    proc = subprocess.Popen(  # noqa: S603 - fixed module invocation
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    secrets = [
        v for v in (
            _API_KEY_SNAPSHOT,
            env.get("DASHSCOPE_API_KEY"), env.get("OPENAI_API_KEY"),
            env.get("ANTHROPIC_API_KEY"), env.get("DEEPSEEK_API_KEY"),
        ) if v and len(v) >= 8
    ]
    lines: list[str] = []

    def _pump() -> None:
        assert proc.stdout is not None
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            for line in proc.stdout:
                # Redact BEFORE the line enters ANY persistent artifact: the
                # returned combined_output feeds _tail_error -> PartResult.error
                # -> QA report / handoff manifest / final report, so the raw
                # value must never survive the pump. Error classification only
                # matches error phrases, never key values.
                line = _redact_secrets(line, secrets) if secrets else line
                lines.append(line)
                try:
                    print(line.rstrip("\n"), flush=True)
                except Exception:  # noqa: BLE001 - echo must not kill the reader
                    pass
                log_file.write(line)
                log_file.flush()

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
    reader.join(timeout=30)
    return proc.returncode, "".join(lines), timed_out


def _next_attempt_log(part_dir: Path, prefix: str) -> Path:
    """``part_log_attempt_01.txt``-style path: attempt-numbered, never
    overwriting a previous attempt's log."""
    n = _next_attempt_number(part_dir, f"{prefix}attempt_", ".txt")
    return part_dir / f"{prefix}attempt_{n:02d}.txt"


def _promote_latest_log(attempt_log: Path, legacy_path: Path) -> None:
    """Keep ``part_log.txt`` / ``part_remodel_log.txt`` as a COPY of the
    newest attempt log -- backward-compatible pointer for tooling that
    expects the legacy filename. History files are never touched."""
    try:
        shutil.copyfile(attempt_log, legacy_path)
    except OSError:
        pass


def _newest(work_dir: Path, pattern: str) -> Path | None:
    """Newest non-empty file matching pattern -- delegates to
    ``file_utils.newest_file`` (single source of truth, R3)."""
    return newest_file(work_dir, pattern)


def _resume_recovery_candidate(part_dir: Path, request: str) -> Path | None:
    """Return a matching unfinished generated script eligible for one Aider recovery.

    New jobs record request fingerprints in ``resume_recovery_attempts.json``.
    Legacy jobs are accepted only when the cached CADBrief's
    ``user_request_raw`` exactly matches the current part request. Audit-only
    builder/reuse/features scripts are deliberately excluded. Existing STEP
    output does not imply success: artifacts are exported before autonomous QA
    and may accompany a failed run. The request+source fingerprint below is
    the replay guard and lets retries continue from the latest repaired code.
    """
    candidates = [
        p for p in part_dir.glob("temp_design*.py")
        if p.is_file()
        and p.stat().st_size > 0
        and not p.name.startswith((
            "temp_design_builder", "temp_design_reuse", "temp_design_features",
        ))
    ]
    if not candidates:
        return None
    design = max(candidates, key=lambda p: p.stat().st_mtime_ns)
    request_fp = hashlib.sha256(request.encode("utf-8")).hexdigest()
    try:
        cache = json.loads(
            (part_dir / "pipeline_cache" / "cad_brief.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError, TypeError):
        return None
    if str(cache.get("user_request_raw", "")) != request:
        return None

    design_fp = hashlib.sha256(design.read_bytes()).hexdigest()
    attempt_key = f"{request_fp}:{design_fp}"
    marker = part_dir / "resume_recovery_attempts.json"
    try:
        attempted = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(attempted, list):
            attempted = []
    except (OSError, ValueError, TypeError):
        attempted = []
    if attempt_key in attempted:
        return None
    # Mark before launching: a crash/timeout must not replay the same
    # deterministic recovery forever on every resume.
    marker.write_text(
        json.dumps(attempted + [attempt_key], indent=2), encoding="utf-8"
    )
    return design


def _part_plan_cache_matches_request(part_dir: Path, request: str) -> bool:
    """Whether the single-part planning cache belongs to this exact request.

    The inner MAC cache predates assembly-level PartSpec fingerprints and may
    otherwise replay an old CADBrief/ArchitectPlan after the Decomposer has
    changed a part.  Missing or unreadable metadata is conservatively stale.
    """
    try:
        cached = json.loads(
            (part_dir / "pipeline_cache" / "cad_brief.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError, TypeError):
        return False
    return str(cached.get("user_request_raw", "")) == request


def _explicit_accepted_cache(
    spec: PartSpec, part_dir: Path, request: str, *, announce: bool = True,
) -> PartResult | None:
    """Load a user-approved on-disk part cache, guarded by request hash.

    Merely finding STEP output is insufficient because artifacts are written
    before QA.  Reuse therefore requires an explicit marker naming all three
    artifacts and matching the exact current PartSpec request.
    """
    marker = part_dir / "accepted_part_cache.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        expected = hashlib.sha256(request.encode("utf-8")).hexdigest()
        if data.get("request_sha256") != expected:
            return None
        step = part_dir / str(data["step"])
        stl = part_dir / str(data["stl"])
        design = part_dir / str(data["python"])
        if not all(p.is_file() and p.stat().st_size > 0 for p in (step, stl, design)):
            return None
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if announce:
        print(f"[assembly] part {spec.part_id}: using explicit accepted disk cache")
    return PartResult(
        part_id=spec.part_id,
        step_path=str(step),
        stl_path=str(stl),
        py_path=str(design),
        part_dir=str(part_dir),
        ok=True,
        attempts=0,
        error=None,
        token_usage=_load_cumulative_tokens(part_dir),
    )


def load_explicit_accepted_cache(
    spec: PartSpec, parts_root: Path,
) -> PartResult | None:
    """Load an explicitly accepted result before selecting a build path."""
    return _explicit_accepted_cache(
        spec, parts_root / spec.part_id, spec.description,
    )


def has_explicit_accepted_cache(spec: PartSpec, parts_root: Path) -> bool:
    """Return whether *spec* has a valid operator-approved cache marker.

    This is the side-effect-free routing counterpart of
    :func:`load_explicit_accepted_cache`; it deliberately performs the same
    request-hash and artifact checks without printing a cache-hit message.
    """
    return _explicit_accepted_cache(
        spec, parts_root / spec.part_id, spec.description, announce=False,
    ) is not None


def run_part(
    spec: PartSpec,
    parts_root: Path,
    *,
    feedback: str = "",
    force_refresh: bool = False,
) -> PartResult:
    """Generate one part via the single-part MAC pipeline.

    Parameters
    ----------
    spec:
        PartSpec whose ``description`` is the entire single-part prompt.
    parts_root:
        ``<work_dir>/parts`` -- the part gets ``parts_root/<part_id>/``.
    feedback:
        Optional QA feedback appended to the prompt on remodel rounds.
    force_refresh:
        Skip the per-part plan cache (used when the previous plan failed).
    """
    part_dir = parts_root / spec.part_id
    part_dir.mkdir(parents=True, exist_ok=True)

    request = spec.description
    if feedback:
        request = (
            f"{spec.description}\n\n"
            f"IMPORTANT correction from assembly-level QA of the previous "
            f"attempt (the part geometry was wrong):\n{feedback}"
        )

    cached = _explicit_accepted_cache(spec, part_dir, request)
    if cached is not None:
        return cached

    python_bin = cfg.PYTHON_BIN or sys.executable
    env = _make_subprocess_env()
    env.update(
        MAC_PART_REQUEST=request,
        MAC_PART_DIR=str(part_dir.resolve()),
        # A changed PartSpec must invalidate the inner single-part plan even
        # on assembly attempt 0.  Previously only outer retry rounds forced a
        # refresh, so an edited assembly prompt deterministically replayed the
        # stale geometry that the edit was meant to fix.
        MAC_FORCE_REFRESH="1" if (
            force_refresh or not _part_plan_cache_matches_request(part_dir, request)
        ) else "",
    )

    # Resume an interrupted code-generation run before paying for a full
    # regeneration. The exact cached request and script-content fingerprint
    # gate prevent stale code from a changed PartSpec being patched.
    recovery_script = _resume_recovery_candidate(part_dir, request)
    if recovery_script is not None:
        print(
            f"[assembly] part {spec.part_id}: unfinished matching script "
            f"found ({recovery_script.name}); trying one Aider recovery"
        )
        recovered = run_part_remodel(
            spec,
            parts_root,
            "Resume this interrupted generation. Complete every remaining "
            "TODO/placeholder, execute the model, and produce valid STEP and "
            "STL outputs while preserving the original design intent.",
            code_path=recovery_script,
        )
        if recovered is not None and recovered.ok:
            return recovered
        if recovered is not None and is_non_retryable_error(recovered.error):
            return recovered
        print(
            f"[assembly] part {spec.part_id}: resume recovery failed; "
            "falling back to full generation"
        )

    _seed_cumulative_tokens(part_dir)
    log_path = _next_attempt_log(part_dir, "part_log_")
    started = time.time()
    attempt = 1
    ok = False
    error: str | None = None
    try:
        returncode, output, timed_out = _stream_subprocess(
            [python_bin, "-u", "-m", "mac_assembly._part_runner"],
            cwd=part_dir,
            env=env,
            timeout=cfg.PART_PIPELINE_TIMEOUT,
            log_path=log_path,
        )
        _promote_latest_log(log_path, part_dir / "part_log.txt")
        if timed_out:
            ok = False
            error = (
                f"single-part pipeline timed out after "
                f"{cfg.PART_PIPELINE_TIMEOUT}s"
            )
        else:
            ok = returncode == 0
            if not ok:
                error = _tail_error(output, returncode or -1)
    except Exception as exc:  # noqa: BLE001
        # crashed before/at launch (no subprocess ran) -- no token archive
        error = f"part subprocess crashed: {exc}"

    step = _newest(part_dir, "temp_output_*.step")
    stl = _newest(part_dir, "temp_output_*.stl")
    design = _newest(part_dir, "temp_design_*.py")

    if ok and (step is None or stl is None):
        ok = False
        error = error or "pipeline reported success but STEP/STL artifacts missing"

    token_usage = _archive_attempt_tokens(part_dir, started)

    return PartResult(
        part_id=spec.part_id,
        step_path=str(step) if step else "",
        stl_path=str(stl) if stl else "",
        py_path=str(design) if design else "",
        part_dir=str(part_dir),
        ok=ok,
        attempts=attempt,
        error=None if ok else (error or "unknown failure"),
        token_usage=token_usage,
    )


def run_part_remodel(
    spec: PartSpec,
    parts_root: Path,
    feedback: str,
    *,
    code_path: Path | None = None,
) -> PartResult | None:
    """Patch an already-generated part with Aider instead of regenerating.

    Reuses MAC's ``graph_aider`` (modify-existing workflow) so verified
    features are preserved and fewer tokens are spent. Returns None when
    there is nothing to patch or the patch run fails in a RETRYABLE way --
    the caller then falls back to full regeneration. A non-retryable failure
    (auth / endpoint / dependency) returns a failed PartResult carrying the
    classified error so the caller does NOT burn a full regeneration on an
    environment that cannot succeed.
    """
    part_dir = parts_root / spec.part_id
    if not list(part_dir.glob("temp_design*.py")):
        return None  # nothing to patch

    # R3: give Aider the part's identity + intent, not just the bare QA error
    # list -- otherwise it patches blind, not knowing which part it is fixing.
    request = (
        f"You are modifying the existing part '{spec.part_id}' "
        f"({spec.part_name}). Original design intent:\n{spec.description}\n\n"
        f"Apply this assembly-level correction to the existing model "
        f"(do not redesign unrelated features):\n{feedback}"
    )

    python_bin = cfg.PYTHON_BIN or sys.executable
    env = _make_subprocess_env()
    env.update(
        MAC_PART_REQUEST=request,
        MAC_PART_DIR=str(part_dir.resolve()),
    )
    if code_path is not None:
        env["MAC_PART_CODE_PATH"] = str(code_path.resolve())

    _seed_cumulative_tokens(part_dir)
    log_path = _next_attempt_log(part_dir, "part_remodel_log_")
    started = time.time()
    try:
        returncode, output, timed_out = _stream_subprocess(
            [python_bin, "-u", "-m", "mac_assembly._part_runner", "--mode", "aider"],
            cwd=part_dir,
            env=env,
            timeout=cfg.PART_PIPELINE_TIMEOUT,
            log_path=log_path,
        )
        _promote_latest_log(log_path, part_dir / "part_remodel_log.txt")
        token_usage = _archive_attempt_tokens(part_dir, started)
        if timed_out:
            return None  # retryable: fall back to full regeneration
        if returncode != 0:
            error = _tail_error(output, returncode)
            if is_non_retryable_error(error):
                return _harvest(
                    spec, part_dir, ok=False, attempt=1, error=error,
                    token_usage=token_usage,
                )
            return None
    except Exception:  # noqa: BLE001
        return None

    return _harvest(spec, part_dir, ok=True, attempt=1, error=None)


def _harvest(spec: PartSpec, part_dir: Path, *, ok: bool, attempt: int,
             error: str | None, token_usage: dict | None = None) -> PartResult:
    step = _newest(part_dir, "temp_output_*.step")
    stl = _newest(part_dir, "temp_output_*.stl")
    design = _newest(part_dir, "temp_design_*.py")
    if ok and (step is None or stl is None):
        ok = False
        error = error or "remodel reported success but STEP/STL missing"
    if token_usage is None:
        token_usage = _load_cumulative_tokens(part_dir)
    return PartResult(
        part_id=spec.part_id,
        step_path=str(step) if step else "",
        stl_path=str(stl) if stl else "",
        py_path=str(design) if design else "",
        part_dir=str(part_dir),
        ok=ok, attempts=attempt,
        error=None if ok else (error or "unknown failure"),
        token_usage=token_usage,
    )


# Env vars forwarded to part subprocesses: API keys + proxy settings. Shared
# by the full-generation and the aider-remodel paths so both behave the same
# behind a proxy (R2).
_ENV_FORWARD_KEYS = (
    "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE",
    "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
)


def _make_subprocess_env() -> dict:
    """Build the env dict for a part subprocess: PYTHONPATH + PATH + the
    forwarded API/proxy vars. Single source for both the full-generation and
    aider-remodel subprocess paths.

    P0-1: when no DASHSCOPE_API_KEY env var exists but main() resolved a key
    from ``multi_agent_cad.config.DS_API_KEY``, the STARTUP SNAPSHOT is
    injected -- a mid-run config.py edit can no longer strip the key from
    later part subprocesses (2026-09-10: a blanked DS_API_KEY made every
    subsequent part die with "DASHSCOPE_API_KEY is not set", which the loop
    then retried as a generic part failure).
    """
    env = dict(
        PATH=os.environ.get("PATH", "/usr/bin:/bin"),
        PYTHONPATH=str(_PROJECT_ROOT),
    )
    for key in _ENV_FORWARD_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    if "DASHSCOPE_API_KEY" not in env and _API_KEY_SNAPSHOT:
        env["DASHSCOPE_API_KEY"] = _API_KEY_SNAPSHOT
    return env


def run_part_builder(spec: PartSpec, parts_root: Path) -> PartResult:
    """Generate a part by calling a named builder from mac_assembly.builders.

    Bypasses the LLM-driven single-part pipeline entirely (no Spec Planner,
    Architect, Coder, Skill Loop). The builder returns a build123d Shape
    which is exported to STEP/STL directly. Zero tokens spent on LLM.

    Use this when PartSpec.builder is set -- e.g. for structures the LLM
    struggles to generate correctly (horizontal-axis cylinders, knuckle
    ears with horizontal bores).
    """
    part_dir = parts_root / spec.part_id
    part_dir.mkdir(parents=True, exist_ok=True)

    builder_spec = spec.builder or {}
    name = builder_spec.get("name", "")
    params = builder_spec.get("params") or {}

    from mac_assembly.builders import build_part
    from build123d import export_step, export_stl

    step_path = part_dir / "temp_output_builder.step"
    stl_path = part_dir / "temp_output_builder.stl"
    py_path = part_dir / "temp_design_builder.py"

    ok = True
    error: str | None = None
    degraded = False
    warnings: list[str] = []
    try:
        shape = build_part(name, params)
        feature_log: list[str] = []
        if spec.features:
            from mac_assembly.feature_operators import apply_feature
            for i, feature in enumerate(spec.features):
                shape = apply_feature(shape, feature)
                feature_log.append(
                    f"{i + 1}. {feature.name} at "
                    f"{feature.attachment.attach_point_mm} "
                    f"direction={feature.attachment.direction}"
                )
        # Audit records both the deterministic base builder and the standard
        # feature-operator chain. It is intentionally non-executable.
        py_path.write_text(
            f'"""Auto-generated source for {spec.part_id} (builder: {name}).\n'
            f'Params: {params}\n'
            f'Feature operators: {feature_log}\n'
            f'Geometry was generated by builders.{name}(**params), then by '
            f'the shared feature_operators.apply_feature chain.\n'
            f'"""\n',
            encoding="utf-8",
        )
        export_step(shape, str(step_path))
        stl_warning = ""
        try:
            export_stl(shape, str(stl_path), tolerance=0.05, angular_tolerance=0.3)
        except Exception as exc:  # noqa: BLE001 - STL failure degrades, not fails
            # BUG-020: STEP is valid -> assembly can still proceed; mark
            # the part degraded so QA knows mesh-based checks (interference,
            # kinematic sweep) will skip. Previously the failure was silent
            # (stl_path=None, ok=True, no warning) and downstream QA
            # silently degraded.
            stl_path = None  # type: ignore
            stl_warning = (
                f"STL export failed ({type(exc).__name__}: {exc}); "
                f"mesh-based QA checks will skip this part"
            )
        if not step_path.is_file() or step_path.stat().st_size == 0:
            ok = False
            error = f"builder {name!r} produced empty STEP"
        elif stl_warning:
            # STEP ok, STL failed: degraded continue (BUG-020).
            degraded = True
            warnings = [stl_warning]
    except Exception as exc:  # noqa: BLE001
        ok = False
        error = f"builder {name!r} crashed: {exc}"

    return PartResult(
        part_id=spec.part_id,
        step_path=str(step_path) if step_path.is_file() else "",
        stl_path=str(stl_path) if stl_path and stl_path.is_file() else "",
        py_path=str(py_path),
        part_dir=str(part_dir),
        ok=ok,
        attempts=1,
        degraded=degraded,
        warnings=warnings,
        error=error,
        token_usage={},  # zero tokens (no LLM call)
    )


def run_part_reuse(
    spec: PartSpec,
    parts_root: Path,
    template_result: PartResult | None,
) -> PartResult:
    """Copy a template part's STEP/STL/py into this instance's directory.

    Zero LLM tokens. The instance shares the template's geometry but
    gets its own part directory (so the codegen + QA + selector
    resolver all see it as an independent labeled solid). Mates place
    it independently -- only the geometry is shared.

    Returns a PartResult with ok=False (and a clear error) when the
    template's result is missing or its STEP file is gone -- the caller
    (node_part_builder) should then surface this as a PART_MISSING QA
    signal pointing at the template.
    """
    import shutil

    part_dir = parts_root / spec.part_id
    part_dir.mkdir(parents=True, exist_ok=True)

    template_id = spec.reuses_part_id
    if template_result is None or not template_result.ok:
        return PartResult(
            part_id=spec.part_id,
            part_dir=str(part_dir),
            ok=False,
            attempts=0,
            error=(
                f"reuse template {template_id!r} not yet built or failed "
                f"-- template must appear before this instance in "
                f"brief.parts (node_part_builder sorts templates first)"
            ),
            token_usage={},
        )

    src_step = Path(template_result.step_path)
    src_stl = Path(template_result.stl_path) if template_result.stl_path else None
    src_py = Path(template_result.py_path) if template_result.py_path else None
    if not src_step.is_file():
        return PartResult(
            part_id=spec.part_id,
            part_dir=str(part_dir),
            ok=False,
            attempts=0,
            error=f"reuse template {template_id!r} STEP missing at {src_step}",
            token_usage={},
        )

    dst_step = part_dir / "temp_output_reuse.step"
    dst_stl = part_dir / "temp_output_reuse.stl"
    dst_py = part_dir / "temp_design_reuse.py"
    shutil.copy2(src_step, dst_step)
    if src_stl and src_stl.is_file():
        shutil.copy2(src_stl, dst_stl)
    if src_py and src_py.is_file():
        # Prepend an audit header to the template's actual source code.
        # Do NOT overwrite with just the header -- downstream Aider
        # repair and the audit trail both need the real source body.
        # Prepending is safe even if the template's source opens with
        # its own module docstring: Python treats the prepended string
        # as the new module docstring and the original as a no-op
        # string expression -- valid syntax, no semantic loss.
        original_code = src_py.read_text(encoding="utf-8")
        header = (
            f'"""Reused geometry from template {template_id!r} '
            f'(copied at runtime by run_part_reuse).\n'
            f'Template source: {src_py}\n'
            f'This file is a copy for audit trail -- the actual geometry '
            f'was generated by the template\'s pipeline (builder or MAC).\n'
            f'"""\n\n'
        )
        dst_py.write_text(header + original_code, encoding="utf-8")

    return PartResult(
        part_id=spec.part_id,
        step_path=str(dst_step),
        stl_path=str(dst_stl) if dst_stl.is_file() else "",
        py_path=str(dst_py),
        part_dir=str(part_dir),
        ok=True,
        attempts=0,  # zero LLM calls
        error=None,
        token_usage={},  # zero tokens (no LLM call)
    )


def run_part_builder_remodel(
    spec: PartSpec,
    parts_root: Path,
    feedback: str,
) -> PartResult:
    """Re-call a builder with LLM-adjusted params to fix a QA failure.

    Builder parts cannot be Aider-patched (``temp_design_builder.py`` is a
    non-executable audit file), so the previous fallback path tried Aider
    on it (failed), then ran the full LLM single-part pipeline on
    ``spec.description`` -- losing the zero-token benefit of the builder
    and ignoring the original builder params. This path instead asks the
    LLM to read the QA feedback and emit adjusted builder params, then
    re-calls the builder directly. One small LLM call instead of the full
    MAC pipeline; preserves the deterministic builder geometry.

    Always returns a PartResult. On failure ok=False with a specific error
    string so the Judge / FeedbackRouter can route accordingly:
      - "LLM param-adjust call failed: <exc>"      -- LLM call crashed
      - "LLM returned invalid params ..."          -- non-dict / empty name
      - "LLM switched to unknown builder <name>"   -- builder name not registered
      - "param validation failed: extra=..., missing=..." -- sig mismatch
      - "<builder crash / empty STEP error>"       -- adjusted params didn't build
    Caller does NOT fall back to full LLM regeneration -- builder geometry
    topology must match the Mating Architect's SELECTOR anchors.

    Token accounting: the param-adjust call runs IN THE PARENT process, so
    its spend is already counted by the parent tracker. It is deliberately
    NOT also folded into the part's cumulative ledger -- the end-to-end
    report sums parent tracker + per-part ledgers, so merging it here would
    double-count every builder remodel.
    """
    from mac_assembly import config_assembly as cfg
    from mac_assembly.llm_utils import call_llm_json

    part_dir = parts_root / spec.part_id
    part_dir.mkdir(parents=True, exist_ok=True)

    builder_spec = spec.builder or {}
    name = builder_spec.get("name", "")
    params = builder_spec.get("params") or {}

    system_prompt = (
        "You adjust the parameters of a parametric CAD builder to fix an "
        "assembly-QA failure. Output ONE JSON object with the adjusted "
        "params. Keep the builder name unchanged. Only change params that "
        "need to change to address the feedback. Return JSON like: "
        '{"name": "<builder_name>", "params": {...}}'
    )
    user_prompt = (
        f"Builder: {name}\n"
        f"Current params: {params}\n"
        f"Part description (intent + local coordinate convention):\n"
        f"{spec.description}\n\n"
        f"Assembly-QA feedback (fix these by adjusting params only -- do "
        f"not switch to a different builder):\n{feedback}"
    )

    try:
        raw = call_llm_json(
            system_prompt,
            user_prompt,
            model=cfg.MATING_MODEL,  # cheap structured-output model
            temperature=cfg.MATING_TEMPERATURE,
            max_tokens=4096,
            extra_kwargs=cfg.MATING_KWARGS,
        )
    except Exception as exc:  # noqa: BLE001
        return PartResult(
            part_id=spec.part_id,
            ok=False,
            attempts=1,
            part_dir=str(part_dir),
            error=f"LLM param-adjust call failed: {exc}",
            token_usage=_load_cumulative_tokens(part_dir),
        )
    usage = _load_cumulative_tokens(part_dir)

    new_name = raw.get("name", name) if isinstance(raw, dict) else name
    new_params = raw.get("params", params) if isinstance(raw, dict) else params
    if not isinstance(new_params, dict) or not new_name:
        return PartResult(
            part_id=spec.part_id,
            ok=False,
            attempts=1,
            part_dir=str(part_dir),
            error="LLM returned invalid params (non-dict or empty builder name)",
            token_usage=usage,
        )
    # Sanity: the LLM should not switch builders (different geometry
    # family); allow it only if the new name is a known builder.
    from mac_assembly.builders import BUILDERS
    if new_name not in BUILDERS:
        return PartResult(
            part_id=spec.part_id,
            ok=False,
            attempts=1,
            part_dir=str(part_dir),
            error=f"LLM switched to unknown builder {new_name!r}",
            token_usage=usage,
        )
    # Validate param keys against the builder's actual signature.
    # The LLM often renames params (plate_w -> plate_width); without
    # this check, build_part(**new_params) raises TypeError and the
    # failure surfaces as a generic builder crash rather than a
    # param-key mismatch. We fail fast here so the caller surfaces
    # the mismatch as a specific error the Judge can route on,
    # instead of a generic builder crash. Extra keys are ALWAYS
    # rejected: builders no longer declare a `**_` sink (D2 -- the
    # sink silently absorbed renamed keys, so the builder ran with
    # defaults and produced wrong geometry). Missing required params
    # (no default) always fail.
    import inspect
    sig = inspect.signature(BUILDERS[new_name])
    signature_params = sig.parameters
    extra = set(new_params) - set(signature_params)
    missing_required = {
        n for n, p in signature_params.items()
        if p.default is inspect.Parameter.empty and n not in new_params
    }
    if extra or missing_required:
        return PartResult(
            part_id=spec.part_id,
            ok=False,
            attempts=1,
            part_dir=str(part_dir),
            error=(
                f"param validation failed: "
                f"extra={sorted(extra) if extra else []}, "
                f"missing={sorted(missing_required)}"
            ),
            token_usage=usage,
        )

    modified = spec.model_copy(update={
        "builder": {"name": new_name, "params": new_params},
    })
    result = run_part_builder(modified, parts_root)
    result.attempts = 1  # single remodel attempt
    result.token_usage = _load_cumulative_tokens(part_dir)
    # Return unconditionally: on success ok=True with the built STEP path;
    # on failure ok=False with the specific builder-crash / empty-STEP
    # error from run_part_builder, so the Judge can distinguish build
    # failures from LLM-param failures above.
    return result


def _tail_error(stdout: str, returncode: int) -> str:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    tail = " | ".join(lines[-4:])[:400]
    return f"returncode={returncode}; tail: {tail}"


def _build_synthetic_spec(spec: PartSpec) -> PartSpec:
    """Construct a PartSpec for the MAC subprocess: base_body.description
    + attach-point hints so the Coder agent leaves flat surfaces where
    features will attach.

    The synthetic spec's `description` is base_body.description + a
    "## Attach-point constraints" section listing each feature's
    attach_point + direction + name. The `builder` / `base_body` /
    `features` fields are cleared so the subprocess treats this as a
    normal single-part MAC run.
    """
    if spec.base_body is None:
        raise ValueError(f"_build_synthetic_spec: part {spec.part_id!r} has no base_body")
    hints = ["## Attach-point constraints (leave flat surfaces here for v3 features):"]
    for i, f in enumerate(spec.features):
        ap = f.attachment.attach_point_mm
        hints.append(
            f"  {i+1}. {f.name} at ({ap[0]}, {ap[1]}, {ap[2]}) direction={f.attachment.direction} "
            f"-- leave a flat region here for feature attachment"
        )
    synthetic_desc = spec.base_body.description + "\n\n" + "\n".join(hints)
    return spec.model_copy(update={
        "description": synthetic_desc,
        "builder": None,
        "base_body": None,
        "features": [],
    })


_LOCAL_BOUNDS_RE = re.compile(
    r"Local\b[^.\n]*?\bX\s*=\s*([+-]?\d+\.?\d*)\s*\.\.\s*([+-]?\d+\.?\d*)\s*,\s*"
    r"Y\s*=\s*([+-]?\d+\.?\d*)\s*\.\.\s*([+-]?\d+\.?\d*)\s*,\s*"
    r"Z\s*=\s*([+-]?\d+\.?\d*)\s*\.\.\s*([+-]?\d+\.?\d*)",
    re.IGNORECASE,
)


def _parse_expected_bounds(description: str) -> tuple[
    float, float, float, float, float, float
] | None:
    """Parse 'Local [coords:] X=min..max, Y=min..max, Z=min..max' from v3 spec.

    Tolerates variants like:
      - "Local X=-5..5, Y=0..30, Z=0..10"
      - "Local coordinates: X = -20..0, Y = -5..+5, Z = 0..10"
      - "Local coords: X=-5..+5, Y=0..30, Z=0..10"

    Returns (xmin, xmax, ymin, ymax, zmin, zmax) or None if not found.
    """
    m = _LOCAL_BOUNDS_RE.search(description)
    if not m:
        return None
    return tuple(float(g) for g in m.groups())  # type: ignore[return-value]


def _maybe_recenter_base_body(base_body: Any, spec: PartSpec) -> Any:
    """Auto-correct base body position when LLM Coder used Sketch+extrude
    (default-centered on origin) instead of Pos(x,y,z)*Box(...) (positioned
    at spec's center).

    Detection (priority order):
      1. Structured ``base_body.local_bounds`` dict {xmin, xmax, ymin, ymax,
         zmin, zmax} -- set by Decomposer when it knows the prism bounds.
         This is the reliable path; no free-text parsing.
      2. Regex fallback: parse 'Local X=.., Y=.., Z=..' from
         ``base_body.description``. Less reliable -- description format
         varies across prompts, but covers existing Decomposer output
         that hasn't been migrated to local_bounds yet.

    After getting expected bounds, compare with actual base body bounds.
    If the SIZES match (within 0.5mm tolerance, or 5mm on axes where the
    body has fillets / half-cylinder tips that may extend the bbox) but
    the POSITIONS differ, translate the base body so its min corner
    aligns with the expected min corner.

    This is a deterministic post-hoc fix -- does not rely on LLM honoring
    prompt instructions about Pos()*Box() vs Sketch+extrude. Single-part
    pipeline prompts (python_coder.md, repair.md) actively teach Sketch+
    extrude as the canonical pattern, so v3 spec instructions to use
    Pos()*Box() are routinely overridden by the LLM's training prior.

    If sizes don't match (e.g. LLM generated wrong dims), no auto-fix --
    the shape itself is wrong, return as-is so QA can flag it.
    """
    if spec.base_body is None:
        return base_body
    # Priority 1: structured local_bounds
    expected = None
    lb = spec.base_body.local_bounds
    if lb is not None:
        try:
            expected = (
                float(lb["xmin"]), float(lb["xmax"]),
                float(lb["ymin"]), float(lb["ymax"]),
                float(lb["zmin"]), float(lb["zmax"]),
            )
        except (KeyError, TypeError, ValueError):  # noqa: BLE001
            expected = None
    # Priority 2: regex fallback on description
    if expected is None:
        expected = _parse_expected_bounds(spec.base_body.description)
    if expected is None:
        return base_body  # no bounds info, can't verify
    ex_min_x, ex_max_x, ex_min_y, ex_max_y, ex_min_z, ex_max_z = expected
    try:
        bb = base_body.bounding_box()
    except Exception:  # noqa: BLE001
        return base_body
    # Check size match along each axis (within 0.5mm tolerance)
    size_x = bb.max.X - bb.min.X
    size_y = bb.max.Y - bb.min.Y
    size_z = bb.max.Z - bb.min.Z
    ex_size_x = ex_max_x - ex_min_x
    ex_size_y = ex_max_y - ex_min_y
    ex_size_z = ex_max_z - ex_min_z
    # Allow some slack for fillets / half-cylinder tips that may extend
    # the bbox beyond the prism's local bounds (e.g. finger_distal has
    # a half-cylinder tip making Y extend past 20 to 25). Use a generous
    # 5mm tolerance on the long axis (max expected dimension), 0.5mm on
    # the other two -- otherwise a non-long axis with 1-4mm error gets
    # mis-judged as "matched" and then wrong-shape-fixed by translation.
    ex_sizes = (ex_size_x, ex_size_y, ex_size_z)
    long_axis = max(range(3), key=lambda i: ex_sizes[i])
    tols = tuple(5.0 + 1e-9 if i == long_axis else 0.5 for i in range(3))
    if abs(size_x - ex_size_x) > tols[0]:
        return base_body  # X size mismatch — wrong shape, no auto-fix
    if abs(size_y - ex_size_y) > tols[1]:
        return base_body  # Y size mismatch
    if abs(size_z - ex_size_z) > tols[2]:
        return base_body  # Z size mismatch
    # Compute translation: shift actual min to expected min along each axis
    dx = ex_min_x - bb.min.X
    dy = ex_min_y - bb.min.Y
    dz = ex_min_z - bb.min.Z
    if abs(dx) < 0.01 and abs(dy) < 0.01 and abs(dz) < 0.01:
        return base_body  # already at expected position
    src = "local_bounds" if lb is not None else "description regex"
    print(f"[assembly] part {spec.part_id}: auto-recentering base body by "
          f"({dx:.2f}, {dy:.2f}, {dz:.2f}) -- LLM used Sketch+extrude "
          f"(centered at origin) instead of Pos()*Box(); applying "
          f"deterministic post-hoc fix from v3 spec {src}.")
    from build123d import Pos
    return Pos(dx, dy, dz) * base_body


def run_part_with_features(
    spec: PartSpec,
    parts_root: Path,
    feedback: str = "",
) -> PartResult:
    """v3 path: LLM-generated base body + deterministic feature operators.

    1. Build a synthetic description (base_body.description + attach-point
       hints) for the MAC subprocess.
    2. No-op remodel guard: when the FULL spec fingerprint is unchanged
       since a FAILED generation, there is no part-level repair channel
       (the REMODEL route never modifies the spec, so the same base +
       the same features reproduces the same failure). Return the
       structured "v3 spec unchanged" marker for the router to RECOMPOSE
       instead of re-running the identical feature chain.
    3. Feature-only remodel fast path: with feedback + a cached base STEP
       whose base_body fingerprint (description AND key_dimensions/
       local_bounds) is unchanged, skip MAC Coder and re-apply the feature
       chain from the CURRENT spec (feature-only changes still take
       effect; saves 1x LLM call + tens of seconds).
    4. Otherwise: run MAC Coder on the synthetic spec. If that fails
       AND we have feedback, do ONE Aider-remodel retry (run_part_remodel)
       on the base body before giving up.
    5. If still failing: with a STEP artifact, return a DEGRADED result
       (ok=True + degraded=True + warnings) so the assembly proceeds for
       inspection but the result is never reused from cache; without an
       artifact, return ok=False -- NO fall-through to LLM Full Regen
       (would re-introduce the kinematic-feature failures v3 was designed
       to avoid).
    6. On success: load base STEP via build123d.import_step, apply each
       feature via apply_feature(), export final STEP/STL + audit .py
       + spec cache (temp_v3_spec.json, with fingerprint + outcome).
    """
    import json
    import shutil

    part_dir = parts_root / spec.part_id
    part_dir.mkdir(parents=True, exist_ok=True)

    base_step_path = part_dir / "temp_output_base.step"
    spec_cache_path = part_dir / "temp_v3_spec.json"

    spec_fp = part_spec_fingerprint(spec)
    base_fp = base_body_fingerprint(spec)

    cached: dict = {}
    if spec_cache_path.exists():
        try:
            loaded = json.loads(spec_cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cached = loaded
        except Exception:  # noqa: BLE001
            cached = {}  # corrupted cache -> full base regen

    # ----- Step 2: no-op remodel guard -----
    if (
        feedback
        and cached.get("spec_fingerprint") == spec_fp
        and cached.get("outcome") == "failed"
    ):
        return PartResult(
            part_id=spec.part_id, part_dir=str(part_dir), ok=False,
            error=(
                "v3 spec unchanged; the same base_body + feature chain "
                "already failed generation with this exact spec and the "
                "REMODEL route cannot modify the spec -- RECOMPOSE is "
                "required (adjust base_body/features or switch this part "
                "to a v2 builder)"
            ),
            token_usage={},
        )

    # ----- Step 3: feature-only remodel fast path detection -----
    reuse_base = False
    if (
        feedback
        and base_step_path.exists()
        and spec.base_body is not None
        and cached.get("base_body_fingerprint") == base_fp
    ):
        reuse_base = True
        print(f"[assembly] part {spec.part_id}: v3 feature-only "
              f"remodel -- reusing cached base STEP, skipping "
              f"MAC Coder (~1x LLM call + tens of seconds saved)")

    # ----- Steps 4-5: generate (or reuse) the base body -----
    # Declared here (not in else) so the reuse_base fast path also leaves them
    # bound -- the final return PartResult(... token_usage=base_tokens) reads them.
    base_warning: str | None = None
    base_tokens: dict = {}  # populated with base_result.token_usage in the else branch
    if reuse_base:
        try:
            from build123d import import_step
            loaded = import_step(str(base_step_path))
            # import_step returns a Solid (or Compound of solids); wrap
            # bare Solid in a Compound so export_step works on the final
            # body (export_step requires Compound, not bare Solid).
            from build123d import Compound
            # Imported STEP compounds can contain a single valid Solid yet
            # still fail when passed directly back to export_step because
            # the OCCT import wrapper is not always re-exportable. Rebuild a
            # clean Compound from its solids in every case.
            solids = list(loaded.solids()) if hasattr(loaded, "solids") else [loaded]
            if not solids:
                raise ValueError("cached base STEP contains no solids")
            base_body = Compound(children=solids)
        except Exception as exc:  # noqa: BLE001
            # The cached base itself is unloadable (e.g. truncated by a
            # crashed part subprocess): drop it AND the spec cache so the
            # next round does a full base regeneration instead of the
            # feature-only fast path replaying the same broken file.
            base_step_path.unlink(missing_ok=True)
            spec_cache_path.unlink(missing_ok=True)
            return PartResult(
                part_id=spec.part_id, part_dir=str(part_dir), ok=False,
                error=f"v3 cached base STEP load failed: {exc}",
                token_usage={},
            )
        # Cached base body may also need recentering (if the cache was
        # populated before _maybe_recenter_base_body existed).
        base_body = _maybe_recenter_base_body(base_body, spec)
    else:
        spec_synthetic = _build_synthetic_spec(spec)
        base_result = run_part(spec_synthetic, parts_root, feedback=feedback)
        if (
            not base_result.ok
            and feedback
            and not is_non_retryable_error(base_result.error)
        ):
            # Aider retry on base body (single retry -- see plan §3a).
            # Skipped for non-retryable failures (auth/endpoint/dependency):
            # the environment cannot succeed, a retry only burns tokens/time.
            remodeled = run_part_remodel(spec_synthetic, parts_root, feedback)
            if remodeled is not None and remodeled.ok:
                base_result = remodeled
        # base_warning already bound above (before the reuse_base branch).
        if not base_result.ok:
            # Continue-on-failure: the LLM Coder / Aider retry failed (e.g.,
            # non-watertight mesh, MISSED_CUT on hemisphere trim, topology
            # errors) but the subprocess produced a STEP file from the last
            # attempt. The user explicitly wants the assembly to proceed
            # with the failed geometry so they can inspect the assembled
            # result; the downstream QA will catch any remaining issues.
            # The final PartResult marks this DEGRADED (ok=True +
            # degraded=True + warnings) -- NOT a clean success: the node's
            # cache-reuse guard refuses to skip a degraded part on later
            # rounds, and the warning is surfaced in the QA report for the
            # Judge. Truly fatal cases (no STEP file at all) still return
            # ok=False.
            if base_result.step_path and Path(base_result.step_path).is_file():
                print(f"[assembly] part {spec.part_id}: v3 base body "
                      f"generation FAILED ({base_result.error}); continuing "
                      f"with last attempt's STEP -- the assembly will use "
                      f"this geometry as-is.")
                base_warning = (
                    f"base body generation failed ({base_result.error}); "
                    f"using last attempt's STEP"
                )
            else:
                return PartResult(
                    part_id=spec.part_id, part_dir=str(part_dir), ok=False,
                    error=(
                        f"v3 base body generation failed after Aider retry "
                        f"AND no STEP file produced. Base error: "
                        f"{base_result.error}. Features: "
                        f"{[f.name for f in spec.features]}. Consider "
                        f"RECOMPOSE (Decomposer switch to v2 builder) or "
                        f"HALT."
                    ),
                    token_usage=base_result.token_usage,
                )
        # Cache the base body STEP for future feature-only remodels
        try:
            shutil.copy2(base_result.step_path, base_step_path)
        except Exception:  # noqa: BLE001
            pass
        try:
            from build123d import import_step, Compound
            loaded = import_step(str(base_step_path))
            solids = list(loaded.solids()) if hasattr(loaded, "solids") else [loaded]
            if not solids:
                raise ValueError("generated base STEP contains no solids")
            # Normalize away the imported Compound wrapper before applying
            # features or exporting the final artifact.
            base_body = Compound(children=solids)
        except Exception as exc:  # noqa: BLE001
            # copy2 above already put the (corrupt) salvaged STEP into the
            # base cache: drop it and the spec cache, same rationale as the
            # reuse_base load-failure branch.
            base_step_path.unlink(missing_ok=True)
            spec_cache_path.unlink(missing_ok=True)
            return PartResult(
                part_id=spec.part_id, part_dir=str(part_dir), ok=False,
                error=f"v3 base STEP load failed after generation: {exc}",
                token_usage=base_result.token_usage,
            )
        base_tokens = base_result.token_usage

    # Auto-correct base body position if LLM Coder used Sketch+extrude
    # (centered at origin) instead of Pos()*Box() (positioned per spec).
    base_body = _maybe_recenter_base_body(base_body, spec)

    # ----- Step 5: apply feature chain -----
    from mac_assembly.feature_operators import apply_feature
    final_body = base_body
    feature_log = []
    feature_failures: list[str] = []
    for i, feature in enumerate(spec.features):
        ap = feature.attachment.attach_point_mm
        try:
            final_body = apply_feature(final_body, feature)
            feature_log.append(f"  {i+1}. {feature.name} at {ap} "
                               f"direction={feature.attachment.direction} -- OK")
        except Exception as exc:  # noqa: BLE001
            # apply_feature already does multi-solid + disjoint fallback;
            # reaching here means the dispatch itself failed -- e.g. an
            # unknown operator name (FEATURE_OPERATORS[name] raises KeyError
            # OUTSIDE apply_feature's internal try). A part missing a
            # feature its mates reference must NOT report ok=True: QA would
            # fail downstream and the router would misattribute a part
            # defect to the mate design (remate instead of remodel).
            feature_log.append(f"  {i+1}. {feature.name} at {ap} -- FAILED: {exc}")
            feature_failures.append(f"{feature.name}: {exc}")

    # ----- Step 6: export final STEP/STL + audit + spec cache -----
    from build123d import export_step, export_stl
    final_step = part_dir / "temp_output_features.step"
    final_stl = part_dir / "temp_output_features.stl"
    audit_py = part_dir / "temp_design_features.py"
    # Export through staging files and promote on success: a failed STEP
    # write must not truncate the previous round's final STEP (the
    # node-level regression guard may keep pointing at that artifact when
    # this rebuild fails). The dot-prefixed names never match the
    # temp_output_*.step globs _newest() scans.
    staging_step = part_dir / ".staging_features.step"
    staging_stl = part_dir / ".staging_features.stl"
    try:
        export_step(final_body, str(staging_step))
    except Exception as exc:  # noqa: BLE001
        # The cached base is POISONED: its geometry fails the STEP writer,
        # so the feature-only fast path would deterministically replay this
        # failure on every later remodel round. Drop the cached base STEP +
        # spec cache so the next round does a FULL base regeneration (a
        # fresh LLM sample) instead.
        base_step_path.unlink(missing_ok=True)
        spec_cache_path.unlink(missing_ok=True)
        staging_step.unlink(missing_ok=True)
        staging_stl.unlink(missing_ok=True)
        return PartResult(
            part_id=spec.part_id, part_dir=str(part_dir), ok=False,
            error=f"v3 final export failed: {exc}",
            token_usage={},
        )
    try:
        export_stl(final_body, str(staging_stl), tolerance=0.05, angular_tolerance=0.3)
        staging_stl.replace(final_stl)
    except Exception:  # noqa: BLE001
        final_stl = None  # type: ignore
        staging_stl.unlink(missing_ok=True)
    staging_step.replace(final_step)

    # Audit .py (non-executable; records base + feature chain)
    audit_py.write_text(
        f'"""v3 part: base_body (LLM-generated) + {len(spec.features)} feature operators.\n\n'
        f'Base body STEP:   parts/{spec.part_id}/temp_output_base.step\n'
        f'Final STEP:       parts/{spec.part_id}/temp_output_features.step\n\n'
        f'Feature chain (applied in order):\n'
        + "\n".join(feature_log)
        + f'\n\nThis file is a copy for audit trail. The actual geometry was built\n'
        f'by run_part_with_features at runtime, calling import_step on the base\n'
        f'STEP then applying each feature operator via apply_feature().\n'
        f'"""\n',
        encoding="utf-8",
    )

    # Spec cache (for the no-op remodel guard + feature-only fast path).
    # `outcome` records how THIS spec last ended: "ok" (clean or degraded --
    # a degraded result still produced geometry), "failed" (no usable
    # geometry; feature chain incomplete). The next remodel with an
    # UNCHANGED fingerprint + "failed" outcome short-circuits to the
    # recompose marker instead of re-running the identical feature chain.
    spec_cache_data = {
        "base_body_description": spec.base_body.description if spec.base_body else "",
        "features": [f.model_dump(mode="json") for f in spec.features],
        "spec_fingerprint": spec_fp,
        "base_body_fingerprint": base_fp,
        "outcome": "failed" if feature_failures else "ok",
    }
    try:
        spec_cache_path.write_text(json.dumps(spec_cache_data, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if feature_failures:
        # Fail the part so the router takes the remodel/part_missing route
        # instead of remate. Artifacts above are still exported for
        # debugging; ok=False carries the attribution.
        return PartResult(
            part_id=spec.part_id,
            step_path=str(final_step) if final_step.is_file() else "",
            stl_path=str(final_stl) if final_stl and final_stl.is_file() else "",
            py_path=str(audit_py),
            part_dir=str(part_dir),
            ok=False,
            attempts=0,
            error=("v3 feature chain incomplete (part geometry is missing "
                   "features its mates may reference): "
                   + "; ".join(feature_failures)),
            token_usage=base_tokens,
        )

    # Clean success OR degraded continue-on-failure. Degraded is expressed
    # via degraded=True + warnings (NOT ok=True + error=<warning> as before):
    # `error` on an ok result used to make node_part_builder and the router
    # treat failed-but-present geometry as a clean success that later
    # rounds would reuse from cache forever.
    return PartResult(
        part_id=spec.part_id,
        step_path=str(final_step) if final_step.is_file() else "",
        stl_path=str(final_stl) if final_stl and final_stl.is_file() else "",
        py_path=str(audit_py),
        part_dir=str(part_dir),
        ok=True,
        attempts=0,
        degraded=bool(base_warning),
        warnings=[base_warning] if base_warning else [],
        error=None,
        token_usage=base_tokens,  # {} on reuse_base fast path; base_result's otherwise
    )
