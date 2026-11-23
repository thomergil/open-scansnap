"""Finding the sheet in a raw scan: synthetic scans built like the iX500's."""

import unittest

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from openscansnap import sheet

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

    def test_scan_without_backing_is_left_whole(self):
        # nothing to tell paper from: keep everything rather than cut
        blank = Image.new("RGB", LETTER, (PAPER,) * 3)
        self.assertEqual(sheet.straighten_and_crop(blank, DPI).size, LETTER)


if __name__ == "__main__":
    unittest.main()
