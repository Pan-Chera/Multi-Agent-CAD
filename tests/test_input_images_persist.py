"""Fix 5: _input_images_dir persists CWD images into the job directory.

The CWD fallback used to be a permanent dependency: a resume launched
from another directory silently loaded an empty image dir (stale
Decomposer fingerprint, starved Judge evidence). Now the first caller
copies the images into <work_dir>/user_input_images/ (copy2 keeps
mtime_ns, so _image_fingerprint is stable across the copy) and every
later caller resolves the job-local dir regardless of CWD.

No external LLM calls; the filesystem is the only dependency.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mac_assembly import nodes_assembly as nodes


class TestInputImagesPersist(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = root / "job"
        self.work_dir.mkdir()
        self.cwd_images = root / "cwd" / "user_input_images"
        self.cwd_images.mkdir(parents=True)
        img = self.cwd_images / "a.png"
        img.write_bytes(b"png-bytes")
        # Deterministic, non-now mtime so copy2 preservation is testable.
        os.utime(img, (1_700_000_000, 1_700_000_000))
        self._cwd_patch = mock.patch.object(
            Path, "cwd", return_value=root / "cwd"
        )
        self._cwd_patch.start()
        self.addCleanup(self._cwd_patch.stop)

    def test_cwd_images_are_copied_into_job_dir(self):
        out = nodes._input_images_dir(self.work_dir)
        self.assertEqual(out, self.work_dir / "user_input_images")
        copied = out / "a.png"
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_bytes(), b"png-bytes")
        # copy2 preserves mtime_ns -> the Decomposer cache fingerprint is
        # identical before and after persistence (no cache invalidation).
        self.assertEqual(
            nodes._image_fingerprint(out),
            nodes._image_fingerprint(self.cwd_images),
        )

    def test_second_call_resolves_job_dir_without_cwd(self):
        nodes._input_images_dir(self.work_dir)  # first call persists
        # Simulate a resume launched from a different, image-less CWD.
        with tempfile.TemporaryDirectory() as other:
            with mock.patch.object(Path, "cwd", return_value=Path(other)):
                out = nodes._input_images_dir(self.work_dir)
        self.assertEqual(out, self.work_dir / "user_input_images")
        self.assertTrue((out / "a.png").is_file())

    def test_existing_job_dir_wins_and_is_not_overwritten(self):
        wd_images = self.work_dir / "user_input_images"
        wd_images.mkdir()
        (wd_images / "b.png").write_bytes(b"job-local")
        out = nodes._input_images_dir(self.work_dir)
        self.assertEqual(out, wd_images)
        self.assertTrue((wd_images / "b.png").is_file())
        self.assertFalse((wd_images / "a.png").exists())  # no merge

    def test_missing_everywhere_returns_cwd_path(self):
        empty = self.work_dir.parent / "empty_cwd"
        empty.mkdir()
        with mock.patch.object(Path, "cwd", return_value=empty):
            out = nodes._input_images_dir(self.work_dir)
        self.assertEqual(out, empty / "user_input_images")
        self.assertFalse((self.work_dir / "user_input_images").exists())

    def test_empty_cwd_dir_is_not_persisted(self):
        for f in self.cwd_images.iterdir():
            f.unlink()
        out = nodes._input_images_dir(self.work_dir)
        self.assertEqual(out, self.cwd_images)  # falls through, no copy
        self.assertFalse((self.work_dir / "user_input_images").exists())

    def test_empty_job_dir_does_not_shadow_cwd_bootstrap(self):
        # A pre-existing EMPTY user_input_images/ inside the job dir
        # (e.g. left behind by a failed bootstrap) must not permanently
        # hide the CWD source -- the next call bootstraps into it.
        (self.work_dir / "user_input_images").mkdir()
        out = nodes._input_images_dir(self.work_dir)
        self.assertEqual(out, self.work_dir / "user_input_images")
        self.assertTrue((out / "a.png").is_file())

    def test_job_dir_with_only_non_image_files_does_not_shadow(self):
        # .DS_Store-style noise is not "valid images": the CWD bootstrap
        # still runs (and afterwards the populated job dir wins).
        wd_images = self.work_dir / "user_input_images"
        wd_images.mkdir()
        (wd_images / ".DS_Store").write_bytes(b"junk")
        out = nodes._input_images_dir(self.work_dir)
        self.assertEqual(out, wd_images)
        self.assertTrue((out / "a.png").is_file())


if __name__ == "__main__":
    unittest.main()
