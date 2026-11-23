"""Settings: defaults.toml, overridden key by key by the user's file."""

import logging
import reprlib
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from . import APP_NAME, files, sane
from .options import Options, ShowThrough

log = logging.getLogger(__name__)

DEFAULTS = Path(__file__).with_name("defaults.toml")
PATH = sane.CONFIG_DIR.parent / "config.toml"
LOG_DIR = Path.home() / "Library" / "Logs" / "open-scansnap"
RAW_DIR = LOG_DIR / "raw"  # debug mode keeps each scan's raw sides here
# the latest scan, kept for review (review.py)
REVIEW_DIR = (
    Path.home() / "Library" / "Application Support" / "open-scansnap" / "review"
)
FILE_MODE = 0o644
# The settings file's format: VERSION_KEY in the file says which wrote it;
# a file without it is format 1 (Open ScanSnap 0.1.0 and 0.1.1). Each format
# up to FORMAT has its upgrade from the one before (_UPGRADES).
FORMAT = 2
VERSION_KEY = "version"
_LOCK = threading.Lock()  # load and save read, and may write, the one file


class ConfigError(sane.ScanError):
    """The user's settings file cannot be used; the message says why."""


@dataclass(frozen=True)
class Settings:
    out_dir: Path
    options: Options
    debug: bool
    show_when_paper_loaded: bool
    review_after_scan: bool


@dataclass(frozen=True)
class _Read:
    """A settings file as read: its settings as this version reads them,
    the format it was written in, and whether there is a file."""

    user: dict
    written: int
    found: bool


def load(path: Path | None = None) -> Settings:
    """The settings in path, PATH by default (read when called, so that
    PATH can be pointed elsewhere). A file an earlier version wrote is read
    upgraded to FORMAT, and saved so, unless it is a link (a dotfile
    manager's): replacing it would cut the link."""
    path = path or PATH
    with _LOCK:
        read = _read(path)
        settings = _settings(_merged(read.user, path), path)
        if read.found and read.written < FORMAT and not path.is_symlink():
            try:
                _write(path, read.user)
                log.info("settings upgraded from format %d to %d", read.written, FORMAT)
            except OSError as e:  # read as upgraded all the same
                log.warning("cannot save the upgraded settings: %s", e)
    return settings


def default_options() -> Options:
    """The scan options of defaults.toml, without the user's file."""
    return _settings(_merged({}, DEFAULTS), DEFAULTS).options


def scan_options(scan: dict, written: int, where: str) -> Options:
    """The Options in scan, a scan table from a file of format written (a
    kept scan's record), read as a settings file's scan table is
    (_as_this_version); where names the file in errors. Raises ConfigError
    for a value no scan can have."""
    user = _as_this_version({"scan": dict(scan)}, written, where)
    return _settings(_merged(user, where), where).options


def _settings(raw: dict, path: Path | str) -> Settings:
    """raw (the defaults merged with path's values) as Settings; raises
    ConfigError for a value no setting can have."""
    if "\0" in raw["out_dir"]:  # no file name can hold it
        raise ConfigError(f"{path}: out_dir holds a NUL character")
    try:
        out_dir = Path(raw["out_dir"]).expanduser()
    except RuntimeError as e:  # ~someone, who has no home folder here
        raise ConfigError(f"{path}: out_dir: {e}") from e
    if not out_dir.is_absolute():
        raise ConfigError(
            f"{path}: out_dir must be an absolute path or start with ~, not {raw['out_dir']!r}"
        )
    try:
        options = Options(**raw["scan"])
    except ValueError as e:
        raise ConfigError(f"{path}: scan.{e}") from e
    return Settings(
        out_dir=out_dir,
        options=options,
        debug=raw["debug"],
        show_when_paper_loaded=raw["window"]["show_when_paper_loaded"],
        review_after_scan=raw["window"]["review_after_scan"],
    )


def home_relative(folder: Path) -> str:
    """folder as the settings file spells it: under the home folder, with ~."""
    if folder == Path.home():
        return "~"
    try:
        return f"~/{folder.relative_to(Path.home())}"
    except ValueError:
        return str(folder)


def save(name: str, value, path: Path | None = None) -> None:
    """Set one setting (dotted, e.g. "window.show_when_paper_loaded") in the
    user's file (path, PATH by default, read when called), keeping the others
    there. The file is rewritten without its comments, in FORMAT. A later
    version's file is refused, left as it is: this version's values may not
    be that version's, and it reads its own format strictly."""
    path = path or PATH
    with _LOCK:
        read = _read(path)
        if read.written > FORMAT:
            raise ConfigError(
                f"{path} was saved by a later version of {APP_NAME}: "
                "update the app to change settings"
            )
        _set(read.user, name, value)
        _settings(_merged(read.user, path), path)  # refuse what load() rejects
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, read.user)


def _set(settings: dict, name: str, value) -> None:
    """Set the dotted setting name in settings, making its tables; raises
    ConfigError where settings hold something else in a table's place."""
    *tables, key = name.split(".")
    for part in tables:
        settings = settings.setdefault(part, {})
        if not isinstance(settings, dict):
            raise ConfigError(
                f"{part} in the settings file is no table: {name} not set"
            )
    settings[key] = value


def _get(settings: dict, name: str):
    """The dotted setting name in settings."""
    for part in name.split("."):
        settings = settings[part]
    return settings


def _write(path: Path, user: dict) -> None:
    files.replace_file(
        path, tomli_w.dumps({VERSION_KEY: FORMAT, **user}).encode(), FILE_MODE
    )


def _read(path: Path) -> _Read:
    """The settings file at path (_Read); raises ConfigError for a file
    that cannot be read."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = None
    except (OSError, ValueError) as e:  # ValueError: not UTF-8
        raise ConfigError(f"{path}: {e}") from e
    try:
        user = {} if text is None else tomllib.loads(text)
        written = user.pop(VERSION_KEY, 1)
        return _Read(_as_this_version(user, written, path), written, text is not None)
    except (ValueError, RecursionError) as e:  # RecursionError: nested too
        raise ConfigError(f"{path}: {e}") from e  # deep for Python to read


def _as_this_version(user: dict, written: int, where: Path | str) -> dict:
    """user (settings in format written) as this version reads them. An
    earlier format is upgraded in place (_UPGRADES); from a later format,
    each setting this version has no place for or cannot use is logged and
    left out, so its default applies (_usable). Raises ConfigError for a
    format that is no number."""
    if type(written) is not int or written < 1:
        raise ConfigError(f"{where}: {VERSION_KEY} must be a number, not {written!r}")
    for version in range(written + 1, FORMAT + 1):
        _UPGRADES[version](user)
    return _usable(user, where) if written > FORMAT else user


def _show_through_on_some_pages(user: dict) -> None:
    """Format 2 (0.1.2): show-through removal switched on or off
    (remove_show_through) becomes show_through. On meant every page; it is
    now gray and color pages, which keeps black and white pages' letters."""
    scan = user.get("scan")
    if isinstance(scan, dict) and isinstance(scan.get("remove_show_through"), bool):
        on = scan.pop("remove_show_through")
        scan["show_through"] = (
            ShowThrough.GRAY_AND_COLOR if on else ShowThrough.KEEP
        ).value


_UPGRADES = {2: _show_through_on_some_pages}  # by the format each makes


def _usable(user: dict, where: Path | str) -> dict:
    """A later version's settings (user) as far as this version can use
    them: the defaults, with each of user's values this version has a place
    for and can use; each other one is logged."""

    def drop(name: str, why: str) -> None:
        log.warning("setting %r is from a later version: not used (%s)", name, why)

    defaults = _defaults()
    usable = _merge(defaults, user, reject=drop)
    for name, value in _leaves(usable):
        default = _get(defaults, name)
        if value == default:
            continue
        alone: dict = {}
        _set(alone, name, value)
        try:
            _settings(_merge(_defaults(), alone), where)
        except ConfigError as e:
            drop(name, str(e))
            _set(usable, name, default)
    return usable


def _leaves(settings: dict, where: str = "") -> list[tuple[str, object]]:
    """Each setting in settings, by dotted name, with its value."""
    found = []
    for key, value in settings.items():
        if isinstance(value, dict):
            found += _leaves(value, f"{where}{key}.")
        else:
            found.append((f"{where}{key}", value))
    return found


def _defaults() -> dict:
    return tomllib.loads(DEFAULTS.read_text())


def _merged(user: dict, path: Path | str) -> dict:
    try:
        return _merge(_defaults(), user)
    except (ValueError, RecursionError) as e:
        raise ConfigError(f"{path}: {e}") from e


def _merge(
    base: dict,
    override: dict,
    where: str = "",
    reject: Callable[[str, str], None] | None = None,
) -> dict:
    """The defaults with the user's values; each value must have its
    default's type (the defaults are the schema). A value that does not fit
    raises ValueError, or goes to reject (its name, and why) when given."""
    out = dict(base)
    for key, value in override.items():
        name = f"{where}{key}"
        default = base.get(key)
        if key not in base:
            why = f"unknown setting {name}"
        elif isinstance(default, dict) and isinstance(value, dict):
            out[key] = _merge(default, value, f"{name}.", reject)
            continue
        elif type(value) is type(default) and not isinstance(default, dict):
            out[key] = value
            continue
        else:
            why = (
                f"{name} must be a {type(default).__name__}, not {reprlib.repr(value)}"
            )
        if reject is None:
            raise ValueError(why)
        reject(name, why)
    return out
