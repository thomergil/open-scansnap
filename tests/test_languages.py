"""Telling a page's language for OCR, when the language is Automatic."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from openscansnap import config, languages, options, pages

# the build's reader of packaging/macos/languages.txt
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging" / "macos"))
import languagelist

INSTALLED = {"eng", "nld", "deu", "fra", "rus", "ukr", "osd"}
PAGE = np.zeros((10, 10), dtype=bool)
DUTCH = "U krijgt deze brief omdat u vanaf nu uw werkelijk rendement mag doorgeven."
ENGLISH = "Your concealed permit expires soon; renew it online with your phone."


def report(script: str) -> str:
    return f"Orientation in degrees: 0\nRotate: 0\nScript: {script}\nScript confidence: 5\n"


class DetectTest(unittest.TestCase):
    def detect(self, osd, text="", installed=INSTALLED):
        """detect on a side whose orientation and script report is osd, and
        that reads as text; and the mock of reading it."""
        side = SimpleNamespace(image=None, ink=PAGE, dpi=300, report=osd)
        with mock.patch.object(pages, "read_text", return_value=text) as read:
            return languages.detect(side, installed), read

    def test_a_script_of_one_installed_language_settles_it(self):
        found, read = self.detect(report("Greek"), installed=INSTALLED | {"ell"})
        self.assertEqual(found, "ell")
        read.assert_not_called()  # no reading needed

    def test_a_script_of_several_is_read_to_tell(self):
        russian = (
            "Все люди рождаются свободными и равными в своем достоинстве и правах."
        )
        found, read = self.detect(report("Cyrillic"), russian)
        self.assertEqual(found, "rus")
        self.assertEqual(read.call_args.args[2], "rus+ukr")  # read in its languages

    def test_a_side_upside_down_is_read_upright(self):
        side = SimpleNamespace(
            image=None,
            ink=np.arange(6).reshape(2, 3) > 2,
            dpi=300,
            report=report("Latin").replace("Rotate: 0", "Rotate: 180"),
        )
        with mock.patch.object(pages, "read_text", return_value=DUTCH) as read:
            languages.detect(side, INSTALLED)
        np.testing.assert_array_equal(read.call_args.args[0], np.rot90(side.ink, 2))

    def test_latin_text_is_identified(self):
        for text, language in ((DUTCH, "nld"), (ENGLISH, "eng")):
            with self.subTest(language=language):
                self.assertEqual(self.detect(report("Latin"), text)[0], language)

    def test_only_installed_languages_are_named(self):
        found, _ = self.detect(report("Latin"), DUTCH, installed={"eng", "deu", "osd"})
        self.assertIn(found, {"eng", "deu"})

    def test_too_little_to_tell(self):
        for osd in (None, report("Latin")):
            with self.subTest(osd=osd):
                self.assertEqual(self.detect(osd, "Aan")[0], languages.FALLBACK)

    def test_text_tells_when_the_script_cannot(self):
        # Tesseract names no script on a page with little text; reading it
        # can still tell the language
        self.assertEqual(self.detect(None, DUTCH)[0], "nld")

    def test_a_script_no_installed_language_is_written_in(self):
        self.assertEqual(self.detect(report("Thai"))[0], languages.FALLBACK)
        self.assertEqual(self.detect(report("Fraktur"))[0], languages.FALLBACK)


class BundledTest(unittest.TestCase):
    def test_every_bundled_language_can_be_detected(self):
        bundled = {
            name.removesuffix(languagelist.DATA) for name in languagelist.data_files()
        } | {"eng"}  # with Tesseract itself
        self.assertEqual(set(languages.ISO), bundled)
        self.assertEqual(set().union(*languages.SCRIPT_LANGUAGES.values()), bundled)


class PrepareTest(unittest.TestCase):
    def test_prepare_needs_the_sides_language(self):
        automatic = replace(
            config.default_options(), language=options.AUTOMATIC_LANGUAGE
        )
        with self.assertRaises(ValueError):
            pages.prepare(None, 300, automatic)


if __name__ == "__main__":
    unittest.main()
