"""Page decisions on synthetic 300 dpi pages. Needs Tesseract (eng)."""

import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np
import pikepdf
from PIL import Image, ImageDraw, ImageFont, ImageOps
from skimage.color import lab2rgb

from openscansnap import bundle, config, pages, pdf, sheet
from openscansnap.pages import Mode

DPI = 300
OPTIONS = replace(
    config.default_options(), language="eng"
)  # one language: pages.prepare
SCALE = OPTIONS.lineart_dpi // DPI  # black and white is stored at this multiple
LETTER = (2550, 3300)  # 8.5 x 11 inches
TEXT = [
    "The quick brown fox jumps over the lazy dog.",
    "Please send all correspondence to the address below.",
    "Your renewal fee is due before the end of the month.",
    "All fees are non-refundable and expire after one year.",
]
WHITE, BLACK = 255, 0
POINTS_PER_INCH = 72


def text_page(ink=BLACK, background=WHITE, size=LETTER) -> Image.Image:
    page = Image.new("L", size, background)
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=48)
    for i, line in enumerate(TEXT * 3):
        draw.text((300, 400 + i * 120), line, fill=ink, font=font)
    return page.convert("RGB")


def size_on_paper_of(pixels: tuple[int, int], dpi: int = DPI) -> tuple[float, float]:
    """Width and height in inches."""
    return pixels[0] / dpi, pixels[1] / dpi


def size_on_paper(page: pages.Page) -> tuple[float, float]:
    return size_on_paper_of(page.image.size, page.dpi)


def ink_in_band(page: pages.Page) -> int:
    band = pages.edge_band(page.dpi)
    ink = ~np.asarray(page.image)
    inner = ink[band:-band, band:-band]
    return int(ink.sum() - inner.sum())


@unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
class PrepareTest(unittest.TestCase):
    def test_text_page_becomes_black_and_white(self):
        page = pages.prepare(text_page(), DPI, OPTIONS)
        self.assertEqual(page.mode, Mode.LINEART)
        self.assertEqual(page.image.mode, "1")
        # at twice the resolution, the same size on paper
        self.assertEqual(page.dpi, DPI * SCALE)
        self.assertEqual(page.image.size, tuple(n * SCALE for n in LETTER))
        self.assertGreater(
            page.readable_words, 0.8 * sum(len(t.split()) for t in TEXT * 3)
        )

    def test_upside_down_page_is_turned_upright(self):
        page = pages.prepare(text_page().rotate(180), DPI, OPTIONS)
        self.assertEqual(size_on_paper(page), size_on_paper_of(LETTER))
        self.assertGreater(
            page.readable_words, 0.8 * sum(len(t.split()) for t in TEXT * 3)
        )

    def test_sideways_page_is_turned_upright(self):
        page = pages.prepare(text_page().rotate(90, expand=True), DPI, OPTIONS)
        self.assertEqual(size_on_paper(page), size_on_paper_of(LETTER))
        self.assertGreater(
            page.readable_words, 0.8 * sum(len(t.split()) for t in TEXT * 3)
        )

    def test_faint_text_on_uneven_paper_stays_readable(self):
        # gray text on paper that darkens from left to right
        page = text_page(ink=150)
        gradient = np.linspace(250, 170, page.width)[None, :, None]
        page = Image.fromarray((np.asarray(page) * (gradient / 255)).astype(np.uint8))
        prepared = pages.prepare(page, DPI, OPTIONS)
        self.assertEqual(prepared.mode, Mode.LINEART)
        self.assertGreater(
            prepared.readable_words, 0.5 * sum(len(t.split()) for t in TEXT * 3)
        )

    def test_colorful_page_stays_color(self):
        page = text_page()
        draw = ImageDraw.Draw(page)
        draw.rectangle((300, 2000, 1200, 2900), fill=(220, 30, 30))
        draw.rectangle((1300, 2000, 2200, 2900), fill=(30, 60, 220))
        prepared = pages.prepare(page, DPI, OPTIONS)
        self.assertEqual(prepared.mode, Mode.COLOR)
        self.assertEqual(prepared.image.mode, "RGB")

    def test_black_print_is_not_color(self):
        self.assertFalse(pages.has_colored_ink(text_page(), DPI))

    def test_a_lone_pen_drawing_is_not_blank(self):
        # a doodle on a back: a thin blue line, no text, far under PICTURE_SHARE
        page = paper()
        ImageDraw.Draw(page).line(
            (1300, 400, 1300, 2600), fill=(110, 130, 210), width=5
        )  # as light as a real one measured: contrast 0.47
        contrast, share = pages.side_contrast(page, DPI)
        self.assertLess(contrast, pages.INK)  # faint: decided by the faint rule
        self.assertLess(share, pages.PICTURE_SHARE)
        kept = pages.prepare(page, DPI, OPTIONS)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.mode, Mode.COLOR)

    def test_a_pen_stroke_makes_the_page_color(self):
        page = text_page()
        ImageDraw.Draw(page).line((300, 2900, 1500, 2950), fill=(40, 70, 200), width=8)
        self.assertTrue(pages.has_colored_ink(page, DPI))
        self.assertEqual(pages.prepare(page, DPI, OPTIONS).mode, Mode.COLOR)

    def test_ink_is_color_from_the_chroma_of_a_navy_letterhead(self):
        # measured: black print at most 7.9, a navy letterhead 12.5 to 14
        for chroma, colored in ((7.9, False), (12.5, True)):
            with self.subTest(chroma=chroma):
                page = text_page()
                ink = tuple(
                    round(v * 255) for v in lab2rgb([[[30.0, 0.0, -chroma]]])[0][0]
                )
                ImageDraw.Draw(page).rectangle((300, 2700, 900, 2900), fill=ink)
                self.assertEqual(pages.has_colored_ink(page, DPI), colored)

    def test_color_strips_at_the_paper_edge_are_not_color(self):
        # a printer's calibration strips: 0.5 mm wide, 4.5 mm long, at the edge
        page = text_page()
        draw = ImageDraw.Draw(page)
        right = page.width - 3
        for top, ink in ((1600, (230, 200, 40)), (1700, (216, 102, 102))):
            draw.rectangle((right - 6, top, right, top + 54), fill=ink)
        self.assertFalse(pages.has_colored_ink(page, DPI))

    def test_a_color_page_is_put_on_white_paper(self):
        # cream paper with the iX500's grain (measured 1-1.5 gray levels),
        # darker toward one side (a shadow), with print and a green logo
        rng = np.random.default_rng(0)
        cream = np.array([238.0, 232.0, 214.0])
        shade = np.linspace(1.0, 0.96, LETTER[0])[np.newaxis, :, np.newaxis]
        grain = rng.normal(0, 1.2, (LETTER[1], LETTER[0], 1))
        page = Image.fromarray(np.clip(cream * shade + grain, 0, 255).astype(np.uint8))
        draw = ImageDraw.Draw(page)
        draw.rectangle((300, 300, 700, 600), fill=(40, 110, 70))
        draw.rectangle((300, 2850, 1500, 2950), fill=(250, 240, 120))  # highlighter
        for line in range(20):
            draw.rectangle(
                (300, 900 + line * 90, 2200, 930 + line * 90), fill=(20, 20, 20)
            )
        white = np.asarray(pages.whitened(page))
        self.assertGreaterEqual(white[150, 1200].min(), 250)  # paper, either side
        self.assertGreaterEqual(white[150, 2400].min(), 250)
        self.assertGreater(np.ptp(white[450, 500]), 30)  # the logo still green
        self.assertGreater(np.ptp(white[2900, 600]), 30)  # the highlighter kept
        self.assertLess(white[915, 1200].max(), 60)  # the print still black

    def test_clean_up_puts_a_stored_color_page_on_white(self):
        rng = np.random.default_rng(0)
        cream = np.array([238.0, 232.0, 214.0])
        grain = rng.normal(0, 1.2, (LETTER[1], LETTER[0], 1))
        page = Image.fromarray(np.clip(cream + grain, 0, 255).astype(np.uint8))
        draw = ImageDraw.Draw(page)
        draw.rectangle((300, 300, 700, 600), fill=(40, 110, 70))
        for line in range(20):
            draw.rectangle(
                (300, 900 + line * 90, 2200, 930 + line * 90), fill=(20, 20, 20)
            )
        for clean_up in (True, False):
            with self.subTest(clean_up=clean_up):
                kept = pages.prepare(page, DPI, replace(OPTIONS, clean_up=clean_up), 0)
                self.assertEqual(kept.mode, Mode.COLOR)
                out = np.asarray(kept.image.convert("RGB"))
                paper = out[
                    out.shape[0] * 150 // LETTER[1], out.shape[1] * 1200 // LETTER[0]
                ]
                self.assertEqual(paper.min() >= 250, clean_up, paper)

    def test_a_page_mostly_picture_is_not_whitened(self):
        rng = np.random.default_rng(0)
        picture = np.kron(rng.integers(150, 250, (33, 26, 3)), np.ones((100, 100, 1)))
        page = Image.fromarray(picture[: LETTER[1], : LETTER[0]].astype(np.uint8))
        self.assertIs(pages.whitened(page), page)

    def test_photo_without_text_stays_grayscale(self):
        rng = np.random.default_rng(0)
        smooth = np.kron(rng.integers(40, 220, (33, 26)), np.ones((100, 100)))[
            : LETTER[1], : LETTER[0]
        ]
        prepared = pages.prepare(
            Image.fromarray(smooth.astype(np.uint8)).convert("RGB"), DPI, OPTIONS
        )
        self.assertEqual(prepared.mode, Mode.GRAY)

    def test_rule_from_the_edge_survives_beyond_the_band(self):
        # a table rule that starts at the paper edge
        page = text_page()
        y = LETTER[1] - 300
        ImageDraw.Draw(page).rectangle((0, y, LETTER[0] // 2, y + 6), fill=0)
        prepared = pages.prepare(page, DPI, OPTIONS)
        ink = ~np.asarray(prepared.image.convert("1"), dtype=bool)
        rule = ink[y * SCALE : (y + 7) * SCALE]
        self.assertTrue(rule[:, 0].any())  # kept to the edge: it reaches past the band

    def test_print_near_the_edge_is_kept(self):
        # a letterhead seal whose top is 2 mm below the paper's top edge
        page = text_page()
        top = round(2 / sheet.MM_PER_INCH * DPI)
        ImageDraw.Draw(page).ellipse((200, top, 500, top + 300), outline=0, width=8)
        prepared = pages.prepare(page, DPI, OPTIONS)
        ink = ~np.asarray(prepared.image.convert("1"), dtype=bool)
        band = pages.edge_band(prepared.dpi)
        self.assertTrue(ink[:band, 200 * SCALE : 500 * SCALE].any())

    def test_edge_shadow_is_cleared_and_text_kept(self):
        page = text_page()
        # as measured on the iX500: a bar 0.9 mm tall along the bottom edge
        shadow = round(0.9 / sheet.MM_PER_INCH * DPI)
        ImageDraw.Draw(page).rectangle(
            (0, LETTER[1] - shadow, LETTER[0], LETTER[1]), fill=(40, 40, 40)
        )
        prepared = pages.prepare(page, DPI, OPTIONS)
        self.assertEqual(ink_in_band(prepared), 0)
        self.assertGreater(
            prepared.readable_words, 0.8 * sum(len(t.split()) for t in TEXT * 3)
        )


def paper(tone=250, noise=2.0, seed=0) -> Image.Image:
    rng = np.random.default_rng(seed)
    sheet = np.clip(tone + rng.normal(0, noise, LETTER[::-1]), 0, 255).astype(np.uint8)
    return Image.fromarray(sheet).convert("RGB")


def note(page: Image.Image, text: str, ink, size=48) -> Image.Image:
    ImageDraw.Draw(page).text(
        (300, 1600), text, fill=ink, font=ImageFont.load_default(size=size)
    )
    return page


@unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
class BlankTest(unittest.TestCase):
    def test_plain_paper_is_blank(self):
        self.assertIsNone(pages.prepare(paper(), DPI, OPTIONS))

    def test_tinted_paper_is_blank(self):
        self.assertIsNone(pages.prepare(paper(tone=225, noise=3), DPI, OPTIONS))

    def test_show_through_is_blank(self):
        # the other side's text, mirrored and faint
        faint = ImageOps.mirror(text_page(ink=215, background=250))
        self.assertIsNone(pages.prepare(faint, DPI, OPTIONS))

    def test_edge_shadow_alone_is_blank(self):
        sheet = paper()
        ImageDraw.Draw(sheet).rectangle(
            (0, LETTER[1] - 20, LETTER[0], LETTER[1] - 8), fill=(40, 40, 40)
        )
        self.assertIsNone(pages.prepare(sheet, DPI, OPTIONS))

    def test_faint_pencil_note_is_kept(self):
        prepared = pages.prepare(
            note(paper(), "faint pencil note here", (170, 170, 170)), DPI, OPTIONS
        )
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.mode, Mode.LINEART)

    def test_lone_signature_on_tinted_paper_is_kept(self):
        prepared = pages.prepare(
            note(paper(tone=235), "Signature: J. Smith", (0, 0, 0)), DPI, OPTIONS
        )
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.mode, Mode.LINEART)

    def test_pale_photo_is_kept(self):
        rng = np.random.default_rng(0)
        pale = np.kron(rng.integers(150, 230, (33, 26)), np.ones((100, 100)))[
            : LETTER[1], : LETTER[0]
        ]
        prepared = pages.prepare(
            Image.fromarray(pale.astype(np.uint8)).convert("RGB"), DPI, OPTIONS
        )
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.mode, Mode.GRAY)


@unittest.skipUnless(bundle.leptonica(), "needs Leptonica")
class TextTiltTest(unittest.TestCase):
    @staticmethod
    def turned(degrees: float) -> Image.Image:
        return text_page().rotate(
            degrees, resample=Image.Resampling.BICUBIC, fillcolor="white"
        )

    def test_text_lines_give_their_tilt(self):
        for degrees in (1.0, -0.5, 5.0):
            with self.subTest(degrees=degrees):
                tilt = pages.text_tilt(self.turned(degrees))
                self.assertAlmostEqual(np.degrees(tilt), degrees, delta=0.05)

    def test_level_text_and_a_side_without_text_have_none(self):
        self.assertIsNone(pages.text_tilt(self.turned(0.05)))
        self.assertIsNone(pages.text_tilt(Image.new("RGB", LETTER, "white")))


@unittest.skipUnless(
    shutil.which(pages.TESSERACT) and shutil.which("pdftotext"),
    "needs Tesseract and pdftotext",
)
class PdfTest(unittest.TestCase):
    def test_pdf_has_one_upright_searchable_page_per_prepared_page(self):
        prepared = [
            pages.prepare(text_page(), DPI, OPTIONS),
            pages.prepare(text_page().rotate(180), DPI, OPTIONS),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            saved = [
                pdf.save_page(page, Path(tmp), n) for n, page in enumerate(prepared)
            ]
            out = Path(tmp) / "scan.pdf"
            out.touch()  # the scan command claims the name first
            pdf.write(saved, out, OPTIONS)
            with pikepdf.open(out) as doc:
                self.assertEqual(len(doc.pages), 2)
                for page in doc.pages:
                    x0, y0, x1, y1 = (float(v) for v in page.mediabox)
                    self.assertAlmostEqual(
                        x1 - x0, LETTER[0] / DPI * POINTS_PER_INCH, delta=1
                    )
                    self.assertAlmostEqual(
                        y1 - y0, LETTER[1] / DPI * POINTS_PER_INCH, delta=1
                    )
            text = subprocess.run(
                ["pdftotext", str(out), "-"], capture_output=True, text=True, check=True
            ).stdout
        self.assertGreaterEqual(
            text.count("quick brown fox"), 6
        )  # three times per page, both pages


class LanguagesTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
    def test_missing_language_data_is_named(self):
        self.assertEqual(pages.missing_languages(["eng"]), [])
        self.assertEqual(pages.missing_languages(["eng", "zzz"]), ["zzz"])

    def test_no_tesseract_means_everything_is_missing(self):
        with mock.patch.object(pages, "TESSERACT", "no-such-tesseract"):
            self.assertEqual(
                pages.missing_languages(["eng"]), ["eng", pages.ORIENTATION_DATA]
            )


class ColorfulnessTest(unittest.TestCase):
    def test_gray_is_not_colorful(self):
        self.assertLess(pages.colorfulness(np.full((10, 10, 3), 128, np.uint8)), 1)

    def test_saturated_colors_are_colorful(self):
        rgb = np.zeros((10, 10, 3), np.uint8)
        rgb[:5, :, 0] = 255
        rgb[5:, :, 2] = 255
        self.assertGreater(pages.colorfulness(rgb), pages.COLORFUL)


if __name__ == "__main__":
    unittest.main()
