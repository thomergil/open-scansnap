"""Writing a file whole, so a reader sees the old file or the new one and
never part of either, even after a crash or a power loss; and private
folders, the owner's only."""

import contextlib
import ctypes
import fcntl
import glob
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

PRIVATE_DIR, PRIVATE_FILE = 0o700, 0o600  # the owner's only
PERMISSIONS = 0o777  # read, write and run for all three: no setuid and the like
# copyfile(3): the ACL and the extended attributes (Finder tags, comments)
COPYFILE_ACL, COPYFILE_XATTR = 1 << 0, 1 << 2
LIBSYSTEM = "/usr/lib/libSystem.B.dylib"


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


def replace_with(path: Path, write: Callable[[Path], None]) -> None:
    """Replace the file path with what write puts in the path it is given (a
    new file beside path), keeping path's permissions and, on macOS, its
    ACL and extended attributes (Finder tags, comments) where the volume
    takes them. A crash can leave the new file beside path (leftovers)."""
    old = os.stat(path)
    fd, name = tempfile.mkstemp(
        dir=path.parent, prefix=_leftover_prefix(path), suffix=path.suffix
    )
    os.close(fd)
    temporary = Path(name)
    try:
        write(temporary)
        os.chmod(temporary, stat.S_IMODE(old.st_mode) & PERMISSIONS)
        _copy_metadata(path, temporary)
        fd = os.open(temporary, os.O_RDONLY)
        try:
            _sync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):  # replaced already; some volumes cannot sync
        _sync_folder(path.parent)


def leftovers(path: Path) -> list[Path]:
    """The new files replace_with left beside path when it was interrupted."""
    pattern = f"{glob.escape(_leftover_prefix(path))}*{glob.escape(path.suffix)}"
    return [
        found
        for found in path.parent.glob(pattern)
        if found.is_file() and not found.is_symlink()
    ]


def _leftover_prefix(path: Path) -> str:
    return f".{path.stem}-"


def private_dir(path: Path) -> Path:
    """path as a folder only its owner can use: made if missing (with its
    parents), else tightened to PRIVATE_DIR. Raises PermissionError, before
    anything is made, when path or its parent is a link or another user's:
    what this process then writes or deletes there could land elsewhere."""
    for folder in (path.parent, path):
        _refuse_unsafe(folder)
    path.mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=True)
    for folder in (path.parent, path):
        _refuse_unsafe(folder)
    # a link put in its place since the check is changed, not followed
    os.chmod(path, PRIVATE_DIR, follow_symlinks=False)
    return path


def _refuse_unsafe(folder: Path) -> None:
    """Raise PermissionError when folder is a link or another user's; a
    folder not made yet is neither."""
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(f"{folder} is a link or another user's")


def _copy_metadata(source: Path, target: Path) -> None:
    """source's ACL and extended attributes on target, where the volume
    takes them (copyfile(3)); kept as far as it goes, never a failure."""
    if sys.platform != "darwin":
        return
    copyfile = ctypes.CDLL(LIBSYSTEM, use_errno=True).copyfile
    copyfile.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    copyfile(bytes(source), bytes(target), None, COPYFILE_ACL | COPYFILE_XATTR)


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
