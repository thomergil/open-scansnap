"""A ScanSnap iX500 on Wi-Fi, as seen by SANE's fujitsu backend: the
registration, reservation and release exchanges and the SCSI command tunnel.

It accepts only what the real Wi-Fi firmware accepts and rejects everything
else as an invalid opcode (see AGENTS.md, "The Wi-Fi firmware rejects").
Each sheet must be read front first, then back, once each. Where the real
scanner's behavior is unknown the fake is strict: a command from a client
that does not hold the reservation closes the connection, a release from
another client is answered BUSY, and a READ gets at most its length.
Control replies are the bytes a real iX500 (firmware 0U00) returned; the
serial number is made up. Each side is served as the JPEG the test supplies.
"""

import socket
import socketserver
import struct
import threading
from dataclasses import dataclass, field

HOST = "127.0.0.1"
REG_PORT, CMD_PORT, HS_PORT = 52217, 53218, 53219
MAGIC = b"VENS"
NAME = b"iX500-TEST000001"
MAC = bytes.fromhex("00809200beef")  # the scanner's hardware address
REPLY_LEN = 132  # the iX500's search reply
KEEP_ALIVE = 1  # at offset 4 of a registration packet; 0 is a search
PASSWORD = "0001"  # the default: last 4 characters of the serial
HELLO = struct.pack(">I4sII", 16, MAGIC, 0, 0)

HS_RESERVE, HS_RELEASE = 0x11, 0x12
OK, BAD_KEY, BUSY, OTHER_HOST = 0, -3, -4, -7
GOOD, CHECK_CONDITION = 0, 2

INQUIRY = bytes.fromhex(
    "060092025b00001046554a49545355205363616e536e6170206958353030202030553030"
    "000000000000000003010000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000"
)
VPD_F0 = bytes.fromhex(
    "06f002008b02580258110258025800320032fffc00001468000050e88f000000920a1800"
    "00000000efbf000400000000000600000000000000000000000000000000000000000000"
    "0000000000000000000000ff00214111a840080000000000000000000099009900c20000"
    "80000020000000b000009100340072000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000"
)
START_JOB_REPLY = bytes(8)

# Sense data: key, ASC, ASCQ as the real scanner reports them
INVALID_OPCODE = (0x05, 0x20, 0x00)
HOPPER_EMPTY = (0x03, 0x80, 0x03)
INVALID_FIELD = (0x05, 0x24, 0x00)
SENSE_EOM, SENSE_ILI = 0x40, 0x20

# The real firmware's command set; everything else is rejected
INQUIRY_CODE, REQUEST_SENSE, HW_STATUS, READ = 0x12, 0x03, 0xC2, 0x28
START_JOB, CANCEL_READ, PRE_READ, SET_PARAMS, START_PAPER, END_JOB = (
    0xD5,
    0xD8,
    0xE9,
    0xD4,
    0xE0,
    0xD6,
)
HOPPER_BYTE, HOPPER_EMPTY_BIT = 3, 0x80
BUTTON_BYTE, SCAN_BUTTON_BIT = 4, 0x01  # a press shows in one reading
READ_IMAGE_FLAG, FRONT_WINDOW, BACK_WINDOW, SEQUENCE_BYTE = 0x02, 0x00, 0x80, 10
IMAGE_SUBHEADER = bytes(2)  # the real reply has a few bytes before the JPEG


def cipher(password: str) -> bytes:
    table = "pFusCANsNapFiPfu"
    return "".join(
        f"{ord(table[i]) + ord(c) + 11:03d}" for i, c in enumerate(password)
    ).encode()


def sense(key: int, asc: int, ascq: int, flags: int = 0, info: int = 0) -> bytes:
    data = bytearray(18)
    data[0] = 0xF0
    data[2] = flags | key
    data[3:7] = (info % 2**32).to_bytes(4, "big")
    data[7] = 0x0A
    data[12], data[13] = asc, ascq
    return bytes(data)


@dataclass
class Scanner:
    """Shared state; tests read it after scanning."""

    hostid: int
    password: str = PASSWORD
    sheets: list[tuple[bytes, bytes]] = field(
        default_factory=list
    )  # (front, back) JPEGs
    holder: bytes | None = None  # MAC holding the reservation
    current: tuple[bytes, bytes] | None = None
    pending: list[int] = field(default_factory=list)  # windows still to read
    last_sense: bytes = sense(0, 0, 0)
    residual: int | None = None  # report this residual instead of the true one
    oversize: int = 0  # bytes added to INQUIRY replies, beyond what was asked
    presses: int = 0  # scan button presses not yet reported
    keep_alives: list = field(default_factory=list)  # the MAC of each keep-alive
    opcodes: list[int] = field(default_factory=list)
    reads: list[tuple[int, int]] = field(default_factory=list)  # (sequence, window)
    rejected: list[int] = field(default_factory=list)
    unreserved: list[int] = field(
        default_factory=list
    )  # commands without the reservation
    macs: set[bytes] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


def read_packet(sock: socket.socket) -> bytes | None:
    head = sock.recv(4, socket.MSG_WAITALL)
    if len(head) < 4:
        return None
    rest = struct.unpack(">I", head)[0] - 4
    body = sock.recv(rest, socket.MSG_WAITALL) if rest else b""
    return head + body if len(body) == rest else None


class Registration(socketserver.BaseRequestHandler):
    """Answers a search, as the iX500 does: only one that carries the
    asking computer's address."""

    def handle(self):
        data, sock = self.request
        if data[:4] != MAGIC or data[8:12] == bytes(4):
            return
        if struct.unpack(">I", data[4:8])[0] == KEEP_ALIVE:
            with self.server.scanner.lock:
                self.server.scanner.keep_alives.append(data[12:18])  # its MAC
            return  # the reply (0x8001 at offset 4) is not needed by clients
        reply = bytearray(REPLY_LEN)
        reply[:4] = MAGIC
        reply[0x10:0x14] = socket.inet_aton(HOST)
        reply[0x1C:0x22] = MAC
        reply[0x28 : 0x28 + len(NAME)] = NAME
        reply[0x7C:0x84] = struct.pack(">Q", self.server.scanner.hostid)
        sock.sendto(bytes(reply), self.client_address)


class Handshake(socketserver.BaseRequestHandler):
    def handle(self):
        s: Scanner = self.server.scanner
        self.request.sendall(HELLO)
        pkt = read_packet(self.request)
        if not pkt or pkt[4:8] != MAGIC:
            return
        command, mac = struct.unpack(">I", pkt[8:12])[0], pkt[16:22]
        with s.lock:
            s.macs.add(mac)
            if command == HS_RESERVE:
                key = pkt[52:100].rstrip(b"\0")
                hostid = struct.unpack(">Q", pkt[108:116])[0]
                if key != cipher(s.password):
                    code = BAD_KEY
                elif hostid != s.hostid:
                    code = OTHER_HOST
                elif s.holder not in (None, mac):
                    code = BUSY
                else:
                    code, s.holder = OK, mac
                self.request.sendall(struct.pack(">I4si", 20, MAGIC, code) + bytes(8))
            elif command == HS_RELEASE:
                code = OK if s.holder == mac else BUSY
                if code == OK:
                    s.holder = None
                self.request.sendall(struct.pack(">I4si", 16, MAGIC, code) + bytes(4))


class Commands(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(HELLO)
        s: Scanner = self.server.scanner
        while (pkt := read_packet(self.request)) is not None:
            cdb_len, read_len, write_len = struct.unpack(">III", pkt[32:44])
            cdb, out = pkt[48 : 48 + cdb_len], pkt[64 : 64 + write_len]
            with s.lock:
                if pkt[16:22] != s.holder:
                    s.unreserved.append(cdb[0])
                    return
            status, data = self.execute(cdb, read_len, out)
            reply = (
                struct.pack(">I4sII", 40 + len(data), MAGIC, 0, status)
                + bytes(24)
                + data
            )
            self.request.sendall(reply)

    def execute(self, cdb: bytes, read_len: int, out: bytes) -> tuple[int, bytes]:
        s: Scanner = self.server.scanner
        op = cdb[0]
        with s.lock:
            s.opcodes.append(op)
            if op == INQUIRY_CODE:
                data = VPD_F0 if cdb[1] & 1 else INQUIRY
                return GOOD, data[:read_len] + bytes(s.oversize)
            if op == REQUEST_SENSE:
                data, s.last_sense = s.last_sense, sense(0, 0, 0)
                return GOOD, data[:read_len]
            if op == HW_STATUS:
                status = bytearray(max(read_len, BUTTON_BYTE + 1))
                status[HOPPER_BYTE] = 0 if s.sheets else HOPPER_EMPTY_BIT
                if s.presses:
                    s.presses -= 1
                    status[BUTTON_BYTE] |= SCAN_BUTTON_BIT
                return GOOD, bytes(status[:read_len])
            if op == START_JOB:
                return GOOD, START_JOB_REPLY[:read_len]
            if op in (CANCEL_READ, PRE_READ, SET_PARAMS, END_JOB):
                return GOOD, b""
            if op == START_PAPER:
                if s.pending:  # the current sheet's sides must be read first
                    s.last_sense = sense(*INVALID_FIELD)
                    return CHECK_CONDITION, b""
                if not s.sheets:
                    s.last_sense = sense(*HOPPER_EMPTY)
                    return CHECK_CONDITION, b""
                s.current = s.sheets.pop(0)
                s.pending = [FRONT_WINDOW, BACK_WINDOW]
                return GOOD, b""
            if op == READ and cdb[3] == READ_IMAGE_FLAG:
                if not s.pending or cdb[5] != s.pending[0]:
                    s.last_sense = sense(*INVALID_FIELD)
                    return CHECK_CONDITION, b""
                s.pending.pop(0)
                s.reads.append((cdb[SEQUENCE_BYTE], cdb[5]))
                image = (IMAGE_SUBHEADER + s.current[cdb[5] == BACK_WINDOW])[:read_len]
                residual = read_len - len(image) if s.residual is None else s.residual
                s.last_sense = sense(0, 0, 0, SENSE_EOM | SENSE_ILI, residual)
                return CHECK_CONDITION, image
            s.rejected.append(op)
            s.last_sense = sense(*INVALID_OPCODE)
            return CHECK_CONDITION, b""


class _TCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _UDP(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeIX500:
    """Serve a Scanner on HOST until stopped."""

    def __init__(self, scanner: Scanner):
        self.scanner = scanner
        self.servers = [
            _UDP((HOST, REG_PORT), Registration),
            _TCP((HOST, HS_PORT), Handshake),
            _TCP((HOST, CMD_PORT), Commands),
        ]
        for server in self.servers:
            server.scanner = scanner

    def __enter__(self):
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        for server in self.servers:
            server.shutdown()
            server.server_close()
