"""The command line: its flags override the settings file, for one scan."""

import argparse
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openscansnap import __main__ as cli
from openscansnap import config, job, sane
from openscansnap.options import Quality, ScanMode

FILE = '[scan]\nmode = "color"\nlanguage = "eng+nld"\nsearchable = false\n'


class ScanFlagsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.toml"
        path.write_text(FILE)
        load = config.load
        for patcher in (
            mock.patch.object(config, "load", lambda: load(path)),
            mock.patch.object(job, "run", return_value=Path("scan.pdf")),
            mock.patch.object(cli, "connected_scanner", return_value=sane.USB_DEVICE),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.file_options = load(path).options

    def scan(self, **flags):
        args = {
            "out_dir": None,
            "mode": None,
            "quality": None,
            "language": None,
        } | flags
        with mock.patch("builtins.print"):
            cli.scan(argparse.Namespace(**args))
        return job.run.call_args.args[1]

    def test_flags_override_the_settings_file(self):
        options = self.scan(mode="gray", quality="draft", language="deu")
        self.assertEqual(
            options,
            replace(
                self.file_options,
                mode=ScanMode.GRAY,
                quality=Quality.DRAFT,
                language="deu",
            ),
        )
        self.assertFalse(options.searchable)  # from the file

    def test_scans_the_scanner_connected(self):
        self.scan()
        self.assertEqual(job.run.call_args.args[2], sane.USB_DEVICE)

    def test_without_flags_the_settings_file_decides(self):
        self.assertEqual(self.scan(), self.file_options)
        self.assertEqual(self.file_options.mode, ScanMode.COLOR)

    def test_out_dir_flag_overrides_the_settings_file(self):
        self.scan(out_dir="~/Scans")
        self.assertEqual(job.run.call_args.args[0], Path.home() / "Scans")
        self.scan()
        self.assertEqual(job.run.call_args.args[0], config.load().out_dir)

    def test_flags_parse_as_the_command_line_spells_them(self):
        argv = ["openscansnap", "scan", "--out-dir", "/tmp/x", "--mode", "gray"]
        argv += ["--quality", "draft", "--language", "deu"]
        with (
            mock.patch("sys.argv", argv),
            mock.patch("builtins.print"),
            mock.patch.object(cli.bundle, "use_bundled_tools"),
        ):
            cli.main()
        out_dir, options = job.run.call_args.args[:2]
        self.assertEqual(out_dir, Path("/tmp/x"))
        self.assertEqual(
            (options.mode, options.quality, options.language), ("gray", "draft", "deu")
        )

    def test_a_language_that_is_no_language_is_refused(self):
        with self.assertRaisesRegex(sane.ScanError, "--language"):
            self.scan(language="eng+")


class RemedyTest(unittest.TestCase):
    """Problems whose fix differs in the app say the command line's."""

    def test_each_problem_says_its_command(self):
        for error, command in (
            (sane.NotFound("No scanner found"), "--host"),
            (sane.NotSetUp("No scanner set up"), "openscansnap setup"),
            (sane.Refused("refused"), "setup --password"),
            (sane.BadConfiguration("fujitsu.conf: x"), "Delete the file"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertIn(command, cli.remedy(error))
        self.assertEqual(cli.remedy(sane.ScanError("No paper in the scanner")), "")

    def test_the_message_and_its_remedy_are_printed(self):
        with (
            mock.patch("sys.argv", ["openscansnap", "scan"]),
            mock.patch.object(cli.bundle, "use_bundled_tools"),
            mock.patch.object(
                cli, "scan", side_effect=sane.NotSetUp("No scanner set up")
            ),
            mock.patch.object(cli, "fail") as fail,
        ):
            cli.main()
        fail.assert_called_once_with(
            "No scanner set up. Run: python -m openscansnap setup"
        )


if __name__ == "__main__":
    unittest.main()
