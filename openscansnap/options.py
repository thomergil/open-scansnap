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
    PHOTO = "photo"  # color; remove_blank, remove_show_through, clean_up off
    # the scanner's image, uncropped and unturned, at the chosen quality
    AS_SCANNED = "as-scanned"


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
    remove_show_through: bool
    clean_up: bool  # creases, shadows, marks at the paper edge
    searchable: bool  # a text layer, by OCR
    language: str  # Tesseract's names, e.g. eng+nld

    def __post_init__(self):
        """Raises ValueError for a choice no scan can have."""
        for name, kind in (("mode", ScanMode), ("quality", Quality)):
            value = getattr(self, name)
            if value not in set(kind):
                choices = ", ".join(member.value for member in kind)
                raise ValueError(f"{name} must be one of {choices}, not {value!r}")
            object.__setattr__(self, name, kind(value))
        if not all(LANGUAGE.fullmatch(name) for name in self.languages):
            raise ValueError(
                "language must be Tesseract language names joined by "
                f"{LANGUAGE_SEPARATOR!r}, e.g. eng{LANGUAGE_SEPARATOR}nld, not {self.language!r}"
            )

    def in_effect(self) -> "Options":
        """These options with the steps the mode rules out switched off:
        a photo is not a document, and As Scanned changes nothing."""
        if self.mode == ScanMode.PHOTO:
            return replace(
                self, remove_blank=False, remove_show_through=False, clean_up=False
            )
        if self.mode == ScanMode.AS_SCANNED:
            return replace(
                self,
                straighten=False,
                upright=False,
                remove_blank=False,
                remove_show_through=False,
                clean_up=False,
            )
        return self

    @property
    def languages(self) -> list[str]:
        return self.language.split(LANGUAGE_SEPARATOR)

    @property
    def page_dpi(self) -> int:
        return page_dpi(self.quality)

    @property
    def lineart_dpi(self) -> int:
        return lineart_dpi(self.quality)
