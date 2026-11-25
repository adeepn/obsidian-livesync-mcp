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
    ConcurrentModification, NoteExists, NoteNotFound,
    StorageError, StorageUnavailable, UnsupportedVault,
)
from tests.fake_backend import FakeBackend


def _without_scan(result):
    return {key: value for key, value in result.items() if key not in ('scan', 'scope')}


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
    fake.add("image.png", data=b"\xffbc")
    result = json.loads(await server.obsidian_fetch("image.png"))
    assert result["path"] == "image.png"
    assert result["size"] == 3
    assert result["sha256"] == hashlib.sha256(b"\xffbc").hexdigest()
    assert not {"content", "data", "text"} & result.keys()


async def test_search_isolated(tools):
    server, fake = tools
    fake.add("bad.md", "needle")
    fake.add("good.md", "a Needle here")
    fake.errors[("read_note", "bad.md")] = StorageError("internal URL/rev")
    result = json.loads(await server.obsidian_search("needle"))
    assert result["skipped"] == ["bad.md"]
    assert [n["path"] for n in result["results"]] == ["good.md"]
    assert ("iterate_texts", ("",)) in fake.calls


async def test_list_notes_recursive_default(tools):
    server, fake = tools
    for path in ("root.md", "projects/a.md", "projects/deep/b.md", "projects2/c.md"):
        fake.add(path, "")
    result = json.loads(await server.obsidian_list_notes("projects"))["results"]
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
    hubs = json.loads(await server.obsidian_find_hub_notes())["results"]
    assert hubs == [{"path": "folder/Hub.md", "incoming": 5, "outgoing": 0, "total": 5}]
    orphans = json.loads(await server.obsidian_find_orphaned_notes())["results"]
    assert orphans == ["alone.md"]


@pytest.mark.parametrize("link", ["[[Hub#Heading|alias]]", "[link](Hub.md#heading)"])
async def test_backlinks_finds_anchor_links(tools, link):
    server, fake = tools
    fake.add("Hub.md", "")
    fake.add("source.md", link)
    result = json.loads(await server.obsidian_get_links("Hub.md"))
    assert result["backlinks"] == [{"path": "source.md"}]
    assert result["outgoing"] == []


@pytest.mark.parametrize("nfc", [False, True])
async def test_graph_tools_use_backend_case_identity(monkeypatch, nfc):
    from tests.test_storage_couchdb import fake_backend

    docs = {"_local/obsydian_livesync_milestone": {
        "handleFilenameCaseSensitive": True, "pathNormalizationVersion": int(nfc)}}
    async with fake_backend(docs) as (backend, _):
        await backend.startup_check()
        for path, text in [("Source.md", "[[A]] [[a]]"), ("A.md", ""), ("a.md", "")]:
            await backend.write_note(path, text, create=True)
        monkeypatch.setattr(server, "backend", backend, raising=False)
        graph = json.loads(await server.obsidian_get_link_graph())
        assert graph["edges"] == [
            {"from": "Source.md", "to": "A.md"},
            {"from": "Source.md", "to": "a.md"},
        ]
        for path in ("A.md", "a.md"):
            result = json.loads(await server.obsidian_get_links(path))
            assert result["path"] == path
            assert result["backlinks"] == [{"path": "Source.md"}]


async def test_refresh_calls_backend_refresh(tools):
    server, fake = tools
    await server.obsidian_refresh()
    assert fake.calls == [("refresh", (True,))]


async def test_tags_and_graph_isolate_unreadable_notes(tools):
    server, fake = tools
    fake.add("bad.md", None)
    fake.add("good.md", "---\ntags: [one]\n---\n#two [[target]]")
    fake.add("target.md", "")
    tags = json.loads(await server.obsidian_get_all_tags())
    tags.pop("scan")
    assert tags == {"tags": {"one": 1, "two": 1}, "skipped": ["bad.md"]}
    graph = json.loads(await server.obsidian_get_link_graph())
    assert graph["skipped"] == ["bad.md"]
    assert graph["edges"] == [{"from": "good.md", "to": "target.md"}]
    assert json.loads(await server.obsidian_get_links("target.md"))["skipped"] == ["bad.md"]
    assert json.loads(await server.obsidian_vault_stats())["skipped"] == ["bad.md"]


async def test_graph_resolves_unreadable_target(tools):
    module, fake = tools
    fake.add("source.md", "[[target]]")
    fake.add("target.md", "")
    fake.errors[("read_note", "target.md")] = StorageError("unreadable")

    graph = json.loads(await module.obsidian_get_link_graph())
    assert set(graph["nodes"]) == {"source.md", "target.md"}
    assert graph["edges"] == [{"from": "source.md", "to": "target.md"}]
    assert graph["skipped"] == ["target.md"]
    assert _without_scan(json.loads(await module.obsidian_find_orphaned_notes())) == {
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
    ConcurrentModification, UnsupportedVault, StorageUnavailable])
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
        url="http://localhost:5984", user="test", password="test", database="vault"), scan_max_notes=10000, scan_max_bytes=67108864, text_scan_max_bytes=8388608, text_verdict_cache_entries=4096, sync_max_size_mb=None)
    backend = server.get_backend(cfg)
    assert isinstance(backend, CouchDBBackend)
    await backend.close()


async def test_get_backend_s3(tmp_path):
    from config import load_config
    from storage_journal import JournalBackend
    from object_storage import Boto3ObjectStorage

    cfg = load_config({
        "LIVESYNC_BACKEND": "s3",
        "LIVESYNC_S3_URI": "sls://AK:SK@https://minio.local?bucket=vault&region=auto",
        "S3_STATE_DIR": str(tmp_path / "state"),
        "S3_REFRESH_MIN_INTERVAL_SEC": "2.5",
        "S3_STATE_SIZE_WARN_MB": "17",
        "MCP_API_KEY": "test-token",
    })
    backend = server.get_backend(cfg)
    assert isinstance(backend, JournalBackend)
    assert isinstance(backend._object_storage, Boto3ObjectStorage)
    assert backend._state_dir == tmp_path / "state"
    assert backend.refresh_min_interval == 2.5
    assert backend.state_size_warn_mb == 17
    assert backend._store is None
    assert not (tmp_path / "state").exists()
    await backend.close()


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


def test_import_does_not_construct_backend():
    # Import must leave the backend unconstructed even with a fully valid
    # environment: only the lifespan builds it, from startup-time config.
    env = _smoke_env({"MCP_API_KEY": secrets.token_hex(32)})
    code = "import server\nassert server.backend is None, type(server.backend)"
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_import_quiets_httpx_request_logging():
    # httpx logs every request line (full internal CouchDB URLs) at
    # INFO; importing the server must pin httpx/httpcore to WARNING.
    env = _smoke_env({"MCP_API_KEY": secrets.token_hex(32)})
    code = "\n".join([
        "import logging, server",
        "assert logging.getLogger('httpx').level == logging.WARNING",
        "assert logging.getLogger('httpcore').level == logging.WARNING",
    ])
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def test_lifespan_uses_factory_config(monkeypatch):
    cfg = server.load_config()
    fresh = FakeBackend()
    received = []

    def fake_get_backend(config):
        received.append(config)
        return fresh

    app = server.create_app(cfg)
    monkeypatch.setattr(server, "get_backend", fake_get_backend)
    # Mutation after construction must affect neither auth nor backend config.
    monkeypatch.setenv("COUCHDB_DATABASE", "different-vault")
    monkeypatch.setenv("MCP_API_KEY", "different-key")
    async with app.settings.lifespan(app):
        assert received == [cfg]
        assert fresh.calls == [("startup_check", ())]
    assert fresh.calls[-1] == ("close", ())


def test_import_without_configuration_fails_closed():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COUCHDB_", "MCP_", "OAUTH_", "LIVESYNC_"))}
    result = subprocess.run([sys.executable, "-c", "import server"], env=env,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "ConfigError" in result.stderr and "LIVESYNC_BACKEND" in result.stderr


async def test_lifespan_checks_and_closes_backend(tools, monkeypatch):
    module, fake = tools
    app = module.create_app(module.load_config())
    monkeypatch.setattr(module, "get_backend", lambda cfg: fake)
    async with app.settings.lifespan(app):
        assert fake.calls == [("startup_check", ())]
    assert fake.calls[-1] == ("close", ())


async def test_partial_graph_consumers_report_skipped(tools):
    module, fake = tools
    fake.add("bad.md", None)
    fake.add("good.md", "")
    assert _without_scan(json.loads(await module.obsidian_find_orphaned_notes())) == {
        "orphans": ["good.md"], "skipped": ["bad.md"]}
    assert _without_scan(json.loads(await module.obsidian_find_hub_notes())) == {
        "hubs": [], "skipped": ["bad.md"]}


async def test_legacy_canvas_add_can_update_existing_file(tools):
    module, fake = tools
    fake.add("Board.canvas", '{"nodes": [], "edges": []}')
    result = await module.obsidian_canvas_node("Board.canvas", text="hello", node_id="one")
    assert result == "Added text node 'one' to 'Board.canvas'."
    assert json.loads(fake.notes["Board.canvas"]["text"])["nodes"][0]["text"] == "hello"


async def test_default_lifespan_closes_on_startup_failure(monkeypatch, caplog):
    fake = FakeBackend()
    fake.errors["startup_check"] = StorageUnavailable()
    monkeypatch.setattr(server, "get_backend", lambda cfg: fake)
    with pytest.raises(RuntimeError, match="Application failed; inspect typed logs") as caught:
        async with server.mcp.settings.lifespan(server.mcp):
            pytest.fail("served after startup failure")
    assert server.backend is None
    assert fake.calls[-1] == ("close", ())
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert 'cause: StorageUnavailable' in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_regex_worker_tool_timeout_and_normal(tools, monkeypatch):
    from tests.regex_doubles import timeout_for
    server, fake = tools
    fake.add('a.md', 'a' * 1024 + '!')
    timeout_for(monkeypatch, lambda pattern, text, op: pattern == '(a+)+$')
    assert 'regex pattern too complex' in await server.obsidian_search('(a+)+$', True)
    assert 'regex pattern too complex' in await server.obsidian_update_note(
        'a.md', 'search-replace', search='(a+)+$', replace='x', regex=True)
    assert json.loads(await server.obsidian_search('a+', True))['results']
    assert '1 matches replaced' in await server.obsidian_update_note(
        'a.md', 'search-replace', search='a+', replace='x', regex=True)
    assert fake.notes['a.md']['text'] == 'x!'

@pytest.mark.parametrize('tool', ['obsidian_search', 'obsidian_get_all_tags',
                                  'obsidian_get_link_graph', 'obsidian_get_links',
                                  'obsidian_vault_stats', 'obsidian_find_hub_notes',
                                  'obsidian_find_orphaned_notes'])
async def test_scan_budget_reports_partial_result(tools, monkeypatch, tool):
    server, fake = tools
    monkeypatch.setattr(server, '_active_scan_limits',
                        server.ContextVar('test_limits', default=(2, 4096)))
    for i in range(8):
        fake.add(f'{i}.md', '#tag [[0]] needle')
    args = {'obsidian_search': ('needle',), 'obsidian_get_links': ('0.md',)}
    result = json.loads(await getattr(server, tool)(*args.get(tool, ())))
    assert result['truncated'] is True
    assert result['scan']['notes'] <= 2
    assert result['scan']['bytes'] <= 4096


async def test_scan_byte_budget_and_concurrency(tools, monkeypatch):
    import asyncio
    server, fake = tools
    monkeypatch.setattr(server, '_active_scan_limits',
                        server.ContextVar('test_limits', default=(10000, 1000)))
    fake.add('large.md', 'x' * 2000)
    result = json.loads(await server.obsidian_search('x'))
    assert result['truncated'] is True
    assert result['scan']['bytes'] <= 1000
    active = peak = 0
    both_entered = asyncio.Event()
    original = fake.iterate_texts
    async def delayed(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if active == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), 30)
            async for item in original(*args, **kwargs):
                yield item
        finally:
            active -= 1
    monkeypatch.setattr(fake, 'iterate_texts', delayed)
    await asyncio.gather(*(server.obsidian_search('x') for _ in range(4)))
    assert peak == 2


async def test_graph_drops_bodies_before_reading_next(tools, monkeypatch):
    import weakref
    server, fake = tools
    for i in range(30):
        fake.add(f'{i}.md', '')
    class Body(str):
        pass
    previous = None
    async def texts():
        nonlocal previous
        for i in range(30):
            if previous is not None:
                assert previous() is None, 'previous body retained'
            body = Body('[[0]]' + ' ' * 10000)
            previous = weakref.ref(body)
            yield fake.notes[f'{i}.md']['meta'], body
            del body
    monkeypatch.setattr(fake, 'iterate_texts', texts)
    result = json.loads(await server.obsidian_get_link_graph())
    assert len(result['nodes']) == 30
    assert len(result['edges']) == 30


async def test_regex_invalid_on_empty_vault(tools):
    server, _ = tools
    assert 'Invalid regular expression' in await server.obsidian_search('[', True)


async def test_truncated_graph_does_not_label_unread_bodies_orphaned(tools, monkeypatch):
    server, fake = tools
    fake.add('a.md', 'x' * 2000)
    fake.add('b.md', '[[a]]')
    monkeypatch.setattr(server, '_active_scan_limits',
                        server.ContextVar('test_limits', default=(10000, 1000)))
    result = json.loads(await server.obsidian_find_orphaned_notes())
    assert result['truncated'] is True
    assert result['orphans'] == []
    assert result['skipped'] == []
    assert result['scope'] == 'scanned pages only'


def test_app_factory_uses_injected_config_and_isolates_backends():
    env = _smoke_env({'MCP_API_KEY': 'import-key-for-test'})
    code = """
import asyncio
from dataclasses import replace
import server
from config import load_config
from tests.fake_backend import FakeBackend
cfg = load_config()
bearer_cfg = replace(cfg, api_key='injected-bearer', host='127.0.0.2', port=9191)
oauth_cfg = replace(cfg, api_key='', oauth_password='injected-password',
                    oauth_client_id='injected-client', oauth_client_secret='injected-secret',
                    server_url='https://factory.test')
a = server.create_app(bearer_cfg)
b = server.create_app(oauth_cfg)
assert a.settings.host == '127.0.0.2' and a.settings.port == 9191
assert a._auth_server_provider is None
assert b._auth_server_provider is not None
assert str(b.settings.auth.issuer_url).startswith('https://factory.test')
async def check():
    assert await a._token_verifier.verify_token('injected-bearer') is not None
    assert await a._token_verifier.verify_token(cfg.api_key) is None
    stores = [FakeBackend(), FakeBackend()]
    stores[0].add('n.md', 'first')
    stores[1].add('n.md', 'second')
    stores[1].index_truncated = True
    received = []
    def get_backend(config):
        received.append(config)
        return stores[len(received)-1]
    server.get_backend = get_backend
    async with a.settings.lifespan(a), b.settings.lifespan(b):
        assert received == [bearer_cfg, oauth_cfg]
        for app, expected in ((a, 'first'), (b, 'second')):
            tools = await app.list_tools()
            assert any(t.name == 'obsidian_fetch' for t in tools)
            result = await app.call_tool('obsidian_fetch', {'path': 'n.md'})
            assert expected in str(result), result
        result = await b.call_tool('obsidian_get_link_graph', {})
        assert 'truncated' in str(result), result
    assert all(s.calls[-1] == ('close', ()) for s in stores)
asyncio.run(check())
"""
    result = subprocess.run([sys.executable, '-c', code], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_server_has_no_direct_environment_reads():
    from pathlib import Path
    assert 'os.environ' not in Path(server.__file__).read_text()
    assert 'os.getenv' not in Path(server.__file__).read_text()


@pytest.mark.parametrize('name', ['obsidian_list_notes', 'obsidian_list_templates',
                                  'obsidian_vault_stats'])
async def test_capped_listings_report_counts(tools, monkeypatch, name):
    module, fake = tools
    for i in range(3):
        fake.add(f'templates/{i}.md', 'body')
    monkeypatch.setattr(module, '_active_scan_limits',
                        module.ContextVar('test_limits', default=(2, 4096)))
    result = json.loads(await getattr(module, name)())
    assert result['truncated'] is True
    assert result['returned'] == 2
    assert result['total'] == 3


@pytest.mark.parametrize('name', ['obsidian_list_notes', 'obsidian_list_templates'])
async def test_uncapped_listings_add_scan_envelope(tools, name):
    module, fake = tools
    fake.add('templates/a.md', 'body')
    result = json.loads(await getattr(module, name)())
    assert isinstance(result['results'], list)
    assert result['scan']['stopped_because'] is None


async def test_factory_scan_limits_are_frozen_and_reach_backend(monkeypatch):
    from dataclasses import replace
    from config import load_config
    cfg = replace(load_config(), scan_max_notes=2, scan_max_bytes=4096)
    real = server.get_backend(cfg)
    assert real._scan_limits == (2, 4096)
    await real.close()
    fake = FakeBackend()
    for i in range(3):
        fake.add(f'{i}.md', 'body')
    monkeypatch.setattr(server, 'get_backend', lambda config: fake)
    app = server.create_app(cfg)
    async with app.settings.lifespan(app):
        for value in ('10000', 'invalid'):
            monkeypatch.setenv('LIVESYNC_SCAN_MAX_NOTES', value)
            result = await app.call_tool('obsidian_list_notes', {})
            # FastMCP returns content blocks for this string-valued tool.
            payload = json.loads(result[1]["result"])
            assert payload['truncated'] is True
            assert payload['returned'] == 2
            assert payload['scan']['max_notes'] == 2
            assert payload['scan']['max_bytes'] == 4096


@pytest.mark.parametrize('name', ['obsidian_list_notes', 'obsidian_list_templates'])
@pytest.mark.parametrize('cap', ['bytes', 'index'])
async def test_capped_empty_listing_does_not_claim_absence(tools, monkeypatch, name, cap):
    module, fake = tools
    if cap == 'bytes':
        fake.add('templates/a.md', 'body')
        monkeypatch.setattr(module, '_active_scan_limits',
                            module.ContextVar('test_limits', default=(2, 1)))
    else:
        # A capped index omitted the requested folder entirely.
        fake.add('other/a.md', 'body')
        fake.index_truncated = True
    kwargs = {'folder': 'templates'}
    response = await getattr(module, name)(**kwargs)
    result = json.loads(response)
    assert result['truncated'] is True
    assert result['results'] == []
    assert result['returned'] == 0
    assert result['total'] == (1 if cap == 'bytes' else 0)
    if cap == 'index':
        assert result['total_is_lower_bound'] is True
    assert 'results truncated' in result['message'].lower()
    assert 'narrow' in result['message'].lower()
    assert 'No notes found' not in response
    assert 'No templates found' not in response


async def test_fetch_couchdb_does_not_scan_index_for_refresh(monkeypatch):
    from tests.test_storage_couchdb import fake_backend

    doc = {"_id": "note.md", "path": "Note.md", "type": "plain",
           "children": ["h:inline"], "eden": {"h:inline": "hello"},
           "ctime": 1, "mtime": 2, "size": 5}
    async with fake_backend({"note.md": doc}) as (backend, log):
        monkeypatch.setattr(server, "backend", backend)
        result = json.loads(await server.obsidian_fetch("Note.md"))
        assert result["content"] == "hello"
        assert not any(r.url.path.endswith("/_all_docs") for r in log)
        log.clear()
        assert await server.obsidian_refresh() == "Vault refreshed."
        assert any(r.method == "GET" and r.url.path.endswith("/_all_docs") for r in log)


@pytest.mark.parametrize("threshold, size, warning", [
    (0, 2 * 1024 * 1024, False),
    (1, 1024 * 1024, False),
    (1, 1024 * 1024 + 1, True),
    (2, 1024 * 1024, False),
])
async def test_vault_stats_local_state(tmp_path, monkeypatch, threshold, size, warning):
    from tests.test_storage_journal import make_backend, healthy_bucket
    from journal_store import JournalStore
    backend, _ = make_backend(tmp_path, healthy_bucket(), state_size_warn_mb=threshold)
    await backend.startup_check()
    calls = []
    def measure(store):
        calls.append(store)
        return size
    monkeypatch.setattr(JournalStore, "state_size_bytes", measure)
    monkeypatch.setattr(server, "backend", backend)
    try:
        result = json.loads(await server.obsidian_vault_stats())
        local = result["local_state"]
        assert local["bytes"] == size
        assert local["path"] == "<value hidden>"
        assert local["warn_threshold_mb"] == threshold
        assert local["warning"] is warning
        assert "MiB" in local["human_readable_size"]
        assert ("hint" in result) is warning
        assert len(calls) == 1
    finally:
        await backend.close()
