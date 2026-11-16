import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import library  # noqa: E402


class CopyThenRemoveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "rips" / "Movie.mkv"
        self.src.parent.mkdir()
        self.src.write_bytes(os.urandom(3 * 1024 * 1024))
        os.utime(self.src, (1_600_000_000, 1_600_000_000))
        self.dst = self.tmp / "Movies" / "Movie (2000).mkv"
        self.dst.parent.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_copy_moves_and_keeps_mtime(self):
        data = self.src.read_bytes()
        library._copy_then_remove(self.src, self.dst)
        self.assertFalse(self.src.exists())
        self.assertEqual(self.dst.read_bytes(), data)
        self.assertEqual(int(self.dst.stat().st_mtime), 1_600_000_000)
        self.assertEqual(list(self.dst.parent.glob("*.part")), [])

    def test_disk_full_mid_copy_removes_part_and_keeps_source(self):
        def full(done, total):
            raise OSError(errno.ENOSPC, "No space left on device")

        with self.assertRaises(OSError) as cm:
            library._copy_then_remove(self.src, self.dst, on_bytes=full)
        self.assertEqual(cm.exception.errno, errno.ENOSPC)
        self.assertTrue(self.src.exists())
        self.assertFalse(self.dst.exists())
        self.assertEqual(list(self.dst.parent.iterdir()), [], "a .part file was left behind")

    def test_interrupt_removes_part(self):
        def stop(done, total):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            library._copy_then_remove(self.src, self.dst, on_bytes=stop)
        self.assertEqual(list(self.dst.parent.iterdir()), [])
        self.assertTrue(self.src.exists())


if __name__ == "__main__":
    unittest.main()
