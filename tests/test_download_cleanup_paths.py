import os
import tempfile
import unittest

from app.downloads import manager


from _state_isolation import isolate_downloads_state, restore_downloads_state


def setUpModule():
    isolate_downloads_state()


def tearDownModule():
    restore_downloads_state()


class CleanupDownloadPathTests(unittest.TestCase):
    def test_path_is_within_handles_other_drive(self):
        self.assertTrue(manager._path_is_within(r"K:\Games\roms\x", r"K:\Games\roms"))
        # os.path.commonpath raises ValueError across drives; this must not.
        self.assertFalse(manager._path_is_within(r"F:\Downloads\aerofoil\x", r"K:\Games\roms"))

    def test_cleanup_removes_leftover_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "dl", "Some Update NSW-VENOM")
            os.makedirs(src)
            for name in ("a.nfo", "a.sfv"):
                open(os.path.join(src, name), "w").close()
            lib = os.path.join(tmp, "lib")
            os.makedirs(lib)
            manager._cleanup_download_path(src, lib)
            self.assertFalse(os.path.exists(src))

    def test_cleanup_never_touches_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = os.path.join(tmp, "lib")
            inner = os.path.join(lib, "Game [0100]")
            os.makedirs(inner)
            open(os.path.join(inner, "g.nsp"), "w").close()
            manager._cleanup_download_path(inner, lib)
            self.assertTrue(os.path.exists(inner))


if __name__ == "__main__":
    unittest.main()
