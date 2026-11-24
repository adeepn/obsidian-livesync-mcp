"""P-21 red-team regressions: typed specifics and total outcome reporting."""
import json

import httpx
import pytest

import server
import storage_base as E
from diagnostic_hygiene import PublicDiagnostic, DiagnosticPath, sanitize_diagnostic
from replay_limits import ReplayBudgetExceeded
from tests.fake_backend import FakeBackend
from storage_couchdb import CouchDBBackend

BAD_NUMBERS = ['SECRET', 1.5, True, None, -1, 2**63, {'SECRET': 1}]


@pytest.mark.parametrize('status', [400,401,403,404,405,408,409,413,429,500,503])
@pytest.mark.parametrize('operation', ['GET','PUT','POST','DELETE'])
async def test_couchdb_transport_specifics(monkeypatch, status, operation):
    transport = httpx.MockTransport(lambda r: httpx.Response(status, text='BODY_SECRET', headers={'secret':'HEADER_SECRET'}))
    b = CouchDBBackend('http://test.invalid', 'u', 'p', 'db', transport=transport)
    monkeypatch.setattr(server, 'backend', b)
    async def refresh(**kwargs):
        response = await b._request(operation, '/db')
        if response.status_code == 404:
            await b._db_info()
    monkeypatch.setattr(b, 'refresh', refresh)
    try:
        result = await server.obsidian_vault_stats()
        assert str(status) in result and operation in result
        assert 'BODY_SECRET' not in result and 'HEADER_SECRET' not in result
    finally:
        await b.close()


@pytest.mark.parametrize('value', BAD_NUMBERS)
@pytest.mark.parametrize('channel', ['observed','limit','decoder_capacity','size','maximum','status'])
@pytest.mark.parametrize('late', [False, True])
def test_numeric_channels_are_bounded_integers(value, channel, late):
    if channel in {'observed','limit','decoder_capacity'}:
        kwargs = dict(observed=1, limit=2, decoder_capacity=3)
        if not late: kwargs[channel] = value
        error = ReplayBudgetExceeded('max_records', **kwargs)
    elif channel == 'status':
        error = PublicDiagnostic(status=1 if late else value, operation='GET', transport='CouchDB')
    else:
        error = E.WriteSkipped('a', 1 if late or channel != 'size' else value,
                              2 if late or channel != 'maximum' else value)
    if late: object.__setattr__(error, channel, value)
    if channel == 'maximum' and value == 1.5:
        assert 'syncMaxSizeInMB (1.5)' in error.format()
        return
    if channel == 'status' and value is None:
        assert 'GET failed' in error.format()  # Optional status means no HTTP response.
        return
    assert '<invalid>' in error.format()
    assert 'SECRET' not in error.format()
    if isinstance(error, E.WriteSkipped):
        rename = E.RenameRefused('a','b', error.size, error.maximum)
        assert '<invalid>' in rename.format()


@pytest.mark.parametrize('name,want', [('ReadError','ReadError'), ('SECRET','SECRET'), ('Oops\nSuccess: saved','<unexpected cause>'), ('has space','<unexpected cause>'), ('A'*65,'<unexpected cause>')])
def test_cause_class_charset(name, want):
    error = E.StorageError(cause=type(name, (Exception,), {})())
    assert error.format().endswith('cause: ' + want)


@pytest.mark.parametrize('char', list('\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b\u200c\u200d\ufeff'))
async def test_path_display_and_structured_identity(monkeypatch, char):
    path = 'a' + char + '.md'
    b = FakeBackend()
    monkeypatch.setattr(server, 'backend', b)
    b.errors['refresh'] = E.WriteSkipped(path, 200, 1)
    result = json.loads(await server.obsidian_create_note(path, 'body'))
    assert result['path'] == path
    bidi = char in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'
    for display in [result['message'], E.StorageError(PublicDiagnostic(paths=(DiagnosticPath(path, True),))).format()]:
        if bidi:
            assert char not in display and f'\\u{ord(char):04x}' in display
        else: assert char in display


@pytest.mark.parametrize('kind', [E.StorageError,E.WriteSkipped,E.RenameRefused,E.OutcomeUnknown,E.MutationNotApplied,E.WriterUnsupported])
async def test_renderer_failure_is_outcome_unknown(monkeypatch, kind):
    b = FakeBackend()
    monkeypatch.setattr(server, 'backend', b)
    e = kind.__new__(kind)
    Exception.__init__(e)
    e.__dict__.update(path=DiagnosticPath('a.md', False), size=1, maximum=2)
    def broken(*args, **kwargs): raise ValueError('SECRET')
    monkeypatch.setattr(kind, 'format', broken)
    if kind is E.OutcomeUnknown: monkeypatch.setattr(kind, 'message', broken)
    b.errors['refresh'] = e
    result = await server.obsidian_create_note('a.md', 'body')
    assert 'internal error while reporting a storage outcome (ValueError)' in result
    assert "operation's result is unknown" in result and 're-read before retrying' in result
    assert 'SECRET' not in result


@pytest.mark.parametrize('value', [None, 1, True, 1.2, {}])
def test_redactor_non_string(value):
    assert sanitize_diagnostic(value) == '[redacted]'


@pytest.mark.parametrize('failure', ['connection','timeout','throttle','missing'])
@pytest.mark.parametrize('operation,method', [('GET','get_object'),('PUT','put_object'),('LIST','list_objects_v2'),('HEAD','head_object')])
def test_s3_adapter_typed_failures(monkeypatch, failure, operation, method):
    from botocore.exceptions import EndpointConnectionError, ReadTimeoutError, ClientError
    from object_storage import Boto3ObjectStorage
    from tests.test_object_storage import make_cfg
    adapter = Boto3ObjectStorage(make_cfg())
    exc = (EndpointConnectionError(endpoint_url='SECRET') if failure == 'connection' else
           ReadTimeoutError(endpoint_url='SECRET') if failure == 'timeout' else
           ClientError({'Error':{'Code':'SlowDown' if failure == 'throttle' else 'NoSuchKey','Message':'SECRET'},
                        'ResponseMetadata':{'HTTPStatusCode':503 if failure == 'throttle' else 404}}, method))
    def fail(**kwargs): raise exc
    monkeypatch.setattr(adapter._client, method, fail)
    if failure == 'missing' and operation in {'GET','HEAD'}:
        assert (adapter.get('a') if operation == 'GET' else adapter.exists('a')) in (None, False)
        return  # ordinary absence is not a storage failure
    with pytest.raises(E.StorageError) as caught:
        if operation == 'GET': adapter.get('a')
        elif operation == 'HEAD': adapter.exists('a')
        elif operation == 'PUT': adapter.put('a',b'a')
        else: adapter.list()
    output = server._storage_message(caught.value)
    assert operation in output and type(exc).__name__ in output
    assert 'SECRET' not in output
    assert caught.value.operation == operation
    assert caught.value.cause_class == type(exc).__name__


@pytest.mark.parametrize('field', ['max_line_bytes','max_inflated_bytes','max_records','max_json_depth','max_object_bytes','max_refresh_keys','max_refresh_bytes','max_refresh_seconds'])
@pytest.mark.parametrize('value', ['1.5','-1',str(2**63),'SECRET', True, None, 1.5])
def test_config_rejects_invalid_budget_integers(field, value):
    from config import load_config, ConfigError
    from tests.test_config import COUCHDB_ENV
    with pytest.raises(ConfigError):
        load_config({**COUCHDB_ENV, 'LIVESYNC_JOURNAL_'+field.upper():value})


@pytest.mark.parametrize('char', list('\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b\u200c\u200d\ufeff'))
async def test_real_stored_path_display(char, monkeypatch):
    from tests.test_storage_couchdb import fake_backend
    path = 'remote'+char+'.md'
    doc = {'_id':path,'_rev':'1-a','path':path,'type':'plain','children':'invalid','ctime':1,'mtime':1,'size':1}
    async with fake_backend({path:doc}) as (b,_):
        monkeypatch.setattr(server,'backend',b)
        result = await server.obsidian_fetch(path)
        if char in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069':
            assert char not in result and f'\\u{ord(char):04x}' in result
        else: assert char in result


@pytest.mark.parametrize('value', BAD_NUMBERS)
@pytest.mark.parametrize('field', ['size','maximum'])
def test_rename_numeric_fields_are_revalidated(value, field):
    error = E.RenameRefused('a','b',1,2)
    setattr(error, field, value)
    if field == 'maximum' and value == 1.5:
        assert 'syncMaxSizeInMB (1.5)' in error.format()
    else:
        assert '<invalid>' in error.format() and 'SECRET' not in error.format()


@pytest.mark.parametrize('value', ['PUT\nSuccess', 'GET SECRET', 'A'*65, None, True, {}])
def test_identifier_fields_fail_closed(value):
    public = PublicDiagnostic(operation=value, transport='S3', cause_class=value)
    # An omitted operation is valid; explicit malformed identifiers never echo.
    if value is not None:
        assert '<unexpected operation>' in public.format()
        assert '<unexpected cause>' in public.format()
    error = ReplayBudgetExceeded(value, 1, 2)
    assert error.format() == 'replay budget exceeded'


@pytest.mark.parametrize('phase', ['publication','readback'])
async def test_journal_preserves_transport_fields_through_uncertainty(tmp_path, monkeypatch, phase):
    from tests.test_storage_journal import make_backend, healthy_bucket
    b, storage = make_backend(tmp_path, healthy_bucket())
    await b.startup_check()
    public = PublicDiagnostic(operation='PUT' if phase == 'publication' else 'GET', transport='S3', status=503, cause_class='ClientError')
    def fail(*args, **kwargs): raise E.StorageUnavailable(public)
    original = storage.get
    def get(key, *args, **kwargs):
        if key.endswith('-docs.jsonl.gz'): return fail()
        return original(key, *args, **kwargs)
    if phase == 'publication': monkeypatch.setattr(storage, 'put_if_absent', fail)
    else: monkeypatch.setattr(storage, 'get', get)
    try:
        with pytest.raises(E.OutcomeUnknown) as caught:
            await b.write_note('new.md','body',create=True)
        text = caught.value.message('note create')
        assert '503' in text and public.operation in text and 'ClientError' in text
    finally: await b.close()
