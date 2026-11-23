"""The monitor's decisions, with a scripted scanner and scan."""

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from openscansnap import monitor, sane, saneapi, scanner
from openscansnap.monitor import Monitor, State, Via

NO_PAPER = {monitor.SCAN_BUTTON: 0, monitor.PAPER: 0}
PAPER = {monitor.SCAN_BUTTON: 0, monitor.PAPER: 1}
PRESSED = {monitor.SCAN_BUTTON: 1, monitor.PAPER: 1}
SECONDS = 5
WIFI_DEVICE = sane.DEVICE_PREFIX + "10.0.0.2"
THIS_HOSTID = 0x00005E0053010000


def setUpModule():
    patcher = mock.patch.object(scanner, "this_hostid", return_value=THIS_HOSTID)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class Harness:
    """One scanner timeline across connections: each reading is taken in
    turn and the last one repeats; an exception in it is raised instead.
    refuse makes every connection fail. Scans are recorded; scan_result,
    if an exception, fails the scan itself, pdf_result the PDF after it (a
    list: each PDF's in turn); making a PDF waits for pdf_gate, and scanning
    for scan_gate."""

    def __init__(
        self,
        readings=(PAPER,),
        refuse=None,
        scan_result=None,
        pdf_result=Path("scan.pdf"),
    ):
        self.readings = list(readings)
        self.refuse = refuse
        self.opened: list[Connection] = []
        self.scans: list[list[bool]] = []  # whether each connection was closed
        self.scanned_from: list[str] = []  # the device name of each scan
        self.device_name = WIFI_DEVICE
        self.paired = ""  # the computers allowed on Wi-Fi, as the backend gives them
        self.network = "home"  # the Wi-Fi network the scanner is set up for
        self.firmware = "0U00"  # the scanner's firmware version
        self.joined = []  # the join requests, as the backend gets them
        self.scan_result = scan_result
        self.pdf_result = pdf_result
        self.pdf_gate = threading.Event()
        self.pdf_gate.set()
        self.scan_gate = threading.Event()
        self.scan_gate.set()
        self.scanning = threading.Event()  # set once a scan began

    def connect(self):
        if self.refuse:
            raise self.refuse
        connection = Connection(self)
        self.opened.append(connection)
        return connection

    def next_reading(self):
        reading = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if isinstance(reading, Exception):
            raise reading
        return reading

    def scan(self, device_name):
        self.scanned_from.append(device_name)
        self.scans.append([connection.closed for connection in self.opened])
        self.scanning.set()
        self.scan_gate.wait()
        if isinstance(self.scan_result, Exception):
            raise self.scan_result
        return self.make_pdf

    def make_pdf(self, progress):
        progress(1, 1)
        self.pdf_gate.wait()
        result = self.pdf_result
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Connection:
    unreadable = ()  # options whose reading fails

    def __init__(self, harness: Harness):
        self.harness = harness
        self.name = harness.device_name
        self.closed = False

    def read(self, names):
        reading = self.harness.next_reading()
        return {name: reading[name] for name in names}

    def has(self, name):
        return name in (
            monitor.PAIRED_HOSTIDS,
            monitor.PAIR_HOSTID,
            monitor.WIFI_NETWORKS,
            monitor.WIFI_CURRENT,
            monitor.WIFI_JOIN,
            monitor.MODEL_NAME,
            monitor.FIRMWARE_VERSION,
        )

    def read_text(self, name):
        if name in self.unreadable:
            raise saneapi.SaneError(saneapi.IO_ERROR, f"cannot read {name}")
        if name == monitor.PAIRED_HOSTIDS:
            return self.harness.paired
        if name == monitor.MODEL_NAME:
            return "ScanSnap iX500"
        if name == monitor.FIRMWARE_VERSION:
            return self.harness.firmware
        assert name == monitor.WIFI_CURRENT
        return f"1\t10.0.0.2\t{self.harness.network}"

    def write_text(self, name, text):
        if name == monitor.PAIR_HOSTID:
            self.harness.paired = text  # the scanner forgets every other one
            return
        assert name == monitor.WIFI_JOIN
        self.harness.joined.append(text)
        self.harness.network = text.split("\t")[2]

    def close(self):
        self.closed = True


class MonitorTest(unittest.TestCase):
    def watch(self, harness: Harness) -> Monitor:
        watcher = Monitor(harness.connect, harness.scan, poll=0.005, retry=0.005)
        watcher.start()
        self.addCleanup(watcher.stop, SECONDS)
        return watcher

    def wait_for(self, watcher: Monitor, done):
        deadline = time.monotonic() + SECONDS
        while not done(watcher.snapshot):
            self.assertLess(time.monotonic(), deadline, watcher.snapshot)
            time.sleep(0.002)
        return watcher.snapshot

    def test_paper_sensor_sets_the_state(self):
        self.wait_for(self.watch(Harness([PAPER])), lambda s: s.state == State.PAPER)
        self.wait_for(self.watch(Harness([NO_PAPER])), lambda s: s.state == State.READY)

    def test_how_the_scanner_is_connected_is_shown(self):
        for device_name, via in ((WIFI_DEVICE, Via.WIFI), (sane.USB_DEVICE, Via.USB)):
            with self.subTest(via=via):
                harness = Harness([PAPER])
                harness.device_name = device_name
                watcher = self.watch(harness)
                done = self.wait_for(watcher, lambda s: s.state == State.PAPER)
                self.assertEqual(done.via, via)

    def test_wi_fi_settings_are_read_over_usb(self):
        here = f"{THIS_HOSTID:016x}"
        for paired, allowed_here, others in (
            ("", False, False),
            (here, True, False),
            (f"0011223344550000,{here}", True, True),
        ):
            with self.subTest(paired=paired):
                harness = Harness([PAPER])
                harness.device_name, harness.paired = sane.USB_DEVICE, paired
                watcher = self.watch(harness)
                done = self.wait_for(watcher, lambda s: s.state == State.PAPER)
                self.assertEqual(done.wifi, monitor.Wifi("home", allowed_here, others))

    def test_older_firmware_is_noticed(self):
        for version, newer in (("0N00", True), ("0U00", False)):
            with self.subTest(version=version):
                harness = Harness([PAPER])
                harness.firmware = version
                watcher = self.watch(harness)
                done = self.wait_for(watcher, lambda s: s.state == State.PAPER)
                self.assertEqual(done.identity.firmware, version)
                self.assertEqual(done.identity.newer is not None, newer)
                self.assertIsNotNone(done.identity.page)

    def test_no_wi_fi_settings_over_wi_fi(self):
        watcher = self.watch(Harness([PAPER]))
        self.assertIsNone(self.wait_for(watcher, lambda s: s.state == State.PAPER).wifi)

    def test_wi_fi_set_up_joins_and_allows_this_mac(self):
        harness = Harness([PAPER])
        harness.device_name, harness.paired = sane.USB_DEVICE, "0011223344550000"
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.wifi is not None)
        network = monitor.Network("office", "WPA2", "AES", -40)

        def set_up(device):
            monitor.set_up_wifi(device, network, "pass phrase", others_warned=True)
            watcher.refresh_wifi(device)

        watcher.run_on_scanner(set_up).result(SECONDS)
        self.assertEqual(harness.joined, ["WPA2\tAES\toffice\tpass phrase"])
        self.assertEqual(harness.paired, f"{THIS_HOSTID:016x}")
        self.assertEqual(watcher.snapshot.wifi, monitor.Wifi("office", True, False))

    def test_wi_fi_set_up_leaves_the_computers_when_this_mac_is_one(self):
        harness = Harness([PAPER])
        both = f"0011223344550000,{THIS_HOSTID:016x}"
        harness.device_name, harness.paired = sane.USB_DEVICE, both
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.wifi is not None)
        network = monitor.Network("office", "WPA2", "AES", -40)
        watcher.run_on_scanner(
            lambda device: monitor.set_up_wifi(
                device, network, "pass phrase", others_warned=False
            )
        ).result(SECONDS)
        self.assertEqual(harness.paired, both)  # the other computer kept

    def test_wi_fi_set_up_changes_nothing_unless_told_what_it_forgets(self):
        network = monitor.Network("office", "WPA2", "AES", -40)
        other = "0011223344550000"
        for unreadable, others_warned in (
            ((), False),  # another computer is allowed: not told
            ((monitor.PAIRED_HOSTIDS,), True),  # who is allowed: not known
        ):
            with self.subTest(unreadable=unreadable):
                harness = Harness([PAPER])
                harness.device_name, harness.paired = sane.USB_DEVICE, other
                with mock.patch.object(Connection, "unreadable", unreadable):
                    watcher = self.watch(harness)
                    self.wait_for(watcher, lambda s: s.wifi is not None)
                    future = watcher.run_on_scanner(
                        lambda device, warned=others_warned: monitor.set_up_wifi(
                            device, network, "pass phrase", others_warned=warned
                        )
                    )
                    with self.assertRaisesRegex(sane.ScanError, "nothing was changed"):
                        future.result(SECONDS)
                self.assertEqual(harness.joined, [])
                self.assertEqual(harness.paired, other)

    def test_wi_fi_set_up_sends_no_password_that_does_not_fit(self):
        harness = Harness([PAPER])
        harness.device_name = sane.USB_DEVICE
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.wifi is not None)
        network = monitor.Network("office", "WPA2", "AES", -40)
        future = watcher.run_on_scanner(
            lambda device: monitor.set_up_wifi(
                device, network, "short", others_warned=True
            )
        )
        with self.assertRaisesRegex(sane.ScanError, "8 to 63"):
            future.result(SECONDS)
        self.assertEqual(harness.joined, [])

    def test_passwords_that_networks_take(self):
        wpa = monitor.Network("n", "WPA2", "AES", 0)
        wep = monitor.Network("n", "WEP", "WEP", 0)
        open_network = monitor.Network("n", "OPEN", "NONE", 0)
        for network, password, fits in (
            (open_network, "", True),
            (wpa, "eight ch", True),
            (wpa, "x" * 63, True),
            (wpa, "f" * 64, True),  # hexadecimal
            (wpa, "seven c", False),
            (wpa, "g" * 64, False),  # 64, not hexadecimal
            (wpa, "caf\u00e9 au lait", False),  # not ASCII
            (wep, "abcde", True),
            (wep, "0123456789", True),
            (wep, "abcdefghijklm", True),  # 13
            (wep, "0123456789abcdef0123456789", True),  # 26 hexadecimal
            (wep, "abcdef", False),
            (wep, "abcdefghijkl", False),  # 12
            (wep, "abcdefghij", False),  # 10, not hexadecimal
            (wep, "g" * 26, False),  # 26, not hexadecimal
        ):
            with self.subTest(security=network.security, password=password):
                problem = monitor.passphrase_problem(network, password)
                self.assertEqual(problem is None, fits, problem)

    def test_networks_strongest_first_each_once(self):
        class Listing:
            def read_text(self, name):
                return (
                    "WPA2\tAES\t-70\tfar\n"
                    "WPA2\tAES\t-30\tnear\n"
                    "WPA2\tAES\t-50\tfar\n"
                    "OPEN\tNONE\t-20\tfar\n"  # the same name, another network
                    "WPA2\tAES\tloud\tbroken\n"
                    "broken line\n"
                )

        self.assertEqual(
            [(n.name, n.open, n.signal) for n in monitor.networks(Listing())],
            [("far", True, -20), ("near", False, -30), ("far", False, -50)],
        )

    def test_names_that_are_not_utf_8_are_shown_and_sent_back_unchanged(self):
        ssid = b"caf\xe9".decode(saneapi.TEXT_ENCODING, saneapi.TEXT_ERRORS)
        network = monitor.Network(ssid, "WPA2", "AES", 0)
        self.assertEqual(network.name, "caf\ufffd")
        self.assertEqual(
            network.ssid.encode(saneapi.TEXT_ENCODING, saneapi.TEXT_ERRORS),
            b"caf\xe9",
        )

    def test_unexpected_failure_leaves_the_scanner_watched(self):
        harness = Harness([PAPER])
        failures = [RuntimeError("boom")]

        def connect():
            if failures:
                raise failures.pop()
            return harness.connect()

        watcher = Monitor(connect, harness.scan, poll=0.005, retry=0.005)
        with self.assertLogs("openscansnap.monitor", "ERROR"):
            watcher.start()
            self.addCleanup(watcher.stop, SECONDS)
            self.wait_for(watcher, lambda s: s.state == State.UNUSABLE)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)

    def test_job_fails_when_watching_the_scanner_fails(self):
        def connect():
            raise RuntimeError("boom")

        watcher = Monitor(connect, lambda name: None, poll=0.005, retry=0.005)
        with self.assertLogs("openscansnap.monitor", "ERROR"):
            watcher.start()
            self.addCleanup(watcher.stop, SECONDS)
            with self.assertRaises(sane.ScanError):
                watcher.run_on_scanner(lambda d: "ran").result(SECONDS)

    def test_job_that_fails_hands_its_failure_back(self):
        watcher = self.watch(Harness([PAPER]))
        self.wait_for(watcher, lambda s: s.state == State.PAPER)

        def fail(_device):
            raise saneapi.SaneError(saneapi.IO_ERROR, "refused")

        with self.assertRaisesRegex(saneapi.SaneError, "refused"):
            watcher.run_on_scanner(fail).result(SECONDS)
        self.assertEqual(watcher.run_on_scanner(lambda d: "ran").result(SECONDS), "ran")

    def test_job_asked_for_during_a_scan_runs_after_it(self):
        harness = Harness([PAPER])
        harness.scan_gate.clear()
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.request_scan()
        self.assertTrue(harness.scanning.wait(SECONDS))
        future = watcher.run_on_scanner(lambda d: "ran")
        harness.scan_gate.set()
        self.assertEqual(future.result(SECONDS), "ran")

    def test_job_without_a_scanner_fails_plainly(self):
        watcher = self.watch(Harness(refuse=saneapi.SaneError(saneapi.IO_ERROR, "off")))
        self.wait_for(watcher, lambda s: s.state == State.OFFLINE)
        with self.assertRaisesRegex(sane.ScanError, "not connected"):
            watcher.run_on_scanner(lambda device: None).result(SECONDS)

    def test_not_connected_shows_no_connection(self):
        watcher = self.watch(Harness(refuse=saneapi.SaneError(saneapi.IO_ERROR, "off")))
        done = self.wait_for(watcher, lambda s: s.state == State.OFFLINE)
        self.assertIsNone(done.via)

    def test_scan_uses_the_scanner_connected(self):
        harness = Harness([PAPER])
        harness.device_name = sane.USB_DEVICE
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.request_scan()
        self.wait_for(watcher, lambda s: harness.scans)
        self.assertEqual(harness.scanned_from, [sane.USB_DEVICE])

    def test_a_press_scans_once_with_the_scanner_closed(self):
        harness = Harness([PAPER, PRESSED, PRESSED, PRESSED, PAPER])
        watcher = self.watch(harness)
        # ready again once the scanner is read after the scan (READY until then)
        self.wait_for(
            watcher, lambda s: s.message == "Saved scan.pdf" and s.state == State.PAPER
        )
        time.sleep(0.05)
        self.assertEqual(len(harness.scans), 1)  # held down, still one scan
        self.assertEqual(harness.scans[0], [True])  # closed before scanning

    def test_pressed_at_connection_does_not_scan(self):
        # the first reading has nothing to compare with
        harness = Harness([PRESSED])
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        time.sleep(0.05)
        self.assertEqual(harness.scans, [])

    def test_request_scans_and_reports_failures(self):
        harness = Harness(
            [PAPER], scan_result=sane.ScanError("No paper in the scanner")
        )
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.request_scan()
        self.wait_for(watcher, lambda s: s.message == "No paper in the scanner")

    def test_pdf_failure_is_reported(self):
        harness = Harness([PAPER], pdf_result=sane.ScanError("Every side was blank"))
        watcher = self.watch(harness)
        watcher.request_scan()
        done = self.wait_for(watcher, lambda s: s.message == "Every side was blank")
        self.assertEqual(done.making, 0)

    def test_pdf_failure_stays_told_after_the_next_pdf(self):
        harness = Harness(
            [PAPER],
            pdf_result=[
                sane.ScanError("Cannot save"),
                Path("second.pdf"),
                Path("third.pdf"),
            ],
        )
        harness.pdf_gate.clear()
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.request_scan()
        self.wait_for(watcher, lambda s: s.making == 1)
        watcher.request_scan()
        self.wait_for(watcher, lambda s: s.making == 2)
        with self.assertLogs("openscansnap.monitor", "WARNING"):
            harness.pdf_gate.set()
            done = self.wait_for(watcher, lambda s: s.making == 0)
        self.assertEqual(done.failure, "Cannot save")
        self.assertEqual(done.message, "Saved second.pdf")
        watcher.request_scan()  # the next scan starts afresh
        self.wait_for(watcher, lambda s: not s.failure)

    def test_the_pdf_saved_last_is_told_and_outlives_a_failure(self):
        harness = Harness(
            [PAPER], pdf_result=[Path("first.pdf"), sane.ScanError("Cannot save")]
        )
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.request_scan()
        self.wait_for(watcher, lambda s: s.saved == Path("first.pdf") and s.making == 0)
        with self.assertLogs("openscansnap.monitor", "WARNING"):
            watcher.request_scan()
            done = self.wait_for(
                watcher, lambda s: s.failure == "Cannot save" and s.making == 0
            )
        self.assertEqual(done.saved, Path("first.pdf"))

    def test_a_queued_pdf_hands_back_what_it_made(self):
        watcher = self.watch(Harness([PAPER]))
        made = watcher.queue_pdf(lambda progress: Path("again.pdf"))
        self.assertEqual(made.result(SECONDS), Path("again.pdf"))
        self.wait_for(watcher, lambda s: s.saved == Path("again.pdf") and s.making == 0)

        def fail(progress):
            raise sane.ScanError("Every page is left out")

        with self.assertLogs("openscansnap.monitor", "WARNING"):
            failed = watcher.queue_pdf(fail)
            with self.assertRaisesRegex(sane.ScanError, "left out"):
                failed.result(SECONDS)

    def test_a_pdf_cancelled_while_waiting_is_skipped(self):
        harness = Harness([PAPER])
        harness.pdf_gate.clear()
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.request_scan()  # its PDF waits at the gate
        self.wait_for(watcher, lambda s: s.making == 1)
        cancelled = watcher.queue_pdf(lambda progress: Path("never.pdf"))
        self.assertTrue(cancelled.cancel())
        harness.pdf_gate.set()
        after = watcher.queue_pdf(lambda progress: Path("after.pdf"))
        self.assertEqual(after.result(SECONDS), Path("after.pdf"))  # thread alive
        self.wait_for(watcher, lambda s: s.making == 0)

    def test_the_sheet_being_made_is_told(self):
        harness = Harness([PAPER])
        harness.pdf_gate.clear()
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.request_scan()
        self.wait_for(watcher, lambda s: s.progress == (1, 1))
        harness.pdf_gate.set()
        self.wait_for(watcher, lambda s: s.making == 0 and s.progress is None)

    def test_next_scan_does_not_wait_for_the_pdf(self):
        harness = Harness([PAPER])
        harness.pdf_gate.clear()  # the first PDF takes long
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.request_scan()
        self.wait_for(watcher, lambda s: s.making == 1)
        watcher.request_scan()  # while the first PDF is still being made
        self.wait_for(watcher, lambda s: s.making == 2)
        self.assertEqual(len(harness.scans), 2)
        harness.pdf_gate.set()
        done = self.wait_for(watcher, lambda s: s.making == 0)
        self.assertEqual(done.message, "Saved scan.pdf")

    def test_a_scans_pdf_is_counted_before_its_scanning_ends(self):
        # no snapshot between shows the app idle: the menu would offer to
        # review the scan this one replaces
        harness = Harness([PAPER])
        harness.pdf_gate.clear()  # the PDF waits
        seen = []
        watcher = self.watch(harness)
        set_snapshot = watcher._set

        def recorded(**changes):
            set_snapshot(**changes)
            seen.append(watcher.snapshot)

        watcher._set = recorded
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.request_scan()
        self.wait_for(watcher, lambda s: s.making == 1 and s.state != State.SCANNING)
        began = next(i for i, s in enumerate(seen) if s.state == State.SCANNING)
        self.assertTrue(
            all(s.state == State.SCANNING or s.making for s in seen[began:])
        )
        harness.pdf_gate.set()

    def test_unexpected_scan_failure_is_shown(self):
        harness = Harness([PAPER], scan_result=RuntimeError("boom"))
        watcher = self.watch(harness)
        with self.assertLogs("openscansnap.monitor", "ERROR"):
            watcher.request_scan()
            self.wait_for(watcher, lambda s: s.message == "Scanning failed: boom")

    def test_new_paper_is_counted_once(self):
        harness = Harness([NO_PAPER, PAPER, PAPER, NO_PAPER, NO_PAPER])
        watcher = self.watch(harness)
        done = self.wait_for(
            watcher, lambda s: s.paper_loads == 1 and s.state == State.READY
        )
        self.assertEqual(done.paper_loads, 1)

    def test_scanner_going_away_and_coming_back(self):
        harness = Harness([PAPER, saneapi.SaneError(saneapi.IO_ERROR, "gone"), PAPER])
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: len(harness.opened) == 2)
        self.assertTrue(harness.opened[0].closed)
        self.wait_for(watcher, lambda s: s.state == State.PAPER)

    def test_scanner_switched_on_later_is_found(self):
        harness = Harness([PAPER], refuse=saneapi.SaneError(saneapi.IO_ERROR, "off"))
        watcher = self.watch(harness)
        self.wait_for(watcher, lambda s: s.state == State.OFFLINE)
        harness.refuse = None  # the cover is opened
        self.wait_for(watcher, lambda s: s.state == State.PAPER)

    def test_unavailable_scanner_states(self):
        for status, state in (
            (saneapi.DEVICE_BUSY, State.BUSY),
            (saneapi.ACCESS_DENIED, State.REFUSED),
            (saneapi.IO_ERROR, State.OFFLINE),
        ):
            with self.subTest(state=state):
                watcher = self.watch(Harness(refuse=saneapi.SaneError(status, "no")))
                self.wait_for(watcher, lambda s, state=state: s.state == state)

    def test_failures_to_reach_the_scanner_have_their_states(self):
        for error, state in (
            (sane.NotFound("No scanner found"), State.NOT_SET_UP),
            (sane.NotSetUp("No scanner set up"), State.NOT_SET_UP),
            (
                sane.BadConfiguration("fujitsu.conf: no wifi line"),
                State.BAD_CONFIGURATION,
            ),
            (sane.Refused("refused"), State.REFUSED),
            (sane.ScanError("Cannot load SANE: image not found"), State.UNUSABLE),
        ):
            with self.subTest(error=type(error).__name__):
                watcher = self.watch(Harness(refuse=error))
                done = self.wait_for(watcher, lambda s, state=state: s.state == state)
                self.assertEqual(done.message, str(error))

    def test_stop_closes_the_scanner(self):
        harness = Harness([PAPER])
        watcher = Monitor(harness.connect, harness.scan, poll=0.005, retry=0.005)
        watcher.start()
        self.wait_for(watcher, lambda s: s.state == State.PAPER)
        watcher.stop(SECONDS)
        self.assertTrue(harness.opened[-1].closed)


class ConnectorTest(unittest.TestCase):
    """Opening the scanner: on USB when it is there; else on Wi-Fi, set up
    when none is, followed when it does not answer, libsane started afresh
    when the configuration changed."""

    HERE = sane.Scanner("10.0.0.2", 1, None, "aa:aa:aa:aa:aa:aa")
    MOVED = sane.Scanner("10.0.0.9", 1, None, "aa:aa:aa:aa:aa:aa")

    def setUp(self):
        self.libraries = []  # each libsane started, in order
        self.unreachable = set()  # hosts that do not answer
        self.refusing = set()  # hosts that answer, refusing us
        self.on_usb = False  # a scanner on USB
        self.usb_status = saneapi.INVAL  # opening USB with none there; INVAL: none

        test = self

        class Library:
            def __init__(self):
                self.opened, self.exited = [], False
                test.libraries.append(self)

            def open(self, name):
                if name == sane.USB_DEVICE:
                    self.opened.append(name)
                    if not test.on_usb:
                        raise saneapi.SaneError(test.usb_status, "no scanner on USB")
                    return name
                host = name.removeprefix(sane.DEVICE_PREFIX)
                self.opened.append(host)
                if host in test.unreachable:
                    raise saneapi.SaneError(saneapi.IO_ERROR, "no answer")
                if host in test.refusing:
                    raise saneapi.SaneError(saneapi.ACCESS_DENIED, "refused")
                return host

            def exit(self):
                self.exited = True

        for patcher in (
            mock.patch.object(saneapi, "Library", Library),
            mock.patch.object(scanner, "set_up", return_value=self.HERE),
            mock.patch.object(scanner, "follow", return_value=self.MOVED),
            mock.patch.object(scanner, "learn_mac", return_value=self.HERE),
            mock.patch.object(scanner, "ensure_config"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.wifi = monitor.Connector()

    def configured(self, value):
        patcher = mock.patch.object(sane, "configured", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_scanner_on_usb_that_cannot_be_used_is_not_passed_over(self):
        self.configured(self.HERE)
        for status in (saneapi.DEVICE_BUSY, saneapi.IO_ERROR):
            with self.subTest(status=status):
                self.usb_status = status
                with self.assertRaises(saneapi.SaneError) as caught:
                    self.wifi()
                self.assertEqual(caught.exception.status, status)
                opened = {name for library in self.libraries for name in library.opened}
                self.assertEqual(opened, {sane.USB_DEVICE})  # no Wi-Fi scanner
                scanner.set_up.assert_not_called()
                scanner.follow.assert_not_called()

    def test_scanner_on_usb_comes_first(self):
        self.on_usb = True
        self.configured(self.HERE)
        self.assertEqual(self.wifi(), sane.USB_DEVICE)
        self.assertEqual(self.libraries[0].opened, [sane.USB_DEVICE])  # no Wi-Fi
        scanner.ensure_config.assert_called_once()

    def test_scanner_on_usb_needs_no_wi_fi_set_up(self):
        self.on_usb = True
        self.configured(None)
        self.assertEqual(self.wifi(), sane.USB_DEVICE)
        scanner.set_up.assert_not_called()

    def test_without_usb_the_wi_fi_scanner_is_opened(self):
        self.configured(self.HERE)
        self.assertEqual(self.wifi(), self.HERE.host)
        self.assertEqual(self.libraries[0].opened, [sane.USB_DEVICE, self.HERE.host])

    def test_set_up_when_none_is(self):
        self.configured(None)
        self.assertEqual(self.wifi(), self.HERE.host)
        scanner.set_up.assert_called_once()

    def test_configured_scanner_is_opened_and_libsane_kept(self):
        self.configured(self.HERE)
        self.wifi()
        self.wifi()
        self.assertEqual(len(self.libraries), 1)
        scanner.set_up.assert_not_called()
        scanner.follow.assert_not_called()

    def test_scanner_that_does_not_answer_is_followed(self):
        self.configured(self.HERE)
        self.unreachable.add(self.HERE.host)
        self.assertEqual(self.wifi(), self.MOVED.host)
        # the new address is a new configuration: libsane read the old one
        self.assertTrue(self.libraries[0].exited)
        self.assertEqual(self.libraries[1].opened, [self.MOVED.host])

    def test_scanner_that_answers_is_not_searched_for(self):
        self.configured(self.HERE)
        self.refusing.add(self.HERE.host)
        with self.assertRaises(saneapi.SaneError):
            self.wifi()
        scanner.follow.assert_not_called()

    def test_scanner_found_nowhere_else_stays_unreachable(self):
        self.configured(self.HERE)
        self.unreachable.add(self.HERE.host)
        scanner.follow.return_value = None
        with self.assertRaises(saneapi.SaneError):
            self.wifi()

    def test_hardware_address_is_learned_before_opening(self):
        # learned first: the Wi-Fi scanner is opened with it configured
        self.configured(sane.Scanner("10.0.0.2", 1))
        self.wifi()
        scanner.learn_mac.assert_called_once()
        wifi_opens = [lib for lib in self.libraries if self.HERE.host in lib.opened]
        self.assertEqual(len(wifi_opens), 1)
        scanner.learn_mac.reset_mock()
        sane.configured.return_value = self.HERE  # it was saved
        self.wifi()
        scanner.learn_mac.assert_not_called()

    def test_damaged_configuration_is_not_set_up_again(self):
        patcher = mock.patch.object(
            sane, "configured", side_effect=sane.ScanError("fujitsu.conf: no wifi line")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaisesRegex(sane.ScanError, "no wifi line"):
            self.wifi()
        scanner.set_up.assert_not_called()

    def test_sane_that_cannot_load_is_a_plain_error(self):
        self.configured(self.HERE)
        with (
            mock.patch.object(
                saneapi, "Library", side_effect=OSError("image not found")
            ),
            self.assertRaisesRegex(sane.ScanError, "Cannot load SANE"),
        ):
            self.wifi()

    def test_new_password_starts_libsane_afresh(self):
        self.configured(self.HERE)
        self.wifi()
        sane.configured.return_value = sane.Scanner(
            "10.0.0.2", 1, "pass", "aa:aa:aa:aa:aa:aa"
        )
        self.wifi()
        self.assertEqual(len(self.libraries), 2)
        self.assertTrue(self.libraries[0].exited)


if __name__ == "__main__":
    unittest.main()
