"""Writing a file whole."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openscansnap import files


class FilesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.path = self.folder / "settings"

    def leftovers(self) -> list[str]:
        return [p.name for p in self.folder.iterdir() if p.name.startswith(".")]

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


if __name__ == "__main__":
    unittest.main()
