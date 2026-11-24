"""The scans kept for review: the latest scan, so its pages can be stored
another way and its PDF made again (reviewwindow.py).

Each scan has a folder under the store's root: the scanner's sides as
lossless PNG (remaking from them repeats Automatic's decisions exactly), its
pages as the PDF stores them, a thumbnail per side, and RECORD: the PDF, the
scan's settings, and each side's result and the user's choice for it. The
store keeps the latest scan and those held open for review; keep prunes the
rest, and prune at start also clears folders a crash left half written.
Scans are private: the folders are the owner's only.
"""

import contextlib
import errno
import json
import logging
import math
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from itertools import repeat
from pathlib import Path

from PIL import Image

from . import __version__, config, files, job, pages, sane
from .options import Options, ScanMode

log = logging.getLogger(__name__)

RECORD = "scan.json"
SIDES, PAGES, THUMBS = "sides", "pages", "thumbs"
PREVIEWS = "previews"  # pages as a choice would store them, made for the window
SIDE_NAME, THUMB_NAME = "{:04d}.png", "{:04d}.png"
# side number, choice, and the app version that made it (another version may
# store the page differently, so its previews are not reused); the page's
# own suffix follows
PREVIEW_NAME = "{:04d}-{}-{}"
NEW_PREFIX = ".new-"  # being written or removed: not a scan in the store
THUMB_PIXELS = 240  # the longer side of a thumbnail
# Lossless and fast enough: 6.6 MB and 0.4 s for a 300 dpi letter side
# (measured on the iX500, 2026-11-23); zlib's level, 0-9
PNG_COMPRESSION = 6
# The most room one side takes in the store (its lossless side, page and
# thumbnail): keep refuses a scan when the disk has less free than this per side
SIDE_BYTES = 10_000_000
WRITERS = 4  # sides written at once


@dataclass(frozen=True)
class Side:
    number: int  # its place in the scan, from 1
    mode: pages.Mode | None  # how its page is stored; None: left out
    method: str  # why (job.SideResult.method)
    choice: job.Choice | None  # the user's choice; None: the scan's mode


@dataclass(frozen=True)
class Scan:
    """One scan in the store."""

    folder: Path
    pdf: Path
    made: float  # when it was stored (time.time())
    options: Options  # the scan's settings
    sides: tuple[Side, ...]

    @property
    def name(self) -> str:
        return self.folder.name

    def side_image(self, number: int) -> Path:
        """The side as the scanner sent it."""
        return self.folder / SIDES / SIDE_NAME.format(number)

    def thumbnail(self, number: int) -> Path:
        return self.folder / THUMBS / THUMB_NAME.format(number)

    def page(self, number: int) -> Path | None:
        """The side's page as the PDF stores it; None when left out."""
        found = sorted((self.folder / PAGES).glob(f"{number:04d}.*"))
        return found[0] if found else None

    def preview(self, number: int, choice: job.Choice) -> Path | None:
        """The side's page as choice would store it, if made (Store.preview);
        else None. A link is never shown: the store makes none."""
        found = sorted(
            path
            for path in (self.folder / PREVIEWS).glob(
                PREVIEW_NAME.format(number, choice, __version__) + ".*"
            )
            if not path.is_symlink()
        )
        return found[0] if found else None

    def choices(self) -> dict[int, job.Choice]:
        return {side.number: side.choice for side in self.sides if side.choice}


class Store:
    """The review folders under root. One writer at a time (the PDF
    thread: keep and remake), besides preview, which may run alongside a
    remake of the same scan: it writes only its own file in the scan's
    PREVIEWS folder, which a remake leaves alone. hold and release may come
    from any thread.
    hold and prune take one lock, so prune never removes a scan that hold is
    opening for review."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._held: set[str] = set()

    def latest(self) -> Scan | None:
        scans = self.scans()
        return newest(scans)

    def scans(self) -> list[Scan]:
        """Every scan in the store; a folder that cannot be read is skipped."""
        found = []
        for folder in self._folders():
            if folder.name.startswith(NEW_PREFIX):
                continue
            try:
                found.append(read(folder))
            except Exception as e:  # noqa: BLE001 - a damaged record is skipped, not fatal
                log.warning("cannot read the review of %s: %s", folder.name, e)
        return found

    def hold(self, scan: Scan) -> Scan | None:
        """Keep scan in the store while it is reviewed, even after newer
        scans are kept; return scan as stored now, or None when it is no
        longer in the store."""
        with self._lock:
            try:
                held = read(scan.folder)
            except Exception:  # noqa: BLE001 - gone, or no longer readable
                return None
            self._held.add(scan.name)
            return held

    def release(self, scan: Scan) -> None:
        with self._lock:
            self._held.discard(scan.name)

    def keep(
        self, pdf: Path, assembled: job.Assembled, batch: job.Batch, options: Options
    ) -> Scan:
        """Store a scan just made into pdf (job.finish's keep), then prune.
        Raises OSError when the store cannot take it (no room, or its folder
        is a link or another user's)."""
        files.private_dir(self.root)
        free = shutil.disk_usage(self.root).free
        if free < len(batch.sides) * SIDE_BYTES:
            raise OSError(errno.ENOSPC, "not enough free disk space to keep the scan")
        new = Path(tempfile.mkdtemp(prefix=NEW_PREFIX, dir=self.root))
        folder = None
        try:
            for part in (SIDES, PAGES, THUMBS):
                (new / part).mkdir(mode=files.PRIVATE_DIR)
            with ThreadPoolExecutor(WRITERS) as pool:  # map stops at the first failure
                list(pool.map(store_side, repeat(new), batch.sides, assembled.sides))
            scan = Scan(
                new,
                pdf,
                time.time(),
                options,
                tuple(Side(r.number, r.mode, r.method, None) for r in assembled.sides),
            )
            write(scan)
            folder = claim_folder(self.root, pdf.stem)
            new.rename(folder)  # onto the empty folder claimed
        except BaseException:
            shutil.rmtree(new, ignore_errors=True)
            if folder is not None and folder.is_dir():
                with contextlib.suppress(OSError):
                    folder.rmdir()  # still empty: the rename did not happen
            raise
        log.info("kept the scan for review")
        self.prune()
        return replace(scan, folder=folder)

    def prune(self) -> None:
        """Remove every scan but those held and the latest whose PDF may
        still be there, and what a crash left half written; and the files
        files.replace_with left beside any scan's PDF when interrupted. Only
        the store's own folders go, never through a link, and never one
        whose record cannot be read (a later version of the app may read
        it); what cannot be removed is logged and left. The PDFs are looked
        at outside the lock: a slow drive does not hold up hold."""
        scans = self.scans()
        present = {scan.name for scan in scans if pdf_may_exist(scan.pdf)}
        with self._lock:
            kept = [scan for scan in scans if scan.name in present]
            latest = newest(kept)
            keep = self._held | ({latest.name} if latest else set())
            doomed = []
            for folder in self._folders():
                try:
                    if folder.name.startswith(NEW_PREFIX):
                        doomed.append(folder)
                    elif folder.name not in keep and any(
                        scan.folder == folder for scan in scans
                    ):  # out of the store at once, under a name of its own
                        trash = Path(tempfile.mkdtemp(prefix=NEW_PREFIX, dir=self.root))
                        folder.rename(trash / folder.name)
                        doomed.append(trash)
                except OSError as e:
                    log.warning("cannot remove the review of %s: %s", folder.name, e)
        for scan in scans:
            if checked_pdf(scan.pdf) is None:
                continue
            try:
                for leftover in files.leftovers(scan.pdf):
                    leftover.unlink()
            except OSError as e:
                log.warning(
                    "cannot clear what was left beside %s: %s", scan.pdf.name, e
                )
        for trash in doomed:
            shutil.rmtree(
                trash,
                onexc=lambda *_, name=trash.name: log.warning("cannot remove %s", name),
            )

    def remake(
        self,
        scan: Scan,
        choices: dict[int, job.Choice],
        progress: job.Progress | None = None,
    ) -> Path:
        """Make scan's PDF again with choices (a side's number to its
        choice; the others as the scan's mode), replacing the PDF in one
        step; the store then holds the new pages. Raises sane.ScanError,
        the PDF unchanged, when it was moved or deleted, when every page
        would be left out, or when making it fails; and, the PDF updated,
        when its review could not follow (job.PdfSaved)."""
        pdf = checked_pdf(scan.pdf)
        try:
            gone = pdf is None or pdf.is_symlink() or not pdf.is_file()
        except OSError as e:
            raise sane.ScanError(
                f"{scan.pdf.name} cannot be reached: {e.strerror}"
            ) from e
        if gone:
            raise sane.ScanError(
                f"{scan.pdf.name} was moved or deleted from {scan.pdf.parent}"
            )
        sides = [scan.side_image(side.number) for side in scan.sides]
        try:
            staging = Path(tempfile.mkdtemp(prefix=NEW_PREFIX, dir=scan.folder))
        except OSError as e:
            raise sane.ScanError(job.describe(e)) from e
        try:
            try:
                assembled = staged(scan, choices, staging, sides, progress)
                files.replace_with(
                    pdf, lambda path: job.write_pdf(assembled, path, scan.options)
                )
            except sane.ScanError:
                raise
            except Exception as e:
                raise sane.ScanError(job.describe(e)) from e
            commit(scan.folder, staging, pdf)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        log.info("made the PDF again, as chosen in review")
        return pdf

    def preview(self, scan: Scan, number: int, choice: job.Choice) -> Path:
        """Side number's page as choice (a mode, not LEAVE_OUT) would store
        it, made from the scanner's sides of its sheet, as a remake would
        (the sheet's two sides are cropped and cleaned together); made once,
        then kept in the scan's folder. Any thread may ask, for a scan held:
        it writes only its own file there. A scan let go meanwhile, and
        pruned, makes it fail: nothing outside the scan's folder is written."""
        made = scan.preview(number, choice)
        if made is not None:
            return made
        first = (number - 1) // job.SIDES_PER_SHEET * job.SIDES_PER_SHEET
        numbers = list(
            range(first + 1, min(first + job.SIDES_PER_SHEET, len(scan.sides)) + 1)
        )
        choices = {  # by place in the sheet; the other side is not made
            place: choice if n == number else job.LEAVE_OUT
            for place, n in enumerate(numbers, 1)
        }
        sides = [scan.side_image(n) for n in numbers]
        folder = scan.folder / PREVIEWS
        folder.mkdir(mode=files.PRIVATE_DIR, exist_ok=True)
        if folder.is_symlink():  # the store is never worked through a link
            raise sane.ScanError(f"{folder} is a link; not used")
        with tempfile.TemporaryDirectory(prefix=NEW_PREFIX, dir=folder) as work:
            assembled = job.assemble(sides, Path(work), scan.options, None, choices)
            page = assembled.pages[0]  # the one side made, kept as chosen
            target = folder / (
                PREVIEW_NAME.format(number, choice, __version__) + page.suffix
            )
            shutil.move(page, target)
        return target

    def _folders(self) -> list[Path]:
        """The store's folders: none when the store is a link or cannot be
        read, and never a link inside it."""
        try:
            if self.root.is_symlink() or not self.root.is_dir():
                return []
            return [
                folder
                for folder in self.root.iterdir()
                if folder.is_dir() and not folder.is_symlink()
            ]
        except OSError as e:
            log.warning("cannot read the scans kept for review: %s", e)
            return []


def staged(
    scan: Scan,
    choices: dict[int, job.Choice],
    staging: Path,
    sides: list[Path],
    progress: job.Progress | None,
) -> job.Assembled:
    """The remade pages, thumbnails and record, in staging."""
    for part in (PAGES, THUMBS):
        (staging / part).mkdir(mode=files.PRIVATE_DIR)
    assembled = job.assemble(sides, staging / PAGES, scan.options, progress, choices)
    if not assembled.pages:
        raise sane.ScanError("every page would be left out; the PDF stays as it is")
    with ThreadPoolExecutor(WRITERS) as pool:  # map stops at the first failure
        list(
            pool.map(
                lambda result: thumbnail(
                    result.page or scan.side_image(result.number),
                    staging / THUMBS / THUMB_NAME.format(result.number),
                ),
                assembled.sides,
            )
        )
    write(
        replace(
            scan,
            folder=staging,
            sides=tuple(
                Side(r.number, r.mode, r.method, choices.get(r.number))
                for r in assembled.sides
            ),
        )
    )
    return assembled


def commit(folder: Path, staging: Path, pdf: Path) -> None:
    """Put the staged pages, thumbnails and record in folder. When that
    fails, the old ones go back, all of them, and job.PdfSaved is raised:
    pdf is already updated, its review is not."""
    done: list[tuple[Path, Path]] = []  # the renames made, to undo
    try:
        for part in (PAGES, THUMBS):
            for source, target in (
                (folder / part, staging / f"old-{part}"),
                (staging / part, folder / part),
            ):
                source.rename(target)
                done.append((source, target))
        os.replace(staging / RECORD, folder / RECORD)
    except OSError as e:
        for source, target in reversed(done):
            try:
                target.rename(source)
            except OSError:
                log.exception("cannot put %s back", source.name)
        log.exception("the PDF is updated, but not its review")
        raise job.PdfSaved(
            f"Updated {pdf.name}, but its review could not follow ({e.strerror})", pdf
        ) from e


def checked_pdf(pdf: Path) -> Path | None:
    """pdf when it can be a scan's PDF: an absolute path ending in .pdf."""
    return pdf if pdf.is_absolute() and pdf.suffix == job.PDF_SUFFIX else None


def pdf_may_exist(pdf: Path) -> bool:
    """False only when pdf is surely gone: its folder can be read and does
    not hold it. A folder out of reach (a drive not mounted, access denied)
    is no sign that the PDF was deleted."""
    try:
        os.stat(pdf)
        return True
    except FileNotFoundError:
        pass
    except OSError as e:  # out of reach, or a folder in its path is a file
        return e.errno != errno.ENOTDIR
    try:
        os.stat(pdf.parent)
    except OSError:
        return True  # its folder is out of reach: a drive not mounted
    return False


def store_side(folder: Path, side: Path, result: job.SideResult) -> None:
    """The scanner's side (lossless), the side's page and its thumbnail."""
    with Image.open(side) as image:
        image.save(
            folder / SIDES / SIDE_NAME.format(result.number),
            compress_level=PNG_COMPRESSION,
            dpi=image.info.get("dpi"),
        )
    page = None
    if result.page is not None:
        page = folder / PAGES / result.page.name
        shutil.copyfile(result.page, page)
    thumbnail(page or side, folder / THUMBS / THUMB_NAME.format(result.number))


def thumbnail(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        image.draft("RGB", (THUMB_PIXELS, THUMB_PIXELS))  # JPEG: decode small
        small = image.convert("L" if image.mode in ("1", "L") else "RGB")
        small.thumbnail((THUMB_PIXELS, THUMB_PIXELS), Image.Resampling.BOX)
        small.save(target)


def options_of(recorded: dict, written: int, where: str) -> Options:
    """The Options in recorded, a scan's settings in the settings format
    written (at most config.FORMAT), read as the settings file is
    (config.scan_options); where names the record in errors. Raises
    config.ConfigError for a value no scan can have."""
    return config.scan_options(recorded, written, where)


def choice_of(value: str | None) -> job.Choice | None:
    """A recorded choice; raises ValueError for one no side can have."""
    if value is None or value == job.LEAVE_OUT:
        return value
    return ScanMode(value)


def claim_folder(root: Path, stem: str) -> Path:
    """A new empty folder in root named after stem that no other scan has
    taken (as job.claim_path names PDFs)."""
    n = 1
    while True:
        folder = root / (stem if n == 1 else f"{stem}_{n}")
        try:
            folder.mkdir(mode=files.PRIVATE_DIR)
            return folder
        except FileExistsError:
            n += 1


def newest(scans: list[Scan]) -> Scan | None:
    """The scan stored last. One stored with a time still to come was
    stored while the clock ran ahead: it counts as the oldest, so a scan
    just stored is never pruned in its favor."""
    now = time.time()
    return max(
        scans,
        key=lambda scan: scan.made if scan.made <= now else -math.inf,
        default=None,
    )


def write(scan: Scan) -> None:
    record = {
        config.VERSION_KEY: config.FORMAT,  # of the options; none before 0.1.2
        "pdf": str(scan.pdf),
        "made": scan.made,
        "options": asdict(scan.options),
        "sides": [
            {
                "number": side.number,
                "mode": side.mode,
                "method": side.method,
                "choice": side.choice,
            }
            for side in scan.sides
        ],
    }
    files.replace_file(
        scan.folder / RECORD, json.dumps(record, indent=1).encode(), files.PRIVATE_FILE
    )


def read(folder: Path) -> Scan:
    """The scan stored in folder; raises OSError, ValueError (including a
    record a later version wrote), KeyError, TypeError, RecursionError or
    config.ConfigError (a setting no scan can have) when it cannot be
    read."""
    parts = ("", SIDES, PAGES, THUMBS, PREVIEWS, RECORD)
    if any((folder / part).is_symlink() for part in parts):
        raise ValueError(f"{folder.name} holds a link")
    record = json.loads((folder / RECORD).read_text())
    if not isinstance(record, dict):
        raise TypeError(f"{folder.name}: {RECORD} holds no record")
    made = float(record["made"])
    if not math.isfinite(made):
        raise ValueError(f"{folder.name} was stored at no time: {made}")
    written = record.get(config.VERSION_KEY, 1)
    if type(written) is int and written > config.FORMAT:
        # made again here, its PDF would lose that version's settings: left
        # for it (a record that cannot be read is never pruned)
        raise ValueError(f"{folder.name} was recorded by a later version")
    return Scan(
        folder,
        Path(record["pdf"]),
        made,
        options_of(record["options"], written, str(folder / RECORD)),
        tuple(
            Side(
                int(side["number"]),
                pages.Mode(side["mode"]) if side["mode"] else None,
                str(side["method"]),
                choice_of(side["choice"]),
            )
            for side in record["sides"]
        ),
    )
