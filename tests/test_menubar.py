"""What the menu bar shows for each scanner state."""

import importlib.util
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from offscreen import Offscreen

from openscansnap import job, options
from openscansnap.monitor import Snapshot, State, Via
from openscansnap.options import Quality, ScanMode, ShowThrough

APPKIT = importlib.util.find_spec("AppKit") is not None


def setUpModule():
    if APPKIT:  # drawing with AppKit would otherwise put Python in the Dock
        from AppKit import NSApplication, NSApplicationActivationPolicyProhibited

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyProhibited
        )


@unittest.skipUnless(APPKIT, "needs PyObjC (macOS)")
class MenuBarTest(Offscreen):
    def setUp(self):
        super().setUp()
        from openscansnap import menubar

        self.menubar = menubar

    def test_every_state_has_a_status_line(self):
        for state in State:
            with self.subTest(state=state):
                status, _ = self.menubar.describe(Snapshot(state=state))
                self.assertTrue(status)

    def test_detail_is_the_last_result_or_what_to_do(self):
        describe = self.menubar.describe
        self.assertEqual(
            describe(Snapshot(state=State.PAPER, message="Saved scan.pdf")),
            ("Ready, paper loaded", "Saved scan.pdf"),
        )
        self.assertIn("ScanSnap Home", describe(Snapshot(state=State.BUSY))[1])
        self.assertEqual(describe(Snapshot(state=State.READY))[1], "")

    def test_a_problem_comes_with_what_to_do_in_the_app(self):
        detail = self.menubar.describe(
            Snapshot(
                state=State.NOT_SET_UP,
                message="No scanner found on this Mac's networks",
            )
        )[1]
        self.assertTrue(
            detail.startswith("No scanner found on this Mac's networks. Turn")
        )
        self.assertIn("Local Network", detail)
        self.assertNotIn("python -m", detail)  # the command line's remedy
        detail = self.menubar.describe(
            Snapshot(
                state=State.BAD_CONFIGURATION, message="/x/fujitsu.conf: no wifi line"
            )
        )[1]
        self.assertIn("Delete that file", detail)

    def test_pdfs_being_made_and_one_that_failed(self):
        describe = self.menubar.describe
        self.assertEqual(
            describe(Snapshot(state=State.PAPER, making=1)),
            ("Processing… Ready to scan again.", "Ready, paper loaded"),
        )
        self.assertEqual(
            describe(Snapshot(state=State.READY, making=1, progress=(3, 13)))[0],
            "Processing 3/13… Ready to scan again.",
        )
        # a failed PDF is told while others are made, and after the next one
        for snapshot in (
            Snapshot(state=State.PAPER, making=1, failure="Cannot save"),
            Snapshot(state=State.PAPER, message="Saved b.pdf", failure="Cannot save"),
        ):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(describe(snapshot)[1], "Cannot save")

    def test_scans_folder_is_shown_from_home_and_cut_short_in_the_middle(self):
        home = Path.home()
        title = self.menubar.folder_title
        self.assertEqual(title(home / "Downloads"), "~/Downloads")
        self.assertEqual(title(Path("/Volumes/Scans")), "/Volumes/Scans")
        long = home / "src" / "some-rather-long-project-name" / "scans" / "receipts"
        shown = title(long)
        self.assertEqual(len(shown), self.menubar.FOLDER_CHARS)
        self.assertTrue(shown.startswith("~/src/some-"))
        self.assertTrue(shown.endswith("…/receipts"))

    def test_a_scanner_that_cannot_be_used_says_how_to_fix_it_first(self):
        for state in (State.REFUSED, State.OFFLINE):
            with self.subTest(state=state):
                detail = self.menubar.describe(
                    Snapshot(state=state, failure="Cannot save")
                )[1]
                self.assertIn(self.menubar.HINT[state], detail)

    def quitting_bar(self, snapshot: Snapshot, keeps_changes: bool = False):
        """A stand-in MenuBar running the real quit methods, with the scanner
        at snapshot (set bar.monitor.snapshot to move on)."""
        bar_class = self.menubar.MenuBar
        reviewing = mock.Mock()
        reviewing.has_changes.return_value = keeps_changes
        reviewing.discard_changes.return_value = False
        bar = SimpleNamespace(
            status_line=mock.Mock(),
            item=object(),
            window=mock.Mock(),
            review_window=reviewing,
            monitor=mock.Mock(snapshot=snapshot),
            quit_state=self.menubar.QuitState.RUNNING,
            reply_when_done=False,
        )
        bar.begin_quitting = lambda: bar_class.begin_quitting(bar)
        bar.finish_quitting = lambda s: bar_class.finish_quitting(bar, s)
        bar.refresh_ = lambda timer: bar_class.refresh_(bar, timer)
        bar.quit_ = lambda sender: bar_class.quit_(bar, sender)
        bar.ask = lambda: bar_class.applicationShouldTerminate_(bar, None)
        return bar

    def quitting(self):
        """AppKit's application and status bar, mocked: the application."""
        app = mock.patch.object(self.menubar, "NSApplication")
        status_bar = mock.patch.object(self.menubar, "NSStatusBar")
        self.addCleanup(app.stop)
        self.addCleanup(status_bar.stop)
        status_bar.start()
        return app.start().sharedApplication.return_value

    def test_quitting_waits_for_the_scan_in_progress(self):
        app = self.quitting()
        bar = self.quitting_bar(Snapshot(state=State.SCANNING))
        bar.quit_(None)
        bar.status_line.setTitle_.assert_called_with("Finishing 1 PDF, then quitting…")
        bar.monitor.snapshot = Snapshot(state=State.SCANNING, making=1)
        bar.refresh_(None)
        bar.status_line.setTitle_.assert_called_with("Finishing 2 PDFs, then quitting…")
        app.terminate_.assert_not_called()
        bar.monitor.snapshot = Snapshot(state=State.READY)
        bar.refresh_(None)
        app.terminate_.assert_called_once()
        self.assertEqual(bar.ask(), self.menubar.NSTerminateNow)  # terminate_ asks

    def test_an_idle_quit_from_outside_is_answered_once(self):
        # logout, an Apple event, an update's relaunch, with nothing in
        # progress: the answer is "later", then exactly one reply
        app = self.quitting()
        bar = self.quitting_bar(Snapshot(state=State.READY))
        self.assertEqual(bar.ask(), self.menubar.NSTerminateLater)
        app.terminate_.assert_not_called()
        app.replyToApplicationShouldTerminate_.assert_not_called()
        bar.monitor.stop.assert_called_once()  # the scanner is let go
        bar.refresh_(None)
        bar.refresh_(None)  # a tick before the app ends: nothing more
        app.replyToApplicationShouldTerminate_.assert_called_once_with(True)
        app.terminate_.assert_not_called()

    def test_a_quit_from_outside_waits_for_the_pdfs(self):
        app = self.quitting()
        bar = self.quitting_bar(Snapshot(state=State.READY, making=1))
        self.assertEqual(bar.ask(), self.menubar.NSTerminateLater)
        bar.refresh_(None)
        app.replyToApplicationShouldTerminate_.assert_not_called()
        bar.monitor.snapshot = Snapshot(state=State.READY)
        bar.refresh_(None)
        app.replyToApplicationShouldTerminate_.assert_called_once_with(True)

    def test_a_quit_from_outside_during_the_menus_quit_is_answered(self):
        app = self.quitting()
        bar = self.quitting_bar(Snapshot(state=State.READY, making=1))
        bar.quit_(None)
        self.assertEqual(bar.ask(), self.menubar.NSTerminateLater)
        bar.monitor.snapshot = Snapshot(state=State.READY)
        bar.refresh_(None)
        app.replyToApplicationShouldTerminate_.assert_called_once_with(True)
        app.terminate_.assert_not_called()

    def test_quit_from_the_menu_while_quitting_goes_on_as_it_is(self):
        # asked once whether to drop changes, the scanner let go once
        app = self.quitting()
        bar = self.quitting_bar(Snapshot(state=State.READY, making=1))
        self.assertEqual(bar.ask(), self.menubar.NSTerminateLater)
        bar.quit_(None)
        bar.monitor.stop.assert_called_once()
        bar.review_window.has_changes.assert_called_once()
        bar.monitor.snapshot = Snapshot(state=State.READY)
        bar.refresh_(None)
        app.replyToApplicationShouldTerminate_.assert_called_once_with(True)
        app.terminate_.assert_not_called()

    def test_a_quit_from_outside_can_be_declined_for_changes_under_review(self):
        self.quitting()
        bar = self.quitting_bar(Snapshot(state=State.READY), keeps_changes=True)
        self.assertEqual(bar.ask(), self.menubar.NSTerminateCancel)
        self.assertEqual(bar.quit_state, self.menubar.QuitState.RUNNING)
        bar.monitor.stop.assert_not_called()

    def test_networks_of_one_name_are_told_apart(self):
        from openscansnap import monitor, wifiwindow

        found = [
            monitor.Network("home", "WPA2", "AES", 0),
            monitor.Network("home", "OPEN", "NONE", 0),
            monitor.Network("office", "WPA2", "AES", 0),
        ]
        self.assertEqual(
            [wifiwindow.title(network, found) for network in found],
            ["home (WPA2, AES)", "home (no password)", "office"],
        )

    def test_the_icon_shimmers_while_scanning_and_making_pdfs(self):
        working = self.menubar.working
        self.assertTrue(working(Snapshot(state=State.SCANNING)))
        self.assertTrue(working(Snapshot(state=State.PAPER, making=1)))
        self.assertFalse(working(Snapshot(state=State.PAPER)))

    def test_scanning_needs_paper(self):
        self.assertTrue(self.menubar.can_scan(Snapshot(state=State.PAPER)))
        for state in set(State) - {State.PAPER}:
            self.assertFalse(self.menubar.can_scan(Snapshot(state=state)))

    def test_icon_shows_how_the_scanner_is_connected_or_an_alert(self):
        Icon, Mark = self.menubar.Icon, self.menubar.Mark
        shown = [Icon(True, Mark.WIFI), Icon(True, Mark.USB), Icon(False, None)]
        shown += [Icon(True, Mark.ALERT), Icon(False, Mark.ALERT)]
        icons = {icon: self.menubar.status_icon(icon) for icon in shown}
        for icon in icons.values():
            self.assertTrue(icon.isTemplate())  # follows the light or dark menu bar
            self.assertGreater(icon.size().height, 0)
        described = {i: str(icons[i].accessibilityDescription()) for i in shown}
        self.assertIn("on Wi-Fi", described[Icon(True, Mark.WIFI)])
        self.assertIn("on USB", described[Icon(True, Mark.USB)])
        self.assertIn("attention", described[Icon(False, Mark.ALERT)])
        self.assertIn("scanner off", described[Icon(False, None)])
        # the letter stays put whichever mark is beside it
        self.assertEqual(len({icons[i].size().width for i in shown}), 1)

    def test_every_icon_the_scanner_can_call_for_is_made(self):
        made = set(self.menubar.ICONS)  # what MenuBar makes
        for state in State:
            for via in (*Via, None):
                for failure in ("", "Making the PDF failed"):
                    snapshot = Snapshot(state=state, via=via, failure=failure)
                    with self.subTest(snapshot=snapshot):
                        self.assertIn(self.menubar.icon_of(snapshot), made)

    def test_the_icon_follows_the_scanner(self):
        Icon, Mark = self.menubar.Icon, self.menubar.Mark
        icon_of = self.menubar.icon_of
        shown = {s: icon_of(Snapshot(state=s, via=Via.USB)) for s in State}
        self.assertEqual(
            {s for s, icon in shown.items() if icon == Icon(True, Mark.USB)},
            {State.READY, State.PAPER, State.SCANNING},
        )
        # an error the user must act on: an exclamation mark, not the cable,
        # and the letter dimmed: the scanner cannot be used
        self.assertEqual(
            {s for s, icon in shown.items() if icon == Icon(False, Mark.ALERT)},
            {
                State.NOT_SET_UP,
                State.BAD_CONFIGURATION,
                State.UNUSABLE,
                State.BUSY,
                State.REFUSED,
            },
        )
        self.assertEqual(shown[State.OFFLINE], Icon(False, None))  # off: no error
        failed = "Making the PDF failed"  # until the next scan
        self.assertEqual(
            icon_of(Snapshot(state=State.PAPER, via=Via.WIFI, failure=failed)),
            Icon(True, Mark.ALERT),
        )
        self.assertEqual(
            icon_of(Snapshot(state=State.OFFLINE, via=Via.WIFI, failure=failed)),
            Icon(False, Mark.ALERT),  # off as well: dimmed
        )

    def test_nothing_is_drawn_up_to_the_edges(self):
        # ink in the outermost pixels means the drawing was cut off there
        from AppKit import (
            NSBitmapImageRep,
            NSCalibratedRGBColorSpace,
            NSGraphicsContext,
            NSMakeRect,
        )

        scale = 2  # a Retina menu bar
        Icon, Mark = self.menubar.Icon, self.menubar.Mark
        for shown in (Icon(False, None), *(Icon(True, mark) for mark in Mark)):
            icon = self.menubar.status_icon(shown)
            width = round(icon.size().width * scale)
            height = round(icon.size().height * scale)
            rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                None, width, height, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
            )
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.setCurrentContext_(
                NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
            )
            icon.drawInRect_(NSMakeRect(0, 0, width, height))
            NSGraphicsContext.restoreGraphicsState()
            edges = [(x, 0) for x in range(width)] + [
                (x, height - 1) for x in range(width)
            ]
            edges += [(0, y) for y in range(height)] + [
                (width - 1, y) for y in range(height)
            ]
            with self.subTest(icon=shown):
                self.assertEqual(
                    max(rep.colorAtX_y_(x, y).alphaComponent() for x, y in edges), 0
                )
        self.assertLessEqual(icon.size().height, 18)  # Apple's menu bar limit

    def test_letter_is_drawn_whole(self):
        # the drawn S must be as wide as the font says its outline is; a
        # clipped belly makes it narrower
        import numpy as np
        from AppKit import (
            NSAttributedString,
            NSBitmapImageRep,
            NSCalibratedRGBColorSpace,
            NSColor,
            NSFont,
            NSFontAttributeName,
            NSGraphicsContext,
            NSMakeRect,
            NSMakeSize,
            NSRectFill,
            NSStringDrawingUsesDeviceMetrics,
        )

        menubar = self.menubar
        font = NSFont.fontWithName_size_(menubar.ICON_FONT, menubar.ICON_HEIGHT)
        letter = NSAttributedString.alloc().initWithString_attributes_(
            menubar.ICON_LETTER, {NSFontAttributeName: font}
        )
        outline = letter.boundingRectWithSize_options_context_(
            NSMakeSize(1000, 1000), NSStringDrawingUsesDeviceMetrics, None
        ).size
        scale, margin = 8, 8  # pixels per point; white around the icon
        icon = menubar.status_icon(menubar.Icon(True, menubar.Mark.WIFI))
        width = round(icon.size().width * scale) + 2 * margin
        height = round(icon.size().height * scale) + 2 * margin
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, width, height, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
        )
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(
            NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        )
        NSColor.whiteColor().set()
        NSRectFill(NSMakeRect(0, 0, width, height))
        icon.drawInRect_(
            NSMakeRect(margin, margin, width - 2 * margin, height - 2 * margin)
        )
        NSGraphicsContext.restoreGraphicsState()
        pixels = np.frombuffer(bytes(rep.bitmapData()), dtype=np.uint8)
        ink = pixels.reshape(height, rep.bytesPerRow())[:, : width * 4 : 4] < 128
        rows = np.nonzero(ink.any(axis=1))[0]
        letter_height = rows.max() - rows.min() + 1
        # the S is the first run of inked columns from the left
        columns = ink.any(axis=0)
        start = int(np.argmax(columns))
        end = start + int(np.argmin(columns[start:]))
        expected = letter_height * outline.width / outline.height
        self.assertGreater(end - start, 0.97 * expected)


@unittest.skipUnless(APPKIT, "needs PyObjC (macOS)")
class SettingsMenuTest(Offscreen):
    """The scan settings in the menu: what they show, and what choosing one
    saves."""

    def setUp(self):
        super().setUp()
        from AppKit import NSStatusBar

        from openscansnap import config, menubar

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.toml"
        load, save = config.load, config.save
        for name, redirected in (
            ("load", lambda path=self.path: load(path)),
            ("save", lambda name, value, path=self.path: save(name, value, path)),
        ):
            patcher = mock.patch.object(config, name, redirected)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(menubar, "open_at_login", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # never a real review window: tests run no event loop, so it would
        # only spin on the owner's screen
        patcher = mock.patch.object(menubar, "ReviewWindow")
        patcher.start()
        self.addCleanup(patcher.stop)
        from openscansnap import review

        self.watcher = mock.Mock(snapshot=Snapshot())
        self.store = review.Store(Path(tmp.name) / "review")
        self.bar = menubar.MenuBar.alloc().initWithMonitor_settings_store_(
            self.watcher, config.load(), self.store
        )
        self.addCleanup(NSStatusBar.systemStatusBar().removeStatusItem_, self.bar.item)
        self.config = config

    def on(self, item) -> bool:
        from AppKit import NSControlStateValueOn

        return item.state() == NSControlStateValueOn

    def saved(self, stem: str):
        """A new scan kept for review, and its PDF saved."""
        import test_review

        scan = test_review.keep(self.store, self.store.root.parent, stem)
        self.watcher.snapshot = Snapshot(saved=scan.pdf)
        self.bar.refresh_(None)
        return scan

    def test_a_new_scan_opens_for_review(self):
        self.assertFalse(self.bar.review_item.isEnabled())  # nothing to review
        with mock.patch.object(self.bar, "open_review") as opened:
            scan = self.saved("scan_1")
        self.assertTrue(self.bar.review_item.isEnabled())
        self.assertEqual(opened.call_args.args[0].name, scan.name)
        with mock.patch.object(self.bar, "open_review") as opened:
            self.bar.pdf_saved(scan.pdf)  # the same scan, made again: no new review
        opened.assert_not_called()

    def test_review_waits_from_a_new_scans_start_until_its_pdf_is_saved(self):
        with mock.patch.object(self.bar, "open_review"):
            first = self.saved("scan_1")
            for snapshot in (
                Snapshot(state=State.SCANNING, saved=first.pdf),
                Snapshot(state=State.READY, making=1, saved=first.pdf),
            ):
                with self.subTest(snapshot=snapshot):
                    self.watcher.snapshot = snapshot
                    self.bar.refresh_(None)
                    self.assertFalse(self.bar.review_item.isEnabled())
            self.saved("scan_2")
        self.assertTrue(self.bar.review_item.isEnabled())

    def test_a_scan_that_makes_no_pdf_leaves_the_last_one_to_review(self):
        with mock.patch.object(self.bar, "open_review"):
            first = self.saved("scan_1")
        self.watcher.snapshot = Snapshot(state=State.SCANNING, saved=first.pdf)
        self.bar.refresh_(None)
        self.watcher.snapshot = Snapshot(state=State.READY, saved=first.pdf)
        self.bar.refresh_(None)  # the scan failed before any PDF
        self.assertTrue(self.bar.review_item.isEnabled())

    def test_a_pdf_not_kept_leaves_nothing_to_review(self):
        self.saved("scan_1")
        with mock.patch.object(self.bar, "open_review") as opened:
            self.watcher.snapshot = Snapshot(saved=Path("/tmp/not-kept.pdf"))
            self.bar.refresh_(None)  # its keeping failed: the older scan stays out
        opened.assert_not_called()
        self.assertFalse(self.bar.review_item.isEnabled())

    def test_no_review_after_scans_when_switched_off(self):
        self.config.save("window.review_after_scan", False)
        self.bar.settings = self.config.load()
        with mock.patch.object(self.bar, "open_review") as opened:
            self.saved("scan_1")
        opened.assert_not_called()
        self.assertTrue(self.bar.review_item.isEnabled())  # still from the menu

    def test_changes_under_review_are_not_replaced_by_a_new_scan(self):
        self.bar.review_window = mock.Mock(has_changes=mock.Mock(return_value=True))
        with mock.patch.object(self.bar, "open_review") as opened:
            self.saved("scan_1")
        opened.assert_not_called()

    def test_the_apps_scans_are_kept_for_review(self):
        from openscansnap import menubar

        with (
            mock.patch.object(
                menubar.config, "load", return_value=mock.Mock(debug=False)
            ),
            mock.patch.object(menubar.job, "scan"),
            mock.patch.object(menubar.job, "finish", autospec=True) as finish,
        ):
            menubar.scan_with_settings("fujitsu:usb", self.store)(None)
        handed = inspect.signature(job.finish).bind(
            *finish.call_args.args, **finish.call_args.kwargs
        )
        self.assertEqual(handed.arguments["keep"], self.store.keep)

    def test_the_app_starts_though_the_review_store_cannot_be_cleared(self):
        from openscansnap import menubar

        store = mock.Mock()
        store.prune.side_effect = PermissionError(1, "Operation not permitted")
        with self.assertLogs("openscansnap.menubar", "ERROR"):
            menubar.clear_review(store)

    def test_review_last_scan_opens_the_latest_showing_the_setting(self):
        from openscansnap import menubar

        self.config.save("window.review_after_scan", False)
        self.bar.settings = self.config.load()
        scan = self.saved("scan_1")
        with mock.patch.object(menubar, "ReviewWindow") as kind:
            self.bar.reviewLastScan_(None)
        made = kind.alloc.return_value.initWithStore_monitor_setAfterScan_
        shown = made.return_value.show
        self.assertEqual(shown.call_args.args[0].name, scan.name)
        self.assertIs(shown.call_args.args[1], False)
        made.call_args.args[2](True)  # the window's checkbox
        self.assertTrue(self.config.load().review_after_scan)

    def test_the_menus_review_checkbox_and_the_windows_set_one_setting(self):
        self.assertTrue(self.on(self.bar.after_scan))  # on by default
        self.bar.review_window = mock.Mock()
        self.bar.toggleReviewAfterScan_(None)
        self.assertFalse(self.config.load().review_after_scan)
        self.assertFalse(self.on(self.bar.after_scan))
        self.bar.review_window.show_after_scan.assert_called_with(False)

    def opened(self, stem):
        """A new scan, opened in the review window, which holds it."""
        with mock.patch.object(self.bar, "open_review"):
            scan = self.saved(stem)
        self.store.hold(scan)
        self.bar.review_window = mock.Mock(scan=scan)
        return scan

    def remade(self, scan):
        """scan's PDF was made again from review."""
        self.watcher.snapshot = Snapshot(saved=scan.pdf)
        self.bar.refresh_(None)

    def test_an_older_scan_made_again_leaves_the_latest_to_review(self):
        first = self.opened("scan_1")
        with mock.patch.object(self.bar, "open_review"):
            self.saved("scan_2")  # the first stays, held by its window
        self.remade(first)
        self.assertTrue(self.bar.review_item.isEnabled())

    def test_a_scan_made_again_stands_in_for_no_newer_one(self):
        first = self.opened("scan_1")
        self.watcher.snapshot = Snapshot(saved=Path("/tmp/not-kept.pdf"))
        self.bar.refresh_(None)  # a newer scan, its keeping failed
        self.remade(first)
        self.assertFalse(self.bar.review_item.isEnabled())

    def test_an_older_scan_made_again_after_its_window_closed(self):
        first = self.opened("scan_1")
        with mock.patch.object(self.bar, "open_review"):
            self.saved("scan_2")
        # the window let it go before the menu saw its PDF saved
        self.store.release(first)
        self.bar.review_window = mock.Mock(scan=None)
        self.remade(first)
        self.assertTrue(self.bar.review_item.isEnabled())

    def test_the_windows_checkbox_sets_the_menus_too(self):
        from openscansnap import menubar

        self.saved("scan_1")  # opened for review
        made = (
            menubar.ReviewWindow.alloc.return_value.initWithStore_monitor_setAfterScan_
        )
        made.call_args.args[2](False)  # the window's checkbox, unticked
        self.assertFalse(self.on(self.bar.after_scan))
        with mock.patch.object(self.bar, "open_review") as opened:
            self.saved("scan_2")
        opened.assert_not_called()

    def test_about_shows_the_icon_the_version_and_the_project(self):
        from openscansnap import HOMEPAGE, __version__, menubar

        options = menubar.about_options()
        self.assertEqual(options["ApplicationName"], menubar.APP_NAME)
        self.assertEqual(options["ApplicationVersion"], __version__)
        self.assertEqual(options["Version"], "")  # no build number beside it
        self.assertIsNotNone(options["ApplicationIcon"])
        credits = options["Credits"]
        self.assertEqual(str(credits.string()), HOMEPAGE.removeprefix("https://"))
        self.assertEqual(
            str(credits.attribute_atIndex_effectiveRange_("NSLink", 0, None)[0]),
            HOMEPAGE,
        )

    def test_defaults_are_shown(self):
        self.assertEqual(
            [mode for mode, item in self.bar.modes.items() if self.on(item)],
            [ScanMode.AUTO],
        )
        self.assertEqual(
            [quality for quality, item in self.bar.qualities.items() if self.on(item)],
            [Quality.HIGH],
        )
        for step in self.steps():
            with self.subTest(step=step):
                self.assertTrue(
                    self.on(self.step(step)) and self.step(step).isEnabled()
                )

    def test_choosing_a_mode_saves_it_and_moves_the_mark(self):
        self.bar.chooseMode_(self.bar.modes[ScanMode.GRAY])
        self.assertEqual(self.config.load().options.mode, ScanMode.GRAY)
        self.assertEqual(
            [mode for mode, item in self.bar.modes.items() if self.on(item)],
            [ScanMode.GRAY],
        )

    def test_steps_a_mode_rules_out_show_off_and_cannot_change(self):
        self.bar.chooseMode_(self.bar.modes[ScanMode.PHOTO])
        for step in ("remove_blank", "clean_up"):
            item = self.step(step)
            self.assertFalse(self.on(item) or item.isEnabled(), step)
        for step in ("straighten", "upright", "searchable"):
            item = self.step(step)
            self.assertTrue(self.on(item) and item.isEnabled(), step)
        # show-through: Keep, and no other choice
        self.assertEqual(self.shown_show_through(), ShowThrough.KEEP)
        self.assertFalse(
            any(i.isEnabled() for i in self.bar.processing["show_through"].values())
        )
        # back to automatic: the steps are as they were
        self.bar.chooseMode_(self.bar.modes[ScanMode.AUTO])
        self.assertTrue(all(self.on(self.step(step)) for step in self.steps()))
        self.assertEqual(self.shown_show_through(), ShowThrough.GRAY_AND_COLOR)

    def test_the_menu_shows_what_each_mode_sets(self):
        for mode in ScanMode:
            self.bar.chooseMode_(self.bar.modes[mode])
            fixed = options.MODE_FIXES.get(mode, {})
            for name, items in self.bar.processing.items():
                for value, item in items.items():
                    with self.subTest(mode=mode, name=name, value=value):
                        self.assertEqual(item.isEnabled(), name not in fixed)
                        if name in fixed:
                            self.assertEqual(self.on(item), fixed[name] == value)

    def test_a_choice_the_mode_sets_cannot_change_even_when_it_agrees(self):
        # Keep chosen, then Photo: Photo keeps too, and the menu still must
        # not offer the choices Photo ignores
        self.bar.chooseProcessing_(
            self.bar.processing["show_through"][ShowThrough.KEEP]
        )
        self.bar.toggleStep_(self.step("clean_up"))  # off, as Photo sets it
        self.bar.chooseMode_(self.bar.modes[ScanMode.PHOTO])
        self.assertFalse(
            any(i.isEnabled() for i in self.bar.processing["show_through"].values())
        )
        self.assertFalse(self.step("clean_up").isEnabled())

    def steps(self) -> list[str]:
        """The Processing options that are switches."""
        return [n for n, items in self.bar.processing.items() if list(items) == [True]]

    def step(self, name: str):
        """The menu item of the Processing switch name."""
        return self.bar.processing[name][True]

    def shown_show_through(self) -> str:
        (shown,) = [
            v
            for v, item in self.bar.processing["show_through"].items()
            if self.on(item)
        ]
        return shown

    def test_show_through_is_one_of_three_choices(self):
        self.assertEqual(
            [
                str(item.title())
                for item in self.bar.processing["show_through"].values()
            ],
            ["Remove on gray and color pages", "Remove on every page", "Keep"],
        )
        processing = [
            str(item.title()) for item in self.step("straighten").menu().itemArray()
        ]
        self.assertEqual(processing.index("Show-through"), 3)  # after blank pages
        self.bar.chooseProcessing_(
            self.bar.processing["show_through"][ShowThrough.KEEP]
        )
        self.assertEqual(self.config.load().options.show_through, ShowThrough.KEEP)
        self.assertEqual(self.shown_show_through(), ShowThrough.KEEP)

    def test_switching_a_step_off_and_on(self):
        item = self.step("searchable")
        self.bar.toggleStep_(item)
        self.assertFalse(self.config.load().options.searchable)
        self.assertFalse(self.on(item))
        self.bar.toggleStep_(item)
        self.assertTrue(self.config.load().options.searchable)

    def test_choosing_a_quality(self):
        self.bar.chooseQuality_(self.bar.qualities[Quality.DRAFT])
        self.assertEqual(self.config.load().options.quality, Quality.DRAFT)

    def test_the_last_language_stays(self):
        self.bar.toggleLanguage_(self.bar.languages["eng"])  # leaves Automatic
        self.assertEqual(self.config.load().options.languages, ["eng"])
        self.bar.toggleLanguage_(self.bar.languages["eng"])
        self.assertEqual(self.config.load().options.languages, ["eng"])
        self.assertTrue(self.on(self.bar.languages["eng"]))

    def test_automatic_language_is_the_default_and_can_be_chosen_again(self):
        automatic = self.bar.languages[options.AUTOMATIC_LANGUAGE]
        self.assertTrue(self.on(automatic))
        self.bar.toggleLanguage_(self.bar.languages["eng"])
        self.assertFalse(self.on(automatic))
        self.bar.toggleLanguage_(automatic)
        self.assertEqual(
            self.config.load().options.language, options.AUTOMATIC_LANGUAGE
        )
        self.assertTrue(self.on(automatic))
        self.assertFalse(self.on(self.bar.languages["eng"]))


class AppIconTest(unittest.TestCase):
    def test_icon_is_drawn_at_full_size(self):
        from openscansnap import appicon

        rep = appicon.bitmap()
        self.assertEqual((rep.pixelsWide(), rep.pixelsHigh()), (appicon.CANVAS,) * 2)
        corner = rep.colorAtX_y_(0, 0)  # outside the rounded square
        self.assertEqual(corner.alphaComponent(), 0)
        middle = rep.colorAtX_y_(appicon.CANVAS // 2, appicon.CANVAS // 2)
        self.assertEqual(middle.alphaComponent(), 1)


if __name__ == "__main__":
    unittest.main()
