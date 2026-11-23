"""Settings: defaults, the user's overrides, and saving a toggle."""

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from openscansnap import config
from openscansnap.options import Quality, ScanMode


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.toml"

    def test_defaults_without_a_file(self):
        settings = config.load(self.path)
        self.assertEqual(settings.out_dir, Path.home() / "Downloads")
        self.assertEqual(settings.options.language, "auto")
        self.assertTrue(settings.show_when_paper_loaded)
        self.assertFalse(settings.debug)

    def test_the_settings_file_is_where_path_points_when_read(self):
        # tests point PATH at a temporary file: the owner's own settings
        # (debug mode on) must never reach a test's scan. A value no real
        # settings file holds, and a save that writes nothing, so that even
        # this test failing cannot touch the owner's file.
        marker = Path(self.tmp.name) / "marker-scans"
        self.path.write_text(f'out_dir = "{marker}"\n')
        with mock.patch.object(config, "PATH", self.path):
            self.assertEqual(config.load().out_dir, marker)
            with mock.patch.object(config.files, "replace_file") as written:
                config.save("debug", False)
        self.assertEqual(written.call_args.args[0], self.path)

    def test_user_values_override_defaults(self):
        self.path.write_text(
            '[scan]\nlanguage = "eng+nld"\n[window]\nshow_when_paper_loaded = false\n'
        )
        settings = config.load(self.path)
        self.assertEqual(settings.options.language, "eng+nld")
        self.assertFalse(settings.show_when_paper_loaded)
        self.assertEqual(settings.out_dir, Path.home() / "Downloads")

    def test_bad_files_are_refused_with_the_reason(self):
        for text, reason in (
            ("langauge = 'eng'\n", "unknown setting langauge"),
            ("[scan]\nmode = 'fancy'\n", "scan.mode must be one of"),
            (
                "[scan]\nlanguage = ''\n",
                "scan.language must be 'auto' or Tesseract language names",
            ),
            (
                "[scan]\nlanguage = 'eng+'\n",
                "scan.language must be 'auto' or Tesseract language names",
            ),
            ("[window]\nshow_when_paper_loaded = 'no'\n", "must be a bool"),
            ("out_dir = 'scans'\n", "absolute path"),
            ("language = \n", str(self.path)),
        ):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaisesRegex(config.ConfigError, reason):
                    config.load(self.path)

    def test_save_sets_one_setting_and_keeps_the_others(self):
        self.path.write_text('out_dir = "~/Scans"\n')
        config.save("window.show_when_paper_loaded", False, self.path)
        self.assertEqual(
            tomllib.loads(self.path.read_text()),
            {"out_dir": "~/Scans", "window": {"show_when_paper_loaded": False}},
        )
        self.assertFalse(config.load(self.path).show_when_paper_loaded)

    def test_save_refuses_what_load_would(self):
        for name, value, reason in (
            ("scan.mode", "fancy", "scan.mode must be one of"),
            (
                "scan.language",
                "",
                "scan.language must be 'auto' or Tesseract language names",
            ),
            ("out_dir", "scans", "absolute path"),
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(config.ConfigError, reason),
            ):
                config.save(name, value, self.path)
            self.assertFalse(self.path.exists())

    def test_default_options_ignore_the_users_file(self):
        with mock.patch.object(
            config, "_user", return_value={"scan": {"mode": "gray"}}
        ):
            options = config.default_options()
        self.assertEqual((options.mode, options.quality), (ScanMode.AUTO, Quality.HIGH))

    def test_chosen_folder_is_saved_relative_to_home(self):
        config.save("out_dir", config.home_relative(Path.home() / "Scans"), self.path)
        self.assertEqual(tomllib.loads(self.path.read_text()), {"out_dir": "~/Scans"})
        self.assertEqual(config.load(self.path).out_dir, Path.home() / "Scans")
        self.assertEqual(
            config.home_relative(Path("/Volumes/Archive")), "/Volumes/Archive"
        )

    def test_save_refuses_a_wrong_type(self):
        with self.assertRaises(config.ConfigError):
            config.save("window.show_when_paper_loaded", "yes", self.path)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
