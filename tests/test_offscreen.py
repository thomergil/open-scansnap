"""The check that keeps test windows off the owner's screen."""

import importlib.util
import unittest
from unittest import mock

import offscreen

APPKIT = importlib.util.find_spec("AppKit") is not None


def window(visible: bool, kind: str = "NSWindow") -> mock.Mock:
    return mock.Mock(
        **{
            "isVisible.return_value": visible,
            "alphaValue.return_value": 1.0,
            "title.return_value": "Review",
            "className.return_value": kind,
        }
    )


@unittest.skipUnless(APPKIT, "needs PyObjC (macOS)")
class OffscreenTest(unittest.TestCase):
    def run_with(self, windows: list, hidden_by_cleanup: bool = False):
        class Shows(offscreen.Offscreen):
            def setUp(self):
                super().setUp()
                if hidden_by_cleanup:
                    self.addCleanup(
                        lambda: [
                            setattr(w.isVisible, "return_value", False) for w in windows
                        ]
                    )

            def test(self):
                pass

        app = mock.Mock(**{"windows.return_value": windows})
        with mock.patch(
            "AppKit.NSApplication", mock.Mock(**{"sharedApplication.return_value": app})
        ):
            result = unittest.TestResult()
            Shows("test").run(result)
        return result

    def test_a_window_shown_fails_the_test_though_a_cleanup_hides_it(self):
        result = self.run_with([window(True)], hidden_by_cleanup=True)
        self.assertEqual(len(result.failures), 1)

    def test_hidden_windows_and_the_menu_bar_icon_pass(self):
        result = self.run_with([window(False), window(True, "NSStatusBarWindow")])
        self.assertEqual(result.failures, [])


if __name__ == "__main__":
    unittest.main()
