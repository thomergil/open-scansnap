"""Watch the scanner and scan when asked: by its scan button or by the app.

One thread owns the scanner connection. It keeps the scanner open between
scans, polls the scan button and the paper sensor, and closes the connection
while a scan runs (the scan opens its own). A scan only takes the sheets in;
a second thread makes their PDF, so the scanner is ready for the next batch
at once. The app reads a Snapshot and asks for scans through request_scan();
it never touches the scanner.
"""

import logging
import queue
import string
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeVar

from . import config, firmware, job, sane, saneapi, scanner

log = logging.getLogger(__name__)
T = TypeVar("T")

# The scan button reports a press as one reading, whatever its length
# (measured on the iX500 at this interval).
POLL_SECONDS = 0.2
# How often to look for a scanner that is off or out of reach. Each try
# takes up to about 8 s more while the scanner does not answer.
RETRY_SECONDS = 3.0
# The fujitsu backend's options: its sensors, its identity, and, over USB,
# its Wi-Fi setup
SCAN_BUTTON, PAPER = "scan", "page-loaded"
MODEL_NAME, FIRMWARE_VERSION = "scanner-model", "firmware-version"
WIFI_NETWORKS = "wifi-networks"  # the networks it sees
WIFI_CURRENT = "wifi-current"  # the network it is set up for
WIFI_JOIN = "wifi-join"
PAIRED_HOSTIDS = "wifi-paired-hostids"  # the computers allowed on Wi-Fi
PAIR_HOSTID = "wifi-pair-hostid"  # allow one, forgetting the others
FIELD_SEPARATOR, LINE_SEPARATOR, HOSTID_SEPARATOR = "\t", "\n", ","
HEX_DIGITS = frozenset(string.hexdigits)


class State(StrEnum):
    NOT_SET_UP = "not set up"  # no scanner configured, and none found
    BAD_CONFIGURATION = "bad configuration"  # the configuration cannot be used
    UNUSABLE = "unusable"  # anything else that keeps the scanner from use
    OFFLINE = "offline"  # off, asleep or out of reach
    BUSY = "busy"  # another program has it
    REFUSED = "refused"  # wrong password or HostID
    READY = "ready"  # no paper
    PAPER = "paper"  # ready, paper loaded
    SCANNING = "scanning"


class Via(StrEnum):
    """How the scanner is connected."""

    USB = "usb"
    WIFI = "wifi"


@dataclass(frozen=True)
class Snapshot:
    state: State = State.OFFLINE
    message: str = ""  # the last scan's result, or why the scanner is unusable
    paper_loads: int = 0  # times paper was put in the feeder
    making: int = 0  # PDFs being made or waiting to be
    failure: str = ""  # why a PDF could not be made; kept until the next scan
    progress: tuple[int, int] | None = None  # the PDF being made: sheet, of sheets
    saved: Path | None = None  # the PDF made last
    via: Via | None = None  # how the scanner is connected; None: it is not
    wifi: "Wifi | None" = None  # the scanner's Wi-Fi settings, read over USB
    identity: "Identity | None" = None  # the scanner's model and firmware


class Device(Protocol):
    """What the monitor needs from an open scanner, as saneapi.Device has."""

    name: str  # SANE's device name

    def has(self, name: str) -> bool: ...

    def read_text(self, name: str) -> str: ...

    def write_text(self, name: str, text: str) -> None: ...

    def read(self, names: tuple[str, ...]) -> dict[str, int]: ...

    def close(self) -> None: ...


class Connector:
    """Opens the scanner: the one on USB when there is one, else the Wi-Fi
    scanner, set up first when none is (scanner.set_up) and followed when
    it no longer answers at its address. Raises saneapi.SaneError when it
    cannot be reached, sane.ScanError when it is not set up or SANE cannot
    start.

    libsane is started (sane_init) on the caller's thread, and started
    again when the configuration changed, since the backend reads it at
    sane_init; call exit() once no Device is in use."""

    def __init__(self):
        self._library = None
        self._started_with = None  # the Wi-Fi configuration libsane read

    def __call__(self) -> saneapi.Device:
        scanner.ensure_config()  # one that finds a scanner on USB, at least
        configured = sane.configured()
        try:
            return self._started(configured).open(sane.USB_DEVICE)
        except saneapi.SaneError as e:
            if e.status != saneapi.INVAL:  # the backend's "no scanner on USB"
                raise  # a scanner on USB, that cannot be used now
        if configured is None:
            configured = scanner.set_up(replacing=False)
        elif configured.mac is None:  # set up by address: learn it, to follow it
            configured = scanner.learn_mac(configured)
        try:
            return self._started(configured).open(sane.device(configured))
        except saneapi.SaneError as e:
            if connection_error(e) is not None:
                raise  # it answered: in use, or refusing us
            moved = scanner.follow(configured)
            if moved is None:
                raise
            return self._started(moved).open(sane.device(moved))

    def _started(self, configured: sane.Scanner | None) -> saneapi.Library:
        """libsane, started with configured as the Wi-Fi configuration."""
        if configured != self._started_with:
            self.exit()
        if self._library is None:
            try:
                self._library = saneapi.Library()
            except OSError as e:  # libsane missing or not loadable
                raise sane.ScanError(f"Cannot load SANE: {e}") from e
            self._started_with = configured
        return self._library

    def exit(self) -> None:
        if self._library is not None:
            self._library.exit()
            self._library = None
            self._started_with = None


@dataclass(frozen=True)
class Wifi:
    """The scanner's Wi-Fi settings, as read over USB."""

    network: (
        str | None
    )  # the network it is set up for, to show; "": none; None: unknown
    allowed_here: bool | None  # whether this Mac may scan over Wi-Fi; None: unknown
    others_allowed: bool  # whether other computers may

    @property
    def forgets_others(self) -> bool | None:
        """Whether allowing this Mac (set_up_wifi) makes the scanner forget
        other computers; None: unknown."""
        if self.allowed_here is None:
            return None
        return self.others_allowed and not self.allowed_here


def wifi_of(device: Device) -> "Wifi | None":
    """The Wi-Fi settings of the scanner, on USB; None when it cannot set
    up Wi-Fi."""
    if not device.has(WIFI_NETWORKS):
        return None
    network = allowed_here = None
    others = False
    try:
        current = device.read_text(WIFI_CURRENT).split(FIELD_SEPARATOR)
        network = saneapi.shown((current + ["", "", ""])[2])
    except saneapi.SaneError as e:
        log.info("cannot read the scanner's Wi-Fi network: %s", e)
    try:
        allowed = set(
            filter(None, device.read_text(PAIRED_HOSTIDS).split(HOSTID_SEPARATOR))
        )
        here = f"{scanner.this_hostid():016x}"
        allowed_here, others = here in allowed, bool(allowed - {here})
    except (saneapi.SaneError, OSError) as e:
        log.info("cannot read the computers allowed on Wi-Fi: %s", e)
    return Wifi(network, allowed_here, others)


@dataclass(frozen=True)
class Network:
    """A Wi-Fi network the scanner sees."""

    ssid: str  # its name, as read_text gives it: sent back unchanged to join it
    security: str  # as the scanner names it: WPA2, OPEN, ...
    encryption: str  # AES, TKIP, NONE, ...
    signal: int  # the scanner's measure: higher is stronger

    @property
    def name(self) -> str:
        """Its name, to show."""
        return saneapi.shown(self.ssid)

    @property
    def open(self) -> bool:
        """Whether it takes no password."""
        return self.encryption == NO_ENCRYPTION


NO_ENCRYPTION, WEP = "NONE", "WEP"  # the scanner's names
# Passphrases the networks take (IEEE 802.11): printable ASCII of these
# lengths, or hexadecimal digits of these
WEP_LENGTHS, WEP_HEX_LENGTHS = (5, 13), (10, 26)
WPA_LENGTHS, WPA_HEX_LENGTH = range(8, 64), 64
WEP_PROBLEM = "The password must be 5 or 13 characters, or 10 or 26 hexadecimal digits"
WPA_PROBLEM = "The password must be 8 to 63 characters, or 64 hexadecimal digits"
NOT_PRINTABLE = (
    "The password can contain only unaccented letters, digits, spaces and the "
    "punctuation on a US keyboard"
)


def networks(device: Device) -> list[Network]:
    """The Wi-Fi networks the scanner, on USB, sees: strongest first, each
    once (one name can be several networks, secured differently)."""
    found: dict[tuple[str, str, str], Network] = {}
    for line in device.read_text(WIFI_NETWORKS).split(LINE_SEPARATOR):
        fields = line.split(FIELD_SEPARATOR, 3)
        if len(fields) != 4 or not fields[3] or not fields[2].lstrip("-").isdigit():
            continue
        security, encryption, signal, ssid = fields
        network = Network(ssid, security, encryption, int(signal))
        key = (ssid, security, encryption)
        if key not in found or found[key].signal < network.signal:
            found[key] = network
    return sorted(found.values(), key=lambda network: -network.signal)


def passphrase_problem(network: Network, password: str) -> str | None:
    """Why network cannot take password; None when it can. An open network
    takes none."""
    if network.open:
        return None
    if not (password.isascii() and password.isprintable()):
        return NOT_PRINTABLE
    if network.encryption == WEP:
        fits = len(password) in WEP_LENGTHS or (
            len(password) in WEP_HEX_LENGTHS and set(password) <= HEX_DIGITS
        )
        return None if fits else WEP_PROBLEM
    fits = len(password) in WPA_LENGTHS or (
        len(password) == WPA_HEX_LENGTH and set(password) <= HEX_DIGITS
    )
    return None if fits else WPA_PROBLEM


def set_up_wifi(
    device: Device, network: Network, password: str, *, others_warned: bool
) -> None:
    """Have the scanner, on USB, join network with password (its Wi-Fi
    passphrase), by DHCP, and let this Mac scan over Wi-Fi. others_warned:
    whether the user was told that other computers will be forgotten
    (Wifi.forgets_others). Raises sane.ScanError with nothing changed when
    the password does not fit, the scanner is not on USB, the computers
    allowed cannot be read, other computers would be forgotten without the
    user being told, or this Mac's HostID cannot be found; and after the
    scanner was told to join, when allowing this Mac fails (allow_here)."""
    problem = passphrase_problem(network, password)
    if problem is not None:
        raise sane.ScanError(problem)
    wifi = wifi_of(device)
    if wifi is None:
        raise sane.ScanError(
            "Connect the scanner with its USB cable to set up its Wi-Fi"
        )
    if wifi.forgets_others is None:
        raise sane.ScanError(
            "Cannot read which computers may scan over Wi-Fi; nothing was changed"
        )
    if wifi.forgets_others and not others_warned:
        raise sane.ScanError(
            "The scanner now allows another computer on Wi-Fi, and joining would "
            "stop that computer from scanning; nothing was changed. "
            "Choose Scan networks, then join again"
        )
    hostid = this_hostid()
    device.write_text(
        WIFI_JOIN,
        FIELD_SEPARATOR.join(
            (network.security, network.encryption, network.ssid, password)
        ),
    )
    log.info("the scanner is joining a Wi-Fi network")  # neither name nor password
    if not wifi.allowed_here:
        allow_here(device, hostid)


def this_hostid() -> int:
    try:
        return scanner.this_hostid()
    except OSError as e:
        raise sane.ScanError(f"Cannot find this Mac's HostID: {e}") from e


def allow_here(device: Device, hostid: int) -> None:
    """Let this Mac (hostid) scan over Wi-Fi: the scanner, on USB, forgets
    every other computer (phones are not affected)."""
    try:
        device.write_text(PAIR_HOSTID, f"{hostid:016x}")
    except saneapi.SaneError as e:
        log.warning("allowing this Mac on Wi-Fi failed: %s", e)
        raise sane.ScanError(
            "The scanner is joining the network but could not allow this Mac to "
            "scan over Wi-Fi. Choose Scan networks and join again; other "
            "computers may also need to be set up again"
        ) from e
    log.info("this Mac may now scan over Wi-Fi")


@dataclass(frozen=True)
class Identity:
    model: str  # as the scanner names itself: ScanSnap iX500
    firmware: str  # its firmware version: 0U00
    newer: str | None  # a newer firmware release; None: none, or not known
    page: str | None  # where to get ScanSnap Home for it; None: model not known


def identity_of(device: Device) -> Identity | None:
    """The scanner's model and firmware; None when it does not say."""
    if not (device.has(MODEL_NAME) and device.has(FIRMWARE_VERSION)):
        return None
    try:
        model = device.read_text(MODEL_NAME)
        version = device.read_text(FIRMWARE_VERSION)
    except saneapi.SaneError as e:
        log.info("cannot read the scanner's firmware version: %s", e)
        return None
    model, version = saneapi.shown(model), saneapi.shown(version)
    log.info("%s, firmware %s", model, version)
    return Identity(
        model, version, firmware.newer(model, version), firmware.page(model)
    )


def via_of(device_name: str) -> Via:
    return Via.USB if device_name == sane.USB_DEVICE else Via.WIFI


def error_state(error: sane.ScanError) -> State:
    """The state a failure to reach the scanner leaves it in."""
    for kind, state in ERROR_STATE.items():
        if isinstance(error, kind):
            return state
    return State.UNUSABLE


ERROR_STATE = {
    sane.NotSetUp: State.NOT_SET_UP,
    sane.BadConfiguration: State.BAD_CONFIGURATION,
    sane.Refused: State.REFUSED,
    sane.Busy: State.BUSY,
}


def connection_error(error: saneapi.SaneError) -> sane.ScanError | None:
    """The failure, one the user can fix, that opening the scanner met: it
    answered, but is in use or refuses us; None for anything else (off, or
    out of reach)."""
    if error.status == saneapi.DEVICE_BUSY:
        return sane.Busy(sane.BUSY_MESSAGE)
    if error.status == saneapi.ACCESS_DENIED:
        return sane.Refused(sane.REFUSED_MESSAGE)
    return None


# The detail when watching the scanner failed unexpectedly (a bug)
UNEXPECTED = (
    "Something unexpected went wrong ({error}); trying again. The log has "
    "details: " + config.home_relative(config.LOG_DIR)
)


class Monitor:
    def __init__(
        self,
        connect: Callable[[], Device],
        scan: Callable[[str], Callable[[job.Progress], Path]],
        poll: float = POLL_SECONDS,
        retry: float = RETRY_SECONDS,
    ):
        """connect opens the scanner (raising saneapi.SaneError, or
        sane.ScanError when it is not set up). scan takes the sheets in from
        the scanner of that SANE device name and returns what makes their
        PDF: a call that returns the PDF, telling its progress as it goes
        (job.Progress). Both raise sane.ScanError for
        problems the user can fix."""
        self._connect, self._scan = connect, scan
        self._device: Device | None = None  # the open scanner; its thread's own
        self._last: dict[str, int] | None = None  # its sensors' last reading
        self._device_name: str | None = None  # of the scanner last opened
        self._pdfs: queue.Queue[tuple[Callable[[job.Progress], Path], Future]] = (
            queue.Queue()
        )
        self._maker = threading.Thread(target=self._make_pdfs, name="pdf", daemon=True)
        self._poll, self._retry = poll, retry
        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self._wake = threading.Event()  # cut a wait short
        self._requested = threading.Event()
        self._jobs: queue.Queue[tuple[Callable[[Device], object], Future]] = (
            queue.Queue()
        )
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="scanner", daemon=True)

    @property
    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def start(self) -> None:
        self._thread.start()
        self._maker.start()

    def request_scan(self) -> None:
        self._requested.set()
        self._wake.set()

    def run_on_scanner(self, task: Callable[[Device], T]) -> Future[T]:
        """Run task with the open scanner, on the thread that owns it; the
        future holds what task returns or raises (sane.ScanError when no
        scanner is open)."""
        future: Future[T] = Future()
        self._jobs.put((task, future))
        self._wake.set()
        return future

    def queue_pdf(self, make_pdf: Callable[[job.Progress], Path]) -> Future[Path]:
        """Make a PDF on the PDF thread, after those already waiting: a scan's,
        or one made again (review.Store.remake). The future holds the PDF,
        or what making it raised."""
        future: Future[Path] = Future()
        with self._lock:
            self._snapshot = replace(self._snapshot, making=self._snapshot.making + 1)
        self._pdfs.put((make_pdf, future))
        return future

    def refresh_wifi(self, device: Device) -> None:
        """Read the scanner's Wi-Fi settings again (a job, over USB)."""
        self._set(wifi=wifi_of(device))

    def stop(self, wait: float) -> None:
        """Stop watching; the scanner is released once a scanner call in
        progress returns. Waits at most wait seconds for that."""
        self._stopping.set()
        self._wake.set()
        self._thread.join(wait)

    def _set(self, **changes) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    self._turn()
                except Exception as e:  # shown, not lost with the thread
                    log.exception("watching the scanner failed")
                    self._release()
                    self._disconnected(State.UNUSABLE, UNEXPECTED.format(error=e))
                    self._run_jobs()  # they fail: not connected
                    self._wait(self._retry)
        finally:
            self._release()

    def _turn(self) -> None:
        """One round: scan when asked; else connect when not connected, run
        the jobs, and read the sensors."""
        if self._requested.is_set():
            self._requested.clear()
            self._release()
            self._run_scan()
            return
        if self._device is None:
            try:
                self._device = self._connect()
            except saneapi.SaneError as e:
                error = connection_error(e)
                self._disconnected(error_state(error) if error else State.OFFLINE, "")
                log.debug("scanner not available: %s", e)
                self._run_jobs()  # they fail: not connected
                self._wait(self._retry)
                return
            except sane.ScanError as e:
                self._disconnected(error_state(e), str(e))
                self._run_jobs()
                self._wait(self._retry)
                return
            self._device_name = self._device.name
            via = via_of(self._device.name)
            log.info("connected over %s", via)
            self._set(
                via=via,
                wifi=wifi_of(self._device) if via == Via.USB else None,
                identity=identity_of(self._device),
            )
        self._run_jobs()
        try:
            values = self._device.read((SCAN_BUTTON, PAPER))
        except saneapi.SaneError as e:
            log.info("scanner went away: %s", e)
            self._release()
            self._disconnected(State.OFFLINE, "")
            return
        self._observe(self._last, values)
        self._last = values
        self._wait(self._poll)

    def _release(self) -> None:
        """Close the scanner, when open."""
        device, self._device, self._last = self._device, None, None
        if device is not None:
            device.close()

    def _disconnected(self, state: State, message: str) -> None:
        self._set(state=state, message=message, via=None, wifi=None, identity=None)

    def _run_jobs(self) -> None:
        device = self._device
        while True:
            try:
                task, future = self._jobs.get_nowait()
            except queue.Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue
            if device is None:
                future.set_exception(sane.ScanError("The scanner is not connected"))
                continue
            try:
                future.set_result(task(device))
            except Exception as e:  # noqa: BLE001 - handed to the caller, not swallowed
                future.set_exception(e)

    def _observe(self, last: dict[str, int] | None, values: dict[str, int]) -> None:
        """React to the sensors: a new press scans, new paper is counted."""
        paper = bool(values[PAPER])
        with self._lock:
            loads = self._snapshot.paper_loads
            if last is not None and paper and not last[PAPER]:
                loads += 1
            self._snapshot = replace(
                self._snapshot,
                state=State.PAPER if paper else State.READY,
                paper_loads=loads,
            )
        if last is not None and values[SCAN_BUTTON] and not last[SCAN_BUTTON]:
            self._requested.set()

    def _run_scan(self) -> None:
        self._set(state=State.SCANNING, message="", failure="")
        try:
            make_pdf = self._scan(self._device_name)
        except sane.ScanError as e:
            self._set(state=State.READY, message=str(e))
        except Exception as e:  # shown, not lost with the thread
            log.exception("scan failed")
            self._set(state=State.READY, message=f"Scanning failed: {e}")
        else:
            # its PDF counted before the scanning stops: no snapshot between
            # shows the app idle (the menu would offer the scan it replaces)
            self.queue_pdf(make_pdf)
            self._set(state=State.READY)  # until the next reading says otherwise

    def _progress(self, sheet: int, sheets: int) -> None:
        self._set(progress=(sheet, sheets))

    def _make_pdfs(self) -> None:
        """Make the PDFs queued (scans', and ones made again) one after the
        other, as they come in."""
        while True:
            make_pdf, future = self._pdfs.get()
            if not future.set_running_or_notify_cancel():  # cancelled while waiting
                with self._lock:
                    self._snapshot = replace(
                        self._snapshot, making=self._snapshot.making - 1
                    )
                continue
            failure, saved, error = "", None, None
            try:
                saved = make_pdf(self._progress)
                message = f"Saved {saved.name}"
            except job.PdfSaved as e:  # saved, though something failed
                log.warning("the PDF is saved, but: %s", e)
                saved, error = e.path, e
                message = failure = str(e)
            except sane.ScanError as e:
                log.warning("making the PDF failed: %s", e)
                message = failure = str(e)
                error = e
            except Exception as e:  # shown, not lost with the thread
                log.exception("making the PDF failed")
                message = failure = f"Making the PDF failed: {e}"
                error = e
            with self._lock:
                self._snapshot = replace(
                    self._snapshot,
                    message=message,
                    failure=failure or self._snapshot.failure,
                    making=self._snapshot.making - 1,
                    progress=None,
                    saved=saved or self._snapshot.saved,
                )
            if error is None:
                future.set_result(saved)
            else:
                future.set_exception(error)
