"""Task 8 runtime refresh and executable configuration boundaries."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

import server
from storage_base import StorageUnavailable
from storage_journal import JournalBackend, MILESTONE_KEY, SYNC_PARAMETERS_KEY
from tests.fake_backend import FakeBackend
from tests.fake_object_storage import FakeObjectStorage
from tests.test_storage_journal import journal_blob, milestone_blob, params_blob


async def test_tool_refreshes_before_read(monkeypatch):
    backend = FakeBackend()

    async def refresh():
        backend.add("remote.md", "new remote content")

    monkeypatch.setattr(backend, "refresh", refresh)
    monkeypatch.setattr(server, "backend", backend)
    result = json.loads(await server.obsidian_fetch("remote.md"))
    assert result["content"] == "new remote content"


async def test_refresh_failure_prevents_stale_read(monkeypatch):
    backend = FakeBackend()
    backend.add("remote.md", "stale content")
    backend.errors["refresh"] = StorageUnavailable("https://user:secret@private-endpoint/db")
    monkeypatch.setattr(server, "backend", backend)
    result = await server.obsidian_fetch("remote.md")
    assert result.startswith("Error:")
    assert "stale content" not in result
    assert "private-endpoint" not in result
    assert not any(call[0] == "read_note" for call in backend.calls)


async def test_journal_tool_replays_first_call_and_later_changes(tmp_path, monkeypatch):
    import storage_journal

    clock = [100.0]
    monkeypatch.setattr(storage_journal.time, "monotonic", lambda: clock[0])
    doc = {"_id": "remote.md", "_rev": "1-a", "path": "remote.md",
           "type": "notes", "data": "first", "ctime": 1, "mtime": 1,
           "size": 5}
    remote = FakeObjectStorage({
        MILESTONE_KEY: milestone_blob(),
        SYNC_PARAMETERS_KEY: params_blob(),
        "1-docs.jsonl.gz": journal_blob([doc]),
    })
    backend = JournalBackend(remote, tmp_path, refresh_min_interval=5)
    await backend.startup_check()
    monkeypatch.setattr(server, "backend", backend)
    try:
        assert json.loads(await server.obsidian_fetch("remote.md"))["content"] == "first"
        remote.put("2-docs.jsonl.gz", journal_blob([
            {**doc, "_rev": "2-b", "data": "second", "size": 6,
             "_revisions": {"start": 2, "ids": ["b", "a"]}},
        ]))
        assert json.loads(await server.obsidian_fetch("remote.md"))["content"] == "first"
        clock[0] += 6
        assert json.loads(await server.obsidian_fetch("remote.md"))["content"] == "second"
    finally:
        await backend.close()


@pytest.mark.parametrize("entry", ["run.py", "server.py"])
def test_entry_without_config_exits_cleanly(entry, tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / entry)], cwd=tmp_path,
        env={"PYTHON_DOTENV_DISABLED": "1"}, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "LIVESYNC_BACKEND is required" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_runtime_surfaces_have_no_backend_placeholders():
    """Historical plans and provenance docstrings are outside this criterion."""
    root = Path(__file__).resolve().parents[1]
    for name in ("server.py", "config.py", "README.md", ".env.example", "CHANGELOG.md"):
        text = (root / name).read_text()
        assert "Plan B" not in text, name
        assert "arrives in Plan B" not in text, name
