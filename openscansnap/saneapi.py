"""The libsane calls the menu bar app needs, through ctypes: list and open
scanners, read their hardware sensors (the scan button, paper in the
feeder), and read and set text options (Wi-Fi pairing over USB).

python-sane would do this, but it is published only as source and this
project installs wheels only. libsane's C API (include/sane/sane.h) is
stable, and these few calls are all the app uses. Use a Library and its
Devices from one thread at a time.
"""

import ctypes
import os
from pathlib import Path

from . import bundle, sane

LIBRARY = bundle.LIBSANE

# SANE_Status (sane.h)
GOOD = 0
DEVICE_BUSY = 3
INVAL = 4
IO_ERROR = 9
ACCESS_DENIED = 11
# Text options are UTF-8; other bytes are kept as lone surrogates
TEXT_ENCODING, TEXT_ERRORS = "utf-8", "surrogateescape"
# SANE_Action, SANE_Value_Type
GET_VALUE, SET_VALUE = 0, 1
TYPE_BOOL, TYPE_INT, TYPE_FIXED, TYPE_STRING = 0, 1, 2, 3


class SaneError(Exception):
    """A libsane call failed; status is its SANE_Status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _OptionDescriptor(ctypes.Structure):  # SANE_Option_Descriptor
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("title", ctypes.c_char_p),
        ("desc", ctypes.c_char_p),
        ("type", ctypes.c_int),
        ("unit", ctypes.c_int),
        ("size", ctypes.c_int),
        ("cap", ctypes.c_int),
        ("constraint_type", ctypes.c_int),
        ("constraint", ctypes.c_void_p),
    ]


class Library:
    """libsane, initialised for the scanners configured in config_dir."""

    def __init__(self, path: Path = LIBRARY, config_dir: Path | None = None):
        if not path.exists():
            raise sane.ScanError(
                f"{path} not found; build SANE first (see docs/DEVELOPMENT.md)"
            )
        # read by libsane; scanimage gets the same
        os.environ.update(bundle.sane_environment(config_dir or sane.CONFIG_DIR))
        self._lib = lib = ctypes.CDLL(str(path))
        lib.sane_strstatus.restype = ctypes.c_char_p
        lib.sane_strstatus.argtypes = [ctypes.c_int]
        lib.sane_init.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_void_p]
        lib.sane_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.sane_close.argtypes = [ctypes.c_void_p]
        lib.sane_get_option_descriptor.restype = ctypes.POINTER(_OptionDescriptor)
        lib.sane_get_option_descriptor.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.sane_control_option.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.check(lib.sane_init(ctypes.byref(ctypes.c_int()), None), "sane_init")

    def check(self, status: int, call: str) -> None:
        if status != GOOD:
            message = self._lib.sane_strstatus(status).decode()
            raise SaneError(status, f"{call}: {message}")

    def open(self, name: str) -> "Device":
        handle = ctypes.c_void_p()
        self.check(self._lib.sane_open(name.encode(), ctypes.byref(handle)), name)
        return Device(self, handle, name)

    def exit(self) -> None:
        """Close every device and release libsane."""
        self._lib.sane_exit()


class Device:
    """An open scanner; close it so others (scanimage) can use it."""

    def __init__(self, library: Library, handle: ctypes.c_void_p, name: str):
        self.name = name  # as opened
        self._library, self._lib, self._handle = library, library._lib, handle
        self._options = {}  # option name: (number, type, size in bytes)
        number = 1  # option 0 is the option count
        while descriptor := self._lib.sane_get_option_descriptor(handle, number):
            option = descriptor.contents
            if option.name and option.type in (TYPE_BOOL, TYPE_INT, TYPE_STRING):
                self._options[option.name.decode()] = (number, option.type, option.size)
            number += 1

    def read(self, names: tuple[str, ...]) -> dict[str, int]:
        """The current values of these boolean or integer options."""
        values = {}
        for name in names:
            number, _, _ = self._option(name, (TYPE_BOOL, TYPE_INT))
            value = ctypes.c_int()
            self._control(name, number, GET_VALUE, ctypes.byref(value))
            values[name] = value.value
        return values

    def has(self, name: str) -> bool:
        """Whether the scanner has this option (some come with the backend
        version or the connection: Wi-Fi setup only over USB)."""
        return name in self._options

    def read_text(self, name: str) -> str:
        """The current value of a text option. Bytes that are not UTF-8 (a
        Wi-Fi network's name can be any bytes) come back as TEXT_ERRORS
        keeps them, so that write_text sends them back unchanged; shown()
        makes such text fit to show."""
        number, _, size = self._option(name, (TYPE_STRING,))
        value = ctypes.create_string_buffer(size)
        self._control(name, number, GET_VALUE, value)
        return value.value.decode(TEXT_ENCODING, TEXT_ERRORS)

    def write_text(self, name: str, text: str) -> None:
        """Set a text option (for some, that acts: pairing a HostID). The
        buffer is zeroed after: the text can hold a passphrase."""
        number, _, size = self._option(name, (TYPE_STRING,))
        data = text.encode(TEXT_ENCODING, TEXT_ERRORS)
        if len(data) >= size:
            raise SaneError(INVAL, f"{name}: longer than {size - 1} bytes")
        buffer = ctypes.create_string_buffer(data, size)
        try:
            self._control(name, number, SET_VALUE, buffer)
        finally:
            ctypes.memset(buffer, 0, size)

    def _option(self, name: str, types: tuple[int, ...]) -> tuple[int, int, int]:
        if not self._handle:
            raise SaneError(INVAL, "the scanner is closed")
        option = self._options.get(name)
        if option is None or option[1] not in types:
            raise SaneError(INVAL, f"the scanner has no option {name}")
        return option

    def _control(self, name: str, number: int, action: int, value) -> None:
        self._library.check(
            self._lib.sane_control_option(self._handle, number, action, value, None),
            name,
        )

    def close(self) -> None:
        if self._handle:
            self._lib.sane_close(self._handle)
            self._handle = None


def shown(text: str) -> str:
    """Text from read_text, fit to show: bytes that are not UTF-8 become
    U+FFFD."""
    return text.encode(TEXT_ENCODING, TEXT_ERRORS).decode(TEXT_ENCODING, "replace")
