"""packaging/macos/languages.txt: the language data bundled besides English,
and the tessdata_fast commit it comes from. Lines: SHA-256, two spaces,
file name; "# commit <hash>" names the commit."""

from pathlib import Path

LIST = Path(__file__).with_name("languages.txt")
DATA = ".traineddata"
COMMIT = "# commit "


def data_files() -> list[str]:
    """The listed language data files: ara.traineddata, ..."""
    return [
        line.split()[-1]
        for line in LIST.read_text().splitlines()
        if line.rstrip().endswith(DATA) and not line.startswith("#")
    ]


def commit() -> str:
    return next(
        line.removeprefix(COMMIT).strip()
        for line in LIST.read_text().splitlines()
        if line.startswith(COMMIT)
    )
