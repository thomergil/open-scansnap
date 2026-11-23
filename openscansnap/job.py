"""One scan: every sheet in the feeder into one PDF. The command line and the
menu bar app both run it.

Every failure arrives as a sane.ScanError whose message says what happened
and, once sheets have gone through the scanner, where their sides are.
"""

import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ocrmypdf.exceptions import ExitCodeException
from PIL import Image

from . import languages, pages, pdf, sane, sheet, showthrough
from .options import LANGUAGE_SEPARATOR, Options, ScanMode, ShowThrough

OUTPUT_NAME = "scan_%Y-%m-%d_%H%M%S"
PDF_SUFFIX = ".pdf"
OUTPUT_MODE = 0o644
SIDES_PREFIX = "openscansnap-sides-"
SIDES_PER_SHEET = 2  # sane.SCAN_OPTIONS scans both sides of every sheet
FULL_TURN = 360  # degrees
# SideResult.method of a side not stored: dropped as blank, or left out
# because its choice was LEAVE_OUT
BLANK, LEFT_OUT = "blank", "left out"

log = logging.getLogger(__name__)


# A side's own choice, instead of the scan's mode: a ScanMode, or LEAVE_OUT
# (its result's method is then LEFT_OUT)
LEAVE_OUT = "leave-out"
Choice = ScanMode | str


@dataclass(frozen=True)
class SheetSide:
    """One side of a sheet, cropped to its paper: as scanned, and with the
    other side's show-through removed (cleaned) when the scan removes it.
    Measures and decisions read the cleaned side, where there is one, so
    the other side's print does not read as this side's own."""

    scanned: Image.Image
    cleaned: Image.Image | None = None

    @property
    def deciding(self) -> Image.Image:
        """The image measurements and decisions use: cleaned where there is
        one, else scanned."""
        return self.scanned if self.cleaned is None else self.cleaned

    def turned_level(self, tilt: float, band: int) -> "SheetSide":
        """Both images turned alike (sheet.turned_level), so a page made
        from one matches what was decided on the other."""
        return SheetSide(
            sheet.turned_level(self.scanned, tilt, band),
            None
            if self.cleaned is None
            else sheet.turned_level(self.cleaned, tilt, band),
        )

    def prepared(
        self, dpi: int, options: Options, turn: int | None
    ) -> "pages.Page | None":
        """The side's page (pages.prepare): made and judged from the side as
        scanned when options keep show-through (Photo, As scanned, or Keep),
        else judged on the cleaned side. When show_through is
        gray-and-color, a black and white page takes its ink from the side
        as scanned: its threshold drops faint show-through itself and keeps
        the letters lying on the other side's ink, which removal can
        erase."""
        show_through = options.in_effect().show_through
        if self.cleaned is None or show_through == ShowThrough.KEEP:
            return pages.prepare(self.scanned, dpi, options, turn)
        if show_through == ShowThrough.EVERY_PAGE:
            return pages.prepare(self.cleaned, dpi, options, turn)
        return pages.prepare(self.cleaned, dpi, options, turn, print_from=self.scanned)


@dataclass(frozen=True)
class SideResult:
    """What became of one side."""

    number: int  # its place in the scan, from 1
    mode: pages.Mode | None  # how its page is stored; None: left out
    method: str  # why: how it was read, "blank", or "left out"
    page: Path | None  # its page, as the PDF stores it; None: left out


@dataclass(frozen=True)
class Assembled:
    """A batch's pages, ready to become its PDF."""

    pages: list[Path]  # in order
    languages: str  # every language read, joined by LANGUAGE_SEPARATOR
    sides: list[SideResult]  # one per side, in order


class Cancelled(sane.ScanError):
    """The user stopped the scan (Ctrl-C) after sheets went through."""


class PdfSaved(sane.ScanError):
    """The PDF at path is saved, but what was to follow it failed."""

    def __init__(self, message: str, path: Path):
        super().__init__(message)
        self.path = path


@dataclass(frozen=True)
class Batch:
    """The sides of one scan, waiting to become a PDF. They live in sides_dir,
    outside any automatic cleanup: the sheets have left the feeder, so the
    sides survive any failure until they are in the PDF."""

    stem: str  # the PDF's name, from when the scan started
    sides_dir: Path
    sides: list[Path]
    stopped: sane.ScanStopped | None  # scanning failed after these sides


def run(
    out_dir: Path, options: Options, device_name: str, keep_raw: Path | None = None
) -> Path:
    """Scan every sheet in the feeder of the scanner device_name (SANE's
    name) into a new PDF in out_dir, as options say; return the PDF. With keep_raw, the raw sides are also copied to a
    folder named after the PDF there, before anything else happens to
    them."""
    return finish(scan(options, device_name, keep_raw), out_dir, options)


def scan(options: Options, device_name: str, keep_raw: Path | None = None) -> Batch:
    """The scanner's part: every sheet in the feeder, as raw sides. Checks
    first that the PDF can be made with options' language, before any paper
    moves."""
    # Automatic falls back to languages.FALLBACK where it cannot tell
    wanted = [languages.FALLBACK] if options.automatic_language else options.languages
    missing = pages.missing_languages(wanted)
    if missing:
        raise sane.ScanError(
            f"Tesseract language data missing: {', '.join(missing)}; "
            "install it with: brew install tesseract tesseract-lang"
        )
    stem = time.strftime(OUTPUT_NAME)
    sides_dir = Path(tempfile.mkdtemp(prefix=SIDES_PREFIX))
    stopped = None
    try:
        sides = sane.scan(sides_dir, device_name)
    except sane.ScanStopped as e:
        sides, stopped = e.pages, e
    except BaseException:
        shutil.rmtree(sides_dir, ignore_errors=True)  # nothing was scanned
        raise
    if keep_raw is not None and sides:
        raw = keep_raw / stem
        raw.mkdir(parents=True, exist_ok=True)
        for side in sides:
            shutil.copy2(side, raw / side.name)
        log.info("raw sides kept in %s", raw)
    return Batch(stem, sides_dir, sides, stopped)


# Told, as each sheet is begun, its number (from 1) and the batch's sheets
Progress = Callable[[int, int], None]


def finish(
    batch: Batch,
    out_dir: Path,
    options: Options,
    progress: Progress | None = None,
    keep: Callable[[Path, Assembled, Batch, Options], None] | None = None,
) -> Path:
    """The rest, which needs no scanner: the batch's PDF in out_dir. keep, if
    given, is handed the PDF, its pages, the batch and options once the PDF
    is written, while the sides and pages still exist (review.Store.keep); a
    failure there is logged, the PDF stands. A batch whose scanning stopped
    partway raises PdfSaved: its PDF holds the sides scanned before."""
    stem, sides_dir, stopped = batch.stem, batch.sides_dir, batch.stopped
    path = None
    try:
        with tempfile.TemporaryDirectory() as pages_dir:
            assembled = assemble(batch.sides, Path(pages_dir), options, progress)
            if assembled.pages:
                out_dir.mkdir(parents=True, exist_ok=True)
                path = claim_path(out_dir, stem)
                try:
                    write_pdf(assembled, path, options)
                except BaseException:
                    path.unlink()
                    raise
                if keep is not None:
                    try:
                        keep(path, assembled, batch, options)
                    except Exception:
                        log.exception("cannot keep the scan for review")
    except KeyboardInterrupt:
        kept = keep_sides(sides_dir, out_dir, stem)
        raise Cancelled(f"Cancelled; the scanned sides are in {kept}") from None
    except Exception as e:
        kept = keep_sides(sides_dir, out_dir, stem)
        reason = describe(e)
        if stopped:
            reason = f"{stopped}; then {reason}"
        raise sane.ScanError(f"{reason}. The scanned sides are in {kept}") from e
    shutil.rmtree(sides_dir, ignore_errors=True)
    if path is None:
        raise stopped or sane.ScanError("Every side was blank; nothing saved")
    if stopped:
        raise PdfSaved(
            f"{stopped}; saved the sides scanned before that in {path}", path
        )
    return path


def write_pdf(assembled: Assembled, path: Path, options: Options) -> None:
    """The PDF of assembled's pages at path, its text read in every language
    the pages were read in."""
    pdf.write(assembled.pages, path, replace(options, language=assembled.languages))


def describe(error: Exception) -> str:
    """A failure while making the PDF, in one line."""
    if isinstance(error, sane.ScanError):
        return str(error)
    if isinstance(error, subprocess.CalledProcessError):
        detail = (error.stderr or "").strip().splitlines()[-1:] or [
            f"exit status {error.returncode}"
        ]
        return f"{Path(error.cmd[0]).name} failed: {detail[0]}"
    if isinstance(error, ExitCodeException):
        return f"OCRmyPDF failed: {error}"
    if isinstance(error, OSError):
        return str(error)
    log.exception("making the PDF failed")
    return f"Making the PDF failed: {error!r}"


def assemble(
    sides: list[Path],
    pages_dir: Path,
    options: Options,
    progress: Progress | None = None,
    choices: Mapping[int, Choice] | None = None,
) -> Assembled:
    """Prepare each side into pages_dir, dropping the blank ones. The sides
    come front, back, front, back (duplex); each sheet's two sides are
    cropped, cleaned of each other's show-through and turned upright
    together, as far as options ask. Each page goes to pages_dir as soon as
    it is ready, so a long batch does not pile up in memory. choices maps a
    side's number to its own choice: stored in that ScanMode (and kept
    though blank), or LEAVE_OUT."""
    choices = choices or {}
    settings, options = options, options.in_effect()  # per side: its own mode's
    saved, results = [], []
    read_in: dict[str, None] = {}  # the languages of the pages saved, in order
    installed = (
        set(pages.installed_languages()) if options.automatic_language else set()
    )
    sheets = math.ceil(len(sides) / SIDES_PER_SHEET)
    for first in range(0, len(sides), SIDES_PER_SHEET):
        if progress is not None:
            progress(first // SIDES_PER_SHEET + 1, sheets)
        sheet_sides = sides[first : first + SIDES_PER_SHEET]
        dpi = resolution(sheet_sides[0])
        images = []
        for side in sheet_sides:
            with Image.open(side) as image:
                image = image.convert("RGB")
                if options.straighten:
                    image = sheet.straighten_and_crop(image, dpi)
                images.append(image)
        # each side cleaned of the other's show-through, when the scan may
        # remove it on some page: both sides as the paper has them
        if len(images) == SIDES_PER_SHEET and options.show_through != ShowThrough.KEEP:
            front, back = images
            both = [
                SheetSide(front, showthrough.clean(front, back, dpi)),
                SheetSide(back, showthrough.clean(back, front, dpi)),
            ]
        else:
            both = [SheetSide(image) for image in images]
        # Turn each side level by its text: paper can feed in at a slant of
        # its own, and the edges, which can run off the scan or be lost
        # against the backing, straightened it only roughly. As scanned
        # keeps every side whole, whatever a side's choice; a side without
        # text keeps the edges' turn.
        for index, number in enumerate(range(first + 1, first + 1 + len(both))):
            if not (
                options.straighten
                and side_options(settings, choices.get(number)).level_text
            ):
                continue
            tilt = text_turn(both[index].deciding)
            if tilt is not None:
                both[index] = both[index].turned_level(tilt, pages.edge_band(dpi))
        detections = [pages.Detection(side.deciding, dpi) for side in both]
        side_languages = [language_of(side, options, installed) for side in detections]
        turns: list[int | None] = [None] * len(both)
        if options.upright and len(both) == SIDES_PER_SHEET:
            turns = sheet_turns(detections, side_languages)
        elif options.upright:  # one side: turned here, from its own detection
            turns = [pages.orientation(detections[0], side_languages[0]) or 0]
        for number, (side, turn, language) in enumerate(
            zip(both, turns, side_languages), first + 1
        ):
            choice = choices.get(number)
            if choice == LEAVE_OUT:
                log.info("side %d: left out, as chosen", number)
                results.append(SideResult(number, None, LEFT_OUT, None))
                continue
            chosen = replace(side_options(settings, choice), language=language)
            result = prepare_side(side, dpi, chosen, pages_dir, number, turn)
            results.append(result)
            if result.page is not None:
                saved.append(result.page)
                read_in.update(dict.fromkeys(language.split(LANGUAGE_SEPARATOR)))
    return Assembled(saved, LANGUAGE_SEPARATOR.join(read_in), results)


def side_options(settings: Options, choice: Choice | None) -> Options:
    """The options a side is stored with: the scan's, or its own choice's
    mode (a side given a mode is kept though blank)."""
    if choice is None or choice == LEAVE_OUT:
        return settings
    return replace(settings, mode=ScanMode(choice), remove_blank=False)


def text_turn(side: Image.Image) -> float | None:
    """The angle side's text is turned by (radians, counterclockwise) when
    it is measurably askew (pages.text_tilt); None when level, without
    text, or not measurable."""
    try:
        tilt = pages.text_tilt(side)
    except sane.ScanError as e:  # without a measure, the side keeps the edges' turn
        log.warning("text tilt not measured: %s", e)
        return None
    if tilt is not None:
        log.debug("text tilt %.2f degrees, turned level", math.degrees(tilt))
    return tilt


def resolution(side: Path) -> int:
    with Image.open(side) as image:
        if "dpi" not in image.info:
            raise sane.ScanError(
                f"{side.name} has no resolution; rebuild SANE (see docs/DEVELOPMENT.md)"
            )
        return round(image.info["dpi"][0])


def language_of(side: pages.Detection, options: Options, installed: set[str]) -> str:
    """The Tesseract language(s) a side is read in: detected among installed
    when Automatic (languages.py), else the chosen ones."""
    if not options.automatic_language:
        return options.language
    if not pages.has_ink(side.image, side.dpi):  # blank: nothing to tell from
        return languages.FALLBACK
    found = languages.detect(side, installed)
    log.debug("detected language %s", found)
    return found


def sheet_turns(sides: list[pages.Detection], side_languages: list[str]) -> list[int]:
    """The turn that makes each side of a sheet upright, each read in its
    language. A side without text turns as the sheet does: its other side's
    turn, which the back sees mirrored (a quarter turn one way shows as a
    quarter turn the other)."""
    front, back = (
        pages.orientation(side, language)
        for side, language in zip(sides, side_languages)
    )
    if front is None and back is not None:
        front = -back % FULL_TURN
    elif back is None and front is not None:
        back = -front % FULL_TURN
    return [front or 0, back or 0]


def prepare_side(
    side: SheetSide,
    dpi: int,
    options: Options,
    pages_dir: Path,
    number: int,
    turn: int | None,
) -> SideResult:
    """The side's page (SheetSide.prepared), saved to pages_dir, or none
    when it is blank."""
    page = side.prepared(dpi, options, turn)
    if page is None:
        log.info("side %d: blank, dropped", number)
        return SideResult(number, None, BLANK, None)
    log.info(
        "side %d: %s (%s, %.0f words read)",
        number,
        page.mode,
        page.method,
        page.readable_words,
    )
    return SideResult(
        number, page.mode, page.method, pdf.save_page(page, pages_dir, number)
    )


def keep_sides(sides_dir: Path, out_dir: Path, stem: str) -> Path:
    """Move the scanned sides into a new folder in out_dir and return it; if
    out_dir cannot take them, leave them where they are and return that."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        kept = Path(tempfile.mkdtemp(prefix=f"{stem}_sides_", dir=out_dir))
    except OSError:
        return sides_dir
    try:
        for side in sorted(sides_dir.iterdir()):
            shutil.move(side, kept / side.name)
    except OSError:
        # some sides may have moved: put them back so they stay together
        for side in kept.iterdir():
            shutil.move(side, sides_dir / side.name)
        kept.rmdir()
        return sides_dir
    sides_dir.rmdir()
    return kept


def claim_path(directory: Path, stem: str) -> Path:
    """Create an empty file named after stem that no other scan has taken,
    and return it."""
    n = 1
    while True:
        suffix = "" if n == 1 else f"_{n}"
        path = directory / f"{stem}{suffix}{PDF_SUFFIX}"
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OUTPUT_MODE))
            return path
        except FileExistsError:
            n += 1
