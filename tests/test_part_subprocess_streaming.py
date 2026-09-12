"""P1-1: part-subprocess output is streamed live (echo + log) and every
attempt's log is preserved; timeouts kill the whole process tree.

Uses short-lived real python subprocesses (no LLM calls).
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


class TestStreamSubprocess(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mac_stream_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _child(self, body: str) -> Path:
        child = self.tmp / f"child_{len(list(self.tmp.iterdir()))}.py"
        child.write_text(body, encoding="utf-8")
        return child

    def test_output_visible_and_logged_before_child_exit(self):
        """The line must be echoed AND in the log file while the child is
        still alive (marker file exists during its sleep)."""
        from mac_assembly.part_generator import _stream_subprocess

        marker = self.tmp / "marker.txt"
        child = self._child(
            "import time\n"
            "print('STREAMED_LINE_1', flush=True)\n"
            f"open({str(marker)!r}, 'w').write('alive')\n"
            "time.sleep(6)\n"
            "print('AFTER_SLEEP', flush=True)\n"
        )
        log = self.tmp / "part_log_attempt_01.txt"
        holder = {}

        def run():
            rc, out, to = _stream_subprocess(
                [sys.executable, "-u", str(child)],
                cwd=self.tmp,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                timeout=30,
                log_path=log,
            )
            holder.update(rc=rc, out=out, to=to)

        buf = io.StringIO()
        thread = threading.Thread(target=run)
        with contextlib.redirect_stdout(buf):
            thread.start()
            deadline = time.time() + 10
            seen = False
            while time.time() < deadline:
                if (
                    "STREAMED_LINE_1" in buf.getvalue()
                    and log.is_file()
                    and "STREAMED_LINE_1" in log.read_text(encoding="utf-8")
                    and marker.is_file()
                ):
                    seen = True
                    break
                time.sleep(0.05)
            thread.join(timeout=30)
        self.assertTrue(seen, "output must be echoed+logged while child runs")
        self.assertEqual(holder.get("rc"), 0)
        self.assertFalse(holder.get("to"))
        self.assertIn("STREAMED_LINE_1", holder.get("out", ""))

    def test_timeout_kills_process_tree_no_orphans(self):
        """A timed-out child AND its grandchild must both die (the child
        runs in its own process group; killpg reaches descendants that a
        bare proc.kill() would orphan)."""
        from mac_assembly.part_generator import _stream_subprocess

        marker = "macgrandchild" + str(int(time.time() * 1000))
        child = self._child(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', "
            f"'import time; time.sleep(60)  # {marker}'])\n"
            "print('spawning done', flush=True)\n"
            "time.sleep(60)\n"
        )
        rc, out, to = _stream_subprocess(
            [sys.executable, "-u", str(child)],
            cwd=self.tmp,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout=2,
            log_path=self.tmp / "part_log_attempt_01.txt",
        )
        self.assertTrue(to)
        self.assertIsNotNone(rc)
        self.assertLess(rc, 0)  # killed by signal, not a clean exit
        # grandchild must be gone (pgrep: rc=0 means still running)
        pgrep = shutil.which("pgrep")
        if pgrep is None:  # pragma: no cover - POSIX-only check
            self.skipTest("pgrep not available")
        deadline = time.time() + 5
        pgrep_rc = 0
        while time.time() < deadline:
            res = subprocess.run(
                [pgrep, "-f", marker], capture_output=True, text=True
            )
            pgrep_rc = res.returncode
            if pgrep_rc != 0:
                break
            time.sleep(0.2)
        self.assertNotEqual(
            pgrep_rc, 0, "grandchild was orphaned by timeout"
        )

    def test_returncode_and_tail_extraction(self):
        from mac_assembly.part_generator import _stream_subprocess, _tail_error

        child = self._child(
            "print('line one', flush=True)\n"
            "print('LAST_LINE', flush=True)\n"
            "import sys; sys.exit(3)\n"
        )
        rc, out, to = _stream_subprocess(
            [sys.executable, "-u", str(child)],
            cwd=self.tmp,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout=30,
            log_path=self.tmp / "part_log_attempt_01.txt",
        )
        self.assertEqual(rc, 3)
        self.assertFalse(to)
        err = _tail_error(out, 3)
        self.assertIn("returncode=3", err)
        self.assertIn("LAST_LINE", err)

    def test_streamed_echo_and_log_redact_api_key(self):
        """If a child ever prints the key, the echo and the log file both
        see <redacted>, never the value."""
        from mac_assembly import part_generator as pg

        secret = "sk-STREAMTEST-12345678"
        child = self._child(
            f"print('key={secret}', flush=True)\n"
        )
        log = self.tmp / "part_log_attempt_01.txt"
        buf = io.StringIO()
        with mock.patch.object(pg, "_API_KEY_SNAPSHOT", secret), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DASHSCOPE_API_KEY", None)
            env = pg._make_subprocess_env()
            self.assertEqual(env["DASHSCOPE_API_KEY"], secret)  # injected
            with contextlib.redirect_stdout(buf):
                rc, out, to = pg._stream_subprocess(
                    [sys.executable, "-u", str(child)],
                    cwd=self.tmp,
                    env=env,
                    timeout=30,
                    log_path=log,
                )
        self.assertEqual(rc, 0)
        echoed = buf.getvalue()
        logged = log.read_text(encoding="utf-8")
        # redaction must cover echo, log file, AND the returned combined
        # output -- the latter feeds _tail_error -> PartResult.error -> QA
        # report / handoff manifest / final report, so the raw key value must
        # never survive the pump (error classification matches phrases, not
        # key values, so it is unaffected)
        for text in (echoed, logged, out):
            self.assertNotIn(secret, text)
            self.assertIn("<redacted>", text)


class TestAttemptLogNaming(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mac_logs_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_attempt_logs_never_overwrite(self):
        from mac_assembly.part_generator import (
            _next_attempt_log,
            _promote_latest_log,
        )

        a1 = _next_attempt_log(self.tmp, "part_log_")
        self.assertEqual(a1.name, "part_log_attempt_01.txt")
        a1.write_text("first attempt", encoding="utf-8")

        a2 = _next_attempt_log(self.tmp, "part_log_")
        self.assertEqual(a2.name, "part_log_attempt_02.txt")
        a2.write_text("second attempt", encoding="utf-8")

        # legacy pointer = copy of the newest; history preserved
        _promote_latest_log(a2, self.tmp / "part_log.txt")
        self.assertEqual(
            (self.tmp / "part_log.txt").read_text(encoding="utf-8"),
            "second attempt",
        )
        self.assertEqual(
            a1.read_text(encoding="utf-8"), "first attempt"
        )

    def test_remodel_log_prefix_is_separate_sequence(self):
        from mac_assembly.part_generator import _next_attempt_log

        (self.tmp / "part_log_attempt_01.txt").write_text("x", encoding="utf-8")
        r1 = _next_attempt_log(self.tmp, "part_remodel_log_")
        self.assertEqual(r1.name, "part_remodel_log_attempt_01.txt")


if __name__ == "__main__":
    unittest.main()
