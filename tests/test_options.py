"""The scan options: each mode, quality and step does what its name says."""

import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from test_pages import DPI, LETTER, paper, text_page

from openscansnap import config, pages, pdf
from openscansnap.options import Quality, ScanMode
from openscansnap.pages import Mode

DEFAULTS = replace(config.default_options(), language="eng")  # pages.prepare: one


class InEffectTest(unittest.TestCase):
    def test_document_modes_keep_every_step(self):
        for mode in (
            ScanMode.AUTO,
            ScanMode.BLACK_AND_WHITE,
            ScanMode.GRAY,
            ScanMode.COLOR,
        ):
            with self.subTest(mode=mode):
                options = replace(DEFAULTS, mode=mode)
                self.assertEqual(options.in_effect(), options)

    def test_photo_drops_the_document_clean_up(self):
        photo = replace(DEFAULTS, mode=ScanMode.PHOTO).in_effect()
        self.assertFalse(
            photo.remove_blank or photo.remove_show_through or photo.clean_up
        )
        self.assertTrue(photo.straighten and photo.upright and photo.searchable)

    def test_as_scanned_changes_nothing(self):
        raw = replace(DEFAULTS, mode=ScanMode.AS_SCANNED).in_effect()
        self.assertFalse(
            raw.straighten
            or raw.upright
            or raw.remove_blank
            or raw.remove_show_through
            or raw.clean_up
        )
        self.assertTrue(raw.searchable)  # a text layer does not change the image

    def test_text_turns_a_side_only_where_straightening_is_in_effect(self):
        options = config.default_options()
        expected = {
            ScanMode.AUTO: True,
            ScanMode.GRAY: True,
            ScanMode.PHOTO: False,  # straightened by its paper alone
            ScanMode.AS_SCANNED: False,  # kept whole
        }
        for mode, level in expected.items():
            with self.subTest(mode=mode):
                self.assertIs(replace(options, mode=mode).level_text, level)
        self.assertFalse(replace(options, straighten=False).level_text)

    def test_choices_no_scan_can_have_are_refused(self):
        for changes in (
            {"mode": "fancy"},
            {"quality": "maximum"},
            {"language": ""},
            {"language": "eng+"},
            {"language": "eng nld"},
            {"language": "auto+eng"},  # Automatic, or languages: not both
            {"language": "eng+auto"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(DEFAULTS, **changes)
        self.assertIs(replace(DEFAULTS, mode="gray").mode, ScanMode.GRAY)
        self.assertEqual(
            replace(DEFAULTS, language="chi_sim+eng").languages, ["chi_sim", "eng"]
        )

    def test_quality_sets_both_resolutions(self):
        for quality, dpi in (
            (Quality.DRAFT, 150),
            (Quality.STANDARD, 200),
            (Quality.HIGH, 300),
        ):
            with self.subTest(quality=quality):
                options = replace(DEFAULTS, quality=quality)
                self.assertEqual(
                    (options.page_dpi, options.lineart_dpi), (dpi, 2 * dpi)
                )


@unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
class ModeTest(unittest.TestCase):
    def prepare(self, image, **changes):
        return pages.prepare(image, DPI, replace(DEFAULTS, **changes))

    def test_each_mode_stores_pages_as_named(self):
        for mode, stored in (
            (ScanMode.BLACK_AND_WHITE, Mode.LINEART),
            (ScanMode.GRAY, Mode.GRAY),
            (ScanMode.COLOR, Mode.COLOR),
            (ScanMode.PHOTO, Mode.COLOR),
            (ScanMode.AS_SCANNED, Mode.COLOR),
        ):
            with self.subTest(mode=mode):
                self.assertEqual(self.prepare(text_page(), mode=mode).mode, stored)

    def test_black_and_white_even_for_a_colorful_page(self):
        page = text_page()
        ImageDraw.Draw(page).rectangle((300, 2000, 2200, 2900), fill=(220, 30, 30))
        self.assertEqual(self.prepare(page).mode, Mode.COLOR)  # automatic
        self.assertEqual(
            self.prepare(page, mode=ScanMode.BLACK_AND_WHITE).mode, Mode.LINEART
        )

    def test_quality_sets_the_stored_resolution(self):
        draft = self.prepare(text_page(), quality=Quality.DRAFT)
        self.assertEqual(draft.dpi, 300)  # black and white: twice 150
        self.assertEqual(draft.image.size, LETTER)  # 8.5 x 11 inches at 300 dpi
        gray = self.prepare(text_page(), quality=Quality.DRAFT, mode=ScanMode.GRAY)
        self.assertEqual(
            (gray.dpi, gray.image.size), (150, (LETTER[0] // 2, LETTER[1] // 2))
        )

    def test_blank_pages_are_kept_when_asked(self):
        self.assertIsNone(self.prepare(paper()))
        self.assertIsNotNone(self.prepare(paper(), remove_blank=False))

    def test_blank_sides_go_in_every_mode_that_removes_them(self):
        # a blank back with the front's print faintly showing through, mirrored
        page = paper()
        ink = np.asarray(text_page().convert("L"))[:, ::-1] < 128
        gray = np.asarray(page.convert("L"), dtype=np.float64)
        gray[ink] -= 40
        show_through = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8)).convert(
            "RGB"
        )
        for mode in (
            ScanMode.AUTO,
            ScanMode.BLACK_AND_WHITE,
            ScanMode.GRAY,
            ScanMode.COLOR,
        ):
            with self.subTest(mode=mode):
                self.assertIsNone(self.prepare(show_through, mode=mode))
        self.assertIsNotNone(self.prepare(show_through, mode=ScanMode.PHOTO))

    def test_pages_stay_as_fed_without_turning(self):
        upside_down = text_page().rotate(180)
        turned = self.prepare(upside_down)
        kept = self.prepare(upside_down, upright=False)
        self.assertGreater(turned.readable_words, kept.readable_words)

    def test_edge_shadow_stays_without_clean_up(self):
        page = text_page()
        ImageDraw.Draw(page).rectangle(
            (0, LETTER[1] - 11, LETTER[0], LETTER[1]), fill=(40, 40, 40)
        )
        cleaned = ~np.asarray(self.prepare(page).image, dtype=bool)
        kept = ~np.asarray(self.prepare(page, clean_up=False).image, dtype=bool)
        self.assertFalse(cleaned[-10:].any())
        self.assertTrue(kept[-10:].any())


@unittest.skipUnless(
    shutil.which(pages.TESSERACT) and shutil.which("pdftotext"),
    "needs Tesseract and pdftotext",
)
class SearchableTest(unittest.TestCase):
    def test_text_layer_only_when_asked(self):
        page = pages.prepare(text_page(), DPI, DEFAULTS)
        texts = {}
        with tempfile.TemporaryDirectory() as tmp:
            saved = [pdf.save_page(page, Path(tmp), 1)]
            for searchable in (True, False):
                out = Path(tmp) / f"{searchable}.pdf"
                out.touch()
                pdf.write(saved, out, replace(DEFAULTS, searchable=searchable))
                texts[searchable] = subprocess.run(
                    ["pdftotext", str(out), "-"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
        self.assertIn("quick brown fox", texts[True])
        self.assertEqual(texts[False].strip(), "")


if __name__ == "__main__":
    unittest.main()
