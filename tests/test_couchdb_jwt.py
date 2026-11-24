"""Pinned httplib.ts:67-212 JWT wire and refresh oracle (synthetic keys only)."""
import base64
import json
import os
from pathlib import Path

import httpx
import pytest

from config import ConfigError, load_config
from storage_couchdb import CouchDBBackend
from tests.couchdb_fake import make_fake_couchdb

FIXTURE = json.loads(Path('tests/fixtures/jwt_upstream.json').read_text())


def env_for(vector=FIXTURE['vectors'][0]):
    a = vector['auth']
    return dict(LIVESYNC_BACKEND='couchdb', COUCHDB_DATABASE='vault', MCP_API_KEY='test-api',
                COUCHDB_JWT_ALGORITHM=a['jwtAlgorithm'], COUCHDB_JWT_KEY=a['jwtKey'],
                COUCHDB_JWT_SUBJECT=a['jwtSub'], COUCHDB_JWT_KEY_ID=a['jwtKid'],
                COUCHDB_JWT_EXPIRY_MINUTES=str(a['jwtExpDuration']))


def generator(vector, clock=None):
    from couchdb_jwt import JWTTokenGenerator
    c = load_config(env_for(vector)).couchdb
    return JWTTokenGenerator(c, clock=clock or (lambda: vector['now']))


@pytest.mark.parametrize('vector', FIXTURE['vectors'])
def test_wire_oracle(vector):
    # httplib.ts:110-142 uses btoa(JSON.stringify), NOT UTF-8 JSON.
    token = generator(vector).token()
    expected = vector['token'].split('.')
    assert token.split('.')[:2] == expected[:2]
    for segment, name in zip(token.split('.'), ('header_json', 'payload_json')):
        assert base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)).decode('latin1') == vector[name]
    a = vector['auth']
    if a['jwtAlgorithm'].startswith('HS'):
        assert token == vector['token']
        key = base64.b64decode(a['jwtKey'])
    else:
        key = vector['public_key']
        assert len(base64.urlsafe_b64decode(token.split('.')[2] + '==')) == (64 if a['jwtAlgorithm'] == 'ES256' else 132)
    # Decode bytes directly: PyJWT assumes UTF-8, but the plugin uses Latin-1 btoa.
    from jwt.algorithms import get_default_algorithms
    algo = get_default_algorithms()[a['jwtAlgorithm']]
    assert algo.verify('.'.join(token.split('.')[:2]).encode(), algo.prepare_key(key),
                       base64.urlsafe_b64decode(token.split('.')[2] + '=='))


@pytest.mark.parametrize('v', FIXTURE['refresh'])
def test_refresh_oracle(v):
    from couchdb_jwt import needs_refresh
    # httplib.ts:181-196: strict <, not <=; seconds truncated before adding margin.
    assert needs_refresh(v['iat'], v['exp'], v['now']) is v['regenerate']


def test_cached_token_boundary():
    v = FIXTURE['vectors'][0]
    now = [v['now']]
    g = generator(v, lambda: now[0])
    first = g.token()
    now[0] += 290.9
    assert g.token() == first
    now[0] += 0.1
    assert g.token() != first


@pytest.mark.parametrize('updates,names', [
    ({'COUCHDB_JWT_KEY': ''}, ['COUCHDB_JWT_KEY']),
    ({'COUCHDB_JWT_KEY': '   '}, ['COUCHDB_JWT_KEY']),
    ({'COUCHDB_JWT_SUBJECT': ' \n '}, ['COUCHDB_JWT_SUBJECT']),
    ({'COUCHDB_JWT_ALGORITHM': 'RS256'}, ['COUCHDB_JWT_ALGORITHM']),
    ({'COUCHDB_USER': 'x'}, ['COUCHDB_USER']),
    ({'COUCHDB_PASSWORD': ''}, ['COUCHDB_PASSWORD']),
    ({'COUCHDB_JWT_ALGORITHM': ''}, ['COUCHDB_JWT_ALGORITHM']),
    ({'COUCHDB_JWT_EXPIRY_MINUTES': 'nan'}, ['COUCHDB_JWT_EXPIRY_MINUTES']),
    ({'COUCHDB_JWT_EXPIRY_MINUTES': 'inf'}, ['COUCHDB_JWT_EXPIRY_MINUTES']),
    ({'COUCHDB_JWT_EXPIRY_MINUTES': '0'}, ['COUCHDB_JWT_EXPIRY_MINUTES']),
    ({'COUCHDB_JWT_EXPIRY_MINUTES': '-2'}, ['COUCHDB_JWT_EXPIRY_MINUTES']),
    ({'COUCHDB_JWT_KEY': 'PRIVATE KEY INVALID'}, ['COUCHDB_JWT_KEY']),
])
def test_config_errors_are_typed(updates, names):
    env = {**env_for(), **updates}
    with pytest.raises(ConfigError) as error:
        load_config(env)
    assert all(n in str(error.value) for n in names)
    if env['COUCHDB_JWT_KEY'].strip():
        assert env['COUCHDB_JWT_KEY'] not in str(error.value)


@pytest.mark.parametrize('missing', ['COUCHDB_JWT_KEY', 'COUCHDB_JWT_SUBJECT'])
def test_missing_jwt_piece(missing):
    env = env_for()
    del env[missing]
    with pytest.raises(ConfigError, match=missing):
        load_config(env)


def test_default_expiry_and_redacted_config():
    env = env_for()
    del env['COUCHDB_JWT_EXPIRY_MINUTES']
    c = load_config(env).couchdb
    assert c.jwt_expiry_minutes == 5
    assert env['COUCHDB_JWT_KEY'] not in repr(c)


@pytest.mark.asyncio
async def test_bearer_all_http_classes():
    c = load_config(env_for()).couchdb
    transport, log = make_fake_couchdb({})
    seen = []
    async def handle(req):
        seen.append((req.method, req.url.path))
        assert req.headers['Authorization'].startswith('Bearer ')
        assert 'Basic ' not in req.headers['Authorization']
        return await transport.handle_async_request(req)
    b = CouchDBBackend('http://test', '', '', 'vault', jwt_config=c,
                       transport=httpx.MockTransport(handle))
    try:
        await b.startup_check()
        for method, path, body in [('GET','vault',None), ('GET','vault/missing',None),
                                    ('PUT','vault/n',{'_id':'n','type':'plain','data':'text'}),
                                    ('POST','vault/_all_docs',{'keys':['n']}),
                                    ('DELETE','vault/n',None)]:
            params = None
            if method == "DELETE":
                doc = await b._request("GET", path)
                params = {"rev": doc.json()["_rev"]}
            await b._request(method, path, json_body=body, params=params)
        info = await b.backend_info()
        assert info['auth_mode'] == 'jwt'
        assert info['jwt_algorithm'] == 'HS256'
        assert {'GET','PUT','POST','DELETE'} <= {m for m,p in seen}
    finally:
        await b.close()


@pytest.mark.asyncio
async def test_transport_secret_never_reaches_tools_or_logs(caplog, monkeypatch):
    import server
    c = load_config(env_for()).couchdb
    async def fail(req):
        raise httpx.ConnectError(c.jwt_key, request=req)
    b = CouchDBBackend('http://test', '', '', 'vault', jwt_config=c,
                       transport=httpx.MockTransport(fail))
    monkeypatch.setattr(server, 'backend', b)
    try:
        result = await server.obsidian_vault_stats()
        assert c.jwt_key not in str(result) + caplog.text
    finally:
        await b.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get('COUCHDB_TEST_JWT_URL'), reason='COUCHDB_TEST_JWT_URL absent')
async def test_live_jwt_startup():
    # Controller supplies JWT-only credentials; never persist or display them.
    from server import get_backend
    env = {k:v for k,v in os.environ.items() if k.startswith('COUCHDB_JWT_')}
    env.update(LIVESYNC_BACKEND='couchdb', COUCHDB_URL=os.environ['COUCHDB_TEST_JWT_URL'],
               COUCHDB_DATABASE=os.environ.get('COUCHDB_DATABASE', 'vault'), MCP_API_KEY='smoke-only')
    b = get_backend(load_config(env))
    try:
        await b.startup_check()
    finally:
        await b.close()


@pytest.mark.asyncio
async def test_factory_and_tool_observability(monkeypatch):
    import server
    b = server.get_backend(load_config(env_for()))
    transport, _ = make_fake_couchdb({})
    await b._client.aclose()
    b._client = httpx.AsyncClient(base_url='http://test/', transport=transport)
    monkeypatch.setattr(server, 'backend', b)
    try:
        result = json.loads(await server.obsidian_vault_stats())
        assert result['auth_mode'] == 'jwt'
        assert result['jwt_algorithm'] == 'HS256'
        for name in ('COUCHDB_JWT_KEY', 'COUCHDB_JWT_SUBJECT', 'COUCHDB_JWT_KEY_ID'):
            value = env_for()[name]
            if value:
                assert value not in json.dumps(result)
    finally:
        await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,algorithm', [('injected-secret', 'injected-secret'), ([], {})])
async def test_tool_auth_fields_closed(monkeypatch, mode, algorithm):
    import server
    from tests.fake_backend import FakeBackend
    b = FakeBackend()
    async def info():
        return {'backend': 'couchdb', 'auth_mode': mode, 'jwt_algorithm': algorithm}
    monkeypatch.setattr(b, 'backend_info', info)
    monkeypatch.setattr(server, 'backend', b)
    result = json.loads(await server.obsidian_vault_stats())
    assert result['auth_mode'] is None
    assert result['jwt_algorithm'] is None


@pytest.mark.asyncio
async def test_basic_auth_observability():
    transport, _ = make_fake_couchdb({})
    b = CouchDBBackend('http://test', 'user', 'password', 'vault', transport=transport)
    try:
        info = await b.backend_info()
        assert info['auth_mode'] == 'basic'
        assert info['jwt_algorithm'] is None
    finally:
        await b.close()


@pytest.mark.asyncio
async def test_backend_cache_is_independent_and_refreshes_on_requests():
    v = FIXTURE['vectors'][0]
    cfg = load_config(env_for(v)).couchdb
    now = [v['now']]
    headers = []
    def reply(req):
        headers.append(req.headers['Authorization'])
        return httpx.Response(200, json={})
    a = CouchDBBackend('http://test', '', '', 'vault', jwt_config=cfg,
                       jwt_clock=lambda: now[0], transport=httpx.MockTransport(reply))
    b = CouchDBBackend('http://test', '', '', 'vault', jwt_config=cfg,
                       jwt_clock=lambda: now[0], transport=httpx.MockTransport(reply))
    try:
        await a._request('GET', 'vault')
        now[0] += 290
        await a._request('GET', 'vault')
        await b._request('GET', 'vault')
        assert headers[0] == headers[1] == 'Bearer ' + v['token']
        assert headers[2] != headers[1]
        now[0] += 1
        await a._request('GET', 'vault')
        assert headers[3] != headers[1]
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_jwt_remote_error_debug_is_closed(caplog, monkeypatch):
    from storage_base import StorageError
    cfg = load_config(env_for()).couchdb
    monkeypatch.setenv('LIVESYNC_UNSAFE_DEBUG', '1')
    caplog.set_level('DEBUG')
    def fail(req):
        return httpx.Response(500, text=cfg.jwt_key)
    b = CouchDBBackend('http://test', '', '', 'vault', jwt_config=cfg,
                       transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(StorageError) as error:
            await b._request('GET', 'vault')
        assert cfg.jwt_key not in str(error.value) + caplog.text
        assert 'unsafe diagnostic:' not in caplog.text
    finally:
        await b.close()


@pytest.mark.parametrize('variable', ['COUCHDB_JWT_SUBJECT', 'COUCHDB_JWT_KEY_ID'])
def test_real_plugin_encoder_constraint_names_recovery(variable):
    with pytest.raises(ConfigError) as error:
        load_config({**env_for(), variable: 'non-Latin-1-🙂'})
    assert variable in str(error.value)
    assert 'Latin-1' in str(error.value)
    assert 'non-Latin-1-🙂' not in str(error.value)


def test_ec_curve_and_container_must_match_plugin():
    from cryptography.hazmat.primitives import serialization
    ec_vector = next(v for v in FIXTURE['vectors'] if v['auth']['jwtAlgorithm'] == 'ES256')
    env = env_for(ec_vector)
    with pytest.raises(ConfigError, match='COUCHDB_JWT_KEY'):
        load_config({**env, 'COUCHDB_JWT_ALGORITHM': 'ES512'})
    key = serialization.load_pem_private_key(env['COUCHDB_JWT_KEY'].encode(), password=None)
    sec1 = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    with pytest.raises(ConfigError, match='PKCS8'):
        load_config({**env, 'COUCHDB_JWT_KEY': sec1})


@pytest.mark.parametrize('v', FIXTURE['key_vectors'])
def test_upstream_key_import(v):
    vector = {**FIXTURE['vectors'][0], 'now': v['now'],
              'auth': {**FIXTURE['vectors'][0]['auth'], 'jwtKey': v['key']}}
    if v['error']:
        with pytest.raises(ConfigError, match='COUCHDB_JWT_KEY'):
            generator(vector)
    else:
        assert generator(vector).token() == v['token']
