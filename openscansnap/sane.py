"""SANE configuration and scanning through the patched fujitsu backend.

The backend in sane-backends/ (branch vens) is built into sane-prefix/; see
the README. Its configuration lives in CONFIG_DIR, not in the build tree.
"""

import ipaddress
import os
import plistlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers.expat import ExpatError

from . import bundle, files

SCANIMAGE = bundle.SCANIMAGE
CONFIG_DIR = Path.home() / ".config" / "open-scansnap" / "sane.d"
SCANSNAP_HOME_SETTINGS = (
    Path.home()
    / "Library"
    / "Preferences"
    / "com.fujitsu.pfu.ScanSnapHomeSetting.plist"
)
BACKEND = "fujitsu"
DEVICE_PREFIX = "fujitsu:wifi:"
# The first scanner on USB (the backend opens it without reaching for the
# Wi-Fi scanners), and the configuration lines that let it find one: the
# iX500's USB vendor and product, as SANE's own fujitsu.conf has them.
USB_DEVICE = "fujitsu:usb"
USB_LINES = ("usb 0x04c5 0x132b",)
# The configuration's lines, as write_config writes them. The "# scanner"
# line holds the scanner's hardware address, for following it to a new IP
# address; SANE skips it as a comment.
WIFI_LINE = re.compile(r"^wifi\s+(\S+)\s*$", re.MULTILINE)
HOSTID_LINE = re.compile(r"^option wifi-hostid\s+(\S+)\s*$", re.MULTILINE)
PASSWORD_LINE = re.compile(r"^option wifi-password\s+(\S+)\s*$", re.MULTILINE)
SCANNER_LINE = re.compile(r"^# scanner\s+(\S+)\s*$", re.MULTILINE)
PAGE_PATTERN = "page%04d.tif"
EXIT_NO_DOCS = 7  # scanimage: the feeder is empty
# Every side in color and whole, backing included, at the scanner's 300 dpi:
# sheet.py finds the sheet, pages.py drops blank sides and chooses how each
# is stored.
# SANE's own crop (--swcrop) places a faint leading edge millimeters inside
# the paper.
SCAN_OPTIONS = [
    "--source",
    "ADF Duplex",
    "--mode",
    "Color",
    "--format=tiff",
]
# The configuration holds the scanner's credentials: owner only.
PRIVATE_DIR, PRIVATE_FILE = 0o700, 0o600
HOSTID_MAX = 2**64 - 1
PASSWORD_MAX = 16  # characters; the backend's VENS_PASSWORD_MAX
# scanimage cancels the scan on Ctrl-C (SIGINT reaches it too), ends the
# job and releases the scanner; this long is allowed for that
CANCEL_WAIT = 10  # seconds
# scanimage's messages (sane_strstatus) for the failures a user can fix
BUSY = "Device busy"
REFUSED = "Access to resource has been denied"
REFUSED_MESSAGE = "Scanner refused the password or HostID"


class ScanError(Exception):
    """A problem the user can fix. The message says what is wrong, and how to
    fix it when that is the same in the app and on the command line; for
    the subclasses below, each of them says how."""


class NotSetUp(ScanError):
    """No scanner is set up."""


class NotFound(NotSetUp):
    """No scanner answered the search to set one up."""


class Refused(ScanError):
    """The scanner refused the password or the HostID."""


class BadConfiguration(ScanError):
    """The scanner configuration exists but cannot be used."""


class ScanStopped(ScanError):
    """Scanning failed after some sides were complete; pages holds them."""

    def __init__(self, message: str, pages: list[Path]):
        super().__init__(message)
        self.pages = pages


@dataclass(frozen=True)
class Scanner:
    host: str  # IPv4 address; the backend speaks IPv4 only
    hostid: int
    password: str | None = field(default=None, repr=False)  # None: the default
    mac: str | None = None  # the scanner's hardware address, when known

    def __post_init__(self):
        try:
            address = ipaddress.IPv4Address(self.host)
        except ValueError as e:
            raise ScanError(f"Scanner address must be IPv4: {e}") from e
        object.__setattr__(self, "host", str(address))  # as the config file spells it
        if not 0 < self.hostid <= HOSTID_MAX:
            raise ScanError(f"HostID must be between 1 and 0x{HOSTID_MAX:x}")
        if self.password is not None and not re.fullmatch(
            rf"[!-~]{{1,{PASSWORD_MAX}}}", self.password
        ):
            raise ScanError(
                f"Password must be 1 to {PASSWORD_MAX} printable characters without spaces"
            )


def scansnap_home_hostid() -> int | None:
    """The HostID ScanSnap Home paired a Wi-Fi scanner with on this Mac;
    None when it never did."""
    try:
        with open(SCANSNAP_HOME_SETTINGS, "rb") as f:
            settings = plistlib.load(f)
        scanners = list(settings.get("Common", {}).get("Wifi Connected Scanner", []))
    except (OSError, ValueError, ExpatError, AttributeError, TypeError):
        return None  # none there, or not as ScanSnap Home writes it
    for scanner in scanners:
        try:
            hostid = int(scanner.get("AutoConnectInfo_ScannerInfo", {}).get("HostID"))
        except (ValueError, AttributeError, TypeError):
            continue  # this entry is not as ScanSnap Home writes it
        if hostid:
            return hostid
    return None


def write_config(scanner: Scanner | None) -> Path:
    """Write the SANE configuration for this Wi-Fi scanner (None: for a
    scanner on USB only), readable by the owner only, and return its
    file."""
    CONFIG_DIR.mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=True)
    CONFIG_DIR.chmod(PRIVATE_DIR)
    lines = list(USB_LINES)
    if scanner is not None:
        lines.append(f"option wifi-hostid 0x{scanner.hostid:016x}")
        if scanner.password:
            lines.append(f"option wifi-password {scanner.password}")
        if scanner.mac:
            lines.append(f"# scanner {scanner.mac}")
        lines.append(f"wifi {scanner.host}")
    conf = CONFIG_DIR / f"{BACKEND}.conf"
    files.replace_file(conf, ("\n".join(lines) + "\n").encode(), PRIVATE_FILE)
    files.replace_file(CONFIG_DIR / "dll.conf", f"{BACKEND}\n".encode(), PRIVATE_FILE)
    return conf


def configured() -> Scanner | None:
    """The Wi-Fi scanner write_config configured; None when there is no
    configuration or it is for USB only. Raises BadConfiguration, naming the
    file, when it cannot be read or names no scanner this program can use."""
    conf = CONFIG_DIR / f"{BACKEND}.conf"
    try:
        text = conf.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise BadConfiguration(f"{conf}: {e}") from e
    lines = {
        name: pattern.search(text)
        for name, pattern in (
            ("host", WIFI_LINE),
            ("hostid", HOSTID_LINE),
            ("password", PASSWORD_LINE),
            ("mac", SCANNER_LINE),
        )
    }
    if not (lines["host"] or lines["hostid"]):
        return None  # for USB only
    try:
        if not (lines["host"] and lines["hostid"]):
            raise ScanError("it has no wifi line or no wifi-hostid option")
        try:
            hostid = int(lines["hostid"].group(1), 0)
        except ValueError as e:
            raise ScanError(f"wifi-hostid: {e}") from e
        return Scanner(
            **{name: match.group(1) for name, match in lines.items() if match}
            | {"hostid": hostid}
        )
    except ScanError as e:
        raise BadConfiguration(f"{conf}: {e}") from e


def device(scanner: Scanner) -> str:
    """scanner's SANE device name."""
    return DEVICE_PREFIX + scanner.host


def ensure_config() -> None:
    """A configuration that finds a scanner on USB: written where there is
    none, and the USB lines added to one written without them. Raises
    BadConfiguration for a configuration that cannot be used."""
    conf = CONFIG_DIR / f"{BACKEND}.conf"
    if not conf.exists():
        write_config(None)
        return
    try:
        lines = conf.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise BadConfiguration(f"{conf}: {e}") from e
    if not all(line in lines for line in USB_LINES):
        write_config(configured())


def scan(out_dir: Path, device_name: str) -> list[Path]:
    """Scan every sheet in the feeder of the scanner device_name (SANE's
    name) into out_dir; return the page files in order. Raises ScanStopped,
    with the complete sides, if scanning fails after some sides."""
    if not SCANIMAGE.exists():
        raise ScanError(f"{SCANIMAGE} not found; build SANE first (see README)")
    process = subprocess.Popen(
        [
            str(SCANIMAGE),
            "-d",
            device_name,
            *SCAN_OPTIONS,
            f"--batch={out_dir / PAGE_PATTERN}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **bundle.sane_environment(CONFIG_DIR)},
    )
    try:
        _, stderr = process.communicate()
    except KeyboardInterrupt:
        try:
            process.communicate(timeout=CANCEL_WAIT)  # keeps draining stderr
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    # scanimage renames a side to its page name only once it is complete
    pages = sorted(out_dir.glob("page*.tif"))
    if process.returncode == EXIT_NO_DOCS and not pages:
        raise ScanError("No paper in the scanner")
    if process.returncode in (0, EXIT_NO_DOCS):
        return pages
    if BUSY in stderr:
        message = (
            "Scanner busy: quit ScanSnap Home and its menu bar item, or wait 30 seconds"
        )
    elif REFUSED in stderr:
        if not pages:
            raise Refused(REFUSED_MESSAGE)
        message = REFUSED_MESSAGE
    else:
        last = stderr.strip().splitlines()[-1:] or ["no message"]
        message = f"Scanning failed: {last[0]}"
    if pages:
        raise ScanStopped(message, pages)
    raise ScanError(message)
