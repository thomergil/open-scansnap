"""Menu bar app (macOS): the scanner's state, a Scan window, and the
scanner's own scan button, without a terminal.

    python -m openscansnap.menubar

The scanner is watched on a thread (monitor.Monitor); this module only
shows its snapshot and passes on the user's clicks.
"""

import logging
import math
import socket
import sys
from collections.abc import Callable
from pathlib import Path

import objc
from AppKit import (
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
    NSMenu,
    NSMenuItem,
    NSModalResponseOK,
    NSOpenPanel,
    NSPanel,
    NSScreen,
    NSSecureTextField,
    NSStatusBar,
    NSStringDrawingUsesDeviceMetrics,
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
    appicon,
    bundle,
    config,
    job,
    monitor,
    pages,
    sane,
    scanner,
    updates,
)
from .monitor import Snapshot, State, Via
from .options import LANGUAGE_SEPARATOR, Quality, ScanMode, lineart_dpi, page_dpi
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
# The shimmer while PDFs are being made, as in the owner's Cannonball app
SHIMMER_STEP = 0.05  # seconds per frame
SHIMMER_FRAMES = 24  # one sweep
SHIMMER_WIDTH = 0.5  # of the band, as a share of the icon's width
SHIMMER_DEPTH = 0.7  # how far the band's middle fades the icon
OFF_ALPHA = 0.4  # the letter while the scanner is off or out of reach
# How the scanner is connected: its SF Symbol, at a size in points that makes
# the marks look alike in weight (the plug is drawn smaller in its box), and
# what VoiceOver says
VIA_SYMBOLS = {Via.WIFI: ("wifi", 7.0), Via.USB: ("cable.connector", 10.0)}
VIA_DESCRIPTIONS = {
    Via.WIFI: "scanner on Wi-Fi",
    Via.USB: "scanner on USB",
    None: "scanner off",
}
PASSWORD_FIELD_WIDTH, PASSWORD_FIELD_HEIGHT = 220.0, 24.0  # points
TESSERACT_VARIANT_SEPARATOR = "_"
# Tesseract's kinds of one language (chi_sim), as the menu names them
TESSERACT_VARIANTS = {
    "sim": "simplified",
    "tra": "traditional",
}  # chi_sim: Chinese, simplified script
MARK_GAP = 1.0  # points between the letter and the mark
ON_STATES = {State.READY, State.PAPER, State.SCANNING}
REFRESH_SECONDS = 0.25
# Quit waits this long for the scanner to be released; a call to a scanner
# that does not answer can take longer, and the scanner then drops the
# reservation by itself within 30 s.
QUIT_WAIT_SECONDS = 2.0
LOG_FILE = config.LOG_DIR / "open-scansnap.log"
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
QUIET_LOGGERS = ("fontTools", "ocrmypdf")
# Scan window, in points
WINDOW_WIDTH, WINDOW_HEIGHT, MARGIN = 320.0, 150.0, 20.0
STATUS_POINTS, DETAIL_POINTS = 15.0, 12.0
BELOW_MENU_BAR = 8.0
# TN3179: connecting a UDP socket to a local address makes macOS ask for
# Local Network access now, while the user is there; it sends nothing.
LOCAL_NETWORK_PROBE_PORT = 9  # the discard port (RFC 863)

TEXT = {
    State.NOT_SET_UP: "Not set up",
    State.BAD_CONFIGURATION: "Scanner configuration damaged",
    State.UNUSABLE: "Scanner cannot be used",
    State.OFFLINE: "Scanner off or out of reach",
    State.BUSY: "Scanner in use by another app",
    State.REFUSED: "Scanner refused the password or HostID",
    State.READY: "Ready: put paper in the feeder",
    State.PAPER: "Ready, paper loaded",
    State.SCANNING: "Scanning…",
    "scan": "Scan",
    "open_folder": "Open scans folder ({name})",
    "change_folder": "Change scans folder…",
    "choose": "Choose",
    "show_on_paper": "Show window when paper is loaded",
    "debug": "Debug mode",
    "making": "Making {n} PDF{s}…",
    "making_ready": "Making {n} PDF{s}… Ready to scan again.",
    "finishing": "Finishing {n} PDF{s}, then quitting…",
    "open_debug": "Open debug folder",
    "mode": "Scan mode",
    "quality": "Quality",
    "processing": "Processing",
    "language": "Text language",
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
    "identity_newer": (
        "⚠ {model}, firmware {firmware}: {newer} is out, update with ScanSnap Home"
    ),
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
    State.OFFLINE: "Open the scanner's cover to switch it on.",
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
# The processing steps a user can switch off: Options field, menu title
STEP_TITLES = {
    "straighten": "Straighten and crop to the sheet",
    "upright": "Turn pages upright",
    "remove_blank": "Remove blank pages",
    "remove_show_through": "Remove show-through",
    "clean_up": "Clean up creases and shadows",
    "searchable": "Make searchable",
}


def describe(snapshot: Snapshot) -> tuple[str, str]:
    """The status line and the detail line below it. While PDFs are being
    made, the status line says so, and that the scanner is free again."""
    plural = {"n": snapshot.making, "s": "" if snapshot.making == 1 else "s"}
    if snapshot.making and snapshot.state in (State.READY, State.PAPER):
        return TEXT["making_ready"].format(**plural), TEXT[snapshot.state]
    if snapshot.making:
        detail = TEXT["making"].format(**plural)
    else:
        message, hint = snapshot.message, HINT.get(snapshot.state, "")
        if message and hint:  # what is wrong, then what to do about it
            detail = f"{message.rstrip('.')}. {hint}"
        else:
            detail = message or hint
    return TEXT[snapshot.state], detail


def can_scan(snapshot: Snapshot) -> bool:
    return snapshot.state == State.PAPER


def status_icon(via: Via | None) -> NSImage:
    """ICON_LETTER in ICON_FONT, ICON_HEIGHT tall, as a template image: with
    the symbol of how the scanner is connected (via) at its upper right,
    dimmed while it is not (None). The image is the letter's own outline,
    so macOS has nothing to shrink."""
    on = via is not None

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
        for kind, (name, points) in VIA_SYMBOLS.items()
    }
    mark = NSMakeSize(
        max(symbol.size().width for symbol in symbols.values()),
        max(symbol.size().height for symbol in symbols.values()),
    )
    width = math.ceil(bounds.size.width + mark.width + MARK_GAP + 2 * ICON_MARGIN)
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
        if on:
            symbol = symbols[via].size()
            symbols[via].drawInRect_(
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
    image.setAccessibilityDescription_(f"{APP_NAME}: {VIA_DESCRIPTIONS[via]}")
    return image


def connection(snapshot: Snapshot) -> Via | None:
    """How the scanner is connected, for the icon; None while it is off or
    out of reach."""
    return snapshot.via if snapshot.state in ON_STATES else None


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
    def initWithMonitor_settings_(
        self, watcher: monitor.Monitor, settings: config.Settings
    ):
        self = objc.super(MenuBar, self).init()  # noqa: PLW0642 - PyObjC's init pattern
        self.monitor, self.settings = watcher, settings
        self.seen = Snapshot()  # counters already acted on
        self.quitting = False
        self.icons = {via: status_icon(via) for via in (None, *Via)}
        self.shimmers = {}  # via: the icon's shimmer frames, made when first needed
        self.shown = None  # the icon shown: via, and whether it shimmers
        self.animation = None  # the shimmer's timer, while PDFs are being made
        self.frames, self.frame = [], 0

        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.item.button().setImage_(self.icons[None])
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
        self.steps = self.choices(processing, None, STEP_TITLES, "toggleStep:")
        self.languages = self.choices(
            processing,
            TEXT["language"],
            {
                code: language_title(code)
                for code in menu_languages(settings.options.languages)
            },
            "toggleLanguage:",
        )
        menu.addItem_(NSMenuItem.separatorItem())
        # the app itself
        self.on_paper = self.add(menu, TEXT["show_on_paper"], "toggleShowOnPaper:")
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
            TEXT["open_folder"].format(name=self.settings.out_dir.name)
        )
        options = self.settings.options
        effective = options.in_effect()
        checks = [
            (self.on_paper, self.settings.show_when_paper_loaded),
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
        for step, item in self.steps.items():
            # a step the scan mode rules out shows as off, and cannot be changed
            checks.append((item, getattr(effective, step)))
            item.setEnabled_(getattr(effective, step) == getattr(options, step))
        for item, on in checks:
            item.setState_(NSControlStateValueOn if on else NSControlStateValueOff)
        self.open_debug.setHidden_(not self.settings.debug)
        use_debug_logging(self.settings.debug)

    def refresh_(self, _timer) -> None:
        snapshot = self.monitor.snapshot
        if self.quitting:
            self.finish_quitting(snapshot)
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
        self.show_icon(connection(snapshot), making=snapshot.making > 0)
        loaded = snapshot.paper_loads > self.seen.paper_loads
        self.seen = snapshot
        if loaded and self.settings.show_when_paper_loaded:
            self.show_window()

    @objc.python_method
    def show_icon(self, via: Via | None, *, making: bool) -> None:
        """The icon for how the scanner is connected; shimmering while PDFs
        are being made (a still, faded frame under Reduce Motion)."""
        if self.shown == (via, making):
            return
        self.shown = (via, making)
        if self.animation is not None:
            self.animation.invalidate()
            self.animation = None
        if not making:
            self.item.button().setImage_(self.icons[via])
            return
        if via not in self.shimmers:
            self.shimmers[via] = shimmer_frames(self.icons[via])
        self.frames, self.frame = self.shimmers[via], 0
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

    def toggleLanguage_(self, sender) -> None:
        """Add the language to the text languages, or take it out; the
        last one stays."""
        code = str(sender.representedObject())
        chosen = self.settings.options.languages
        if code not in chosen:
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
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(HOMEPAGE))

    def quit_(self, _sender) -> None:
        """Quit once the scans already taken in have their PDFs."""
        self.window.orderOut_(None)
        self.monitor.stop(QUIT_WAIT_SECONDS)
        self.quitting = True
        self.refresh_(None)

    @objc.python_method
    def finish_quitting(self, snapshot: Snapshot) -> None:
        if snapshot.making:
            self.status_line.setTitle_(
                TEXT["finishing"].format(
                    n=snapshot.making, s="" if snapshot.making == 1 else "s"
                )
            )
            return
        NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
        NSApplication.sharedApplication().terminate_(None)


def menu_languages(chosen: list[str]) -> list[str]:
    """The languages for the menu, by name: the Tesseract languages
    installed that macOS can name, and those already chosen."""
    installed = [code for code in pages.installed_languages() if language_name(code)]
    codes = [*installed, *(code for code in chosen if code not in installed)]
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


def scan_with_settings(device_name: str) -> Callable[[], Path]:
    """Take the sheets in from the scanner device_name; return what makes
    their PDF, with the settings of when they were scanned."""
    settings = config.load()  # the file may have changed since the app started
    keep_raw = config.RAW_DIR if settings.debug else None
    batch = job.scan(settings.options, device_name, keep_raw)
    return lambda: job.finish(batch, settings.out_dir, settings.options)


def use_debug_logging(on: bool) -> None:
    """In debug mode the app logs every decision, edge finding included."""
    logging.getLogger("openscansnap").setLevel(logging.DEBUG if on else logging.INFO)


def main() -> None:
    bundle.use_bundled_tools()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format=LOG_FORMAT)
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
    watcher = monitor.Monitor(monitor.Connector(), scan_with_settings)
    watcher.start()
    bar = MenuBar.alloc().initWithMonitor_settings_(watcher, settings)  # noqa: F841 - keep a reference
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
