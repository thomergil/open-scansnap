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
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeVar

from . import firmware, sane, saneapi, scanner

log = logging.getLogger(__name__)
T = TypeVar("T")

# The scan button reports a press as one reading, whatever its length
# (measured on the iX500 at this interval).
POLL_SECONDS = 0.2
# How often to look for a scanner that is off or out of reach. Each try
# takes up to about 8 s more while the scanner does not answer.
RETRY_SECONDS = 3.0
SCAN_BUTTON, PAPER = "scan", "page-loaded"  # the fujitsu backend's sensor names


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
            if e.status != saneapi.INVAL:
                raise  # a scanner on USB, that cannot be used now
        if configured is None:
            configured = scanner.set_up(replacing=False)
        elif configured.mac is None:  # set up by address: learn it, to follow it
            configured = scanner.learn_mac(configured)
        try:
            return self._started(configured).open(sane.device(configured))
        except saneapi.SaneError as e:
            if e.status in STATUS_STATE:
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


# The backend's options for Wi-Fi setup, over USB
PAIRED_HOSTIDS = "wifi-paired-hostids"  # the computers allowed on Wi-Fi
PAIR_HOSTID = "wifi-pair-hostid"  # allow one, forgetting the others
WIFI_CURRENT = "wifi-current"
HOSTID_SEPARATOR = ","


@dataclass(frozen=True)
class Wifi:
    """The scanner's Wi-Fi settings, as read over USB."""

    network: str | None  # the network it is set up for; "": none; None: unknown
    allowed_here: bool | None  # whether this Mac may scan over Wi-Fi; None: unknown
    others_allowed: bool  # whether other computers may


def wifi_of(device: Device) -> "Wifi | None":
    """The Wi-Fi settings of the scanner, on USB; None when it cannot set
    up Wi-Fi."""
    if not device.has(WIFI_NETWORKS):
        return None
    network = allowed_here = None
    others = False
    try:
        current = device.read_text(WIFI_CURRENT).split(FIELD_SEPARATOR)
        network = (current + ["", "", ""])[2]
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


def allow_here(device: Device) -> None:
    """Let this Mac scan over Wi-Fi: the scanner, on USB, forgets every other
    computer (phones are not affected)."""
    try:
        hostid = scanner.this_hostid()
    except OSError as e:
        raise sane.ScanError(f"Cannot find this Mac's HostID: {e}") from e
    device.write_text(PAIR_HOSTID, f"{hostid:016x}")
    log.info("this Mac may now scan over Wi-Fi")


WIFI_NETWORKS = "wifi-networks"  # the backend's options, over USB
WIFI_JOIN = "wifi-join"
FIELD_SEPARATOR, LINE_SEPARATOR = "\t", "\n"


@dataclass(frozen=True)
class Network:
    """A Wi-Fi network the scanner sees."""

    name: str  # its SSID
    security: str  # as the scanner names it: WPA2, OPEN, ...
    encryption: str  # AES, TKIP, NONE, ...
    signal: int  # the scanner's measure: higher is stronger


def networks(device: Device) -> list[Network]:
    """The Wi-Fi networks the scanner, on USB, sees: strongest first, each
    name once."""
    found: dict[str, Network] = {}
    for line in device.read_text(WIFI_NETWORKS).split(LINE_SEPARATOR):
        fields = line.split(FIELD_SEPARATOR, 3)
        if len(fields) != 4 or not fields[3]:
            continue
        security, encryption, signal, name = fields
        network = Network(name, security, encryption, int(signal))
        if name not in found or found[name].signal < network.signal:
            found[name] = network
    return sorted(found.values(), key=lambda network: -network.signal)


def set_up_wifi(device: Device, network: Network, password: str) -> None:
    """Have the scanner, on USB, join network with password (its Wi-Fi
    passphrase), by DHCP, and let this Mac scan over Wi-Fi."""
    device.write_text(
        WIFI_JOIN,
        FIELD_SEPARATOR.join(
            (network.security, network.encryption, network.name, password)
        ),
    )
    log.info("the scanner is joining a Wi-Fi network")  # neither name nor password
    wifi = wifi_of(device)
    if wifi is None or not wifi.allowed_here:
        allow_here(device)


MODEL_NAME, FIRMWARE_VERSION = "scanner-model", "firmware-version"  # the backend's


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
}
STATUS_STATE = {
    saneapi.DEVICE_BUSY: State.BUSY,
    saneapi.ACCESS_DENIED: State.REFUSED,
}


class Monitor:
    def __init__(
        self,
        connect: Callable[[], Device],
        scan: Callable[[str], Callable[[], Path]],
        poll: float = POLL_SECONDS,
        retry: float = RETRY_SECONDS,
    ):
        """connect opens the scanner (raising saneapi.SaneError, or
        sane.ScanError when it is not set up). scan takes the sheets in from
        the scanner of that SANE device name and returns what makes their
        PDF: a call that returns the PDF. Both raise sane.ScanError for
        problems the user can fix."""
        self._connect, self._scan = connect, scan
        self._device_name: str | None = None  # of the scanner last opened
        self._pdfs: queue.Queue[Callable[[], Path]] = queue.Queue()
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

    def run_on_scanner(self, job: Callable[[Device], T]) -> Future[T]:
        """Run job with the open scanner, on the thread that owns it; the
        future holds what job returns or raises (sane.ScanError when no
        scanner is open)."""
        future: Future[T] = Future()
        self._jobs.put((job, future))
        self._wake.set()
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
        device, last = None, None
        try:
            while not self._stopping.is_set():
                self._run_jobs(device)
                if self._requested.is_set():
                    self._requested.clear()
                    if device:
                        device.close()
                        device, last = None, None
                    self._run_scan()
                    continue
                if device is None:
                    try:
                        device = self._connect()
                    except saneapi.SaneError as e:
                        self._set(
                            state=STATUS_STATE.get(e.status, State.OFFLINE),
                            message="",
                            via=None,
                            wifi=None,
                            identity=None,
                        )
                        log.debug("scanner not available: %s", e)
                        self._wait(self._retry)
                        continue
                    except sane.ScanError as e:
                        self._set(
                            state=error_state(e),
                            message=str(e),
                            via=None,
                            wifi=None,
                            identity=None,
                        )
                        self._wait(self._retry)
                        continue
                    self._device_name = device.name
                    via = via_of(device.name)
                    log.info("connected over %s", via)
                    self._set(
                        via=via,
                        wifi=wifi_of(device) if via == Via.USB else None,
                        identity=identity_of(device),
                    )
                try:
                    values = device.read((SCAN_BUTTON, PAPER))
                except saneapi.SaneError as e:
                    log.info("scanner went away: %s", e)
                    device.close()
                    device, last = None, None
                    self._set(state=State.OFFLINE, via=None, wifi=None, identity=None)
                    continue
                self._observe(last, values)
                last = values
                self._wait(self._poll)
        finally:
            if device:
                device.close()

    def _run_jobs(self, device: Device | None) -> None:
        while True:
            try:
                job, future = self._jobs.get_nowait()
            except queue.Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue
            if device is None:
                future.set_exception(sane.ScanError("The scanner is not connected"))
                continue
            try:
                future.set_result(job(device))
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
        self._set(state=State.SCANNING, message="")
        try:
            make_pdf = self._scan(self._device_name)
        except sane.ScanError as e:
            self._set(state=State.READY, message=str(e))
        except Exception as e:  # shown, not lost with the thread
            log.exception("scan failed")
            self._set(state=State.READY, message=f"Scanning failed: {e}")
        else:
            with self._lock:
                self._snapshot = replace(
                    self._snapshot,
                    state=State.READY,  # until the next reading says otherwise
                    making=self._snapshot.making + 1,
                )
            self._pdfs.put(make_pdf)

    def _make_pdfs(self) -> None:
        """Make the scans' PDFs one after the other, as they come in."""
        while True:
            make_pdf = self._pdfs.get()
            try:
                message = f"Saved {make_pdf().name}"
            except sane.ScanError as e:
                message = str(e)
            except Exception as e:  # shown, not lost with the thread
                log.exception("making the PDF failed")
                message = f"Making the PDF failed: {e}"
            with self._lock:
                self._snapshot = replace(
                    self._snapshot, message=message, making=self._snapshot.making - 1
                )
