"""The window that sets up the scanner's Wi-Fi, over USB.

It opens at once and asks the scanner, on its thread (Monitor.run_on_scanner),
for the networks it sees; the list fills in when the scanner answers. Cancel
closes it at any time; Scan networks and Join wait for the scanner. Join
has the scanner join the chosen network with the password typed, and lets
this Mac scan over Wi-Fi (monitor.set_up_wifi). Every state, waiting and
failures included, shows in the window; opened again while the scanner
works, it shows that work's end.
"""

from concurrent.futures import Future

import objc
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSImage,
    NSMakeRect,
    NSPanel,
    NSPopUpButton,
    NSProgressIndicator,
    NSProgressIndicatorStyleSpinning,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSSecureTextField,
    NSTextField,
    NSTimer,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from . import monitor, saneapi

TEXT = {
    "title": "Set up the scanner's Wi-Fi",
    "scanning": "Scanning for Wi-Fi networks…",
    "choose": "Choose the network the scanner joins, and type its password.",
    "others": (
        "Joining stops other computers from scanning over Wi-Fi until they are "
        "set up again; phones still can."
    ),
    "none": "The scanner sees no Wi-Fi network. It uses 2.4 GHz networks only.",
    "joining": "Joining {name}…",
    "joined": (
        "The scanner is joining {name}. Once its Wi-Fi light is steady, unplug "
        "the USB cable; the app then finds the scanner on the network."
    ),
    "failed": "Cannot set up the scanner's Wi-Fi: {error}",
    "password": "Password",
    "show": "Show password",
    "hide": "Hide password",
    "join": "Join",
    "scan_networks": "Scan networks",
    "open_network": "{name} (no password)",
    "secured_network": "{name} ({security}, {encryption})",
    "cancel": "Cancel",
    "done": "Done",
}
# Layout, in points
WIDTH, HEIGHT, MARGIN, GAP = 440.0, 210.0, 20.0, 10.0
FIELD_HEIGHT, BUTTON_WIDTH, BUTTON_HEIGHT = 26.0, 124.0, 30.0
MESSAGE_HEIGHT = 48.0
SPINNER = 16.0
EYE = 22.0  # the show-password button
EYE_SHOW, EYE_HIDE = "eye", "eye.slash"  # SF Symbols
CHECK_SECONDS = 0.2  # how often the window looks for the scanner's answer


class WifiWindow(NSObject):
    def initWithMonitor_(self, watcher: monitor.Monitor):
        self = objc.super(WifiWindow, self).init()  # noqa: PLW0642 - PyObjC's init pattern
        self.monitor = watcher
        self.found: list[monitor.Network] = []
        self.others_warned = False  # whether the user was told others are forgotten
        self.pending: tuple[Future, object] | None = None  # (future, when done)
        self.timer = None
        self.panel = self.make_panel()
        return self

    @objc.python_method
    def make_panel(self) -> NSPanel:
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_(TEXT["title"])
        panel.setReleasedWhenClosed_(False)
        inner = WIDTH - 2 * MARGIN
        y = HEIGHT - MARGIN - MESSAGE_HEIGHT
        self.message = NSTextField.wrappingLabelWithString_("")
        self.message.setFrame_(
            NSMakeRect(MARGIN + SPINNER + GAP, y, inner - SPINNER - GAP, MESSAGE_HEIGHT)
        )
        self.spinner = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(MARGIN, y + MESSAGE_HEIGHT - SPINNER, SPINNER, SPINNER)
        )
        self.spinner.setStyle_(NSProgressIndicatorStyleSpinning)
        self.spinner.setDisplayedWhenStopped_(False)
        y -= FIELD_HEIGHT + GAP
        self.names = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(MARGIN, y, inner, FIELD_HEIGHT), False
        )
        self.names.setTarget_(self)
        self.names.setAction_("chooseNetwork:")
        y -= FIELD_HEIGHT + GAP
        # the password, hidden or shown: two fields in one place, one visible
        frame = NSMakeRect(MARGIN, y, inner - EYE - GAP, FIELD_HEIGHT - 4)
        self.hidden = NSSecureTextField.alloc().initWithFrame_(frame)
        self.shown = NSTextField.alloc().initWithFrame_(frame)
        self.shown.setHidden_(True)
        for field in (self.hidden, self.shown):
            field.setPlaceholderString_(TEXT["password"])
        self.password = self.hidden
        self.eye = NSButton.buttonWithImage_target_action_(
            self.eye_image(EYE_SHOW), self, "showPassword:"
        )
        self.eye.setBordered_(False)
        self.eye.setFrame_(NSMakeRect(WIDTH - MARGIN - EYE, y, EYE, FIELD_HEIGHT - 4))
        self.eye.setToolTip_(TEXT["show"])
        self.join = self.button(TEXT["join"], "join:", WIDTH - MARGIN - BUTTON_WIDTH)
        self.join.setKeyEquivalent_("\r")
        self.again = self.button(
            TEXT["scan_networks"],
            "scanNetworks:",
            WIDTH - MARGIN - 2 * BUTTON_WIDTH - GAP,
        )
        self.cancel = self.button(TEXT["cancel"], "close:", MARGIN)
        for view in (
            self.message,
            self.spinner,
            self.names,
            self.hidden,
            self.shown,
            self.eye,
            self.join,
            self.again,
            self.cancel,
        ):
            panel.contentView().addSubview_(view)
        return panel

    @objc.python_method
    def button(self, title: str, action: str, x: float) -> NSButton:
        button = NSButton.buttonWithTitle_target_action_(title, self, action)
        button.setFrame_(NSMakeRect(x, MARGIN - 4, BUTTON_WIDTH, BUTTON_HEIGHT))
        return button

    @objc.python_method
    def open(self) -> None:
        """Show the window and scan for networks; while the scanner still
        works on what the window asked, show the window only."""
        self.panel.center()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        if self.pending is None:
            self.scanNetworks_(None)

    @objc.python_method
    def show(self, message: str, *, busy: bool = False, choosing: bool = False) -> None:
        self.message.setStringValue_(message)
        (self.spinner.startAnimation_ if busy else self.spinner.stopAnimation_)(None)
        self.names.setEnabled_(choosing)
        self.join.setEnabled_(choosing)
        self.again.setEnabled_(not busy)
        self.ask_password(choosing)

    @objc.python_method
    def ask_password(self, choosing: bool) -> None:
        """The password fields: enabled while choosing a network that takes
        a password."""
        network = self.chosen() if choosing else None
        wanted = network is not None and not network.open
        for control in (self.hidden, self.shown, self.eye):
            control.setEnabled_(wanted)
        if not wanted:
            for field in (self.hidden, self.shown):
                field.setStringValue_("")

    @objc.python_method
    def chosen(self) -> "monitor.Network | None":
        index = self.names.indexOfSelectedItem()
        return self.found[index] if 0 <= index < len(self.found) else None

    def chooseNetwork_(self, _sender) -> None:
        self.ask_password(True)

    @objc.python_method
    def run(self, job, when_done) -> None:
        """job on the scanner's thread; when_done(future) here once it ends."""
        self.pending = (self.monitor.run_on_scanner(job), when_done)
        if self.timer is None:
            self.timer = (
                NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                    CHECK_SECONDS, self, "check:", None, True
                )
            )
            NSRunLoop.currentRunLoop().addTimer_forMode_(
                self.timer, NSRunLoopCommonModes
            )

    def check_(self, _timer) -> None:
        if self.pending is None or not self.pending[0].done():
            return
        future, when_done = self.pending
        self.pending = None
        self.timer.invalidate()
        self.timer = None
        if future.exception() is not None:
            self.show(TEXT["failed"].format(error=future.exception()))
            return
        when_done(future.result())

    def scanNetworks_(self, _sender) -> None:
        self.cancel.setTitle_(TEXT["cancel"])
        self.show(TEXT["scanning"], busy=True)

        def scan(device):
            self.monitor.refresh_wifi(device)  # who is allowed, as of now
            return monitor.networks(device)

        self.run(scan, self.list_networks)

    @objc.python_method
    def list_networks(self, found: list[monitor.Network]) -> None:
        self.found = found
        self.names.removeAllItems()
        if not found:
            self.show(TEXT["none"])
            return
        # items added to the menu itself stay apart even when titled alike
        menu = self.names.menu()
        for network in found:
            menu.addItemWithTitle_action_keyEquivalent_(title(network, found), None, "")
        wifi = self.monitor.snapshot.wifi
        # the network set up, preselected: never an open one, which anyone
        # can name like it
        current = [
            i
            for i, network in enumerate(found)
            if wifi and network.name == wifi.network and not network.open
        ]
        if current:
            self.names.selectItemAtIndex_(current[0])
        # unknown (None) is told as if it would: set_up_wifi then refuses
        self.others_warned = wifi is not None and wifi.forgets_others is not False
        self.show(
            TEXT["choose"] + (" " + TEXT["others"] if self.others_warned else ""),
            choosing=True,
        )
        self.panel.makeFirstResponder_(self.password)

    def join_(self, _sender) -> None:
        network = self.chosen()
        if network is None:
            return
        secret = str(self.password.stringValue())
        problem = monitor.passphrase_problem(network, secret)
        if problem is not None:
            self.message.setStringValue_(problem)
            return
        for field in (self.hidden, self.shown):
            field.setStringValue_("")
        self.show(TEXT["joining"].format(name=network.name), busy=True)
        others_warned = self.others_warned

        def set_up(device):
            monitor.set_up_wifi(device, network, secret, others_warned=others_warned)
            self.monitor.refresh_wifi(device)

        def joined(_result):
            self.show(TEXT["joined"].format(name=network.name))
            self.cancel.setTitle_(TEXT["done"])
            self.again.setEnabled_(False)

        self.run(set_up, joined)

    def showPassword_(self, _sender) -> None:
        """Show the password typed, or hide it again."""
        showing = self.password is self.hidden
        before, after = (
            (self.hidden, self.shown) if showing else (self.shown, self.hidden)
        )
        after.setStringValue_(before.stringValue())
        before.setStringValue_("")
        before.setHidden_(True)
        after.setHidden_(False)
        self.password = after
        self.eye.setImage_(self.eye_image(EYE_HIDE if showing else EYE_SHOW))
        self.eye.setToolTip_(TEXT["hide" if showing else "show"])
        self.panel.makeFirstResponder_(after)

    @objc.python_method
    def eye_image(self, symbol: str):
        return NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            symbol, TEXT["show"] if symbol == EYE_SHOW else TEXT["hide"]
        )

    def close_(self, _sender) -> None:
        self.panel.orderOut_(None)


def title(network: monitor.Network, found: list[monitor.Network]) -> str:
    """network's title in the list: its name; "no password" for an open
    network, always (anyone can name one like a secured network); and its
    security when another network has the same name."""
    if network.open:
        return TEXT["open_network"].format(name=network.name)
    if sum(other.name == network.name for other in found) == 1:
        return network.name
    return TEXT["secured_network"].format(
        name=network.name,
        security=saneapi.shown(network.security),
        encryption=saneapi.shown(network.encryption),
    )
