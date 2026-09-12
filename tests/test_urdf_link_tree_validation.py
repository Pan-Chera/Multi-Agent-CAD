"""BUG-005: _build_link_tree used set difference to find a single root,
but sets silently collapse duplicates -- a diamond DAG (A->B, A->C, B->D,
C->D) has len(roots)=1 yet D has two parents, which URDF does not support.
The check must detect multi-parent children, cycles, and unreachable nodes.

No external LLM calls; pure data-driven tests on _build_link_tree.
"""
from __future__ import annotations

import unittest

from mac_assembly.urdf_export import _build_link_tree


def _mate(fixed: str, moving: str) -> dict:
    return {
        "fixed_endpoint": {"part": fixed},
        "moving_endpoint": {"part": moving},
    }


class TestBuildLinkTree(unittest.TestCase):
    def test_linear_chain_is_valid(self):
        mates = [_mate("base", "mid"), _mate("mid", "top")]
        root, ok = _build_link_tree(mates)
        self.assertEqual(root, "base")
        self.assertTrue(ok)

    def test_branching_tree_is_valid(self):
        # base -> {left, right}, both children of base only.
        mates = [_mate("base", "left"), _mate("base", "right")]
        root, ok = _build_link_tree(mates)
        self.assertEqual(root, "base")
        self.assertTrue(ok)

    def test_diamond_dag_rejected(self):
        # A -> B, A -> C, B -> D, C -> D -- D has two parents (URDF invalid).
        mates = [
            _mate("a", "b"),
            _mate("a", "c"),
            _mate("b", "d"),
            _mate("c", "d"),
        ]
        root, ok = _build_link_tree(mates)
        self.assertFalse(ok, "diamond DAG must be rejected (multi-parent D)")
        # root may be reported or None -- the validity flag is what matters.

    def test_cycle_rejected(self):
        # A -> B -> A: cycle, no root (each is both parent and child).
        mates = [_mate("a", "b"), _mate("b", "a")]
        root, ok = _build_link_tree(mates)
        self.assertFalse(ok, "cycle must be rejected")

    def test_multiple_roots_rejected(self):
        # Two separate trees: A->B and C->D. Two roots.
        mates = [_mate("a", "b"), _mate("c", "d")]
        root, ok = _build_link_tree(mates)
        self.assertFalse(ok, "multiple roots must be rejected")

    def test_unreachable_node_rejected(self):
        # A -> B is the tree, C is a free-floating mate target with no
        # incoming parent: A is root but C never appears as a child of
        # anyone in a valid tree path from root.
        mates = [_mate("a", "b"), _mate("c", "c")]  # c->c is a self-loop
        root, ok = _build_link_tree(mates)
        # Self-loop on c makes c both a parent and a child -> not in roots.
        # The structure is invalid either way.
        self.assertFalse(ok, "self-loop / unreachable nodes must be rejected")


if __name__ == "__main__":
    unittest.main()
