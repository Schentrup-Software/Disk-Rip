import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import paths  # noqa: E402
from paths import RootError, validate_root  # noqa: E402


class ValidateRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.app = self.tmp / "app"
        self.app.mkdir()
        self.lib = self.tmp / "Movies"
        self.lib.mkdir()

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            if p.is_dir():
                os.chmod(p, 0o755)
        self._tmp.cleanup()

    def check(self, value, **kw):
        kw.setdefault("app_dir", self.app)
        return validate_root("movies", value, **kw)

    def test_accepts_writable_folder(self):
        self.assertEqual(self.check(str(self.lib)), self.lib)
        self.assertEqual(list(self.lib.iterdir()), [], "write probe left a file")

    def test_rejects_empty(self):
        for v in ("", "   ", None):
            with self.assertRaises(RootError):
                self.check(v)

    def test_rejects_relative(self):
        with self.assertRaises(RootError) as cm:
            self.check("Movies/")
        self.assertIn("relative", cm.exception.reason)
        self.assertEqual(cm.exception.name, "movies")

    def test_rejects_urls(self):
        for v in ("smb://nas/Movies/", "smb:/nas/Movies", "afp://nas/x",
                  "nfs:host:/x", "http://x/y"):
            with self.assertRaises(RootError, msg=v) as cm:
                self.check(v)
            self.assertIn("network address", cm.exception.reason)

    def test_rejects_missing(self):
        with self.assertRaises(RootError):
            self.check(str(self.tmp / "nope"))

    def test_rejects_file(self):
        f = self.tmp / "file.mkv"
        f.write_bytes(b"x")
        with self.assertRaises(RootError):
            self.check(str(f))

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_rejects_read_only(self):
        os.chmod(self.lib, 0o555)
        with self.assertRaises(RootError) as cm:
            self.check(str(self.lib))
        self.assertIn("not writable", cm.exception.reason)
        # reading is still fine when we won't write
        self.assertEqual(self.check(str(self.lib), must_write=False), self.lib)

    def test_rejects_inside_app_dir(self):
        inner = self.app / "smb:" / "nas" / "Movies"
        inner.mkdir(parents=True)
        with self.assertRaises(RootError) as cm:
            self.check(str(inner))
        self.assertIn("Disk-Rip folder", cm.exception.reason)
        with self.assertRaises(RootError):
            self.check(str(self.app))

    def test_rejects_symlink_into_app_dir(self):
        target = self.app / "out"
        target.mkdir()
        link = self.tmp / "link"
        link.symlink_to(target)
        with self.assertRaises(RootError):
            self.check(str(link))

    def test_rejects_inside_work_dir(self):
        work = self.tmp / "work"
        (work / "x").mkdir(parents=True)
        with self.assertRaises(RootError):
            self.check(str(work / "x"), work_dir=work)

    def test_work_dir_itself_may_live_in_app_dir(self):
        work = self.app / "_work"
        work.mkdir()
        self.assertEqual(validate_root("work", str(work), app_dir=self.app), work)

    def test_expands_user(self):
        home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.tmp)
        try:
            self.assertEqual(self.check("~/Movies"), self.lib)
        finally:
            if home is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = home


class DestinationTests(unittest.TestCase):
    def test_check_destination(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "app"
            paths.check_destination(Path(d) / "Movies" / "X (2001)", app_dir=app)
            for bad in ("smb:/nas/Movies/X", "Movies/X", "smb://nas/Movies/X",
                        str(app / "smb:" / "X")):
                with self.assertRaises(RootError, msg=bad):
                    paths.check_destination(bad, app_dir=app)


class ResolveSmbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vol = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_paths_pass_through(self):
        self.assertEqual(paths.resolve_spec("movies", " /data/Movies "), "/data/Movies")

    def test_smb_is_an_error_off_macos(self):
        with self.assertRaises(RootError):
            paths.resolve_spec("movies", "smb://nas/Movies/", platform="linux")

    def test_mounted_share_resolves(self):
        (self.vol / "Movies" / "Sub Dir").mkdir(parents=True)
        got = paths.resolve_spec("movies", "smb://user@nas.example.com/Movies/Sub%20Dir/",
                                 platform="darwin", volumes=self.vol,
                                 mount=lambda url: self.fail("already mounted"))
        self.assertEqual(got, str(self.vol / "Movies" / "Sub Dir"))

    def test_unmounted_share_is_mounted(self):
        calls = []

        def mount(url):
            calls.append(url)
            (self.vol / "Shows").mkdir()

        got = paths.resolve_spec("tv", "smb://nas/Shows", platform="darwin",
                                 volumes=self.vol, mount=mount)
        self.assertEqual(got, str(self.vol / "Shows"))
        self.assertEqual(calls, ["smb://nas/Shows"])

    def test_mount_failure(self):
        with self.assertRaises(RootError) as cm:
            paths.resolve_spec("tv", "smb://nas/Shows", platform="darwin",
                               volumes=self.vol, mount=lambda url: None)
        self.assertIn("could not be mounted", cm.exception.reason)

    def test_resolve_config_roots_handles_all_four(self):
        for share in ("rips", "Movies", "Shows", "scratch"):
            (self.vol / share).mkdir()
        cfg = {"rips_root": "smb://nas/rips", "movie_root": "smb://nas/Movies/",
               "tv_root": "smb://nas/Shows/", "work_dir": "smb://nas/scratch",
               "language": "en"}
        errors = paths.resolve_config_roots(cfg, platform="darwin", volumes=self.vol)
        self.assertEqual(errors, {})
        self.assertEqual(cfg["movie_root"], str(self.vol / "Movies"))
        self.assertEqual(cfg["tv_root"], str(self.vol / "Shows"))
        self.assertEqual(cfg["rips_root"], str(self.vol / "rips"))
        self.assertEqual(cfg["work_dir"], str(self.vol / "scratch"))

    def test_resolve_config_roots_keeps_failures(self):
        cfg = {"movie_root": "smb://nas/Movies/"}
        errors = paths.resolve_config_roots(cfg, platform="linux")
        self.assertIn("movies", errors)
        self.assertEqual(cfg["movie_root"], "smb://nas/Movies/")
        status, _ = paths.check_roots(cfg, errors=errors)
        self.assertEqual(status["movies"]["error"], errors["movies"])


class OverlapTests(unittest.TestCase):
    def test_overlap_is_a_warning(self):
        w = paths.overlap_warnings({"rips": "/data/rips", "movies": "/data/rips/Movies",
                                    "tv": "/data/tv"})
        self.assertEqual(len(w), 1)
        self.assertIn("movie_root", w[0])
        self.assertEqual(len(paths.overlap_warnings({"rips": "/data/x/rips",
                                                     "movies": "/data/x"})), 1)
        self.assertEqual(paths.overlap_warnings({"movies": "/m"}), [])

    def test_check_roots_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "m").mkdir()
            (Path(d) / "work").mkdir()
            cfg = {"movie_root": str(Path(d) / "m"), "tv_root": "smb://nas/Shows/",
                   "rips_root": "", "work_dir": str(Path(d) / "work")}
            status, _ = paths.check_roots(cfg, app_dir=Path(d) / "app")
            self.assertTrue(status["movies"]["ok"])
            self.assertFalse(status["tv"]["ok"])
            self.assertIn("network address", status["tv"]["error"])
            self.assertIsNone(status["rips"]["error"])     # unset is not an error


if __name__ == "__main__":
    unittest.main()
