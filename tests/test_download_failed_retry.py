import threading
import unittest
from unittest.mock import MagicMock, patch

from app.downloads import manager
from app.downloads.prowlarr import pick_best_result


def _result(title, url, size=100):
    return {
        "title": title,
        "size": size,
        "seeders": 0,
        "leechers": 0,
        "download_url": url,
        "protocol": "usenet",
        "age_minutes": 600,
    }


def _fresh_state(pending=None, failed_releases=None):
    return {
        "running": False,
        "last_run": 0.0,
        "pending": pending or {},
        "completed": set(),
        "completed_identities": set(),
        "duplicates": [],
        "failed_releases": failed_releases or [],
    }


def _snapshot(history_items, active_items=None):
    client_cfg = {"type": "sabnzbd", "url": "http://fixture", "api_key": "k"}
    return {
        "active_by_protocol": {"usenet": {"client_cfg": client_cfg, "items": active_items or []}},
        "completed_by_protocol": {"usenet": {"client_cfg": client_cfg, "items": history_items}},
        "errors_by_protocol": {},
    }


def _pending(nzo_id="nzo_1", name="Example Title Update v1.0.1 NSW-GRP"):
    return {
        "title_id": "0100000000010000",
        "version": 65536,
        "hash": nzo_id,
        "id": nzo_id,
        "expected_name": name,
        "title_name": "Example Title",
        "protocol": "usenet",
        "client_type": "sabnzbd",
        "download_url": "http://fixture/nzb/1",
        "state": "queued",
    }


class PickBestResultExclusionTests(unittest.TestCase):
    def test_skips_release_by_normalized_name(self):
        results = [
            _result("Example Title Update v1.0.1 NSW-GRP", "http://fixture/nzb/1", size=50),
            _result("Example Title Update v1.0.1 NSW-OTHER", "http://fixture/nzb/2", size=500),
        ]
        best = pick_best_result(results, exclude_names={"example title update v1 0 1 nsw grp"})
        self.assertEqual(best["download_url"], "http://fixture/nzb/2")

    def test_skips_release_by_download_url(self):
        results = [_result("Example Title Update v1.0.1 NSW-GRP", "http://fixture/nzb/1")]
        self.assertIsNone(pick_best_result(results, exclude_urls={"http://fixture/nzb/1"}))

    def test_no_exclusions_keeps_results(self):
        results = [_result("Example Title Update v1.0.1 NSW-GRP", "http://fixture/nzb/1")]
        self.assertIsNotNone(pick_best_result(results))


class RetryFailedPendingTests(unittest.TestCase):
    def setUp(self):
        self.state = _fresh_state()
        patches = [
            patch.object(manager, "_state", self.state),
            patch.object(manager, "_state_lock", threading.Lock()),
            patch.object(manager, "_state_loaded", True),
            patch.object(manager, "_persist_downloads_state_locked"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @patch("app.downloads.manager.remove_completed_download", return_value=(True, "ok"))
    def test_failed_job_is_blacklisted_removed_and_dropped(self, remove_mock):
        self.state["pending"]["0100000000010000:65536"] = _pending()
        snapshot = _snapshot([{
            "id": "nzo_1",
            "name": "Example Title Update v1.0.1 NSW-GRP",
            "status": "Failed",
            "state": "failed",
            "state_reason": "Aborted, cannot be completed",
        }])

        retried = manager._retry_failed_pending_downloads({}, snapshot)

        self.assertEqual(retried, 1)
        self.assertEqual(self.state["pending"], {})
        remove_mock.assert_called_once()
        self.assertEqual(remove_mock.call_args.args[2], "nzo_1")
        self.assertTrue(remove_mock.call_args.kwargs["delete_files"])
        entry = self.state["failed_releases"][0]
        self.assertEqual(entry["update_key"], "0100000000010000:65536")
        self.assertEqual(entry["download_url"], "http://fixture/nzb/1")
        self.assertEqual(entry["reason"], "Aborted, cannot be completed")
        self.assertEqual(manager._count_failed_attempts("0100000000010000:65536"), 1)
        names, urls = manager._get_failed_release_exclusions()
        self.assertIn("example title update v1 0 1 nsw grp", names)
        self.assertIn("http://fixture/nzb/1", urls)

    @patch("app.downloads.manager.remove_completed_download", return_value=(True, "ok"))
    def test_completed_and_active_jobs_are_left_alone(self, remove_mock):
        self.state["pending"]["a:1"] = _pending(nzo_id="nzo_done", name="Done Release NSW-GRP")
        self.state["pending"]["b:1"] = _pending(nzo_id="nzo_live", name="Live Release NSW-GRP")
        snapshot = _snapshot(
            [{"id": "nzo_done", "name": "Done Release NSW-GRP", "status": "Completed", "state": "completed"}],
            active_items=[{"id": "nzo_live", "name": "Live Release NSW-GRP", "status": "Downloading"}],
        )

        self.assertEqual(manager._retry_failed_pending_downloads({}, snapshot), 0)
        self.assertEqual(set(self.state["pending"]), {"a:1", "b:1"})
        remove_mock.assert_not_called()

    @patch("app.downloads.manager.remove_completed_download", return_value=(False, "boom"))
    def test_keeps_entry_when_history_removal_fails(self, _remove_mock):
        self.state["pending"]["0100000000010000:65536"] = _pending()
        snapshot = _snapshot([{"id": "nzo_1", "name": "Example Title Update v1.0.1 NSW-GRP", "status": "Failed", "state": "failed"}])

        self.assertEqual(manager._retry_failed_pending_downloads({}, snapshot), 0)
        self.assertIn("0100000000010000:65536", self.state["pending"])
        self.assertEqual(self.state["failed_releases"], [])

    @patch("app.downloads.manager.remove_completed_download", return_value=(True, "ok"))
    def test_duplicate_entries_for_one_failed_job_remove_it_once(self, remove_mock):
        self.state["pending"]["0100000000010000:65536"] = _pending()
        restored = _pending()
        restored.update({"title_id": None, "version": None})
        self.state["pending"]["restored:usenet:sabnzbd:nzo_1"] = restored
        snapshot = _snapshot([{"id": "nzo_1", "name": "Example Title Update v1.0.1 NSW-GRP", "status": "Failed", "state": "failed"}])

        self.assertEqual(manager._retry_failed_pending_downloads({}, snapshot), 2)
        self.assertEqual(self.state["pending"], {})
        remove_mock.assert_called_once()
        self.assertEqual(manager._count_failed_attempts("0100000000010000:65536"), 1)

    def test_skips_protocol_with_poll_errors(self):
        self.state["pending"]["0100000000010000:65536"] = _pending()
        snapshot = _snapshot([{"id": "nzo_1", "name": "x", "status": "Failed", "state": "failed"}])
        snapshot["errors_by_protocol"] = {"usenet": ["completed: timeout"]}

        self.assertEqual(manager._retry_failed_pending_downloads({}, snapshot), 0)
        self.assertIn("0100000000010000:65536", self.state["pending"])


class MaxFailedAttemptsTests(unittest.TestCase):
    def test_bounds_and_default(self):
        self.assertEqual(manager._get_max_failed_attempts({}), 3)
        self.assertEqual(manager._get_max_failed_attempts({"max_failed_attempts": "bad"}), 3)
        self.assertEqual(manager._get_max_failed_attempts({"max_failed_attempts": 0}), 3)
        self.assertEqual(manager._get_max_failed_attempts({"max_failed_attempts": 99}), 20)
        self.assertEqual(manager._get_max_failed_attempts({"max_failed_attempts": 5}), 5)

    @patch("app.downloads.manager._check_completed")
    @patch("app.downloads.manager._search_and_queue")
    @patch("app.downloads.manager._count_failed_attempts", side_effect=lambda key: 3 if key == "A:1" else 0)
    @patch("app.downloads.manager._get_missing_updates", return_value=[
        {"title_id": "A", "title_name": "Example A", "version": 1},
        {"title_id": "B", "title_name": "Example B", "version": 1},
    ])
    @patch("app.downloads.manager._retry_failed_pending_downloads")
    @patch("app.downloads.manager._reconcile_missing_pending_downloads")
    @patch("app.downloads.manager._restore_pending_from_active")
    @patch("app.downloads.manager._get_download_activity_snapshot", return_value={})
    @patch("app.downloads.manager._get_configured_protocols", return_value=["usenet"])
    @patch("app.downloads.manager.ProwlarrClient", MagicMock())
    def test_updates_past_the_limit_are_not_searched(self, *mocks):
        search_mock = mocks[7]
        manager._process_downloads({"prowlarr": {"url": "http://fixture", "api_key": "k"}})
        searched = [call.kwargs["update"]["title_id"] for call in search_mock.call_args_list]
        self.assertEqual(searched, ["B"])


if __name__ == "__main__":
    unittest.main()
