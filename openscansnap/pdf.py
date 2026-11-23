"""Assemble prepared pages into one upright, searchable PDF."""

import tempfile
from pathlib import Path

import img2pdf
import ocrmypdf

from .options import Options
from .pages import Mode, Page

# Color and gray pages are stored as JPEG at this quality.
JPEG_QUALITY = 90
LINEART_COMPRESSION = "group4"  # CCITT, lossless; img2pdf embeds it as is


def save_page(page: Page, directory: Path, number: int) -> Path:
    """Save a page in the form the PDF stores it, with its resolution;
    return the file."""
    dpi = (page.dpi, page.dpi)
    if page.mode == Mode.LINEART:
        path = directory / f"{number:04d}.tif"
        page.image.save(path, compression=LINEART_COMPRESSION, dpi=dpi)
    else:
        path = directory / f"{number:04d}.jpg"
        page.image.save(path, quality=JPEG_QUALITY, dpi=dpi)
    return path


def write(pages: list[Path], path: Path, options: Options) -> None:
    """Write the saved pages to path, in order, each sized by its own
    resolution; when options ask, with OCRmyPDF's text layer."""
    images = img2pdf.convert([str(page) for page in pages])
    if not options.searchable:
        path.write_bytes(images)
        return
    with tempfile.TemporaryDirectory() as tmp:
        assembled = Path(tmp) / "assembled.pdf"
        assembled.write_bytes(images)
        ocrmypdf.ocr(
            assembled,
            path,
            language=options.languages,
            output_type="pdf",
            optimize=1,
            progress_bar=False,
        )
