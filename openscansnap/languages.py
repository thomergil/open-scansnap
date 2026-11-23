"""The language of a page's text, for OCR, when the scan's language is
Automatic.

Tesseract's orientation and script detection (its "osd" data, which also
turns pages upright) names the page's script. A script that one installed
language is written in settles it (Greek, Thai); for a script of several
(Latin, Cyrillic, Arabic, Han), the page is read and py3langid (a language
identifier, BSD licensed) names the language among them.
"""

import functools
import logging
import re

from py3langid import langid

from . import pages
from .options import LANGUAGE_SEPARATOR

log = logging.getLogger(__name__)

FALLBACK = "eng"  # when nothing can be told: a page with little text
LATIN = "Latin"
OSD_SCRIPT = re.compile(r"^Script: (\w+)$", re.MULTILINE)
# The bundled languages (packaging/macos/languages.txt and English) by script,
# as Tesseract names the script
LATIN_LANGUAGES = (
    "eng", "spa", "fra", "deu", "ita", "por", "nld", "swe", "dan", "nor", "fin",
    "pol", "ces", "slk", "hun", "ron", "hrv", "slv", "tur", "vie", "ind", "msa",
)  # fmt: skip
SCRIPT_LANGUAGES = {
    LATIN: LATIN_LANGUAGES,
    "Cyrillic": ("rus", "ukr", "bul"),
    "Greek": ("ell",),
    "Arabic": ("ara", "fas"),
    "Hebrew": ("heb",),
    "Han": ("chi_sim", "chi_tra", "jpn"),  # Japanese written mostly in kanji too
    "Japanese": ("jpn",),
    "Hiragana": ("jpn",),
    "Katakana": ("jpn",),
    "Hangul": ("kor",),
    "Korean": ("kor",),
    "Thai": ("tha",),
    "Devanagari": ("hin",),
    "Bengali": ("ben",),
}
# py3langid's codes (ISO 639-1) for Tesseract's; it does not tell simplified
# from traditional Chinese, so both are read
ISO = {
    "eng": "en", "spa": "es", "fra": "fr", "deu": "de", "ita": "it", "por": "pt",
    "nld": "nl", "swe": "sv", "dan": "da", "nor": "no", "fin": "fi", "pol": "pl",
    "ces": "cs", "slk": "sk", "hun": "hu", "ron": "ro", "hrv": "hr", "slv": "sl",
    "tur": "tr", "vie": "vi", "ind": "id", "msa": "ms", "rus": "ru", "ukr": "uk",
    "bul": "bg", "ell": "el", "ara": "ar", "fas": "fa", "heb": "he", "chi_sim": "zh",
    "chi_tra": "zh", "jpn": "ja", "kor": "ko", "tha": "th", "hin": "hi", "ben": "bn",
}  # fmt: skip
# Languages py3langid does not tell apart reliably, read together: standard
# Indonesian and Malay share most of their words (measured, 2026-11-23)
READ_TOGETHER = ({"id", "ms"},)
LEAST_TEXT = 20  # characters py3langid needs to tell a language


def detect(side: pages.Detection, installed: set[str]) -> str:
    """The Tesseract language(s), joined by LANGUAGE_SEPARATOR, of the side's
    text (any way up: it is read turned as Tesseract's orientation report
    proposes), among installed. A script with one installed language
    settles it; otherwise the side is read (Latin script: in English, enough
    to tell; others: in their languages) and py3langid names the language.
    With too little text to tell: FALLBACK for Latin script, all of the
    script's installed languages for another."""
    script = page_script(side.report) or LATIN  # little text: most likely Latin
    candidates = [c for c in SCRIPT_LANGUAGES.get(script, ()) if c in installed]
    if len(candidates) <= 1:
        return candidates[0] if candidates else FALLBACK
    reading = FALLBACK if script == LATIN else LANGUAGE_SEPARATOR.join(candidates)
    upright = pages.turned_ink(side.ink, pages.proposed_turn(side.report))
    text = pages.read_text(upright, side.dpi, reading)
    if len(text.strip()) < LEAST_TEXT:
        return FALLBACK if script == LATIN else reading
    identifier = language_identifier()
    identifier.set_languages(sorted({ISO[code] for code in candidates}))
    iso, _ = identifier.classify(text)
    alike = next((group for group in READ_TOGETHER if iso in group), {iso})
    found = LANGUAGE_SEPARATOR.join(code for code in candidates if ISO[code] in alike)
    log.debug("language: %s (%s script)", found, script)
    return found


@functools.cache
def language_identifier() -> langid.LanguageIdentifier:
    """py3langid's identifier, its model loaded once (a quarter second);
    this module's own, as set_languages changes it."""
    return langid.LanguageIdentifier.from_model_file(langid.MODEL_FILE)


def page_script(report: str | None) -> str | None:
    """Tesseract's name for the script in its orientation and script report;
    None when the side has too little text to tell."""
    found = OSD_SCRIPT.search(report or "")
    return found.group(1) if found else None
