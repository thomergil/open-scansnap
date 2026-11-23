"""Make every library symlink in the app loadable under its own name; run by
build-macos.sh after PyInstaller and before signing.

    python fix_links.py APP

PyInstaller collects one copy of libraries that share a real file name and
links the other names to it: Homebrew's libjpeg.8.dylib (which SANE loads)
becomes a symlink to the libjpeg.8.3.2.dylib that pikepdf's wheel ships.
Under the hardened runtime, dyld refuses a library whose install name does
not end in the name it was asked for, so each such link becomes a copy with
the link's name as its install name.
"""

import shutil
import subprocess
import sys
from pathlib import Path

LIBRARY_SUFFIXES = {".dylib", ".so"}


def install_name(library: Path) -> str:
    lines = subprocess.run(
        ["otool", "-D", str(library)], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return lines[1].strip() if len(lines) > 1 else ""


def main(app: Path) -> None:
    # code loads from Frameworks; Resources only links there for data lookups
    for link in sorted((app / "Contents" / "Frameworks").rglob("*")):
        if (
            not link.is_symlink()
            or link.suffix not in LIBRARY_SUFFIXES
            or not link.resolve().is_file()
        ):
            continue
        name = install_name(link.resolve())
        if not name or Path(name).name == link.name:
            continue
        target = link.resolve()
        link.unlink()
        shutil.copy2(target, link)
        subprocess.run(
            ["install_name_tool", "-id", f"@rpath/{link.name}", str(link)],
            check=True,
            capture_output=True,
        )
        print(
            f"{link.relative_to(app)}: copy of {target.name}, install name @rpath/{link.name}"
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
