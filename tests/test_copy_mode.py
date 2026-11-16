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


class CopyModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        for d in ("rips/Disc", "Movies", "work"):
            (self.tmp / d).mkdir(parents=True)
        self.src = self.tmp / "rips/Disc/title_t00.mkv"
        self.src.write_bytes(os.urandom(5000))
        (self.tmp / "rips/Disc/title_t00.en.srt").write_text("1\n")
        self.target = self.tmp / "Movies/Movie (2000)/Movie (2000) - 1080p.mkv"
        self.cfg = {"movie_root": str(self.tmp / "Movies"),
                    "rips_root": str(self.tmp / "rips"), "work_dir": str(self.tmp / "work")}
        self.items = [{"file": "f", "group": "g", "src": str(self.src), "role": "main",
                       "category": "Other", "name": "x", "target": str(self.target),
                       "size": 5000, "status": "move"}]

    def tearDown(self):
        self._tmp.cleanup()

    def apply(self, **kw):
        return library.apply_plan(self.items, self.cfg, Scan(self.tmp / "rips"), **kw)

    def test_copy_keeps_originals_and_sidecars(self):
        log = self.apply(mode="copy", prune=True)
        self.assertEqual(self.items[0]["state"], "done")
        self.assertTrue(self.src.exists())
        self.assertEqual(self.target.read_bytes(), self.src.read_bytes())
        self.assertTrue((self.target.parent / "Movie (2000) - 1080p.en.srt").exists())
        self.assertTrue((self.tmp / "rips/Disc/title_t00.en.srt").exists())
        self.assertEqual(log["pruned"], [], "nothing is pruned in copy mode")
        self.assertEqual(log["mode"], "copy")

    def test_undo_of_a_copy_removes_only_the_copy(self):
        log = self.apply(mode="copy")
        restored, problems = library.undo(log["path"])
        self.assertEqual((restored, problems), (2, []))
        self.assertTrue(self.src.exists())
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.parent.exists())

    def test_undo_leaves_a_copy_that_no_longer_matches(self):
        log = self.apply(mode="copy")
        self.target.write_bytes(b"edited")
        _, problems = library.undo(log["path"])
        self.assertTrue(self.target.exists())
        self.assertTrue(any("differs" in p for p in problems))

    def test_copy_on_one_filesystem_still_needs_space(self):
        with mock.patch.object(library, "needs_copy", return_value=False), \
                mock.patch.object(library, "free_bytes", return_value=GiB):
            self.apply(mode="move")                    # a rename: fits
            self.assertTrue(self.target.exists())
            os.replace(self.target, self.src)
            self.items[0].pop("state")
            with self.assertRaises(dr.DiskRipError):
                self.apply(mode="copy")                 # a real copy: doesn't

    def test_mode_from_config_and_env(self):
        self.assertEqual(library.transfer_mode({}), "move")
        self.assertEqual(library.transfer_mode({"transfer_mode": "copy"}), "copy")
        self.assertEqual(library.transfer_mode({"transfer_mode": "copy"}, "move"), "move")
        with mock.patch.dict(os.environ, {"DISKRIP_TRANSFER_MODE": "copy"}):
            self.assertEqual(library.transfer_mode({}), "copy")
        with self.assertRaises(dr.DiskRipError):
            library.transfer_mode({"transfer_mode": "zap"})

    def test_read_only_rips_is_fine_for_copy(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        os.chmod(self.tmp / "rips", 0o555)
        try:
            self.apply(mode="copy")
            self.assertEqual(self.items[0]["state"], "done")
        finally:
            os.chmod(self.tmp / "rips", 0o755)


if __name__ == "__main__":
    unittest.main()
