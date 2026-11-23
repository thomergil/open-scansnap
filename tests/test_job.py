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

from openscansnap import config, job, pages, showthrough
from openscansnap.options import ScanMode

DPI = 300
POINTS_PER_INCH = 72
QUARTER, HALF = 90, 180
TOLERANCE_INCHES = 0.05


class SheetTurnsTest(unittest.TestCase):
    def turns(self, front: int | None, back: int | None) -> list[int]:
        with mock.patch.object(pages, "orientation", side_effect=[front, back]):
            return job.sheet_turns([None, None], DPI, "eng")

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
        self.assertEqual(job.sheet_turns(sides, DPI, "eng"), [HALF, HALF])


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
        path = job.assemble(self.sides, pages_dir, self.tmp, "scan", options)
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
        self.assertEqual(turns.call_args.args[0], stored)
        self.assertEqual([c.args[5] for c in prepare.call_args_list], [90, 270])

    def test_steps_switched_off_are_not_run(self):
        with (
            mock.patch.object(showthrough, "clean") as clean,
            mock.patch.object(job, "sheet_turns") as turns,
        ):
            self.assemble(remove_show_through=False, upright=False)
        clean.assert_not_called()
        turns.assert_not_called()


if __name__ == "__main__":
    unittest.main()
