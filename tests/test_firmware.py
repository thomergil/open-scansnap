"""Telling whether a scanner's firmware has a newer release."""

import unittest

from openscansnap import firmware


class FirmwareTest(unittest.TestCase):
    def test_older_firmware_has_a_newer_release(self):
        self.assertEqual(
            firmware.newer("ScanSnap iX500", "0N00"), firmware.NEWEST["ScanSnap iX500"]
        )

    def test_known_models_lead_to_scansnap_home(self):
        self.assertEqual(firmware.page("ScanSnap iX500"), firmware.DOWNLOADS)
        self.assertIsNone(firmware.page("fi-7160"))

    def test_the_newest_or_later_has_none(self):
        for version in ("0U00", "0V00", "0u00"):
            with self.subTest(version=version):
                self.assertIsNone(firmware.newer("ScanSnap iX500", version))

    def test_unknown_model_or_version_says_nothing(self):
        self.assertIsNone(firmware.newer("fi-7160", "0A00"))
        self.assertIsNone(firmware.newer("ScanSnap iX500", ""))


if __name__ == "__main__":
    unittest.main()
