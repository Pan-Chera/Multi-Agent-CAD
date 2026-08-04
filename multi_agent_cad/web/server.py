"""FastAPI server for the MAC web UI.

Run with::

    python -m multi_agent_cad.web

Listens on ``0.0.0.0:8000`` by default (override with ``MAC_WEB_HOST`` /
``MAC_WEB_PORT``). Single-user, trusted-network only — the pipeline
executes generated Python server-side; treat the server as your own dev
environment.

Architecture::

    browser  ──HTTP──▶  FastAPI (this module)
                         ├─ POST /api/run           → spawn web_runner subprocess
                         ├─ GET  /api/jobs/{id}/events  → SSE progress stream
                         ├─ GET  /api/jobs/{id}/files/{name} → serve GLB/STEP/...
                         └─ static/  → index.html, app.js, styles.css
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError as exc:  # pragma: no cover
    print(
        "Web extras not installed. Run:  pip install -e '.[web]'",
        file=sys.stderr,
    )
    raise

_STATIC_DIR = Path(__file__).resolve().parent / "static"
# Repo root, so the web_runner subprocess (whose cwd is a per-job tempdir)
# can still `import multi_agent_cad` without a pip-installed package.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# In-memory job registry (single-user, process-lifetime). A dict is fine
# because only one user is expected and jobs rarely exceed a handful.
_JOBS: dict[str, dict[str, Any]] = {}

# Auto-cleanup: delete job tempdirs 6h after the job started (and exited),
# and sweep orphaned macjob_* dirs from previous server runs every 30 min.
_CLEANUP_AFTER_SECONDS = 6 * 3600
_CLEANUP_INTERVAL_SECONDS = 30 * 60


async def _cleanup_old_jobs() -> None:
    """Delete finished-job tempdirs older than 6h, plus orphaned macjob_* dirs.

    Two sources:
      1. Known jobs in _JOBS whose subprocess has exited and whose
         ``started_at`` (wall-clock) is older than the threshold.
      2. Orphaned ``macjob_*`` dirs in the system tempdir that aren't in
         _JOBS (left over from previous server runs / crashes). Protected
         from the create-race by the mtime check — a fresh tempdir's mtime
         is ~now, well above the threshold.
    """
    now = time.time()
    threshold = now - _CLEANUP_AFTER_SECONDS
    known_tempdirs: set[str] = set()
    finished: list[str] = []

    for job_id, job in list(_JOBS.items()):
        td = str(job.get("tempdir") or "")
        known_tempdirs.add(td)
        started = job.get("started_at", now)
        proc = job.get("proc")
        proc_done = proc is not None and proc.returncode is not None
        if proc_done and started < threshold:
            shutil.rmtree(job["tempdir"], ignore_errors=True)
            finished.append(job_id)
    for jid in finished:
        _JOBS.pop(jid, None)

    temp_root = tempfile.gettempdir()
    n_orphans = 0
    for p in Path(temp_root).glob("macjob_*"):
        if str(p) in known_tempdirs:
            continue
        try:
            if p.stat().st_mtime < threshold:
                shutil.rmtree(p, ignore_errors=True)
                n_orphans += 1
        except OSError:
            continue
    if finished or n_orphans:
        print(f"[web] cleanup: removed {len(finished)} finished job(s), "
              f"{n_orphans} orphan(s)", file=sys.stderr)


async def _cleanup_loop() -> None:
    """Periodic background sweep; started at app startup, cancelled on shutdown."""
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        try:
            await _cleanup_old_jobs()
        except Exception as exc:  # pragma: no cover
            print(f"[web] cleanup pass failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Provider presets & config schema
# ---------------------------------------------------------------------------

_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "qwen": {
        "ds_base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "model_hint": "qwen3.7-max",
    },
    "openai": {
        "ds_base_url": "https://api.openai.com/v1",
        "model_hint": "gpt-4o",
    },
    "deepseek": {
        "ds_base_url": "https://api.deepseek.com/v1",
        "model_hint": "deepseek-chat",
    },
    "gemini": {
        "ds_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model_hint": "gemini-2.0-flash",
    },
    "ollama": {
        "ds_base_url": "http://localhost:11434/v1",
        "model_hint": "qwen2.5-coder:32b",
    },
}


def _config_schema() -> dict:
    """Return editable config fields with current defaults from config.py."""
    from multi_agent_cad import config as cfg

    fields = [
        "DS_BASE_URL", "API_KEY_ENV_VAR", "API_BASE_ENV_VAR",
        "MAX_RETRIES", "MAX_EXEC_RETRIES",
        "SPEC_PLANNER_MODEL", "SPEC_PLANNER_TEMPERATURE", "SPEC_PLANNER_MAX_TOKENS",
        "ARCHITECT_MODEL", "ARCHITECT_TEMPERATURE", "ARCHITECT_MAX_TOKENS",
        "CODER_MODEL", "CODER_TEMPERATURE", "CODER_MAX_TOKENS",
        "AIDER_MODEL", "AIDER_MAX_TOKENS",
        "REPAIR_MODEL", "REPAIR_TEMPERATURE", "REPAIR_MAX_TOKENS",
        "USER_REQUEST", "WORKFLOW_ID",
    ]
    return {k: getattr(cfg, k, None) for k in fields}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()


app = FastAPI(title="MAC Web UI", lifespan=_lifespan)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config/schema")
async def get_schema() -> dict:
    return {"config": _config_schema(), "providers": _PROVIDER_PRESETS}


@app.post("/api/run")
async def run(req: Request) -> dict:
    body = await req.json()
    config = body.get("config", {}) or {}
    prompt = (body.get("prompt") or config.get("USER_REQUEST", "") or "").strip()
    dest_path = (body.get("dest_path") or "").strip()
    workflow = body.get("workflow") or config.get("WORKFLOW_ID", "original")
    api_key = body.get("api_key", "") or config.get("DS_API_KEY", "")

    if not prompt:
        raise HTTPException(400, "prompt is required")
    if not api_key:
        raise HTTPException(400, "api_key is required (fill it in the form)")

    job_id = uuid.uuid4().hex[:12]
    tempdir = Path(tempfile.mkdtemp(prefix=f"macjob_{job_id}_"))

    # config.json — everything except the API key (key goes via env var).
    cfg_out = dict(config)
    cfg_out["USER_REQUEST"] = prompt
    cfg_out["WORKFLOW_ID"] = workflow
    cfg_out.pop("DS_API_KEY", None)  # never persist the key to disk
    config_path = tempdir / "config.json"
    config_path.write_text(json.dumps(cfg_out, indent=2), encoding="utf-8")

    env = {
        **os.environ,
        "MAC_CONFIG_FILE": str(config_path),
        "DASHSCOPE_API_KEY": api_key,
        "PYTHONUNBUFFERED": "1",
        # The subprocess cwd is the per-job tempdir (isolation), not the
        # project root — and the package may not be pip-installed in the
        # venv (editable install). Add the repo root to sys.path so the
        # runner can `import multi_agent_cad` from anywhere.
        "PYTHONPATH": str(_REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }

    cmd = [
        sys.executable, "-m", "multi_agent_cad.web_runner",
        "--config", str(config_path),
        "--out", str(tempdir),
        "--workflow", workflow,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        cwd=str(tempdir),
    )

    queue: asyncio.Queue = asyncio.Queue()
    job: dict[str, Any] = {
        "proc": proc,
        "tempdir": tempdir,
        "dest_path": dest_path or None,
        "queue": queue,
        "result": None,
        "started_at": time.time(),
    }
    _JOBS[job_id] = job

    asyncio.create_task(_reader_task(job_id, job))
    asyncio.create_task(_watcher_task(job_id, job))
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    """Terminate a running job. Whatever artifacts exist on disk become
    downloadable — the reader task will emit a synthetic 'done' with the
    harvested file paths once the subprocess exits."""
    if job_id not in _JOBS:
        raise HTTPException(404, "job not found")
    job = _JOBS[job_id]
    proc = job.get("proc")
    if proc is None or proc.returncode is not None:
        return {"status": "already_done", "rc": proc.returncode if proc else None}
    if job.get("cancelled"):
        return {"status": "already_cancelling"}
    job["cancelled"] = True
    asyncio.create_task(_terminate_job(job))
    return {"status": "cancelling"}


async def _terminate_job(job: dict) -> None:
    """Send SIGTERM, wait up to 5s, escalate to SIGKILL if the runner
    hasn't exited (e.g. stuck inside a subprocess.run for the CAD script).
    The reader task observes the proc exit and emits the synthetic 'done'."""
    proc = job["proc"]
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
    except ProcessLookupError:
        pass


async def _reader_task(job_id: str, job: dict) -> None:
    proc = job["proc"]
    queue: asyncio.Queue = job["queue"]
    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                await queue.put({"log": line})
                continue
            if not isinstance(msg, dict):
                # The subprocess (web_runner + LangGraph / aider / build123d)
                # sometimes prints lines that happen to parse as valid JSON
                # but aren't objects (quoted strings, numbers, arrays) — e.g.
                # aider echoing a JSON literal mid-repair. Treat them as log
                # lines so the SSE stream keeps flowing instead of crashing
                # the reader on `msg.get("done")`.
                await queue.put({"log": line})
                continue
            if msg.get("done"):
                if job.get("dest_path"):
                    _copy_artifacts(msg, job["dest_path"])
                job["result"] = msg
                await queue.put(msg)
                break
            await queue.put(msg)

        rc = await proc.wait()
        if job.get("result") is None:
            if job.get("cancelled"):
                # User clicked Stop — emit a synthetic 'done' from whatever's
                # on disk so the UI can offer downloads of half-finished work.
                msg = _harvest_intermediate(job)
                if job.get("dest_path"):
                    _copy_artifacts(msg, job["dest_path"])
                job["result"] = msg
                await queue.put(msg)
            else:
                err = {"error": f"runner exited (rc={rc}) without 'done'", "rc": rc}
                job["result"] = err
                await queue.put(err)
    except Exception as exc:  # pragma: no cover
        err = {"error": f"reader task crashed: {exc}"}
        job["result"] = err
        await queue.put(err)
    finally:
        await queue.put(None)  # sentinel: SSE stream ends


def _stl_to_glb(stl_path: str, glb_path: str) -> bool:
    """Convert an STL mesh to GLB for browser preview. Returns True on success."""
    try:
        import trimesh
        mesh = trimesh.load(stl_path)
        mesh.export(glb_path, file_type="glb")
        return True
    except Exception as exc:
        print(f"[web] STL→GLB conversion failed: {exc}", file=sys.stderr)
        return False


async def _watcher_task(job_id: str, job: dict) -> None:
    """Poll the job's STL; whenever it changes, convert to a live-preview GLB
    and push an event to the SSE stream. Lets the browser show each
    intermediate model as the autonomous loop iterates (Aider repair →
    re-execute → new STL). Stops when the subprocess exits or 'done' is sent.

    The autonomous loop writes a *new* file per iteration rather than
    overwriting in place:
      - first attempt:  temp_output_0.stl
      - aider retry:     temp_output_aider_autonomous_0.stl
      - plain retry:     temp_output_autonomous_0.stl
      - iteration N:     temp_output_N.stl
    So we glob all ``temp_output_*.stl`` and track the newest by mtime —
    a new file appearing OR an existing file's mtime changing both trigger
    a fresh live preview.
    """
    tempdir = job["tempdir"]
    proc = job["proc"]
    queue: asyncio.Queue = job["queue"]
    live_glb = tempdir / "temp_output_0.live.glb"
    last_sig: tuple[str, float] | None = None  # (path, mtime) of last converted STL
    while proc.returncode is None:
        if job.get("result") is not None:
            break  # reader task already emitted 'done'; stop pushing intermediates
        await asyncio.sleep(1.0)
        try:
            stls = sorted(
                tempdir.glob("temp_output_*.stl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not stls:
                continue
            stl_path = stls[0]
            mtime = stl_path.stat().st_mtime
            sig = (str(stl_path), mtime)
            if sig == last_sig:
                continue
            last_sig = sig
        except OSError:
            continue
        # Wait for the write to complete + size to stabilize (avoid half-written reads)
        await asyncio.sleep(1.0)
        try:
            s1 = stl_path.stat().st_size
            await asyncio.sleep(0.3)
            s2 = stl_path.stat().st_size
            if s1 != s2:
                continue
        except OSError:
            continue
        ok = await asyncio.to_thread(_stl_to_glb, str(stl_path), str(live_glb))
        if not ok:
            continue
        ts = int(time.time() * 1000)
        await queue.put({
            "intermediate": True,
            "glb": f"/api/jobs/{job_id}/files/live.glb?t={ts}",
        })


def _copy_artifacts(result: dict, dest: str) -> None:
    """Copy generated artifacts to the user-specified server-side path (req #2)."""
    try:
        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)
        for key in ("step", "stl", "glb", "py", "measurements", "missed"):
            src = result.get(key)
            if src:
                p = Path(src)
                if p.is_file():
                    shutil.copy2(p, dest_path / p.name)
    except Exception as exc:  # non-fatal — main result already captured
        print(f"[web] copy artifacts to {dest} failed: {exc}", file=sys.stderr)


def _harvest_intermediate(job: dict) -> dict:
    """Build a synthetic 'done' message from whatever files exist in tempdir.

    Called when the user cancels mid-run — the runner subprocess didn't emit
    a 'done' record, so we assemble one from disk state so the UI can show
    download buttons for the half-finished artifacts.
    """
    tempdir: Path = job["tempdir"]

    def _find(pattern: str) -> str | None:
        matches = sorted(
            tempdir.rglob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return str(matches[0]) if matches else None

    step = _find("temp_output_*.step")
    stl = _find("temp_output_*.stl")
    py = _find("temp_design_*.py")
    measurements = _find("temp_measurements_*.json")
    missed = _find("temp_missed_*.json")
    glb = _find("*.glb")
    # No GLB yet but STL exists — convert now so the browser preview works
    # for the cancelled-state artifacts.
    if not glb and stl:
        live_glb = tempdir / "temp_output_0.live.glb"
        if _stl_to_glb(stl, str(live_glb)):
            glb = str(live_glb)

    return {
        "done": True,
        "cancelled": True,
        "error_type": "CANCELLED_BY_USER",
        "step": step,
        "stl": stl,
        "glb": glb,
        "py": py,
        "measurements": measurements,
        "missed": missed,
        "tokens": None,
        "api_calls": None,
    }


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    if job_id not in _JOBS:
        raise HTTPException(404, "job not found")
    job = _JOBS[job_id]
    queue: asyncio.Queue = job["queue"]

    async def event_stream():
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("done") or msg.get("error"):
                # Drain any remaining (e.g. sentinel) without blocking.
                while not queue.empty():
                    extra = queue.get_nowait()
                    if extra is None:
                        break
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/result")
async def job_result(job_id: str) -> JSONResponse:
    if job_id not in _JOBS:
        raise HTTPException(404, "job not found")
    return JSONResponse(_JOBS[job_id].get("result") or {"status": "running"})


@app.get("/api/jobs/{job_id}/files/{name}")
async def job_file(job_id: str, name: str) -> FileResponse:
    if job_id not in _JOBS:
        raise HTTPException(404, "job not found")
    job = _JOBS[job_id]
    result = job.get("result") or {}
    tempdir = job.get("tempdir")

    def _find_in_tempdir(pattern: str) -> str | None:
        if not tempdir:
            return None
        matches = sorted(
            Path(tempdir).rglob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return str(matches[0]) if matches else None

    name_map = {
        "model.glb": result.get("glb") or _find_in_tempdir("*.glb"),
        "live.glb": str(tempdir / "temp_output_0.live.glb") if tempdir else None,
        "model.step": result.get("step") or _find_in_tempdir("temp_output_*.step"),
        "model.stl": result.get("stl") or _find_in_tempdir("temp_output_*.stl"),
        "source.py": result.get("py") or _find_in_tempdir("temp_design_*.py"),
        "measurements.json": result.get("measurements")
            or _find_in_tempdir("temp_measurements_*.json"),
        "missed.json": result.get("missed")
            or _find_in_tempdir("temp_missed_*.json"),
    }
    path = name_map.get(name)
    if not path or not Path(path).is_file():
        raise HTTPException(404, f"file '{name}' not available (yet)")
    media_types = {
        "model.glb": "model/gltf-binary",
        "live.glb": "model/gltf-binary",
        "model.step": "application/step",
        "model.stl": "model/stl",
        "source.py": "text/x-python",
        "measurements.json": "application/json",
        "missed.json": "application/json",
    }
    # `live.glb` is overwritten in-place by the watcher task as the autonomous
    # loop iterates. Without no-cache, browsers may serve a stale copy from
    # heuristic caching even when the URL's `?t=<ts>` query changes — the
    # model-viewer then renders the old scene, not the new one. Setting this
    # on every file is harmless (each job has a unique job_id path segment).
    headers = {"Cache-Control": "no-cache, must-revalidate"}
    return FileResponse(
        path,
        media_type=media_types.get(name, "application/octet-stream"),
        headers=headers,
    )


# Static frontend — mounted LAST so /api/* routes take precedence.
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> None:
    host = os.environ.get("MAC_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("MAC_WEB_PORT", "8000"))
    print(f"\n  MAC Web UI — http://{host}:{port}")
    print("  Single-user, trusted-network only. Generated .py runs server-side.")
    print(f"  Auto-cleanup: job tempdirs removed {_CLEANUP_AFTER_SECONDS // 3600}h after completion.\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
