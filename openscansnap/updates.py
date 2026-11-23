"""Software updates for the macOS app, by Sparkle (sparkle-project.org): the
app checks its feed (Info.plist's SUFeedURL) by itself, and "Check for
updates…" asks now. Each release's disk image is signed with the EdDSA key
whose public half is SUPublicEDKey.

Only the built app carries Sparkle; run from a checkout there is nothing to
update, and updater() returns None.
"""

import logging
import sys
from pathlib import Path

import objc

from . import bundle

log = logging.getLogger(__name__)

FRAMEWORK = "Sparkle.framework"


def updater():
    """Sparkle's standard updater, started; None outside the built app."""
    if not bundle.FROZEN:
        return None
    # sys.executable is Contents/MacOS/<app>; frameworks are beside MacOS
    path = Path(sys.executable).resolve().parent.parent / "Frameworks" / FRAMEWORK
    try:
        objc.loadBundle("Sparkle", globals(), bundle_path=str(path))
    except ImportError as e:
        log.error("updates unavailable: cannot load %s: %s", path, e)
        return None
    controller = objc.lookUpClass("SPUStandardUpdaterController")
    return (
        controller.alloc().initWithStartingUpdater_updaterDelegate_userDriverDelegate_(
            True, None, None
        )
    )
