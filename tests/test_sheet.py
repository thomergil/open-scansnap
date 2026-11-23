"""Finding the sheet in a raw scan: synthetic scans built like the iX500's."""

import unittest

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from openscansnap import pages, sheet

DPI = 300
LETTER = (2550, 3300)  # 8.5 x 11 inches
FRAME = (2640, 3480)  # the scanner's area: a little wider and longer than the sheet
OFFSET = (40, 60)  # the sheet's top-left corner in the frame (backing above it)
BACKING, STRIP, PAPER, INK, SHADOW = 236, 222, 254, 20, 90
NOISE = 1.4  # the iX500's backing noise, measured
SOFT_EDGE_PX = 5  # Gaussian sigma: a ramp of about 1.7 mm, as on its front sensor
TOLERANCE_PX = round(0.5 / 25.4 * DPI)  # half a millimeter
NEAR_EDGE_PX = round(2 / 25.4 * DPI)  # print this close to the edge must stay


def page(upside_down: bool) -> np.ndarray:
    """A letter page with lines of print and a mark 2 mm from one edge."""
    image = Image.new("L", LETTER, PAPER)
    draw = ImageDraw.Draw(image)
    for y in range(500, 3000, 150):
        draw.rectangle((300, y, 2200, y + 30), fill=INK)
    draw.ellipse((300, NEAR_EDGE_PX, 700, NEAR_EDGE_PX + 400), outline=INK, width=12)
    if upside_down:
        image = image.rotate(180)
    return np.asarray(image, dtype=np.float64)


def scan(content: np.ndarray, tilt: float, seed: int = 0) -> Image.Image:
    """The scanner's view: backing with a darker strip at one side, the sheet
    tilted, its edges soft, and a shadow line along the trailing edge."""
    rng = np.random.default_rng(seed)
    frame = np.full(FRAME[::-1], float(BACKING))
    frame[:, :25] = STRIP
    sheet_mask = np.zeros(FRAME[::-1])
    sheet_mask[OFFSET[1] : OFFSET[1] + LETTER[1], OFFSET[0] : OFFSET[0] + LETTER[0]] = 1
    paper = np.full(FRAME[::-1], float(PAPER))
    paper[OFFSET[1] : OFFSET[1] + LETTER[1], OFFSET[0] : OFFSET[0] + LETTER[0]] = (
        content
    )
    shadow_row = OFFSET[1] + LETTER[1]
    frame[shadow_row : shadow_row + 4, OFFSET[0] : OFFSET[0] + LETTER[0]] = SHADOW
    soft = ndimage.gaussian_filter(sheet_mask, SOFT_EDGE_PX)
    combined = frame * (1 - soft) + paper * soft
    combined += rng.normal(0, NOISE, combined.shape)
    image = Image.fromarray(np.clip(combined, 0, 255).astype(np.uint8)).convert("RGB")
    return image.rotate(
        tilt, resample=Image.Resampling.BICUBIC, fillcolor=(BACKING,) * 3
    )


class SheetTest(unittest.TestCase):
    def test_sheet_is_found_whichever_way_it_was_fed(self):
        for upside_down in (False, True):
            for tilt in (1.5, -1.5):
                with self.subTest(upside_down=upside_down, tilt=tilt):
                    result = sheet.straighten_and_crop(
                        scan(page(upside_down), tilt), DPI
                    )
                    self.assertLessEqual(abs(result.width - LETTER[0]), TOLERANCE_PX)
                    self.assertLessEqual(abs(result.height - LETTER[1]), TOLERANCE_PX)
                    gray = np.asarray(result.convert("L"), dtype=np.float64)
                    # no backing left along any edge
                    for edge in (gray[2], gray[-3], gray[:, 2], gray[:, -3]):
                        self.assertGreater(np.median(edge), BACKING + 6 * NOISE)
                    # the mark 2 mm from the edge is still there
                    band = (
                        gray[: 2 * NEAR_EDGE_PX]
                        if not upside_down
                        else gray[-2 * NEAR_EDGE_PX :]
                    )
                    self.assertTrue((band < (PAPER + INK) / 2).any())

    def test_straight_sheet_is_not_turned(self):
        edges = sheet.find_edges(sheet._gray(scan(page(False), 0.0)), DPI)
        for side, edge in edges.items():
            with self.subTest(side=side):
                self.assertIsNotNone(edge)
                self.assertLess(abs(np.degrees(edge.tilt)), 0.1)

    def test_edges_are_where_the_sheet_is(self):
        edges = sheet.find_edges(sheet._gray(scan(page(False), 0.0)), DPI)
        expected = {
            "top": OFFSET[1],
            "bottom": OFFSET[1] + LETTER[1],
            "left": OFFSET[0],
            "right": OFFSET[0] + LETTER[0],
        }
        for side, position in expected.items():
            with self.subTest(side=side):
                self.assertLessEqual(abs(edges[side].position - position), TOLERANCE_PX)

    def test_text_turned_level_keeps_the_sheets_edges_at_the_border(self):
        # after a small turn the sheet's own edges stay at the image's border,
        # within the edge band (where pages.clear_edge_marks clears the
        # iX500's edge shadow), and the content stays: an outline drawn on
        # the sheet's edges must end up on the image's
        sheet_image = Image.new("RGB", LETTER, (PAPER,) * 3)
        draw = ImageDraw.Draw(sheet_image)
        width, height = LETTER
        draw.rectangle((0, 0, width - 1, height - 1), outline=(INK,) * 3, width=2)
        draw.rectangle((1000, 1500, 1500, 1800), fill=(INK,) * 3)  # the content
        band = pages.edge_band(DPI)
        for degrees in (0.5, -0.5):
            with self.subTest(degrees=degrees):
                level = sheet.turned_level(sheet_image, np.radians(degrees), band)
                ink = np.asarray(level.convert("L")) < (PAPER + INK) / 2
                content = ink[1550:1750, 1050:1450].copy()
                outline = ink.copy()
                outline[1400:1900, 900:1600] = False  # the content's surroundings
                ys, xs = np.nonzero(outline)
                to_border = np.minimum.reduce(
                    [xs, ys, level.width - 1 - xs, level.height - 1 - ys]
                )
                self.assertLessEqual(int(to_border.max()), 2)  # on the border
                self.assertGreater(content.mean(), 0.9)

    def test_a_large_text_turn_cuts_no_more_than_the_band(self):
        # a crooked copy, 4 degrees: the largest upright rectangle would cut
        # 10 mm off each side; print 5 mm from the edge stays
        sheet_image = Image.new("RGB", LETTER, (PAPER,) * 3)
        width, height = LETTER
        near = sheet.pixels(5.0, DPI)
        mark = (near, height // 2 - 40, near + 30, height // 2 + 40)
        ImageDraw.Draw(sheet_image).rectangle(mark, fill=(INK,) * 3)
        band = pages.edge_band(DPI)
        for degrees in (4.0, -4.0):
            with self.subTest(degrees=degrees):
                level = sheet.turned_level(sheet_image, np.radians(degrees), band)
                # cut, and by the band exactly: the rectangle would cut more
                self.assertEqual(level.size, (width - 2 * band, height - 2 * band))
                gray = np.asarray(level.convert("L"))
                left = gray[:, : near + 60]
                self.assertTrue((left < (PAPER + INK) / 2).any())  # the mark
                corner = int(gray[0, 0])  # filled with the paper, not black
                self.assertAlmostEqual(corner, PAPER, delta=3)

    def test_a_wavy_edge_is_still_an_edge(self):
        # the waviest real edge measured scattered 3.5 mm from its line; this
        # one, waving 13 mm deep, scatters 3.6 mm
        gray = sheet._gray(scan(page(False), 0.0))
        depth = sheet.pixels(13.0, DPI)
        for row in range(OFFSET[1], OFFSET[1] + LETTER[1]):
            reach = round(depth * (1 + np.sin(row / 300)) / 2)
            gray[row, : OFFSET[0] + reach] = BACKING
        self.assertIsNotNone(sheet.find_edges(gray, DPI)["left"])

    def test_text_turned_counterclockwise_is_turned_back(self):
        tone = 225  # off-white paper: the corners brought in must match it
        image = Image.new("RGB", LETTER, (tone,) * 3)
        ImageDraw.Draw(image).rectangle((300, 1600, 2200, 1630), fill=(INK,) * 3)
        for degrees in (3.0, -3.0):
            with self.subTest(degrees=degrees):
                askew = image.rotate(
                    degrees, resample=Image.Resampling.BICUBIC, fillcolor=(tone,) * 3
                )
                level = sheet.turned_level(askew, np.radians(degrees), LETTER[0])
                gray = np.asarray(level.convert("L"))
                rows = np.nonzero((gray < (tone + INK) / 2).any(axis=1))[0]
                self.assertLess(rows.max() - rows.min(), 40)  # level again
                self.assertAlmostEqual(int(gray[0, 0]), tone, delta=3)

    def test_an_edge_running_off_the_scan_does_not_read_as_the_border(self):
        # turned 3 degrees, a letter page is wider than the scan: where its
        # side runs past the scan's border, that border is no edge of it
        # (a sheet 2 degrees askew read as 0.3, 2026-11-23)
        for degrees in (3.0, 2.25):
            gray = sheet._gray(scan(page(False), degrees))
            for side in ("left", "right"):
                edge = sheet.find_edges(gray, DPI)[side]
                with self.subTest(degrees=degrees, side=side):
                    self.assertIsNotNone(edge)
                    self.assertAlmostEqual(np.degrees(edge.tilt), degrees, delta=0.05)

    def test_an_edge_whose_samples_scatter_is_not_an_edge(self):
        # paper barely brighter than the backing: on each line, paper is first
        # found at a random depth (10 mm scatter, as the failed edges measured)
        rng = np.random.default_rng(0)
        gray = np.full(FRAME[::-1], float(BACKING)) + rng.normal(0, NOISE, FRAME[::-1])
        top, bottom = OFFSET[1], OFFSET[1] + LETTER[1]
        for row in range(top, bottom):
            gray[row, OFFSET[0] + rng.integers(0, 500) : OFFSET[0] + LETTER[0]] = PAPER
        edges = sheet.find_edges(gray, DPI)
        self.assertIsNone(edges["left"])
        self.assertIsNotNone(edges["right"])

    def test_scan_without_backing_is_left_whole(self):
        # nothing to tell paper from: keep everything rather than cut
        blank = Image.new("RGB", LETTER, (PAPER,) * 3)
        self.assertEqual(sheet.straighten_and_crop(blank, DPI).size, LETTER)


if __name__ == "__main__":
    unittest.main()
