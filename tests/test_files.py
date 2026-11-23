"""Writing a file whole."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openscansnap import files


class InFolder(unittest.TestCase):
    """A temporary folder to write in."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.path = self.folder / "settings"

    def leftovers(self) -> list[str]:
        return [p.name for p in self.folder.iterdir() if p.name.startswith(".")]


class FilesTest(InFolder):
    def test_replaces_with_the_mode_asked(self):
        self.path.write_text("old")
        self.path.chmod(0o644)
        old_umask = os.umask(0o077)
        try:
            files.replace_file(self.path, b"new", 0o640)
        finally:
            os.umask(old_umask)
        self.assertEqual(self.path.read_bytes(), b"new")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(self.leftovers(), [])

    def test_a_link_is_replaced_not_followed(self):
        target = self.folder / "target"
        target.write_text("someone else's")
        self.path.symlink_to(target)
        files.replace_file(self.path, b"ours", 0o600)
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(target.read_text(), "someone else's")

    def test_a_failed_write_leaves_the_old_file_and_no_temporary(self):
        self.path.write_text("old")
        with (
            mock.patch.object(files.os, "replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            files.replace_file(self.path, b"new", 0o600)
        self.assertEqual(self.path.read_text(), "old")
        self.assertEqual(self.leftovers(), [])


class ReplaceWithTest(InFolder):
    def test_the_new_file_keeps_the_old_ones_permissions_and_tags(self):
        pdf = self.folder / "scan.pdf"
        pdf.write_bytes(b"old")
        pdf.chmod(0o640)  # not what a new temporary file gets (0600)
        tagged = shutil.which("xattr") is not None
        if tagged:
            subprocess.run(["xattr", "-w", "user.tag", "red", str(pdf)], check=True)
        files.replace_with(pdf, lambda path: path.write_bytes(b"new"))
        self.assertEqual(pdf.read_bytes(), b"new")
        self.assertEqual(stat.S_IMODE(pdf.stat().st_mode), 0o640)
        if tagged:
            got = subprocess.run(
                ["xattr", "-p", "user.tag", str(pdf)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(got.stdout.strip(), "red")
        self.assertEqual(self.leftovers(), [])

    def test_metadata_the_volume_refuses_does_not_fail_the_replace(self):
        pdf = self.folder / "scan.pdf"
        pdf.write_bytes(b"old")
        refusing = mock.Mock()
        refusing.copyfile.return_value = -1
        with mock.patch.object(files.ctypes, "CDLL", return_value=refusing):
            files.replace_with(pdf, lambda path: path.write_bytes(b"new"))
        self.assertEqual(pdf.read_bytes(), b"new")

    def test_a_failed_write_leaves_the_old_file(self):
        pdf = self.folder / "scan.pdf"
        pdf.write_bytes(b"old")

        def fail(path):
            path.write_bytes(b"half")
            raise OSError("disk full")

        with self.assertRaises(OSError):
            files.replace_with(pdf, fail)
        self.assertEqual(pdf.read_bytes(), b"old")
        self.assertEqual(self.leftovers(), [])


class PrivateDirTest(InFolder):
    def test_made_or_tightened_to_the_owner_only(self):
        made = files.private_dir(self.folder / "new" / "store")
        self.assertEqual(stat.S_IMODE(made.stat().st_mode), files.PRIVATE_DIR)
        wide = self.folder / "wide"
        wide.mkdir(mode=0o755)
        files.private_dir(wide)
        self.assertEqual(stat.S_IMODE(wide.stat().st_mode), files.PRIVATE_DIR)

    def test_a_parent_that_is_a_link_is_refused_and_nothing_is_made(self):
        target = self.folder / "target"
        target.mkdir()
        link = self.folder / "link"
        link.symlink_to(target)
        with self.assertRaises(PermissionError):
            files.private_dir(link / "store")
        self.assertEqual(list(target.iterdir()), [])

    def test_a_folder_of_another_user_is_refused(self):
        theirs = self.folder / "theirs"
        theirs.mkdir()
        with (
            mock.patch.object(files.os, "getuid", return_value=os.getuid() + 1),
            self.assertRaises(PermissionError),
        ):
            files.private_dir(theirs)

    def test_a_link_is_refused(self):
        target = self.folder / "target"
        target.mkdir()
        link = self.folder / "link"
        link.symlink_to(target)
        with self.assertRaises(PermissionError):
            files.private_dir(link)


if __name__ == "__main__":
    unittest.main()
