"""BUG-024: node_assembler used to write assembly_step_path and
assembly_stl_path to state as if they were valid even when the assembly
script failed (ok=False). Downstream _final_report printed them as
"Assembly: <path>" pointing at nonexistent files, and the handoff path
could pick up stale artifacts from a previous run. Fix: when ok=False,
set both artifact paths to "" (keep assembly_py_path + exec_error).

No external LLM calls; the assembly script + run_assembly_script are
monkeypatched.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mac_assembly.nodes_assembly import node_assembler
from mac_assembly.schemas_assembly import (
    Anchor,
    AnchorKind,
    AssemblyBrief,
    FaceQuery,
    InterfaceType,
    FunctionalInterface,
    MateSpec,
    MateType,
    MatingPlan,
    PartResult,
    PartSpec,
)


def _brief_plan_parts(work_dir: Path):
    brief = AssemblyBrief(
        assembly_name="t",
        parts=[
            PartSpec(part_id="a", part_name="a", description=""),
            PartSpec(part_id="b", part_name="b", description=""),
        ],
        interfaces=[
            FunctionalInterface(
                interface_id="itf", part_a="a", part_b="b",
                interface_type=InterfaceType.HINGE, description="",
            ),
        ],
        user_request_raw="r",
    )
    plan = MatingPlan(
        assembly_name="t",
        mates=[
            MateSpec(
                mate_id="m1", mate_type=MateType.FACE_TO_FACE,
                fixed_part_id="a", moving_part_id="b",
                fixed_anchor=Anchor(kind=AnchorKind.FACE, face="top"),
                moving_anchor=Anchor(kind=AnchorKind.FACE, face="bottom"),
                offset_mm=0.0,
            ),
        ],
    )
    part_results = {
        "a": PartResult(
            part_id="a", step_path=str(work_dir / "a.step"),
            stl_path="", py_path="", part_dir=str(work_dir),
            ok=True, attempts=1, token_usage={},
        ),
        "b": PartResult(
            part_id="b", step_path=str(work_dir / "b.step"),
            stl_path="", py_path="", part_dir=str(work_dir),
            ok=True, attempts=1, token_usage={},
        ),
    }
    return brief, plan, part_results


def _state(work_dir: Path, brief, plan, part_results):
    return {
        "work_dir": str(work_dir),
        "assembly_brief": brief,
        "mating_plan": plan,
        "part_results": part_results,
        "iteration_count": 0,
        "node_history": [],
        "execution_log": [],
    }


class TestAssemblerNoFakeArtifactPaths(unittest.TestCase):
    def test_failed_script_does_not_write_fake_step_stl_paths(self):
        from mac_assembly.assembly_qa import ReconcileResult
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            brief, plan, parts = _brief_plan_parts(work_dir)
            script_path = work_dir / "temp_assembly_0.py"
            script_path.write_text("# stub\n", encoding="utf-8")
            with mock.patch(
                "mac_assembly.nodes_assembly.write_assembly_script",
                return_value=script_path,
            ), mock.patch(
                "mac_assembly.nodes_assembly.run_assembly_script",
                return_value=(False, "Traceback: simulated crash"),
            ), mock.patch(
                # Bypass reconcile so we reach the actual assembly path.
                "mac_assembly.nodes_assembly.reconcile_dimensions",
                return_value=ReconcileResult([]),
            ), mock.patch(
                # Skip LLM repair (no external calls).
                "mac_assembly.nodes_assembly._llm_repair_assembly",
                return_value=None,
            ):
                out = node_assembler(_state(work_dir, brief, plan, parts))
        # Keep the script path (file exists) and exec error (diagnostic).
        self.assertNotEqual(out.get("assembly_py_path", ""), "")
        self.assertIn("simulated crash", out.get("assembly_exec_error", ""))
        # BUG-024: must NOT write fake STEP/STL paths -- files don't exist.
        self.assertEqual(
            out.get("assembly_step_path"), "",
            f"failed assembly must not write a fake STEP path, got: "
            f"{out.get('assembly_step_path')}",
        )
        self.assertEqual(
            out.get("assembly_stl_path"), "",
            f"failed assembly must not write a fake STL path, got: "
            f"{out.get('assembly_stl_path')}",
        )


if __name__ == "__main__":
    unittest.main()
