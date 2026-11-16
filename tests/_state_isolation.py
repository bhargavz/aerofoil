"""Keep tests away from the live data/downloads_state.json.

app.downloads.manager persists its queue state to DATA_DIR. Without this, tests that
queue or complete fake downloads write their fixtures into a running install's state.
Call isolate_downloads_state() from setUpModule and restore it in tearDownModule.
"""
import os
import shutil
import tempfile

import app.downloads.manager as downloads_manager

_saved = {}


def isolate_downloads_state():
    if _saved:
        return
    tmp_dir = tempfile.mkdtemp(prefix="aerofoil-test-state-")
    _saved["file"] = downloads_manager._DOWNLOADS_STATE_FILE
    _saved["dir"] = tmp_dir
    downloads_manager._DOWNLOADS_STATE_FILE = os.path.join(tmp_dir, "downloads_state.json")


def restore_downloads_state():
    if not _saved:
        return
    downloads_manager._DOWNLOADS_STATE_FILE = _saved.pop("file")
    shutil.rmtree(_saved.pop("dir"), ignore_errors=True)
