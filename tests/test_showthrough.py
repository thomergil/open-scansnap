"""Show-through: the other side's print, seen through the paper."""

import unittest
import warnings

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from test_pages import LETTER, text_page

from openscansnap import showthrough

DPI = 300
PAPER, INK = 254, 0


def faint(image: Image.Image, passes: float) -> Image.Image:
    """image as it shows through the paper: mirrored, its darkness scaled."""
    gray = np.asarray(ImageOps.mirror(image).convert("L"), dtype=np.float64)
    return Image.fromarray((PAPER - (PAPER - gray) * passes).astype(np.uint8)).convert(
        "RGB"
    )


class ShowThroughTest(unittest.TestCase):
    def test_other_side_showing_through_is_painted_out(self):
        front = text_page()  # print on the front
        back = Image.new("RGB", LETTER, (PAPER,) * 3)
        # the back: the front shows through, plus a mark of its own
        back = Image.fromarray(
            np.minimum(np.asarray(back), np.asarray(faint(front, 0.15)))
        )
        ImageDraw.Draw(back).rectangle((300, 3000, 1200, 3040), fill=(INK,) * 3)
        cleaned = np.asarray(
            showthrough.clean(back, front, DPI).convert("L"), dtype=np.float64
        )
        before = np.asarray(back.convert("L"), dtype=np.float64)
        text_area = np.s_[350:2000, 200:2400]
        self.assertGreater(
            (before[text_area] < PAPER - 10).mean(), 0.01
        )  # it was there
        self.assertLess((cleaned[text_area] < PAPER - 10).mean(), 0.001)  # it is gone
        self.assertLess(
            cleaned[3000:3040, 300:1200].mean(), 40
        )  # the back's own mark stays

    def test_marker_soaked_through_is_painted_out(self):
        # a thick marker drawing on the front soaks through almost as dark
        front = Image.new("RGB", LETTER, (PAPER,) * 3)
        ImageDraw.Draw(front).ellipse(
            (600, 900, 1800, 2400), outline=(INK,) * 3, width=40
        )
        back = faint(front, 0.35)  # real marker measured 0.13-0.24 (2026-11-22)
        cleaned = np.asarray(
            showthrough.clean(back, front, DPI).convert("L"), dtype=np.float64
        )
        self.assertLess((cleaned < PAPER - 10).mean(), 0.0005)

    def test_print_a_copy_touches_stays(self):
        # a pen drawing's copy runs into this side's own letters: one mark,
        # mostly the faint copy; the letters stay, the copy goes (a drawing
        # behind a typed page lost a t and an i, 2026-11-23)
        front = Image.new("RGB", LETTER, (PAPER,) * 3)
        ImageDraw.Draw(front).ellipse(
            (600, 900, 1800, 2400), outline=(INK,) * 3, width=12
        )
        back = faint(front, 0.3)
        draw = ImageDraw.Draw(back)
        # letters, thin strokes crossing the copy's ring at its left
        # (mirrored: the ring's left on the back is at x = 2550 - 1800)
        for x in (735, 760):
            draw.rectangle((x, 1600, x + 6, 1700), fill=(INK,) * 3)
        cleaned = np.asarray(
            showthrough.clean(back, front, DPI).convert("L"), dtype=np.float64
        )
        for x in (735, 760):
            with self.subTest(letter=x):
                self.assertLess(cleaned[1600:1700, x + 1 : x + 6].mean(), 40)
        ring = np.s_[1000:1400, 900:1700]  # the copy, away from the letters
        self.assertLess((cleaned[ring] < PAPER - 10).mean(), 0.0005)

    def test_own_print_stays_when_the_other_side_shows_only_it(self):
        # a printed front and a blank back: what the back has is the front
        # showing through, which must not take the front's own print away
        front = text_page()
        back = faint(front, 0.15)
        cleaned = np.asarray(
            showthrough.clean(front, back, DPI).convert("L"), dtype=np.float64
        )
        before = np.asarray(front.convert("L"), dtype=np.float64)
        self.assertEqual((cleaned < PAPER / 2).sum(), (before < PAPER / 2).sum())

    def test_writing_across_the_show_through_stays(self):
        # the back's own pen line crosses the front's print showing through
        front = text_page()
        back = faint(front, 0.15)
        ImageDraw.Draw(back).line((200, 380, 2400, 1500), fill=(30, 30, 30), width=6)
        cleaned = np.asarray(
            showthrough.clean(back, front, DPI).convert("L"), dtype=np.float64
        )
        line = np.asarray(back.convert("L")) < 60
        self.assertLess(cleaned[line].mean(), 60)

    def test_show_through_fainter_than_a_mark_is_painted_out(self):
        # so faint that the paper's own noise hides it pixel by pixel
        front = text_page()
        back = np.asarray(faint(front, 0.03), dtype=np.float64)
        back += np.random.default_rng(0).normal(0, 2.0, back.shape[:2])[..., None]
        back = Image.fromarray(np.clip(back, 0, 255).astype(np.uint8))
        cleaned = np.asarray(
            showthrough.clean(back, front, DPI).convert("L"), dtype=np.float64
        )
        under = np.asarray(front.convert("L"))[:, ::-1] < 128  # where the front shows
        self.assertLess(abs(cleaned[~under].mean() - cleaned[under].mean()), 1.0)

    def test_show_through_offset_from_the_other_side_is_painted_out(self):
        # the two sides never line up to the pixel
        front = text_page()
        back = Image.fromarray(
            np.roll(np.asarray(faint(front, 0.15)), (4, -6), axis=(0, 1))
        )
        cleaned = np.asarray(
            showthrough.clean(back, front, DPI).convert("L"), dtype=np.float64
        )
        self.assertLess((cleaned[350:2000, 200:2400] < PAPER - 10).mean(), 0.001)

    def test_side_of_one_tone_is_unchanged_without_warnings(self):
        side = Image.new("RGB", LETTER, (PAPER,) * 3)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # numpy's "Mean of empty slice"
            self.assertIs(showthrough.clean(side, text_page(), DPI), side)

    def test_print_on_both_sides_stays(self):
        # a letter printed on both sides, one side's print right behind the
        # other's and a shade darker: print, not a copy (2026-11-22)
        front = text_page(ink=40)
        back = ImageOps.mirror(text_page(ink=INK))
        cleaned = np.asarray(showthrough.clean(front, back, DPI).convert("L"))
        before = np.asarray(front.convert("L"))
        self.assertEqual((cleaned < PAPER / 2).sum(), (before < PAPER / 2).sum())

    def test_alignment_beyond_the_sheet_is_ignored(self):
        # two unrelated prints match best far apart: the crops' alignment is
        # used instead, and nothing of this side is removed
        front = text_page()
        back = Image.fromarray(
            np.roll(np.asarray(faint(front, 0.15)), (900, 0), axis=(0, 1))
        )
        cleaned = np.asarray(
            showthrough.clean(back, front, DPI).convert("L"), dtype=np.float64
        )
        before = np.asarray(back.convert("L"), dtype=np.float64)
        self.assertEqual((cleaned < PAPER - 10).sum(), (before < PAPER - 10).sum())

    def test_sheet_without_show_through_is_unchanged(self):
        front, back = text_page(), Image.new("RGB", LETTER, (PAPER,) * 3)
        self.assertIs(showthrough.clean(back, front, DPI), back)


if __name__ == "__main__":
    unittest.main()
