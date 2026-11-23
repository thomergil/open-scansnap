"""Decide how to store each scanned page, from measurements of that page.

Each side arrives already cropped to the sheet and cleaned of the other
side's show-through, as far as the scan's Options ask (job.py). Here it is
turned upright (Tesseract's orientation detection) and stored as its
Options.mode says. In the automatic mode each page gets what suits it:

- dropped when blank: nothing on it is clearly darker than the paper, or it
  has only faint marks that Tesseract reads better mirrored (show-through,
  smudges) or, when they do not read as text, that cover less than
  PICTURE_SHARE of the side (in every mode that removes blank sides);
- color, when any ink on it is colored (a pen stroke, a stamp), judged by
  its CIELAB chroma, or when the page as a whole is colorful (Hasler and
  Süsstrunk's metric, for photos);
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

from . import sheet
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
# Background estimation works on a copy shrunk so that its scale is at
# least this many pixels: the result is smooth on that scale anyway.
BACKGROUND_SAMPLES_PER_SCALE = 4
TESSERACT = "tesseract"
TESSERACT_LAYOUT = "3"  # automatic page segmentation
TESSERACT_ORIENTATION = "0"  # orientation and script detection only
TESSERACT_TOO_LITTLE_TEXT = "Too few characters"  # its reason for no orientation
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
    image: Image.Image, dpi: int, options: Options, turn: int | None = None
) -> Page | None:
    """The stored form of one side, already cropped to the sheet; None when
    it is blank and blank sides are left out. turn is the clockwise turn
    that makes it upright, when already known (from the sheet's other side);
    None finds it here."""
    options = options.in_effect()
    gray = np.asarray(image.convert("L"))
    band = edge_band(dpi)
    contrast, share = ink_contrast(gray[band:-band, band:-band])
    if options.remove_blank and contrast < NO_INK:
        return None

    if not options.upright:
        turn = 0
    elif turn is None:
        turn = orientation(image, dpi, options.language) or 0
    image = turned(image, turn)
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
    gray = np.asarray(image.convert("L"))
    sources = {
        "otsu": gray,
        **({"otsu-flat": flatten(gray)} if options.clean_up else {}),
    }
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
    ):
        return None
    if fixed is not None:
        return stored(image, dpi, options, fixed, method, scores[best])
    if auto and not readable:
        return stored(image, dpi, options, Mode.GRAY, "no readable text", scores[best])

    factor = options.lineart_dpi / dpi
    ink = upscale(sources[best], factor) <= limits[best]
    if options.clean_up:
        clear_edge_marks(ink, edge_band(options.lineart_dpi))
    return Page(
        Image.fromarray(~ink).convert("1"),
        options.lineart_dpi,
        Mode.LINEART,
        best,
        scores[best],
    )


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
    """A color or gray page at the chosen quality's resolution."""
    if options.page_dpi != dpi:
        factor = options.page_dpi / dpi
        image = image.resize(
            (round(image.width * factor), round(image.height * factor)),
            Image.Resampling.LANCZOS,
        )
    return Page(
        image.convert("RGB" if mode == Mode.COLOR else "L"),
        options.page_dpi,
        mode,
        method,
        readable,
    )


def orientation(image: Image.Image, dpi: int, language: str) -> int | None:
    """The clockwise turn (0, 90, 180 or 270 degrees) that makes the page's
    text read upright; None when it has too little text to tell. Tesseract
    proposes a turn, kept only if the page then reads better."""
    gray = np.asarray(image.convert("L"))
    ink = gray <= threshold_otsu(gray)
    try:
        report = tesseract(ink, dpi, "--psm", TESSERACT_ORIENTATION)
    except subprocess.CalledProcessError as e:
        if TESSERACT_TOO_LITTLE_TEXT in (e.stderr or ""):
            return None
        raise
    proposal = OSD_ROTATE.search(report)
    turn = int(proposal.group(1)) if proposal else 0
    if not turn:
        return 0
    proposed = np.rot90(ink, k=-turn // QUARTER_TURN)  # clockwise
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
        if (
            (rows.start == 0 and rows.stop <= band)
            or (rows.stop == height and rows.start >= height - band)
            or (cols.start == 0 and cols.stop <= band)
            or (cols.stop == width and cols.start >= width - band)
        ):
            ink[rows, cols][labels[rows, cols] == number] = False


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
    """Whether any mark of ink at least LEGIBLE_POINTS tall or wide is
    colored. Marks are ink by Otsu's threshold; each one's median chroma is
    measured, so the scanner's slight color fringes on black print do not
    count."""
    step = 2  # half the resolution is plenty to tell a mark's color, and fast
    small = image.convert("RGB").reduce(step)
    gray = np.asarray(small.convert("L"), dtype=np.float64)
    ink = gray <= threshold_otsu(gray)
    labels, count = ndimage.label(ink, structure=EIGHT_NEIGHBORS)
    if not count:
        return False
    chroma = np.hypot(*np.moveaxis(rgb2lab(np.asarray(small))[..., 1:], -1, 0))
    smallest = LEGIBLE_POINTS / POINTS_PER_INCH * dpi / step
    for number, (rows, cols) in enumerate(ndimage.find_objects(labels), 1):
        if max(rows.stop - rows.start, cols.stop - cols.start) < smallest:
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
