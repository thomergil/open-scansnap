"""Writing a file whole, so a reader sees the old file or the new one and
never part of either, even after a crash or a power loss."""

import fcntl
import os
import tempfile
from pathlib import Path


def replace_file(path: Path, data: bytes, mode: int) -> None:
    """Replace path with data, with permissions mode (also when path is a
    link: the link is replaced, not followed)."""
    temporary = _written(path, data, mode)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _sync_folder(path.parent)


def _written(path: Path, data: bytes, mode: int) -> Path:
    """A new file beside path holding data, on the disk."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(fd, mode)
            f.write(data)
            f.flush()
            _sync(fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _sync_folder(folder: Path) -> None:
    """Put folder's entries (a rename, a new link) on the disk."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        _sync(fd)
    finally:
        os.close(fd)


def _sync(fd: int) -> None:
    """Put fd's data on the disk itself: on macOS fsync only hands it to the
    drive, which may hold it in its cache (fsync(2)); F_FULLFSYNC flushes
    that too. Some file systems do not support it."""
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except OSError:
        os.fsync(fd)
