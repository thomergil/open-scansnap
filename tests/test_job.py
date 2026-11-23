"""A sheet's two sides together: cropping, show-through and turning upright,
each only when the scan's options ask."""

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pypdfium2
from PIL import Image
from test_pages import text_page
from test_sheet import FRAME, LETTER, page, scan

from openscansnap import (
    bundle,
    config,
    job,
    languages,
    leptonica,
    pages,
    pdf,
    sane,
    showthrough,
)
from openscansnap.options import AUTOMATIC_LANGUAGE, ScanMode

DPI = 300
POINTS_PER_INCH = 72
QUARTER, HALF = 90, 180
TOLERANCE_INCHES = 0.05


class SheetTurnsTest(unittest.TestCase):
    def turns(self, front: int | None, back: int | None) -> list[int]:
        with mock.patch.object(
            pages, "orientation", side_effect=[front, back]
        ) as orientation:
            turns = job.sheet_turns(["front", "back"], ["nld", "deu"])
        # each side, in its own language
        self.assertEqual(
            [c.args for c in orientation.call_args_list],
            [("front", "nld"), ("back", "deu")],
        )
        return turns

    def test_each_side_with_text_keeps_its_own_turn(self):
        self.assertEqual(self.turns(HALF, 0), [HALF, 0])

    def test_a_side_without_text_turns_with_the_sheet(self):
        self.assertEqual(self.turns(HALF, None), [HALF, HALF])
        # a quarter turn of the sheet shows as the other quarter on the back
        self.assertEqual(self.turns(QUARTER, None), [QUARTER, 3 * QUARTER])
        self.assertEqual(self.turns(None, QUARTER), [3 * QUARTER, QUARTER])

    def test_without_text_on_either_side_nothing_turns(self):
        self.assertEqual(self.turns(None, None), [0, 0])

    @unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
    def test_upside_down_sheet_with_a_blank_back(self):
        sides = [text_page().rotate(HALF), Image.new("RGB", text_page().size, "white")]
        detections = [pages.Detection(side, DPI) for side in sides]
        self.assertEqual(job.sheet_turns(detections, ["eng", "eng"]), [HALF, HALF])


@unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
class AssembleTest(unittest.TestCase):
    """One fed sheet (as the scanner sends it: backing around a tilted
    sheet) through job.assemble."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.sides = []
        for number, image in enumerate(
            (scan(page(False), 1.0), scan(page(False), -1.0, seed=1)), 1
        ):
            path = self.tmp / f"page{number:04d}.tif"
            image.save(path, dpi=(DPI, DPI))
            self.sides.append(path)

    def assemble(self, **changes) -> list[tuple[float, float]]:
        """The PDF's page sizes, in inches."""
        pages_dir = self.tmp / "pages"
        pages_dir.mkdir(exist_ok=True)
        options = replace(
            config.default_options(), remove_blank=False, searchable=False, **changes
        )
        assembled = job.assemble(self.sides, pages_dir, options)
        path = self.tmp / "scan.pdf"
        job.write_pdf(assembled, path, options)
        document = pypdfium2.PdfDocument(path)
        try:
            return [
                tuple(n / POINTS_PER_INCH for n in document[i].get_size())
                for i in range(len(document))
            ]
        finally:
            document.close()
            path.unlink()

    def assertSizes(self, sizes, expected):
        for size in sizes:
            for got, want in zip(size, expected):
                self.assertAlmostEqual(got, want, delta=TOLERANCE_INCHES)

    def test_sheets_are_cropped_to_their_edges(self):
        self.assertSizes(self.assemble(), [n / DPI for n in LETTER])

    def measured(self, found_edges, mode=ScanMode.AUTO, choices=None):
        """The sides pages.text_tilt measured, and the sides show-through
        returned, when the sides' edges were found or not."""
        cleaned = []

        def clean(side, other, dpi):
            out = side.copy()  # what clean made, not its input
            cleaned.append(out)
            return out

        options = replace(
            config.default_options(),
            mode=mode,
            remove_blank=False,
            searchable=False,
            remove_show_through=True,
        )
        pages_dir = self.tmp / "pages"
        pages_dir.mkdir(exist_ok=True)
        with (
            mock.patch.object(
                job.sheet, "straightened", side_effect=lambda i, d: (i, found_edges)
            ),
            mock.patch.object(showthrough, "clean", side_effect=clean),
            mock.patch.object(pages, "text_tilt", return_value=None) as measured,
        ):
            job.assemble(self.sides, pages_dir, options, None, choices)
        return [c.args[0] for c in measured.call_args_list], cleaned

    def test_a_side_its_edges_did_not_straighten_is_turned_by_its_text(self):
        measured, cleaned = self.measured(found_edges=False)
        self.assertEqual(len(measured), 2)
        for got, side in zip(measured, cleaned, strict=True):
            self.assertIs(got, side)  # measured as show-through left it

    def test_its_edges_straighten_a_side_they_were_found_on(self):
        self.assertEqual(self.measured(found_edges=True)[0], [])

    def test_a_photo_is_not_turned_by_lines_in_it(self):
        self.assertEqual(self.measured(False, ScanMode.PHOTO)[0], [])
        # a side's own choice decides for that side
        measured, cleaned = self.measured(False, choices={2: ScanMode.PHOTO})
        self.assertEqual(len(measured), 1)
        self.assertIs(measured[0], cleaned[0])
        measured, _ = self.measured(False, ScanMode.PHOTO, {1: ScanMode.GRAY})
        self.assertEqual(len(measured), 1)

    def test_as_scanned_turns_no_side_whatever_its_choice(self):
        # the whole scan, backing and all: what Leptonica misreads
        measured, _ = self.measured(False, ScanMode.AS_SCANNED, {1: ScanMode.COLOR})
        self.assertEqual(measured, [])

    @unittest.skipUnless(bundle.leptonica(), "needs Leptonica")
    def test_text_askew_comes_out_level(self):
        # the measure and the turn agree on which way is which
        page = text_page()
        for degrees in (2.5, -2.5):
            with self.subTest(degrees=degrees):
                askew = page.rotate(
                    degrees, resample=Image.Resampling.BICUBIC, fillcolor="white"
                )
                level = job.level_text(askew)
                skew = leptonica.find_skew(pages.binarized(level))
                self.assertLess(abs(skew.degrees), 0.1)

    def test_a_side_askew_is_turned_level_whole(self):
        side = Image.new("RGB", (400, 600), "white")
        with mock.patch.object(pages, "text_tilt", return_value=None):
            self.assertIs(job.level_text(side), side)
        with mock.patch.object(pages, "text_tilt", return_value=0.02):
            level = job.level_text(side)
        self.assertGreater(level.width, side.width)  # turned, nothing cut

    def test_a_tilt_that_cannot_be_measured_leaves_the_side(self):
        side = Image.new("RGB", (400, 600), "white")
        with (
            mock.patch.object(
                pages, "text_tilt", side_effect=sane.ScanError("cannot read")
            ),
            self.assertLogs("openscansnap.job", "WARNING"),
        ):
            self.assertIs(job.level_text(side), side)

    def test_as_scanned_keeps_the_whole_scan(self):
        self.assertSizes(
            self.assemble(mode=ScanMode.AS_SCANNED), [n / DPI for n in FRAME]
        )

    def test_cleaned_sides_and_their_turns_are_what_is_stored(self):
        cleaned = {}

        def clean(side, other, dpi):
            out = side.copy()  # a new image: what clean made, not its input
            cleaned[id(out)] = (side, other)
            return out

        with (
            mock.patch.object(showthrough, "clean", side_effect=clean) as clean_calls,
            mock.patch.object(job, "sheet_turns", return_value=[90, 270]) as turns,
            mock.patch.object(
                job, "prepare_side", side_effect=job.prepare_side
            ) as prepare,
        ):
            self.assemble()
        (front, back, _), (back_again, front_again, _) = (
            c.args for c in clean_calls.call_args_list
        )
        # each side is cleaned of the other side as scanned
        self.assertIs(front, front_again)
        self.assertIs(back, back_again)
        stored = [c.args[0] for c in prepare.call_args_list]
        self.assertEqual([cleaned[id(image)][0] for image in stored], [front, back])
        self.assertEqual([side.image for side in turns.call_args.args[0]], stored)
        self.assertEqual([c.args[5] for c in prepare.call_args_list], [90, 270])

    def test_no_language_is_detected_on_a_blank_side(self):
        blank = Image.new("RGB", (2550, 3300), "white")
        options = replace(config.default_options(), language=AUTOMATIC_LANGUAGE)
        with mock.patch.object(languages, "detect") as detect:
            self.assertEqual(
                job.language_of(pages.Detection(blank, DPI), options, {"eng", "nld"}),
                languages.FALLBACK,
            )
        detect.assert_not_called()

    def test_each_side_is_read_in_its_language_and_the_pdf_in_all(self):
        read_in = []

        def prepare_side(image, dpi, options, pages_dir, number, turn):
            read_in.append(options.language)
            page = pages_dir / f"{number}.png"
            return job.SideResult(number, pages.Mode.LINEART, "otsu", page)

        with (
            mock.patch.object(job, "language_of", side_effect=["nld", "deu+eng"]),
            mock.patch.object(job, "sheet_turns", return_value=[0, 0]) as turns,
            mock.patch.object(job, "prepare_side", side_effect=prepare_side),
            mock.patch.object(pdf, "write") as write,
        ):
            options = config.default_options()
            job.write_pdf(
                job.assemble(self.sides, self.tmp, options), self.tmp, options
            )
        self.assertEqual(turns.call_args.args[1], ["nld", "deu+eng"])  # turned in them
        self.assertEqual(read_in, ["nld", "deu+eng"])
        # every language read, in the order they came
        self.assertEqual(write.call_args.args[2].language, "nld+deu+eng")

    def test_a_sides_choice_overrides_the_scans_mode(self):
        options = replace(config.default_options(), language="eng", searchable=False)
        assembled = job.assemble(
            self.sides,
            self.tmp,
            options,
            choices={1: ScanMode.COLOR, 2: job.LEAVE_OUT},
        )
        front, back = assembled.sides
        self.assertEqual(front.mode, pages.Mode.COLOR)
        self.assertEqual(
            (back.mode, back.method, back.page), (None, job.LEFT_OUT, None)
        )
        self.assertEqual(assembled.pages, [front.page])

    def test_a_blank_side_given_a_mode_is_kept(self):
        blank = self.tmp / "blank.tif"
        Image.new("RGB", (2550, 3300), "white").save(blank, dpi=(DPI, DPI))
        options = replace(config.default_options(), language="eng")
        dropped = job.assemble([blank], self.tmp, options).sides[0]
        self.assertEqual(dropped.method, job.BLANK)
        kept = job.assemble([blank], self.tmp, options, choices={1: ScanMode.GRAY})
        self.assertEqual(kept.sides[0].mode, pages.Mode.GRAY)

    def test_each_sheet_is_told_as_it_is_begun(self):
        told = []
        with (
            mock.patch.object(
                job, "prepare_side", return_value=job.SideResult(1, None, "blank", None)
            ),
            mock.patch.object(job, "language_of", return_value="eng"),
            mock.patch.object(job, "sheet_turns", return_value=[0, 0]),
        ):
            job.assemble(
                self.sides * 2,  # two sheets
                self.tmp,
                config.default_options(),
                lambda sheet, sheets: told.append((sheet, sheets)),
            )
        self.assertEqual(told, [(1, 2), (2, 2)])

    def test_steps_switched_off_are_not_run(self):
        with (
            mock.patch.object(showthrough, "clean") as clean,
            mock.patch.object(job, "sheet_turns") as turns,
        ):
            self.assemble(remove_show_through=False, upright=False)
        clean.assert_not_called()
        turns.assert_not_called()


class FinishKeepTest(unittest.TestCase):
    """job.finish hands the scan to keep, whose failure never fails the PDF."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.out = self.tmp / "out"
        self.options = config.default_options()

    def finish(self, keep, stopped=None):
        sides_dir = self.tmp / "sides"
        sides_dir.mkdir()
        side = sides_dir / "page0001.tif"
        side.write_bytes(b"side")
        batch = job.Batch("scan", sides_dir, [side], stopped)
        page = self.tmp / "0001.jpg"
        result = job.SideResult(1, pages.Mode.COLOR, "colorful", page)
        assembled = job.Assembled([page], "eng", [result])
        with (
            mock.patch.object(job, "assemble", return_value=assembled),
            mock.patch.object(
                job, "write_pdf", side_effect=lambda a, p, o: p.write_bytes(b"%PDF")
            ),
        ):
            path = job.finish(batch, self.out, self.options, None, keep)
        return path, assembled, batch

    def test_keep_is_handed_the_pdf_and_its_pages(self):
        keep = mock.Mock()
        path, assembled, batch = self.finish(keep)
        keep.assert_called_once_with(path, assembled, batch, self.options)

    def test_a_batch_stopped_partway_is_saved_kept_and_told_saved(self):
        keep = mock.Mock()
        stopped = sane.ScanStopped("Paper jam", [])
        with self.assertRaisesRegex(job.PdfSaved, "Paper jam; saved the sides") as told:
            self.finish(keep, stopped)
        self.assertEqual(told.exception.path.read_bytes(), b"%PDF")
        keep.assert_called_once()

    def test_finish_hands_the_store_a_scan_it_can_keep(self):
        from openscansnap import review

        store = review.Store(self.tmp / "review")
        sides_dir = self.tmp / "sides"
        sides_dir.mkdir()
        side = sides_dir / "page0001.tif"
        Image.new("RGB", (85, 110), "white").save(side, dpi=(DPI, DPI))

        def assemble(sides, pages_dir, options, progress):
            page = Path(pages_dir) / "0001.jpg"
            Image.new("RGB", (85, 110), "white").save(page)
            result = job.SideResult(1, pages.Mode.COLOR, "colorful", page)
            return job.Assembled([page], "eng", [result])

        with (
            mock.patch.object(job, "assemble", side_effect=assemble),
            mock.patch.object(
                job, "write_pdf", side_effect=lambda a, p, o: p.write_bytes(b"%PDF")
            ),
        ):
            path = job.finish(
                job.Batch("scan", sides_dir, [side], None),
                self.out,
                self.options,
                None,
                store.keep,
            )
        kept = store.latest()
        self.assertEqual(kept.pdf, path)
        self.assertIsNotNone(kept.page(1))
        self.assertTrue(kept.side_image(1).is_file())

    def test_any_keep_failure_leaves_the_pdf(self):
        for error in (OSError("disk full"), ValueError("bad"), MemoryError()):
            with self.subTest(error=type(error).__name__):
                shutil.rmtree(self.tmp / "sides", ignore_errors=True)
                shutil.rmtree(self.out, ignore_errors=True)
                with self.assertLogs("openscansnap.job", "ERROR"):
                    path, _, _ = self.finish(mock.Mock(side_effect=error))
                self.assertEqual(path.read_bytes(), b"%PDF")
                self.assertEqual([p.name for p in self.out.iterdir()], [path.name])


if __name__ == "__main__":
    unittest.main()
