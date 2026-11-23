"""Decide how to store each scanned page, from measurements of that page.

Each side arrives already cropped to the sheet and cleaned of the other
side's show-through, as far as the scan's Options ask (job.py). Here it is
turned upright (Tesseract's orientation detection) and stored as its
Options.mode says. In the automatic mode each page gets what suits it:

- dropped when blank: nothing on it is clearly darker than the paper, or it
  has only faint marks that Tesseract reads better mirrored (show-through,
  smudges) or, when they do not read as text, that cover less than
  PICTURE_SHARE of the side (in every mode that removes blank sides); a side
  with colored ink is never blank;
- color, when any ink on it is colored (a pen stroke, a stamp), judged by
  its CIELAB chroma, or when the page as a whole is colorful (Hasler and
  Süsstrunk's metric, for photos); with clean-up, a color or gray page that
  is mostly paper is put on white paper (whitened);
- black and white otherwise: Otsu's global threshold, on the scan as it is
  or, with clean-up, with the paper's unevenness (shading, the broad shadow
  of folds) divided out first, whichever Tesseract reads best; a global
  threshold leaves the faint lines of creases out, where a local one
  (Sauvola's) turned them into black lines. With clean-up, marks at the
  paper edge that fit within the edge band (its shadow, dust) are cleared;
- grayscale when no binarization yields readable text, as for a photo.

Pages are stored at the resolution of the chosen Options.quality.
"""

import csv
import functools
import io
import logging
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from PIL import Image
from scipy import ndimage
from skimage.color import rgb2lab
from skimage.filters import threshold_otsu

from . import leptonica, sheet
from .options import Options, ScanMode

log = logging.getLogger(__name__)

# Ink contrast: how much darker than the paper Otsu's darker class is, as a
# share of the paper's brightness. Measured on one real iX500 sheet: printed
# front 0.63, blank back with show-through 0.19. On synthetic sides: paper
# noise 0.02, a faint pencil line 0.30, a block photo 0.52. NO_INK and INK
# sit between those classes. Below NO_INK a side is blank; from INK up it
# carries ink. In between, it carries ink if Tesseract reads at least
# ONE_WORD and the text does not read better mirrored (show-through is the
# other side's mirror image); when nothing reads as text, it carries ink if
# its marks cover at least PICTURE_SHARE of it. The share classes are 300
# times apart: the show-through covered 0.15% of its side, the photo 46%.
# A photo paler than INK is therefore still kept.
NO_INK = 0.1
INK = 0.5
ONE_WORD = 1.0
PICTURE_SHARE = 0.2
# Hasler and Süsstrunk (2003), "Measuring colourfulness in natural images":
# below 15 an image is "not colourful".
COLORFUL = 15.0
# A mark of ink is colored when its median CIELAB chroma C* (distance from
# gray, in perceptual units) reaches this: above the most black print showed
# by about one just-noticeable difference (CIE delta E 2.3). On the iX500,
# black print, creases and show-through measured at most 8; a navy letterhead
# 12.5-14, blue ballpoint 20 and up (2026-11-22).
COLORED_INK_CHROMA = 10.0
# Marks smaller than the smallest print in normal use do not decide the page.
LEGIBLE_POINTS = 6.0
POINTS_PER_INCH = 72
# The scanner leaves the paper edge's shadow and dust along the edge of the
# cropped page (measured on the iX500: a bar 0.9 mm wide, touching the edge).
# On black-and-white pages, marks that touch the edge and lie within this
# distance of it are cleared. Print near the edge stays: it does not touch
# the edge (a letterhead seal 2 mm from it), or reaches past the band (a
# rule running off the page).
EDGE_BAND_MM = 3.0
# A color or gray page is whitened (on white paper) only when paper (paper_tone,
# within NOISE_SIGMAS of its grain) is at least this share of it: halfway
# between what was measured (2026-11-23, iX500), document pages 0.67-0.78
# (letters with a logo, forms with colored boxes, checklists) and a card
# with two photos 0.48. Six pages: tune with more. Wrong either way, a page
# is only left as scanned, or a picture's lightest parts turn white. Paper
# shaded deeply over much of the page (a deep fold) is left as scanned.
PAPER_MAJORITY = 0.58
WHITE = 255
# Background estimation works on a copy shrunk so that its scale is at
# least this many pixels: the result is smooth on that scale anyway.
BACKGROUND_SAMPLES_PER_SCALE = 4
TESSERACT = "tesseract"
TESSERACT_LAYOUT = "3"  # automatic page segmentation
TESSERACT_ORIENTATION = "0"  # orientation and script detection only
TESSERACT_TOO_LITTLE_TEXT = "Too few characters"  # its reason for no orientation
# Leptonica's deskew (pixDeskewGeneral) turns a page only when its skew
# measure is this sure and the turn at least this large (MinAllowedConfidence
# and MinDeskewAngle in skew.c, Leptonica 1.87). Measured: text pages 4-17,
# a blank page and a pen drawing 1.2 (2026-11-23).
SKEW_CONFIDENCE = 3.0
SKEW_LEAST_DEGREES = 0.1
ORIENTATION_DATA = "osd"
TSV_CONFIDENCE, TSV_TEXT = "conf", "text"
CONFIDENCE_SCALE = 100.0
# Tesseract's orientation report: the clockwise turn that makes text upright
OSD_ROTATE = re.compile(r"^Rotate: (\d+)$", re.MULTILINE)
QUARTER_TURN = 90
EIGHT_NEIGHBORS = np.ones((3, 3), dtype=bool)  # diagonal pixels connect marks


class Mode(StrEnum):
    COLOR = "color"
    GRAY = "gray"
    LINEART = "lineart"


# The modes that store every page alike, and how
FIXED_MODES = {
    ScanMode.COLOR: Mode.COLOR,
    ScanMode.PHOTO: Mode.COLOR,
    ScanMode.AS_SCANNED: Mode.COLOR,
    ScanMode.GRAY: Mode.GRAY,
}


@dataclass(frozen=True)
class Page:
    image: Image.Image  # "RGB", "L" or "1", matching mode
    dpi: int
    mode: Mode
    method: str  # how the page was rendered, for the log
    readable_words: float  # Tesseract's expected number of correctly read words


def prepare(
    image: Image.Image,
    dpi: int,
    options: Options,
    turn: int | None = None,
    print_from: Image.Image | None = None,
) -> Page | None:
    """The stored form of one side, already cropped to the sheet; None when
    it is blank and blank sides are left out. turn is the clockwise turn
    that makes it upright, when already known (from the sheet's other side);
    None finds it here. Blankness, turn, mode and threshold method are
    judged on image; a black and white page takes its ink from print_from
    instead, when given (the same side, in other pixels), binarized the way
    image read best."""
    if options.automatic_language:
        raise ValueError("pages.prepare needs the side's language (job.language_of)")
    options = options.in_effect()
    contrast, share = side_contrast(image, dpi)
    if options.remove_blank and contrast < NO_INK:
        return None

    if not options.upright:
        turn = 0
    elif turn is None:
        turn = orientation(Detection(image, dpi), options.language) or 0
    image = turned(image, turn)
    print_from = image if print_from is None else turned(print_from, turn)
    auto = options.mode == ScanMode.AUTO
    faint = contrast < INK
    color = auto and (
        colorfulness(np.asarray(image.convert("RGB"))) >= COLORFUL
        or has_colored_ink(image, dpi)
    )
    # stored alike whatever is read: colored, or a mode that stores all alike
    fixed = Mode.COLOR if color else FIXED_MODES.get(options.mode)
    method = "colorful" if color else options.mode
    if fixed is not None and not (options.remove_blank and faint):
        return stored(image, dpi, options, fixed, method, 0.0)

    # Faint sides, and sides that may become black and white, are read:
    # binarized by Otsu's threshold, and with clean-up also with the
    # paper's shading (creases) divided out, whichever reads best.
    binarize = {"otsu": lambda gray: gray}
    if options.clean_up:
        binarize["otsu-flat"] = flatten
    gray = np.asarray(image.convert("L"))
    sources = {name: way(gray) for name, way in binarize.items()}
    limits = {name: threshold_otsu(source) for name, source in sources.items()}
    candidates = {name: sources[name] <= limits[name] for name in sources}
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        scores = dict(
            zip(
                candidates,
                pool.map(
                    lambda ink: readable_words(ink, dpi, options.language),
                    candidates.values(),
                ),
            )
        )
    best = max(scores, key=scores.get)
    readable = scores[best] >= ONE_WORD
    if (
        options.remove_blank
        and faint
        and faint_side_is_blank(candidates[best], scores[best], share, dpi, options)
        # a side with colored ink (C* >= COLORED_INK_CHROMA) is kept: paper,
        # creases and show-through measure at most C* 8, and a pen drawing can
        # be too small for PICTURE_SHARE
        and not (color or has_colored_ink(image, dpi))
    ):
        return None
    if fixed is not None:
        return stored(image, dpi, options, fixed, method, scores[best])
    if auto and not readable:
        return stored(image, dpi, options, Mode.GRAY, "no readable text", scores[best])

    source, limit = sources[best], limits[best]
    if print_from is not image:
        source = binarize[best](np.asarray(print_from.convert("L")))
        limit = threshold_otsu(source)
    factor = options.lineart_dpi / dpi
    ink = upscale(source, factor) <= limit
    if options.clean_up:
        clear_edge_marks(ink, edge_band(options.lineart_dpi))
    return Page(
        Image.fromarray(~ink).convert("1"),
        options.lineart_dpi,
        Mode.LINEART,
        best,
        scores[best],
    )


def side_contrast(image: Image.Image, dpi: int) -> tuple[float, float]:
    """ink_contrast of a side, its edge band aside (the paper's edge)."""
    gray = np.asarray(image.convert("L"))
    band = edge_band(dpi)
    return ink_contrast(gray[band:-band, band:-band])


def has_ink(image: Image.Image, dpi: int) -> bool:
    """Whether anything on a side is clearly darker than its paper."""
    return side_contrast(image, dpi)[0] >= NO_INK


def faint_side_is_blank(
    ink: np.ndarray, words: float, share: float, dpi: int, options: Options
) -> bool:
    """Whether a side with only faint marks (ink, of which Tesseract read
    words) is blank: its marks do not read as text and cover less than
    PICTURE_SHARE of it, or read better mirrored (the other side showing
    through)."""
    if words < ONE_WORD:
        return share < PICTURE_SHARE
    return readable_words(np.fliplr(ink), dpi, options.language) > words


def stored(
    image: Image.Image,
    dpi: int,
    options: Options,
    mode: Mode,
    method: str,
    readable: float,
) -> Page:
    """A color or gray page at the chosen quality's resolution; with clean-up,
    on white paper (whitened)."""
    if options.page_dpi != dpi:
        factor = options.page_dpi / dpi
        image = image.resize(
            (round(image.width * factor), round(image.height * factor)),
            Image.Resampling.LANCZOS,
        )
    if options.clean_up:
        image = whitened(image)
    return Page(
        image.convert("RGB" if mode == Mode.COLOR else "L"),
        options.page_dpi,
        mode,
        method,
        readable,
    )


@dataclass(frozen=True, eq=False)
class Detection:
    """A side, and what Tesseract's orientation and script detection tells
    of it (the side's turn, and its script: languages.py); each is worked
    out once, when first asked for."""

    image: Image.Image
    dpi: int

    @functools.cached_property
    def ink(self) -> np.ndarray:
        return binarized(self.image)

    @functools.cached_property
    def report(self) -> str | None:
        """Tesseract's report; None when the side has too little text."""
        return osd(self.ink, self.dpi)


def proposed_turn(report: str | None) -> int:
    """The clockwise turn Tesseract's orientation report proposes; 0 when it
    has none."""
    proposal = OSD_ROTATE.search(report or "")
    return int(proposal.group(1)) if proposal else 0


def turned_ink(ink: np.ndarray, turn: int) -> np.ndarray:
    """ink turned clockwise by turn degrees."""
    return np.rot90(ink, k=-turn // QUARTER_TURN)


def orientation(side: Detection, language: str) -> int | None:
    """The clockwise turn (0, 90, 180 or 270 degrees) that makes the side's
    text read upright; None when it has too little text to tell. Tesseract
    proposes a turn, kept only if the side then reads better."""
    ink, dpi = side.ink, side.dpi
    if side.report is None:
        return None
    turn = proposed_turn(side.report)
    if not turn:
        return 0
    proposed = turned_ink(ink, turn)
    with ThreadPoolExecutor(max_workers=2) as pool:
        as_is, after = pool.map(
            lambda mask: readable_words(mask, dpi, language), (ink, proposed)
        )
    return turn if after > as_is else 0


def turned(image: Image.Image, turn: int) -> Image.Image:
    """image turned clockwise by turn degrees."""
    return (
        image.rotate(-turn, expand=True) if turn else image
    )  # PIL turns counterclockwise


def missing_languages(languages: list[str]) -> list[str]:
    """The Tesseract data this module needs for languages but that is not
    installed; all of it when Tesseract itself is missing."""
    installed = set(installed_languages())
    return [name for name in [*languages, ORIENTATION_DATA] if name not in installed]


def installed_languages() -> list[str]:
    """Tesseract's installed data: languages, and the orientation data;
    none when Tesseract itself is missing."""
    try:
        listing = subprocess.run(
            [TESSERACT, "--list-langs"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [
        line.strip() for line in listing.stdout.splitlines()[1:]
    ]  # after the header


def clear_edge_marks(ink: np.ndarray, band: int) -> None:
    """Clear the marks (connected ink, True) that touch an edge of the page
    and lie within band pixels of that edge."""
    labels, _ = ndimage.label(ink, structure=EIGHT_NEIGHBORS)
    height, width = ink.shape
    border = np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1]))
    boxes = ndimage.find_objects(labels)
    for number in np.unique(border[border > 0]):
        rows, cols = boxes[number - 1]
        touched = {
            edge
            for edge, touches in (
                ("top", rows.start == 0),
                ("bottom", rows.stop == height),
                ("left", cols.start == 0),
                ("right", cols.stop == width),
            )
            if touches
        }
        if touched & band_edges(rows, cols, ink.shape, band):
            ink[rows, cols][labels[rows, cols] == number] = False


def band_edges(rows: slice, cols: slice, shape: tuple[int, ...], band: int) -> set[str]:
    """The edges of a page of shape whose band (band pixels wide) holds the
    whole box (rows, cols): top, bottom, left, right."""
    height, width = shape[:2]
    return {
        edge
        for edge, within in (
            ("top", rows.stop <= band),
            ("bottom", rows.start >= height - band),
            ("left", cols.stop <= band),
            ("right", cols.start >= width - band),
        )
        if within
    }


def edge_band(dpi: int) -> int:
    """Width of the edge band in pixels."""
    return sheet.pixels(EDGE_BAND_MM, dpi)


def ink_contrast(gray: np.ndarray) -> tuple[float, float]:
    """Split the side with Otsu's threshold; return how much darker the
    darker class is than the paper (a share of the paper's brightness) and
    the share of the side the darker class covers."""
    values = gray.astype(np.float64)
    if values.min() == values.max():
        return 0.0, 0.0
    threshold = threshold_otsu(values)
    dark, paper = values[values <= threshold], values[values > threshold]
    return float((paper.mean() - dark.mean()) / paper.mean()), dark.size / values.size


def has_colored_ink(image: Image.Image, dpi: int) -> bool:
    """Whether any mark of ink at least LEGIBLE_POINTS tall or wide, and not
    within the edge band, is colored. Marks are ink by Otsu's threshold; each
    one's median chroma is measured, so the scanner's slight color fringes on
    black print do not count. The edge band holds no content, but can hold a
    printer's color calibration strips."""
    step = 2  # half the resolution is plenty to tell a mark's color, and fast
    small = image.convert("RGB").reduce(step)
    gray = np.asarray(small.convert("L"), dtype=np.float64)
    ink = gray <= threshold_otsu(gray)
    labels, count = ndimage.label(ink, structure=EIGHT_NEIGHBORS)
    if not count:
        return False
    chroma = np.hypot(*np.moveaxis(rgb2lab(np.asarray(small))[..., 1:], -1, 0))
    smallest = LEGIBLE_POINTS / POINTS_PER_INCH * dpi / step
    band = edge_band(dpi) // step
    for number, (rows, cols) in enumerate(ndimage.find_objects(labels), 1):
        if max(rows.stop - rows.start, cols.stop - cols.start) < smallest or band_edges(
            rows, cols, ink.shape, band
        ):
            continue
        mark = labels[rows, cols] == number
        if np.median(chroma[rows, cols][mark]) >= COLORED_INK_CHROMA:
            return True
    return False


def colorfulness(rgb: np.ndarray) -> float:
    """Hasler and Süsstrunk's colorfulness of an RGB image."""
    r, g, b = (rgb[..., i].astype(np.float64) for i in range(3))
    rg = r - g
    yb = (r + g) / 2 - b
    return float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))


def whitened(image: Image.Image) -> Image.Image:
    """image on white paper, as a document: the paper's brightness and tint,
    its folds and shadows included, divided out of each channel (as flatten
    does), and the paper's grain lifted to white; colored marks keep their
    color. Unchanged unless paper is at least PAPER_MAJORITY of the page:
    on a page with less paper than that, a picture's light parts would turn
    white."""
    # single precision: a 300 dpi page's channels take half the memory, and
    # gray levels need far less than float32's precision
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    gray = rgb.mean(axis=2)
    level, noise = paper_tone(gray)
    # a document: paper, within its grain of its level, is most of the page;
    # light parts of a picture are not that close to it
    if (np.abs(gray - level) <= sheet.NOISE_SIGMAS * noise).mean() < PAPER_MAJORITY:
        return image
    # paper, shaded too (folds, shadows): the brighter class, but not what is
    # more colorful than the paper (a highlighter stroke, a pastel fill)
    lab = rgb2lab(rgb / WHITE)
    tint = np.median(
        lab[..., 1:][np.abs(gray - level) <= sheet.NOISE_SIGMAS * noise], axis=0
    )
    colored = np.hypot(*np.moveaxis(lab[..., 1:] - tint, -1, 0)) >= COLORED_INK_CHROMA
    paper = (gray > threshold_otsu(gray)) & ~colored
    # paper: about WHITE, in gray levels as the noise is measured
    flat = rgb / np.maximum(paper_background(rgb, paper), 1.0) * WHITE
    level, noise = sheet.level_and_noise(flat.mean(axis=2)[paper])
    white = level - sheet.NOISE_SIGMAS * noise
    out = np.clip(flat / white * WHITE, 0, WHITE).astype(np.uint8)
    return Image.fromarray(out, "RGB").convert(image.mode)


def paper_tone(gray: np.ndarray) -> tuple[float, float]:
    """The paper's level, the most common tone of the brighter class by
    Otsu's threshold; and its grain, the noise between neighboring pixels
    there, which a picture's spread of tones does not widen."""
    bright = gray > threshold_otsu(gray)
    counts, _ = np.histogram(gray[bright], bins=WHITE + 1, range=(0, WHITE + 1))
    level = float(np.argmax(counts)) + 0.5
    pairs = bright[:, 1:] & bright[:, :-1]
    steps = (gray[:, 1:] - gray[:, :-1])[pairs]
    # a difference of two pixels has sqrt(2) times one pixel's noise
    noise = sheet.MAD_TO_SIGMA * float(np.median(np.abs(steps))) / np.sqrt(2)
    return level, max(noise, sheet.QUANTIZATION_SIGMA)


def flatten(gray: np.ndarray) -> np.ndarray:
    """gray with the paper's own unevenness divided out, as background
    normalization does (Leptonica's pixBackgroundNorm): the paper's
    brightness is estimated from paper pixels only and interpolated under
    the ink (normalized convolution). Its scale is the page's: the farthest
    any ink pixel lies from paper, so every patch of ink is bridged."""
    values = gray.astype(np.float64)
    paper = values > threshold_otsu(values)
    if paper.all() or not paper.any():
        return values
    level = float(np.median(values[paper]))
    return values / np.maximum(paper_background(values, paper), 1.0) * level


def paper_background(values: np.ndarray, paper: np.ndarray) -> np.ndarray:
    """The paper's brightness everywhere: measured where paper is True,
    interpolated elsewhere (normalized convolution), at the scale of the
    farthest any other pixel lies from paper. values is H×W, or H×W×C with
    each channel interpolated alike."""
    reach = float(ndimage.distance_transform_edt(~paper).max())
    step = max(1, int(reach // BACKGROUND_SAMPLES_PER_SCALE))
    small_paper = paper[::step, ::step]
    sigma = max(reach / step, 1.0)
    weight = np.maximum(
        ndimage.gaussian_filter(small_paper.astype(np.float64), sigma),
        np.finfo(np.float64).tiny,
    )
    channels = values[..., np.newaxis] if values.ndim == 2 else values
    background = np.empty(channels.shape, dtype=np.float64)
    for channel in range(channels.shape[2]):
        small = channels[::step, ::step, channel]
        total = ndimage.gaussian_filter(np.where(small_paper, small, 0.0), sigma)
        background[..., channel] = np.asarray(
            Image.fromarray((total / weight).astype(np.float32), "F").resize(
                (values.shape[1], values.shape[0]), Image.Resampling.BILINEAR
            ),
            dtype=np.float64,
        )
    return background[..., 0] if values.ndim == 2 else background


def upscale(values: np.ndarray, factor: float) -> np.ndarray:
    """values resampled bicubically to factor times the pixels each way, as
    float32."""
    height, width = values.shape
    image = Image.fromarray(values.astype(np.float32), "F")
    size = (round(width * factor), round(height * factor))
    return np.asarray(image.resize(size, Image.Resampling.BICUBIC))


def binarized(image: Image.Image) -> np.ndarray:
    """image's ink by Otsu's threshold, for Tesseract's detections."""
    gray = np.asarray(image.convert("L"))
    return gray <= threshold_otsu(gray)


def osd(ink: np.ndarray, dpi: int) -> str | None:
    """Tesseract's orientation and script report on ink; None when it has
    too little text to tell."""
    try:
        return tesseract(ink, dpi, "--psm", TESSERACT_ORIENTATION)
    except subprocess.CalledProcessError as e:
        if TESSERACT_TOO_LITTLE_TEXT in (e.stderr or ""):
            return None
        raise


def read_text(ink: np.ndarray, dpi: int, language: str) -> str:
    """The text Tesseract reads in ink."""
    return tesseract(ink, dpi, "-l", language, "--psm", TESSERACT_LAYOUT)


def readable_words(ink: np.ndarray, dpi: int, language: str) -> float:
    """Tesseract's expected number of correctly read words: the sum of its
    word confidences. Rewards both reading more words and reading them
    surely."""
    tsv = tesseract(ink, dpi, "-l", language, "--psm", TESSERACT_LAYOUT, "tsv")
    words = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    return sum(
        float(w[TSV_CONFIDENCE]) / CONFIDENCE_SCALE
        for w in words
        if (w.get(TSV_TEXT) or "").strip() and float(w[TSV_CONFIDENCE]) > 0
    )


def text_tilt(image: Image.Image) -> float | None:
    """How far the lines of text on image are turned, in radians
    counterclockwise, as Leptonica measures them; None where Leptonica's own
    deskew would leave the page as it is: too unsure (a page without lines of
    text), or already level."""
    skew = leptonica.find_skew(binarized(image))
    if skew.confidence < SKEW_CONFIDENCE or abs(skew.degrees) < SKEW_LEAST_DEGREES:
        return None
    return float(np.radians(skew.degrees))


def tesseract(ink: np.ndarray, dpi: int, *args: str) -> str:
    """Run Tesseract on an ink mask (True = ink) scanned at dpi; return its
    output."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "page.png")
        Image.fromarray(~ink).convert("1").save(path, dpi=(dpi, dpi))
        result = subprocess.run(
            [TESSERACT, path, "-", *args],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},  # callers run several at once
        )
    return result.stdout
