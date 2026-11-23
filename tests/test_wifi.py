"""SANE's fujitsu backend over Wi-Fi, against the fake iX500 on localhost.
Needs the patched SANE built into sane-prefix/ (see README)."""

import argparse
import contextlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fake_ix500
import numpy as np
import pikepdf
from fake_ix500 import FakeIX500, Scanner
from PIL import Image, ImageDraw, ImageFont
from test_pages import LETTER, text_page

from openscansnap import __main__ as cli
from openscansnap import config, job, monitor, pages, sane, saneapi

HOSTID = 0x1122334455667788
WIFI_DEVICE = sane.DEVICE_PREFIX + fake_ix500.HOST
KEEP_ALIVE_WITHIN = 12  # seconds: the backend's VENS_KEEPALIVE_SECONDS, and some
DEVICE = sane.DEVICE_PREFIX + fake_ix500.HOST
BACKING = (232, 232, 232)  # the scanner's gray backing around the sheet
SCAN_AREA = (LETTER[0] + 58, LETTER[1] + 100)
LOCALLY_ADMINISTERED = 0x02  # first byte of a MAC that no vendor assigned
RED, BLUE = (220, 30, 30), (30, 60, 220)
POINTS_PER_INCH = 72


def side(page: Image.Image | None) -> bytes:
    """A side as the scanner sends it: the sheet on the backing, as JPEG."""
    area = Image.new("RGB", SCAN_AREA, BACKING)
    area.paste(page or Image.new("RGB", LETTER, (250, 250, 250)), (29, 40))
    return jpeg(area)


def marked(word: str) -> Image.Image:
    """A text page with word in large type at the top."""
    page = text_page()
    ImageDraw.Draw(page).text(
        (300, 150), word, fill=(0, 0, 0), font=ImageFont.load_default(size=120)
    )
    return page


def jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90, dpi=(300, 300))
    return out.getvalue()


def color_of(path: Path) -> str:
    with Image.open(path) as image:
        r, _, b = np.asarray(image.convert("RGB")).reshape(-1, 3).mean(0)
    return "red" if r > b else "blue"


@unittest.skipUnless(sane.SCANIMAGE.exists(), "needs SANE built into sane-prefix/")
class WifiTest(unittest.TestCase):
    def setUp(self):
        # no USB lines: the tests must not find a real scanner on USB
        usb = mock.patch.object(sane, "USB_LINES", ())
        usb.start()
        self.addCleanup(usb.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "sane.d"
        self.configure(HOSTID)

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, hostid, password=None):
        with mock.patch.object(sane, "CONFIG_DIR", self.config):
            sane.write_config(sane.Scanner(fake_ix500.HOST, hostid, password))

    def scanimage(self, *args: str, debug: int = 0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(sane.SCANIMAGE), *args],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SANE_CONFIG_DIR": str(self.config),
                "SANE_DEBUG_FUJITSU": str(debug),
            },
            timeout=120,
        )

    def scan_sides(
        self, scanner: Scanner, *options: str
    ) -> tuple[subprocess.CompletedProcess, list[Path]]:
        out = Path(self.tmp.name) / "out"
        out.mkdir(exist_ok=True)
        with FakeIX500(scanner):
            result = self.scanimage(
                "-d", DEVICE, *options, f"--batch={out / sane.PAGE_PATTERN}"
            )
        return result, sorted(out.glob("page*.tif"))

    def sane_scan(self, scanner: Scanner) -> list[Path]:
        with (
            FakeIX500(scanner),
            mock.patch.object(sane, "CONFIG_DIR", self.config),
            tempfile.TemporaryDirectory() as work,
        ):
            return sane.scan(Path(work), WIFI_DEVICE)

    def listed(self, scanner: Scanner) -> bool:
        with FakeIX500(scanner):
            return DEVICE in self.scanimage("-L").stdout

    def test_scanner_is_listed(self):
        self.assertTrue(self.listed(Scanner(HOSTID)))

    def test_wrong_hostid_is_reported(self):
        self.configure(HOSTID + 1)
        with self.assertRaisesRegex(sane.ScanError, "refused the password or HostID"):
            self.sane_scan(Scanner(HOSTID, sheets=[(side(None), side(None))]))

    def test_wrong_password_is_reported(self):
        with self.assertRaisesRegex(sane.ScanError, "refused the password or HostID"):
            self.sane_scan(
                Scanner(HOSTID, password="9999", sheets=[(side(None), side(None))])
            )

    def test_scanner_held_by_another_client_is_reported_busy(self):
        with self.assertRaisesRegex(sane.ScanError, "busy: quit ScanSnap Home"):
            self.sane_scan(
                Scanner(HOSTID, holder=b"\x01" * 6, sheets=[(side(None), side(None))])
            )

    def test_empty_feeder_is_reported(self):
        scanner = Scanner(HOSTID)
        with self.assertRaisesRegex(sane.ScanError, "No paper"):
            self.sane_scan(scanner)
        self.assertIsNone(scanner.holder)

    def test_unreachable_scanner_is_an_io_error(self):
        with (
            mock.patch.object(sane, "CONFIG_DIR", self.config),
            tempfile.TemporaryDirectory() as work,
            self.assertRaisesRegex(sane.ScanError, "Error during device I/O"),
        ):
            sane.scan(Path(work), WIFI_DEVICE)  # no scanner answers

    def test_reply_longer_than_asked_is_rejected(self):
        scanner = Scanner(HOSTID, oversize=10)
        with FakeIX500(scanner):
            result = self.scanimage("-L", debug=5)
        self.assertEqual(result.returncode, 0, result.stderr)  # rejected, not crashed
        self.assertNotIn(DEVICE, result.stdout)
        self.assertIn("bad reply (length", result.stderr)

    def own_mac(self) -> bytes:
        scanner = Scanner(HOSTID)
        self.listed(scanner)
        (mac,) = scanner.macs
        return mac

    def test_own_stale_reservation_is_released(self):
        scanner = Scanner(HOSTID, holder=self.own_mac())
        self.assertTrue(self.listed(scanner))
        self.assertIsNone(scanner.holder)

    def test_client_without_hardware_address_has_a_stable_host_mac(self):
        # loopback has no MAC, as macOS hides the real one: the backend
        # derives one from the host, the same every time
        mac = self.own_mac()
        self.assertEqual(mac[0], LOCALLY_ADMINISTERED)
        self.assertNotEqual(mac[1:], bytes(5))
        self.assertEqual(self.own_mac(), mac)

    def test_duplex_scan_uses_only_wifi_commands(self):
        scanner = Scanner(
            HOSTID,
            sheets=[
                (side(None), side(text_page())),
                (side(text_page()), side(text_page())),
            ],
        )
        result, sides = self.scan_sides(scanner, *sane.SCAN_OPTIONS)
        self.assertEqual(
            result.returncode, 0, result.stderr
        )  # the batch ends when the feeder empties
        self.assertEqual(len(sides), 4)
        for path in sides:
            with Image.open(path) as image:  # whole: pages.prepare() crops
                self.assertEqual(image.size, SCAN_AREA)
        self.assertEqual(scanner.rejected, [])  # only commands the firmware takes
        self.assertEqual(scanner.unreserved, [])
        job = [op for op in scanner.opcodes if op in (0xD5, 0xD8, 0xE9, 0xD4, 0xE0)]
        self.assertEqual(job[:5], [0xD5, 0xD8, 0xE9, 0xD4, 0xE0])
        windows = [fake_ix500.FRONT_WINDOW, fake_ix500.BACK_WINDOW] * 2
        self.assertEqual(scanner.reads, list(enumerate(windows)))  # sequence 0, 1, 2, 3
        self.assertEqual(scanner.opcodes[-1], fake_ix500.END_JOB)
        self.assertIsNone(scanner.holder)  # released

    def scan_colors(self, source: str) -> list[str]:
        sheet = (
            side(Image.new("RGB", LETTER, RED)),
            side(Image.new("RGB", LETTER, BLUE)),
        )
        scanner = Scanner(HOSTID, sheets=[sheet, sheet])
        options = (
            "--source",
            source,
            "--mode",
            "Color",
            "--format=tiff",
        )
        result, sides = self.scan_sides(scanner, *options)
        self.assertEqual(result.returncode, 0, result.stderr)
        # every side is read, in order, whatever is kept
        windows = [fake_ix500.FRONT_WINDOW, fake_ix500.BACK_WINDOW] * 2
        self.assertEqual(scanner.reads, list(enumerate(windows)))
        self.assertEqual(scanner.opcodes[-1], fake_ix500.END_JOB)
        return [color_of(path) for path in sides]

    def test_duplex_sides_arrive_front_then_back(self):
        self.assertEqual(self.scan_colors("ADF Duplex"), ["red", "blue", "red", "blue"])

    def test_adf_front_keeps_only_fronts(self):
        self.assertEqual(self.scan_colors("ADF Front"), ["red", "red"])

    def test_adf_back_keeps_only_backs(self):
        self.assertEqual(self.scan_colors("ADF Back"), ["blue", "blue"])

    def assert_side_fails_cleanly(self, scanner: Scanner):
        result, sides = self.scan_sides(scanner, *sane.SCAN_OPTIONS)
        self.assertGreater(result.returncode, 0, result.stderr)  # an error, not a crash
        self.assertNotEqual(result.returncode, sane.EXIT_NO_DOCS, result.stderr)
        self.assertEqual(sides, [])
        self.assertEqual(scanner.opcodes[-1], fake_ix500.END_JOB)
        self.assertIsNone(scanner.holder)

    def test_undecodable_side_fails_cleanly(self):
        garbage = b"\xff\xd8garbage\xff\xd9"
        self.assert_side_fails_cleanly(Scanner(HOSTID, sheets=[(garbage, garbage)]))

    def test_truncated_side_fails_cleanly(self):
        whole = side(text_page())
        cut = whole[: len(whole) // 2] + b"\xff\xd9"
        self.assert_side_fails_cleanly(Scanner(HOSTID, sheets=[(cut, cut)]))

    def test_side_larger_than_the_scan_area_fails_cleanly(self):
        longest = 17628 * 300 // 1200  # VENS_AREA_LENGTH at 300 dpi
        for size in ((SCAN_AREA[0] + 200, SCAN_AREA[1]), (SCAN_AREA[0], longest + 100)):
            with self.subTest(size=size):
                huge = jpeg(Image.new("RGB", size, BACKING))
                self.assert_side_fails_cleanly(Scanner(HOSTID, sheets=[(huge, huge)]))

    def test_bogus_residual_is_ignored(self):
        # a residual larger than the READ must not turn into a negative length
        scanner = Scanner(
            HOSTID, residual=2**31, sheets=[(side(text_page()), side(text_page()))]
        )
        result, sides = self.scan_sides(scanner, *sane.SCAN_OPTIONS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(sides), 2)

    @unittest.skipUnless(
        shutil.which(pages.TESSERACT) and shutil.which("pdftotext"),
        "needs Tesseract and pdftotext",
    )
    def test_scan_command_writes_pdf_without_blank_sides(self):
        # the blank side is not first; each page is told apart by its word
        sheets = [
            (side(marked("ALPHA")), side(None)),
            (side(marked("BRAVO")), side(marked("CHARLIE"))),
        ]
        out_dir = Path(self.tmp.name) / "pdfs"
        output = io.StringIO()
        with (
            FakeIX500(Scanner(HOSTID, sheets=sheets)),
            mock.patch.object(sane, "CONFIG_DIR", self.config),
            mock.patch.object(config, "PATH", self.config.parent / "config.toml"),
            contextlib.redirect_stdout(output),
        ):
            cli.scan(
                argparse.Namespace(
                    out_dir=str(out_dir), mode=None, quality=None, language="eng"
                )
            )
        (out,) = out_dir.glob("scan_*.pdf")
        self.assertEqual(output.getvalue(), f"Saved {out}\n")
        with pikepdf.open(out) as doc:
            self.assertEqual(len(doc.pages), 3)
            for page in doc.pages:
                width = float(page.mediabox[2]) - float(page.mediabox[0])
                self.assertAlmostEqual(
                    width, LETTER[0] / 300 * POINTS_PER_INCH, delta=5
                )
                (image,) = page.images.values()
                self.assertEqual(int(image.BitsPerComponent), 1)  # black and white
        text = subprocess.run(
            ["pdftotext", str(out), "-"], capture_output=True, text=True, check=True
        ).stdout
        words = [
            next(w for w in ("ALPHA", "BRAVO", "CHARLIE") if w in page)
            for page in text.split("\f")[:3]
        ]
        self.assertEqual(words, ["ALPHA", "BRAVO", "CHARLIE"])

    def run_job(self, scanner: Scanner) -> Path:
        with (
            FakeIX500(scanner),
            mock.patch.object(sane, "CONFIG_DIR", self.config),
        ):
            return job.run(
                Path(self.tmp.name) / "pdfs", config.default_options(), WIFI_DEVICE
            )

    @unittest.skipUnless(
        shutil.which(pages.TESSERACT) and shutil.which("pdftotext"),
        "needs Tesseract and pdftotext",
    )
    def test_sides_scanned_before_a_failure_are_saved(self):
        garbage = b"\xff\xd8garbage\xff\xd9"
        scanner = Scanner(
            HOSTID, sheets=[(side(marked("ALPHA")), side(None)), (garbage, garbage)]
        )
        with self.assertRaisesRegex(sane.ScanError, "saved the sides scanned before"):
            self.run_job(scanner)
        (out,) = (Path(self.tmp.name) / "pdfs").glob("scan_*.pdf")
        text = subprocess.run(
            ["pdftotext", str(out), "-"], capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("ALPHA", text)
        self.assertIsNone(scanner.holder)

    @unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
    def test_scanned_sides_are_kept_when_the_pdf_cannot_be_written(self):
        scanner = Scanner(HOSTID, sheets=[(side(marked("ALPHA")), side(None))])
        out_dir = Path(self.tmp.name) / "pdfs"
        with (
            mock.patch.object(job.pdf, "write", side_effect=OSError("disk full")),
            self.assertRaisesRegex(sane.ScanError, "disk full") as raised,
        ):
            self.run_job(scanner)
        self.assertEqual(list(out_dir.glob("scan_*.pdf")), [])  # no empty PDF left
        (kept,) = out_dir.glob("scan_*_sides_*")
        self.assertIn(f"The scanned sides are in {kept}", str(raised.exception))
        self.assertEqual(len(list(kept.glob("page*.tif"))), 2)

    @unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
    def test_scanned_sides_survive_an_unwritable_out_dir(self):
        scanner = Scanner(HOSTID, sheets=[(side(marked("ALPHA")), side(None))])
        out_dir = Path(self.tmp.name) / "pdfs"
        out_dir.mkdir()
        out_dir.chmod(0o500)
        try:
            with self.assertRaises(sane.ScanError) as raised:
                self.run_job(scanner)
        finally:
            out_dir.chmod(0o700)
        found = re.search(r"The scanned sides are in (.+)", str(raised.exception))
        self.assertIsNotNone(found, str(raised.exception))
        kept = Path(found[1])
        self.addCleanup(shutil.rmtree, kept, True)
        self.assertEqual(len(list(kept.glob("page*.tif"))), 2)

    def wait_for(self, watcher: monitor.Monitor, done, seconds: float = 120):
        deadline = time.monotonic() + seconds
        while not done(watcher.snapshot):
            self.assertLess(time.monotonic(), deadline, watcher.snapshot)
            time.sleep(0.05)
        return watcher.snapshot

    def watch(self, scanner: Scanner, out_dir: Path) -> monitor.Monitor:
        """A monitor of the fake scanner, stopped at the end of the test."""
        patch = mock.patch.object(sane, "CONFIG_DIR", self.config)
        patch.start()
        fake = FakeIX500(scanner).__enter__()
        connect = monitor.Connector()

        def scan(device_name):
            batch = job.scan(config.default_options(), device_name)
            return lambda: job.finish(batch, out_dir, config.default_options())

        watcher = monitor.Monitor(
            connect,
            scan,
            poll=0.05,
            retry=0.2,
        )
        watcher.start()

        def stop():
            watcher.stop(30)
            connect.exit()
            fake.__exit__(None, None, None)
            patch.stop()

        self.addCleanup(stop)
        return watcher

    def test_sensors_are_read_through_libsane(self):
        scanner = Scanner(HOSTID, sheets=[(side(None), side(None))], presses=1)
        with FakeIX500(scanner), mock.patch.object(sane, "CONFIG_DIR", self.config):
            library = saneapi.Library()
            try:
                device = library.open(DEVICE)
                readings = [device.read(("scan", "page-loaded")) for _ in range(3)]
                device.close()
            finally:
                library.exit()
        self.assertEqual([r["page-loaded"] for r in readings], [1, 1, 1])
        self.assertEqual([r["scan"] for r in readings], [1, 0, 0])  # one press, once
        self.assertIsNone(scanner.holder)  # closing releases the scanner

    @unittest.skipUnless(
        shutil.which(pages.TESSERACT) and shutil.which("pdftotext"),
        "needs Tesseract and pdftotext",
    )
    def test_scan_button_makes_a_pdf(self):
        scanner = Scanner(HOSTID, sheets=[(side(marked("ALPHA")), side(None))])
        out_dir = Path(self.tmp.name) / "pdfs"
        watcher = self.watch(scanner, out_dir)
        self.wait_for(watcher, lambda s: s.state == monitor.State.PAPER)
        with scanner.lock:
            scanner.presses = 1
        done = self.wait_for(watcher, lambda s: s.message.startswith("Saved"))
        (out,) = out_dir.glob("scan_*.pdf")
        self.assertEqual(done.message, f"Saved {out.name}")
        text = subprocess.run(
            ["pdftotext", str(out), "-"], capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("ALPHA", text)

    def test_paper_put_in_the_feeder_is_noticed(self):
        scanner = Scanner(HOSTID)
        watcher = self.watch(scanner, Path(self.tmp.name) / "pdfs")
        self.wait_for(watcher, lambda s: s.state == monitor.State.READY)
        with scanner.lock:
            scanner.sheets.append((side(None), side(None)))
        snapshot = self.wait_for(watcher, lambda s: s.state == monitor.State.PAPER)
        self.assertEqual(snapshot.paper_loads, 1)

    def test_open_scanner_is_kept_alive(self):
        # the iX500 ends a reservation 40 s after it began without keep-alives
        scanner = Scanner(HOSTID)
        with (
            FakeIX500(scanner),
            mock.patch.object(sane, "CONFIG_DIR", self.config),
        ):
            library = saneapi.Library(config_dir=self.config)
            try:
                device = library.open(WIFI_DEVICE)
                deadline = time.monotonic() + KEEP_ALIVE_WITHIN
                while not scanner.keep_alives and time.monotonic() < deadline:
                    device.read((monitor.SCAN_BUTTON, monitor.PAPER))
                    time.sleep(0.2)
                device.close()
            finally:
                library.exit()
        self.assertGreater(scanner.keep_alives, 0)

    def test_scanner_held_by_another_app_shows_as_busy(self):
        watcher = self.watch(Scanner(HOSTID, holder=b"\x01" * 6), Path(self.tmp.name))
        self.wait_for(watcher, lambda s: s.state == monitor.State.BUSY)

    def test_debug_mode_keeps_the_raw_sides(self):
        scanner = Scanner(HOSTID, sheets=[(side(marked("ALPHA")), side(None))])
        raw = Path(self.tmp.name) / "raw"
        with (
            FakeIX500(scanner),
            mock.patch.object(sane, "CONFIG_DIR", self.config),
            mock.patch.object(job, "assemble", return_value=None),  # no OCR needed
            self.assertRaisesRegex(sane.ScanError, "blank"),
        ):
            job.run(
                Path(self.tmp.name) / "pdfs",
                config.default_options(),
                WIFI_DEVICE,
                keep_raw=raw,
            )
        (kept,) = raw.iterdir()
        sides = sorted(kept.glob("page*.tif"))
        self.assertEqual(len(sides), 2)
        with Image.open(sides[0]) as image:
            self.assertEqual(image.size, SCAN_AREA)  # as the scanner sent it
