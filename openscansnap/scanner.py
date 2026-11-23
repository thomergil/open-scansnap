"""Which scanner the app uses, and how to reach it: found on the network,
followed when its IP address changes, and its password when it is not the
default.

The configuration itself is sane.py's (sane.write_config); this module
decides what goes in it.
"""

import logging
import threading
from dataclasses import replace

from . import discovery, sane

log = logging.getLogger(__name__)

# Held while the configuration is read and written: the app's menu sets the
# password while its monitor thread sets the scanner up or follows it.
CONFIGURING = threading.RLock()

# Rounds of searching for the configured scanner on its network. Few: they
# run on each attempt to reach it, so a sleeping scanner that misses them
# answers a later attempt.
FOLLOW_ROUNDS = 2
NOT_FOUND = "No scanner found on this Mac's networks"


def this_hostid() -> int:
    """This Mac's HostID: the one ScanSnap Home paired with here, else the
    one it would pair with."""
    return sane.scansnap_home_hostid() or discovery.mac_hostid()


def set_up(
    host: str | None = None,
    hostid: int | None = None,
    password: str | None = None,
    *,
    replacing: bool = True,
) -> sane.Scanner:
    """Configure the scanner and return it. Without host, the scanner found
    on the network: one that names this Mac's HostID, else the first that
    answered. Without replacing, a Wi-Fi scanner configured meanwhile (by
    the command line) is kept and returned instead. Raises sane.NotFound
    when no scanner answers, sane.ScanError when the search cannot run."""
    mac = None
    try:
        hostid = this_hostid() if hostid is None else hostid
        if host is None:
            found = choose(discovery.find(), hostid)
            host, mac = found.host, found.mac
    except OSError as e:  # no network interface, or the search could not be sent
        raise sane.ScanError(f"Cannot search for the scanner: {e}") from e
    scanner = sane.Scanner(host, hostid, password, mac)
    with CONFIGURING:
        existing = None if replacing else sane.configured()
        if existing is not None:
            log.info("kept the scanner configured while searching")
            return existing
        save(scanner)
    log.info("set up the scanner at %s", host)
    return scanner


def choose(found: list[discovery.Found], hostid: int) -> discovery.Found:
    if not found:
        raise sane.NotFound(NOT_FOUND)
    return next((scanner for scanner in found if scanner.hostid == hostid), found[0])


def follow(scanner: sane.Scanner) -> sane.Scanner | None:
    """scanner at its new IP address on its network, configured; None when
    it answers where it was, is not found elsewhere, or its hardware
    address is not known. Raises sane.ScanError when the new address
    cannot be saved."""
    if scanner.mac is None:
        return None
    same = [found for found in search_near(scanner.host) if found.mac == scanner.mac]
    if not same or any(found.host == scanner.host for found in same):
        return None
    moved = update(when_host=scanner.host, host=same[0].host)
    log.info("the scanner moved to %s", moved.host)
    return moved


def learn_mac(scanner: sane.Scanner) -> sane.Scanner:
    """scanner with the hardware address of what answers at its address,
    configured, so follow can find it elsewhere later; scanner itself when
    nothing answers there or the address cannot be saved."""
    for found in search_near(scanner.host):
        if found.host == scanner.host:
            try:
                return update(when_host=scanner.host, mac=found.mac)
            except sane.ScanError as e:
                log.info("cannot save the scanner's hardware address: %s", e)
    return scanner


def search_near(host: str) -> list[discovery.Found]:
    """The scanners found on host's network; none when it cannot be
    searched."""
    try:
        return discovery.find(rounds=FOLLOW_ROUNDS, near=host)
    except OSError as e:
        log.info("cannot search for the scanner: %s", e)
        return []


def ensure_config() -> None:
    """sane.ensure_config, with the other writers held off; raises
    sane.ScanError when it cannot be saved."""
    with CONFIGURING:
        try:
            sane.ensure_config()
        except OSError as e:
            raise sane.ScanError(f"Cannot save the scanner configuration: {e}") from e


def set_password(password: str) -> sane.Scanner:
    """Configure password for the scanner set up; raises sane.ScanError when
    none is, the password is not one the scanner can have, or it cannot be
    saved."""
    return update(password=password)


def update(when_host: str | None = None, **changes) -> sane.Scanner:
    """The scanner set up, with changes configured; unchanged when it is no
    longer at when_host (the configuration changed while changes were
    found). Raises sane.NotSetUp when none is set up, sane.ScanError when
    the changes cannot be saved."""
    with CONFIGURING:
        configured = sane.configured()
        if configured is None:
            raise sane.NotSetUp("No scanner set up")
        if when_host is not None and configured.host != when_host:
            return configured
        changed = replace(configured, **changes)
        save(changed)
        return changed


def save(scanner: sane.Scanner) -> None:
    """Configure scanner; raises sane.ScanError when it cannot be saved."""
    with CONFIGURING:
        try:
            sane.write_config(scanner)
        except OSError as e:
            raise sane.ScanError(f"Cannot save the scanner configuration: {e}") from e
