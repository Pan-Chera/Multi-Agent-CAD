"""P1-3: token usage must accumulate ACROSS attempts, never be lost when a
later (cheap/failed) PartResult overwrites an earlier one.

Covers:
- 100-token first attempt + 0-token second attempt -> report 100
- 100-token first attempt + 40-token second attempt -> report 140
- re-reading the same summary snapshot does not double count
- a timed-out attempt (no fresh summary on disk) keeps the ledger intact
- a stale summary predating the attempt (mtime guard) is not re-counted
- builder-only / no-LLM paths contribute nothing
- the final end-to-end report reads the cumulative ledger, not the last
  PartResult's overwritten token_usage
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from mac_assembly.part_generator import (
    _archive_attempt_tokens,
    _seed_cumulative_tokens,
    load_cumulative_token_usage,
)


class TestTokenArchiving(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mac_tokens_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.part_dir = self.tmp / "parts" / "p1"
        self.part_dir.mkdir(parents=True)

    def _write_summary(self, data: dict) -> None:
        (self.part_dir / "token_summary.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_failed_second_attempt_does_not_lose_first(self):
        """The original bug: chassis_body's 101k-token first attempt was
        overwritten by a keyless fast failure that reported 0."""
        started = time.time()
        self._write_summary(
            {"n_calls": 5, "total_tokens": 100, "total_input": 60,
             "total_output": 40}
        )
        cum = _archive_attempt_tokens(self.part_dir, started)
        self.assertEqual(cum["total_tokens"], 100)

        # second attempt: fast failure, zero tokens, overwrites the file
        started2 = time.time()
        self._write_summary({"n_calls": 0, "total_tokens": 0})
        cum2 = _archive_attempt_tokens(self.part_dir, started2)
        self.assertEqual(cum2["total_tokens"], 100)
        self.assertEqual(load_cumulative_token_usage(self.part_dir)["n_calls"], 5)

    def test_usage_sums_across_attempts(self):
        started = time.time()
        self._write_summary({"n_calls": 2, "total_tokens": 100})
        _archive_attempt_tokens(self.part_dir, started)

        started2 = time.time()
        self._write_summary({"n_calls": 1, "total_tokens": 40})
        cum = _archive_attempt_tokens(self.part_dir, started2)
        self.assertEqual(cum["total_tokens"], 140)
        self.assertEqual(cum["n_calls"], 3)

    def test_repeated_read_does_not_double_count(self):
        started = time.time()
        self._write_summary({"n_calls": 1, "total_tokens": 100})
        _archive_attempt_tokens(self.part_dir, started)
        # the snapshot was moved away; re-archiving adds nothing
        cum = _archive_attempt_tokens(self.part_dir, started)
        self.assertEqual(cum["total_tokens"], 100)

    def test_timeout_attempt_keeps_already_persisted_tokens(self):
        """A SIGKILLed attempt leaves no fresh summary: the ledger must
        keep the earlier attempts' spend (tokens already on disk)."""
        started = time.time()
        self._write_summary({"n_calls": 1, "total_tokens": 100})
        _archive_attempt_tokens(self.part_dir, started)

        # attempt 2 times out hard: no token_summary.json written at all
        started2 = time.time()
        cum = _archive_attempt_tokens(self.part_dir, started2)
        self.assertEqual(cum["total_tokens"], 100)

    def test_stale_summary_not_recounted(self):
        """A summary predating the current attempt (subprocess died
        before its finally could write) must not be counted twice."""
        started = time.time()
        self._write_summary({"n_calls": 1, "total_tokens": 100})
        _archive_attempt_tokens(self.part_dir, started)

        # simulate a rename failure leaving the old file behind with an
        # old mtime, then a new attempt archives again
        old = time.time() - 3600
        self._write_summary({"n_calls": 1, "total_tokens": 100})
        stale = self.part_dir / "token_summary.json"
        os.utime(stale, (old, old))
        cum = _archive_attempt_tokens(self.part_dir, time.time())
        self.assertEqual(cum["total_tokens"], 100)

    def test_seed_folds_preexisting_summary_of_resumed_job(self):
        (self.part_dir / "token_summary.json").write_text(
            json.dumps({"n_calls": 4, "total_tokens": 101276}), encoding="utf-8"
        )
        _seed_cumulative_tokens(self.part_dir)
        self.assertEqual(
            load_cumulative_token_usage(self.part_dir)["total_tokens"], 101276
        )
        # a new attempt adds on top of the resumed ledger
        started = time.time()
        self._write_summary({"n_calls": 1, "total_tokens": 50})
        cum = _archive_attempt_tokens(self.part_dir, started)
        self.assertEqual(cum["total_tokens"], 101326)

    def test_no_seed_once_ledger_exists(self):
        _seed_cumulative_tokens(self.part_dir)  # no summary -> no-op
        self.assertEqual(load_cumulative_token_usage(self.part_dir), {})
        (self.part_dir / "token_summary_cumulative.json").write_text(
            json.dumps({"total_tokens": 10}), encoding="utf-8"
        )
        (self.part_dir / "token_summary.json").write_text(
            json.dumps({"total_tokens": 999}), encoding="utf-8"
        )
        _seed_cumulative_tokens(self.part_dir)  # ledger exists -> no-op
        self.assertEqual(load_cumulative_token_usage(self.part_dir)["total_tokens"], 10)

    def test_builder_only_paths_contribute_zero(self):
        """A part dir with no summary and no ledger reports nothing."""
        self.assertEqual(load_cumulative_token_usage(self.part_dir), {})
        cum = _archive_attempt_tokens(self.part_dir, time.time())
        self.assertEqual(cum, {})

    def test_malformed_summary_is_skipped(self):
        (self.part_dir / "token_summary.json").write_text(
            "not json at all", encoding="utf-8"
        )
        cum = _archive_attempt_tokens(self.part_dir, time.time())
        self.assertEqual(cum, {})
        # the malformed snapshot was still archived for audit
        snapshots = list(self.part_dir.glob("token_summary_attempt_*.json"))
        self.assertEqual(len(snapshots), 1)


class TestEndToEndReport(unittest.TestCase):
    def test_report_reads_cumulative_not_last_result(self):
        """Final aggregation must read cumulative_part_token_usage even
        when the (overwritten) last PartResult carries zeros."""
        from mac_assembly.graph_assembly import _print_end_to_end_tokens
        from mac_assembly.schemas_assembly import PartResult

        state = {
            "part_results": {
                "chassis_body": PartResult(
                    part_id="chassis_body", ok=False, token_usage={}
                ),
            },
            "cumulative_part_token_usage": {
                "chassis_body": {
                    "n_calls": 12, "total_tokens": 101276,
                    "total_input": 61234, "total_output": 40042,
                },
                "dropped_part": {  # removed by a recompose, spend survives
                    "n_calls": 1, "total_tokens": 5000,
                    "total_input": 3000, "total_output": 2000,
                },
            },
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_end_to_end_tokens(state)
        out = buf.getvalue()
        self.assertIn("13 calls", out)  # 12 + 1 (dropped part's calls kept)
        self.assertIn("106,276", out)  # 101276 + 5000, thousands-separated

    def test_report_falls_back_to_last_result_without_ledger(self):
        from mac_assembly.graph_assembly import _print_end_to_end_tokens
        from mac_assembly.schemas_assembly import PartResult

        state = {
            "part_results": {
                "p": PartResult(
                    part_id="p", ok=True,
                    token_usage={"n_calls": 1, "total_tokens": 7,
                                 "total_input": 4, "total_output": 3},
                ),
            },
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_end_to_end_tokens(state)
        self.assertIn("7", buf.getvalue())


class TestBuilderRemodelTokenAttribution(unittest.TestCase):
    """The builder-param LLM adjustment runs IN THE PARENT process, so its
    spend is already counted by the parent tracker. It must NOT also be
    folded into the part's cumulative ledger -- the end-to-end report sums
    parent tracker + per-part ledgers, so merging would double-count."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mac_br_tokens_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.parts_root = self.tmp / "parts"
        self.parts_root.mkdir(parents=True)

    def _spec(self):
        from mac_assembly.schemas_assembly import PartSpec

        return PartSpec(
            part_id="pb", part_name="PB", description="d",
            # y_axis_truss_clevis_link: real builder, no required params
            builder={"name": "y_axis_truss_clevis_link", "params": {}},
        )

    def test_parent_call_not_folded_into_part_ledger(self):
        from unittest import mock

        from mac_assembly import part_generator as pg

        spec = self._spec()
        fake_result = pg.PartResult(part_id="pb", ok=True)
        with mock.patch(
            "mac_assembly.llm_utils.call_llm_json",
            return_value={"name": "y_axis_truss_clevis_link", "params": {}},
        ), mock.patch.object(
            pg, "run_part_builder", return_value=fake_result,
        ) as rb:
            result = pg.run_part_builder_remodel(spec, self.parts_root, "fb")
        self.assertTrue(result.ok)
        # no part ledger file written -- the parent tracker alone accounts
        # for the in-process param-adjust call
        self.assertFalse(
            (self.parts_root / "pb" / "token_summary_cumulative.json").exists()
        )
        self.assertEqual(result.token_usage, {})
        # the adjusted params were re-applied to the same builder
        self.assertEqual(
            rb.call_args[0][0].builder["name"], "y_axis_truss_clevis_link"
        )

    def test_crashed_llm_call_writes_no_part_ledger(self):
        from unittest import mock

        from mac_assembly import part_generator as pg

        spec = self._spec()
        with mock.patch(
            "mac_assembly.llm_utils.call_llm_json",
            side_effect=RuntimeError("connection reset"),
        ):
            result = pg.run_part_builder_remodel(spec, self.parts_root, "fb")
        self.assertFalse(result.ok)
        self.assertIn("LLM param-adjust call failed", result.error)
        # spend of the crashed call lives in the parent tracker; the part
        # ledger must stay empty so the final report does not double-count
        self.assertFalse(
            (self.parts_root / "pb" / "token_summary_cumulative.json").exists()
        )
        self.assertEqual(result.token_usage, {})


class TestSweepReadsDiskLedger(unittest.TestCase):
    """_keep_best may keep an OLDER success whose token_usage predates newer
    failed attempts -- the part_builder sweep must read the ledger FILE, not
    trust PartResult.token_usage."""

    def test_stale_partresult_cannot_revert_cumulative_state(self):
        from unittest import mock

        from mac_assembly import nodes_assembly as na
        from mac_assembly.schemas_assembly import PartSpec, PartResult

        tmp = Path(tempfile.mkdtemp(prefix="mac_sweep_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        parts_root = tmp / "parts"
        part_dir = parts_root / "p1"
        part_dir.mkdir(parents=True)
        (part_dir / "token_summary_cumulative.json").write_text(
            json.dumps({"n_calls": 3, "total_tokens": 300}), encoding="utf-8"
        )

        spec = PartSpec(
            part_id="p1", part_name="P1", description="d",
            builder={"name": "y_axis_truss_clevis_link", "params": {}},
        )
        # new attempt FAILED; _keep_best resurrects the old success whose
        # token_usage is stale (100 < the on-disk 300)
        stale_result = PartResult(
            part_id="p1", ok=True,
            token_usage={"n_calls": 1, "total_tokens": 100},
        )
        with mock.patch.object(
            na, "run_part_builder", return_value=stale_result,
        ):
            update = na.node_part_builder({
                "work_dir": str(tmp),
                "assembly_brief": mock.Mock(parts=[spec]),
                "part_specs": [spec],
                "part_results": {},
                "cumulative_part_token_usage": {},
                "execution_log": [],
                "node_history": [],
                "state": {},
            })
        self.assertEqual(
            update["cumulative_part_token_usage"]["p1"]["total_tokens"], 300
        )


if __name__ == "__main__":
    unittest.main()
