import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import diskrip as dr  # noqa: E402
import library  # noqa: E402

GiB = 1 << 30


class Scan:
    def __init__(self, root):
        self.root = Path(root)
        self.groups, self.files = [], {}


class SpaceTests(unittest.TestCase):
    """The rips and Movies folders live on one temp filesystem here, so
    needs_copy() is patched to behave as if they were different pools."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        for d in ("rips/Disc", "Movies", "work"):
            (self.tmp / d).mkdir(parents=True)
        self.src = self.tmp / "rips/Disc/title_t00.mkv"
        self.src.write_bytes(b"x" * 4096)
        self.target = self.tmp / "Movies/Movie (2000)/Movie (2000) - 1080p.mkv"
        self.cfg = {"movie_root": str(self.tmp / "Movies"),
                    "rips_root": str(self.tmp / "rips"), "work_dir": str(self.tmp / "work")}
        self.item = {"file": "f", "group": "g", "src": str(self.src), "role": "main",
                     "category": "Other", "name": "x", "target": str(self.target),
                     "size": 4096, "status": "move"}
        patcher = mock.patch.object(library, "needs_copy", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_check_space_counts_copies_per_filesystem(self):
        with mock.patch.object(library, "free_bytes", return_value=GiB + 10_000):
            library.check_space([(self.src, self.target, 4096)])          # fits
            with self.assertRaises(dr.DiskRipError) as cm:
                library.check_space([(self.src, self.target, 20_000)])
        self.assertIn("Not enough free space", str(cm.exception))

    def test_renames_need_no_space(self):
        with mock.patch.object(library, "needs_copy", return_value=False), \
                mock.patch.object(library, "free_bytes", return_value=0):
            library.check_space([(self.src, self.target, 10 * GiB)])

    def test_apply_refuses_before_moving_anything(self):
        with mock.patch.object(library, "free_bytes", return_value=GiB):
            with self.assertRaises(dr.DiskRipError):
                library.apply_plan([self.item], self.cfg, Scan(self.tmp / "rips"))
        self.assertTrue(self.src.exists())
        self.assertFalse(self.target.parent.exists())
        self.assertFalse((self.tmp / "work/library-logs").exists())

    def test_undo_refuses_when_the_way_back_is_full(self):
        """The NAS case: undo copies a movie back to a pool with no room."""
        self.target.parent.mkdir()
        os.replace(self.src, self.target)
        log = self.tmp / "work/log.json"
        log.write_text(json.dumps({"movie_root": self.cfg["movie_root"],
                                   "moves": [{"src": str(self.src), "dst": str(self.target),
                                              "how": "copied"}]}))
        with mock.patch.object(library, "free_bytes", return_value=1024):
            with self.assertRaises(dr.DiskRipError):
                library.undo(log)
        self.assertTrue(self.target.exists(), "nothing may move when undo can't finish")
        self.assertFalse(self.src.exists())
        self.assertEqual(list(self.src.parent.iterdir()), [], "no .part left behind")

    def test_undo_runs_when_it_fits(self):
        self.target.parent.mkdir()
        os.replace(self.src, self.target)
        log = self.tmp / "work/log.json"
        log.write_text(json.dumps({"movie_root": self.cfg["movie_root"],
                                   "moves": [{"src": str(self.src), "dst": str(self.target),
                                              "how": "copied"}]}))
        with mock.patch.object(library, "free_bytes", return_value=10 * GiB):
            restored, problems = library.undo(log)
        self.assertEqual((restored, problems), (1, []))
        self.assertTrue(self.src.exists())


if __name__ == "__main__":
    unittest.main()
