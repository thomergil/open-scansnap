"""Which scanner the app uses: set up from the search, followed to a new
address, given a password."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openscansnap import discovery, sane, scanner

OURS, THEIRS = 0x00005E0053010000, 0x0011223344550000


def found(host, mac, hostid=None):
    return discovery.Found(host=host, model="iX500", mac=mac, hostid=hostid, serial="X")


class ScannerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (
            mock.patch.object(sane, "CONFIG_DIR", Path(tmp.name) / "sane.d"),
            mock.patch.object(scanner, "this_hostid", return_value=OURS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def search_finds(self, *scanners):
        patcher = mock.patch.object(discovery, "find", return_value=list(scanners))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_set_up_takes_the_scanner_paired_with_this_mac(self):
        self.search_finds(
            found("10.0.0.2", "aa:aa:aa:aa:aa:aa", THEIRS),
            found("10.0.0.3", "bb:bb:bb:bb:bb:bb", OURS),
        )
        configured = scanner.set_up()
        self.assertEqual(
            (configured.host, configured.mac), ("10.0.0.3", "bb:bb:bb:bb:bb:bb")
        )
        self.assertEqual(configured.hostid, OURS)
        self.assertEqual(sane.configured(), configured)

    def test_set_up_takes_the_first_when_none_names_this_mac(self):
        self.search_finds(
            found("10.0.0.2", "aa:aa:aa:aa:aa:aa"), found("10.0.0.3", "bb")
        )
        self.assertEqual(scanner.set_up().host, "10.0.0.2")

    def test_nothing_found_says_what_to_check(self):
        self.search_finds()
        with self.assertRaises(sane.NotFound):
            scanner.set_up()
        self.assertIsNone(sane.configured())

    def test_search_failure_is_a_plain_message(self):
        with (
            mock.patch.object(
                discovery, "find", side_effect=OSError("No route to host")
            ),
            self.assertRaisesRegex(sane.ScanError, "Cannot search for the scanner"),
        ):
            scanner.set_up()

    def test_moved_scanner_is_followed_and_keeps_its_password(self):
        sane.write_config(sane.Scanner("10.0.0.2", OURS, "pass", "aa:aa:aa:aa:aa:aa"))
        self.search_finds(found("10.0.0.9", "aa:aa:aa:aa:aa:aa", OURS))
        moved = scanner.follow(sane.configured())
        self.assertEqual((moved.host, moved.password), ("10.0.0.9", "pass"))
        self.assertEqual(sane.configured(), moved)

    def test_scanner_still_where_it_was_is_not_moved(self):
        configured = sane.Scanner("10.0.0.2", OURS, None, "aa:aa:aa:aa:aa:aa")
        sane.write_config(configured)
        self.search_finds(
            found("10.0.0.2", "aa:aa:aa:aa:aa:aa"), found("10.0.0.5", "bb")
        )
        self.assertIsNone(scanner.follow(configured))

    def test_look_alike_does_not_win_over_the_scanner_where_it_was(self):
        # another host answering first with the scanner's hardware address
        configured = sane.Scanner("10.0.0.2", OURS, "pass", "aa:aa:aa:aa:aa:aa")
        sane.write_config(configured)
        self.search_finds(
            found("10.0.0.66", "aa:aa:aa:aa:aa:aa"),
            found("10.0.0.2", "aa:aa:aa:aa:aa:aa"),
        )
        self.assertIsNone(scanner.follow(configured))
        self.assertEqual(sane.configured(), configured)

    def test_following_searches_only_the_scanners_network(self):
        configured = sane.Scanner("10.0.0.2", OURS, None, "aa:aa:aa:aa:aa:aa")
        with mock.patch.object(discovery, "find", return_value=[]) as find:
            scanner.follow(configured)
        self.assertEqual(find.call_args.kwargs["near"], "10.0.0.2")

    def test_scanner_without_known_address_is_not_searched_for(self):
        with mock.patch.object(discovery, "find") as find:
            self.assertIsNone(scanner.follow(sane.Scanner("10.0.0.2", OURS)))
        find.assert_not_called()

    def test_hardware_address_is_learned_where_the_scanner_answers(self):
        sane.write_config(sane.Scanner("10.0.0.2", OURS, "pass"))
        self.search_finds(
            found("10.0.0.5", "bb"), found("10.0.0.2", "aa:aa:aa:aa:aa:aa")
        )
        learned = scanner.learn_mac(sane.configured())
        expected = sane.Scanner("10.0.0.2", OURS, "pass", "aa:aa:aa:aa:aa:aa")
        self.assertEqual((learned, sane.configured()), (expected, expected))

    def test_hardware_address_not_learned_is_no_error(self):
        sane.write_config(sane.Scanner("10.0.0.2", OURS))
        with mock.patch.object(discovery, "find", side_effect=OSError("no route")):
            self.assertIsNone(scanner.learn_mac(sane.configured()).mac)
        self.assertIsNone(sane.configured().mac)

    def test_hardware_address_that_cannot_be_saved_leaves_the_scanner(self):
        configured = sane.Scanner("10.0.0.2", OURS, "pass")
        sane.write_config(configured)
        self.search_finds(found("10.0.0.2", "aa:aa:aa:aa:aa:aa"))
        with mock.patch.object(sane, "write_config", side_effect=OSError("read-only")):
            self.assertEqual(scanner.learn_mac(configured), configured)
        self.assertEqual(sane.configured(), configured)

    def test_hardware_address_is_not_written_onto_a_changed_configuration(self):
        # the configuration moved to another host while the search ran
        old = sane.Scanner("10.0.0.2", OURS, "pass")
        new = sane.Scanner("10.0.0.7", OURS, "pass")
        sane.write_config(new)
        self.search_finds(found("10.0.0.2", "aa:aa:aa:aa:aa:aa"))
        self.assertEqual(scanner.learn_mac(old), new)
        self.assertEqual(sane.configured(), new)

    def test_automatic_set_up_keeps_a_configuration_written_meanwhile(self):
        written = sane.Scanner("10.0.0.7", OURS, "pass")

        def search(*args, **kwargs):
            sane.write_config(written)  # another process, during the search
            return [found("10.0.0.2", "aa:aa:aa:aa:aa:aa", OURS)]

        with mock.patch.object(discovery, "find", side_effect=search):
            self.assertEqual(scanner.set_up(replacing=False), written)
        self.assertEqual(sane.configured(), written)

    def test_password_is_saved_with_the_rest(self):
        sane.write_config(sane.Scanner("10.0.0.2", OURS, None, "aa:aa:aa:aa:aa:aa"))
        scanner.set_password("s3cret")
        self.assertEqual(
            sane.configured(),
            sane.Scanner("10.0.0.2", OURS, "s3cret", "aa:aa:aa:aa:aa:aa"),
        )

    def test_password_needs_a_scanner_and_a_valid_password(self):
        with self.assertRaisesRegex(sane.ScanError, "No scanner set up"):
            scanner.set_password("1234")
        sane.write_config(sane.Scanner("10.0.0.2", OURS))
        with self.assertRaisesRegex(sane.ScanError, "Password must be"):
            scanner.set_password("has space")
        self.assertIsNone(sane.configured().password)


if __name__ == "__main__":
    unittest.main()
