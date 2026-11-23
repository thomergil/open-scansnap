"""Whether a scanner's firmware has a newer release. Updating it is left to
ScanSnap Home (a failed update can leave the scanner unusable); the app only
says that one exists, and where to get ScanSnap Home.

The newest release of each model is kept here, from PFU's firmware pages,
so the app asks nobody: a newer release reaches users with an app update.
"""

# model (as the scanner names itself): its newest firmware version, from
# https://www.pfu.ricoh.com/global/scanners/scansnap/dl/firmware/ix500.html
NEWEST = {"ScanSnap iX500": "0U00"}  # 0U00 (2025): ScanSnap Cloud security
# PFU's ScanSnap downloads (choose the scanner and system there): ScanSnap
# Home, which updates the firmware
DOWNLOADS = "https://scansnap.com/d/"


def page(model: str) -> str | None:
    """Where to get ScanSnap Home for model; None when the model is not
    known."""
    return DOWNLOADS if model in NEWEST else None


def newer(model: str, version: str) -> str | None:
    """The newer firmware release for a scanner of model with firmware
    version; None when it has the newest or the model is not known. PFU's
    versions (0N00, 0U00) rise in letter order."""
    newest = NEWEST.get(model)
    if newest is None or not version or version.upper() >= newest:
        return None
    return newest
