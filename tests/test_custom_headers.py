"""G5: utils.ts:459-469; MinioStorageAdapter.ts:54-100 (pinned commonlib)."""
import json
from dataclasses import replace
from io import BytesIO, StringIO
from pathlib import Path

import httpx
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import EndpointConnectionError
from dotenv import dotenv_values

from config import ConfigError, load_config
from tests.test_config import COUCHDB_ENV
from tests.test_object_storage import make_cfg
from object_storage import Boto3ObjectStorage
from storage_couchdb import CouchDBBackend
from tests.couchdb_fake import make_fake_couchdb

FIXTURE = json.loads(Path('tests/fixtures/headers_upstream.json').read_text())


def env_for(backend):
    if backend == 'couchdb':
        return dict(COUCHDB_ENV)
    return dict(LIVESYNC_BACKEND='s3', LIVESYNC_S3_URI='sls://ak:sk@https://objects.example?bucket=vault&region=us-east-1', MCP_API_KEY='test-key')


@pytest.mark.parametrize('backend', ['couchdb', 's3'])
@pytest.mark.parametrize('vector', FIXTURE['vectors'])
def test_text_parser_upstream(backend, vector):
    # Exact outputs from common/utils.ts:459-469, including colon truncation.
    cfg = load_config({**env_for(backend), backend.upper()+'_CUSTOM_HEADERS': vector['input']})
    assert dict(getattr(cfg, backend).custom_headers) == vector['expected']


@pytest.mark.parametrize('backend', ['couchdb', 's3'])
def test_json_and_dotenv(backend):
    variable = backend.upper()+'_CUSTOM_HEADERS'
    text = 'X-Proxy: first\nX-Proxy: last\nX-Other: value'
    parsed = dotenv_values(stream=StringIO(variable+'="'+text.replace('\n', '\\n')+'"'))
    a = load_config({**env_for(backend), **parsed})
    b = load_config({**env_for(backend), variable: '{"X-Proxy":"last","X-Other":"value"}'})
    assert getattr(a, backend).custom_headers == getattr(b, backend).custom_headers
    assert 'last' not in repr(getattr(a, backend))
    assert getattr(load_config(env_for(backend)), backend).custom_headers == ()


@pytest.mark.parametrize('backend', ['couchdb', 's3'])
@pytest.mark.parametrize('value,rule,ordinal', [
    ('Good: ok\nBad name: PRIVATE', 'field name', 2),
    ('{"Good":"ok","Bad":"PRIVATE\\r"}', 'control', 2),
    ('{"Bad":"PRIVATE\\n"}', 'control', 1),
    ('{"Bad":"PRIVATE\\u0000"}', 'control', 1),
    ('{"Bad":"PRIVATE\\t"}', 'control', 1),
    ('{"Bad":"PRIVATE\\u007f"}', 'control', 1),
    ('{"Bad":12}', 'string', 1),
    ('{"Bad":"PRIVATE","Bad":"again"}', 'duplicate', 2),
    ('{"Bad":', 'JSON', 1),
    ('["PRIVATE"]', 'JSON object', 1),
])
def test_validation_never_echoes_headers(backend, value, rule, ordinal):
    variable = backend.upper()+'_CUSTOM_HEADERS'
    with pytest.raises(ConfigError) as caught:
        load_config({**env_for(backend), variable:value})
    message = str(caught.value)
    assert variable in message and f'entry {ordinal}' in message and rule in message
    assert 'PRIVATE' not in message and 'Bad' not in message


@pytest.mark.parametrize('value,expected', [('true', True), ('false', False)])
def test_addressing_config(value, expected):
    # common/models/setting.const.defaults.ts:210; MinioStorageAdapter.ts:61-67.
    assert load_config({**env_for('s3'), 'S3_FORCE_PATH_STYLE':value}).s3.force_path_style is expected
    assert load_config(env_for('s3')).s3.force_path_style is True


@pytest.mark.parametrize('value', ['', '1', 'yes', 'TRUE', ' false '])
def test_addressing_invalid(value):
    with pytest.raises(ConfigError, match='S3_FORCE_PATH_STYLE.*true or false'):
        load_config({**env_for('s3'), 'S3_FORCE_PATH_STYLE':value})


async def test_couchdb_all_routes_headers_and_origin():
    transport, requests = make_fake_couchdb({})
    backend = CouchDBBackend('https://db.example/proxy', 'u', 'p', 'vault', transport=transport,
                            custom_headers=(('X-Proxy','credential'),))
    try:
        for method, path in [('GET','vault'), ('GET','vault/note'), ('PUT','vault/note'),
                             ('GET','vault/note/attachment'), ('GET','vault/_local/device'),
                             ('POST','vault/_all_docs'), ('DELETE','vault/note')]:
            try:
                await backend._request(method, path, json_body={} if method in ('PUT','POST') else None)
            except Exception:
                pass  # The fake need not implement each route to record its wire request.
        assert len(requests) == 7
        assert all(r.headers['X-Proxy'] == 'credential' for r in requests)
        assert all(r.url.host == 'db.example' and r.url.path.startswith('/proxy/') for r in requests)
        with pytest.raises(Exception):
            await backend._request('GET', 'https://other.example/vault')
        assert len(requests) == 7
    finally:
        await backend.close()


class Raw(BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.body = body
    def stream(self, amt=None, decode_content=False):
        yield self.body


@pytest.mark.parametrize('style', [True, False])
def test_s3_signed_wire_all_operations(style, monkeypatch):
    # MinioStorageAdapter.ts:61-100: endpoint style + finalizeRequest override.
    cfg = replace(make_cfg(endpoint='https://objects.example'), force_path_style=style,
                  custom_headers=(('X-Proxy','credential'), ('Authorization','proxy-auth')))
    storage = Boto3ObjectStorage(cfg)
    seen = []
    signed = []
    storage._client.meta.events.register('before-send.s3', lambda request, **kw: signed.append(dict(request.headers)))
    def send(request):
        seen.append(request)
        body = b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>' if '?list-type=' in request.url else (b'data' if request.method == 'GET' else b'')
        return AWSResponse(request.url, 200, {'content-length':str(len(body))}, Raw(body))
    monkeypatch.setattr(storage._client._endpoint.http_session, 'send', send)
    assert storage.get('key') == b'data'
    storage.put('key', b'data')
    assert storage.put_if_absent('key', b'data') == 'created'
    assert storage.exists('key')
    assert storage.list() == []
    # Same exists+put calls used by the journal's B-10 fallback.
    storage.exists('fallback'); storage.put('fallback', b'data')
    assert len(seen) == 7
    from urllib.parse import urlsplit
    for request, prepared in zip(seen, signed):
        auth = next(v for k, v in prepared.items() if k.lower() == 'authorization')
        assert auth.startswith(b'AWS4-HMAC-SHA256')
        assert not any(k.lower() == 'x-proxy' for k in prepared)
        assert request.headers['X-Proxy'] == b'credential'
        assert request.headers['Authorization'] == b'proxy-auth'
        assert urlsplit(request.url).hostname == ('objects.example' if style else 'test-bucket.objects.example')
    assert 'If-None-Match' in seen[2].headers


async def test_couchdb_error_hides_header(monkeypatch, caplog):
    import server
    secret = 'G5PrivateProxyCredential'
    def fail(request):
        raise httpx.ConnectError(secret, request=request)
    backend = CouchDBBackend('https://db.example', 'u','p','vault', transport=httpx.MockTransport(fail), custom_headers=(('X-Proxy',secret),))
    monkeypatch.setattr(server, 'backend', backend)
    try:
        result = await server.obsidian_vault_stats()
        assert secret not in result + caplog.text
    finally:
        await backend.close()


def test_s3_error_hides_header(monkeypatch, caplog):
    from storage_base import StorageError
    secret = 'G5PrivateProxyCredential'
    storage = Boto3ObjectStorage(replace(make_cfg(), custom_headers=(('X-Proxy',secret),)))
    def fail(request):
        raise EndpointConnectionError(endpoint_url=secret)
    monkeypatch.setattr(storage._client._endpoint.http_session, 'send', fail)
    for action in [lambda:storage.get('key'), lambda:storage.put('key',b'x'), lambda:storage.put_if_absent('key',b'x'), lambda:storage.exists('key'), lambda:storage.list()]:
        with pytest.raises(StorageError) as caught:
            action()
        assert secret not in str(caught.value) + caplog.text


async def test_header_observability_and_factory(monkeypatch, tmp_path):
    import server
    from storage_journal import JournalBackend
    from tests.fake_object_storage import FakeObjectStorage
    transport, requests = make_fake_couchdb({})
    cfg = load_config({**COUCHDB_ENV, 'COUCHDB_CUSTOM_HEADERS':'X-Proxy: private'})
    backend = server.get_backend(cfg)
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(base_url='https://db.example/', transport=transport)
    try:
        info = await backend.backend_info()
        assert info['custom_headers_configured'] is True
        assert type(info['custom_headers_count']) is int and info['custom_headers_count'] == 1
        assert requests[-1].headers['X-Proxy'] == 'private'
        monkeypatch.setattr(server, 'backend', backend)
        result = json.loads(await server.obsidian_vault_stats())
        assert result['custom_headers_configured'] is True
        assert result['custom_headers_count'] == 1
        assert 'X-Proxy' not in json.dumps(info) and 'private' not in json.dumps(result)
    finally:
        await backend.close()
    objects = FakeObjectStorage()
    objects.custom_headers_count = 2
    journal = JournalBackend(objects, tmp_path)
    try:
        info = await journal.backend_info()
        assert info['custom_headers_configured'] is True and info['custom_headers_count'] == 2
        monkeypatch.setattr(server, 'backend', journal)
        # Avoid startup: only the statistics projection is under test here.
        async def notes(*args, **kwargs): return []
        async def tags(*args, **kwargs): return {'tags':[], 'skipped':0}
        monkeypatch.setattr(journal, 'list_notes', notes)
        monkeypatch.setattr(server, '_tags', tags)
        monkeypatch.setattr(journal, 'refresh', notes)
        result = json.loads(await server.obsidian_vault_stats())
        assert result['custom_headers_count'] == 2
    finally:
        await journal.close()


async def test_couchdb_redirect_does_not_forward_headers():
    requests = []
    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={'Location':'https://other.example/vault'})
    backend = CouchDBBackend('https://db.example','u','p','vault', custom_headers=(('X-Proxy','private'),), transport=httpx.MockTransport(redirect))
    try:
        await backend._request('GET','vault')
        assert len(requests) == 1 and requests[0].url.host == 'db.example'
    finally:
        await backend.close()


def test_s3_default_path_wire(monkeypatch):
    storage = Boto3ObjectStorage(make_cfg(endpoint='https://objects.example'))
    seen = []
    def send(request):
        seen.append(request)
        return AWSResponse(request.url, 200, {}, Raw(b''))
    monkeypatch.setattr(storage._client._endpoint.http_session, 'send', send)
    storage.exists('key')
    assert seen[0].url == 'https://objects.example/test-bucket/testprefix/key'


@pytest.mark.parametrize('operation', ['get', 'put', 'put_if_absent', 'exists', 'list'])
def test_s3_debug_error_never_emits_credentials(operation, monkeypatch, caplog):
    import logging
    from storage_base import StorageError
    secret = 'G5PrivateProxyCredential'
    storage = Boto3ObjectStorage(replace(make_cfg(), custom_headers=(('X-Proxy',secret),)))
    def fail(request):
        raise EndpointConnectionError(endpoint_url=secret)
    monkeypatch.setattr(storage._client._endpoint.http_session, 'send', fail)
    with caplog.at_level(logging.DEBUG), pytest.raises(StorageError) as caught:
        getattr(storage, operation)(*({'get':('k',), 'put':('k',b'x'), 'put_if_absent':('k',b'x'), 'exists':('k',), 'list':()}[operation]))
    assert secret not in str(caught.value) + caplog.text


@pytest.mark.parametrize('value', [True, -1, 'secret', 2**63])
async def test_count_projection_rejects_untyped(value, monkeypatch):
    import server
    from tests.fake_backend import FakeBackend
    backend = FakeBackend()
    async def info():
        return {'backend':'couchdb', 'custom_headers_count':value, 'custom_headers_configured':'secret'}
    backend.backend_info = info
    monkeypatch.setattr(server, 'backend', backend)
    result = json.loads(await server.obsidian_vault_stats())
    assert result['custom_headers_count'] == 0
    assert result['custom_headers_configured'] is False


@pytest.mark.parametrize('vector', FIXTURE['merge_vectors'])
async def test_couchdb_appends_custom_headers_like_upstream(vector):
    # RemoteService.ts:360-369 appends, including Authorization; normal
    # replication differs from StreamingFetch.ts:281-283's set operation.
    requests = []
    def send(request):
        requests.append(request)
        return httpx.Response(200, json={})
    backend = CouchDBBackend('https://db.example','u','p','vault',
                            custom_headers=tuple(vector['custom'].items()), transport=httpx.MockTransport(send))
    # The fixture's Basic credential is an input to the actual upstream merge.
    backend._auth_header = vector['base']['Authorization']
    try:
        await backend._request('POST' if 'Content-Type' in vector['base'] else 'GET', 'vault',
                               json_body={} if 'Content-Type' in vector['base'] else None)
        for name,value in vector['expected'].items():
            assert requests[0].headers[name] == value
    finally:
        await backend.close()


@pytest.mark.parametrize('backend', ['couchdb','s3'])
def test_non_bytestring_value_names_real_transport_constraint(backend):
    variable = backend.upper()+'_CUSTOM_HEADERS'
    with pytest.raises(ConfigError, match=variable+r' entry 1:.*Latin-1.*ASCII'):
        load_config({**env_for(backend), variable:'{"X-Proxy":"\u0100"}'})


@pytest.mark.parametrize('backend', ['couchdb','s3'])
def test_json_syntax_error_entry_ordinal(backend):
    variable = backend.upper()+'_CUSTOM_HEADERS'
    with pytest.raises(ConfigError, match=variable+' entry 2:.*JSON'):
        load_config({**env_for(backend), variable:'{"X-First":"ok", "X-Secret": PRIVATE}'})


@pytest.mark.parametrize('backend', ['couchdb','s3'])
@pytest.mark.parametrize('control', [chr(i) for i in range(32)] + [chr(i) for i in range(127,160)])
def test_all_http_value_controls_are_rejected(backend, control):
    variable = backend.upper()+'_CUSTOM_HEADERS'
    with pytest.raises(ConfigError, match=variable+' entry 1:.*control'):
        load_config({**env_for(backend), variable:json.dumps({'X-Proxy':'left'+control+'right'})})


async def test_s3_transport_error_tool_boundary(monkeypatch, caplog, tmp_path):
    import logging
    import server
    from storage_journal import JournalBackend
    secret = 'G5PrivateProxyCredential'
    storage = Boto3ObjectStorage(replace(make_cfg(), custom_headers=(('X-Proxy',secret),)))
    def fail(request):
        raise EndpointConnectionError(endpoint_url=secret)
    monkeypatch.setattr(storage._client._endpoint.http_session, 'send', fail)
    backend = JournalBackend(storage, tmp_path)
    monkeypatch.setattr(server, 'backend', backend)
    try:
        with caplog.at_level(logging.DEBUG):
            result = await server.obsidian_vault_stats()
        assert secret not in result + caplog.text
        assert 'Error' in result
    finally:
        await backend.close()


@pytest.mark.parametrize('status', [401, 403, 500])
async def test_couchdb_echoed_proxy_error_stays_private(status, caplog, monkeypatch):
    import logging
    import server
    secret = 'G5PrivateProxyCredential'
    def fail(request):
        return httpx.Response(status, text=secret, headers={'X-Proxy':secret})
    backend = CouchDBBackend('https://db.example','u','p','vault',
                            transport=httpx.MockTransport(fail), custom_headers=(('X-Proxy',secret),))
    monkeypatch.setattr(server, 'backend', backend)
    monkeypatch.setenv('LIVESYNC_UNSAFE_DEBUG','1')
    try:
        with caplog.at_level(logging.DEBUG):
            result = await server.obsidian_vault_stats()
        assert secret not in result + caplog.text
        assert 'Error' in result
    finally:
        await backend.close()


@pytest.mark.parametrize('style', [True, False])
async def test_journal_conditional_fallback_on_signed_wire(style, tmp_path, monkeypatch):
    from urllib.parse import urlsplit, unquote
    from xml.sax.saxutils import escape
    from tests.test_journal_security import backend as make_peer
    peer, fake = await make_peer(tmp_path)
    objects = dict(fake._objects)
    storage = Boto3ObjectStorage(replace(make_cfg(endpoint='https://objects.example', prefix=''),
                                       force_path_style=style, custom_headers=(('X-Proxy','credential'),)))
    seen = []
    def send(request):
        seen.append(request)
        url = urlsplit(request.url)
        assert url.hostname == ('objects.example' if style else 'test-bucket.objects.example')
        assert request.headers['X-Proxy'] == b'credential'
        assert request.headers['Authorization'].startswith(b'AWS4-HMAC-SHA256')
        key = unquote(url.path.lstrip('/'))
        if style:
            key = key.split('/', 1)[1] if '/' in key else ''
        status = 200
        if 'list-type=' in url.query:
            body = ('<ListBucketResult><IsTruncated>false</IsTruncated>'+''.join('<Contents><Key>'+escape(k)+'</Key></Contents>' for k in sorted(objects))+'</ListBucketResult>').encode()
        elif request.method == 'PUT' and 'If-None-Match' in request.headers:
            status, body = 501, b'<Error><Code>NotImplemented</Code></Error>'
        elif request.method == 'PUT':
            payload = request.body if isinstance(request.body, bytes) else request.body.read()
            if request.headers.get('Content-Encoding') == b'aws-chunked':
                # The real SDK emits HTTP chunk framing plus checksum trailers.
                from io import BytesIO
                stream, chunks = BytesIO(payload), []
                while True:
                    size = int(stream.readline().split(b';',1)[0],16)
                    if not size:
                        break
                    chunks.append(stream.read(size))
                    assert stream.read(2) == b'\r\n'
                payload = b''.join(chunks)
            objects[key] = payload
            body = b''
        elif key not in objects:
            status, body = 404, b''
        else:
            body = objects[key] if request.method == 'GET' else b''
        return AWSResponse(request.url, status, {'content-length':str(len(body))}, Raw(body))
    monkeypatch.setattr(storage._client._endpoint.http_session, 'send', send)
    peer._object_storage = storage
    try:
        await peer.write_note('proxy.md','body',create=True)
        journal_key = peer._store.last_sent_key
        assert objects[journal_key]
        journal_requests = [r for r in seen if journal_key in r.url]
        assert [r.method for r in journal_requests] == ['PUT','HEAD','PUT','GET']
    finally:
        await peer.close()


def test_couchdb_fresh_process_transport_trace_is_private():
    # httpcore loggers are lazily imported by a real AsyncHTTPTransport.
    # A MockTransport cannot prove this first-connection boundary.
    import subprocess
    import sys
    code = '''
import asyncio, logging
from storage_couchdb import CouchDBBackend
logging.basicConfig(level=logging.DEBUG)
backend = CouchDBBackend('https://db.example','u','p','vault', custom_headers=(('X-Proxy','G5PrivateProxyCredential'),))
from httpcore._trace import Trace
try:
    with Trace('receive_response_headers', logging.getLogger('httpcore.http11')):
        raise ValueError('G5PrivateProxyCredential')
except ValueError:
    pass
asyncio.run(backend.close())
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0
    assert 'G5PrivateProxyCredential' not in result.stdout + result.stderr


FRAMING_NAMES = ['Content-Length', 'Transfer-Encoding', 'Host', 'Connection',
                 'Keep-Alive', 'TE', 'Trailer', 'Upgrade', 'Proxy-Authorization',
                 'Proxy-Connection', 'Proxy-Arbitrary']


@pytest.mark.parametrize('backend', ['couchdb', 's3'])
@pytest.mark.parametrize('name', FRAMING_NAMES)
@pytest.mark.parametrize('lower', [False, True])
def test_framing_headers_refused_without_values(backend, name, lower):
    # Deliberate safety difference: native MinioStorageAdapter.ts:86-100
    # copies arbitrary fields at finalizeRequest, after framing/signing.
    name = name.lower() if lower else name
    variable = backend.upper() + '_CUSTOM_HEADERS'
    with pytest.raises(ConfigError) as caught:
        load_config({**env_for(backend), variable: json.dumps({name: 'PRIVATE'})})
    message = str(caught.value)
    assert variable in message and 'entry 1' in message
    assert 'transport-owned' in message and 'remove' in message
    assert 'PRIVATE' not in message
    assert repr(name) not in message


@pytest.mark.parametrize('name', FRAMING_NAMES)
@pytest.mark.parametrize('backend', ['couchdb', 's3-path', 's3-host'])
def test_injected_framing_headers_never_reach_prepared_request(name, backend, monkeypatch):
    # Embedders may inject Config dataclasses without load_config. Reject at
    # adapter construction as well, before any HTTP request can be prepared.
    sent = []
    with pytest.raises(ConfigError):
        if backend == 'couchdb':
            CouchDBBackend('https://db.example', 'u', 'p', 'vault',
                transport=httpx.MockTransport(lambda r: sent.append(r)),
                custom_headers=((name, 'PRIVATE'),))
        else:
            cfg = replace(make_cfg(), force_path_style=backend == 's3-path',
                          custom_headers=((name, 'PRIVATE'),))
            Boto3ObjectStorage(cfg)
    assert not sent


async def test_final_couchdb_framing_with_safe_custom_headers():
    sent = []
    def send(request):
        sent.append(request)
        return httpx.Response(201, json={'ok': True})
    backend = CouchDBBackend('https://db.example', 'u', 'p', 'vault',
        transport=httpx.MockTransport(send), custom_headers=(('X-Proxy', 'safe'),))
    try:
        await backend._request('PUT', 'vault/a', json_body={'data': '123456789'})
        request = sent[0]
        assert request.headers['host'] == 'db.example'
        assert int(request.headers['content-length']) == len(request.content)
        assert 'transfer-encoding' not in request.headers
    finally:
        await backend.close()


@pytest.mark.parametrize('style', [True, False])
def test_final_s3_framing_with_safe_custom_headers(style, monkeypatch):
    from urllib.parse import urlsplit
    storage = Boto3ObjectStorage(replace(make_cfg(endpoint='https://objects.example'),
        force_path_style=style, custom_headers=(('X-Proxy', 'safe'),)))
    sent = []
    def send(request):
        body = request.body.read() if hasattr(request.body, 'read') else request.body
        sent.append((request, body))
        return AWSResponse(request.url, 200, {}, Raw(b''))
    monkeypatch.setattr(storage._client._endpoint.http_session, 'send', send)
    storage.put('probe', b'123456789')
    request, body = sent[0]
    assert urlsplit(request.url).hostname == ('objects.example' if style else 'test-bucket.objects.example')
    assert not ('Content-Length' in request.headers and 'Transfer-Encoding' in request.headers)
    if 'Content-Length' in request.headers:
        assert int(request.headers['Content-Length']) == len(body)
    assert request.headers['X-Proxy'] == b'safe'
