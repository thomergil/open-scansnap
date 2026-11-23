"""SANE configuration written for the Wi-Fi scanner, and the scan command's
checks before it scans."""

import plistlib
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openscansnap import config, files, job, sane

HOST, HOSTID = "192.168.1.20", 0x1122334455667788


class ScannerTest(unittest.TestCase):
    def test_rejects_what_the_backend_cannot_use(self):
        for host, hostid, password in [
            (
                "scanner.local\nwifi 10.0.0.1",
                HOSTID,
                None,
            ),  # not an address; would add a line
            ("::1", HOSTID, None),  # the backend speaks IPv4 only
            ("fe80::1%x\noption wifi-password 1", HOSTID, None),
            (HOST, 0, None),
            (HOST, 2**64, None),
            (HOST, HOSTID, "has space"),
            (HOST, HOSTID, "x" * (sane.PASSWORD_MAX + 1)),
        ]:
            with (
                self.subTest(host=host, hostid=hostid, password=password),
                self.assertRaises(sane.ScanError),
            ):
                sane.Scanner(host, hostid, password)

    def test_config_names_the_scanner(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(sane, "CONFIG_DIR", Path(tmp) / "sane.d"),
        ):
            scanner = sane.Scanner(HOST, HOSTID, "1234", "00:80:92:00:be:ef")
            conf = sane.write_config(scanner)
            self.assertEqual(
                conf.read_text().splitlines(),
                [
                    *sane.USB_LINES,
                    "option wifi-hostid 0x1122334455667788",
                    "option wifi-password 1234",
                    "# scanner 00:80:92:00:be:ef",
                    f"wifi {HOST}",
                ],
            )
            self.assertEqual(sane.configured(), scanner)
            self.assertEqual(sane.device(scanner), sane.DEVICE_PREFIX + HOST)

    def test_config_without_password_or_address_reads_back(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(sane, "CONFIG_DIR", Path(tmp) / "sane.d"),
        ):
            scanner = sane.Scanner(HOST, HOSTID)
            sane.write_config(scanner)
            self.assertEqual(sane.configured(), scanner)

    def test_missing_config_is_none(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(sane, "CONFIG_DIR", Path(tmp)),
        ):
            self.assertIsNone(sane.configured())

    def test_damaged_config_is_reported_not_ignored(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(sane, "CONFIG_DIR", Path(tmp)),
        ):
            conf = Path(tmp) / f"{sane.BACKEND}.conf"
            for text in (
                f"wifi {HOST}\n",
                "option wifi-hostid zz\nwifi 10.0.0.1\n",
                "option wifi-hostid 0x1\nwifi scanner.local\n",
                "option wifi-hostid 0x1\noption wifi-password "
                + "x" * 17
                + "\nwifi 10.0.0.1\n",
            ):
                with (
                    self.subTest(text=text),
                    self.assertRaisesRegex(sane.ScanError, str(conf)),
                ):
                    conf.write_text(text)
                    sane.configured()

    def test_scansnap_home_settings_of_any_shape(self):
        info = {"AutoConnectInfo_ScannerInfo": {"HostID": HOSTID}}
        bad = {"AutoConnectInfo_ScannerInfo": {"HostID": "x"}}
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "settings.plist"
            for settings, hostid in (
                ({"Common": {"Wifi Connected Scanner": [{}, info]}}, HOSTID),
                ({"Common": {"Wifi Connected Scanner": []}}, None),
                ({"Common": "not a dictionary"}, None),
                ({"Common": {"Wifi Connected Scanner": [bad]}}, None),
            ):
                with (
                    self.subTest(settings=settings),
                    mock.patch.object(sane, "SCANSNAP_HOME_SETTINGS", plist),
                ):
                    plist.write_bytes(plistlib.dumps(settings))
                    self.assertEqual(sane.scansnap_home_hostid(), hostid)
            for settings in (
                {"Common": {"Wifi Connected Scanner": [bad, info]}},  # a bad one first
                {
                    "Common": {
                        "Wifi Connected Scanner": [
                            [1],
                            {"AutoConnectInfo_ScannerInfo": []},
                            info,
                        ]
                    }
                },
            ):
                with (
                    self.subTest(settings=settings),
                    mock.patch.object(sane, "SCANSNAP_HOME_SETTINGS", plist),
                ):
                    plist.write_bytes(plistlib.dumps(settings))
                    self.assertEqual(sane.scansnap_home_hostid(), HOSTID)
            for content in (
                b"<?xml version='1.0'?><plist><dict><key>",
                b"\x00\x01junk",
            ):
                with (
                    self.subTest(content=content),
                    mock.patch.object(sane, "SCANSNAP_HOME_SETTINGS", plist),
                ):
                    plist.write_bytes(content)
                    self.assertIsNone(sane.scansnap_home_hostid())
            with mock.patch.object(sane, "SCANSNAP_HOME_SETTINGS", Path(tmp) / "none"):
                self.assertIsNone(sane.scansnap_home_hostid())

    def test_unreadable_config_is_reported_not_taken_as_missing(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(sane, "CONFIG_DIR", Path(tmp)),
        ):
            conf = Path(tmp) / f"{sane.BACKEND}.conf"
            for name, make in (
                ("not UTF-8", lambda: conf.write_bytes(b"wifi \xff\xfe\n")),
                ("a folder", conf.mkdir),
            ):
                with self.subTest(name):
                    conf.unlink(missing_ok=True)
                    make()
                    with self.assertRaisesRegex(sane.BadConfiguration, str(conf)):
                        sane.configured()

    def test_config_for_usb_only_is_no_wi_fi_scanner(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(sane, "CONFIG_DIR", Path(tmp) / "sane.d"),
        ):
            sane.ensure_config()
            conf = sane.CONFIG_DIR / f"{sane.BACKEND}.conf"
            self.assertEqual(conf.read_text().splitlines(), list(sane.USB_LINES))
            self.assertIsNone(sane.configured())

    def test_usb_lines_are_added_to_a_config_without_them(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(sane, "CONFIG_DIR", Path(tmp) / "sane.d"),
        ):
            scanner = sane.Scanner(HOST, HOSTID, "1234", "00:80:92:00:be:ef")
            with mock.patch.object(sane, "USB_LINES", ()):
                conf = sane.write_config(scanner)  # as written before USB
            sane.ensure_config()
            self.assertTrue(set(sane.USB_LINES) <= set(conf.read_text().splitlines()))
            self.assertEqual(sane.configured(), scanner)  # the rest kept

    def test_config_is_private_even_if_it_was_not(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(sane, "CONFIG_DIR", Path(tmp) / "sane.d"),
        ):
            sane.CONFIG_DIR.mkdir()
            sane.CONFIG_DIR.chmod(0o755)  # explicitly, whatever the umask
            old = sane.CONFIG_DIR / f"{sane.BACKEND}.conf"
            old.write_text("wifi 10.0.0.1\n")
            old.chmod(0o644)
            conf = sane.write_config(sane.Scanner(HOST, HOSTID))
            self.assertEqual(stat.S_IMODE(conf.stat().st_mode), files.PRIVATE_FILE)
            self.assertEqual(
                stat.S_IMODE(sane.CONFIG_DIR.stat().st_mode), files.PRIVATE_DIR
            )
            self.assertEqual(conf.read_text().splitlines()[-1], f"wifi {HOST}")


class ScanCommandTest(unittest.TestCase):
    def test_missing_language_is_reported_before_any_paper_moves(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                sane, "scan", side_effect=AssertionError("scanned")
            ) as scan,
            self.assertRaisesRegex(sane.ScanError, "zzz"),
        ):
            job.run(
                Path(tmp),
                replace(config.default_options(), language="eng+zzz"),
                sane.USB_DEVICE,
            )
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
