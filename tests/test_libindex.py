import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import libdb  # noqa: E402
from libindex import LibraryIndex, parse_folder, title_key  # noqa: E402

IROBOT = {"id": 2048, "title": "I, Robot", "original_title": "I, Robot",
          "release_date": "2004-07-15", "runtime": 115,
          "external_ids": {"imdb_id": "tt0343818"}}


class FakeProber:
    """Stands in for ffprobe: info comes from a dict keyed by file name."""

    def __init__(self, db, info=None):
        self.db = db
        self.info = info or {}
        self.calls = []

    def probe(self, path):
        os.stat(path)
        self.calls.append(Path(path).name)
        return self.info.get(Path(path).name,
                             {"duration": 60.0, "width": 1920, "height": 1080,
                              "audio": 1, "chapters": 0, "title": "", "ok": True})

    def save(self):
        pass


def touch(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class ParseTests(unittest.TestCase):
    def test_parse_folder_names(self):
        p = parse_folder("I, Robot (2004) [imdb-tt0343818]")
        self.assertEqual((p["title"], p["year"], p["imdb"]), ("I, Robot", 2004, "tt0343818"))
        p = parse_folder("Coco (2017) {tmdb-354912}")
        self.assertEqual((p["title"], p["year"], p["tmdb"]), ("Coco", 2017, 354912))
        p = parse_folder("Shrek the Third")
        self.assertEqual((p["title"], p["year"], p["imdb"]), ("Shrek the Third", None, None))
        self.assertEqual(parse_folder("Movie [imdbid-tt1234567]")["imdb"], "tt1234567")

    def test_title_key(self):
        self.assertEqual(title_key("The Devil's Advocate"), title_key("Devil's Advocate"))
        self.assertEqual(title_key("I, Robot"), "i robot")
        self.assertEqual(title_key("Fast & Furious"), title_key("Fast and Furious"))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.movies = self.tmp / "Movies"
        self.movies.mkdir()
        self.db = libdb.DB(self.tmp / "work" / libdb.FILENAME)
        self.prober = FakeProber(self.db)

    def tearDown(self):
        self.db.conn.close()
        self._tmp.cleanup()

    def index(self):
        return LibraryIndex(self.movies, self.prober).refresh()

    def test_matches_by_name_and_by_id(self):
        for n in ("I, Robot (2004)", "I, Robot (2004) [imdb-tt0343818]", "I Robot",
                  "I, Robot (1999)", "I, Robot (2004) [imdb-tt9999999]", "Coco (2017)",
                  ".diskrip-duplicates"):
            (self.movies / n).mkdir()
        got = self.index().matches(IROBOT)
        self.assertEqual(sorted(got), ["I Robot", "I, Robot (2004)",
                                       "I, Robot (2004) [imdb-tt0343818]"])

    def test_learned_ids_match_odd_names_and_survive_restart(self):
        (self.movies / "irobot rip").mkdir()
        idx = self.index()
        self.assertEqual(idx.matches(IROBOT), [])
        idx.learn("irobot rip", IROBOT)
        self.assertEqual(self.index().matches(IROBOT), ["irobot rip"])
        db2 = libdb.DB(self.db.path)               # a new process
        self.assertEqual(LibraryIndex(self.movies, FakeProber(db2), db2).refresh()
                         .matches(IROBOT), ["irobot rip"])
        db2.conn.close()

    def test_videos_cached_until_folder_changes(self):
        f = self.movies / "I, Robot (2004)"
        touch(f / "I, Robot (2004).mkv")
        touch(f / "Featurettes" / "Making Of.mkv")
        touch(f / "poster.jpg")
        vids = self.index().videos("I, Robot (2004)")
        self.assertEqual(sorted(v["rel"] for v in vids),
                         ["Featurettes/Making Of.mkv", "I, Robot (2004).mkv"])
        self.assertEqual({v["rel"]: v["top"] for v in vids},
                         {"I, Robot (2004).mkv": True, "Featurettes/Making Of.mkv": False})
        _, stored = self.db.folder_files(self.movies, "I, Robot (2004)")
        self.assertEqual(len(stored), 2)
        # a new file appears: the folder's mtime changes and it is re-walked
        touch(f / "Featurettes" / "Trailer.mkv")
        os.utime(f / "Featurettes", ns=(1, 2))
        vids = self.index().videos("I, Robot (2004)")
        self.assertEqual(len(vids), 3)

    def test_removed_folders_are_forgotten(self):
        (self.movies / "Gone (2001)").mkdir()
        self.index().learn("Gone (2001)", IROBOT)
        (self.movies / "Gone (2001)").rmdir()
        self.index()
        self.assertEqual(self.db.folders(self.movies), {})

    def test_library_duplicates(self):
        for n in ("I, Robot (2004)", "I, Robot (2004) [imdb-tt0343818]", "Coco (2017)",
                  "Coco (2017) {tmdb-354912}", "Shrek (2001)"):
            (self.movies / n).mkdir()
        dupes = sorted(self.index().duplicates())
        self.assertEqual(dupes, [["Coco (2017)", "Coco (2017) {tmdb-354912}"],
                                 ["I, Robot (2004)", "I, Robot (2004) [imdb-tt0343818]"]])


if __name__ == "__main__":
    unittest.main()
