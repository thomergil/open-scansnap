"""The installation check the macOS build runs inside the app."""

import shutil
import unittest

from openscansnap import pages, selftest


@unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
class SelfTestTest(unittest.TestCase):
    def test_passes_with_tesseract_installed(self):
        selftest.run()


if __name__ == "__main__":
    unittest.main()
