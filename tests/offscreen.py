"""Tests of the app's windows run in a process with no event loop: a window
shown there shows the owner a spinning wait cursor. Window tests keep theirs
invisible (alpha 0) and never activate the app; this check fails a test that
shows one anyway."""

import unittest

# the menu bar icon's own window, which a menu test's status item has: it
# sits in the menu bar and spins nothing
STATUS_BAR_WINDOW = "NSStatusBarWindow"


def assert_nothing_on_screen(test: unittest.TestCase) -> None:
    """Fail test when any window of this process can be seen."""
    from AppKit import NSApplication

    seen = [
        str(window.title()) or window.className()
        for window in NSApplication.sharedApplication().windows()
        if window.isVisible()
        and window.alphaValue() > 0
        and window.className() != STATUS_BAR_WINDOW
    ]
    test.assertEqual(seen, [], "a test showed a window on the screen")


class Offscreen(unittest.TestCase):
    """A test case whose windows must stay off the screen: checked when the
    test ends, before its cleanups could hide a window it showed."""

    def tearDown(self):
        assert_nothing_on_screen(self)
        super().tearDown()
