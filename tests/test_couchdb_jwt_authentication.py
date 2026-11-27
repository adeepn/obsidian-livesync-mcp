"""RemoteService.ts:479 verifies connectivity; httplib.ts:166-196 mints tokens.

CouchDB 3.5.3 real evidence: bad HMAC 400, expired 401 (2026-11-25).
"""
import httpx
import pytest

from config import load_config
from diagnostic_codes import DiagnosticCode
from storage_base import StorageUnavailable
from storage_couchdb import CouchDBBackend
from tests.couchdb_fake import make_fake_couchdb
from tests.test_couchdb_jwt import env_for


def backend_with_session(status, name, *, later_status=None):
    cfg = load_config(env_for()).couchdb
    base, _ = make_fake_couchdb({})
    requests = []
    async def handle(req):
        requests.append(req)
        if req.url.path == '/_session':
            return httpx.Response(status, json={'userCtx': {'name': name}, 'reason': 'PRIVATE_REASON'})
        if later_status is not None:
            return httpx.Response(later_status, json={'error': 'bad_request', 'reason': 'PRIVATE_REASON Bad HMAC in authentication token'})
        return await base.handle_async_request(req)
    backend = CouchDBBackend('http://test', '', '', 'vault', jwt_config=cfg, transport=httpx.MockTransport(handle))
    return backend, cfg, requests


@pytest.mark.parametrize('status,name', [(400,None), (401,None), (200,None), (200,'different')])
async def test_startup_requires_authenticated_subject(status, name, caplog):
    b, _, requests = backend_with_session(status, name)
    try:
        with pytest.raises(StorageUnavailable) as error:
            await b.startup_check()
        assert error.value.diagnostic.code == DiagnosticCode.COUCHDB_JWT_AUTHORIZATION_ERROR
        for field in ('COUCHDB_JWT_KEY', 'COUCHDB_JWT_ALGORITHM', 'COUCHDB_JWT_KEY_ID', 'COUCHDB_JWT_SUBJECT'):
            assert field in error.value.format()
        assert requests[0].url.path == '/_session'
        assert 'PRIVATE_REASON' not in error.value.format() + caplog.text
    finally:
        await b.close()


async def test_startup_accepts_right_subject():
    cfg = load_config(env_for()).couchdb
    b, _, requests = backend_with_session(200, cfg.jwt_subject)
    try:
        await b.startup_check()
        assert requests[0].url.path == '/_session'
    finally:
        await b.close()


@pytest.mark.parametrize('code', [400,401])
@pytest.mark.parametrize('authenticated', [False,True])
async def test_later_error_probes_session_without_echo(code, authenticated, caplog):
    cfg = load_config(env_for()).couchdb
    b, _, requests = backend_with_session(200, cfg.jwt_subject if authenticated else None, later_status=code)
    try:
        with pytest.raises(StorageUnavailable) as error:
            await b._request('GET', 'vault')
        expected = (DiagnosticCode.COUCHDB_JWT_AUTHORIZATION_ERROR if not authenticated or code == 401 else DiagnosticCode.FAILED)
        assert error.value.diagnostic.code == expected
        assert 'PRIVATE_REASON' not in error.value.format() + caplog.text
        if code == 400:
            assert any(r.url.path == '/_session' for r in requests)
    finally:
        await b.close()
