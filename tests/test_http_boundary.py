"""Hosting policies; installed mcp/server/fastmcp/server.py:179,985 and
transport_security.py:147-211 own request admission, not vault admission.
Device size predicate: commonlib services/base/VaultService.ts:73-81.
"""
import base64
import hashlib
import json
from contextlib import asynccontextmanager

import httpx
import pytest

import server
from config import ConfigError, load_config
from tests.test_config import COUCHDB_ENV
from tests.fake_backend import FakeBackend


@asynccontextmanager
async def http_peer(monkeypatch, backend=None, **env):
    cfg = load_config({**COUCHDB_ENV, 'MCP_SERVER_URL': 'http://localhost:8484', **env})
    backend = backend or FakeBackend()
    monkeypatch.setattr(server, 'get_backend', lambda cfg: backend)
    peer = server.create_app(cfg)
    peer.settings.stateless_http = True
    peer.settings.json_response = True
    app = peer.streamable_http_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=cfg.server_url, headers={
                'Authorization': 'Bearer ' + cfg.api_key,
                'Accept': 'application/json, text/event-stream'}) as client:
            yield client, backend, peer


def call_body(name, arguments):
    return {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments}}


@pytest.mark.parametrize('mib', [12, 40, 100])
async def test_http_large_publication(monkeypatch, mib):
    data = (b'a' if mib == 12 else b'\x00\xff') * (mib * 1024**2 // (1 if mib == 12 else 2))
    path = 'large.md' if mib == 12 else 'large.bin'
    name = 'obsidian_create_note' if mib == 12 else 'obsidian_upload_attachment'
    args = {'path': path, 'content': data.decode()} if mib == 12 else {
        'path': path, 'content_b64': base64.b64encode(data).decode()}
    async with http_peer(monkeypatch) as (client, backend, peer):
        response = await client.post('/mcp', json=call_body(name, args))
        assert response.status_code == 200
        result = response.json()['result']
        assert not result.get('isError')
        note = await backend.read_note(path)
        actual = note['text'].encode() if note['is_text'] else note['data']
        assert hashlib.sha256(actual).digest() == hashlib.sha256(data).digest()


@pytest.mark.parametrize('limit,status', [('1024', 413), ('0', 200)])
async def test_http_explicit_limit(monkeypatch, limit, status):
    async with http_peer(monkeypatch, LIVESYNC_MAX_REQUEST_BYTES=limit) as (client, backend, peer):
        response = await client.post('/mcp', json=call_body('obsidian_create_note', {
            'path': 'limit.md', 'content': 'x' * 2048}))
        assert response.status_code == status
        assert ('limit.md' in backend.notes) == (status == 200)


def test_request_config():
    assert load_config(COUCHDB_ENV).max_request_bytes == 1024**3
    assert load_config({**COUCHDB_ENV, 'LIVESYNC_MAX_REQUEST_BYTES': '0'}).max_request_bytes == 0
    for value in ['-1', '', '1.5', 'private-value', True, 2.5]:
        with pytest.raises(ConfigError, match='LIVESYNC_MAX_REQUEST_BYTES.*nonnegative integer'):
            load_config({**COUCHDB_ENV, 'LIVESYNC_MAX_REQUEST_BYTES': value})
