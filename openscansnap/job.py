"""One scan: every sheet in the feeder into one PDF. The command line and the
menu bar app both run it.

Every failure arrives as a sane.ScanError whose message says what happened
and, once sheets have gone through the scanner, where their sides are.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ocrmypdf.exceptions import ExitCodeException
from PIL import Image

from . import pages, pdf, sane, sheet, showthrough
from .options import Options

OUTPUT_NAME = "scan_%Y-%m-%d_%H%M%S"
PDF_SUFFIX = ".pdf"
OUTPUT_MODE = 0o644
SIDES_PREFIX = "openscansnap-sides-"
SIDES_PER_SHEET = 2  # sane.SCAN_OPTIONS scans both sides of every sheet
FULL_TURN = 360  # degrees

log = logging.getLogger(__name__)


class Cancelled(sane.ScanError):
    """The user stopped the scan (Ctrl-C) after sheets went through."""


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
    missing = pages.missing_languages(options.languages)
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


def finish(batch: Batch, out_dir: Path, options: Options) -> Path:
    """The rest, which needs no scanner: the batch's PDF in out_dir."""
    stem, sides_dir, stopped = batch.stem, batch.sides_dir, batch.stopped
    try:
        with tempfile.TemporaryDirectory() as pages_dir:
            path = assemble(batch.sides, Path(pages_dir), out_dir, stem, options)
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
        raise sane.ScanError(
            f"{stopped}; saved the sides scanned before that in {path}"
        )
    return path


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
    sides: list[Path], pages_dir: Path, out_dir: Path, stem: str, options: Options
) -> Path | None:
    """Prepare each side, drop the blank ones and write the PDF; None when
    every side is blank. The sides come front, back, front, back (duplex);
    each sheet's two sides are cropped, cleaned of each other's show-through
    and turned upright together, as far as options ask. Each page goes to
    pages_dir as soon as it is ready, so a long batch does not pile up in
    memory."""
    options = options.in_effect()
    saved = []
    for first in range(0, len(sides), SIDES_PER_SHEET):
        sheet_sides = sides[first : first + SIDES_PER_SHEET]
        dpi = resolution(sheet_sides[0])
        images = []
        for side in sheet_sides:
            with Image.open(side) as image:
                image = image.convert("RGB")
                if options.straighten:
                    image = sheet.straighten_and_crop(image, dpi)
                images.append(image)
        turns = [None] * len(images)
        if len(images) == SIDES_PER_SHEET:
            front, back = images
            if options.remove_show_through:
                images = [
                    showthrough.clean(front, back, dpi),
                    showthrough.clean(back, front, dpi),
                ]
            if options.upright:
                turns = sheet_turns(images, dpi, options.language)
        for number, (image, turn) in enumerate(zip(images, turns), first + 1):
            saved.extend(prepare_side(image, dpi, options, pages_dir, number, turn))
    if not saved:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path = claim_path(out_dir, stem)
    try:
        pdf.write(saved, path, options)
    except BaseException:
        path.unlink()
        raise
    return path


def resolution(side: Path) -> int:
    with Image.open(side) as image:
        if "dpi" not in image.info:
            raise sane.ScanError(
                f"{side.name} has no resolution; rebuild SANE (see README)"
            )
        return round(image.info["dpi"][0])


def sheet_turns(sides: list[Image.Image], dpi: int, language: str) -> list[int]:
    """The turn that makes each side of a sheet upright. A side without text
    turns as the sheet does: its other side's turn, which the back sees
    mirrored (a quarter turn one way shows as a quarter turn the other)."""
    front, back = (pages.orientation(side, dpi, language) for side in sides)
    if front is None and back is not None:
        front = -back % FULL_TURN
    elif back is None and front is not None:
        back = -front % FULL_TURN
    return [front or 0, back or 0]


def prepare_side(
    image: Image.Image,
    dpi: int,
    options: Options,
    pages_dir: Path,
    number: int,
    turn: int | None,
) -> list[Path]:
    """The side's saved page, or nothing when it is blank."""
    page = pages.prepare(image, dpi, options, turn)
    if page is None:
        log.info("side %d: blank, dropped", number)
        return []
    log.info(
        "side %d: %s (%s, %.0f words read)",
        number,
        page.mode,
        page.method,
        page.readable_words,
    )
    return [pdf.save_page(page, pages_dir, number)]


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
