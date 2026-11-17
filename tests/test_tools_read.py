"""Read tools exercise the real handlers against an isolated vault."""

import hashlib
import os
import secrets
import subprocess
import sys
from types import SimpleNamespace
import json

import pytest

import server

from storage_base import (
    BinaryNoteError, ConcurrentModification, NoteExists, NoteNotFound,
    StorageError, StorageUnavailable, UnsupportedVault,
)
from tests.fake_backend import FakeBackend


@pytest.fixture
def tools(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(server, "backend", fake, raising=False)
    return server, fake


async def test_fetch_returns_raw_content(tools):
    server, fake = tools
    raw = "---\ntags: [one]\ntitle: Example\n---\nBody\n"
    fake.add("Note.md", raw)
    result = json.loads(await server.obsidian_fetch("Note.md"))
    assert result["content"] == raw
    assert result["properties"] == {"tags": ["one"], "title": "Example"}
    fake.add("Empty.md", "")
    assert json.loads(await server.obsidian_fetch("Empty.md"))["properties"] == {}


async def test_fetch_binary_meta(tools):
    server, fake = tools
    fake.add("image.png", data=b"abc")
    result = json.loads(await server.obsidian_fetch("image.png"))
    assert result["path"] == "image.png"
    assert result["size"] == 3
    assert result["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert not {"content", "data", "text"} & result.keys()


async def test_search_isolated(tools):
    server, fake = tools
    fake.add("bad.md", "needle")
    fake.add("good.md", "a Needle here")
    fake.errors[("read_note", "bad.md")] = StorageError("internal URL/rev")
    result = json.loads(await server.obsidian_search("needle"))
    assert result["skipped"] == ["bad.md"]
    assert [n["path"] for n in result["results"]] == ["good.md"]
    assert ("iterate_texts", (".md",)) in fake.calls


async def test_list_notes_recursive_default(tools):
    server, fake = tools
    for path in ("root.md", "projects/a.md", "projects/deep/b.md", "projects2/c.md"):
        fake.add(path, "")
    result = json.loads(await server.obsidian_list_notes("projects"))
    assert [n["path"] for n in result] == ["projects/a.md", "projects/deep/b.md"]
    assert ("list_notes", ("projects", None, True)) in fake.calls


async def test_vault_stats_no_size_crash(tools):
    server, fake = tools
    fake.add("missing.md", "#tag")
    fake.add("none.md", "", size=None)
    del fake.notes["missing.md"]["meta"]["size"]
    result = json.loads(await server.obsidian_vault_stats())
    assert result["total_size_bytes"] == 0
    assert result["total_notes"] == 2
    assert result["total_tags"] == 1


async def test_hub_incoming(tools):
    server, fake = tools
    fake.add("folder/Hub.md", "")
    for i in range(5):
        fake.add(f"{i}.md", "[[Hub]] [[Hub#heading]]")
    fake.add("alone.md", "[[missing]]")
    hubs = json.loads(await server.obsidian_find_hub_notes())
    assert hubs == [{"path": "folder/Hub.md", "incoming": 5, "outgoing": 0, "total": 5}]
    orphans = json.loads(await server.obsidian_find_orphaned_notes())
    assert orphans == ["alone.md"]


async def test_backlinks_finds_anchor_links(tools):
    server, fake = tools
    fake.add("Hub.md", "")
    fake.add("source.md", "[[Hub#Heading|alias]] [link](Hub.md#heading)")
    result = json.loads(await server.obsidian_get_links("Hub.md"))
    assert result["backlinks"] == [{"path": "source.md"}]
    assert result["outgoing"] == []


async def test_refresh_calls_backend_refresh(tools):
    server, fake = tools
    await server.obsidian_refresh()
    assert ("refresh", ()) in fake.calls


async def test_tags_and_graph_isolate_unreadable_notes(tools):
    server, fake = tools
    fake.add("bad.md", None)
    fake.add("good.md", "---\ntags: [one]\n---\n#two [[target]]")
    fake.add("target.md", "")
    tags = json.loads(await server.obsidian_get_all_tags())
    assert tags == {"tags": {"one": 1, "two": 1}, "skipped": ["bad.md"]}
    graph = json.loads(await server.obsidian_get_link_graph())
    assert graph["skipped"] == ["bad.md"]
    assert graph["edges"] == [{"from": "good.md", "to": "target.md"}]


async def test_graph_resolves_unreadable_target(tools):
    module, fake = tools
    fake.add("source.md", "[[target]]")
    fake.add("target.md", "")
    fake.errors[("read_note", "target.md")] = StorageError("unreadable")

    graph = json.loads(await module.obsidian_get_link_graph())
    assert set(graph["nodes"]) == {"source.md", "target.md"}
    assert graph["edges"] == [{"from": "source.md", "to": "target.md"}]
    assert graph["skipped"] == ["target.md"]
    assert json.loads(await module.obsidian_find_orphaned_notes()) == {
        "orphans": [], "skipped": ["target.md"]}


async def test_graph_keeps_unreadable_basename_candidate(tools):
    module, fake = tools
    fake.add("source.md", "[[target]]")
    fake.add("one/target.md", "")
    fake.add("two/target.md", "")
    fake.errors[("read_note", "two/target.md")] = StorageError("unreadable")

    graph = json.loads(await module.obsidian_get_link_graph())
    assert set(graph["nodes"]) == {
        "source.md", "one/target.md", "two/target.md"}
    assert graph["edges"] == []
    assert graph["skipped"] == ["two/target.md"]


async def test_malformed_frontmatter_is_clear_and_isolated(tools):
    server, fake = tools
    fake.add("bad.md", "---\ntags: [unterminated\n---\n#tag")
    result = await server.obsidian_fetch("bad.md")
    assert "frontmatter" in result.lower() and "error" in result.lower()
    tags = json.loads(await server.obsidian_get_all_tags())
    assert tags["skipped"] == ["bad.md"]
    assert "frontmatter" in tags["errors"]["bad.md"].lower()


@pytest.mark.parametrize("error", [StorageError, NoteNotFound, NoteExists,
    BinaryNoteError, ConcurrentModification, UnsupportedVault, StorageUnavailable])
async def test_storage_errors_are_sanitized(tools, error):
    server, fake = tools
    fake.errors["read_note"] = error("https://internal:secret/db _rev=private")
    result = await server.obsidian_fetch("note.md")
    assert "internal" not in result and "private" not in result
    assert "error" in result.lower() or "not found" in result.lower()


async def test_search_regex_and_invalid_pattern(tools):
    server, fake = tools
    fake.add("a.md", "Needle 42")
    assert json.loads(await server.obsidian_search(r"Needle \d+", regex=True))["results"]
    assert "invalid" in (await server.obsidian_search("[", regex=True)).lower()


async def test_get_backend_couchdb():
    from config import CouchDBConfig
    from storage_couchdb import CouchDBBackend
    cfg = SimpleNamespace(backend="couchdb", couchdb=CouchDBConfig(
        url="http://localhost:5984", user="test", password="test", database="vault"))
    backend = server.get_backend(cfg)
    assert isinstance(backend, CouchDBBackend)
    await backend.close()


def test_get_backend_s3_pending():
    with pytest.raises(RuntimeError, match="^S3 backend arrives in Plan B$"):
        server.get_backend(SimpleNamespace(backend="s3"))


async def test_vault_stats_reports_backend(tools):
    module, fake = tools
    fake.add("note.md", "")
    result = json.loads(await module.obsidian_vault_stats())
    assert result["backend"] == "fake"


def _smoke_env(extra: dict) -> dict:
    """Clean environment plus a fully configured couchdb backend."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COUCHDB_", "MCP_", "OAUTH_", "LIVESYNC_"))}
    return {**env, "LIVESYNC_BACKEND": "couchdb",
            "COUCHDB_URL": "http://localhost:5984",
            "COUCHDB_USER": "smoke", "COUCHDB_PASSWORD": "smoke-password",
            "COUCHDB_DATABASE": "vault", **extra}


def test_import_server_smoke_bearer_mode_real_mcp():
    # A1 regression pin: import must stay clean on the real mcp SDK with a
    # throwaway bearer key (32+ chars, never a real secret).
    env = _smoke_env({"MCP_API_KEY": secrets.token_hex(32)})
    result = subprocess.run([sys.executable, "-c", "import server"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_import_server_smoke_oauth_mode_real_mcp():
    env = _smoke_env({"OAUTH_PASSWORD": secrets.token_hex(16),
                      "OAUTH_CLIENT_ID": "smoke-client",
                      "OAUTH_CLIENT_SECRET": secrets.token_hex(32)})
    result = subprocess.run([sys.executable, "-c", "import server"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_import_without_configuration_and_startup_fails_closed():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COUCHDB_", "MCP_", "OAUTH_", "LIVESYNC_"))}
    code = """
import asyncio
import server
from config import ConfigError
assert server.backend is None
async def check():
    assert 'configuration' in await server.obsidian_fetch('note.md')
    try:
        async with server._lifespan(server.mcp):
            raise AssertionError('served without configuration')
    except ConfigError:
        pass
asyncio.run(check())
"""
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def test_lifespan_checks_and_closes_backend(tools, monkeypatch):
    module, fake = tools
    monkeypatch.setattr(module, "load_config", lambda env: object())
    monkeypatch.setattr(module, "AUTH_MODE", "bearer")
    async with module._lifespan(module.mcp):
        assert fake.calls == [("startup_check", ())]
    assert fake.calls[-1] == ("close", ())


async def test_partial_graph_consumers_report_skipped(tools):
    module, fake = tools
    fake.add("bad.md", None)
    fake.add("good.md", "")
    assert json.loads(await module.obsidian_find_orphaned_notes()) == {
        "orphans": ["good.md"], "skipped": ["bad.md"]}
    assert json.loads(await module.obsidian_find_hub_notes()) == {
        "hubs": [], "skipped": ["bad.md"]}


async def test_legacy_canvas_add_can_update_existing_file(tools):
    module, fake = tools
    fake.add("Board.canvas", '{"nodes": [], "edges": []}')
    result = await module.obsidian_canvas_node("Board.canvas", text="hello", node_id="one")
    assert result == "Added text node 'one' to 'Board.canvas'."
    assert json.loads(fake.notes["Board.canvas"]["text"])["nodes"][0]["text"] == "hello"


def test_startup_rejects_auth_added_after_import():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COUCHDB_", "MCP_", "OAUTH_", "LIVESYNC_"))}
    code = """
import asyncio
import os
import server
from config import ConfigError
from tests.fake_backend import FakeBackend
os.environ.update(LIVESYNC_BACKEND='couchdb', COUCHDB_URL='http://localhost:5984',
                  COUCHDB_USER='test', COUCHDB_PASSWORD='test',
                  COUCHDB_DATABASE='vault', MCP_API_KEY='test')
server.backend = FakeBackend()
async def check():
    try:
        async with server._lifespan(server.mcp):
            raise AssertionError('served with authentication unconfigured')
    except ConfigError:
        pass
asyncio.run(check())
"""
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
