"""Where the programs open-scansnap runs are: in this checkout, or inside
the macOS app that scripts/build-macos.sh makes.

The app carries SANE (sane-prefix/, as built from sane-backends/) and
Tesseract (tesseract/, its libraries with the app's others) beside the
Python code, in the same layout.
"""

import ctypes.util
import os
import sys
from pathlib import Path

# PyInstaller unpacks the app's files here; sys._MEIPASS is its documented name
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent.parent
SANE_PREFIX = ROOT / "sane-prefix"
SCANIMAGE = SANE_PREFIX / "bin" / "scanimage"
LIBSANE = (
    SANE_PREFIX
    / "lib"
    / ("libsane.1.dylib" if sys.platform == "darwin" else "libsane.so.1")
)
# SANE looks for backends in its build's library directory and in the
# directories listed in LD_LIBRARY_PATH (sane-dll(5)); the app's copy of the
# fujitsu backend is not where the build put it.
SANE_BACKENDS = SANE_PREFIX / "lib" / "sane"
TESSERACT = ROOT / "tesseract"
# Leptonica, which Tesseract links: the app carries it beside its other
# libraries; a checkout finds it through the system's library search
# (ctypes.util.find_library)
LEPTONICA = "leptonica"
# its file name in the app, as Tesseract links it (major version 6); a
# Leptonica that renames it fails the build's self-test
LEPTONICA_IN_APP = ROOT / "libleptonica.6.dylib"


def leptonica() -> Path | None:
    """Where Leptonica's library is; None when it is not installed."""
    if FROZEN:
        return LEPTONICA_IN_APP if LEPTONICA_IN_APP.exists() else None
    found = ctypes.util.find_library(LEPTONICA)
    return Path(found) if found else None


def sane_environment(config_dir: Path) -> dict[str, str]:
    """What SANE (libsane or scanimage) needs in its environment to use the
    scanners configured in config_dir."""
    return {"SANE_CONFIG_DIR": str(config_dir), "LD_LIBRARY_PATH": str(SANE_BACKENDS)}


def use_bundled_tools() -> None:
    """Inside the app, run its own Tesseract (by pages and OCRmyPDF) with
    its own language data."""
    if FROZEN:
        os.environ["PATH"] = (
            f"{TESSERACT / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
        )
        os.environ["TESSDATA_PREFIX"] = str(TESSERACT / "share" / "tessdata")
