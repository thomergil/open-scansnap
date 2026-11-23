"""Paint out show-through: the print on the other side of the sheet, seen or
soaked through the paper.

The scanner reads both sides of every sheet, so the other side's print is
known. The back is the front mirrored left to right; phase correlation
(scikit-image) lines the two up to a fraction of a pixel. A mark on one side
is the other side's print coming through when

- more of it lies on the other side's ink than off it (a copy sits where its
  original is; real writing that crosses the other side's print covers only
  the crossing), and
- it is at most COPY_DARKNESS as dark there as the other side's ink (paper
  stops most of the light; print on both sides of a page, one over the
  other, is about equally dark).

Both hold for faint show-through and for dark ink that soaked through (a
marker). Such marks, with their soft edges, become paper. So does anything
too faint here to be part of a mark, where the other side has ink. This is the idea of Sharma, "Show-through cancellation in
scans of duplex printed documents" (IEEE Transactions on Image Processing,
2001).
"""

import logging

import numpy as np
from PIL import Image
from scipy import ndimage
from skimage.filters import apply_hysteresis_threshold, threshold_otsu
from skimage.registration import phase_cross_correlation

from . import pages, sheet

log = logging.getLogger(__name__)

ALIGNMENT_UPSAMPLING = 4  # phase correlation to a quarter pixel
# A copy keeps at most this share of its original's darkness: print is seen
# through paper, which stops most of the light (office paper's opacity, ISO
# 2471, is around 90%). Measured (2026-11-22): copies 0.02-0.24 of the other
# side's darkness, print over the other side's print 0.8-0.96.
COPY_DARKNESS = 0.5
# The two sides' crops agree to within this: each edge is found to about half
# a millimeter (sheet.py, tests/test_sheet.py TOLERANCE_PX), and a sheet
# shifts little between the two sensors
ALIGNMENT_MM = 2.0
# A mark's soft edge: pixels joining it that are darker than the paper by
# more than this many times its noise (2: outside 95% of the paper's tones)
FRINGE_SIGMAS = 2.0


def clean(side: Image.Image, other: Image.Image, dpi: int) -> Image.Image:
    """side with other's print coming through painted out (both cropped to
    the sheet, scanned at dpi; other is the sheet's other side as
    scanned)."""
    gray = np.asarray(side.convert("L"), dtype=np.float64)
    other_gray = np.asarray(other.convert("L"), dtype=np.float64)
    if gray.min() == gray.max() or other_gray.min() == other_gray.max():
        return side  # one tone: nothing on it, or nothing to come through
    height = min(gray.shape[0], other_gray.shape[0])
    width = min(gray.shape[1], other_gray.shape[1])
    here = gray[:height, :width]
    mirror = other_gray[:height, :width][:, ::-1]  # the other side, from this one
    shift, _, _ = phase_cross_correlation(
        255 - here, 255 - mirror, upsample_factor=ALIGNMENT_UPSAMPLING
    )
    # both sides are cropped to the same sheet, so a copy lies within a few
    # millimeters of its original; a larger shift is a chance match between
    # unrelated prints (both sides printed): trust the crop instead
    reach = np.abs(np.subtract(gray.shape, other_gray.shape)) + sheet.pixels(
        ALIGNMENT_MM, dpi
    )
    if np.any(np.abs(shift) > reach):
        log.debug("show-through: alignment %s beyond the sheet's, ignored", shift)
        shift = np.zeros(2)
    aligned = ndimage.shift(mirror, shift, order=1, mode="nearest")
    paper, noise = _paper(gray)
    other_paper, other_noise = _paper(other_gray)
    # the other side's marks, blurred edges included, as this side sees them
    other_ink = aligned < other_paper - sheet.NOISE_SIGMAS * other_noise
    if not other_ink.any():
        return side

    # this side's marks, each with its soft edge: what joins a mark and still
    # stands out from the paper's noise (hysteresis thresholding)
    own_ink = apply_hysteresis_threshold(
        paper - gray, FRINGE_SIGMAS * noise, sheet.NOISE_SIGMAS * noise
    )
    marks, count = ndimage.label(
        own_ink[:height, :width], structure=pages.EIGHT_NEIGHBORS
    )
    if not count:
        return side
    numbers = np.arange(count + 1)
    size = ndimage.sum_labels(np.ones_like(here), marks, numbers)
    on = ndimage.sum_labels(other_ink, marks, numbers)
    # each mark's darkness, and the other side's, where they coincide
    with np.errstate(invalid="ignore"):  # marks off the other side's ink
        dark_here = paper - ndimage.mean(here, marks * other_ink, numbers)
        dark_there = other_paper - ndimage.mean(aligned, marks * other_ink, numbers)
    coming_through = (on > size - on) & (dark_here <= COPY_DARKNESS * dark_there)
    coming_through[0] = False  # the background
    through = np.zeros(gray.shape, dtype=bool)
    # the marks coming through, edges and all, and, where the other side has
    # ink, what is too faint here to be part of a mark: the faintest copies
    through[:height, :width] = coming_through[marks] | (
        other_ink & ~own_ink[:height, :width]
    )
    if not through.any():
        return side
    log.debug(
        "show-through: %d of %d marks, %.2f%% of the side; shift %s",
        int(coming_through.sum()),
        count,
        through.mean() * 100,
        shift,
    )
    rgb = np.asarray(side.convert("RGB"), dtype=np.float64).copy()
    # the paper's color from paper alone: not from the show-through itself
    rgb[through] = pages.paper_background(rgb, ~own_ink & ~through)[through]
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")


def _paper(gray: np.ndarray) -> tuple[float, float]:
    """The level of a side's paper (Otsu's brighter class) and its noise."""
    return sheet.level_and_noise(gray[gray > threshold_otsu(gray)])
