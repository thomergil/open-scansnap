"""The review store: the latest scan kept, its PDF made again as chosen."""

import json
import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np
import pypdfium2
from PIL import Image, ImageDraw

from openscansnap import config, files, job, pages, review, sane
from openscansnap.options import ScanMode, ShowThrough

DPI = 300
SIZE = (850, 1100)  # pixels: small sides keep the store's writes quick


def side(path: Path, tone: int) -> Path:
    Image.new("RGB", SIZE, (tone, tone, tone)).save(path, dpi=(DPI, DPI))
    return path


MARK = (200, 300, 650, 800)  # a black box drawn on one kept side


def mark(scan: review.Scan, number: int) -> Image.Image:
    """Draw MARK on side number of scan, as stored; the side as marked."""
    path = scan.side_image(number)
    with Image.open(path) as image:
        marked = image.convert("RGB")
    ImageDraw.Draw(marked).rectangle(MARK, fill="black")
    marked.save(path)
    return marked


def keep(store: review.Store, tmp: Path, stem="scan_1", sides=2) -> review.Scan:
    """A scan of sides, made in tmp, the last side left out as blank, kept in
    store."""
    made = tmp / stem
    (made / "sides").mkdir(parents=True)
    (made / "pages").mkdir()
    scanned, results = [], []
    for number in range(1, sides + 1):
        scanned.append(side(made / "sides" / f"page{number:04d}.tif", 200))
        if number < sides:
            page = made / "pages" / f"{number:04d}.jpg"
            Image.new("RGB", SIZE, "white").save(page, dpi=(DPI, DPI))
            results.append(job.SideResult(number, pages.Mode.COLOR, "colorful", page))
        else:
            results.append(job.SideResult(number, None, job.BLANK, None))
    assembled = job.Assembled([r.page for r in results if r.page], "eng", results)
    batch = job.Batch(stem, made / "sides", scanned, None)
    options = replace(config.default_options(), language="eng")
    pdf = tmp / f"{stem}.pdf"
    pdf.write_bytes(b"the first PDF")
    return store.keep(pdf, assembled, batch, options)


class StoredScan(unittest.TestCase):
    """A store in a temporary folder, and scans to keep in it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = review.Store(self.tmp / "review")
        self.options = replace(config.default_options(), language="eng")

    def keep(self, stem="scan_1", sides=2) -> review.Scan:
        return keep(self.store, self.tmp, stem, sides)


class StoreTest(StoredScan):
    def test_a_kept_scan_reads_back_whole(self):
        kept = self.keep()
        self.assertEqual(self.store.latest(), kept)
        self.assertEqual([s.mode for s in kept.sides], [pages.Mode.COLOR, None])
        for number in (1, 2):
            self.assertTrue(kept.side_image(number).is_file())
            self.assertTrue(kept.thumbnail(number).is_file())
        self.assertIsNotNone(kept.page(1))
        self.assertIsNone(kept.page(2))  # left out: no page
        with Image.open(kept.side_image(1)) as image:  # the scanner's, unchanged
            self.assertEqual(image.getpixel((5, 5)), (200, 200, 200))

    def test_only_the_latest_and_held_scans_stay(self):
        first = self.keep("scan_1")
        self.store.hold(first)
        second = self.keep("scan_2")
        third = self.keep("scan_3")
        self.assertEqual(
            {scan.name for scan in self.store.scans()}, {first.name, third.name}
        )
        self.store.release(first)
        self.store.prune()
        self.assertEqual([scan.name for scan in self.store.scans()], [third.name])
        self.assertFalse(second.folder.exists())

    def test_a_half_written_scan_is_cleared(self):
        self.keep()
        leftover = self.store.root / f"{review.NEW_PREFIX}x"
        leftover.mkdir()
        self.store.prune()
        self.assertFalse(leftover.exists())

    def test_the_store_is_private(self):
        self.store.root.mkdir(mode=0o755)  # made wider before: tightened
        kept = self.keep()
        self.assertEqual(self.store.root.stat().st_mode & 0o777, files.PRIVATE_DIR)
        record = kept.folder / review.RECORD
        self.assertEqual(record.stat().st_mode & 0o777, files.PRIVATE_FILE)

    def test_the_scanners_sides_are_kept_losslessly(self):
        folder = self.tmp / "scan"
        for part in (review.SIDES, review.PAGES, review.THUMBS):
            (folder / part).mkdir(parents=True)
        noise = np.random.default_rng(0).integers(0, 256, (400, 300, 3), dtype=np.uint8)
        scanned = self.tmp / "page0001.tif"
        Image.fromarray(noise).save(scanned, dpi=(DPI, DPI))
        review.store_side(folder, scanned, job.SideResult(1, None, job.BLANK, None))
        with Image.open(folder / review.SIDES / "0001.png") as kept:
            self.assertEqual(kept.format, "PNG")
            np.testing.assert_array_equal(np.asarray(kept), noise)
            self.assertEqual(round(kept.info["dpi"][0]), DPI)

    def test_a_scan_is_not_kept_without_room_for_it(self):
        room = mock.Mock(free=review.SIDE_BYTES)  # room for one side of two
        with (
            mock.patch.object(review.shutil, "disk_usage", return_value=room),
            self.assertRaises(OSError),
        ):
            self.keep()
        self.assertEqual(self.store.scans(), [])

    def test_a_scan_whose_pdf_was_deleted_goes(self):
        kept = self.keep()
        kept.pdf.unlink()
        self.store.prune()
        self.assertEqual(self.store.scans(), [])

    def test_a_scan_no_longer_stored_cannot_be_held(self):
        first = self.keep("scan_1")
        self.keep("scan_2")  # first pruned
        self.assertIsNone(self.store.hold(first))

    def test_only_the_stores_own_folders_are_pruned(self):
        self.keep()
        theirs = self.store.root / "notes"
        theirs.mkdir()
        self.store.prune()
        self.assertTrue(theirs.is_dir())

    def test_a_store_that_is_a_link_is_left_alone(self):
        elsewhere = self.tmp / "elsewhere"
        (elsewhere / "folder").mkdir(parents=True)
        (elsewhere / "folder" / review.RECORD).write_text("{}")
        self.store.root.symlink_to(elsewhere)
        self.store.prune()
        self.assertTrue((elsewhere / "folder").is_dir())
        with self.assertRaises(PermissionError):
            self.keep()

    def test_prune_clears_only_a_remakes_leftovers_beside_the_pdf(self):
        kept = self.keep()
        stem = kept.pdf.stem
        leftover = kept.pdf.with_name(f".{stem}-k2j3.pdf")
        others = [
            kept.pdf.with_name(name)
            for name in (".DS_Store", f".{stem}_2-k2j3.pdf", "notes.pdf")
        ]
        for path in (leftover, *others):
            path.write_bytes(b"x")
        self.store.prune()
        self.assertFalse(leftover.exists())
        self.assertTrue(all(path.exists() for path in others))
        self.assertEqual(kept.pdf.read_bytes(), b"the first PDF")

    def test_a_pdf_on_a_drive_not_mounted_is_not_taken_for_deleted(self):
        kept = self.keep()
        review.write(replace(kept, pdf=Path("/Volumes/not-mounted-oss/scan_1.pdf")))
        self.store.prune()
        self.assertEqual([scan.name for scan in self.store.scans()], [kept.name])

    def test_a_pdf_out_of_reach_is_not_taken_for_deleted(self):
        kept = self.keep()
        stat = os.stat

        def denied(path, *args, **kwargs):
            if Path(path) == kept.pdf:
                raise PermissionError(1, "Operation not permitted")
            return stat(path, *args, **kwargs)

        with mock.patch.object(review.os, "stat", side_effect=denied):
            self.store.prune()
        self.assertEqual([scan.name for scan in self.store.scans()], [kept.name])

    def test_a_record_with_a_choice_no_side_can_have_is_skipped(self):
        kept = self.keep()
        record = kept.folder / review.RECORD
        data = json.loads(record.read_text())
        data["sides"][0]["choice"] = "sideways"
        record.write_text(json.dumps(data))
        with self.assertLogs("openscansnap.review", "WARNING"):
            self.assertEqual(self.store.scans(), [])

    def test_a_record_that_cannot_be_read_is_not_pruned(self):
        kept = self.keep("scan_1")
        (kept.folder / review.RECORD).write_text("{ not a record")
        with self.assertLogs("openscansnap.review", "WARNING"):
            self.keep("scan_2")  # prunes
        self.assertTrue((kept.folder / review.RECORD).is_file())

    def test_a_record_a_later_version_wrote_is_left_to_it(self):
        kept = self.keep("scan_1")
        record = kept.folder / review.RECORD
        data = json.loads(record.read_text())
        # made again here, its PDF would lose that version's settings: it is
        # left for that version, unread, and so never pruned
        data["version"] = config.FORMAT + 1
        data["options"]["setting_of_a_later_version"] = True
        record.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "later version"):
            review.read(kept.folder)
        with self.assertLogs("openscansnap.review", "WARNING"):
            self.keep("scan_2")  # prunes
        self.assertEqual(json.loads(record.read_text()), data)

    def test_a_record_says_its_format(self):
        kept = self.keep()
        data = json.loads((kept.folder / review.RECORD).read_text())
        self.assertEqual(data["version"], config.FORMAT)

    def test_a_record_that_is_no_record_is_refused(self):
        kept = self.keep()
        record = kept.folder / review.RECORD
        for text in ("[]", '"scan"', "3"):
            record.write_text(text)
            with self.subTest(text=text), self.assertRaises(TypeError):
                review.read(kept.folder)

    def test_a_scan_stored_while_the_clock_ran_ahead_does_not_stay_latest(self):
        # a scan just stored is never pruned in its favor
        ahead = self.keep("scan_1")
        record = ahead.folder / review.RECORD
        data = json.loads(record.read_text())
        data["made"] = time.time() + 3600
        record.write_text(json.dumps(data))
        stored = self.keep("scan_2")
        self.assertEqual(self.store.latest(), stored)
        self.assertEqual([scan.name for scan in self.store.scans()], [stored.name])

    def test_a_record_stored_at_no_time_is_refused(self):
        kept = self.keep()
        record = kept.folder / review.RECORD
        data = json.loads(record.read_text())
        for made in ("NaN", "Infinity"):
            record.write_text(json.dumps(data).replace(str(data["made"]), made))
            with self.subTest(made=made), self.assertRaisesRegex(ValueError, "no time"):
                review.read(kept.folder)

    def test_a_record_whose_version_is_no_number_is_refused(self):
        kept = self.keep()
        record = kept.folder / review.RECORD
        data = json.loads(record.read_text())
        for version in (0, "2", 2.5, True):
            data["version"] = version
            record.write_text(json.dumps(data))
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(config.ConfigError, "version must be"),
            ):
                review.read(kept.folder)

    def test_a_record_of_this_version_with_a_value_none_can_have_is_refused(self):
        kept = self.keep()
        record = kept.folder / review.RECORD
        data = json.loads(record.read_text())
        data["options"]["straighten"] = "yes"
        record.write_text(json.dumps(data))
        with self.assertRaisesRegex(config.ConfigError, "straighten must be a bool"):
            review.read(kept.folder)

    def test_a_record_of_0_1_reads_its_show_through_as_it_meant(self):
        # 0.1.0 and 0.1.1 recorded show-through removal on or off: off stays
        # off when such a scan is made again, not the new default
        kept = self.keep()
        record = kept.folder / review.RECORD
        data = json.loads(record.read_text())
        del data["version"]  # 0.1 recorded none
        del data["options"]["show_through"]
        data["options"]["remove_show_through"] = False
        record.write_text(json.dumps(data))
        self.assertEqual(
            review.read(kept.folder).options.show_through, ShowThrough.KEEP
        )


class RemakeTest(StoredScan):
    def test_the_pdf_is_made_again_as_chosen(self):
        kept = self.keep()
        kept.pdf.chmod(0o640)  # the user's own permissions (not a new file's): kept
        choices = {1: job.LEAVE_OUT, 2: ScanMode.COLOR}  # the blank side brought back
        with mock.patch.object(job, "write_pdf", side_effect=write_pdf) as written:
            self.assertEqual(self.store.remake(kept, choices), kept.pdf)
        self.assertEqual(kept.pdf.read_bytes(), b"one page")
        self.assertEqual(len(written.call_args.args[0].pages), 1)
        again = self.store.latest()
        self.assertEqual([s.mode for s in again.sides], [None, pages.Mode.COLOR])
        self.assertEqual(again.choices(), choices)
        self.assertIsNone(again.page(1))
        self.assertIsNotNone(again.page(2))
        self.assertEqual(
            sorted(p.name for p in kept.pdf.parent.glob(f".{kept.pdf.stem}*")), []
        )  # nothing left beside the PDF
        self.assertEqual(kept.pdf.stat().st_mode & 0o777, 0o640)
        self.assertEqual(
            [p.name for p in kept.folder.iterdir() if p.name.startswith(".")], []
        )  # nor in the store

    def test_every_page_left_out_changes_nothing(self):
        kept = self.keep()
        with self.assertRaisesRegex(sane.ScanError, "every page would be left out"):
            self.store.remake(kept, {1: job.LEAVE_OUT, 2: job.LEAVE_OUT})
        self.assertEqual(kept.pdf.read_bytes(), b"the first PDF")
        self.assertEqual(self.store.latest().choices(), {})

    def test_a_failed_pdf_leaves_the_first_and_says_why_in_a_line(self):
        kept = self.keep()
        with (
            mock.patch.object(
                job, "write_pdf", side_effect=OSError(28, "No space left")
            ),
            self.assertRaisesRegex(sane.ScanError, "^\\[Errno 28\\] No space left$"),
        ):
            self.store.remake(kept, {2: ScanMode.GRAY})
        self.assertEqual(kept.pdf.read_bytes(), b"the first PDF")
        self.assertIsNotNone(self.store.latest().page(1))

    def test_nothing_changes_until_the_new_pages_are_all_made(self):
        kept = self.keep()
        before = kept.thumbnail(1).read_bytes()
        with (
            mock.patch.object(review, "thumbnail", side_effect=OSError("disk full")),
            self.assertRaises(sane.ScanError),
        ):
            self.store.remake(kept, {2: ScanMode.GRAY})
        self.assertEqual(kept.pdf.read_bytes(), b"the first PDF")
        self.assertEqual(kept.thumbnail(1).read_bytes(), before)
        self.assertEqual(self.store.latest().choices(), {})

    def test_a_pdf_moved_away_is_not_made_again(self):
        kept = self.keep()
        kept.pdf.rename(self.tmp / "renamed.pdf")
        with self.assertRaisesRegex(sane.ScanError, "moved or deleted"):
            self.store.remake(kept, {2: ScanMode.GRAY})
        self.assertFalse(kept.pdf.exists())  # not made again where it was

    def test_a_pdf_replaced_by_a_link_is_not_made_again(self):
        kept = self.keep()
        moved = self.tmp / "moved.pdf"
        kept.pdf.rename(moved)
        kept.pdf.symlink_to(moved)
        with (
            mock.patch.object(job, "write_pdf", side_effect=write_pdf),
            self.assertRaisesRegex(sane.ScanError, "moved or deleted"),
        ):
            self.store.remake(kept, {2: ScanMode.GRAY})
        self.assertTrue(kept.pdf.is_symlink())
        self.assertEqual(moved.read_bytes(), b"the first PDF")

    def test_a_review_that_cannot_follow_its_pdf_keeps_its_old_self_whole(self):
        for fails_at in (review.THUMBS, review.RECORD):
            with self.subTest(fails_at=fails_at):
                kept = self.keep(f"scan_{fails_at}")
                pages_before = self.contents(kept.folder / review.PAGES)
                thumbs_before = self.contents(kept.folder / review.THUMBS)
                with (
                    mock.patch.object(job, "write_pdf", side_effect=write_pdf),
                    self.failing(kept, fails_at),
                    self.assertLogs("openscansnap.review", "ERROR"),
                    self.assertRaises(job.PdfSaved) as raised,
                ):
                    self.store.remake(kept, {2: ScanMode.GRAY})
                self.assertEqual(raised.exception.path, kept.pdf)
                self.assertEqual(kept.pdf.read_bytes(), b"one page")  # updated
                # the review as it was: its record, pages and thumbnails
                self.assertEqual(review.read(kept.folder).choices(), {})
                self.assertEqual(
                    self.contents(kept.folder / review.PAGES), pages_before
                )
                self.assertEqual(
                    self.contents(kept.folder / review.THUMBS), thumbs_before
                )

    @staticmethod
    def contents(folder: Path) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in folder.iterdir()}

    @staticmethod
    def failing(kept: review.Scan, part: str):
        """Moving the staged part (a folder, or the record) into kept's folder
        fails, as on a full disk."""
        rename, replace_file = Path.rename, os.replace

        def failing_rename(path, target):
            if path.name == part and path.parent != kept.folder:
                raise OSError(28, "No space left")
            return rename(path, target)

        def failing_replace(source, target):
            if Path(source).name == part and Path(target).parent == kept.folder:
                raise OSError(28, "No space left")
            return replace_file(source, target)

        if part == review.RECORD:
            return mock.patch.object(review.os, "replace", failing_replace)
        return mock.patch.object(Path, "rename", failing_rename)


def write_pdf(assembled, path, options):
    path.write_bytes(b"one page" if len(assembled.pages) == 1 else b"pages")


@unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
class PreviewTest(StoredScan):
    def test_a_side_is_made_as_chosen_once_then_shown_again(self):
        kept = self.keep()  # the second side was left out as blank
        made = self.store.preview(kept, 2, ScanMode.GRAY)
        self.assertEqual(made.parent, kept.folder / review.PREVIEWS)
        with Image.open(made) as page:
            self.assertEqual(page.mode, "L")  # stored as gray, though blank
        with mock.patch.object(job, "assemble") as assembled:
            self.assertEqual(self.store.preview(kept, 2, ScanMode.GRAY), made)
        assembled.assert_not_called()
        self.assertEqual(self.store.latest().page(2), None)  # the PDF's are untouched
        # another version makes pages otherwise: its preview is not shown
        with mock.patch.object(review, "__version__", "0.0.1"):
            self.assertIsNone(kept.preview(2, ScanMode.GRAY))

    def test_a_preview_that_is_a_link_is_not_shown(self):
        kept = self.keep()
        made = self.store.preview(kept, 2, ScanMode.GRAY)
        elsewhere = self.tmp / "elsewhere.png"
        made.rename(elsewhere)
        made.symlink_to(elsewhere)
        self.assertIsNone(kept.preview(2, ScanMode.GRAY))

    def test_the_last_sheets_lone_side_is_made(self):
        kept = self.keep(sides=3)
        with Image.open(self.store.preview(kept, 3, ScanMode.BLACK_AND_WHITE)) as page:
            self.assertEqual(page.mode, "1")

    def assertMarkIn(self, page_path: Path, marked: Image.Image):
        """The page at page_path is the side marked (mark()), not another."""
        with Image.open(page_path) as page:
            ink = np.asarray(page.convert("L")) < 128
            scale = page.width / marked.width  # pages are stored at their own dpi
        left, top, right, bottom = (round(n * scale) for n in MARK)
        self.assertTrue(ink[top + 20 : bottom - 20, left + 20 : right - 20].all())
        self.assertFalse(ink[:, right + 20 :].any())

    def test_each_choice_has_its_own_preview(self):
        kept = self.keep()
        marked = mark(kept, 2)
        gray = self.store.preview(kept, 2, ScanMode.GRAY)
        lineart = self.store.preview(kept, 2, ScanMode.BLACK_AND_WHITE)
        self.assertNotEqual(gray, lineart)
        with Image.open(lineart) as page:
            self.assertEqual(page.mode, "1")
        self.assertMarkIn(lineart, marked)

    def test_a_later_sheets_back_is_made_from_itself(self):
        kept = self.keep(sides=4)  # side 4, the second sheet's back, left out
        marked = mark(kept, 4)
        made = self.store.preview(kept, 4, ScanMode.BLACK_AND_WHITE)
        with Image.open(made) as page:
            self.assertEqual(page.mode, "1")
        self.assertMarkIn(made, marked)

    def test_a_preview_is_made_with_its_sheets_other_side(self):
        # as a remake makes it: the two sides cleaned of each other together
        kept = self.keep(sides=4)
        assemble = job.assemble
        with mock.patch.object(job, "assemble", side_effect=assemble) as made:
            self.store.preview(kept, 4, ScanMode.GRAY)
        sides, _pages_dir, _options, _progress, choices = made.call_args.args
        self.assertEqual(sides, [kept.side_image(3), kept.side_image(4)])
        self.assertEqual(choices, {1: job.LEAVE_OUT, 2: ScanMode.GRAY})

    def test_previews_through_a_link_are_refused(self):
        kept = self.keep()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (kept.folder / review.PREVIEWS).symlink_to(elsewhere)
        with self.assertRaisesRegex(sane.ScanError, "is a link"):
            self.store.preview(kept, 2, ScanMode.GRAY)
        self.assertEqual(list(elsewhere.iterdir()), [])


class RealPdfTest(StoredScan):
    def test_a_remade_pdf_has_the_pages_chosen(self):
        kept = self.keep()  # both sides blank: an even gray
        options = replace(self.options, searchable=False)
        self.store.remake(replace(kept, options=options), {2: ScanMode.GRAY})
        document = pypdfium2.PdfDocument(kept.pdf)
        try:
            self.assertEqual(len(document), 1)  # the side given a mode, though blank
        finally:
            document.close()
        self.assertEqual(
            [s.method for s in self.store.latest().sides], [job.BLANK, ScanMode.GRAY]
        )


if __name__ == "__main__":
    unittest.main()
