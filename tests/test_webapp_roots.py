import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import unittest.mock
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import webapp  # noqa: E402


class WebRootErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "Shows").mkdir()
        cfg = {"tmdb_api_key": "", "movie_root": "relative/Movies",
               "tv_root": str(tmp / "Shows"), "rips_root": "",
               "work_dir": str(tmp / "work"), "makemkvcon": "", "ffmpeg": ""}
        path = tmp / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        webapp.Handler.app = webapp.App(str(path))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_roots_endpoint_reports_bad_root(self):
        code, j = self.request("/api/roots")
        self.assertEqual(code, 200)
        self.assertIn("relative path", j["roots"]["movies"]["error"])
        self.assertTrue(j["roots"]["tv"]["ok"])
        self.assertTrue(j["roots"]["work"]["ok"])      # created by load_config
        self.assertIsNone(j["roots"]["rips"]["error"])

    def test_rip_with_bad_root_is_409(self):
        code, j = self.request("/api/rip", {"kind": "movie"})
        self.assertEqual(code, 409)
        self.assertEqual(j["root"], "movies")
        self.assertIn("movie_root", j["error"])


class WebMergeTests(unittest.TestCase):
    """Library check endpoints, with ffprobe replaced by a fake."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_library_merge import FakeProber, HD, info, touch
        import library
        import libdb
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.movies = tmp / "Movies"
        touch(cls.movies / "Coco (2017)" / "Coco (2017).mkv")
        touch(cls.movies / "Coco (2017) {tmdb-354912}" / "Coco.mkv")
        table = {"Coco (2017).mkv": info(6300, HD), "Coco.mkv": info(6301, HD)}
        cfg = {"tmdb_api_key": "", "movie_root": str(cls.movies), "tv_root": "",
               "rips_root": "", "work_dir": str(tmp / "work"), "makemkvcon": "", "ffmpeg": ""}
        path = tmp / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        cls.patch = unittest.mock.patch.object(
            library, "Prober", lambda c: FakeProber(libdb.open_db(c["work_dir"]), table))
        cls.patch.start()
        cls.app = webapp.App(str(path))
        webapp.Handler.app = cls.app
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    request = WebRootErrorTests.request

    def test_find_preview_merge_undo(self):
        code, j = self.request("/api/library/duplicates")
        self.assertEqual((code, j["sets"]), (200, [["Coco (2017)", "Coco (2017) {tmdb-354912}"]]))
        folders = j["sets"][0]
        code, plan = self.request("/api/library/merge/plan", {"folders": folders})
        self.assertEqual(code, 200)
        self.assertEqual(plan["final"], "Coco (2017) {tmdb-354912}")
        self.assertIn("quarantine", [o["op"] for o in plan["ops"]])
        self.assertTrue((self.movies / "Coco (2017)").exists(), "preview changes nothing")
        code, r = self.request("/api/library/merge", {"folders": folders})
        self.assertEqual((code, r["failed"]), (200, []))
        self.assertFalse((self.movies / "Coco (2017)").exists())
        code, j = self.request("/api/library/duplicates")
        self.assertEqual(j["sets"], [])
        code, u = self.request("/api/library/undo", {"log": r["log"]})
        self.assertEqual((code, u["problems"]), (200, []))
        self.assertTrue((self.movies / "Coco (2017)" / "Coco (2017).mkv").exists())


if __name__ == "__main__":
    unittest.main()
