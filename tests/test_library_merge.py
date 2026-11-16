"""Library-aware planning: the I, Robot case from the NAS.

The library already had "I, Robot (2004)" when a run created
"I, Robot (2004) [imdb-tt0343818]" next to it. A new scan must fold both into
one standard folder, skip what the library already has, add a new resolution
as a second version, and keep everything undoable."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import libdb  # noqa: E402
import library  # noqa: E402
from libindex import LibraryIndex  # noqa: E402

IROBOT = {"id": 2048, "title": "I, Robot", "original_title": "I, Robot",
          "release_date": "2004-07-15", "runtime": 115,
          "external_ids": {"imdb_id": "tt0343818"}}
CANON = "I, Robot (2004) [imdb-tt0343818]"
HD = {"width": 1920, "height": 1080}
UHD = {"width": 3840, "height": 2160}


def info(duration, res):
    return dict(res, duration=duration, audio=1, chapters=0, title="", ok=True)


class FakeProber:
    def __init__(self, db, table):
        self.db, self.table = db, table

    def probe(self, path):
        os.stat(path)
        return self.table.get(Path(path).name, info(60.0, HD))

    def save(self):
        pass


def touch(path, size=16):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    return path


class Scan:
    def __init__(self, root, groups, idx):
        self.root = Path(root)
        self.groups = groups
        self.files = {f.id: f for g in groups for f in g.files}
        self.by_id = {g.id: g for g in groups}
        self.libindex = idx


class MergeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.rips, self.movies = self.tmp / "rips", self.tmp / "Movies"
        self.cfg = {"movie_root": str(self.movies), "rips_root": str(self.rips),
                    "work_dir": str(self.tmp / "work"), "movie_min_minutes": 70}
        self.db = libdb.DB(self.tmp / "work" / libdb.FILENAME)
        self.table = {
            "I, Robot (2004).mkv": info(6900, HD),
            f"{CANON} - 1080p.mkv": info(6905, HD),
            "Making Of.mkv": info(300, HD),
        }

    def tearDown(self):
        self.db.conn.close()
        self._tmp.cleanup()

    # --- fixtures ---------------------------------------------------------
    def library_with_two_folders(self):
        old = self.movies / "I, Robot (2004)"
        touch(old / "I, Robot (2004).mkv", 4000)
        touch(old / "I, Robot (2004).nfo")
        touch(old / "Featurettes" / "Making Of.mkv", 800)
        touch(old / "poster.jpg")
        touch(self.movies / CANON / f"{CANON} - 1080p.mkv", 4100)
        touch(self.movies / "Coco (2017)" / "Coco (2017).mkv")

    def scan(self, files):
        """files: [(name, duration, res, role, category)] in rips/IROBOT_US."""
        g = library.Group("i robot")
        g.query, g.details, g.confidence = "I Robot", IROBOT, "high"
        g.folders = [{"name": "IROBOT_US", "disc": None, "bonus": False}]
        for name, dur, res, role, cat in files:
            p = touch(self.rips / "IROBOT_US" / name, 900)
            f = library.LibFile(self.rips, "IROBOT_US", p)
            f.duration, f.width, f.height = dur, res["width"], res["height"]
            f.audio, f.ok, f.role, f.category = 1, True, role, cat
            f.name = Path(name).stem
            g.files.append(f)
        idx = LibraryIndex(self.movies, FakeProber(self.db, self.table)).refresh()
        return Scan(self.rips, [g], idx)

    def default_scan(self):
        return self.scan([
            ("title_t00.mkv", 6902, UHD, "main", "Other"),           # a 4K version
            ("Making Of.mkv", 300.4, HD, "extra", "Featurettes"),     # already there
            ("Deleted Scene.mkv", 500, HD, "extra", "Deleted Scenes"),  # new
        ])

    def by_src(self, items):
        return {os.path.relpath(i["src"], self.tmp): i for i in items}

    # --- tests ------------------------------------------------------------
    def test_plan_merges_two_library_folders(self):
        self.library_with_two_folders()
        scan = self.default_scan()
        summary = {}
        items = library.build_plan(scan, self.cfg, summary=summary)
        info_ = summary[scan.groups[0].id]
        self.assertEqual(sorted(info_["existing"]), ["I, Robot (2004)", CANON])
        self.assertEqual(info_["final"], CANON)
        ops = [(i["op"], os.path.relpath(i["src"], self.movies),
                i["target"] and os.path.relpath(i["target"], self.movies))
               for i in items if i.get("kind") == "lib"]
        q = os.path.join(library.QUARANTINE, ops[0][2].split(os.sep)[1])  # the stamp
        self.assertIn(("quarantine", "I, Robot (2004)/I, Robot (2004).mkv",
                       os.path.join(q, "I, Robot (2004)", "I, Robot (2004).mkv")), ops)
        self.assertIn(("quarantine", "I, Robot (2004)/I, Robot (2004).nfo",
                       os.path.join(q, "I, Robot (2004)", "I, Robot (2004).nfo")), ops)
        self.assertIn(("merge", "I, Robot (2004)/Featurettes/Making Of.mkv",
                       f"{CANON}/Featurettes/Making Of.mkv"), ops)
        self.assertIn(("merge", "I, Robot (2004)/poster.jpg", f"{CANON}/poster.jpg"), ops)
        self.assertEqual(ops[-1], ("remove-folder", "I, Robot (2004)", None))

        files = self.by_src(items)
        main = files["rips/IROBOT_US/title_t00.mkv"]
        self.assertEqual(main["status"], "move")
        self.assertEqual(os.path.relpath(main["target"], self.movies),
                         f"{CANON}/{CANON} - 2160p.mkv")
        self.assertIn("2160p version next to the 1080p", main["note"])
        dup = files["rips/IROBOT_US/Making Of.mkv"]
        self.assertEqual(dup["status"], "duplicate")
        new = files["rips/IROBOT_US/Deleted Scene.mkv"]
        self.assertEqual((new["status"], os.path.relpath(new["target"], self.movies)),
                         ("move", f"{CANON}/Deleted Scenes/Deleted Scene.mkv"))

    def test_same_resolution_feature_is_a_duplicate(self):
        self.library_with_two_folders()
        scan = self.scan([("title_t00.mkv", 6901, HD, "main", "Other")])
        item = [i for i in library.build_plan(scan, self.cfg) if i.get("file")][0]
        self.assertEqual(item["status"], "duplicate")
        self.assertEqual(item["dup_of"], f"{CANON} - 1080p.mkv")

    def test_apply_then_undo_round_trip(self):
        self.library_with_two_folders()
        scan = self.default_scan()
        before = sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
                        if p.is_file() and "work" not in p.parts)
        items = library.build_plan(scan, self.cfg)
        log = library.apply_plan(items, self.cfg, scan)
        failed = [(i["src"], i.get("error")) for i in items if i.get("state") == "failed"]
        self.assertEqual(failed, [])

        final = self.movies / CANON
        self.assertFalse((self.movies / "I, Robot (2004)").exists())
        self.assertEqual(sorted(str(p.relative_to(final)) for p in final.rglob("*")
                                if p.is_file()),
                         sorted([f"{CANON} - 1080p.mkv", f"{CANON} - 2160p.mkv",
                                 "Featurettes/Making Of.mkv", "poster.jpg",
                                 "Deleted Scenes/Deleted Scene.mkv"]))
        quarantined = {p.name for p in (self.movies / library.QUARANTINE).rglob("*")
                       if p.is_file()}
        self.assertTrue({"I, Robot (2004).mkv", "I, Robot (2004).nfo", ".plexignore"}
                        <= quarantined)
        self.assertTrue((self.rips / "IROBOT_US" / "Making Of.mkv").exists(),
                        "a duplicate stays in rips")
        # the folder is now known by id for the next audit
        self.assertEqual(self.db.folders(self.movies)[CANON]["tmdb"], 2048)

        restored, problems = library.undo(log["path"])
        self.assertEqual(problems, [])
        after = sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
                       if p.is_file() and "work" not in p.parts
                       and library.QUARANTINE not in p.parts)
        self.assertEqual(after, before)

    def test_rename_option(self):
        old = self.movies / "I, Robot (2004)"
        touch(old / "I, Robot (2004).mkv", 4000)
        touch(old / "I, Robot (2004).en.srt")
        rows = [("title_t00.mkv", 6902, UHD, "main", "Other")]

        summary = {}
        items = library.build_plan(self.scan(rows), self.cfg, summary=summary)
        ops = [(i["op"], os.path.relpath(i["src"], self.movies),
                os.path.relpath(i["target"], self.movies)) for i in items if i.get("kind")]
        self.assertEqual(ops, [
            ("rename-folder", "I, Robot (2004)", CANON),
            ("rename-file", f"{CANON}/I, Robot (2004).mkv", f"{CANON}/{CANON} - 1080p.mkv"),
            ("rename-file", f"{CANON}/I, Robot (2004).en.srt",
             f"{CANON}/{CANON} - 1080p.en.srt"),
        ])
        self.assertTrue(list(summary.values())[0]["renameAvailable"])

        summary = {}
        items = library.build_plan(self.scan(rows), self.cfg, {"rename": False}, summary)
        self.assertEqual([i for i in items if i.get("kind")], [])
        main = [i for i in items if i.get("file")][0]
        self.assertEqual(os.path.relpath(main["target"], self.movies),
                         "I, Robot (2004)/I, Robot (2004) - 2160p.mkv")
        info_ = list(summary.values())[0]
        self.assertEqual((info_["final"], info_["renameAvailable"]),
                         ("I, Robot (2004)", True))

    def test_per_movie_rename_override(self):
        touch(self.movies / "I, Robot (2004)" / "I, Robot (2004).mkv", 4000)
        scan = self.scan([("title_t00.mkv", 6902, UHD, "main", "Other")])
        gid = scan.groups[0].id
        items = library.build_plan(scan, self.cfg,
                                   {"rename": True, "groups": {gid: {"rename": False}}})
        self.assertEqual([i for i in items if i.get("kind")], [])

    def test_failed_folder_rename_skips_that_movie(self):
        touch(self.movies / "I, Robot (2004)" / "I, Robot (2004).mkv", 4000)
        scan = self.scan([("Deleted Scene.mkv", 500, HD, "extra", "Deleted Scenes")])
        items = library.build_plan(scan, self.cfg)
        real = library.move_file

        def flaky(src, dst, on_bytes=None):
            if Path(src).is_dir():
                raise PermissionError(13, "Permission denied")
            return real(src, dst, on_bytes)

        with mock.patch.object(library, "move_file", side_effect=flaky):
            library.apply_plan(items, self.cfg, scan)
        states = {i.get("op") or "file": i["state"] for i in items if "state" in i}
        self.assertEqual(states["rename-folder"], "failed")
        self.assertEqual(states["file"], "skipped")
        self.assertFalse((self.movies / CANON).exists(), "no second folder may appear")
        self.assertTrue((self.rips / "IROBOT_US" / "Deleted Scene.mkv").exists())

    def test_no_library_match_keeps_old_behaviour(self):
        touch(self.movies / "Coco (2017)" / "Coco (2017).mkv")
        scan = self.default_scan()
        items = library.build_plan(scan, self.cfg)
        self.assertEqual([i for i in items if i.get("kind")], [])
        self.assertEqual({i["status"] for i in items}, {"move"})

    def test_cli_prints_a_merge_plan(self):
        import contextlib
        import io
        self.library_with_two_folders()
        scan = self.default_scan()
        scan.skipped = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            counts = library.print_plan(scan, library.build_plan(scan, self.cfg), self.cfg)
        self.assertEqual((counts["move"], counts["lib"], counts["duplicate"]), (2, 5, 1))
        self.assertIn("quarantine", out.getvalue())

    def test_log_records_library_changes(self):
        self.library_with_two_folders()
        scan = self.default_scan()
        log = library.apply_plan(library.build_plan(scan, self.cfg), self.cfg, scan)
        saved = json.loads(Path(log["path"]).read_text())
        self.assertIn("quarantine", {m["how"] for m in saved["moves"]})
        self.assertEqual(saved["pruned_library"], [str(self.movies / "I, Robot (2004)")])


if __name__ == "__main__":
    unittest.main()
