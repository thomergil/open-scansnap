"""Menu bar app (macOS): the scanner's state, a Scan window, and the
scanner's own scan button, without a terminal.

    python -m openscansnap.menubar

The scanner is watched on a thread (monitor.Monitor); this module only
shows its snapshot and passes on the user's clicks.
"""

import faulthandler
import functools
import logging
import math
import os
import socket
import sys
from collections.abc import Callable
from enum import Enum, StrEnum, auto
from pathlib import Path
from typing import NamedTuple

import objc
from AppKit import (
    NSAboutPanelOptionApplicationIcon,
    NSAboutPanelOptionApplicationName,
    NSAboutPanelOptionApplicationVersion,
    NSAboutPanelOptionCredits,
    NSAboutPanelOptionVersion,
    NSAlert,
    NSAlertFirstButtonReturn,
    NSAlertStyleCritical,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSCompositingOperationDestinationOut,
    NSControlSizeLarge,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFont,
    NSFontAttributeName,
    NSFontWeightBold,
    NSForegroundColorAttributeName,
    NSGradient,
    NSGraphicsContext,
    NSImage,
    NSImageSymbolConfiguration,
    NSLinkAttributeName,
    NSMenu,
    NSMenuItem,
    NSModalResponseOK,
    NSOpenPanel,
    NSPanel,
    NSScreen,
    NSSecureTextField,
    NSStatusBar,
    NSStringDrawingUsesDeviceMetrics,
    NSTerminateCancel,
    NSTerminateLater,
    NSTerminateNow,
    NSTextField,
    NSVariableStatusItemLength,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskUtilityWindow,
    NSWorkspace,
)
from Foundation import (
    NSURL,
    NSLocale,
    NSMakePoint,
    NSMakeRect,
    NSMakeSize,
    NSObject,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSTimer,
)
from PyObjCTools import AppHelper

from . import (
    APP_NAME,
    HOMEPAGE,
    __version__,
    appicon,
    bundle,
    config,
    job,
    monitor,
    pages,
    review,
    reviewwindow,
    sane,
    scanner,
    updates,
)
from .monitor import Snapshot, State, Via
from .options import (
    AUTOMATIC_LANGUAGE,
    LANGUAGE_SEPARATOR,
    Quality,
    ScanMode,
    ShowThrough,
    lineart_dpi,
    page_dpi,
)
from .reviewwindow import ReviewWindow
from .wifiwindow import WifiWindow

# The menu bar icon: one letter, drawn as a template so it follows the light
# or dark menu bar. Futura Bold sets it apart from ScanSnap Home's slanted S.
ICON_LETTER = "S"
ICON_FONT = "Futura-Bold"
# Apple's guideline for menu bar icons: about 16 points tall, at most 18
ICON_HEIGHT = 16.0
# Blank around the drawing, for the anti-aliased edge pixels that lie just
# outside the letter's outline (cut off without it); 16 + 2 is Apple's 18
ICON_MARGIN = 1.0
# The shimmer while scanning or making PDFs, as in the owner's Cannonball app
SHIMMER_STEP = 0.05  # seconds per frame
SHIMMER_FRAMES = 24  # one sweep
SHIMMER_WIDTH = 0.5  # of the band, as a share of the icon's width
SHIMMER_DEPTH = 0.7  # how far the band's middle fades the icon
OFF_ALPHA = 0.4  # the letter's opacity while the scanner cannot be used


class Mark(StrEnum):
    """What the icon shows beside its letter: how the scanner is connected,
    or that something needs the user."""

    WIFI = "wifi"
    USB = "usb"
    ALERT = "alert"


class Icon(NamedTuple):
    """The menu bar icon: its letter dimmed unless the scanner can be used
    (ON_STATES), and its mark, if any."""

    on: bool
    mark: Mark | None


VIA_MARKS = {Via.WIFI: Mark.WIFI, Via.USB: Mark.USB}
# Each mark's SF Symbol, at a size in points that makes the marks look alike
# in weight (the plug is drawn smaller in its box), and what VoiceOver says
MARK_SYMBOLS = {
    Mark.WIFI: ("wifi", 7.0),
    Mark.USB: ("cable.connector", 10.0),
    Mark.ALERT: ("exclamationmark", 8.0),
}
MARK_DESCRIPTIONS = {
    Mark.WIFI: "scanner on Wi-Fi",
    Mark.USB: "scanner on USB",
    Mark.ALERT: "needs your attention",
    None: "scanner off",
}
ICONS = tuple(Icon(on, mark) for on in (True, False) for mark in MARK_DESCRIPTIONS)
PASSWORD_FIELD_WIDTH, PASSWORD_FIELD_HEIGHT = 220.0, 24.0  # points
TESSERACT_VARIANT_SEPARATOR = "_"
# Tesseract's kinds of one language (chi_sim), as the menu names them
TESSERACT_VARIANTS = {
    "sim": "simplified",
    "tra": "traditional",
}  # chi_sim: Chinese, simplified script
MARK_GAP = 1.0  # points between the letter and the mark
ON_STATES = {State.READY, State.PAPER, State.SCANNING}
# what keeps the scanner from use until the user acts (the menu says what)
ERROR_STATES = {
    State.NOT_SET_UP,
    State.BAD_CONFIGURATION,
    State.UNUSABLE,
    State.BUSY,
    State.REFUSED,
}
REFRESH_SECONDS = 0.25
# Quit waits this long for the scanner to be released; a call to a scanner
# that does not answer can take longer, and the scanner then drops the
# reservation by itself within 30 s.
QUIT_WAIT_SECONDS = 2.0
LOG_FILE = config.LOG_DIR / "open-scansnap.log"
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
STDERR_FD = 2  # the process's standard error, which native libraries write to
QUIET_LOGGERS = ("fontTools", "ocrmypdf")
# Scan window, in points
WINDOW_WIDTH, WINDOW_HEIGHT, MARGIN = 320.0, 150.0, 20.0
STATUS_POINTS, DETAIL_POINTS = 15.0, 12.0
BELOW_MENU_BAR = 8.0
# TN3179: connecting a UDP socket to a local address makes macOS ask for
# Local Network access now, while the user is there; it sends nothing.
LOCAL_NETWORK_PROBE_PORT = 9  # the discard port (RFC 863)
FOLDER_CHARS = 40  # the longest scans folder shown whole in the menu
ELLIPSIS = "…"

TEXT = {
    State.NOT_SET_UP: "Not set up",
    State.BAD_CONFIGURATION: "Scanner configuration damaged",
    State.UNUSABLE: "Scanner cannot be used",
    State.OFFLINE: "Scanner off or out of reach",
    State.BUSY: "Scanner in use by another app",
    State.REFUSED: sane.REFUSED_MESSAGE,
    State.READY: "Ready: put paper in the feeder",
    State.PAPER: "Ready, paper loaded",
    State.SCANNING: "Scanning…",
    "scan": "Scan",
    "review": reviewwindow.MENU_TITLE,
    "open_folder": "Open scans folder ({name})",
    "change_folder": "Change scans folder…",
    "choose": "Choose",
    "show_on_paper": "Show window when paper is loaded",
    "review_after_scan": reviewwindow.AFTER_SCAN_MENU_TITLE,
    "debug": "Debug mode",
    "making": "Processing…",
    "making_sheet": "Processing {sheet}/{sheets}…",
    "ready_again": "{processing} Ready to scan again.",
    "finishing": "Finishing {n} PDF{s}, then quitting…",
    "open_debug": "Open debug folder",
    "mode": "Scan mode",
    "quality": "Quality",
    "processing": "Processing",
    "language": "Text language",
    "language_auto": "Automatic",
    "quality_choice": "{name}: {page} dpi, black and white {lineart} dpi",
    "about": f"About {APP_NAME}…",
    "updates": "Check for updates…",
    "quit": f"Quit {APP_NAME}",
    "cannot_start": f"{APP_NAME} cannot start",
    "cannot_save": "Cannot save the setting",
    "password": "Scanner password…",
    "password_title": "Scanner password",
    "password_detail": (
        "The password set on the scanner. Unless it was changed, it is the "
        "last 4 characters of the serial number on the scanner's label."
    ),
    "save": "Save",
    "identity": "{model}, firmware {firmware}",
    "identity_newer": "⚠ {model}, firmware {firmware}: update to {newer} with ScanSnap Home",
    "network": "Network: {name}",
    "network_none": "Network: none set up",
    "wifi": "Set up Wi-Fi…",
    "cancel": "Cancel",
    "login_approval": f"Allow {APP_NAME} in System Settings > General > Login Items",
    "login_failed": f"Could not open {APP_NAME} at login: {{reason}}",
}
# What the user can do about a state, shown when there is no scan result
HINT = {
    State.NOT_SET_UP: (
        "Turn the scanner on and check that it is on this Mac's network; if it "
        f"is, allow {APP_NAME} in System Settings > Privacy & Security > Local "
        "Network."
    ),
    State.BAD_CONFIGURATION: "Delete that file to set the scanner up again.",
    State.OFFLINE: (
        "Open the scanner's paper chute to turn it on. If it is on, plug in its "
        "USB cable, or check that its Wi-Fi switch is on and it is on this Mac's "
        "network."
    ),
    State.BUSY: "Quit ScanSnap Home, including its menu bar item.",
    State.REFUSED: (
        f"If its password was changed, choose {TEXT['password']} in this menu."
    ),
}

log = logging.getLogger(__name__)


MODE_TITLES = {
    ScanMode.AUTO: "Automatic",
    ScanMode.BLACK_AND_WHITE: "Black and white document",
    ScanMode.GRAY: "Gray document",
    ScanMode.COLOR: "Color document",
    ScanMode.PHOTO: "Photo",
    ScanMode.AS_SCANNED: "As scanned",
}
QUALITY_TITLES = {
    Quality.DRAFT: "Draft",
    Quality.STANDARD: "Standard",
    Quality.HIGH: "High",
}
SHOW_THROUGH_TITLES = {
    ShowThrough.GRAY_AND_COLOR: "Remove on gray and color pages",
    ShowThrough.EVERY_PAGE: "Remove on every page",
    ShowThrough.KEEP: "Keep",
}
# The Processing menu's switches and submenus of fixed values, in order: each
# an Options field, with its title, or its submenu's title and titles by
# value; the Text language submenu, built from the installed languages,
# follows them
PROCESSING: dict[str, str | tuple[str, dict]] = {
    "straighten": "Straighten and crop to the sheet",
    "upright": "Turn pages upright",
    "remove_blank": "Remove blank pages",
    "show_through": ("Show-through", SHOW_THROUGH_TITLES),
    "clean_up": "Clean up creases and shadows",
    "searchable": "Make searchable",
}


def describe(snapshot: Snapshot) -> tuple[str, str]:
    """The status line and the detail line below it. While PDFs are being
    made, the status line says so (the sheet being made, of how many), and
    that the scanner is free again. A PDF that could not be made is told
    until the next scan, while the scanner can be used."""
    processing = processing_line(snapshot)
    if snapshot.making and snapshot.state in (State.READY, State.PAPER):
        return (
            TEXT["ready_again"].format(processing=processing),
            snapshot.failure or TEXT[snapshot.state],
        )
    usable = snapshot.state in ON_STATES
    if snapshot.failure and usable:  # else what to do about the scanner comes first
        detail = snapshot.failure
    elif snapshot.making:
        detail = processing
    else:
        message, hint = snapshot.message, HINT.get(snapshot.state, "")
        if message and hint:  # what is wrong, then what to do about it
            detail = f"{message.rstrip('.')}. {hint}"
        else:
            detail = message or hint
    return TEXT[snapshot.state], detail


def processing_line(snapshot: Snapshot) -> str:
    """Processing 3/13…: the sheet of the PDF being made; before its first
    sheet, Processing…"""
    if snapshot.progress is None:
        return TEXT["making"]
    sheet, sheets = snapshot.progress
    return TEXT["making_sheet"].format(sheet=sheet, sheets=sheets)


def folder_title(folder: Path) -> str:
    """folder as the menu shows it: as the settings spell it (from ~ inside
    the home folder); when longer than FOLDER_CHARS, the middle cut short,
    the first and last parts kept (~/src/scan…/receipts)."""
    shown = config.home_relative(folder)
    first, _, rest = shown.partition("/")  # "~" or "" (from /)
    if len(shown) <= FOLDER_CHARS or "/" not in rest:
        return shown
    middle, _, last = rest.rpartition("/")
    room = max(FOLDER_CHARS - len(first) - len("/" + ELLIPSIS + "/") - len(last), 1)
    return f"{first}/{middle[:room]}{ELLIPSIS}/{last}"


def can_scan(snapshot: Snapshot) -> bool:
    return snapshot.state == State.PAPER


class QuitState(Enum):
    """The quit's progress: not begun, finishing the scan and PDFs in
    progress, or done."""

    RUNNING = auto()
    FINISHING = auto()
    DONE = auto()


def status_icon(icon: Icon) -> NSImage:
    """ICON_LETTER in ICON_FONT, ICON_HEIGHT tall, as a template image,
    dimmed unless icon is on, with icon's mark at its upper right. The image
    is the letter's own outline, so macOS has nothing to shrink."""
    on, mark = icon

    def letter_at(points: float) -> NSAttributedString:
        font = NSFont.fontWithName_size_(
            ICON_FONT, points
        ) or NSFont.boldSystemFontOfSize_(points)
        color = NSColor.colorWithWhite_alpha_(0, 1 if on else OFF_ALPHA)
        return NSAttributedString.alloc().initWithString_attributes_(
            ICON_LETTER,
            {NSFontAttributeName: font, NSForegroundColorAttributeName: color},
        )

    def outline(text: NSAttributedString):
        return text.boundingRectWithSize_options_context_(
            NSMakeSize(1000, 1000), NSStringDrawingUsesDeviceMetrics, None
        )

    # the font size at which the letter itself is ICON_HEIGHT tall
    letter = letter_at(
        ICON_HEIGHT * ICON_HEIGHT / outline(letter_at(ICON_HEIGHT)).size.height
    )
    bounds = outline(letter)
    # the widest symbol's room, so the letter does not move when it changes
    symbols = {
        kind: NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            name, None
        ).imageWithSymbolConfiguration_(
            NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                points, NSFontWeightBold
            )
        )
        for kind, (name, points) in MARK_SYMBOLS.items()
    }
    room = NSMakeSize(
        max(symbol.size().width for symbol in symbols.values()),
        max(symbol.size().height for symbol in symbols.values()),
    )
    width = math.ceil(bounds.size.width + room.width + MARK_GAP + 2 * ICON_MARGIN)
    height = math.ceil(bounds.size.height + 2 * ICON_MARGIN)

    def draw(_rect) -> bool:
        # the drawing origin is the letter's baseline; shift it so the
        # letter's outline starts ICON_MARGIN from the image's corner
        # the box is measured from the drawing origin, so it must reach the
        # outline's far side: origin offset plus outline size (narrower clips)
        letter.drawWithRect_options_context_(
            NSMakeRect(
                ICON_MARGIN - bounds.origin.x,
                ICON_MARGIN - bounds.origin.y,
                bounds.origin.x + bounds.size.width,
                bounds.origin.y + bounds.size.height,
            ),
            NSStringDrawingUsesDeviceMetrics,
            None,
        )
        if mark is not None:
            symbol = symbols[mark].size()
            symbols[mark].drawInRect_(
                NSMakeRect(
                    ICON_MARGIN + bounds.size.width + MARK_GAP,
                    height - ICON_MARGIN - symbol.height,
                    symbol.width,
                    symbol.height,
                )
            )
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(
        NSMakeSize(width, height), False, draw
    )
    image.setTemplate_(True)
    image.setAccessibilityDescription_(f"{APP_NAME}: {MARK_DESCRIPTIONS[mark]}")
    return image


def working(snapshot: Snapshot) -> bool:
    """Whether the app is at work, scanning or making PDFs: the icon
    shimmers, and Review last scan… waits."""
    return snapshot.state == State.SCANNING or snapshot.making > 0


def about_options() -> dict:
    """The About window's contents: the app's icon and name, its version,
    and the project's page as a link."""
    link = NSAttributedString.alloc().initWithString_attributes_(
        HOMEPAGE.removeprefix("https://"),
        {
            NSLinkAttributeName: NSURL.URLWithString_(HOMEPAGE),
            NSFontAttributeName: NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()),
        },
    )
    return {
        NSAboutPanelOptionApplicationIcon: appicon.image(),
        NSAboutPanelOptionApplicationName: APP_NAME,
        NSAboutPanelOptionApplicationVersion: __version__,
        NSAboutPanelOptionVersion: "",  # no build number beside the version
        NSAboutPanelOptionCredits: link,
    }


def icon_of(snapshot: Snapshot) -> Icon:
    """The icon for snapshot: on while the scanner can be used; marked
    ALERT while something needs the user (an error state, or a PDF that
    failed, until the next scan), else with how a usable scanner is
    connected."""
    on = snapshot.state in ON_STATES
    if snapshot.state in ERROR_STATES or snapshot.failure:
        return Icon(on, Mark.ALERT)
    return Icon(on, VIA_MARKS.get(snapshot.via) if on else None)


def open_at_login() -> str | None:
    """Register the app to open at login (only the .app can be); return a
    problem to show, if any."""
    if not bundle.FROZEN:
        return None
    from ServiceManagement import (
        SMAppService,
        SMAppServiceStatusEnabled,
        SMAppServiceStatusRequiresApproval,
    )

    item = SMAppService.mainAppService()
    if item.status() == SMAppServiceStatusEnabled:
        return None
    ok, error = item.registerAndReturnError_(None)
    if item.status() == SMAppServiceStatusRequiresApproval:
        return TEXT["login_approval"]
    if not ok:
        problem = TEXT["login_failed"].format(
            reason=error.localizedDescription() if error else "unknown"
        )
        log.error("%s", problem)
        return problem
    return None


def request_local_network(host: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect((host, LOCAL_NETWORK_PROBE_PORT))
        except OSError as e:
            log.debug("local network request to %s: %s", host, e)


def confirm(title: str, body: str, action: str) -> bool:
    """Whether the user chooses action over Cancel."""
    panel = NSAlert.alloc().init()
    panel.setMessageText_(title)
    panel.setInformativeText_(body)
    panel.addButtonWithTitle_(action)
    panel.addButtonWithTitle_(TEXT["cancel"])
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    return panel.runModal() == NSAlertFirstButtonReturn


def alert(title: str, body: str) -> None:
    panel = NSAlert.alloc().init()
    panel.setAlertStyle_(NSAlertStyleCritical)
    panel.setMessageText_(title)
    panel.setInformativeText_(body)
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    panel.runModal()


class MenuBar(NSObject):
    def initWithMonitor_settings_store_(
        self, watcher: monitor.Monitor, settings: config.Settings, store: review.Store
    ):
        self = objc.super(MenuBar, self).init()  # noqa: PLW0642 - PyObjC's init pattern
        self.monitor, self.settings, self.store = watcher, settings, store
        self.review_window = None  # opened after a scan, or from the menu
        latest = store.latest()
        self.reviewed = latest.name if latest else None  # the scan shown last
        self.seen = Snapshot()  # counters already acted on
        self.quit_state = QuitState.RUNNING
        # macOS asked to quit (applicationShouldTerminate_) and waits for
        # replyToApplicationShouldTerminate_ once the quit is done
        self.reply_when_done = False
        self.icons = {icon: status_icon(icon) for icon in ICONS}
        self.shimmers = {}  # by Icon: its shimmer frames, made when first needed
        self.shown = None  # the Icon shown, and whether it shimmers
        self.animation = None  # the shimmer's timer, while working
        self.frames, self.frame = [], 0

        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.item.button().setImage_(self.icons[Icon(False, None)])
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.wifi_window = None  # opened from the menu
        # what is: the scanner's state, which scanner, its Wi-Fi network
        self.status_line = self.add(menu, "")
        self.status_line.setEnabled_(False)
        problem = open_at_login()
        if problem:
            self.add(menu, problem).setEnabled_(False)
        self.identity_item = self.add(menu, "", "openFirmwarePage:")
        self.network_item = self.add(menu, "")
        self.network_item.setEnabled_(False)
        menu.addItem_(NSMenuItem.separatorItem())
        # what the scanner can do now
        self.scan_item = self.add(menu, TEXT["scan"], "scan:")
        self.review_item = self.add(menu, TEXT["review"], "reviewLastScan:")
        # a scan is there to review; the item also waits while one is made
        self.reviewable = latest is not None
        self.review_item.setEnabled_(self.reviewable)
        self.password_item = self.add(menu, TEXT["password"], "enterPassword:")
        self.wifi_item = self.add(menu, TEXT["wifi"], "setUpWifi:")
        menu.addItem_(NSMenuItem.separatorItem())
        # where scans go
        self.open_folder = self.add(menu, "", "openFolder:")
        self.add(menu, TEXT["change_folder"], "changeFolder:")
        menu.addItem_(NSMenuItem.separatorItem())
        # how scans come out
        self.modes = self.choices(menu, TEXT["mode"], MODE_TITLES, "chooseMode:")
        self.qualities = self.choices(
            menu,
            TEXT["quality"],
            {
                quality: TEXT["quality_choice"].format(
                    name=title, page=page_dpi(quality), lineart=lineart_dpi(quality)
                )
                for quality, title in QUALITY_TITLES.items()
            },
            "chooseQuality:",
        )
        processing = self.submenu(menu, TEXT["processing"])
        self.processing: dict[str, dict] = {}  # by option: its items, by value
        for name, entry in PROCESSING.items():
            if isinstance(entry, str):  # a switch: checked when on
                item = self.add(processing, entry, "toggleStep:")
                item.setRepresentedObject_(name)
                self.processing[name] = {True: item}
            else:
                title, titles = entry
                self.processing[name] = self.choices(
                    processing, title, titles, "chooseProcessing:"
                )
        self.languages = self.choices(
            processing,
            TEXT["language"],
            {
                AUTOMATIC_LANGUAGE: TEXT["language_auto"],
                **{
                    code: language_title(code)
                    for code in menu_languages(settings.options.languages)
                },
            },
            "toggleLanguage:",
        )
        self.languages[AUTOMATIC_LANGUAGE].menu().insertItem_atIndex_(
            NSMenuItem.separatorItem(), 1
        )
        menu.addItem_(NSMenuItem.separatorItem())
        # the app itself
        self.on_paper = self.add(menu, TEXT["show_on_paper"], "toggleShowOnPaper:")
        self.after_scan = self.add(
            menu, TEXT["review_after_scan"], "toggleReviewAfterScan:"
        )
        self.debug = self.add(menu, TEXT["debug"], "toggleDebug:")
        self.open_debug = self.add(menu, TEXT["open_debug"], "openDebugFolder:")
        menu.addItem_(NSMenuItem.separatorItem())
        self.add(menu, TEXT["about"], "about:")
        self.updater = updates.updater()
        if self.updater is not None:
            check = self.add(menu, TEXT["updates"])
            check.setTarget_(self.updater)
            check.setAction_("checkForUpdates:")
        self.add(menu, TEXT["quit"], "quit:")
        self.item.setMenu_(menu)
        self.show_settings()

        self.window = self.make_window()
        self.refresh_(None)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            REFRESH_SECONDS, self, "refresh:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(
            timer, NSRunLoopCommonModes
        )  # also while the menu is open
        return self

    @objc.python_method
    def add(self, menu, title: str, action: str | None = None) -> NSMenuItem:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        if action:
            item.setTarget_(self)
        menu.addItem_(item)
        return item

    @objc.python_method
    def submenu(self, menu, title: str) -> NSMenu:
        inner = NSMenu.alloc().initWithTitle_(title)
        inner.setAutoenablesItems_(False)
        self.add(menu, title).setSubmenu_(inner)
        return inner

    @objc.python_method
    def choices(
        self, menu, title: str | None, titles: dict, action: str
    ) -> dict[str, NSMenuItem]:
        """An item per choice, in a submenu titled title (None: in menu
        itself), each carrying its value; by value."""
        if title is not None:
            menu = self.submenu(menu, title)
        items = {}
        for value, text in titles.items():
            items[value] = self.add(menu, text, action)
            items[value].setRepresentedObject_(str(value))
        return items

    @objc.python_method
    def make_window(self) -> NSPanel:
        """A floating panel that does not take the keyboard from other apps."""
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskUtilityWindow
            | NSWindowStyleMaskNonactivatingPanel
        )
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT),
            style,
            NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_(APP_NAME)
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setReleasedWhenClosed_(False)
        inner = WINDOW_WIDTH - 2 * MARGIN

        self.window_status = NSTextField.labelWithString_("")
        self.window_status.setFont_(NSFont.boldSystemFontOfSize_(STATUS_POINTS))
        self.window_status.setFrame_(NSMakeRect(MARGIN, 108, inner, 22))
        self.window_detail = NSTextField.wrappingLabelWithString_("")
        self.window_detail.setFont_(NSFont.systemFontOfSize_(DETAIL_POINTS))
        self.window_detail.setTextColor_(NSColor.secondaryLabelColor())
        self.window_detail.setFrame_(NSMakeRect(MARGIN, 62, inner, 42))
        self.window_button = NSButton.buttonWithTitle_target_action_(
            TEXT["scan"], self, "scan:"
        )
        self.window_button.setControlSize_(NSControlSizeLarge)
        self.window_button.setKeyEquivalent_("\r")
        self.window_button.setFrame_(NSMakeRect(MARGIN, 16, inner, 36))
        for view in (self.window_status, self.window_detail, self.window_button):
            panel.contentView().addSubview_(view)
        return panel

    @objc.python_method
    def show_window(self) -> None:
        """Bring the window up below the menu bar icon, without taking focus."""
        if not self.window.isVisible():
            icon = self.item.button().window().frame()
            screen = (NSScreen.mainScreen() or NSScreen.screens()[0]).visibleFrame()
            x = icon.origin.x + icon.size.width / 2 - WINDOW_WIDTH / 2
            x = max(
                screen.origin.x,
                min(x, screen.origin.x + screen.size.width - WINDOW_WIDTH),
            )
            self.window.setFrameTopLeftPoint_(
                NSMakePoint(x, icon.origin.y - BELOW_MENU_BAR)
            )
        self.window.orderFrontRegardless()

    @objc.python_method
    def show_settings(self) -> None:
        self.open_folder.setTitle_(
            TEXT["open_folder"].format(name=folder_title(self.settings.out_dir))
        )
        options = self.settings.options
        effective = options.in_effect()
        checks = [
            (self.on_paper, self.settings.show_when_paper_loaded),
            (self.after_scan, self.settings.review_after_scan),
            (self.debug, self.settings.debug),
            *((item, mode == options.mode) for mode, item in self.modes.items()),
            *(
                (item, quality == options.quality)
                for quality, item in self.qualities.items()
            ),
            *(
                (item, code in options.languages)
                for code, item in self.languages.items()
            ),
        ]
        for name, items in self.processing.items():
            # an option the scan mode sets shows as it sets it, unchangeable
            for value, item in items.items():
                checks.append((item, getattr(effective, name) == value))
                item.setEnabled_(not options.fixed_by_mode(name))
        for item, on in checks:
            item.setState_(NSControlStateValueOn if on else NSControlStateValueOff)
        self.open_debug.setHidden_(not self.settings.debug)
        if self.review_window is not None:  # its checkbox is the same setting
            self.review_window.show_after_scan(self.settings.review_after_scan)
        use_debug_logging(self.settings.debug)

    def refresh_(self, _timer) -> None:
        snapshot = self.monitor.snapshot
        if self.quit_state == QuitState.FINISHING:
            self.finish_quitting(snapshot)
        if self.quit_state != QuitState.RUNNING:
            return
        status, detail = describe(snapshot)
        self.status_line.setTitle_(status)
        self.scan_item.setEnabled_(can_scan(snapshot))
        self.password_item.setHidden_(snapshot.state != State.REFUSED)
        self.show_wifi(snapshot)
        self.show_identity(snapshot)
        self.window_status.setStringValue_(status)
        self.window_detail.setStringValue_(detail)
        self.window_button.setEnabled_(can_scan(snapshot))
        self.show_icon(icon_of(snapshot), busy=working(snapshot))
        loaded = snapshot.paper_loads > self.seen.paper_loads
        saved = snapshot.saved != self.seen.saved
        self.seen = snapshot
        if loaded and self.settings.show_when_paper_loaded:
            self.show_window()
        if saved:
            self.pdf_saved(snapshot.saved)
        # from the moment a new scan begins until its PDF is saved, the last
        # scan is about to be replaced: no review of it then
        self.review_item.setEnabled_(self.reviewable and not working(snapshot))

    @objc.python_method
    def pdf_saved(self, pdf: Path) -> None:
        """pdf was saved. When it is a new scan's, the scan opens for review
        if the setting says so and no other scan has changes still open; a
        PDF made again only refreshes the menu. A scan that could not be kept
        for review leaves nothing to review: no older scan stands in for it."""
        window = self.review_window
        latest = self.store.latest()
        new = latest is not None and latest.pdf == pdf and latest.name != self.reviewed
        if not new and any(scan.pdf == pdf for scan in self.store.scans()):
            return  # a scan made again from review: what there is to review stays
        self.reviewable = new  # a new scan not kept leaves nothing to review
        if not new:
            return
        self.reviewed = latest.name
        if self.settings.review_after_scan and not (window and window.has_changes()):
            self.open_review(latest)

    @objc.python_method
    def open_review(self, scan: review.Scan) -> None:
        if self.review_window is None:
            self.review_window = (
                ReviewWindow.alloc().initWithStore_monitor_setAfterScan_(
                    self.store,
                    self.monitor,
                    lambda on: self.save_setting("window.review_after_scan", on),
                )
            )
        self.review_window.show(scan, self.settings.review_after_scan)

    def reviewLastScan_(self, _sender) -> None:
        latest = self.store.latest()
        if latest is not None:
            self.open_review(latest)

    @objc.python_method
    def show_icon(self, icon: Icon, *, busy: bool) -> None:
        """icon (icon_of) in the menu bar; shimmering while busy
        (working(); a still, faded frame under Reduce Motion)."""
        if self.shown == (icon, busy):
            return
        self.shown = (icon, busy)
        if self.animation is not None:
            self.animation.invalidate()
            self.animation = None
        if not busy:
            self.item.button().setImage_(self.icons[icon])
            return
        if icon not in self.shimmers:
            self.shimmers[icon] = shimmer_frames(self.icons[icon])
        self.frames, self.frame = self.shimmers[icon], 0
        if NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion():
            self.item.button().setImage_(self.frames[len(self.frames) // 2])
            return
        self.animation = (
            NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                SHIMMER_STEP, self, "nextFrame:", None, True
            )
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(
            self.animation, NSRunLoopCommonModes
        )  # also while the menu is open

    def nextFrame_(self, _timer) -> None:
        self.item.button().setImage_(self.frames[self.frame])
        self.frame = (self.frame + 1) % len(self.frames)

    def scan_(self, _sender) -> None:
        self.monitor.request_scan()

    def openFolder_(self, _sender) -> None:
        folder = self.settings.out_dir
        folder.mkdir(parents=True, exist_ok=True)
        NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(folder)))

    def changeFolder_(self, _sender) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setCanCreateDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setPrompt_(TEXT["choose"])
        panel.setMessage_(TEXT["change_folder"].rstrip("…"))
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(self.settings.out_dir)))
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if panel.runModal() == NSModalResponseOK:
            self.save_setting("out_dir", config.home_relative(Path(panel.URL().path())))

    def toggleReviewAfterScan_(self, _sender) -> None:
        self.save_setting(
            "window.review_after_scan", not self.settings.review_after_scan
        )

    def toggleShowOnPaper_(self, _sender) -> None:
        self.save_setting(
            "window.show_when_paper_loaded", not self.settings.show_when_paper_loaded
        )

    def toggleDebug_(self, _sender) -> None:
        self.save_setting("debug", not self.settings.debug)

    @objc.python_method
    def show_wifi(self, snapshot: Snapshot) -> None:
        """The scanner's Wi-Fi network and its setup: there while the scanner
        is on USB and can set it up."""
        wifi = snapshot.wifi
        known = wifi is not None and wifi.network is not None
        self.network_item.setHidden_(not known)
        if known:
            self.network_item.setTitle_(
                TEXT["network"].format(name=wifi.network)
                if wifi.network
                else TEXT["network_none"]
            )
        self.wifi_item.setHidden_(wifi is None)

    @objc.python_method
    def show_identity(self, snapshot: Snapshot) -> None:
        """The scanner's model and firmware, with a warning when a newer
        firmware is out; choosing it opens PFU's ScanSnap downloads."""
        identity = snapshot.identity
        self.identity_item.setHidden_(identity is None)
        if identity is None:
            return
        self.identity_item.setTitle_(
            TEXT["identity_newer" if identity.newer else "identity"].format(
                model=identity.model, firmware=identity.firmware, newer=identity.newer
            )
        )
        self.identity_item.setEnabled_(identity.page is not None)

    def openFirmwarePage_(self, _sender) -> None:
        identity = self.monitor.snapshot.identity
        if identity is not None and identity.page is not None:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(identity.page))

    def setUpWifi_(self, _sender) -> None:
        if self.wifi_window is None:
            self.wifi_window = WifiWindow.alloc().initWithMonitor_(self.monitor)
        self.wifi_window.open()

    def enterPassword_(self, _sender) -> None:
        panel = NSAlert.alloc().init()
        panel.setMessageText_(TEXT["password_title"])
        panel.setInformativeText_(TEXT["password_detail"])
        field = NSSecureTextField.alloc().initWithFrame_(
            NSMakeRect(0, 0, PASSWORD_FIELD_WIDTH, PASSWORD_FIELD_HEIGHT)
        )
        panel.setAccessoryView_(field)
        panel.addButtonWithTitle_(TEXT["save"])
        panel.addButtonWithTitle_(TEXT["cancel"])
        panel.window().setInitialFirstResponder_(field)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if panel.runModal() != NSAlertFirstButtonReturn:
            return
        try:
            scanner.set_password(str(field.stringValue()))
        except sane.ScanError as e:
            alert(TEXT["cannot_save"], str(e))

    def chooseMode_(self, sender) -> None:
        self.save_setting("scan.mode", str(sender.representedObject()))

    def chooseQuality_(self, sender) -> None:
        self.save_setting("scan.quality", str(sender.representedObject()))

    def toggleStep_(self, sender) -> None:
        step = str(sender.representedObject())
        self.save_setting(f"scan.{step}", not getattr(self.settings.options, step))

    def chooseProcessing_(self, sender) -> None:
        """A value of a Processing option that is a submenu (PROCESSING)."""
        (name,) = [
            n for n, items in self.processing.items() if sender in items.values()
        ]
        self.save_setting(f"scan.{name}", str(sender.representedObject()))

    def toggleLanguage_(self, sender) -> None:
        """Automatic; or add the language to the text languages, or take it
        out (the last one stays)."""
        code = str(sender.representedObject())
        chosen = self.settings.options.languages
        if code == AUTOMATIC_LANGUAGE or self.settings.options.automatic_language:
            chosen = [code]
        elif code not in chosen:
            chosen = [*chosen, code]
        elif len(chosen) > 1:
            chosen = [name for name in chosen if name != code]
        self.save_setting("scan.language", LANGUAGE_SEPARATOR.join(chosen))

    def openDebugFolder_(self, _sender) -> None:
        config.RAW_DIR.mkdir(parents=True, exist_ok=True)
        NSWorkspace.sharedWorkspace().openURL_(
            NSURL.fileURLWithPath_(str(config.LOG_DIR))
        )

    @objc.python_method
    def save_setting(self, name: str, value) -> None:
        try:
            config.save(name, value)
            self.settings = config.load()
        except (config.ConfigError, OSError) as e:
            alert(TEXT["cannot_save"], str(e))
        self.show_settings()

    def about_(self, _sender) -> None:
        """macOS's own About window: the icon, the version, a link to the
        project."""
        app = NSApplication.sharedApplication()
        app.activateIgnoringOtherApps_(True)
        app.orderFrontStandardAboutPanelWithOptions_(about_options())

    def applicationShouldTerminate_(self, _app) -> int:
        """A quit from outside the menu (an Apple event, logout, Sparkle
        relaunching for an update) finishes as Quit does: PDFs made, the
        scanner released. Without this, macOS ended the app at once: PDFs in
        progress were lost and the scanner stayed reserved (2026-11-23)."""
        if self.quit_state == QuitState.DONE:
            return NSTerminateNow
        if self.quit_state == QuitState.RUNNING and not self.begin_quitting():
            return NSTerminateCancel  # the user kept changes under review
        # refresh_ finishes it and replies: its timer runs in the common
        # modes, so also while macOS waits
        self.reply_when_done = True
        return NSTerminateLater

    def quit_(self, _sender) -> None:
        """Quit once the scan in progress is taken in, and the scans taken in
        have their PDFs; changes under review are dropped only if the user
        agrees. Once quitting has begun, it goes on as it is."""
        if self.quit_state == QuitState.RUNNING and self.begin_quitting():
            self.refresh_(None)  # nothing in progress: quits now

    @objc.python_method
    def begin_quitting(self) -> bool:
        """Stop scanning and start finishing (finish_quitting); False when
        the user keeps changes under review instead."""
        reviewing = self.review_window
        if reviewing and reviewing.has_changes() and not reviewing.discard_changes():
            return False
        self.window.orderOut_(None)
        self.monitor.stop(QUIT_WAIT_SECONDS)
        self.quit_state = QuitState.FINISHING
        return True

    @objc.python_method
    def finish_quitting(self, snapshot: Snapshot) -> None:
        # a scan in progress becomes one more PDF
        pdfs = snapshot.making + (snapshot.state == State.SCANNING)
        if pdfs:
            self.status_line.setTitle_(
                TEXT["finishing"].format(n=pdfs, s="" if pdfs == 1 else "s")
            )
            return
        NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
        self.quit_state = QuitState.DONE
        app = NSApplication.sharedApplication()
        if self.reply_when_done:
            app.replyToApplicationShouldTerminate_(True)
        else:
            app.terminate_(None)  # asks applicationShouldTerminate_: done


def menu_languages(chosen: list[str]) -> list[str]:
    """The languages for the menu, by name: the Tesseract languages
    installed that macOS can name, and those already chosen."""
    installed = [code for code in pages.installed_languages() if language_name(code)]
    extra = [c for c in chosen if c not in installed and c != AUTOMATIC_LANGUAGE]
    codes = [*installed, *extra]
    return sorted(codes, key=language_title)


def language_title(code: str) -> str:
    """English for eng, "Chinese, simplified" for chi_sim; the code itself
    when macOS cannot name it."""
    name = language_name(code)
    if name is None:
        return code
    variant = code.partition(TESSERACT_VARIANT_SEPARATOR)[2]
    return f"{name}, {TESSERACT_VARIANTS.get(variant, variant)}" if variant else name


def language_name(code: str) -> str | None:
    base = code.split(TESSERACT_VARIANT_SEPARATOR)[0]
    return NSLocale.currentLocale().localizedStringForLanguageCode_(base)


def shimmer(icon: NSImage, position: float) -> NSImage:
    """icon faded under a soft vertical band centered at position (a share
    of its width), at its own size; as the owner's Cannonball app does."""
    size = icon.size()
    band = size.width * SHIMMER_WIDTH
    clear = NSColor.colorWithWhite_alpha_(0, 0)
    faded = NSColor.colorWithWhite_alpha_(0, SHIMMER_DEPTH)

    def draw(rect) -> bool:
        icon.drawInRect_(rect)
        NSGraphicsContext.currentContext().setCompositingOperation_(
            NSCompositingOperationDestinationOut
        )
        gradient = NSGradient.alloc().initWithColors_([clear, faded, clear])
        gradient.drawInRect_angle_(
            NSMakeRect(position * size.width - band / 2, 0, band, size.height), 0
        )
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(size, False, draw)
    image.setTemplate_(True)
    image.setAccessibilityDescription_(icon.accessibilityDescription())
    return image


def shimmer_frames(icon: NSImage) -> list[NSImage]:
    """One sweep of the band across icon, left to right."""
    edge = SHIMMER_WIDTH / 2  # band centers beyond this miss the icon
    step = (1 + 2 * edge) / (SHIMMER_FRAMES + 1)
    return [shimmer(icon, -edge + i * step) for i in range(1, SHIMMER_FRAMES + 1)]


def scan_with_settings(
    device_name: str, store: review.Store
) -> Callable[[job.Progress], Path]:
    """Take the sheets in from the scanner device_name; return what makes
    their PDF, with the settings of when they were scanned, and keeps the
    scan in store for review."""
    settings = config.load()  # the file may have changed since the app started
    keep_raw = config.RAW_DIR if settings.debug else None
    batch = job.scan(settings.options, device_name, keep_raw)
    return lambda progress: job.finish(
        batch, settings.out_dir, settings.options, progress, store.keep
    )


def clear_review(store: review.Store) -> None:
    """What a crash or an older scan left in store; a failure is logged, and
    the app starts without it (prune runs again after each scan)."""
    try:
        store.prune()
    except Exception:
        log.exception("cannot clear the scans kept for review")


def use_debug_logging(on: bool) -> None:
    """In debug mode the app logs every decision, edge finding included."""
    logging.getLogger("openscansnap").setLevel(logging.DEBUG if on else logging.INFO)


def main() -> None:
    bundle.use_bundled_tools()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format=LOG_FORMAT)
    # An exception in Python code AppKit calls (a table's rows, a timer) ends
    # the app; the traceback is the only record of why. PyObjC and Python
    # write such tracebacks, and a fatal error's, to stderr, as libraries
    # such as Leptonica write their errors: send stderr, Python's and the
    # process's, to the log, which an app's stderr otherwise never reaches.
    stream = open(LOG_FILE, "a", buffering=1)  # noqa: SIM115 - the app's life
    os.dup2(stream.fileno(), STDERR_FD)
    sys.stderr = stream
    faulthandler.enable(stream)
    objc.options.verbose = True
    for chatty in QUIET_LOGGERS:  # a line per font table and step of every PDF
        logging.getLogger(chatty).setLevel(logging.WARNING)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon
    app.setApplicationIconImage_(appicon.image())  # in alerts, also outside the .app
    try:
        settings = config.load()
    except config.ConfigError as e:
        log.error("%s", e)
        alert(TEXT["cannot_start"], str(e))
        sys.exit(1)
    try:
        configured = sane.configured()
    except sane.ScanError:
        configured = None  # the menu says what is wrong with it
    if configured is not None:  # else the search for it asks
        request_local_network(configured.host)
    store = review.Store(config.REVIEW_DIR)
    clear_review(store)  # before any scan
    watcher = monitor.Monitor(
        monitor.Connector(), functools.partial(scan_with_settings, store=store)
    )
    watcher.start()
    bar = MenuBar.alloc().initWithMonitor_settings_store_(watcher, settings, store)
    app.setDelegate_(bar)  # quits from outside the menu finish as Quit does
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
