import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import makemkv_setup as ms  # noqa: E402

SECRET = "M-SECRET-KEY-123"


class SetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    def run_setup(self, env, fetch=lambda: "T-BETA"):
        return ms.setup(env, self.home, fetch, self.logs.append)

    def conf(self):
        return (self.home / ".MakeMKV" / "settings.conf").read_text()

    def test_registered_key_is_written_and_never_logged(self):
        self.assertEqual(self.run_setup({"MAKEMKV_KEY": f"  {SECRET} \n"}), "registered")
        self.assertIn(f'app_Key = "{SECRET}"', self.conf())
        self.assertIn('app_UpdateEnable = "0"', self.conf())
        self.assertNotIn(SECRET, " ".join(self.logs))
        mode = (self.home / ".MakeMKV" / "settings.conf").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_key_file(self):
        f = self.home / "key.txt"
        f.write_text(SECRET + "\n")
        self.assertEqual(self.run_setup({"MAKEMKV_KEY_FILE": str(f)}), "registered")
        self.assertIn(SECRET, self.conf())

    def test_blank_or_beta_fetches_the_beta_key_and_caches_it(self):
        for env in ({}, {"MAKEMKV_KEY": "beta"}):
            self.assertEqual(self.run_setup(env), "beta")
            self.assertIn('app_Key = "T-BETA"', self.conf())

        def offline():
            raise OSError("network down")
        self.assertEqual(self.run_setup({}, offline), "beta-cached")
        self.assertIn('app_Key = "T-BETA"', self.conf())

    def test_no_key_at_all(self):
        def offline():
            raise OSError("network down")
        self.assertEqual(self.run_setup({}, offline), "missing")
        self.assertNotIn("app_Key", self.conf())

    def test_unset_leaves_settings_alone(self):
        d = self.home / ".MakeMKV"
        d.mkdir()
        (d / "settings.conf").write_text("mine\n")
        self.assertEqual(self.run_setup({"MAKEMKV_KEY": "UNSET"}), "unset")
        self.assertEqual(self.conf(), "mine\n")

    def test_beta_key_parsing(self):
        html = '<div class="codebox"><pre><code>T-abc@DEF_123</code></pre></div><code>2nd</code>'
        self.assertEqual(ms._CODE_RE.search(html).group(1), "T-abc@DEF_123")

    def test_main_never_prints_the_key(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ms.setup({"MAKEMKV_KEY": SECRET, "HOME": str(self.home)})
        self.assertNotIn(SECRET, out.getvalue())


if __name__ == "__main__":
    unittest.main()
