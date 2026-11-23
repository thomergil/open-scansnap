"""The review window: every page of the latest scan, how each is stored, and
a way to store some of them another way and update the PDF
(review.Store.remake).

The pages are listed on the left, as thumbnails, with how each is stored;
shift-click and command-click select several. The large preview shows the
page as the PDF has it now, or as a change would store it. "Store as"
applies to every selected page; the list marks a change until Update PDF
makes it, on the PDF thread (Monitor.queue_pdf). A page left out as blank
is listed too, and can be brought back. A choice changes how the page is
stored; cropping, show-through and the sheet's turn follow the scan's
settings, as the sheet's two sides share them. The window opens after each
scan while the review_after_scan setting is on: its checkbox here, and the
menu's AFTER_SCAN_MENU_TITLE, both set it. The menu's MENU_TITLE opens the
window any time.
"""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

import objc
from AppKit import (
    NSAlert,
    NSAlertSecondButtonReturn,
    NSApplication,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSButton,
    NSButtonTypeSwitch,
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageCell,
    NSImageFrameNone,
    NSImageScaleProportionallyDown,
    NSImageView,
    NSIndexSet,
    NSMakeRect,
    NSMakeSize,
    NSMenuItem,
    NSMutableAttributedString,
    NSPopUpButton,
    NSProgressIndicator,
    NSProgressIndicatorStyleSpinning,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSScrollView,
    NSTableColumn,
    NSTableView,
    NSTableViewSolidHorizontalGridLineMask,
    NSTextField,
    NSTimer,
    NSViewHeightSizable,
    NSViewMaxXMargin,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from . import job, monitor, pages, review
from .options import ScanMode

MENU_TITLE = "Review last scan…"  # the menu item that opens this window
# the menu's checkbox for the review_after_scan setting, which this window's
# checkbox sets too
AFTER_SCAN_MENU_TITLE = "Show review after each scan"
TEXT = {
    "title": "Review {name}",
    "store_as": "Store as:",
    "pages": "Pages of the scan",
    "preview": "The page as it is in the PDF",
    "mixed": "Mixed",
    "one": "Select several pages with Shift-click or ⌘-click to change them together.",
    "many": "Store as applies to all {n} selected pages.",
    "page": "Page {n}",
    "left_out": "Not in the PDF",
    "page_of": "Page {n} of {total}",
    "blank": "Blank",
    "by_choice": "Left out by you",
    "blank_heading": "Not in the PDF: it looked blank",
    "by_choice_heading": "Not in the PDF: you left it out",
    "changed": "→ {then}",
    "shown": "The preview shows the page as it is in the PDF now.",
    "previewing": "Making the page as you chose it…",
    "previewed": "The preview shows your change; click Update PDF to apply it.",
    "previewed_out": "This page will be left out of the PDF when you click Update PDF.",
    "preview_failed": "Could not show your change: {error}",
    "after_scan": "Open this window after each scan",
    "after_scan_note": "You can also turn this on or off in the menu: "
    f"{AFTER_SCAN_MENU_TITLE}.",
    "all_left_out": "Choose how to store at least one page: a PDF needs one.",
    "close": "Close",
    "update": "Update PDF",
    "updating": "Updating the PDF…",
    "updating_sheet": "Updating the PDF… {sheet}/{sheets}",
    "updated": "Updated {name}.",
    "failed": "Could not update the PDF: {error}",
    "behind": "The PDF is updated, but this window could not show it: click Update "
    "PDF again.",
    "discard": "Discard your changes to the pages?",
    "discard_detail": "Changes not yet made with Update PDF are dropped.",
    "discard_button": "Discard",
    "keep_editing": "Keep editing",
}
# How each stored mode reads in the list
STORED = {
    pages.Mode.LINEART: "Black and white",
    pages.Mode.GRAY: "Gray",
    pages.Mode.COLOR: "Color",
}
# The Store as choices: a page's mode, or leaving it out (job.Choice)
CHOICES = {
    ScanMode.AUTO: "Automatic",
    ScanMode.BLACK_AND_WHITE: "Black and white",
    ScanMode.GRAY: "Gray",
    ScanMode.COLOR: "Color",
    ScanMode.PHOTO: "Photo",
    job.LEAVE_OUT: "Leave out",
}
AS_SCANNED_TITLE = "As scanned"  # offered only when the scan was made so
# The popup's two states, never chosen: several pages chosen differently,
# and a page not in the PDF that has no choice of its own
MIXED, OUT = "mixed", "out"
# How each line of a row in the list reads: (point size, color)
ROW_STYLES = {
    "stored": (NSFont.systemFontSize(), NSColor.labelColor),
    "changed": (NSFont.systemFontSize(), NSColor.controlAccentColor),
    "place": (NSFont.smallSystemFontSize(), NSColor.secondaryLabelColor),
}
# Layout, in points
WIDTH, HEIGHT, MARGIN, GAP = 980.0, 720.0, 16.0, 10.0
LIST_WIDTH, THUMB_WIDTH, ROW_HEIGHT = 290.0, 90.0, 112.0
CONTROL_HEIGHT, BUTTON_WIDTH, BUTTON_HEIGHT = 26.0, 120.0, 30.0
BOTTOM = 76.0  # the checkbox, its note and the buttons
SPINNER = 16.0
PREVIEW_SPINNER = 32.0  # over the page while a change is made into a preview
CHECK_SECONDS = (
    0.2  # how often the window checks on a PDF update or a preview being made
)


class ReviewWindow(NSObject):
    def initWithStore_monitor_setAfterScan_(
        self,
        store: review.Store,
        watcher: monitor.Monitor,
        set_after_scan: Callable[[bool], None],
    ):
        self = objc.super(ReviewWindow, self).init()  # noqa: PLW0642 - PyObjC's init pattern
        self.store, self.monitor, self.set_after_scan = store, watcher, set_after_scan
        self.scan: review.Scan | None = None
        self.pending: dict[int, job.Choice | None] = {}  # side: its new choice
        self.thumbs: dict[int, NSImage] = {}
        self.updating: Future | None = None
        # closed by the user (a minimized window is open), from close until
        # shown again
        self.closed = True
        self.progress: tuple[int, int] | None = None  # the update's: sheet, of sheets
        # pages made as a changed choice would store them, one at a time
        self.previews = ThreadPoolExecutor(1, thread_name_prefix="preview")
        self.previewing: Future | None = None  # the page shown, as changed
        self.preview_timer = None
        self.timer = None
        self.window = self.make_window()
        return self

    # --- building the window

    @objc.python_method
    def make_window(self) -> NSWindow:
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered,
            False,
        )
        window.setReleasedWhenClosed_(False)
        window.setMinSize_(NSMakeSize(WIDTH * 0.7, HEIGHT * 0.7))
        window.setDelegate_(self)
        content = window.contentView()
        top = HEIGHT - MARGIN
        list_height = top - BOTTOM - GAP

        # the pages, on the left
        self.table = NSTableView.alloc().initWithFrame_(
            NSMakeRect(0, 0, LIST_WIDTH, list_height)
        )
        thumbs = NSTableColumn.alloc().initWithIdentifier_("thumb")
        thumbs.setWidth_(THUMB_WIDTH)
        cell = NSImageCell.alloc().init()
        cell.setImageScaling_(NSImageScaleProportionallyDown)
        cell.setImageFrameStyle_(NSImageFrameNone)
        thumbs.setDataCell_(cell)
        words = NSTableColumn.alloc().initWithIdentifier_("words")
        words.setWidth_(LIST_WIDTH - THUMB_WIDTH - GAP)
        words.dataCell().setWraps_(True)
        for column in (thumbs, words):
            column.setEditable_(False)
            self.table.addTableColumn_(column)
        self.table.setHeaderView_(None)
        self.table.setRowHeight_(ROW_HEIGHT)
        # each page plainly apart: a line between rows, every other one shaded
        self.table.setGridStyleMask_(NSTableViewSolidHorizontalGridLineMask)
        self.table.setGridColor_(NSColor.separatorColor())
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setAllowsMultipleSelection_(True)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        scroller = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(MARGIN, BOTTOM + GAP, LIST_WIDTH, list_height)
        )
        scroller.setDocumentView_(self.table)
        scroller.setHasVerticalScroller_(True)
        scroller.setAutoresizingMask_(NSViewHeightSizable)

        # the chosen page, on the right
        left = MARGIN + LIST_WIDTH + 2 * GAP
        right = WIDTH - MARGIN - left
        y = top - CONTROL_HEIGHT
        self.heading = self.label("", left, y, right, bold=True)
        self.heading.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        y -= CONTROL_HEIGHT + GAP / 2
        store_as = self.label(TEXT["store_as"], left, y - 3, 70)
        store_as.setAutoresizingMask_(NSViewMinYMargin)
        self.choice = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(left + 74, y, 200, CONTROL_HEIGHT), False
        )
        self.choice.setTarget_(self)
        self.choice.setAction_("choose:")
        self.choice.setAutoresizingMask_(NSViewMinYMargin)
        y -= CONTROL_HEIGHT  # the hint on a line of its own, below
        self.hint = self.label("", left, y, right, small=True)
        self.hint.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        y -= GAP
        self.preview = NSImageView.alloc().initWithFrame_(
            NSMakeRect(left, BOTTOM + GAP + 18, right, y - BOTTOM - GAP - 18)
        )
        self.preview.setImageScaling_(NSImageScaleProportionallyDown)
        self.preview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        # over the page, centered, while a change is made into a preview
        frame = self.preview.frame()
        self.preview_spinner = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(
                frame.origin.x + (frame.size.width - PREVIEW_SPINNER) / 2,
                frame.origin.y + (frame.size.height - PREVIEW_SPINNER) / 2,
                PREVIEW_SPINNER,
                PREVIEW_SPINNER,
            )
        )
        self.preview_spinner.setStyle_(NSProgressIndicatorStyleSpinning)
        self.preview_spinner.setDisplayedWhenStopped_(False)
        self.preview_spinner.setAutoresizingMask_(
            NSViewMinXMargin | NSViewMaxXMargin | NSViewMinYMargin | NSViewMaxYMargin
        )
        # below the preview: what it shows, or how an update goes
        self.status = self.label(TEXT["shown"], left, BOTTOM + GAP, right, small=True)
        self.status.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        self.spinner = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(left - SPINNER - 2, BOTTOM + GAP + 2, SPINNER, SPINNER)
        )
        self.spinner.setStyle_(NSProgressIndicatorStyleSpinning)
        self.spinner.setDisplayedWhenStopped_(False)
        self.spinner.setAutoresizingMask_(NSViewMaxYMargin)

        # the setting and the buttons, along the bottom
        self.after_scan = NSButton.alloc().initWithFrame_(
            NSMakeRect(MARGIN, 40, 420, 20)
        )
        self.after_scan.setButtonType_(NSButtonTypeSwitch)
        self.after_scan.setTitle_(TEXT["after_scan"])
        self.after_scan.setTarget_(self)
        self.after_scan.setAction_("toggleAfterScan:")
        # as wide as the buttons leave
        note_width = WIDTH - 2 * MARGIN - 20 - 2 * BUTTON_WIDTH - 2 * GAP
        note = self.label(
            TEXT["after_scan_note"], MARGIN + 20, 18, note_width, small=True
        )
        self.update = self.button(
            TEXT["update"], "updatePdf:", WIDTH - MARGIN - BUTTON_WIDTH
        )
        self.update.setKeyEquivalent_("\r")
        self.close = self.button(
            TEXT["close"], "close:", WIDTH - MARGIN - 2 * BUTTON_WIDTH - GAP
        )
        self.close.setKeyEquivalent_("\x1b")  # Escape
        for view in (self.update, self.close):
            view.setAutoresizingMask_(NSViewMinXMargin)
        # what VoiceOver says for the parts without a title of their own
        for view, name in (
            (self.table, TEXT["pages"]),
            (self.choice, TEXT["store_as"].rstrip(":")),
            (self.preview, TEXT["preview"]),
        ):
            view.setAccessibilityLabel_(name)
        for view in (
            scroller,
            self.heading,
            store_as,
            self.choice,
            self.hint,
            self.preview,
            self.preview_spinner,  # after the preview: drawn over it
            self.status,
            self.spinner,
            self.after_scan,
            note,
            self.update,
            self.close,
        ):
            content.addSubview_(view)
        return window

    @objc.python_method
    def label(self, text, x, y, width, *, bold=False, small=False) -> NSTextField:
        field = NSTextField.labelWithString_(text)
        field.setFrame_(NSMakeRect(x, y, width, CONTROL_HEIGHT - 6))
        size = NSFont.smallSystemFontSize() if small else NSFont.systemFontSize()
        field.setFont_(
            NSFont.boldSystemFontOfSize_(size)
            if bold
            else NSFont.systemFontOfSize_(size)
        )
        if small:
            field.setTextColor_(NSColor.secondaryLabelColor())
        return field

    @objc.python_method
    def button(self, title: str, action: str, x: float) -> NSButton:
        button = NSButton.buttonWithTitle_target_action_(title, self, action)
        button.setFrame_(NSMakeRect(x, 24, BUTTON_WIDTH, BUTTON_HEIGHT))
        return button

    # --- showing a scan

    @objc.python_method
    def show(self, scan: review.Scan, after_scan: bool) -> None:
        """Show scan's pages from the first, the window in front; nothing
        when scan is no longer in the store. The scan already shown is only
        brought forward, and another replaces unsaved changes only when the
        user agrees. While an update runs, its scan stays: the window is
        only brought forward."""
        showing = self.scan is not None and not self.closed
        if self.updating is None and not (showing and self.scan.name == scan.name):
            if showing and self.has_changes() and not self.discard_changes():
                return
            if not self.load(scan):
                return
            self.show_after_scan(after_scan)
            self.table.selectRowIndexes_byExtendingSelection_(
                NSIndexSet.indexSetWithIndex_(0), False
            )
            self.status.setStringValue_(TEXT["shown"])
        if self.closed:
            self.window.center()
        self.closed = False
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.table)

    @objc.python_method
    def load(self, scan: review.Scan) -> bool:
        """Hold scan and fill the window with it as stored now, keeping the
        pages selected; False, and nothing changed, when scan is no longer
        in the store."""
        held = self.store.hold(scan)
        if held is None:
            return False
        if self.scan is not None and self.scan.name != held.name:
            self.store.release(self.scan)
        self.scan, self.pending, self.thumbs = held, {}, {}
        self.window.setTitle_(TEXT["title"].format(name=held.pdf.name))
        self.fill_choices()
        self.table.reloadData()
        self.update.setEnabled_(False)
        self.show_selection()
        return True

    @objc.python_method
    def has_changes(self) -> bool:
        return self.scan is not None and bool(self.pending)

    @objc.python_method
    def fill_choices(self) -> None:
        self.choice.removeAllItems()
        menu = self.choice.menu()
        for title, value in ((TEXT["mixed"], MIXED), (TEXT["left_out"], OUT)):
            state = menu.addItemWithTitle_action_keyEquivalent_(title, None, "")
            state.setRepresentedObject_(value)
            state.setEnabled_(False)
            state.setHidden_(True)
        offered = dict(CHOICES)
        if self.scan.options.mode == ScanMode.AS_SCANNED:
            offered = {ScanMode.AS_SCANNED: AS_SCANNED_TITLE, **offered}
        for value, title in offered.items():
            if value == job.LEAVE_OUT:
                menu.addItem_(NSMenuItem.separatorItem())
            item = menu.addItemWithTitle_action_keyEquivalent_(title, None, "")
            item.setRepresentedObject_(str(value))
        self.choice.setAutoenablesItems_(False)

    # --- the list

    def numberOfRowsInTableView_(self, _table) -> int:
        return len(self.scan.sides) if self.scan else 0

    def tableView_objectValueForTableColumn_row_(self, _table, column, row):
        side = self.scan.sides[row]
        if column.identifier() == "thumb":
            if side.number not in self.thumbs:
                self.thumbs[side.number] = NSImage.alloc().initWithContentsOfFile_(
                    str(self.scan.thumbnail(side.number))
                )
            return self.thumbs[side.number]
        return styled(self.row_lines(row))

    def tableViewSelectionDidChange_(self, _notification) -> None:
        self.show_selection()

    @objc.python_method
    def row_lines(self, row: int) -> list[tuple[str, str]]:
        """The row's lines, each with its style (ROW_STYLES): how the page is
        stored, the change chosen, and, subtly, its page number."""
        side = self.scan.sides[row]
        page = self.page_number(row)
        lines = [(self.result(side), "stored")]
        if side.number in self.pending:
            then = self.choice_title(self.pending[side.number])
            lines.append((TEXT["changed"].format(then=then), "changed"))
        place = TEXT["page"].format(n=page) if page else TEXT["left_out"]
        lines.append((place, "place"))
        return lines

    @objc.python_method
    def row_text(self, row: int) -> str:
        return "\n".join(line for line, _ in self.row_lines(row))

    @objc.python_method
    def page_number(self, row: int) -> int | None:
        """The page's number in the PDF; None when it is left out."""
        if self.scan.sides[row].mode is None:
            return None
        return sum(1 for side in self.scan.sides[: row + 1] if side.mode is not None)

    @objc.python_method
    def result(self, side: review.Side) -> str:
        """How the side is stored, as the list says it."""
        if side.mode is not None:
            return STORED[side.mode]
        return TEXT["by_choice"] if side.method == job.LEFT_OUT else TEXT["blank"]

    @objc.python_method
    def left_out_heading(self, side: review.Side) -> str:
        """Why the side is not in the PDF, as the preview's heading says it."""
        if side.method == job.LEFT_OUT:
            return TEXT["by_choice_heading"]
        return TEXT["blank_heading"]

    @objc.python_method
    def choice_title(self, choice: job.Choice | None) -> str:
        value = choice if choice is not None else self.scan.options.mode
        return CHOICES.get(value, AS_SCANNED_TITLE)

    @objc.python_method
    def selected(self) -> list[int]:
        """The rows selected; none while no scan is shown."""
        rows = self.table.selectedRowIndexes()
        return [
            row
            for row in range(self.numberOfRowsInTableView_(self.table))
            if rows.containsIndex_(row)
        ]

    @objc.python_method
    def chosen(self, side: review.Side) -> str:
        """The side's choice as the popup names it: its own; else the scan's
        mode, or OUT for a side not in the PDF (picking the scan's mode then
        brings it back)."""
        choice = self.pending.get(side.number, side.choice)
        if choice is not None:
            return str(choice)
        return OUT if side.mode is None else str(self.scan.options.mode)

    @objc.python_method
    def show_selection(self) -> None:
        rows = self.selected()
        if not rows:
            self.choice.setEnabled_(False)
            return
        self.choice.setEnabled_(self.updating is None)
        values = {self.chosen(self.scan.sides[row]) for row in rows}
        shown = MIXED if len(values) > 1 else values.pop()
        for state in (MIXED, OUT):
            item = self.choice.itemAtIndex_(
                self.choice.indexOfItemWithRepresentedObject_(state)
            )
            item.setHidden_(state != shown)
        self.choice.selectItemAtIndex_(
            self.choice.indexOfItemWithRepresentedObject_(shown)
        )
        self.hint.setStringValue_(
            TEXT["one"] if len(rows) == 1 else TEXT["many"].format(n=len(rows))
        )
        focus = self.table.selectedRow()
        side = self.scan.sides[focus]
        page = self.page_number(focus)
        self.heading.setStringValue_(
            TEXT["page_of"].format(n=page, total=self.pages())
            if page
            else self.left_out_heading(side)
        )
        self.show_page(side)

    # --- the preview

    @objc.python_method
    def show_page(self, side: review.Side) -> None:
        """The side in the preview: as the PDF has it, or, when its choice
        was changed, as the change would store it (made in the background,
        a spinner over the page meanwhile)."""
        if self.previewing is not None:
            self.previewing.cancel()  # not wanted now, if not begun
        self.previewing = None
        self.preview_spinner.stopAnimation_(None)
        if side.number not in self.pending or self.updating is not None:
            self.show_image(
                self.scan.page(side.number) or self.scan.side_image(side.number)
            )
            if self.updating is None:
                self.status.setStringValue_(TEXT["shown"])
            return
        pending = self.pending[side.number]  # None: back to the scan's mode
        choice = review.choice_of(pending or str(self.scan.options.mode))
        if choice == job.LEAVE_OUT:
            self.show_image(self.scan.side_image(side.number))
            self.status.setStringValue_(TEXT["previewed_out"])
            return
        self.previewing = self.previews.submit(
            self.store.preview, self.scan, side.number, choice
        )
        self.preview_spinner.startAnimation_(None)
        self.status.setStringValue_(TEXT["previewing"])
        if self.preview_timer is None:
            self.preview_timer = (
                NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                    CHECK_SECONDS, self, "previewCheck:", None, True
                )
            )
            NSRunLoop.currentRunLoop().addTimer_forMode_(
                self.preview_timer, NSRunLoopCommonModes
            )

    def previewCheck_(self, _timer) -> None:
        if self.previewing is not None and not self.previewing.done():
            return
        self.preview_timer.invalidate()
        self.preview_timer = None
        if self.previewing is None:  # another page shown since
            return
        made, self.previewing = self.previewing, None
        self.preview_spinner.stopAnimation_(None)
        if self.updating is not None:  # the update's status stays, and its page
            return
        if made.exception() is not None:
            error = job.describe(made.exception())
            self.status.setStringValue_(TEXT["preview_failed"].format(error=error))
            return
        self.show_image(made.result())
        self.status.setStringValue_(TEXT["previewed"])

    @objc.python_method
    def show_image(self, path) -> None:
        self.preview.setImage_(NSImage.alloc().initWithContentsOfFile_(str(path)))

    @objc.python_method
    def pages(self) -> int:
        return sum(1 for side in self.scan.sides if side.mode is not None)

    # --- choosing

    def choose_(self, sender) -> None:
        value = str(sender.selectedItem().representedObject())
        if value in (MIXED, OUT):
            return
        for row in self.selected():
            side = self.scan.sides[row]
            # picking the scan's own mode clears the side's choice, except
            # where its place in the PDF rests on a choice: a side not in the
            # PDF, or one a choice brought back. There it is a choice of its
            # own, which keeps the side in.
            own = (
                side.mode is None
                or side.choice is not None
                or value != str(self.scan.options.mode)
            )
            choice = value if own else None
            if choice == side.choice:
                self.pending.pop(side.number, None)
            else:
                self.pending[side.number] = choice
        self.table.reloadData()
        self.show_selection()
        self.update.setEnabled_(self.has_changes() and self.updating is None)

    def toggleAfterScan_(self, sender) -> None:
        self.set_after_scan(sender.state() == NSControlStateValueOn)

    @objc.python_method
    def show_after_scan(self, on: bool) -> None:
        """The checkbox as the review_after_scan setting is (set here or in
        the menu)."""
        self.after_scan.setState_(
            NSControlStateValueOn if on else NSControlStateValueOff
        )

    # --- updating the PDF

    def updatePdf_(self, _sender) -> None:
        if not self.has_changes() or self.updating is not None:
            return
        choices = self.scan.choices()
        for number, choice in self.pending.items():
            if choice is None:
                choices.pop(number, None)
            else:
                choices[number] = choice
        # a side with no choice of its own comes out as it did: a remake
        # repeats Automatic's decisions
        if all(
            choices.get(side.number, None if side.mode else job.LEAVE_OUT)
            == job.LEAVE_OUT
            for side in self.scan.sides
        ):
            self.status.setStringValue_(TEXT["all_left_out"])
            return
        scan, store = self.scan, self.store
        self.progress = None  # this update's own, not another PDF's

        def remake(progress: job.Progress):
            def told(sheet: int, sheets: int) -> None:  # to this window and the menu
                self.told(sheet, sheets)
                progress(sheet, sheets)

            return store.remake(scan, choices, told)

        self.updating = self.monitor.queue_pdf(remake)
        self.busy(True)
        self.show_selection()  # the page as stored, not a preview, meanwhile
        self.status.setStringValue_(TEXT["updating"])
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            CHECK_SECONDS, self, "check:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)

    def check_(self, _timer) -> None:
        if self.updating is None:
            return
        if not self.updating.done():
            progress = self.progress
            self.status.setStringValue_(
                TEXT["updating_sheet"].format(sheet=progress[0], sheets=progress[1])
                if progress
                else TEXT["updating"]
            )
            return
        done, self.updating = self.updating, None
        self.timer.invalidate()
        self.timer = None
        self.busy(False)
        if self.closed:  # closed while updating: done with it (minimized is open)
            self.let_go()
            return
        error = done.exception()
        if isinstance(error, job.PdfSaved):  # the changes stay, to update again
            self.status.setStringValue_(TEXT["behind"])
            return
        if error is not None:
            self.status.setStringValue_(
                TEXT["failed"].format(error=job.describe(error))
            )
            return
        self.load(self.scan)  # as stored now, the same pages selected
        self.status.setStringValue_(TEXT["updated"].format(name=done.result().name))

    @objc.python_method
    def told(self, sheet: int, sheets: int) -> None:
        """The update's progress (job.Progress), from the PDF thread."""
        self.progress = (sheet, sheets)

    @objc.python_method
    def busy(self, busy: bool) -> None:
        (self.spinner.startAnimation_ if busy else self.spinner.stopAnimation_)(None)
        self.choice.setEnabled_(not busy)
        self.update.setEnabled_(not busy and self.has_changes())

    # --- closing

    def close_(self, _sender) -> None:
        self.window.performClose_(None)

    def windowShouldClose_(self, _window) -> bool:
        if not self.has_changes() or self.updating is not None:
            return True
        return self.discard_changes()

    @objc.python_method
    def discard_changes(self) -> bool:
        """Whether the user agrees to drop the changes not yet in the PDF."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_(TEXT["discard"])
        alert.setInformativeText_(TEXT["discard_detail"])
        # the safe one first: Return keeps editing, Escape too
        alert.addButtonWithTitle_(TEXT["keep_editing"])
        alert.addButtonWithTitle_(TEXT["discard_button"]).setKeyEquivalent_("d")
        return alert.runModal() == NSAlertSecondButtonReturn

    def windowWillClose_(self, _notification) -> None:
        if self.previewing is not None:
            self.previewing.cancel()  # one begun finishes unseen
            self.previewing = None
        self.preview_spinner.stopAnimation_(None)
        self.closed = True
        self.pending = {}
        if self.scan is not None and self.updating is None:
            self.let_go()

    @objc.python_method
    def let_go(self) -> None:
        """Release the scan and empty the table with it: AppKit may draw the
        table again as the window goes, and a row it asks for must exist."""
        self.store.release(self.scan)
        self.scan, self.pending, self.thumbs = None, {}, {}
        self.table.reloadData()


def styled(lines: list[tuple[str, str]]) -> NSAttributedString:
    """lines (text, ROW_STYLES key) as one text, each line in its style."""
    text = NSMutableAttributedString.alloc().init()
    for index, (line, style) in enumerate(lines):
        size, color = ROW_STYLES[style]
        attributes = {
            NSFontAttributeName: NSFont.systemFontOfSize_(size),
            NSForegroundColorAttributeName: color(),
        }
        piece = line + ("\n" if index < len(lines) - 1 else "")
        text.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(piece, attributes)
        )
    return text
