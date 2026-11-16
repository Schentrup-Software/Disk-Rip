import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import library  # noqa: E402
from paths import RootError  # noqa: E402


class FakeScan:
    def __init__(self, root):
        self.root = Path(root)
        self.groups = []
        self.files = {}


class LibraryRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        for d in ("rips", "Movies", "work"):
            (self.tmp / d).mkdir()
        self.cfg = {"movie_root": str(self.tmp / "Movies"),
                    "rips_root": str(self.tmp / "rips"),
                    "work_dir": str(self.tmp / "work")}

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_plan_refuses_url_movie_root(self):
        self.cfg["movie_root"] = "smb://nas.example.com/Movies/"
        with self.assertRaises(RootError) as cm:
            library.build_plan(FakeScan(self.tmp / "rips"), self.cfg)
        self.assertEqual(cm.exception.name, "movies")

    def test_build_plan_accepts_good_root(self):
        self.assertEqual(library.build_plan(FakeScan(self.tmp / "rips"), self.cfg), [])

    def test_apply_plan_refuses_missing_root_and_writes_no_log(self):
        self.cfg["movie_root"] = str(self.tmp / "gone")
        with self.assertRaises(RootError):
            library.apply_plan([], self.cfg, FakeScan(self.tmp / "rips"))
        self.assertFalse((self.tmp / "work" / "library-logs").exists())

    def test_move_file_refuses_relative_destination(self):
        src = self.tmp / "rips" / "a.mkv"
        src.write_bytes(b"data")
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            with self.assertRaises(RootError):
                library.move_file(src, Path("smb:/nas/Movies/a.mkv"))
        finally:
            os.chdir(cwd)
        self.assertTrue(src.exists())
        self.assertFalse((self.tmp / "smb:").exists())

    def test_resolve_root_rejects_relative(self):
        with self.assertRaises(RootError):
            library.resolve_root("rips")


if __name__ == "__main__":
    unittest.main()
