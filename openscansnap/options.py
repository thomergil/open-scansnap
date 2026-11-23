"""What a scan should produce: the user's choices, frozen for one scan.

The settings file holds them (config.py); the app's menu changes them. A scan
takes an Options when it starts and keeps it until its PDF is written, so a
change in the menu never applies to half a scan.
"""

import re
from dataclasses import dataclass, replace
from enum import StrEnum


class ScanMode(StrEnum):
    """How every page is stored."""

    AUTO = "auto"  # per page: black and white, gray or color, whichever suits it
    BLACK_AND_WHITE = "black-and-white"
    GRAY = "gray"
    COLOR = "color"
    PHOTO = "photo"  # color, without the document steps (MODE_FIXES, level_text)
    # the scanner's image, uncropped and unturned, at the chosen quality
    AS_SCANNED = "as-scanned"


class ShowThrough(StrEnum):
    """The pages from which show-through (the other side's print showing
    through the paper) is removed. A black and white page's threshold
    already drops faint show-through, and removing it there can take letters
    lying on the other side's ink (a pen scribble behind a typed page cost
    an s, a u and i-dots, 2026-11-23)."""

    GRAY_AND_COLOR = "gray-and-color"  # pages stored in gray or color
    EVERY_PAGE = "every-page"
    KEEP = "keep"  # none


class Quality(StrEnum):
    """Stored resolution. The scanner sends 300 dpi, so no quality stores
    color or gray finer than that."""

    DRAFT = "draft"
    STANDARD = "standard"
    HIGH = "high"


# Stored resolution of color and gray pages; black and white is stored at
# twice this, so letter edges stay smooth.
QUALITY_DPI = {Quality.DRAFT: 150, Quality.STANDARD: 200, Quality.HIGH: 300}
LINEART_FACTOR = 2
LANGUAGE_SEPARATOR = "+"  # Tesseract's: eng+nld
LANGUAGE = re.compile(
    r"[A-Za-z0-9_/]+"
)  # one Tesseract name: eng, chi_sim, script/Latin
# The language setting for each side's language detected (languages.py),
# instead of Tesseract names
AUTOMATIC_LANGUAGE = "auto"
# The settings each mode fixes, whatever the user chose: a photo is not a
# document, and As scanned changes nothing
MODE_FIXES = {
    ScanMode.PHOTO: {
        "remove_blank": False,
        "show_through": ShowThrough.KEEP,
        "clean_up": False,
    },
    ScanMode.AS_SCANNED: {
        "straighten": False,
        "upright": False,
        "remove_blank": False,
        "show_through": ShowThrough.KEEP,
        "clean_up": False,
    },
}


def page_dpi(quality: Quality) -> int:
    """Stored resolution of color and gray pages."""
    return QUALITY_DPI[quality]


def lineart_dpi(quality: Quality) -> int:
    """Stored resolution of black-and-white pages."""
    return page_dpi(quality) * LINEART_FACTOR


@dataclass(frozen=True)
class Options:
    """The choices; their defaults are defaults.toml's
    (config.default_options)."""

    mode: ScanMode
    quality: Quality
    straighten: bool  # straighten and crop to the sheet
    upright: bool  # turn pages so their text reads upright
    remove_blank: bool
    show_through: ShowThrough
    clean_up: bool  # creases, shadows, marks at the paper edge
    searchable: bool  # a text layer, by OCR
    language: str  # Tesseract's names, e.g. eng+nld; or AUTOMATIC_LANGUAGE

    def __post_init__(self):
        """Raises ValueError for a choice no scan can have."""
        for name, kind in (
            ("mode", ScanMode),
            ("quality", Quality),
            ("show_through", ShowThrough),
        ):
            value = getattr(self, name)
            if value not in set(kind):
                choices = ", ".join(member.value for member in kind)
                raise ValueError(f"{name} must be one of {choices}, not {value!r}")
            object.__setattr__(self, name, kind(value))
        names = self.languages
        automatic = AUTOMATIC_LANGUAGE in names
        if (automatic and len(names) > 1) or not all(map(LANGUAGE.fullmatch, names)):
            raise ValueError(
                f"language must be {AUTOMATIC_LANGUAGE!r} or Tesseract language "
                f"names joined by {LANGUAGE_SEPARATOR!r}, e.g. "
                f"eng{LANGUAGE_SEPARATOR}nld, not {self.language!r}"
            )

    def in_effect(self) -> "Options":
        """These options with what the mode sets (MODE_FIXES)."""
        return replace(self, **MODE_FIXES.get(self.mode, {}))

    def fixed_by_mode(self, name: str) -> bool:
        """Whether the mode sets the option name, whatever is chosen."""
        return name in MODE_FIXES.get(self.mode, {})

    @property
    def languages(self) -> list[str]:
        return self.language.split(LANGUAGE_SEPARATOR)

    @property
    def automatic_language(self) -> bool:
        """Whether each side's language is detected."""
        return self.language == AUTOMATIC_LANGUAGE

    @property
    def level_text(self) -> bool:
        """Whether a side is turned level by its text: when straightening is
        in effect, and not on a photo, whatever lines are in it."""
        return self.in_effect().straighten and self.mode != ScanMode.PHOTO

    @property
    def page_dpi(self) -> int:
        return page_dpi(self.quality)

    @property
    def lineart_dpi(self) -> int:
        return lineart_dpi(self.quality)
