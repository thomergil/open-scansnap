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
from openscansnap.options import Quality, ScanMode

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

    def test_quitting_waits_for_the_scan_in_progress(self):
        bar = SimpleNamespace(status_line=mock.Mock(), item=object())
        with (
            mock.patch.object(self.menubar, "NSApplication") as app,
            mock.patch.object(self.menubar, "NSStatusBar"),
        ):
            terminate = app.sharedApplication.return_value.terminate_
            for snapshot, title in (
                (Snapshot(state=State.SCANNING), "Finishing 1 PDF, then quitting…"),
                (
                    Snapshot(state=State.SCANNING, making=1),
                    "Finishing 2 PDFs, then quitting…",
                ),
            ):
                self.menubar.MenuBar.finish_quitting(bar, snapshot)
                bar.status_line.setTitle_.assert_called_with(title)
            terminate.assert_not_called()
            self.menubar.MenuBar.finish_quitting(bar, Snapshot(state=State.READY))
            terminate.assert_called_once()

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

    def test_icon_shows_how_the_scanner_is_connected(self):
        icons = {via: self.menubar.status_icon(via) for via in (None, *Via)}
        for icon in icons.values():
            self.assertTrue(icon.isTemplate())  # follows the light or dark menu bar
            self.assertGreater(icon.size().height, 0)
        self.assertIn("on Wi-Fi", icons[Via.WIFI].accessibilityDescription())
        self.assertIn("on USB", icons[Via.USB].accessibilityDescription())
        self.assertIn("scanner off", icons[None].accessibilityDescription())
        # the letter stays put whichever mark is beside it
        self.assertEqual(icons[Via.WIFI].size().width, icons[Via.USB].size().width)
        connection = self.menubar.connection
        on_states = {
            s for s in State if connection(Snapshot(state=s, via=Via.USB)) is not None
        }
        self.assertEqual(on_states, {State.READY, State.PAPER, State.SCANNING})
        self.assertEqual(
            connection(Snapshot(state=State.PAPER, via=Via.WIFI)), Via.WIFI
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
        for via in (None, *Via):
            icon = self.menubar.status_icon(via)
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
            with self.subTest(via=via):
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
        icon = menubar.status_icon(Via.WIFI)
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
        for step, item in self.bar.steps.items():
            with self.subTest(step=step):
                self.assertTrue(self.on(item) and item.isEnabled())

    def test_choosing_a_mode_saves_it_and_moves_the_mark(self):
        self.bar.chooseMode_(self.bar.modes[ScanMode.GRAY])
        self.assertEqual(self.config.load().options.mode, ScanMode.GRAY)
        self.assertEqual(
            [mode for mode, item in self.bar.modes.items() if self.on(item)],
            [ScanMode.GRAY],
        )

    def test_steps_a_mode_rules_out_show_off_and_cannot_change(self):
        self.bar.chooseMode_(self.bar.modes[ScanMode.PHOTO])
        for step in ("remove_blank", "remove_show_through", "clean_up"):
            item = self.bar.steps[step]
            self.assertFalse(self.on(item) or item.isEnabled(), step)
        for step in ("straighten", "upright", "searchable"):
            item = self.bar.steps[step]
            self.assertTrue(self.on(item) and item.isEnabled(), step)
        # back to automatic: the steps are as they were
        self.bar.chooseMode_(self.bar.modes[ScanMode.AUTO])
        self.assertTrue(all(self.on(item) for item in self.bar.steps.values()))

    def test_switching_a_step_off_and_on(self):
        item = self.bar.steps["searchable"]
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
