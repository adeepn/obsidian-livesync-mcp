"""P-22 per-field numeric contracts and real producer regressions."""
import sys
import time

import pytest

from diagnostic_hygiene import PublicDiagnostic
from diagnostic_codes import DiagnosticCode
from replay_limits import ReplayBudgetExceeded
from storage_base import WriteSkipped, RenameRefused

VALID = [(0.5, '0.5'), (0.25, '0.25'), (1.5, '1.5'), (0.1, '0.1'),
         (50, '50'), (50.0, '50'), (1e-7, '0.0000001'), (2**63-1, '9223372036854775807')]
INVALID = ['SECRET', None, {}, [], True, float('nan'), float('inf'), -1, 2**63]


@pytest.mark.parametrize('value,want', VALID)
@pytest.mark.parametrize('late', [False, True])
def test_measured_numeric_channels(value, want, late):
    for error in [WriteSkipped('a', 1048577, value if not late else 1),
                  RenameRefused('a', 'b', 1048577, value if not late else 1)]:
        if late: error.maximum = value
        assert f'syncMaxSizeInMB ({want})' in error.format()
    for field in ['observed', 'limit']:
        error = ReplayBudgetExceeded('max_refresh_seconds', value if field == 'observed' else 1,
                                     value if field == 'limit' else 1)
        if late: setattr(error, field, value)
        assert f'{field if field == "observed" else "configured"} {want}' in error.format()


@pytest.mark.parametrize('value', INVALID)
@pytest.mark.parametrize('late', [False, True])
def test_invalid_measured_channels(value, late):
    for field in ['observed', 'limit']:
        error = ReplayBudgetExceeded('max_refresh_seconds', value if field == 'observed' else 1,
                                     value if field == 'limit' else 1)
        if late: setattr(error, field, value)
        assert '<invalid>' in error.format() and 'SECRET' not in error.format()
    for cls, args in [(WriteSkipped, ('a', 1, value)), (RenameRefused, ('a','b',1,value))]:
        error = cls(*args)
        if late: error.maximum = value
        assert 'syncMaxSizeInMB (<invalid>)' in error.format()


def test_real_elapsed_and_signal():
    start = time.monotonic()
    elapsed = time.monotonic() - start
    assert '<invalid>' not in ReplayBudgetExceeded('max_refresh_seconds', elapsed, 1).format()
    assert ReplayBudgetExceeded('max_refresh_seconds', 1.25, 1).format() == 'max_refresh_seconds budget limit reached: observed 1.25, configured 1; raise LIVESYNC_JOURNAL_MAX_REFRESH_SECONDS'
    from selection_engine import run, EngineFailure
    with pytest.raises(EngineFailure, match=r'\(status -15\)'):
        run([sys.executable, '-c', 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)'])


@pytest.mark.parametrize('value', [-256, -15, 0, 256, -257, 257, True, 1.5, None, 'SECRET'])
def test_exit_status(value):
    error = PublicDiagnostic(DiagnosticCode.SELECTION_ENGINE, selection_cause='nonzero-exit', status=value)
    if value is None: return  # omitted status denotes unavailable process status
    want = str(value) if type(value) is int and -256 <= value <= 256 else '<invalid>'
    assert f'(status {want})' in error.format()
    object.__setattr__(error, 'status', value)
    assert f'(status {want})' in error.format()


@pytest.mark.parametrize('budget', ['max_line_bytes', 'max_inflated_bytes', 'max_records',
    'max_json_depth', 'max_object_bytes', 'max_refresh_keys', 'max_refresh_bytes'])
@pytest.mark.parametrize('value', [0, 1048577, 2**63-1, 1.5, *INVALID])
@pytest.mark.parametrize('late', [False, True])
def test_integer_budget_channels(budget, value, late):
    want = str(value) if type(value) is int and 0 <= value < 2**63 else '<invalid>'
    error = ReplayBudgetExceeded(budget, value, value, decoder_capacity=value)
    if late:
        error.observed = error.limit = error.decoder_capacity = value
    assert error.format() == (f'{budget} budget limit reached: observed {want}, configured {want}; '
        f'raise LIVESYNC_JOURNAL_{budget.upper()}; decoder capacity {want} exhausted; '
        'raising the variable alone cannot admit this input; reduce nesting')


async def test_real_refresh_duration(tmp_path, monkeypatch):
    from tests.test_journal_security import backend
    from tests.test_storage_journal import journal_blob, meta_doc
    import storage_journal
    peer, storage = await backend(tmp_path, {'1-docs.jsonl.gz': journal_blob([meta_doc('a.md', '1-a')])})
    clock = [100.0]
    monkeypatch.setattr(storage_journal.time, 'monotonic', lambda: clock[0])
    peer.max_refresh_seconds = 1
    original = storage.get
    def slow_get(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 101.25
        return result
    storage.get = slow_get
    try:
        with pytest.raises(ReplayBudgetExceeded) as caught:
            await peer.refresh(force=True)
        assert caught.value.format() == ('max_refresh_seconds budget limit reached: observed 1.25, '
            'configured 1; raise LIVESYNC_JOURNAL_MAX_REFRESH_SECONDS')
    finally:
        await peer.close()


GUIDANCE = {
    400: 'the server rejected the request as invalid; do not retry unchanged',
    405: 'method not allowed (server/configuration mismatch)',
    408: 'the request timed out; the outcome of a write is unknown — re-read before retrying',
    413: "the request is too large for the server; do not retry unchanged (split the content or raise the server's limit)",
    429: 'rate limited; wait before retrying',
}


@pytest.mark.parametrize('status', [400,405,408,413,429,500,503,499])
@pytest.mark.parametrize('operation', ['GET','HEAD','LIST','PUT','POST','DELETE'])
@pytest.mark.parametrize('write', [False, True])
def test_status_guidance(status, operation, write):
    import server
    from storage_base import StorageUnavailable, OutcomeUnknown
    public = PublicDiagnostic(status=status, operation=operation, transport='CouchDB')
    error = (OutcomeUnknown if write else StorageUnavailable)(public)
    text = server._storage_message(error)
    if status in GUIDANCE:
        guidance = GUIDANCE[status]
        if status == 408 and operation not in {'PUT','POST','DELETE'}:
            guidance = 'the request timed out; try again later'
        assert f'HTTP {status} for {operation}: {guidance}' in text
        assert 'storage is unavailable; try again later' not in text
    elif write:
        assert 'outcome unknown' in text and 'MAY have been applied' in text
    else:
        assert 'storage is unavailable; try again later' in text


@pytest.mark.parametrize('status', [400,405,408,413,429,500,503,499])
@pytest.mark.parametrize('operation', ['GET','HEAD','PUT','POST','DELETE'])
async def test_couchdb_status_classification(status, operation):
    import httpx
    from storage_couchdb import CouchDBBackend
    from storage_base import StorageError, OutcomeUnknown
    b = CouchDBBackend('http://test.invalid', 'u', 'p', 'db',
        transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    try:
        with pytest.raises(StorageError) as caught:
            await b._request(operation, '/db')
        assert isinstance(caught.value, OutcomeUnknown) == (operation in {'PUT','POST','DELETE'} and (status == 408 or status >= 500))
    finally:
        await b.close()


@pytest.mark.parametrize('status', [400,405,408,413,429,500,503,499])
def test_s3_put_status_classification(status):
    from botocore.exceptions import ClientError
    from object_storage import Boto3ObjectStorage
    from tests.test_object_storage import make_cfg
    from storage_base import OutcomeUnknown
    adapter = Boto3ObjectStorage(make_cfg())
    error = adapter._put_failure('a', ClientError({'Error': {'Code':'Failure'},
        'ResponseMetadata': {'HTTPStatusCode':status}}, 'PutObject'))
    assert isinstance(error, OutcomeUnknown) == (status == 408 or status >= 500)
