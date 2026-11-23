"""Check the Finder layout written into a mounted disk image (run by
build-macos.sh with dmgbuild's Python): python check_dmg_layout.py VOLUME APP.

The app and the Applications link must sit where dmg_settings.py puts them,
over the arrow dmg_background.py draws, and the background must be there.
"""

import sys
from pathlib import Path

from ds_store import DSStore

ICONS = {"Applications": (495, 165)}  # and the app at APP_SPOT
APP_SPOT = (145, 165)


def problems(volume: Path, app: str) -> list[str]:
    found = []
    if not (volume / app).is_dir():
        found.append(f"no {app}")
    link = volume / "Applications"
    if not link.is_symlink() or str(link.readlink()) != "/Applications":
        found.append("no Applications link")
    if not list(volume.glob(".background*")):
        found.append("no background")
    positions = {}
    with DSStore.open(str(volume / ".DS_Store"), "r") as store:
        for entry in store:
            if entry.code == b"Iloc":
                positions[entry.filename] = tuple(entry.value)
    for name, spot in {app: APP_SPOT, **ICONS}.items():
        if positions.get(name) != spot:
            found.append(f"{name} at {positions.get(name)}, not {spot}")
    return found


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    found = problems(Path(sys.argv[1]), sys.argv[2])
    for problem in found:
        print(f"disk image layout: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
