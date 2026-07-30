"""
Per-workflow token tracker for the multi-agent CAD pipeline.

When imported, this module installs two capture paths:

  1. ``openai.resources.chat.completions.Completions.create`` is patched.
     Captures usage from every **direct** ``client.chat.completions.create``
     call (the 4 LLM call sites in nodes.py).

  2. ``litellm.success_callback`` gets a callback appended.
     Captures usage from every **Aider / litellm** call. Aider calls
     ``litellm.completion(...)`` which internally uses
     ``openai_client.chat.completions.with_raw_response.create(...)`` — a
     different openai SDK method than #1, so #1 alone misses Aider. The
     litellm callback fires after the response is fully assembled (covers
     sync / async / streaming) with `.usage` populated.

Each captured call is attributed to a node by walking the call stack and
finding the nearest ``node_*`` function — so the per-module breakdown is
accurate even when the API call is buried in an Aider / helper.

Usage in graph.py::

    from multi_agent_cad.token_tracker import tracker
    tracker.reset()
    ... run workflow ...
    tracker.print_summary()
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import sys
import threading
import time
from collections import defaultdict

# Contextvar: set on entry to each node_* function (see _wrap_node in patch()).
# Required because litellm fires success_callback from inside an asyncio event
# loop, so inspect.stack() at callback time doesn't see the original sync caller.
# contextvars propagate through asyncio automatically, so the wrapper on node_*
# makes the current module visible inside the callback.
_current_module: contextvars.ContextVar[str] = contextvars.ContextVar(
    "multi_agent_cad_current_module", default="unknown",
)


class _Tracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._calls: list[dict] = []
        self._patched = False

    def reset(self) -> None:
        with self._lock:
            self._calls = []

    def _record(self, *, model, usage, module: str) -> None:
        if usage is None:
            return
        try:
            prompt = getattr(usage, "prompt_tokens", 0) or 0
            completion = getattr(usage, "completion_tokens", 0) or 0
            api_total = getattr(usage, "total_tokens", 0) or (prompt + completion)

            cache_read = 0
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                if hasattr(details, "model_dump"):
                    cache_read = (details.model_dump() or {}).get("cached_tokens", 0) or 0
                else:
                    cache_read = getattr(details, "cached_tokens", 0) or 0

            comp_details = getattr(usage, "completion_tokens_details", None)
            reasoning = 0
            if comp_details is not None:
                if hasattr(comp_details, "model_dump"):
                    reasoning = (comp_details.model_dump() or {}).get("reasoning_tokens", 0) or 0
                else:
                    reasoning = getattr(comp_details, "reasoning_tokens", 0) or 0
        except Exception:
            return

        # Bailian GLM non-standard behavior: completion_tokens is the VISIBLE
        # answer only, reasoning_tokens (thinking) is reported SEPARATELY and
        # is NOT a subset of completion_tokens. So actual billed output =
        # completion + reasoning. Standard OpenAI o1 reports reasoning as a
        # subset of completion — don't double-count there. Heuristic:
        # if reasoning > completion, Bailian-style (add them); else standard.
        if reasoning > 0 and reasoning > completion:
            billed_output = completion + reasoning
        else:
            billed_output = completion

        # Bailian's total_tokens also excludes reasoning — recompute.
        billed_total = prompt + cache_read + billed_output

        with self._lock:
            self._calls.append({
                "ts": time.time(),
                "module": module,
                "model": model,
                "input": prompt,
                "output_visible": completion,
                "output": billed_output,
                "cache_read": cache_read,
                "reasoning": reasoning,
                "total": billed_total,
                "api_total": api_total,
            })

    def summary(self) -> dict:
        with self._lock:
            calls = list(self._calls)
        per_module: dict = defaultdict(lambda: {
            "calls": 0, "input": 0, "output": 0,
            "cache_read": 0, "reasoning": 0, "total": 0,
        })
        for c in calls:
            m = per_module[c["module"]]
            m["calls"] += 1
            m["input"] += c["input"]
            m["output"] += c["output"]
            m["cache_read"] += c["cache_read"]
            m["reasoning"] += c["reasoning"]
            m["total"] += c["total"]
        return {
            "n_calls": len(calls),
            "total_input": sum(c["input"] for c in calls),
            "total_output": sum(c["output"] for c in calls),
            "total_cache_read": sum(c["cache_read"] for c in calls),
            "total_reasoning": sum(c["reasoning"] for c in calls),
            "total_tokens": sum(c["total"] for c in calls),
            "per_module": dict(per_module),
            "calls": calls,
        }

    def print_summary(self, file=None) -> None:
        out = file or sys.stdout
        s = self.summary()

        print("\n" + "=" * 78, file=out)
        print("TOKEN USAGE SUMMARY  (multi-agent CAD workflow)", file=out)
        print("=" * 78, file=out)
        print(f"  Total API calls : {s['n_calls']}", file=out)
        print(f"  Total tokens    : {s['total_tokens']:,}", file=out)
        print(f"    input         : {s['total_input']:,}", file=out)
        print(f"    output        : {s['total_output']:,}"
              f"  (visible + reasoning; reasoning subset: {s['total_reasoning']:,})",
              file=out)
        print(f"    cache_read    : {s['total_cache_read']:,}", file=out)
        print(file=out)
        print(f"  {'module':<40} {'calls':>6} {'input':>10} "
              f"{'output':>10} {'cache_r':>10} {'total':>12}",
              file=out)
        print("  " + "-" * 95, file=out)
        for name, m in sorted(s["per_module"].items()):
            print(f"  {name:<40} {m['calls']:>6} {m['input']:>10,} "
                  f"{m['output']:>10,} {m['cache_read']:>10,} {m['total']:>12,}",
                  file=out)
        print("  " + "-" * 95, file=out)
        print(f"  {'TOTAL':<40} {s['n_calls']:>6} {s['total_input']:>10,} "
              f"{s['total_output']:>10,} {s['total_cache_read']:>10,} "
              f"{s['total_tokens']:>12,}", file=out)
        print("=" * 78, file=out)

    def patch(self) -> None:
        if self._patched:
            return
        self._patched = True

        # 1) Patch openai.resources.chat.completions.Completions.create
        try:
            from openai.resources.chat.completions import Completions
            original = Completions.create

            def patched_create(self_, *args, **kwargs):
                resp = original(self_, *args, **kwargs)
                try:
                    model = (getattr(resp, "model", None)
                              or kwargs.get("model", "?"))
                    usage = getattr(resp, "usage", None)
                    module = _detect_module()
                    tracker._record(model=model, usage=usage, module=module)
                except Exception:
                    pass
                return resp

            Completions.create = patched_create  # type: ignore[assignment]
        except Exception as e:
            print(f"[token_tracker] failed to patch openai: {e}",
                  file=sys.stderr)

        # 2) Register litellm success_callback to capture Aider/litellm calls.
        # Why: Aider calls litellm.completion() which internally uses
        # openai_client.chat.completions.with_raw_response.create() — a
        # *different* openai SDK method than the one patched above. So the
        # Completions.create patch doesn't fire for Aider. litellm's
        # success_callback catches all litellm calls (sync / async / stream)
        # after the response is fully assembled, with usage populated.
        try:
            import litellm as _litellm

            def _litellm_success_callback(kwargs, completion_response,
                                          start_time, end_time):
                try:
                    if hasattr(completion_response, "usage"):
                        usage = completion_response.usage
                    elif isinstance(completion_response, dict):
                        usage = completion_response.get("usage")
                    else:
                        usage = None
                    if hasattr(completion_response, "model"):
                        model = completion_response.model
                    elif isinstance(completion_response, dict):
                        model = completion_response.get("model", "?")
                    else:
                        model = "?"
                    module = _detect_module()
                    tracker._record(model=model, usage=usage, module=module)
                except Exception:
                    pass

            if not any(getattr(cb, "__name__", "") == "_litellm_success_callback"
                       for cb in _litellm.success_callback):
                _litellm.success_callback.append(_litellm_success_callback)

            # litellm logs via a global ThreadPoolExecutor
            # (litellm.litellm_core_utils.thread_pool_executor.executor).
            # Worker threads don't inherit the caller's contextvars, so
            # _current_module reads as "unknown" inside success_callback.
            # Fix: swap that executor's class to a subclass that wraps every
            # submitted task with contextvars.copy_context().run(...) so the
            # caller's context propagates into the worker thread.
            import concurrent.futures.thread as _cft

            class _ContextAwareExecutor(_cft.ThreadPoolExecutor):
                def submit(self, fn, *args, **kwargs):
                    ctx = contextvars.copy_context()
                    def wrapped(*a, **kw):
                        return ctx.run(lambda: fn(*a, **kw))
                    return super().submit(wrapped, *args, **kwargs)

            try:
                import litellm.litellm_core_utils.thread_pool_executor as _lt_pool
                if not isinstance(_lt_pool.executor, _ContextAwareExecutor):
                    _lt_pool.executor.__class__ = _ContextAwareExecutor
            except (ImportError, TypeError):
                pass
        except ImportError:
            pass  # litellm not installed; direct calls still tracked

        # 3) Wrap each node_* function in multi_agent_cad.nodes to set
        # _current_module contextvar on entry. Required for litellm's
        # success_callback (which fires from an async event loop) to know
        # which node triggered the Aider/litellm call. Without this,
        # _detect_module's stack walk returns 'unknown' for Aider calls.
        try:
            import multi_agent_cad.nodes as _nodes
            for _name in dir(_nodes):
                if not _name.startswith("node_"):
                    continue
                _fn = getattr(_nodes, _name, None)
                if (not callable(_fn)
                        or getattr(_fn, "_token_tracker_wrapped", False)):
                    continue
                setattr(_nodes, _name, _wrap_node(_fn))
        except ImportError:
            pass  # nodes module not available (e.g. unit testing token_tracker alone)


def _detect_module() -> str:
    """Find which node_* triggered this API call.

    Primary: read from ``_current_module`` contextvar (set by the node_*
    wrappers installed in :meth:`_Tracker.patch`). This propagates through
    asyncio, so it works inside litellm's success_callback which fires from
    an async event loop and would not see the sync caller on the stack.

    Fallback: walk the call stack for a ``node_*`` frame — works for
    direct sync openai SDK calls (nodes.py's 4 direct LLM call sites).
    """
    mod = _current_module.get()
    if mod != "unknown":
        return mod
    for frame_info in inspect.stack():
        fn = frame_info.function
        if fn.startswith("node_"):
            return fn
    return "unknown"


def _wrap_node(fn):
    """Decorator: set ``_current_module`` to ``fn.__name__`` for the call."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        token = _current_module.set(fn.__name__)
        try:
            return fn(*args, **kwargs)
        finally:
            _current_module.reset(token)
    wrapper._token_tracker_wrapped = True  # type: ignore[attr-defined]
    return wrapper


tracker = _Tracker()
tracker.patch()
