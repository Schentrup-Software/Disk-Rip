import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import libdb  # noqa: E402


class DbTests(unittest.TestCase):
    def test_probes_round_trip_and_null_db(self):
        with tempfile.TemporaryDirectory() as d:
            db = libdb.DB(Path(d) / libdb.FILENAME)
            info = {"duration": 5.0, "ok": True}
            db.save_probes({"/x.mkv": {"size": 1, "mtime": 2, "info": info}})
            self.assertEqual(db.load_probes(), {"/x.mkv": {"size": 1, "mtime": 2, "info": info}})
            db.conn.close()
        null = libdb.NullDB()
        null.save_probes({})
        self.assertEqual(null.load_probes(), {})
        self.assertEqual(null.folder_files("/m", "x"), (None, []))

    def test_prober_imports_legacy_json_cache(self):
        import library
        with tempfile.TemporaryDirectory() as d:
            legacy = {"/a.mkv": {"size": 3, "mtime": 4, "info": {"duration": 1.0, "ok": True}}}
            Path(d, "library-probe-cache.json").write_text(json.dumps(legacy))
            cfg = {"work_dir": d, "ffprobe": sys.executable}   # any existing file
            p = library.Prober(cfg)
            self.assertEqual(p.cache, legacy)
            p.save()
            self.assertEqual(p.db.load_probes(), legacy)
            p.db.conn.close()
            libdb._OPEN.clear()


if __name__ == "__main__":
    unittest.main()
