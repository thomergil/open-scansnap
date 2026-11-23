"""What the menu bar shows for each scanner state."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
class MenuBarTest(unittest.TestCase):
    def setUp(self):
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
@unittest.skipUnless(APPKIT, "needs PyObjC (macOS)")
class SettingsMenuTest(unittest.TestCase):
    """The scan settings in the menu: what they show, and what choosing one
    saves."""

    def setUp(self):
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
        watcher = mock.Mock(snapshot=Snapshot())
        self.bar = menubar.MenuBar.alloc().initWithMonitor_settings_(
            watcher, config.load()
        )
        self.addCleanup(NSStatusBar.systemStatusBar().removeStatusItem_, self.bar.item)
        self.config = config

    def on(self, item) -> bool:
        from AppKit import NSControlStateValueOn

        return item.state() == NSControlStateValueOn

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
        english = self.bar.languages["eng"]
        self.bar.toggleLanguage_(english)
        self.assertEqual(self.config.load().options.languages, ["eng"])
        self.assertTrue(self.on(english))


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
