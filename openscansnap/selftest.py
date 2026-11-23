"""Check that this installation can turn a scanned page into a searchable
PDF: page decisions, Tesseract and OCRmyPDF, with no scanner. The macOS build
runs it inside the finished app (--self-test), where a missing piece of the
bundle shows up only at run time.
"""

import sys
import tempfile
from pathlib import Path

import pypdfium2
from PIL import Image, ImageDraw, ImageFont

from . import bundle, config, pages, pdf

DPI = 300
LETTER = (2550, 3300)  # 8.5 x 11 inches at DPI
TEXT = "The quick brown fox jumps over the lazy dog"
FONT_PIXELS = 60


def run() -> None:
    """Raise if a letter-size page of text does not come out as a black and
    white PDF page whose text layer holds the text."""
    bundle.use_bundled_tools()
    image = Image.new("RGB", LETTER, (250, 250, 250))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=FONT_PIXELS)
    for line in range(3):
        draw.text((300, 400 + line * 2 * FONT_PIXELS), TEXT, fill=(0, 0, 0), font=font)
    options = config.default_options()
    page = pages.prepare(image, DPI, options)
    if page is None or page.mode != pages.Mode.LINEART:
        raise RuntimeError(f"page of text came out as {page and page.mode}")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "selftest.pdf"
        out.touch()
        pdf.write([pdf.save_page(page, Path(tmp), 1)], out, options)
        text = pypdfium2.PdfDocument(out)[0].get_textpage().get_text_range()
    if "quick brown fox" not in text:
        raise RuntimeError(f"text layer does not hold the text: {text[:80]!r}")


def main() -> None:
    try:
        run()
    except Exception as e:  # noqa: BLE001 - any failure fails the build, with its cause
        print(f"self-test failed: {e!r}", file=sys.stderr)
        sys.exit(1)
    print("self-test passed")


if __name__ == "__main__":
    main()
