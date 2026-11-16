import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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


if __name__ == "__main__":
    unittest.main()
