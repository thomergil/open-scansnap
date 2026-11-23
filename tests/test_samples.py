"""Regression test on real scans: each folder of samples/ (not in git: the
owner's own documents) holds a sheet's raw sides as the scanner sent them
(page0001.tif, ...) and expected.toml, what became of each side when the
result was checked by eye, and words a side must read ("reads"), added by
hand; every stored page's text must also be level. Run python tests/test_samples.py --record NAME
to write expected.toml for a sample from the current pipeline."""

import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import tomli_w
from skimage.filters import threshold_otsu

from openscansnap import config, job, pages, pdf

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EXPECTED = "expected.toml"
# words read may vary a little with Tesseract's version; a lost word is a
# regression (show-through removal once took 15% of a letter's words)
WORDS_KEPT = 0.95
# a stored page's text lines are level to within this, as Leptonica measures
# them: the half degree a reader sees (an off-white letter came out 0.9
# degrees off, a sheet running off the scan 1.1, 2026-11-23)
LEVEL_DEGREES = 0.5


def outcome(sample: Path) -> list[dict]:
    """What becomes of each side of sample: its mode and words read, or
    blank."""
    sides = sorted(sample.glob("page*.tif"))
    saved = {}

    def save_page(page, pages_dir, number):
        saved[number] = page
        return pages_dir / f"{number}.png"

    with (
        tempfile.TemporaryDirectory() as tmp,
        mock.patch.object(pdf, "save_page", side_effect=save_page),
    ):
        job.assemble(sides, Path(tmp), config.default_options())
    result = []
    for number in range(1, len(sides) + 1):
        page = saved.get(number)
        if page is None:
            result.append({"mode": "blank"})
        else:
            result.append(
                {
                    "mode": str(page.mode),
                    "words": words(page),
                    "tilt": tilt(page),
                    "text": text(page),
                }
            )
    return result


def tilt(page: pages.Page) -> float:
    """How far the stored page's text lines are turned, in degrees; 0 where
    pages.text_tilt finds none, or none worth turning."""
    found = pages.text_tilt(page.image)
    return 0.0 if found is None else float(np.degrees(found))


def ink(page: pages.Page) -> np.ndarray:
    """The ink of a stored page, whatever its mode."""
    gray = np.asarray(page.image.convert("L"))
    return gray <= threshold_otsu(gray) if gray.min() < gray.max() else gray < 0


def words(page: pages.Page) -> int:
    """The words Tesseract reads on a stored page."""
    return round(pages.readable_words(ink(page), page.dpi, "eng"))


def text(page: pages.Page) -> str:
    """The text Tesseract reads on a stored page."""
    return pages.read_text(ink(page), page.dpi, "eng")


def samples() -> list[Path]:
    if not SAMPLES.is_dir():
        return []
    return sorted(p for p in SAMPLES.iterdir() if (p / EXPECTED).is_file())


@unittest.skipUnless(samples(), "no samples/ with expected.toml")
@unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
class SamplesTest(unittest.TestCase):
    def test_each_sample_comes_out_as_checked(self):
        for sample in samples():
            expected = tomllib.loads((sample / EXPECTED).read_text())["side"]
            with self.subTest(sample=sample.name):
                got = outcome(sample)
                self.assertEqual(
                    [side["mode"] for side in got], [side["mode"] for side in expected]
                )
                for number, (now, then) in enumerate(zip(got, expected), 1):
                    if "tilt" in now:
                        self.assertLessEqual(
                            abs(now["tilt"]),
                            LEVEL_DEGREES,
                            f"side {number}: its text is not level",
                        )
                    for word in then.get("reads", []):  # letters lost show here
                        self.assertIn(word, now["text"], f"side {number}")
                    if "words" in then:
                        self.assertGreaterEqual(
                            now["words"],
                            WORDS_KEPT * then["words"],
                            f"side {number}: fewer words read",
                        )


if __name__ == "__main__":
    if sys.argv[1:2] == ["--record"]:
        for name in sys.argv[2:]:
            result = [  # level is required of every page, not recorded
                {k: v for k, v in side.items() if k not in ("tilt", "text")}
                for side in outcome(SAMPLES / name)
            ]
            (SAMPLES / name / EXPECTED).write_text(
                "# what became of each side, checked by eye\n"
                + tomli_w.dumps({"side": result})
            )
            print(name, result)
    else:
        unittest.main()
