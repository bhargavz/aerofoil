import os
import tempfile
import unittest
from unittest import mock

from app.downloads import manager

from _state_isolation import isolate_downloads_state, restore_downloads_state


def setUpModule():
    isolate_downloads_state()


def tearDownModule():
    restore_downloads_state()


KEY = "01005CF01E784000:2162688"


def _info():
    return {
        "title_id": "01005CF01E784000",
        "version": 2162688,
        "protocol": "usenet",
        "client_type": "sabnzbd",
        "expected_name": "TMNT.Update.v1.1.1.NSW-VENOM",
        "download_url": "http://prowlarr/dl/1",
    }


class StaleReleaseTests(unittest.TestCase):
    def setUp(self):
        manager._ensure_downloads_state_loaded()
        with manager._state_lock:
            manager._state["pending"] = {KEY: _info()}
            manager._state["completed"] = set()
            manager._state["failed_releases"] = []
            manager._state["duplicates"] = []
        self.match = {"id": "nzo1", "name": "TMNT.Update.v1.1.1.NSW-VENOM", "path": r"F:\Downloads\aerofoil\x"}
        self.bucket = {"items": [self.match], "matched_ids": set(), "client_cfg": {"type": "sabnzbd"}}
        patches = [
            mock.patch.object(manager, "_match_completed_item", return_value=self.match),
            mock.patch.object(manager, "_resolve_completed_update_info", side_effect=lambda info, m: info),
            mock.patch.object(manager, "remove_completed_download", return_value=(True, "")),
            mock.patch.object(manager, "_cleanup_refused_download"),
            mock.patch.object(manager, "_persist_downloads_state_locked"),
        ]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def _run(self, reason):
        with mock.patch.object(manager, "_move_completed_with_reason", return_value=(None, reason)):
            with manager._state_lock:
                info = manager._state["pending"][KEY]
                return manager._process_tracked_completed_item_locked(KEY, info, self.bucket)

    def test_refused_release_is_blacklisted_but_not_counted(self):
        self._run("duplicate update: 01005CF01E784000 v131072 not newer than v2031616")
        names, urls = manager._get_failed_release_exclusions()
        self.assertIn(manager.normalize_release_name(self.match["name"]), names)
        self.assertEqual(manager._count_failed_attempts(KEY), 0)
        self.assertNotIn(KEY, manager._state["pending"])

    def test_unimportable_extracts_archives_first(self):
        with mock.patch.object(manager, "_extract_archives", return_value=True) as extract:
            self._run("no importable files found")
        extract.assert_called_once()
        self.assertIn(KEY, manager._state["pending"])
        self.assertTrue(manager._state["pending"][KEY]["extract_attempted"])
        self.assertEqual(manager._state["failed_releases"], [])

    def test_unimportable_without_archives_gives_up(self):
        with mock.patch.object(manager, "_extract_archives", return_value=False):
            self._run("no update file found")
        self.assertNotIn(KEY, manager._state["pending"])
        self.assertEqual(manager._count_failed_attempts(KEY), 1)
        manager.remove_completed_download.assert_called_once()
        manager._cleanup_refused_download.assert_called_once_with(self.match["path"])

    def test_unimportable_after_extraction_gives_up(self):
        with manager._state_lock:
            manager._state["pending"][KEY]["extract_attempted"] = True
        with mock.patch.object(manager, "_extract_archives") as extract:
            self._run("no importable files found")
        extract.assert_not_called()
        self.assertNotIn(KEY, manager._state["pending"])


class ArchiveHelperTests(unittest.TestCase):
    def test_first_volumes_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.rar", "a.r00", "b.part01.rar", "b.part02.rar", "c.nfo"):
                open(os.path.join(tmp, name), "w").close()
            found = sorted(os.path.basename(p) for p in manager._first_archive_volumes(tmp))
            self.assertEqual(found, ["a.rar", "b.part01.rar"])

    def test_extract_without_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(manager._extract_archives(tmp))


if __name__ == "__main__":
    unittest.main()
