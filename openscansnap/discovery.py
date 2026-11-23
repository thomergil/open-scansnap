"""Find ScanSnap scanners on the local networks, as ScanSnap Home does.

Each round sends ScanSnap Home's three search packets to UDP port 52217 at
every network's broadcast address; each scanner on Wi-Fi answers with its
address, model, serial number, hardware address and a HostID: that of the
computer connected to it, at least while one is. The scanner ignores a
search that does not carry the asking computer's address, but needs no
hardware address in it, so none is sent. A sleeping scanner misses the
first rounds while its Wi-Fi wakes up.

Nothing authenticates a reply: any computer on the network can answer. A
reply counts only when it comes from inside a network searched, from the
address it names. The SANE backend (fujitsu-vens.c, VENS_REG_*) sends the
same three packets, with a hardware address, to one scanner's address;
AGENTS.md has the layout.

The HostID is ScanSnap Home's: the Mac's hardware (MAC) address shifted
left 16 bits (mac_hostid; hostid_mac says which interface's). A scanner paired by
ScanSnap Home on this Mac therefore accepts it.
"""

import ipaddress
import logging
import re
import socket
import struct
import time
from dataclasses import dataclass, field

import objc
import psutil
from Foundation import NSBundle

log = logging.getLogger(__name__)

PORT = 52217
# ScanSnap Home's (the backend's VENS_REG_REPLY_TO); the scanner answers the
# sending port
REPLY_PORT = 55264
# (magic, flags, version) of ScanSnap Home's three search packets
SEARCHES = ((b"VENS", 0x0010, 0), (b"ssNR", 0x0100, 0), (b"V2ss", 0x1000, 1))
SEARCH_LEN = 32
SEARCH_IP, SEARCH_REPLY_PORT, SEARCH_FLAGS = 8, 20, 24  # offsets
REPLY_MAGIC = b"VENS"
REPLY_MAX = 512
# The iX500's 132-byte reply: offsets and lengths (verified 2026-11-22)
REPLY_ADDRESS = 0x10
REPLY_MAC, MAC_LEN = 0x1C, 6
# "<model>-<serial>"; the backend reads VENS_REG_NAME_LEN (16) of it
REPLY_NAME, NAME_LEN = 0x28, 64
REPLY_HOSTID, HOSTID_LEN = 0x7C, 8
# <model>-<serial>; a model may hold "-" (fi-7160), a serial not
NAME = re.compile(r"([A-Za-z0-9-]{1,32})-([A-Za-z0-9]{1,32})")
MAX_SCANNERS = 16  # replies kept per search: more is a flood, not scanners
# A scanner waking from sleep answers within these (the backend's
# VENS_REG_ROUNDS)
ROUNDS = 8
ROUND_SECONDS = 1.0
# ScanSnap Home's HostID: from _getHostId in its SshResident (AGENTS.md)
HOSTID_SHIFT = 16
PREFERRED_INTERFACE, FALLBACK_INTERFACE = "en1", "en0"
SYSTEM_CONFIGURATION = "/System/Library/Frameworks/SystemConfiguration.framework"
# name, Objective-C signature (returned, then arguments; @ is an object)
# and, for a Copy function, that what it returns is ours to release
SC_FUNCTIONS = [
    ("SCNetworkInterfaceCopyAll", b"@", "", {"retval": {"already_cfretained": True}}),
    ("SCNetworkInterfaceGetBSDName", b"@@"),
    ("SCNetworkInterfaceGetHardwareAddressString", b"@@"),
]
_system_configuration: dict = {}  # SC_FUNCTIONS by name, loaded on first use

# This Mac's address on a network (with the network's prefix), and the
# network's broadcast address
Network = tuple[ipaddress.IPv4Interface, str]


@dataclass(frozen=True)
class Found:
    host: str  # IPv4 address
    model: str  # e.g. iX500
    mac: str  # the scanner's hardware address, e.g. 00:00:5e:00:53:99
    hostid: int | None  # of the computer connected to it; None: none
    # its last 4 characters are the default password: never logged
    serial: str = field(repr=False)


def find(
    rounds: int = ROUNDS, round_seconds: float = ROUND_SECONDS, near: str | None = None
) -> list[Found]:
    """Every scanner that answers within rounds rounds, stopping after the
    first round any scanner answers in; in the order they answered. With
    near, an IPv4 address, only the network that holds it is searched, and
    none when no network does."""
    networks = local_networks()
    if near is not None:
        networks = [
            (interface, broadcast)
            for interface, broadcast in networks
            if ipaddress.IPv4Address(near) in interface.network
        ]
    if not networks:
        log.debug("no IPv4 network to search")
        return []
    found: list[Found] = []
    mine = my_addresses()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        for _ in range(rounds):
            for interface, broadcast in networks:
                for packet in search_packets(str(interface.ip)):
                    try:
                        sock.sendto(packet, (broadcast, PORT))
                    except OSError as e:  # a network that went away
                        log.debug("search on %s: %s", interface, e)
            deadline = time.monotonic() + round_seconds
            while (
                len(found) < MAX_SCANNERS and (left := deadline - time.monotonic()) > 0
            ):
                sock.settimeout(left)
                try:
                    reply, (sender, _) = sock.recvfrom(REPLY_MAX)
                except TimeoutError:
                    break
                scanner = parse_reply(reply, sender)
                if (
                    scanner
                    and answers_from(sender, networks, mine)
                    and scanner not in found
                ):
                    log.info("found %s at %s", scanner.model, scanner.host)
                    found.append(scanner)
            if found:
                break
    return found


def search_packets(address: str) -> list[bytes]:
    """ScanSnap Home's search packets from address (this Mac's), without
    the hardware address ScanSnap Home also puts in them."""
    packets = []
    for magic, flags, version in SEARCHES:
        packet = bytearray(SEARCH_LEN)
        packet[: len(magic)] = magic
        packet[len(magic) : SEARCH_IP] = struct.pack(">I", version)
        packet[SEARCH_IP : SEARCH_IP + 4] = socket.inet_aton(address)
        packet[SEARCH_REPLY_PORT : SEARCH_REPLY_PORT + 4] = struct.pack(
            ">I", REPLY_PORT
        )
        packet[SEARCH_FLAGS : SEARCH_FLAGS + 2] = struct.pack(">H", flags)
        packets.append(bytes(packet))
    return packets


def parse_reply(reply: bytes, sender: str) -> Found | None:
    """The scanner a reply from sender describes; None for any other
    datagram, a reply naming an address other than sender's, or a name that
    is not <model>-<serial>."""
    if len(reply) < REPLY_NAME + NAME_LEN or not reply.startswith(REPLY_MAGIC):
        return None
    if socket.inet_ntoa(reply[REPLY_ADDRESS : REPLY_ADDRESS + 4]) != sender:
        return None
    raw_name = reply[REPLY_NAME : REPLY_NAME + NAME_LEN].split(b"\0")[0]
    name = NAME.fullmatch(raw_name.decode("ascii", errors="replace"))
    if name is None:
        return None
    hostid = None
    if len(reply) >= REPLY_HOSTID + HOSTID_LEN:
        hostid = int.from_bytes(reply[REPLY_HOSTID : REPLY_HOSTID + HOSTID_LEN]) or None
    return Found(
        host=sender,
        model=name.group(1),
        mac=reply[REPLY_MAC : REPLY_MAC + MAC_LEN].hex(":"),
        hostid=hostid,
        serial=name.group(2),
    )


def answers_from(
    sender: str, networks: list[Network], mine: set[ipaddress.IPv4Address]
) -> bool:
    """Whether sender is another host on one of networks: inside it, none of
    its own addresses (network, broadcast), and none of this Mac's (mine)."""
    address = ipaddress.IPv4Address(sender)
    if address in mine:
        return False
    for interface, _ in networks:
        own = (interface.network.network_address, interface.network.broadcast_address)
        if address in interface.network and address not in own:
            return True
    return False


def my_addresses() -> set[ipaddress.IPv4Address]:
    """Every IPv4 address of this Mac, on every interface."""
    return {
        ipaddress.IPv4Address(address.address)
        for addresses in psutil.net_if_addrs().values()
        for address in addresses
        if address.family == socket.AF_INET
    }


def local_networks() -> list[Network]:
    """This Mac's address on each IPv4 network it is on that has a
    broadcast address, loopback aside, with that broadcast address."""
    up = {name for name, stats in psutil.net_if_stats().items() if stats.isup}
    networks = []
    for name, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if (
                name in up
                and address.family == socket.AF_INET
                and address.netmask
                and address.broadcast
            ):
                interface = ipaddress.IPv4Interface(
                    f"{address.address}/{address.netmask}"
                )
                if not interface.is_loopback:
                    networks.append((interface, address.broadcast))
    return networks


def hostid_mac() -> bytes:
    """The hardware address ScanSnap Home takes the HostID from: en1, else
    the first interface whose name starts with en1 (en10, ...), else en0.
    macOS hides the real addresses from getifaddrs (each reads
    02:00:00:00:00:00); SystemConfiguration gives them."""
    macs = hardware_addresses()
    preferred = sorted(name for name in macs if name.startswith(PREFERRED_INTERFACE))
    name = preferred[0] if preferred else FALLBACK_INTERFACE
    if name not in macs:
        raise OSError("this Mac has no en0 or en1 network interface")
    try:
        return bytes.fromhex(macs[name].replace(":", ""))
    except ValueError as e:
        raise OSError(f"{name} has no usable hardware address") from e


def hardware_addresses() -> dict[str, str]:
    """Each network interface's hardware address, by BSD name (en0)."""
    functions = system_configuration()
    addresses = {}
    for interface in functions["SCNetworkInterfaceCopyAll"]():
        name = functions["SCNetworkInterfaceGetBSDName"](interface)
        address = functions["SCNetworkInterfaceGetHardwareAddressString"](interface)
        if name and address:
            addresses[str(name)] = str(address)
    return addresses


def system_configuration() -> dict:
    """SC_FUNCTIONS by name, all of them; raises OSError when they cannot be
    loaded."""
    if not _system_configuration:
        bundle = NSBundle.bundleWithPath_(SYSTEM_CONFIGURATION)
        if bundle is None:
            raise OSError(f"{SYSTEM_CONFIGURATION} not found")
        loaded: dict = {}
        try:
            objc.loadBundleFunctions(bundle, loaded, SC_FUNCTIONS, skip_undefined=False)
        except objc.error as e:
            raise OSError(f"SystemConfiguration: {e}") from e
        _system_configuration.update(loaded)  # only once every one is there
    return _system_configuration


def mac_hostid() -> int:
    """This Mac's HostID, as ScanSnap Home computes it."""
    return int.from_bytes(hostid_mac()) << HOSTID_SHIFT
