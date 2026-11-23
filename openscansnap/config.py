"""Settings: defaults.toml, overridden key by key by the user's file."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from . import files, sane
from .options import Options

DEFAULTS = Path(__file__).with_name("defaults.toml")
PATH = sane.CONFIG_DIR.parent / "config.toml"
LOG_DIR = Path.home() / "Library" / "Logs" / "open-scansnap"
RAW_DIR = LOG_DIR / "raw"  # debug mode keeps each scan's raw sides here
# the latest scan, kept for review (review.py)
REVIEW_DIR = (
    Path.home() / "Library" / "Application Support" / "open-scansnap" / "review"
)
FILE_MODE = 0o644


class ConfigError(sane.ScanError):
    """The user's settings file cannot be used; the message says why."""


@dataclass(frozen=True)
class Settings:
    out_dir: Path
    options: Options
    debug: bool
    show_when_paper_loaded: bool
    review_after_scan: bool


def load(path: Path | None = None) -> Settings:
    """The settings in path, PATH by default (read when called, so that
    PATH can be pointed elsewhere)."""
    path = path or PATH
    return _settings(_merged(_user(path), path), path)


def default_options() -> Options:
    """The scan options of defaults.toml, without the user's file."""
    return _settings(_merged({}, DEFAULTS), DEFAULTS).options


def _settings(raw: dict, path: Path) -> Settings:
    """raw (the defaults merged with path's values) as Settings; raises
    ConfigError for a value no setting can have."""
    out_dir = Path(raw["out_dir"]).expanduser()
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
    there. The file is rewritten without its comments."""
    path = path or PATH
    user = _user(path)
    *tables, key = name.split(".")
    table = user
    for part in tables:
        table = table.setdefault(part, {})
    table[key] = value
    _settings(_merged(user, path), path)  # refuse to write what load() rejects
    path.parent.mkdir(parents=True, exist_ok=True)
    files.replace_file(path, tomli_w.dumps(user).encode(), FILE_MODE)


def _user(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as e:  # including tomllib.TOMLDecodeError
        raise ConfigError(f"{path}: {e}") from e


def _merged(user: dict, path: Path) -> dict:
    try:
        return _merge(tomllib.loads(DEFAULTS.read_text()), user)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e


def _merge(base: dict, override: dict, where: str = "") -> dict:
    """The defaults with the user's values; each value must have its
    default's type (the defaults are the schema)."""
    out = dict(base)
    for key, value in override.items():
        name = f"{where}{key}"
        if key not in base:
            raise ValueError(f"unknown setting {name}")
        default = base[key]
        if isinstance(default, dict) and isinstance(value, dict):
            out[key] = _merge(default, value, f"{name}.")
        elif type(value) is type(default) and not isinstance(default, dict):
            out[key] = value
        else:
            raise ValueError(
                f"{name} must be a {type(default).__name__}, not {value!r}"
            )
    return out
