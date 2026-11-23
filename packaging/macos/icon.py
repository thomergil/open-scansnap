"""Write the app icon as .icns: python icon.py OUT.icns (run by build-macos.sh).

The drawing is openscansnap.appicon's, which the running app also uses.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from openscansnap import appicon

SIZES = [16, 32, 128, 256, 512]  # each also at @2x (Apple's iconset sizes)


def main(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        master = Path(tmp) / "icon.png"
        appicon.write_png(master)
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size in SIZES:
            for scale, suffix in ((1, ""), (2, "@2x")):
                pixels = str(size * scale)
                target = iconset / f"icon_{size}x{size}{suffix}.png"
                subprocess.run(
                    ["sips", "-z", pixels, pixels, str(master), "--out", str(target)],
                    check=True,
                    capture_output=True,
                )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
