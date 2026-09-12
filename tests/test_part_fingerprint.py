"""P0-2: PartSpec fingerprint + recompose invalidation.

Covers: the canonical fingerprint (all geometry-affecting fields, not just
description), _prune_part_results_for_brief (removed ids, changed specs,
template -> instance propagation, legacy results without a fingerprint),
and node_part_builder's reuse guard (ok AND not degraded AND fingerprint
match) with mocked generators -- no external LLM calls.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mac_assembly import nodes_assembly as nodes
from mac_assembly.schemas_assembly import (
    AssemblyBrief,
    BaseBodySpec,
    Feature,
    FeatureAttachment,
    PartResult,
    PartSpec,
    base_body_fingerprint,
    part_spec_fingerprint,
)


def _llm_part(part_id="block", description="a 10x10x10 mm cube", **kw):
    return PartSpec(part_id=part_id, part_name=part_id, description=description, **kw)


def _builder_part(part_id="plate", **params):
    return PartSpec(
        part_id=part_id, part_name=part_id, description="plate",
        builder={"name": "mounting_plate",
                 "params": {"width": 10, "depth": 10, "thickness": 2, **params}},
    )


def _v3_part(part_id="palm", desc="base", attach=(0, 0, 0), ear_length=20):
    return PartSpec(
        part_id=part_id, part_name=part_id, description="audit",
        base_body=BaseBodySpec(description=desc, key_dimensions={"w": 10}),
        features=[Feature(
            name="clevis_fork",
            params={"ear_length": ear_length, "ear_width": 16,
                    "tongue_thickness": 3, "bore_radius": 2.5},
            attachment=FeatureAttachment(
                attach_point_mm=list(attach), direction="+y"),
        )],
    )


def _brief(parts):
    return AssemblyBrief(
        assembly_name="t", parts=parts, user_request_raw="r",
        expected_part_count=len(parts),
    )


def _ok_result(part_id, fp="", **kw):
    return PartResult(part_id=part_id, ok=True, step_path=f"/tmp/{part_id}.step",
                      spec_fingerprint=fp, **kw)


class TestPartSpecFingerprint(unittest.TestCase):
    def test_stable_for_identical_specs(self):
        a, b = _builder_part(), _builder_part()
        self.assertEqual(part_spec_fingerprint(a), part_spec_fingerprint(b))

    def test_builder_params_change_fingerprint(self):
        self.assertNotEqual(
            part_spec_fingerprint(_builder_part(hole_radius=1.0)),
            part_spec_fingerprint(_builder_part(hole_radius=2.0)),
        )

    def test_description_change_fingerprint(self):
        self.assertNotEqual(
            part_spec_fingerprint(_llm_part(description="a")),
            part_spec_fingerprint(_llm_part(description="b")),
        )

    def test_feature_attachment_change_fingerprint(self):
        self.assertNotEqual(
            part_spec_fingerprint(_v3_part(attach=(1, 0, 0))),
            part_spec_fingerprint(_v3_part(attach=(2, 0, 0))),
        )

    def test_feature_params_change_fingerprint(self):
        self.assertNotEqual(
            part_spec_fingerprint(_v3_part(ear_length=20)),
            part_spec_fingerprint(_v3_part(ear_length=24)),
        )

    def test_generation_mode_change_fingerprint(self):
        same_desc = _llm_part(description="x")
        with_builder = PartSpec(
            part_id="block", part_name="block", description="x",
            builder={"name": "lid", "params": {"width": 1, "depth": 1, "thickness": 1}},
        )
        self.assertNotEqual(
            part_spec_fingerprint(same_desc), part_spec_fingerprint(with_builder)
        )

    def test_reuse_target_change_fingerprint(self):
        a = _llm_part(part_id="s2", reuses_part_id="s1")
        b = _llm_part(part_id="s2", reuses_part_id="s9")
        self.assertNotEqual(part_spec_fingerprint(a), part_spec_fingerprint(b))

    def test_base_body_fingerprint_tracks_key_dimensions(self):
        p1 = _v3_part(desc="base")
        p2 = p1.model_copy(update={
            "base_body": BaseBodySpec(description="base", key_dimensions={"w": 12})
        })
        self.assertEqual(base_body_fingerprint(p1), base_body_fingerprint(p1))
        self.assertNotEqual(base_body_fingerprint(p1), base_body_fingerprint(p2))
        # description-only fingerprint would have missed this:
        self.assertEqual(p1.base_body.description, p2.base_body.description)


class TestPrunePartResults(unittest.TestCase):
    def test_drops_parts_removed_from_brief(self):
        brief = _brief([_llm_part(part_id="a")])
        results = {
            "a": _ok_result("a", part_spec_fingerprint(_llm_part(part_id="a"))),
            "gone": _ok_result("gone", "x"),
        }
        pruned = nodes._prune_part_results_for_brief(brief, results)
        self.assertEqual(set(pruned), {"a"})

    def test_drops_changed_fingerprint(self):
        spec = _builder_part()
        brief = _brief([spec])
        results = {"plate": _ok_result("plate", "stale_fp")}
        pruned = nodes._prune_part_results_for_brief(brief, results)
        self.assertEqual(pruned, {})

    def test_keeps_matching_fingerprint(self):
        spec = _builder_part()
        brief = _brief([spec])
        results = {"plate": _ok_result("plate", part_spec_fingerprint(spec))}
        pruned = nodes._prune_part_results_for_brief(brief, results)
        self.assertIn("plate", pruned)

    def test_legacy_result_without_fingerprint_is_dropped(self):
        spec = _llm_part()
        brief = _brief([spec])
        results = {"block": _ok_result("block", "")}
        pruned = nodes._prune_part_results_for_brief(brief, results)
        self.assertEqual(pruned, {})

    def test_template_change_invalidates_instances(self):
        tmpl = _llm_part(part_id="screw", description="NEW screw")
        inst = _llm_part(part_id="screw_2", reuses_part_id="screw",
                         description="instance at corner")
        brief = _brief([tmpl, inst])
        results = {
            "screw": _ok_result("screw", "stale"),  # template changed
            "screw_2": _ok_result("screw_2", part_spec_fingerprint(inst)),
        }
        pruned = nodes._prune_part_results_for_brief(brief, results)
        self.assertEqual(pruned, {})

    def test_reuse_fingerprint_changes_when_template_spec_changes(self):
        old_tmpl = _llm_part(part_id="screw", description="old geometry")
        new_tmpl = _llm_part(part_id="screw", description="new geometry")
        inst = _llm_part(
            part_id="screw_2", reuses_part_id="screw", description="instance"
        )
        old_brief = _brief([old_tmpl, inst])
        new_brief = _brief([new_tmpl, inst])
        old_fps = nodes._effective_part_fingerprints(old_brief)
        new_fps = nodes._effective_part_fingerprints(new_brief)
        self.assertNotEqual(old_fps["screw_2"], new_fps["screw_2"])
        results = {
            "screw": _ok_result("screw", old_fps["screw"]),
            "screw_2": _ok_result("screw_2", old_fps["screw_2"]),
        }
        self.assertEqual(
            nodes._prune_part_results_for_brief(new_brief, results), {}
        )


class TestNodePartBuilderReuseGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _state(self, brief, part_results, **extra):
        state = {
            "assembly_brief": brief,
            "work_dir": str(self.work_dir),
            "part_results": part_results,
            "remodel_part_ids": [],
            "repair_context": "",
            "execution_log": [],
            "node_history": [],
        }
        state.update(extra)
        return state

    def test_identical_spec_is_reused_without_generator_call(self):
        spec = _builder_part()
        brief = _brief([spec])
        results = {"plate": _ok_result("plate", part_spec_fingerprint(spec))}
        with mock.patch.object(nodes, "run_part_builder") as m_builder:
            out = nodes.node_part_builder(self._state(brief, results))
        m_builder.assert_not_called()
        self.assertIn("plate", out["part_results"])

    def test_changed_builder_params_rebuilds(self):
        old_spec = _builder_part(hole_radius=1.0)
        new_spec = _builder_part(hole_radius=2.0)
        brief = _brief([new_spec])
        results = {"plate": _ok_result("plate", part_spec_fingerprint(old_spec))}
        fresh = _ok_result("plate", "")
        with mock.patch.object(nodes, "run_part_builder", return_value=fresh) as m:
            out = nodes.node_part_builder(self._state(brief, results))
        self.assertEqual(m.call_count, 1)
        stored = out["part_results"]["plate"]
        self.assertEqual(stored.spec_fingerprint, part_spec_fingerprint(new_spec))

    def test_changed_feature_attachment_rebuilds(self):
        old = _v3_part(attach=(1, 0, 0))
        new = _v3_part(attach=(2, 0, 0))
        brief = _brief([new])
        results = {"palm": _ok_result("palm", part_spec_fingerprint(old))}
        fresh = _ok_result("palm", "")
        with mock.patch.object(
            nodes, "run_part_with_features", return_value=fresh
        ) as m:
            out = nodes.node_part_builder(self._state(brief, results))
        self.assertEqual(m.call_count, 1)
        self.assertEqual(
            out["part_results"]["palm"].spec_fingerprint,
            part_spec_fingerprint(new),
        )

    def test_removed_part_id_is_dropped_from_state(self):
        spec = _llm_part(part_id="kept")
        brief = _brief([spec])
        results = {
            "kept": _ok_result("kept", part_spec_fingerprint(spec)),
            "removed": _ok_result("removed", "whatever"),
        }
        with mock.patch.object(nodes, "run_part"):
            out = nodes.node_part_builder(self._state(brief, results))
        self.assertNotIn("removed", out["part_results"])

    def test_degraded_result_is_not_reused(self):
        spec = _llm_part()
        brief = _brief([spec])
        results = {
            "block": _ok_result("block", part_spec_fingerprint(spec),
                                degraded=True, warnings=["base failed"]),
        }
        fresh = _ok_result("block", "")
        with mock.patch.object(nodes, "run_part", return_value=fresh) as m:
            out = nodes.node_part_builder(self._state(brief, results))
        self.assertEqual(m.call_count, 1)
        self.assertFalse(out["part_results"]["block"].degraded)

    def test_template_change_recopies_instances(self):
        tmpl = _llm_part(part_id="screw", description="NEW")
        inst = _llm_part(part_id="screw_2", reuses_part_id="screw")
        brief = _brief([tmpl, inst])
        results = {
            "screw": _ok_result("screw", "stale"),
            "screw_2": _ok_result("screw_2", "stale"),
        }
        new_tmpl = _ok_result("screw", "")
        new_inst = _ok_result("screw_2", "")
        with mock.patch.object(nodes, "run_part", return_value=new_tmpl) as m_run, \
             mock.patch.object(
                 nodes, "run_part_reuse", return_value=new_inst) as m_reuse:
            out = nodes.node_part_builder(self._state(brief, results))
        self.assertEqual(m_run.call_count, 1)  # template rebuilt once
        self.assertEqual(m_reuse.call_count, 1)  # instance re-copied
        # The instance must have received THIS round's template result
        # (the node stamps the fresh fingerprint onto it before storing,
        # so compare content, not identity).
        _, kwargs = m_reuse.call_args
        tmpl_res = kwargs["template_result"]
        self.assertEqual(tmpl_res.step_path, new_tmpl.step_path)
        self.assertEqual(tmpl_res.spec_fingerprint, part_spec_fingerprint(tmpl))
        self.assertEqual(
            out["part_results"]["screw_2"].spec_fingerprint,
            nodes._effective_part_fingerprints(brief)["screw_2"],
        )


if __name__ == "__main__":
    unittest.main()
