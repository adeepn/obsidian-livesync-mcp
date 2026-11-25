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


@pytest.mark.parametrize('route', ['content-type', 'host', 'origin', 'path', 'body', 'tool', 'resource', 'prompt'])
async def test_http_logs_withhold_request_values(monkeypatch, route):
    import logging
    marker = 'PRIVATE_HTTP_MARKER_012345'
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(dict(record.__dict__))

    root = logging.getLogger()
    capture = Capture()
    root.addHandler(capture)
    old = root.level
    root.setLevel(logging.DEBUG)
    try:
        async with http_peer(monkeypatch) as (client, backend, peer):
            headers = {}
            path = '/mcp'
            body = call_body('obsidian_list_notes', {})
            if route == 'content-type':
                headers['Content-Type'] = marker
            elif route in ('host', 'origin'):
                # Exercise real validation although deployment delegates rebinding
                # protection to its authenticated proxy by default.
                from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
                original = TransportSecurityMiddleware.__init__
                def protected(self, settings=None):
                    original(self, TransportSecuritySettings(enable_dns_rebinding_protection=True,
                        allowed_hosts=['localhost:8484'], allowed_origins=['http://localhost:8484']))
                monkeypatch.setattr(TransportSecurityMiddleware, '__init__', protected)
                headers[route] = marker
            elif route == 'path':
                path = '/' + marker
            elif route == 'body':
                body = {'jsonrpc': '2.0', 'id': 1, 'method': marker, 'params': {marker: marker}}
            elif route == 'tool':
                body = call_body(marker, {})
            elif route == 'resource':
                body = {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/read', 'params': {'uri': 'file:///' + marker}}
            elif route == 'prompt':
                body = {'jsonrpc': '2.0', 'id': 1, 'method': 'prompts/get', 'params': {'name': marker}}
            response = await client.post(path, headers=headers, json=body)
            assert response.status_code in (200, 400, 403, 404, 421)
        assert records
        assert not [(r['name'], [k for k, v in r.items() if marker in repr(v)]) for r in records if marker in repr(r)]
        sdk = [r for r in records if r['name'].startswith('mcp.')]
        assert sdk
        for r in sdk:
            assert r['args'] == () and r['exc_info'] is None and r['exc_text'] is None
            assert r['msg'].startswith('sdk_event logger=')
    finally:
        root.removeHandler(capture)
        root.setLevel(old)


@pytest.mark.parametrize('late_handler', [False, True])
def test_sanitized_access_logs_support_installed_uvicorn_formatter(late_handler):
    # Installed uvicorn/logging.py:99-117 destructures access args. Typed
    # filtering must also adapt that formatter instead of breaking hosting.
    import logging
    from uvicorn.logging import AccessFormatter
    from hosting_logging import install_hosting_log_filters
    rendered = []
    class Capture(logging.Handler):
        def emit(self, record):
            rendered.append(self.format(record))
    handler = Capture()
    handler.setFormatter(AccessFormatter('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'))
    logger = logging.getLogger('uvicorn.access')
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        if not late_handler:
            logger.addHandler(handler)
        install_hosting_log_filters()
        if late_handler:
            logger.addHandler(handler)
        logger.info('%s - "%s %s HTTP/%s" %d', 'PRIVATE_CLIENT', 'GET', '/PRIVATE_PATH', '1.1', 200)
        assert len(rendered) == 1
        assert 'sdk_event' in rendered[0]
        assert 'PRIVATE' not in rendered[0]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
