"""Finder layout of Open ScanSnap's disk image, for dmgbuild (run by
build-macos.sh with -D app=APP -D background=TIFF): the app on the left, an
arrow, the Applications folder on the right. The positions are
dmg_background.py's, which draws the arrow between them."""

from pathlib import Path

application = defines["app"]  # noqa: F821 - supplied by dmgbuild
files = [application]
symlinks = {"Applications": "/Applications"}
format = "UDZO"
background = defines["background"]  # noqa: F821
window_rect = ((100, 100), (640, 350))  # dmg_background.WINDOW
default_view = "icon-view"
icon_size = 104
text_size = 14
icon_locations = {  # dmg_background.APP_SPOT and APPLICATIONS_SPOT
    Path(application).name: (145, 165),
    "Applications": (495, 165),
}
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
