"""Settings: defaults, the user's overrides, and saving a toggle."""

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from openscansnap import config
from openscansnap.options import Quality, ScanMode, ShowThrough


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
            {
                "version": config.FORMAT,
                "out_dir": "~/Scans",
                "window": {"show_when_paper_loaded": False},
            },
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
        self.path.write_text('[scan]\nmode = "gray"\n')
        with mock.patch.object(config, "PATH", self.path):
            self.assertEqual(config.load().options.mode, ScanMode.GRAY)
            options = config.default_options()
        self.assertEqual((options.mode, options.quality), (ScanMode.AUTO, Quality.HIGH))

    def test_chosen_folder_is_saved_relative_to_home(self):
        config.save("out_dir", config.home_relative(Path.home() / "Scans"), self.path)
        self.assertEqual(
            tomllib.loads(self.path.read_text()),
            {"version": config.FORMAT, "out_dir": "~/Scans"},
        )
        self.assertEqual(config.load(self.path).out_dir, Path.home() / "Scans")
        self.assertEqual(
            config.home_relative(Path("/Volumes/Archive")), "/Volumes/Archive"
        )

    def test_save_refuses_a_wrong_type(self):
        with self.assertRaises(config.ConfigError):
            config.save("window.show_when_paper_loaded", "yes", self.path)
        self.assertFalse(self.path.exists())

    def test_a_file_from_0_1_is_read_upgraded_and_saved_so(self):
        # 0.1.0 and 0.1.1 wrote show-through removal on or off, and no format
        for old, new in ((True, ShowThrough.GRAY_AND_COLOR), (False, ShowThrough.KEEP)):
            with self.subTest(old=old):
                self.path.write_text(
                    f"debug = true\n[scan]\nremove_show_through = {str(old).lower()}\n"
                )
                settings = config.load(self.path)
                self.assertEqual(settings.options.show_through, new)
                self.assertTrue(settings.debug)  # the rest as it was
                self.assertEqual(
                    tomllib.loads(self.path.read_text()),
                    {
                        "version": config.FORMAT,
                        "debug": True,
                        "scan": {"show_through": str(new)},
                    },
                )

    def test_saving_writes_the_format(self):
        config.save("debug", True, self.path)
        self.assertEqual(tomllib.loads(self.path.read_text())["version"], config.FORMAT)

    def test_a_current_file_is_not_rewritten(self):
        self.path.write_text(f"version = {config.FORMAT}\ndebug = true\n# mine\n")
        config.load(self.path)
        self.assertIn("# mine", self.path.read_text())

    def test_a_later_versions_settings_are_left_out_and_its_file_kept(self):
        text = f"version = {config.FORMAT + 1}\ndebug = true\n[scan]\nsharpen = 3\n"
        self.path.write_text(text)
        with self.assertLogs("openscansnap.config", "WARNING") as logged:
            settings = config.load(self.path)
        self.assertTrue(settings.debug)
        self.assertIn("scan.sharpen", logged.output[0])
        self.assertEqual(self.path.read_text(), text)

    def test_a_later_versions_new_value_is_left_out_so_the_app_starts(self):
        later = config.FORMAT + 1
        self.path.write_text(
            f'version = {later}\n[scan]\nshow_through = "smart"\nmode = 3\n'
        )
        with self.assertLogs("openscansnap.config", "WARNING") as logged:
            settings = config.load(self.path)
        self.assertEqual(
            settings.options.show_through, config.default_options().show_through
        )
        self.assertEqual(settings.options.mode, config.default_options().mode)
        self.assertEqual(len(logged.output), 2)

    def test_a_file_nested_too_deeply_is_refused_not_a_crash(self):
        self.path.write_text("debug = " + "{a = " * 5000 + "1" + "}" * 5000 + "\n")
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_a_value_nested_too_deeply_to_print_is_refused_not_a_crash(self):
        # tables nested as deep as a key may go (tomllib refuses more than
        # 1000 parts): refused with a message, and the message stays short
        self.path.write_text("[debug" + ".a" * 990 + "]\n")
        with self.assertRaises(config.ConfigError) as refused:
            config.load(self.path)
        self.assertLess(len(str(refused.exception)), 500)

    def test_a_file_gone_while_read_is_no_file(self):
        self.path.write_text("debug = true\n")
        read_text = Path.read_text

        def gone(path, *args, **kwargs):
            if path == self.path:  # deleted after it was found
                raise FileNotFoundError(path)
            return read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", gone):
            self.assertFalse(config.load(self.path).debug)

    def test_the_settings_of_this_format_stay_what_they_are(self):
        # a setting or value added without a new FORMAT stops the version
        # before from starting: it reads its own format strictly. Adding one
        # means FORMAT + 1, its upgrade, and this list.
        names = sorted(name for name, _ in config._leaves(config._defaults()))
        self.assertEqual(
            (config.FORMAT, names),
            (
                2,
                [
                    "debug",
                    "out_dir",
                    "scan.clean_up",
                    "scan.language",
                    "scan.mode",
                    "scan.quality",
                    "scan.remove_blank",
                    "scan.searchable",
                    "scan.show_through",
                    "scan.straighten",
                    "scan.upright",
                    "window.review_after_scan",
                    "window.show_when_paper_loaded",
                ],
            ),
        )
        self.assertEqual(
            [
                sorted(m.value for m in kind)
                for kind in (ScanMode, Quality, ShowThrough)
            ],
            [
                sorted(
                    ["auto", "black-and-white", "gray", "color", "photo", "as-scanned"]
                ),
                ["draft", "high", "standard"],
                ["every-page", "gray-and-color", "keep"],
            ],
        )

    def test_a_home_folder_that_is_not_there_is_refused_not_a_crash(self):
        self.path.write_text('out_dir = "~nosuchuser-open-scansnap/Scans"\n')
        with self.assertRaisesRegex(config.ConfigError, "out_dir"):
            config.load(self.path)

    def test_saving_into_a_later_versions_file_is_refused(self):
        # this version's values may not be that version's, which reads its
        # own format strictly: its file is left as it is, and why is said
        text = (
            f'version = {config.FORMAT + 1}\nwindow = "compact"\n[scan]\nsharpen = 3\n'
        )
        self.path.write_text(text)
        with self.assertLogs("openscansnap.config", "WARNING"):
            self.assertTrue(config.load(self.path).show_when_paper_loaded)
        with self.assertRaisesRegex(config.ConfigError, "later version"):
            config.save("debug", True, self.path)
        self.assertEqual(self.path.read_text(), text)

    def test_a_file_that_is_not_utf_8_is_refused_not_a_crash(self):
        self.path.write_bytes(b"debug = true\n# \xff\n")
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_a_folder_name_with_a_nul_is_refused(self):
        for out_dir in ("~a\\u0000", "/Scans\\u0000"):
            self.path.write_text(f'out_dir = "{out_dir}"\n')
            with self.subTest(out_dir=out_dir), self.assertRaises(config.ConfigError):
                config.load(self.path)

    def test_a_linked_file_is_upgraded_in_memory_only(self):
        # a dotfile manager's link: replacing it would cut the link
        target = self.path.with_name("managed.toml")
        target.write_text("[scan]\nremove_show_through = false\n")
        self.path.symlink_to(target)
        self.assertEqual(config.load(self.path).options.show_through, ShowThrough.KEEP)
        self.assertTrue(self.path.is_symlink())
        self.assertNotIn("version", target.read_text())

    def test_an_upgrade_that_cannot_be_saved_is_read_all_the_same(self):
        self.path.write_text("[scan]\nremove_show_through = false\n")
        with (
            mock.patch.object(config.files, "replace_file", side_effect=OSError("ro")),
            self.assertLogs("openscansnap.config", "WARNING"),
        ):
            options = config.load(self.path).options
        self.assertEqual(options.show_through, ShowThrough.KEEP)
        self.assertNotIn("version", self.path.read_text())

    def test_no_file_is_written_by_reading(self):
        config.load(self.path)
        self.assertFalse(self.path.exists())

    def test_every_format_has_its_upgrade(self):
        self.assertEqual(set(config._UPGRADES), set(range(2, config.FORMAT + 1)))

    def test_a_version_that_is_no_number_is_refused(self):
        self.path.write_text('version = "two"\n')
        with self.assertRaisesRegex(config.ConfigError, "version must be a number"):
            config.load(self.path)


if __name__ == "__main__":
    unittest.main()
