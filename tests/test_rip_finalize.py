"""Ripping straight into the library: rip_into_place with a fake makemkvcon."""

import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import diskrip as dr  # noqa: E402
import library  # noqa: E402

FAKE = textwrap.dedent("""\
    #!{python}
    import os, sys
    mode = os.environ.get("FAKE_MKV", "ok")
    dest = sys.argv[-1]
    print("PRGC:5018,0,\\"Saving to MKV file\\"", flush=True)
    for pct in (0, 50, 100):
        print(f"PRGV:{{pct}},{{pct}},100", flush=True)
    if mode in ("ok", "two"):
        open(os.path.join(dest, "title_t00.mkv"), "wb").write(b"m" * 1000)
    if mode == "two":
        open(os.path.join(dest, "title_t01.mkv"), "wb").write(b"m")
    if mode == "fail":
        open(os.path.join(dest, "title_t00.mkv"), "wb").write(b"partial")
        print('MSG:5003,0,0,"Error: Failed to save title 0","x"', flush=True)
        sys.exit(1)
    """)


class RipIntoPlaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.movies = self.tmp / "Movies"
        self.movies.mkdir()
        exe = self.tmp / "makemkvcon"
        exe.write_text(FAKE.format(python=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        self.mk = dr.MakeMKV(str(exe))
        self.title = dr.Title(0)
        self.title.output_name = "title_t00.mkv"
        self.title.size_bytes = 1000
        self.final = self.movies / "Coco (2017) [imdb-tt2380307]" / "Coco (2017) [imdb-tt2380307] - 1080p.mkv"

    def tearDown(self):
        os.environ.pop("FAKE_MKV", None)
        self._tmp.cleanup()

    def rip(self, mode="ok"):
        os.environ["FAKE_MKV"] = mode
        seen = []
        out = dr.rip_into_place(self.mk, 0, self.title, self.final, self.movies,
                                on_progress=lambda pct, phase="": seen.append(pct))
        return out, seen

    def jobs(self):
        return [d for d in (self.movies / dr.RIP_TMP).iterdir() if d.is_dir()]

    def test_rip_lands_at_final_name(self):
        out, seen = self.rip()
        self.assertEqual(out, self.final)
        self.assertEqual(self.final.read_bytes(), b"m" * 1000)
        self.assertEqual(self.jobs(), [], "temp folder removed")
        self.assertTrue((self.movies / dr.RIP_TMP / ".plexignore").exists())
        self.assertEqual(seen[-1], 100)

    def test_taken_name_gets_a_suffix(self):
        self.final.parent.mkdir()
        self.final.write_bytes(b"old")
        out, _ = self.rip()
        self.assertEqual(out.name, "Coco (2017) [imdb-tt2380307] - 1080p (2).mkv")
        self.assertEqual(self.final.read_bytes(), b"old")

    def test_failed_rip_removes_partial_file(self):
        out, _ = self.rip("fail")
        self.assertIsNone(out)
        self.assertIn("Failed to save", self.mk.last_rip_error)
        self.assertEqual(self.jobs(), [])
        self.assertFalse(self.final.exists())

    def test_two_files_is_an_error_and_kept_for_inspection(self):
        with self.assertRaises(dr.DiskRipError):
            self.rip("two")
        self.assertEqual(len(self.jobs()), 1)
        self.assertFalse(self.final.exists())

    def test_not_enough_space_refuses_before_ripping(self):
        self.title.size_bytes = 40 * 10**9
        with mock.patch.object(dr.paths, "free_bytes", return_value=10 * 10**9):
            with self.assertRaises(dr.DiskRipError) as cm:
                self.rip()
        self.assertIn("40.0 GB", str(cm.exception))
        self.assertFalse((self.movies / dr.RIP_TMP).exists())


class FindMakemkvconTests(unittest.TestCase):
    def test_found_on_path_when_config_is_blank(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "makemkvcon"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": d}):
                self.assertEqual(dr.find_makemkvcon(""), str(exe))
                self.assertEqual(dr.find_makemkvcon("/nope/makemkvcon"), str(exe))
                self.assertEqual(dr.find_makemkvcon(str(exe)), str(exe))
            with mock.patch.dict(os.environ, {"PATH": ""}), \
                    mock.patch.object(dr, "MAKEMKVCON_CANDIDATES", ("makemkvcon",)):
                self.assertEqual(dr.find_makemkvcon(""), "")

    def test_load_config_fills_it_in(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "makemkvcon"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            cfg = Path(d) / "config.json"
            cfg.write_text('{"tmdb_api_key": "", "makemkvcon": "", "work_dir": "%s"}'
                           % (Path(d) / "work"))
            with mock.patch.dict(os.environ, {"PATH": d}):
                self.assertEqual(dr.load_config(cfg)["makemkvcon"], str(exe))


class SweepTests(unittest.TestCase):
    def test_sweeps_only_old_disk_rip_leftovers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tmp = root / dr.RIP_TMP
            old_job = tmp / "20260101T000000Z-abcdef-t00"
            new_job = tmp / "20261116T000000Z-123456-t01"
            other = tmp / "my stuff"
            for p in (old_job, new_job, other):
                p.mkdir(parents=True)
                (p / "x.mkv").write_bytes(b"x")
            old = time.time() - 48 * 3600
            os.utime(old_job, (old, old))
            os.utime(other, (old, old))
            self.assertEqual(dr.sweep_rip_tmp(root, 24), [old_job.name])
            self.assertTrue(new_job.exists() and other.exists())

            part = root / "Coco (2017)" / "Coco.mkv.part"
            keep = root / "Coco (2017)" / "notes.part"
            fresh = root / "Shrek (2001)" / "Shrek.mkv.part"
            for p in (part, keep, fresh):
                p.parent.mkdir(exist_ok=True)
                p.write_bytes(b"x")
            os.utime(part, (old, old))
            os.utime(keep, (old, old))
            self.assertEqual(library.sweep_parts(root, 24),
                             [os.path.join("Coco (2017)", "Coco.mkv.part")])
            self.assertTrue(keep.exists() and fresh.exists())


if __name__ == "__main__":
    unittest.main()
