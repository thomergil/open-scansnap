"""Command line: set up the scanner once, then scan to a PDF. The app sets
the scanner up by itself; setup is for the command line alone, or for a
scanner the search does not find.

python -m openscansnap setup [--host IP] [--hostid N] [--password]
python -m openscansnap scan [--out-dir DIR] [--mode MODE] [--quality QUALITY]
                           [--language eng+nld] [-v]

The scan command's defaults come from ~/.config/open-scansnap/config.toml
(see openscansnap/defaults.toml).
"""

import argparse
import getpass
import logging
import sys
from dataclasses import replace
from pathlib import Path

from . import bundle, config, job, monitor, sane, saneapi, scanner
from .options import Quality, ScanMode

EXIT_CANCELLED = 130  # the shell's code for a program stopped by Ctrl-C

log = logging.getLogger("openscansnap")


def setup(args: argparse.Namespace) -> None:
    password = getpass.getpass("Scanner password: ") if args.password else None
    hostid = None if args.hostid is None else parse_hostid(args.hostid)
    configured = scanner.set_up(args.host, hostid, password)
    print(f"Scanner at {configured.host} set up in {sane.CONFIG_DIR}")


def parse_hostid(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError as e:
        raise sane.ScanError(f"HostID: {e}") from e


def scan(args: argparse.Namespace) -> None:
    settings = config.load()
    out_dir = (
        settings.out_dir if args.out_dir is None else Path(args.out_dir).expanduser()
    )
    chosen = {
        name: value
        for name, value in (
            ("mode", args.mode and ScanMode(args.mode)),
            ("quality", args.quality and Quality(args.quality)),
            ("language", args.language),
        )
        if value
    }
    try:
        options = replace(settings.options, **chosen)
    except ValueError as e:
        raise sane.ScanError(f"--{e}") from e
    keep_raw = config.RAW_DIR if settings.debug else None
    print(f"Saved {job.run(out_dir, options, connected_scanner(), keep_raw)}")


def connected_scanner() -> str:
    """SANE's name of the scanner to use: on USB when it is there, as in the
    app; the scanner itself is closed again for scanimage."""
    connector = monitor.Connector()
    try:
        device = connector()
        device.close()
        return device.name
    except saneapi.SaneError as e:
        raise monitor.connection_error(e) or sane.ScanError(
            f"Scanner not available: {e}"
        ) from e
    finally:
        connector.exit()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="openscansnap",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="explain each page's treatment"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    s = commands.add_parser("setup", help="configure SANE for the Wi-Fi scanner")
    s.add_argument("--host", help="scanner IP address (default: found on the network)")
    s.add_argument(
        "--hostid",
        help="HostID the scanner was paired with (default: this Mac's, as "
        "ScanSnap Home computes it)",
    )
    s.add_argument(
        "--password",
        action="store_true",
        help="ask for the scanner password (default: last 4 characters of its serial)",
    )
    s.set_defaults(run=setup)

    c = commands.add_parser("scan", help="scan every sheet in the feeder to one PDF")
    c.add_argument("--out-dir", help="folder for the PDF (default: out_dir setting)")
    c.add_argument(
        "--mode",
        choices=[mode.value for mode in ScanMode],
        help="how pages are stored (default: scan.mode setting)",
    )
    c.add_argument(
        "--quality",
        choices=[quality.value for quality in Quality],
        help="stored resolution (default: scan.quality setting)",
    )
    c.add_argument(
        "--language",
        help="auto (each page's, detected), or Tesseract language(s), e.g. eng+nld "
        "(default: scan.language setting)",
    )
    c.set_defaults(run=scan)

    args = parser.parse_args()
    bundle.use_bundled_tools()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log.setLevel(logging.INFO if args.verbose else logging.WARNING)
    try:
        args.run(args)
    except job.Cancelled as e:
        print(e, file=sys.stderr)
        sys.exit(EXIT_CANCELLED)
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        sys.exit(EXIT_CANCELLED)
    except sane.ScanError as e:
        fail(f"{e}. {remedy(e)}" if remedy(e) else str(e))
    except OSError as e:  # setup writing the configuration
        fail(str(e))


# How to fix the problems whose fix differs between the command line and
# the app; the most specific class first
REMEDIES = {
    sane.NotFound: "Turn the scanner on and check that it is on this Mac's "
    "network, or run: python -m openscansnap setup --host <its IP address>",
    sane.NotSetUp: "Run: python -m openscansnap setup",
    sane.Refused: "If its password was changed, run: python -m openscansnap setup --password",
    sane.BadConfiguration: "Delete the file, or run: python -m openscansnap setup",
}


def remedy(error: sane.ScanError) -> str:
    return next(
        (text for kind, text in REMEDIES.items() if isinstance(error, kind)), ""
    )


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
