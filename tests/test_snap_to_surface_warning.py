"""BUG-039: _snap_to_surface used to swallow STL-export / mesh-load / empty-
mesh failures silently -- returned (original_attach_point, None). The caller
had no signal that the snap was skipped, so the next failure (disjoint
detection) raised a confusing error that didn't point back to the snap
root cause. Fix: keep the original attach point (do not block the feature)
but return a non-None warning string describing the failure.

No external LLM calls; trimesh / export_stl are monkeypatched.
"""
from __future__ import annotations

import unittest
from unittest import mock

from build123d import Box

from mac_assembly.feature_operators import _snap_to_surface


class TestSnapToSurfaceWarning(unittest.TestCase):
    def _base(self):
        return Box(10, 10, 10)

    def test_stl_export_failure_returns_warning(self):
        """When export_stl raises, the snap must still return the
        original attach point AND a non-None warning."""
        with mock.patch(
            "build123d.export_stl",
            side_effect=RuntimeError("simulated STL export crash"),
        ):
            point, warning = _snap_to_surface(
                self._base(), [0, 0, 11], "+z", surface_axis=None
            )
        self.assertEqual(point, [0, 0, 11])
        self.assertIsNotNone(warning)
        self.assertIn("snap skipped", warning.lower())
        self.assertIn("STL", warning)

    def test_trimesh_unavailable_returns_warning(self):
        """When trimesh/numpy import fails, the snap must return a
        warning (not silent None)."""
        with mock.patch(
            "builtins.__import__",
            side_effect=ImportError("trimesh not installed"),
        ):
            point, warning = _snap_to_surface(
                self._base(), [0, 0, 11], "+z", surface_axis=None
            )
        self.assertEqual(point, [0, 0, 11])
        # When import fails the function returns early; either way, the
        # warning must be set (not silent None).
        self.assertIsNotNone(
            warning,
            "trimesh import failure must surface a warning, not silent None",
        )


if __name__ == "__main__":
    unittest.main()
