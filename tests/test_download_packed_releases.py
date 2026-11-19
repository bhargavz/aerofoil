import unittest
from unittest import mock

from app.downloads import manager, usenet_client
from app.downloads.update_selection import NZB_UPDATE_SELECTION_ERROR

from _state_isolation import isolate_downloads_state, restore_downloads_state


def setUpModule():
    isolate_downloads_state()


def tearDownModule():
    restore_downloads_state()


OBFUSCATED = [
    {"filename": "98b9e0.par2", "nzf_id": "a"},
    {"filename": "98b9e0.part01.rar", "nzf_id": "b"},
    {"filename": "98b9e0.part02.rar", "nzf_id": "c"},
    {"filename": "98b9e0.vol00+01.par2", "nzf_id": "d"},
]


class PackedReleaseTests(unittest.TestCase):
    def test_is_packed_release(self):
        self.assertTrue(usenet_client._is_packed_release([f["filename"] for f in OBFUSCATED]))
        self.assertTrue(usenet_client._is_packed_release(["x.r00", "x.rar", "x.sfv"]))
        self.assertTrue(usenet_client._is_packed_release(["x.7z.001"]))
        self.assertFalse(usenet_client._is_packed_release(["Game [0100][v65536].nsp", "x.rar"]))
        self.assertFalse(usenet_client._is_packed_release(["x.nfo", "x.sfv"]))

    @mock.patch.object(usenet_client, "_delete_job_files")
    @mock.patch.object(usenet_client, "_get_job_files", return_value=OBFUSCATED)
    def test_packed_update_job_keeps_every_file(self, _files, delete_files):
        ok, message = usenet_client._restrict_job_to_matching_update_files("http://sab", "k", "nzo1")
        self.assertTrue(ok)
        self.assertIsNone(message)
        delete_files.assert_not_called()

    @mock.patch.object(usenet_client, "_delete_job_files")
    @mock.patch.object(usenet_client, "_get_job_files", return_value=OBFUSCATED)
    def test_packed_dlc_job_keeps_every_file(self, _files, delete_files):
        ok, _ = usenet_client._restrict_job_to_matching_dlc_files("http://sab", "k", "nzo1")
        self.assertTrue(ok)
        delete_files.assert_not_called()

    @mock.patch.object(
        usenet_client,
        "_get_job_files",
        return_value=[{"filename": "Other Game [0100000000001000][v131072].nsp", "nzf_id": "a"}],
    )
    def test_unpacked_job_without_match_still_fails(self, _files):
        ok, message = usenet_client._restrict_job_to_matching_update_files(
            "http://sab", "k", "nzo1", expected_version=65536
        )
        self.assertFalse(ok)
        self.assertEqual(message, NZB_UPDATE_SELECTION_ERROR)


    @mock.patch.object(usenet_client.time, "sleep")
    @mock.patch.object(usenet_client, "_delete_job_files", return_value=True)
    def test_waits_for_slow_nzb_fetch(self, _delete, _sleep):
        calls = {"n": 0}

        def files(*_a, **_k):
            calls["n"] += 1
            return OBFUSCATED if calls["n"] > 20 else []

        with mock.patch.object(usenet_client, "_get_job_files", side_effect=files):
            ok, _ = usenet_client._restrict_job_to_matching_update_files("http://sab", "k", "nzo1")
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 21)


class SelectionFailureBlacklistTests(unittest.TestCase):
    def setUp(self):
        manager._ensure_downloads_state_loaded()
        with manager._state_lock:
            manager._state["pending"] = {}
            manager._state["completed"] = set()
            manager._state["failed_releases"] = []

    @mock.patch.object(manager, "_get_known_update_version", return_value=0)
    @mock.patch.object(manager, "_build_queries", return_value=["Game"])
    @mock.patch.object(manager, "queue_download", return_value=(False, NZB_UPDATE_SELECTION_ERROR, None))
    def test_release_with_no_matching_update_is_blacklisted(self, _queue, _queries, _known):
        client = mock.Mock()
        client.search.return_value = [{
            "title": "Game.Update.v1.1.NSW-GRP",
            "download_url": "http://prowlarr/dl/9",
            "protocol": "usenet",
        }]
        update = {"title_id": "0100000000001000", "version": 65536, "title_name": "Game"}
        with mock.patch.object(manager, "pick_best_result", side_effect=lambda results, **kw: results[0]), \
             mock.patch.object(manager, "_get_protocol_client_cfg", return_value={"type": "sabnzbd"}):
            ok, message = manager._search_and_queue(
                client, update, {}, [], [], [], [], 0, 0, 10, allowed_protocols=["usenet"]
            )
        self.assertFalse(ok)
        self.assertEqual(message, NZB_UPDATE_SELECTION_ERROR)
        failed = manager._state["failed_releases"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "Game.Update.v1.1.NSW-GRP")
        self.assertEqual(failed[0]["update_key"], "0100000000001000:65536")
        names, urls = manager._get_failed_release_exclusions()
        self.assertIn("http://prowlarr/dl/9", urls)


class RestoreRaceTests(unittest.TestCase):
    def setUp(self):
        manager._ensure_downloads_state_loaded()
        with manager._state_lock:
            manager._state["pending"] = {}
            manager._state["completed"] = set()

    def test_track_pending_replaces_restored_entry_for_same_job(self):
        with manager._state_lock:
            manager._state["pending"]["restored:usenet:sabnzbd:nzo-1"] = {
                "id": "nzo-1", "hash": "nzo-1", "protocol": "usenet", "client_type": "sabnzbd",
            }
        update = {"title_id": "0100000000001000", "version": 65536, "title_name": "Game"}
        manager._track_pending("0100000000001000:65536", update, "nzo-1", protocol="usenet", client_type="sabnzbd")
        self.assertEqual(list(manager._state["pending"]), ["0100000000001000:65536"])

    def test_restore_skips_jobs_still_fetching_their_nzb(self):
        snapshot = {
            "active_by_protocol": {"usenet": {"items": [
                {"id": "nzo-2", "hash": "nzo-2", "protocol": "usenet", "client_type": "sabnzbd",
                 "status": "Grabbing", "name": "Trying to fetch NZB from http://x/4/download"},
            ]}},
            "completed_by_protocol": {},
        }
        with mock.patch.object(manager, "_get_completed_poll_targets", return_value=[("usenet", {"type": "sabnzbd"})]):
            restored = manager._restore_pending_from_active({}, snapshot=snapshot)
        self.assertEqual(restored, 0)
        self.assertEqual(manager._state["pending"], {})


if __name__ == "__main__":
    unittest.main()
