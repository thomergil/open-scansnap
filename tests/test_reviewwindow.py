"""The review window: pages listed, chosen alone or together, PDF updated."""

import importlib.util
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

import test_review
from offscreen import Offscreen

from openscansnap import job, pages, review, sane
from openscansnap.monitor import Snapshot
from openscansnap.options import ScanMode

APPKIT = importlib.util.find_spec("AppKit") is not None


class PdfThread:
    """Monitor.queue_pdf, run when the test says: what it queued, and the
    future it handed out."""

    def __init__(self):
        self.queued: list[tuple] = []
        self.told: list[tuple[int, int]] = []  # the progress the menu was given

    def __call__(self, make_pdf):
        future = Future()
        self.queued.append((make_pdf, future))
        return future

    def run(self):
        make_pdf, future = self.queued.pop(0)
        try:
            future.set_result(make_pdf(lambda *sheet: self.told.append(sheet)))
        except Exception as e:  # noqa: BLE001 - handed over, as the monitor does
            future.set_exception(e)


@unittest.skipUnless(APPKIT, "needs PyObjC (macOS)")
class ReviewWindowTest(Offscreen):
    def setUp(self):
        super().setUp()
        from openscansnap import reviewwindow
        from openscansnap.reviewwindow import ReviewWindow

        # out of sight and never taking focus: the tests run no event loop,
        # so a visible window would only spin
        patcher = mock.patch.object(reviewwindow, "NSApplication")
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = review.Store(Path(tmp.name) / "review")
        # three sides: pages 1 and 2 in color, the third left out as blank
        self.scan = test_review.keep(self.store, Path(tmp.name), sides=3)
        self.pdf_thread = PdfThread()
        self.watcher = mock.Mock(snapshot=Snapshot(), queue_pdf=self.pdf_thread)
        self.after_scan = mock.Mock()
        self.window = ReviewWindow.alloc().initWithStore_monitor_setAfterScan_(
            self.store, self.watcher, self.after_scan
        )
        self.window.window.setAlphaValue_(0.0)
        self.window.show(self.scan, True)
        self.addCleanup(self.window.window.orderOut_, None)
        # a change's preview is made by a fake, at once and gated by the test
        # that wants to: no real page work races the test's end
        self.made = self.scan.thumbnail(2)  # a file no page of the PDF is
        self.preview_gate = threading.Event()
        self.preview_gate.set()

        def preview(scan, number, choice):
            self.preview_gate.wait(10)
            return self.made

        patcher = mock.patch.object(self.store, "preview", side_effect=preview)
        self.preview = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.window.previews.shutdown, wait=True)

    def select(self, *rows):
        from AppKit import NSMutableIndexSet

        chosen = NSMutableIndexSet.indexSet()
        for row in rows:
            chosen.addIndex_(row)
        self.window.table.selectRowIndexes_byExtendingSelection_(chosen, False)

    def choose(self, value):
        popup = self.window.choice
        popup.selectItemAtIndex_(popup.indexOfItemWithRepresentedObject_(str(value)))
        self.window.choose_(popup)

    def rows(self) -> list[str]:
        return [self.window.row_text(row) for row in range(len(self.scan.sides))]

    def update(self, remake):
        """Update PDF, with store.remake doing remake; its arguments."""
        self.window.updatePdf_(None)
        self.assertFalse(self.window.update.isEnabled())  # until it is done
        with mock.patch.object(self.store, "remake", side_effect=remake) as made:
            self.pdf_thread.run()
        self.window.check_(None)
        return made.call_args.args

    def test_every_side_is_listed_with_how_it_is_stored(self):
        self.assertEqual(
            self.rows(),
            ["Color\nPage 1", "Color\nPage 2", "Blank\nNot in the PDF"],
        )

    def test_a_choice_applies_to_every_selected_page(self):
        self.select(0, 1)
        self.assertIn("2 selected", self.window.hint.stringValue())
        self.choose(ScanMode.GRAY)
        self.assertEqual(self.window.pending, {1: "gray", 2: "gray"})
        self.assertEqual(
            self.rows()[:2], ["Color\n→ Gray\nPage 1", "Color\n→ Gray\nPage 2"]
        )
        self.assertTrue(self.window.update.isEnabled())

    def test_a_change_is_previewed_with_a_spinner_meanwhile(self):
        self.preview_gate.clear()  # being made
        self.select(0)
        with mock.patch.object(self.window, "show_image") as shown:
            self.choose(ScanMode.GRAY)
            self.assertIsNotNone(self.window.preview_timer)  # spinning until made
            self.assertEqual(
                self.window.status.stringValue(), "Making the page as you chose it…"
            )
            self.window.previewCheck_(None)  # not made yet: still spinning
            self.assertIsNotNone(self.window.preview_timer)
            self.preview_gate.set()
            self.window.previewing.made.result()
            self.window.previewCheck_(None)
        self.assertEqual(self.preview.call_args.args, (self.scan, 1, ScanMode.GRAY))
        shown.assert_called_with(self.made)
        self.assertEqual(
            self.window.status.stringValue(),
            "The preview shows your change; click Update PDF to apply it.",
        )
        self.assertIsNone(self.window.preview_timer)
        self.assertIsNone(self.window.previewing)

    def test_a_preview_for_a_page_no_longer_shown_is_not_shown(self):
        self.preview_gate.clear()
        self.select(0)
        self.choose(ScanMode.GRAY)
        started = self.window.previewing.made
        self.select(2)  # another page, no change on it
        with mock.patch.object(self.window, "show_image") as shown:
            self.preview_gate.set()
            started.result() if not started.cancelled() else None
            self.window.previewCheck_(None)
        shown.assert_not_called()
        self.assertEqual(
            self.window.status.stringValue(),
            "The preview shows the page as it is in the PDF now.",
        )

    def test_a_page_left_out_is_previewed_as_scanned(self):
        self.select(0)
        self.choose(job.LEAVE_OUT)
        self.preview.assert_not_called()
        self.assertIn("left out", self.window.status.stringValue())

    def test_the_change_stays_in_view_while_the_update_runs(self):
        self.preview_gate.clear()  # the preview is still being made
        self.select(0)
        self.choose(ScanMode.GRAY)
        started = self.window.previewing.made
        with mock.patch.object(self.window, "show_image") as shown:
            self.window.updatePdf_(None)
            self.assertIs(self.window.previewing.made, started)  # not dropped
            self.preview_gate.set()
            started.result()
            self.window.previewCheck_(None)
        shown.assert_called_once_with(self.made)  # the change, not the old page
        self.assertIsNone(self.window.previewing)
        self.assertEqual(self.window.status.stringValue(), "Updating the PDF…")

    def test_a_changed_page_selected_during_an_update_shows_the_change(self):
        self.select(0, 1)
        self.choose(ScanMode.GRAY)  # previews the focus: row 1, side 2
        self.window.previewing.made.result()
        self.window.updatePdf_(None)
        with mock.patch.object(self.window, "show_image") as shown:
            self.select(0)  # side 1
            self.assertIsNotNone(self.window.previewing)
            self.window.previewing.made.result()
            self.window.previewCheck_(None)
            self.select(2)  # no change on it, and blank: shown as scanned
        self.assertEqual(self.preview.call_count, 2)
        self.assertEqual(self.preview.call_args.args, (self.scan, 1, ScanMode.GRAY))
        self.assertEqual(
            shown.call_args_list,
            [mock.call(self.made), mock.call(self.scan.side_image(3))],
        )
        self.assertEqual(self.window.status.stringValue(), "Updating the PDF…")

    def test_a_failed_update_keeps_its_error_over_a_late_preview(self):
        failures = {
            "No space left": sane.ScanError("No space left"),
            "click Update PDF again": job.PdfSaved("review behind", Path("x.pdf")),
        }
        for said, failure in failures.items():
            with self.subTest(said=said):
                self.preview_gate.clear()  # the change's preview is still being made
                self.select(0)
                self.choose(ScanMode.GRAY)
                started = self.window.previewing.made

                def fail(scan, choices, progress, failure=failure):
                    raise failure

                self.update(fail)  # refused at once, the preview still being made
                self.preview_gate.set()
                started.result()
                self.window.previewCheck_(None)
                self.assertIn(said, self.window.status.stringValue())
                self.assertEqual(self.window.pending, {1: "gray"})

    def test_an_update_done_while_a_preview_is_made_shows_the_stored_page(self):
        self.preview_gate.clear()
        self.select(0)
        self.choose(ScanMode.GRAY)
        started = self.window.previewing.made
        with mock.patch.object(self.window, "show_image") as shown:
            self.update(lambda scan, choices, progress: scan.pdf)
            self.preview_gate.set()
            started.result() if not started.cancelled() else None
            self.window.previewCheck_(None)
        self.assertNotIn(mock.call(self.made), shown.call_args_list)
        self.assertEqual(shown.call_args, mock.call(self.scan.page(1)))
        self.assertIn("Updated", self.window.status.stringValue())
        self.assertIsNone(self.window.preview_timer)

    def test_a_failed_preview_says_why_and_shows_the_side_as_scanned(self):
        self.preview.side_effect = OSError("disk full")
        self.select(1)
        self.choose(ScanMode.GRAY)
        self.window.previewing.made.exception()
        with mock.patch.object(self.window, "show_image") as shown:
            self.window.previewCheck_(None)
        shown.assert_called_once_with(self.scan.side_image(2))  # not another page's
        self.assertIn("Could not show your change", self.window.status.stringValue())

    def test_a_preview_failed_during_an_update_shows_the_side_as_scanned(self):
        self.preview.side_effect = OSError("disk full")
        self.select(0, 1)
        self.choose(ScanMode.GRAY)
        self.window.updatePdf_(None)
        self.select(1)
        self.window.previewing.made.exception()
        with mock.patch.object(self.window, "show_image") as shown:
            self.window.previewCheck_(None)
        shown.assert_called_once_with(self.scan.side_image(2))
        self.assertEqual(self.window.status.stringValue(), "Updating the PDF…")

    def test_a_preview_is_dropped_when_nothing_is_selected(self):
        self.preview.side_effect = OSError("disk full")
        self.select(0)
        self.choose(ScanMode.GRAY)
        made = self.window.previewing.made
        self.window.table.deselectAll_(None)
        self.assertIsNone(self.window.previewing)
        made.exception() if not made.cancelled() else None
        with mock.patch.object(self.window, "show_image") as shown:
            self.window.previewCheck_(None)
        shown.assert_not_called()  # least of all the last side, sides[-1]

    def test_store_as_stays_off_after_a_failed_update_with_nothing_selected(self):
        self.select(0)
        self.choose(ScanMode.GRAY)
        self.window.updatePdf_(None)
        self.window.table.deselectAll_(None)
        failed = sane.ScanError("No space left")  # no reload: busy alone decides
        with mock.patch.object(self.store, "remake", side_effect=failed):
            self.pdf_thread.run()
        self.window.check_(None)
        self.assertFalse(self.window.choice.isEnabled())

    def test_differing_choices_show_as_mixed(self):
        self.select(0)
        self.choose(job.LEAVE_OUT)
        self.select(0, 1)
        self.assertEqual(self.window.choice.titleOfSelectedItem(), "Mixed")

    def test_choosing_the_scans_own_mode_again_undoes_the_change(self):
        self.select(0)
        self.choose(ScanMode.GRAY)
        self.choose(ScanMode.AUTO)  # the scan's mode
        self.assertEqual(self.window.pending, {})
        self.assertFalse(self.window.update.isEnabled())

    def test_a_blank_side_is_brought_back_in_the_scans_own_mode(self):
        self.select(2)
        self.assertEqual(self.window.choice.titleOfSelectedItem(), "Not in the PDF")
        self.choose(ScanMode.AUTO)  # the scan's mode, chosen for this side
        self.assertEqual(self.window.pending, {3: "auto"})

    def test_update_remakes_the_pdf_with_every_choice(self):
        self.select(0)
        self.choose(job.LEAVE_OUT)
        self.select(2)
        self.choose(ScanMode.COLOR)
        _, choices, _ = self.update(lambda scan, choices, progress: scan.pdf)
        self.assertEqual(choices, {1: job.LEAVE_OUT, 3: ScanMode.COLOR})
        self.assertEqual(self.window.pending, {})
        self.assertIn("Updated", self.window.status.stringValue())

    def test_a_failed_update_says_why_and_keeps_the_changes(self):
        self.select(0)
        self.choose(ScanMode.GRAY)

        def fail(scan, choices, progress):
            raise sane.ScanError("No space left")

        self.update(fail)
        self.assertIn("No space left", self.window.status.stringValue())
        self.assertEqual(self.window.pending, {1: "gray"})

    def test_every_page_left_out_is_refused_before_any_work(self):
        self.select(0, 1)
        self.choose(job.LEAVE_OUT)
        self.window.updatePdf_(None)
        self.assertEqual(self.pdf_thread.queued, [])
        self.assertIn("at least one page", self.window.status.stringValue())

    def test_the_checkbox_sets_review_after_scan(self):
        self.window.after_scan.performClick_(None)
        self.after_scan.assert_called_once_with(False)

    def test_the_scan_is_held_while_shown(self):
        newer = test_review.keep(self.store, self.store.root.parent, "scan_2")
        self.assertEqual(
            {scan.name for scan in self.store.scans()}, {self.scan.name, newer.name}
        )
        self.window.window.performClose_(None)
        self.store.prune()
        self.assertEqual([scan.name for scan in self.store.scans()], [newer.name])

    def test_a_closed_window_has_no_rows_left_to_draw(self):
        # AppKit may draw the table once more as the window goes: a row of a
        # scan let go raised there, and ended the app (2026-11-23)
        self.select(0)
        self.window.window.performClose_(None)
        self.assertIsNone(self.window.scan)
        self.assertEqual(self.window.table.numberOfRows(), 0)

    def test_closed_while_updating_no_rows_are_left_when_done(self):
        self.select(0)
        self.choose(ScanMode.GRAY)
        self.window.updatePdf_(None)
        self.window.window.performClose_(None)
        self.assertEqual(self.window.table.numberOfRows(), 3)  # still held
        with mock.patch.object(self.store, "remake", return_value=self.scan.pdf):
            self.pdf_thread.run()
        self.window.check_(None)
        self.assertEqual(self.window.table.numberOfRows(), 0)

    def test_an_update_done_while_minimized_keeps_the_scan(self):
        # minimized, the window is out of sight but open: a minimized window
        # taken for closed lost its scan, and Update PDF then ended the app
        self.select(0)
        self.choose(ScanMode.GRAY)
        self.window.updatePdf_(None)
        self.window.window.orderOut_(None)  # out of sight, as when minimized
        with mock.patch.object(self.store, "remake", return_value=self.scan.pdf):
            self.pdf_thread.run()
        self.window.check_(None)
        self.assertEqual(self.window.scan.name, self.scan.name)
        self.assertEqual(self.window.table.numberOfRows(), 3)
        self.window.updatePdf_(None)  # nothing left to update: no error

    def test_while_an_update_runs_its_scan_stays_shown(self):
        newer = test_review.keep(self.store, self.store.root.parent, "scan_2")
        self.select(0)
        self.choose(ScanMode.GRAY)
        self.window.updatePdf_(None)
        self.window.show(newer, True)  # a new scan's review, meanwhile
        self.assertEqual(self.window.scan.name, self.scan.name)
        with mock.patch.object(self.store, "remake", return_value=self.scan.pdf):
            self.pdf_thread.run()
        self.window.check_(None)
        self.assertEqual(self.window.scan.name, self.scan.name)
        self.assertIn(self.scan.pdf.name, self.window.status.stringValue())

    def test_showing_another_scan_lets_the_first_go(self):
        newer = test_review.keep(self.store, self.store.root.parent, "scan_2")
        self.window.show(newer, True)
        self.store.prune()
        self.assertEqual({s.name for s in self.store.scans()}, {newer.name})

    def test_closed_while_updating_the_scan_is_let_go_when_done(self):
        newer = test_review.keep(self.store, self.store.root.parent, "scan_2")
        self.select(0)
        self.choose(ScanMode.GRAY)
        self.window.updatePdf_(None)
        self.window.window.performClose_(None)
        with mock.patch.object(self.store, "remake", return_value=self.scan.pdf):
            self.pdf_thread.run()
        self.store.prune()  # still held: the update is not seen through yet
        self.assertEqual(
            {s.name for s in self.store.scans()}, {self.scan.name, newer.name}
        )
        self.window.check_(None)
        self.store.prune()
        self.assertEqual({s.name for s in self.store.scans()}, {newer.name})

    def test_changes_are_not_dropped_without_asking(self):
        from AppKit import NSAlertFirstButtonReturn

        from openscansnap import reviewwindow

        self.select(0)
        self.choose(ScanMode.GRAY)
        with mock.patch.object(reviewwindow, "NSAlert") as alert:
            ask = alert.alloc.return_value.init.return_value
            ask.runModal.return_value = NSAlertFirstButtonReturn  # Keep editing
            self.window.window.performClose_(None)
        self.assertTrue(self.window.window.isVisible())
        self.assertEqual(self.window.pending, {1: "gray"})

    def test_discard_closes_drops_the_changes_and_lets_the_scan_go(self):
        from AppKit import NSAlertFirstButtonReturn

        from openscansnap import reviewwindow

        newer = test_review.keep(self.store, self.store.root.parent, "scan_2")
        self.select(0)
        self.choose(ScanMode.GRAY)
        with mock.patch.object(reviewwindow, "NSAlert") as alert:
            ask = alert.alloc.return_value.init.return_value
            ask.runModal.return_value = NSAlertFirstButtonReturn + 1  # Discard
            self.window.window.performClose_(None)
        self.assertFalse(self.window.window.isVisible())
        self.assertEqual(self.window.pending, {})
        self.store.prune()
        self.assertEqual({s.name for s in self.store.scans()}, {newer.name})

    @unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
    def test_a_page_brought_back_stays_when_automatic_is_picked_for_it(self):
        with mock.patch.object(job, "write_pdf", side_effect=test_review.write_pdf):
            self.store.remake(self.scan, {3: ScanMode.GRAY})  # the blank side back
        self.window.load(self.scan)
        self.assertIsNotNone(self.window.scan.sides[2].mode)
        self.select(2)
        self.choose(ScanMode.AUTO)  # the scan's own mode
        self.assertEqual(self.window.pending, {3: "auto"})  # still its own choice

    def test_an_update_shows_its_own_progress_and_keeps_the_place(self):
        self.select(1)
        self.choose(ScanMode.GRAY)
        self.window.updatePdf_(None)
        self.watcher.snapshot = Snapshot(progress=(7, 20))  # another PDF's
        self.window.check_(None)
        self.assertEqual(self.window.status.stringValue(), "Updating the PDF…")

        shown = []

        def remake(scan, choices, progress):
            progress(1, 2)  # as the store tells it, sheet by sheet
            self.window.check_(None)
            shown.append(str(self.window.status.stringValue()))
            progress(2, 2)
            return scan.pdf

        with mock.patch.object(self.store, "remake", side_effect=remake):
            self.pdf_thread.run()
        self.assertEqual(shown, ["Updating the PDF… 1/2"])
        self.assertEqual(self.pdf_thread.told, [(1, 2), (2, 2)])  # the menu's too
        self.window.check_(None)
        self.assertEqual(self.window.table.selectedRow(), 1)  # not back to page 1
        self.select(0)
        self.choose(ScanMode.GRAY)
        self.window.updatePdf_(None)  # the next update starts afresh
        self.window.check_(None)
        self.assertEqual(self.window.status.stringValue(), "Updating the PDF…")

    def test_the_update_spinner_sits_in_the_page_column_beside_the_status(self):
        # in the gutter, the spinner touched the list's selection border
        status, spinner = self.window.status.frame(), self.window.spinner.frame()
        page = self.window.preview.frame()
        self.select(0)
        self.choose(ScanMode.GRAY)
        self.window.updatePdf_(None)
        busy = self.window.status.frame()
        self.assertEqual(spinner.origin.x, page.origin.x)
        self.assertGreater(busy.origin.x, spinner.origin.x + spinner.size.width)
        self.assertEqual(
            busy.origin.x + busy.size.width, page.origin.x + page.size.width
        )
        with mock.patch.object(self.store, "remake", return_value=self.scan.pdf):
            self.pdf_thread.run()
        self.window.check_(None)
        done = self.window.status.frame()
        self.assertEqual(
            (done.origin.x, done.size.width), (status.origin.x, status.size.width)
        )

    def test_a_pdf_saved_without_its_review_keeps_the_changes(self):
        self.select(0)
        self.choose(ScanMode.GRAY)

        def behind(scan, choices, progress):
            raise job.PdfSaved("its review could not follow", scan.pdf)

        self.update(behind)
        self.assertIn("click Update PDF again", self.window.status.stringValue())
        self.assertEqual(self.window.pending, {1: "gray"})
        self.assertTrue(self.window.update.isEnabled())

    def test_review_last_scan_again_keeps_the_changes(self):
        self.window.window.orderFront_(None)
        self.select(0)
        self.choose(ScanMode.GRAY)
        self.window.show(self.scan, True)  # the same scan: only brought forward
        self.assertEqual(self.window.pending, {1: "gray"})

    def test_a_scan_no_longer_stored_is_not_shown(self):
        newer = test_review.keep(self.store, self.store.root.parent, "scan_2")
        self.window.window.performClose_(None)
        self.store.prune()  # the first scan goes
        self.window.show(self.scan, True)
        self.assertFalse(self.window.window.isVisible())
        self.window.show(newer, True)
        self.assertTrue(self.window.window.isVisible())


if __name__ == "__main__":
    unittest.main()
