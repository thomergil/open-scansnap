"""Draw the disk image's background: the app's name, and an arrow from the
app's icon to the Applications folder (python dmg_background.py OUT.tiff; run
by build-macos.sh). Drawn at 1x and 2x and joined into one TIFF, which
Finder shows sharp on Retina screens.

The layout matches dmg_settings.py: a WINDOW-sized window, the icons centered
on APP_SPOT and APPLICATIONS_SPOT.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from openscansnap import APP_NAME, appicon

WINDOW = (640, 350)  # points
APP_SPOT, APPLICATIONS_SPOT = (145, 165), (495, 165)  # icon centers
PAPER = (246, 244, 239)
BORDER = (226, 222, 214)
TITLE = (28, 36, 44)
RULE = (226, 222, 214)
# the app icon's blue: the iX500's front light on Wi-Fi
ARROW = tuple(round(255 * c) for c in appicon.LIGHT_COLOR)
FONT = Path("/System/Library/Fonts/SFNS.ttf")  # San Francisco, macOS's own
TITLE_SIZE = 22
MARGIN = 32
ARROW_HALF = 56  # from the middle between the icons to each end
ARROW_WIDTH = 16
HEAD = 28  # the arrowhead's legs, along each axis


def draw(scale: int) -> Image.Image:
    """The background at scale (1 or 2) times its size in points."""
    width, height = (n * scale for n in WINDOW)
    image = Image.new("RGB", (width, height), PAPER)
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=10 * scale, outline=BORDER, width=scale
    )
    font = ImageFont.truetype(str(FONT), TITLE_SIZE * scale)
    font.set_variation_by_name("Bold")
    pen.text((MARGIN * scale, 36 * scale), APP_NAME, font=font, fill=TITLE, anchor="lm")
    rule = 82 * scale
    pen.line(
        (MARGIN * scale, rule, width - MARGIN * scale, rule), fill=RULE, width=scale
    )
    middle = (APP_SPOT[0] + APPLICATIONS_SPOT[0]) / 2
    y = APP_SPOT[1] * scale
    start, tip = (middle - ARROW_HALF) * scale, (middle + ARROW_HALF) * scale
    stroke = ARROW_WIDTH * scale
    legs = [
        (start, y, tip, y),
        (tip - HEAD * scale, y - HEAD * scale, tip, y),
        (tip - HEAD * scale, y + HEAD * scale, tip, y),
    ]
    for x0, y0, x1, y1 in legs:
        pen.line((x0, y0, x1, y1), fill=ARROW, width=stroke)
        for x, y_end in ((x0, y0), (x1, y1)):  # round ends and joints
            r = stroke / 2
            pen.ellipse((x - r, y_end - r, x + r, y_end + r), fill=ARROW)
    return image


def main(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        one, two = Path(tmp) / "background.png", Path(tmp) / "background@2x.png"
        draw(1).save(one, dpi=(72, 72))
        draw(2).save(two, dpi=(144, 144))
        subprocess.run(
            ["tiffutil", "-cathidpicheck", str(one), str(two), "-out", str(out)],
            check=True,
            capture_output=True,
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
