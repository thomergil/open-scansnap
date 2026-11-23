"""Check that this installation can turn a scanned page into a searchable
PDF: page decisions, Tesseract, Leptonica and OCRmyPDF, with no scanner. The macOS build
runs it inside the finished app (--self-test), where a missing piece of the
bundle shows up only at run time.
"""

import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pypdfium2
from PIL import Image, ImageDraw, ImageFont

from . import bundle, config, languages, pages, pdf

LANGUAGE = "eng"  # bundled; the self-test page is English

DPI = 300
LETTER = (2550, 3300)  # 8.5 x 11 inches at DPI
TEXT = "The quick brown fox jumps over the lazy dog"
FONT_PIXELS = 60
TURN_DEGREES, TURN_TOLERANCE = 1.0, 0.1


def run() -> None:
    """Raise if a letter-size page of English text is not detected as
    English, or does not come out as a black and white PDF page whose text
    layer holds the text."""
    bundle.use_bundled_tools()
    image = Image.new("RGB", LETTER, (250, 250, 250))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=FONT_PIXELS)
    for line in range(3):
        draw.text((300, 400 + line * 2 * FONT_PIXELS), TEXT, fill=(0, 0, 0), font=font)
    # the language model and data, as a scan in Automatic uses them
    detected = languages.detect(
        pages.Detection(image, DPI), set(pages.installed_languages())
    )
    if detected != LANGUAGE:
        raise RuntimeError(f"English page detected as {detected!r}")
    # Leptonica, loaded from the app, measures how far text is turned
    turned = image.rotate(TURN_DEGREES, fillcolor=(250, 250, 250))
    tilt = pages.text_tilt(turned)
    if tilt is None or abs(math.degrees(tilt) - TURN_DEGREES) > TURN_TOLERANCE:
        raise RuntimeError(f"text turned {TURN_DEGREES} degrees measured as {tilt}")
    options = replace(config.default_options(), language=LANGUAGE)
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
