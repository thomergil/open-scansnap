"""Find the sheet in a raw scan, straighten it and crop to it.

The scanner returns more than the sheet: its light gray backing shows on
every side, and the sheet may be slightly turned. The sheet's edges are found
statistically, so nothing depends on its size or on which way it was fed:

1. The backing: the scanner captures backing before and after every sheet,
   so the scan's first and last rows show its tones and noise. Paper is
   brighter than every backing tone (the iX500's backing is light gray);
   the paper's own level is the median of the pixels that bright.
2. From each side, sample lines every SAMPLE_MM inward. On each, the edge is
   where the first rise from the backing to paper brightness crosses half
   way between the two levels, to a fraction of a pixel: the usual position
   of a blurred edge (scanners blur it over a fraction of a millimeter).
3. One straight line per side through those points, by the Theil-Sen
   estimator (SciPy): the median of the slopes between all pairs of points,
   so stray points (a dog-ear) and a slightly wavy edge (a curled leading
   edge) do not pull it.

The sheet's tilt, from its left and right sides (they run along the feed),
straightens the scan; the edges found again on the straightened scan give the
crop. A side where too few sample lines find paper, or whose samples scatter
more than EDGE_SCATTER_MM, is not cropped: the result may keep some backing,
but never loses paper. job.assemble later turns a scan that has text level
by that text (turned_level): paper can feed in at a slant of its own.
"""

import logging
from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.stats import theilslopes

log = logging.getLogger(__name__)

MM_PER_INCH = 25.4
SAMPLE_MM = 0.7  # between sample lines: several hundred per side on a letter
BORDER_MM = 0.35  # the first and last rows, all backing
RUN_MM = 0.25  # paper brightness must last this long (not a speck)
SMOOTHING_PX = 1.0  # Gaussian sigma against sensor noise
# A level differs from a tone with noise when it lies more than NOISE_SIGMAS
# times that noise (its robust spread) away. Here paper is brighter than the
# brightest backing tones (this percentile of the border rows); in
# showthrough.py a mark is darker than its side's paper.
BACKING_PERCENTILE = 99.5
NOISE_SIGMAS = 6.0
# median absolute deviation to standard deviation, for normal noise
MAD_TO_SIGMA = 1.4826
# The least noise an 8-bit image has: its rounding to whole levels (the
# standard deviation of a uniform error of one level). A flat backing, as in
# a JPEG of one color, measures none.
QUANTIZATION_SIGMA = 1 / np.sqrt(12)
# A side is an edge when paper is found on at least this share of its sample
# lines: an edge runs along the sheet; fewer hits are specks, not paper.
FOUND_SHARE = 0.5
# A side is an edge when the median distance of its samples from the line
# fitted through them is at most this. Measured on 736 edges of 27 iX500 scans
# (2026-11-23): real edges at most 3.5 mm, the most on a wavy trailing edge;
# where paper is barely brighter than the backing, samples find paper at
# random depths, 10 mm and more (an off-white letter's left side: 18 mm, its
# fitted line turned 2.3 degrees). The cutoff lies in that gap.
EDGE_SCATTER_MM = 6.0
SIDES = ("top", "bottom", "left", "right")


@dataclass(frozen=True)
class Edge:
    position: float  # pixels from the scan's top (top, bottom) or left (left, right)
    tilt: float  # radians, counterclockwise


def straighten_and_crop(image: Image.Image, dpi: int) -> Image.Image:
    """The sheet in a raw scan, straight by its edges and without backing."""
    gray = _gray(image)
    edges = find_edges(gray, dpi)
    tilts = _tilts(edges)
    if tilts:
        angle = float(np.degrees(np.median(tilts)))
        if abs(angle) > 0:
            level = _backing(gray, dpi)[0]
            image = image.rotate(
                -angle,  # PIL turns counterclockwise; the sheet is turned by angle
                resample=Image.Resampling.BICUBIC,
                fillcolor=(round(level),) * len(image.getbands()),
            )
            gray = _gray(image)
            edges = find_edges(gray, dpi)
    height, width = gray.shape
    box = (
        _position(edges["left"], 0.0),
        _position(edges["top"], 0.0),
        _position(edges["right"], float(width)),
        _position(edges["bottom"], float(height)),
    )
    log.debug("sheet: tilt %s, box %s", tilts, box)
    left, top, right, bottom = (round(v) for v in box)
    if right - left < 1 or bottom - top < 1:
        return image
    return image.crop((left, top, right, bottom))


def turned_level(sheet: Image.Image, tilt: float, band: int) -> Image.Image:
    """sheet (a side cropped to its paper) with a tilt (radians,
    counterclockwise) undone, cut to the largest upright rectangle inside
    the turned sheet: its edges stay on the image's border, where the page
    decisions look for the scanner's edge shadow and a printer's color
    strips. That costs (half the side) x tan(tilt) off each side: 0.7 mm at
    0.3 degrees on a letter page, but 7 mm at 3 degrees (a crooked copy).
    No more than band pixels (the edge band, pages.edge_band) come off a
    side, so print near the edge stays; the corners left over are filled
    with the sheet's paper."""
    paper = tuple(round(float(np.median(band))) for band in sheet.split())
    turned = sheet.rotate(
        -float(np.degrees(tilt)),  # PIL turns counterclockwise, about the center
        resample=Image.Resampling.BICUBIC,
        fillcolor=paper if len(paper) > 1 else paper[0],
    )
    # the centered upright rectangle whose corners lie on the turned sides:
    # a cos + b sin = W/2 and a sin + b cos = H/2 for half-sides a, b; past
    # 45 degrees none fits, and nothing is cut
    width, height = sheet.size
    cos, sin = float(np.cos(abs(tilt))), float(np.sin(abs(tilt)))
    across = cos * cos - sin * sin
    if across <= 0:
        return turned
    a = (width / 2 * cos - height / 2 * sin) / across
    b = (height / 2 * cos - width / 2 * sin) / across
    across_cut = min(max(int(np.ceil(width / 2 - a)), 0), band)
    down_cut = min(max(int(np.ceil(height / 2 - b)), 0), band)
    if 2 * across_cut >= width or 2 * down_cut >= height:
        return turned
    return turned.crop((across_cut, down_cut, width - across_cut, height - down_cut))


def _tilts(edges: dict[str, "Edge | None"]) -> list[float]:
    """The sheet's tilt as its sides measure it. A sheet is a rectangle, so
    all four share one tilt; the left and right sides run along the feed,
    have the most samples and do not curl like a leading edge, so they
    decide when both are found."""
    long_sides = [edges[s].tilt for s in ("left", "right") if edges[s] is not None]
    if len(long_sides) == 2:
        return long_sides
    return [e.tilt for e in edges.values() if e is not None]


def find_edges(gray: np.ndarray, dpi: int) -> dict[str, Edge | None]:
    """Each side's edge in a smoothed grayscale scan; None where the
    samples do not agree."""
    level, threshold = _backing(gray, dpi)
    paper = gray[gray > threshold]
    if not paper.size:
        return dict.fromkeys(SIDES)
    half = (level + float(np.median(paper))) / 2
    return {side: _edge(gray, side, half, threshold, dpi) for side in SIDES}


def _gray(image: Image.Image) -> np.ndarray:
    return ndimage.gaussian_filter(
        np.asarray(image.convert("L"), dtype=np.float64), SMOOTHING_PX
    )


def pixels(mm: float, dpi: int) -> int:
    return max(1, round(mm / MM_PER_INCH * dpi))


def level_and_noise(values: np.ndarray) -> tuple[float, float]:
    """The level of values that are one tone with noise (the median), and
    the noise's standard deviation, measured robustly (the median absolute
    deviation) and at least QUANTIZATION_SIGMA."""
    level = float(np.median(values))
    noise = MAD_TO_SIGMA * float(np.median(np.abs(values - level)))
    return level, max(noise, QUANTIZATION_SIGMA)


def _backing(gray: np.ndarray, dpi: int) -> tuple[float, float]:
    """The backing's level, and the brightness above which a pixel is paper."""
    rows = pixels(BORDER_MM, dpi)
    border = np.concatenate([gray[:rows].ravel(), gray[-rows:].ravel()])
    level, noise = level_and_noise(border)
    return level, float(
        np.percentile(border, BACKING_PERCENTILE)
    ) + NOISE_SIGMAS * noise


def _edge(
    gray: np.ndarray, side: str, half: float, threshold: float, dpi: int
) -> Edge | None:
    # turn the scan so that this side is the top: lines run down the columns
    view = {"top": gray, "bottom": gray[::-1], "left": gray.T, "right": gray.T[::-1]}[
        side
    ]
    run = pixels(RUN_MM, dpi)
    depths, columns = [], []
    for column in range(0, view.shape[1], pixels(SAMPLE_MM, dpi)):
        depth = _depth(view[:, column], half, threshold, run)
        if depth is not None:
            depths.append(depth)
            columns.append(column)
    lines = len(range(0, view.shape[1], pixels(SAMPLE_MM, dpi)))
    if len(depths) < max(2, FOUND_SHARE * lines):
        log.info(
            "sheet: %s edge not found (paper on %d of %d lines)",
            side,
            len(depths),
            lines,
        )
        return None
    # Theil-Sen: the median of the slopes between all pairs of points, robust
    # to stray points and to an edge that is not quite straight
    slope, intercept, _, _ = theilslopes(depths, columns)
    across = view.shape[1] / 2
    depth = intercept + slope * across
    scatter = float(
        np.median(
            np.abs(np.asarray(depths) - (intercept + slope * np.asarray(columns)))
        )
    )
    log.debug(
        "sheet: %s: paper on %d of %d lines, scatter %.1f px",
        side,
        len(depths),
        lines,
        scatter,
    )
    if scatter > pixels(EDGE_SCATTER_MM, dpi):
        log.info("sheet: %s edge not found (its samples scatter)", side)
        return None
    # back to the scan's coordinates; the tilt's sign follows the turn above
    position = {
        "top": depth,
        "bottom": gray.shape[0] - 1 - depth,
        "left": depth,
        "right": gray.shape[1] - 1 - depth,
    }[side]
    across_scan = gray.shape[0] if side in ("top", "bottom") else gray.shape[1]
    if not 0 <= position <= across_scan:
        log.info("sheet: %s edge fitted outside the scan", side)
        return None
    tilt = (
        float(np.arctan(slope)) * {"top": -1, "bottom": 1, "left": 1, "right": -1}[side]
    )
    return Edge(position=float(position), tilt=tilt)


def _depth(line: np.ndarray, half: float, threshold: float, run: int) -> float | None:
    """The position, in pixels from the scan's border inward, at which
    paper starts on line: where the first rise that reaches paper
    brightness crosses half (halfway from the backing's level to the
    paper's). None when the line finds no paper, or
    no backing before it: there the sheet runs past the scan's border, and
    its edge is out of sight (a sheet turned 2.25 degrees is wider than the
    iX500's window: the border, as straight as the scan, read as its edge,
    2026-11-23)."""
    paper = line > threshold
    lasting = np.convolve(paper, np.ones(run, dtype=int), "valid") == run
    if not lasting.any():
        return None
    end = int(np.argmax(lasting))
    below = np.nonzero(line[:end] <= half)[0]
    if not len(below):
        return None
    i = int(below[-1])  # line[i] <= half < line[i + 1]
    return i + (half - line[i]) / (line[i + 1] - line[i])


def _position(edge: Edge | None, border: float) -> float:
    return border if edge is None else edge.position
