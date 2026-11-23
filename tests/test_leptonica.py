"""Leptonica's skew measure, through ctypes."""

import unittest
from unittest import mock

import numpy as np
from PIL import Image
from test_sheet import INK, LETTER, PAPER, page

from openscansnap import bundle, leptonica, sane


def lines(degrees: float) -> np.ndarray:
    """The ink of a letter page of print, turned."""
    turned = Image.fromarray(page(False).astype(np.uint8)).rotate(
        degrees, fillcolor=PAPER
    )
    return np.asarray(turned) < (PAPER + INK) / 2


@unittest.skipUnless(bundle.leptonica(), "needs Leptonica")
class FindSkewTest(unittest.TestCase):
    def test_lines_turned_counterclockwise_measure_positive(self):
        for degrees in (2.0, -0.7):
            with self.subTest(degrees=degrees):
                skew = leptonica.find_skew(lines(degrees))
                self.assertAlmostEqual(skew.degrees, degrees, delta=0.05)
                self.assertGreater(skew.confidence, 3)

    def test_a_page_without_ink_is_measured_unsure(self):
        skew = leptonica.find_skew(np.zeros(LETTER[::-1], dtype=bool))
        self.assertLess(skew.confidence, 3)


class MissingTest(unittest.TestCase):
    def test_without_leptonica_says_what_to_install(self):
        leptonica._library.cache_clear()
        self.addCleanup(leptonica._library.cache_clear)
        with (
            mock.patch.object(bundle, "leptonica", return_value=None),
            self.assertRaisesRegex(sane.ScanError, "install Tesseract"),
        ):
            leptonica.find_skew(np.zeros((10, 10), dtype=bool))


if __name__ == "__main__":
    unittest.main()
