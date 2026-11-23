"""Leptonica, the image library Tesseract is built on (BSD 2-clause; the app
carries it with Tesseract), through ctypes: its measure of how far a page's
text lines are turned, pixFindSkew. It finds the turn at which the rows of
ink are sharpest (a projection-profile sweep of DefaultSweepRange, 7 degrees
either way, then a binary search), and how sure that is: the sharpest rows'
score over the weakest in the sweep (skew.c, Leptonica 1.87).
"""

import ctypes
import functools
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

from . import bundle, sane


@dataclass(frozen=True)
class Skew:
    degrees: float  # counterclockwise, as the page is turned
    confidence: float  # 0 on a page without lines of ink


@functools.cache
def _library() -> ctypes.CDLL:
    path = bundle.leptonica()
    if path is None:
        raise sane.ScanError(
            "Leptonica not found; install Tesseract (see docs/DEVELOPMENT.md)"
        )
    lib = ctypes.CDLL(str(path))
    lib.pixReadMem.restype = ctypes.c_void_p
    lib.pixReadMem.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    lib.pixFindSkew.restype = ctypes.c_int
    lib.pixFindSkew.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
    ]
    lib.pixDestroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    return lib


def find_skew(ink: np.ndarray) -> Skew:
    """How far the lines of ink (True = ink) are turned; raises
    sane.ScanError when Leptonica cannot measure it."""
    lib = _library()
    if not ink.any():  # Leptonica refuses an empty page
        return Skew(0.0, 0.0)
    png = io.BytesIO()
    Image.fromarray(~ink).convert("1").save(png, "PNG")
    data = png.getvalue()
    pix = ctypes.c_void_p(lib.pixReadMem(data, len(data)))
    if not pix:
        raise sane.ScanError("Leptonica could not read the page")
    try:
        degrees, confidence = ctypes.c_float(), ctypes.c_float()
        if lib.pixFindSkew(pix, ctypes.byref(degrees), ctypes.byref(confidence)):
            raise sane.ScanError("Leptonica could not measure the page's skew")
    finally:
        lib.pixDestroy(ctypes.byref(pix))
    return Skew(degrees.value, confidence.value)
