"""Collect the licenses of everything the macOS app bundles, and SANE's
source changes, into one folder; run by scripts/build-macos.sh.

    python packaging/macos/licenses.py OUT_DIR [--other NAME=LICENSE_FILE]... BINARY...

BINARY: the native programs and libraries the app carries; the Homebrew
libraries they load are found with otool and each one's license is copied
from its Homebrew keg (or from packaging/macos/licenses/<formula>.txt when
the keg has none). The Python packages are those in requirements.txt. A
component without a license stops the build.
"""

import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDORED = Path(__file__).with_name("licenses")
SANE = ROOT / "sane-backends"
SANE_UPSTREAM = "https://gitlab.com/sane-project/backends"
LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE)", re.IGNORECASE)
CODE_SUFFIXES = {".py", ".pyc", ".so", ".dylib"}  # e.g. pyobjc's copying module


def is_license(path) -> bool:
    return bool(LICENSE_FILE.match(path.name)) and path.suffix not in CODE_SUFFIXES


REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==")
# LGPL Python modules: the app stores them as plain .py files (not in
# PyInstaller's archive), so a user can replace them as the LGPL requires
REPLACEABLE_MODULES = ("img2pdf", "fpdf")  # fpdf is fpdf2's module


class MissingLicense(SystemExit):
    pass


def homebrew_prefix() -> Path:
    return Path(
        subprocess.run(
            ["brew", "--prefix"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )


def linked_libraries(binary: Path) -> list[Path]:
    """The libraries binary loads, as otool lists them."""
    lines = subprocess.run(
        ["otool", "-L", str(binary)], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [Path(line.split(" (")[0].strip()) for line in lines[1:]]


def homebrew_formulae(binaries: list[Path], prefix: Path) -> set[str]:
    """The Homebrew formulae the binaries come from or load libraries from,
    directly or through each other."""
    formulae, seen, todo = set(), set(), [b.resolve() for b in binaries]
    while todo:
        binary = todo.pop()
        if binary in seen:
            continue
        seen.add(binary)
        if binary.is_relative_to(prefix / "Cellar"):
            formulae.add(binary.relative_to(prefix / "Cellar").parts[0])
        for library in linked_libraries(binary):
            real = library.resolve() if library.exists() else library
            if real.is_relative_to(prefix):
                todo.append(real)
    return formulae


def copy_formula_license(formula: str, prefix: Path, out: Path) -> None:
    keg = (prefix / "opt" / formula).resolve()
    files = [f for f in keg.iterdir() if is_license(f)]
    vendored = VENDORED / f"{formula}.txt"
    if not files and vendored.exists():
        files = [vendored]
    if not files:
        raise MissingLicense(f"no license for Homebrew {formula}; add {vendored}")
    target = out / formula
    target.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy(f, target / f.name)


def copy_python_licenses(out: Path) -> list[tuple[str, str]]:
    """Copy the license of each package in requirements.txt; return
    (name, license) pairs for the notice."""
    names = [
        m[1]
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if (m := REQUIREMENT.match(line))
    ]
    listed = []
    for name in names:
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue  # not for this platform
        meta = dist.metadata
        kind = meta.get("License-Expression") or next(
            (
                c.split(" :: ")[-1]
                for c in meta.get_all("Classifier") or []
                if c.startswith("License ::")
            ),
            (meta.get("License") or "").splitlines()[0] if meta.get("License") else "",
        )
        files = [f for f in dist.files or [] if is_license(f)]
        if not files and not kind:
            raise MissingLicense(f"no license for Python package {name}")
        target = out / name
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            dest = target / f.name
            if dest.exists():  # several license files of the same name
                dest = target / f"{f.parent.name}-{f.name}"
            shutil.copy(f.locate(), dest)
        if not files:
            (target / "LICENSE.txt").write_text(f"{name}: {kind}\n")
        listed.append((name, kind or "see files"))
    return listed


def sane_source(out: Path) -> str:
    """SANE's changes as a patch against the upstream commit they are based
    on; return that commit."""
    base = subprocess.run(
        ["git", "-C", str(SANE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tracked = subprocess.run(
        ["git", "-C", str(SANE), "diff", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    added = subprocess.run(
        ["git", "-C", str(SANE), "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    new = "".join(
        # exits 1 because the files differ, as they always do here
        subprocess.run(
            ["git", "-C", str(SANE), "diff", "--no-index", "/dev/null", name],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
        for name in added
    )
    (out / "sane-backends-wifi.patch").write_text(tracked + new)
    shutil.copy(SANE / "LICENSE", out / "LICENSE")
    shutil.copy(SANE / "COPYING", out / "COPYING")
    return base


def main(out: Path, binaries: list[Path], others: dict[str, Path]) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy(ROOT / "LICENSE", out / "open-scansnap-LICENSE.txt")
    prefix = homebrew_prefix()
    formulae = sorted(homebrew_formulae(binaries, prefix))
    for formula in formulae:
        copy_formula_license(formula, prefix, out / "homebrew")
    python = copy_python_licenses(out / "python")
    for name, license_file in others.items():
        (out / name).mkdir()
        shutil.copy(license_file, out / name / license_file.name)
    sane = out / "sane-backends"
    sane.mkdir()
    base = sane_source(sane)
    notice = [
        "# Open ScanSnap: licenses",
        "",
        "Open ScanSnap's own code is under the MIT License (open-scansnap-LICENSE.txt).",
        "The app also carries these programs and libraries, each under its own license;",
        "the license texts are in the folders named below.",
        "",
        "## SANE (sane-backends/)",
        "",
        "scanimage, libsane and the fujitsu backend: GNU GPL version 2 or later, with the",
        "SANE exception for programs that use the SANE libraries (sane-backends/LICENSE).",
        f"Built from {SANE_UPSTREAM} at commit {base}, with the changes",
        "in sane-backends/sane-backends-wifi.patch (the ScanSnap Wi-Fi support).",
        "",
        "## Tesseract and the libraries the native programs load (homebrew/)",
        "",
        *[f"- {formula}" for formula in formulae],
        "",
        *(
            line
            for name in others
            for line in (
                f"## {name} ({name}/)",
                "",
                f"{name} is bundled unchanged; its license is in {name}/.",
                "",
            )
        ),
        "## Python packages (python/)",
        "",
        *[f"- {name}: {kind}" for name, kind in python],
        "",
        f"{', '.join(REPLACEABLE_MODULES)} (LGPL) are stored as plain Python files in the app,",
        "so they can be replaced.",
        "",
    ]
    (out / "NOTICE.md").write_text("\n".join(notice))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("binaries", type=Path, nargs="+")
    parser.add_argument(
        "--other", action="append", default=[], help="NAME=LICENSE_FILE"
    )
    args = parser.parse_args()
    main(
        args.out,
        args.binaries,
        {k: Path(v) for k, v in (o.split("=", 1) for o in args.other)},
    )
