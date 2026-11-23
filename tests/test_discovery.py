"""Finding scanners on the network, and this Mac's HostID."""

import ipaddress
import socket
import struct
import unittest
from typing import ClassVar
from unittest import mock

import fake_ix500

from openscansnap import discovery

THIS_MAC = bytes.fromhex("00005e005301")
HOSTID = 0x00005E0053010000
# The fake listens on 127.0.0.1 alone: search there, as from 127.0.0.2
LOOPBACK = [(ipaddress.IPv4Interface("127.0.0.2/8"), fake_ix500.HOST)]
HOME = [(ipaddress.IPv4Interface("10.0.0.5/23"), "10.0.1.255")]
MINE = {ipaddress.IPv4Address("10.0.0.5"), ipaddress.IPv4Address("10.0.0.71")}
OTHER = [(ipaddress.IPv4Interface("10.0.0.5/24"), "10.0.0.255")]


class FakeSocket:
    """A UDP socket that records what is sent and returns the replies
    given, then times out."""

    replies: ClassVar[list] = []  # patched per test
    sent: ClassVar[list] = []  # patched per test

    def __init__(self, *args):
        self.replies = list(type(self).replies)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def setsockopt(self, *args):
        pass

    def bind(self, *args):
        pass

    def settimeout(self, *args):
        pass

    def sendto(self, packet, to):
        type(self).sent.append(to[0])

    def recvfrom(self, size):
        if not self.replies:
            raise TimeoutError
        return self.replies.pop(0)


def reply(sender="10.0.1.224", name=b"iX500-A1B2C3D4E5", hostid=HOSTID) -> bytes:
    """An iX500's search reply."""
    data = bytearray(132)
    data[:4] = b"VENS"
    data[0x10:0x14] = socket.inet_aton(sender)
    data[0x1C:0x22] = bytes.fromhex("00005e005399")
    data[0x28 : 0x28 + len(name)] = name
    data[0x7C:0x84] = struct.pack(">Q", hostid)
    return bytes(data)


class SearchTest(unittest.TestCase):
    def test_packets_are_scansnap_homes_without_a_hardware_address(self):
        packets = discovery.search_packets("10.0.0.71")
        self.assertEqual([p[:4] for p in packets], [b"VENS", b"ssNR", b"V2ss"])
        for packet in packets:
            with self.subTest(magic=packet[:4]):
                self.assertEqual(len(packet), 32)
                self.assertEqual(socket.inet_ntoa(packet[8:12]), "10.0.0.71")
                self.assertEqual(packet[12:20], bytes(8))
                self.assertEqual(struct.unpack(">I", packet[20:24])[0], 55264)
        self.assertEqual(struct.unpack(">I", packets[2][4:8])[0], 1)  # V2ss: version 1

    def test_reply_is_read(self):
        found = discovery.parse_reply(reply(), "10.0.1.224")
        self.assertEqual(
            (found.host, found.model, found.serial, found.mac, found.hostid),
            ("10.0.1.224", "iX500", "A1B2C3D4E5", "00:00:5e:00:53:99", HOSTID),
        )
        self.assertNotIn("A1B2C3D4E5", repr(found))  # its end is the default password

    def test_model_with_a_dash(self):
        found = discovery.parse_reply(reply(name=b"fi-7160-A1B2"), "10.0.1.224")
        self.assertEqual((found.model, found.serial), ("fi-7160", "A1B2"))

    def test_replies_that_are_not_a_scanners_are_ignored(self):
        for label, datagram, sender in (
            ("empty", b"", "10.0.0.1"),
            ("short", b"VENS" + bytes(20), "10.0.0.1"),
            ("other magic", b"XXXX" + reply()[4:], "10.0.1.224"),
            ("names another address", reply("10.0.1.9"), "10.0.1.224"),
            ("no serial", reply(name=b"iX500A1B2C3"), "10.0.1.224"),
            ("a line break", reply(name=b"iX500-A1\nforged"), "10.0.1.224"),
        ):
            with self.subTest(label):
                self.assertIsNone(discovery.parse_reply(datagram, sender))

    def test_reply_without_a_hostid(self):
        self.assertIsNone(discovery.parse_reply(reply(hostid=0), "10.0.1.224").hostid)

    def test_only_other_hosts_on_a_searched_network_count(self):
        for sender, counts in (
            ("10.0.1.224", True),
            ("10.0.0.9", True),  # a /23
            ("172.16.0.7", False),  # another network
            ("8.8.8.8", False),
            ("10.0.1.255", False),  # its broadcast address
            ("10.0.0.0", False),  # the network's own address
            ("10.0.0.5", False),  # this Mac
            ("10.0.0.71", False),  # this Mac, on its other interface
            ("0.0.0.0", False),
            ("127.0.0.1", False),
        ):
            with self.subTest(sender=sender):
                self.assertEqual(discovery.answers_from(sender, HOME, MINE), counts)


class FindTest(unittest.TestCase):
    def find(self, networks=LOOPBACK, **kwargs):
        with (
            mock.patch.object(discovery, "local_networks", return_value=networks),
            mock.patch.object(
                discovery, "my_addresses", return_value={networks[0][0].ip}
            ),
        ):
            return discovery.find(**kwargs)

    def test_scanner_on_the_network_is_found(self):
        with fake_ix500.FakeIX500(fake_ix500.Scanner(hostid=HOSTID)):
            found = self.find()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].host, fake_ix500.HOST)
        self.assertEqual(found[0].mac, fake_ix500.MAC.hex(":"))
        self.assertEqual(found[0].hostid, HOSTID)

    def test_search_near_an_address_stays_on_its_network(self):
        with fake_ix500.FakeIX500(fake_ix500.Scanner(hostid=HOSTID)):
            self.assertEqual(len(self.find(near=fake_ix500.HOST)), 1)
            with mock.patch.object(discovery, "search_packets") as packets:
                self.assertEqual(self.find(near="10.0.0.1"), [])
        packets.assert_not_called()  # nothing sent anywhere

    def test_nothing_found_in_the_rounds_given(self):
        self.assertEqual(self.find(rounds=2, round_seconds=0.1), [])

    def test_a_flood_of_replies_is_cut_short(self):
        replies = [
            (reply(fake_ix500.HOST, name=b"iX500-%04d" % n), (fake_ix500.HOST, 1))
            for n in range(40)
        ]
        with mock.patch.object(FakeSocket, "replies", replies), self.fake_socket():
            found = self.find(rounds=1)
        self.assertEqual(len(found), discovery.MAX_SCANNERS)

    def test_replies_from_outside_the_searched_networks_are_dropped(self):
        replies = [
            (reply("10.9.9.9"), ("10.9.9.9", 52217)),  # another network
            (reply("127.255.255.255"), ("127.255.255.255", 52217)),  # its broadcast
        ]
        with mock.patch.object(FakeSocket, "replies", replies), self.fake_socket():
            self.assertEqual(self.find(rounds=1, round_seconds=0.1), [])

    def test_search_near_an_address_sends_to_its_network_alone(self):
        with mock.patch.object(FakeSocket, "sent", []), self.fake_socket():
            self.find(
                networks=LOOPBACK + OTHER,
                rounds=1,
                round_seconds=0.01,
                near=fake_ix500.HOST,
            )
            self.assertEqual(set(FakeSocket.sent), {LOOPBACK[0][1]})

    def fake_socket(self):
        return mock.patch.object(discovery.socket, "socket", FakeSocket)


class HostidTest(unittest.TestCase):
    def hostid_mac(self, addresses):
        with mock.patch.object(discovery, "hardware_addresses", return_value=addresses):
            return discovery.hostid_mac()

    def test_en1_comes_first_as_in_scansnap_home(self):
        addresses = {"en0": "00:00:5e:00:53:02", "en1": "00:00:5e:00:53:01"}
        self.assertEqual(self.hostid_mac(addresses), THIS_MAC)
        addresses = {"en10": "02:00:00:00:00:10", **addresses}
        self.assertEqual(self.hostid_mac(addresses), THIS_MAC)

    def test_en0_without_en1(self):
        self.assertEqual(self.hostid_mac({"en0": "00:00:5e:00:53:01"}), THIS_MAC)

    def test_neither_or_an_unusable_address(self):
        for addresses in ({"en5": "00:00:5e:00:53:01"}, {"en0": "not an address"}):
            with self.subTest(addresses=addresses), self.assertRaises(OSError):
                self.hostid_mac(addresses)

    def test_hostid_is_the_address_shifted(self):
        with mock.patch.object(discovery, "hostid_mac", return_value=THIS_MAC):
            self.assertEqual(discovery.mac_hostid(), HOSTID)

    def test_this_macs_addresses_are_real(self):
        # macOS shows getifaddrs 02:00:00:00:00:00 for every interface
        self.assertTrue(
            any(
                mac != "02:00:00:00:00:00"
                for mac in discovery.hardware_addresses().values()
            )
        )


if __name__ == "__main__":
    unittest.main()
