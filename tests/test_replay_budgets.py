"""Operational budgets are explicit, tunable protection, never file policy."""
import pytest
from config import load_config, ConfigError
from tests.test_config import COUCHDB_ENV
from journal_codec import deflate_journal, inflate_journal, parse_journal


@pytest.mark.parametrize('operation,variable,observed,limit', [
    (lambda: inflate_journal(deflate_journal(b'x' * 20), max_output_bytes=10), 'LIVESYNC_JOURNAL_MAX_INFLATED_BYTES', 11, 10),
    (lambda: parse_journal(b'xxxxxxxxxxx\n', max_line_bytes=10), 'LIVESYNC_JOURNAL_MAX_LINE_BYTES', 11, 10),
    (lambda: parse_journal(b'{"_id":"a","_rev":"1-a"}\n' * 2, max_records=1), 'LIVESYNC_JOURNAL_MAX_RECORDS', 2, 1),
])
def test_budget_diagnostic(operation, variable, observed, limit):
    with pytest.raises(ValueError) as caught:
        operation()
    message = str(caught.value)
    assert variable in message
    assert str(observed) in message and str(limit) in message
    assert 'raise' in message


@pytest.mark.parametrize('variable,field', [
    ('LIVESYNC_JOURNAL_MAX_LINE_BYTES', 'max_line_bytes'),
    ('LIVESYNC_JOURNAL_MAX_INFLATED_BYTES', 'max_inflated_bytes'),
    ('LIVESYNC_JOURNAL_MAX_RECORDS', 'max_records'),
    ('LIVESYNC_JOURNAL_MAX_JSON_DEPTH', 'max_json_depth'),
    ('LIVESYNC_JOURNAL_MAX_OBJECT_BYTES', 'max_object_bytes'),
    ('LIVESYNC_JOURNAL_MAX_REFRESH_KEYS', 'max_refresh_keys'),
    ('LIVESYNC_JOURNAL_MAX_REFRESH_BYTES', 'max_refresh_bytes'),
    ('LIVESYNC_JOURNAL_MAX_REFRESH_SECONDS', 'max_refresh_seconds'),
])
def test_budget_env_has_no_upper_ceiling(variable, field):
    cfg = load_config({**COUCHDB_ENV, variable: str(2**50)})
    assert getattr(cfg.replay_limits, field) == 2**50
    with pytest.raises(ConfigError, match=variable):
        load_config({**COUCHDB_ENV, variable: '0'})


def test_size_override_defaults_to_vault():
    assert load_config(COUCHDB_ENV).sync_max_size_mb is None
    assert load_config({**COUCHDB_ENV, 'LIVESYNC_SYNC_MAX_SIZE_MB': '0.5'}).sync_max_size_mb == .5


async def test_throttled_budget_failure_keeps_actionable_diagnostic(tmp_path):
    from tests.test_journal_security import backend
    from tests.test_storage_journal import journal_blob, meta_doc
    from replay_limits import ReplayBudgetExceeded
    peer, _ = await backend(tmp_path, {'1-docs.jsonl.gz': journal_blob([meta_doc('a.md', '1-a')])}, max_line_bytes=8)
    try:
        for force in [True, False]:
            with pytest.raises(ReplayBudgetExceeded, match='LIVESYNC_JOURNAL_MAX_LINE_BYTES'):
                await peer.refresh(force=force)
    finally:
        await peer.close()


def test_raise_parser_thresholds_accepts_same_payload():
    data = b'{"_id":"a","_rev":"1-a","nested":[[0]]}\n'
    for variable, kwargs in [
        ('MAX_LINE_BYTES', {'max_line_bytes': len(data)-2}),
        ('MAX_RECORDS', {'max_records': 1}),
        ('MAX_JSON_DEPTH', {'max_json_depth': 2}),
    ]:
        payload = data * 2 if variable == 'MAX_RECORDS' else data
        with pytest.raises(ValueError, match='LIVESYNC_JOURNAL_' + variable):
            parse_journal(payload, **kwargs)
        cfg = load_config({**COUCHDB_ENV, 'LIVESYNC_JOURNAL_' + variable: '1000'})
        high = {next(iter(kwargs)): getattr(cfg.replay_limits, next(iter(kwargs)))}
        assert parse_journal(payload, **high).docs[0]['_id'] == 'a'
    blob = deflate_journal(data)
    with pytest.raises(ValueError, match='LIVESYNC_JOURNAL_MAX_INFLATED_BYTES'):
        inflate_journal(blob, max_output_bytes=1)
    cfg = load_config({**COUCHDB_ENV, 'LIVESYNC_JOURNAL_MAX_INFLATED_BYTES': '1000'})
    assert inflate_journal(blob, max_output_bytes=cfg.replay_limits.max_inflated_bytes) == data


async def test_factory_wires_all_replay_overrides(tmp_path, monkeypatch):
    import server
    from tests.fake_object_storage import FakeObjectStorage
    from tests.test_storage_journal import healthy_bucket
    seen = {}
    storage = FakeObjectStorage(healthy_bucket())
    def adapter(cfg, *, max_object_bytes):
        storage.max_object_bytes = max_object_bytes
        seen['max_object_bytes'] = max_object_bytes
        return storage
    monkeypatch.setattr(server, 'Boto3ObjectStorage', adapter)
    overrides = dict(max_line_bytes=1024, max_inflated_bytes=2048, max_records=100,
                     max_json_depth=20, max_object_bytes=4096, max_refresh_keys=50,
                     max_refresh_bytes=8192, max_refresh_seconds=2.5)
    cfg = load_config({'LIVESYNC_BACKEND': 's3',
        'LIVESYNC_S3_URI': 'sls://AK:SK@https://minio.local?bucket=vault&region=auto',
        'MCP_API_KEY': 'test-token', 'S3_STATE_DIR': str(tmp_path),
        'LIVESYNC_SYNC_MAX_SIZE_MB': '0',
        **{'LIVESYNC_JOURNAL_' + k.upper(): str(v) for k, v in overrides.items()}})
    peer = server.get_backend(cfg)
    try:
        await peer.startup_check()
        assert peer.sync_max_size_mb == 0
        for key, value in overrides.items():
            assert getattr(storage if key == 'max_object_bytes' else peer, key) == value
        await peer.write_attachment('a.pdf', b'abcd', create=True)
    finally:
        await peer.close()


@pytest.mark.parametrize('declared', [True, False])
def test_object_budget_is_actionable_and_can_be_raised(declared):
    from io import BytesIO
    from unittest.mock import Mock
    from object_storage import Boto3ObjectStorage, ObjectSizeLimit
    storage = object.__new__(Boto3ObjectStorage)
    storage._bucket, storage._prefix = 'test', ''
    storage._client = Mock()
    def response():
        return {'Body': BytesIO(b'x' * 20), **({'ContentLength': 20} if declared else {})}
    storage._client.get_object.side_effect = lambda **kw: response()
    storage.max_object_bytes = 10
    with pytest.raises(ObjectSizeLimit, match='observed.*configured 10.*LIVESYNC_JOURNAL_MAX_OBJECT_BYTES'):
        storage.get('a')
    cfg = load_config({**COUCHDB_ENV, 'LIVESYNC_JOURNAL_MAX_OBJECT_BYTES': '20'})
    storage.max_object_bytes = cfg.replay_limits.max_object_bytes
    assert storage.get('a') == b'x' * 20


@pytest.mark.parametrize('kind', ['keys', 'bytes', 'seconds'])
async def test_refresh_threshold_can_be_raised_from_env(tmp_path, monkeypatch, kind):
    from tests.test_journal_security import backend
    from tests.test_storage_journal import journal_blob, meta_doc
    from replay_limits import ReplayBudgetExceeded
    import storage_journal
    objects = {'1-docs.jsonl.gz': journal_blob([meta_doc('a.md', '1-a')])}
    peer, storage = await backend(tmp_path, objects)
    clock = [0.0]
    monkeypatch.setattr(storage_journal.time, 'monotonic', lambda: clock[0])
    if kind == 'keys':
        peer.max_refresh_keys = 1
    elif kind == 'bytes':
        peer.max_refresh_bytes = 1
    else:
        peer.max_refresh_seconds = 1
        original_get = storage.get
        def slow_get(*args, **kwargs):
            result = original_get(*args, **kwargs)
            clock[0] += 2
            return result
        storage.get = slow_get
    try:
        variable = 'LIVESYNC_JOURNAL_MAX_REFRESH_' + kind.upper()
        with pytest.raises(ReplayBudgetExceeded, match=variable):
            await peer.refresh(force=True)
        cfg = load_config({**COUCHDB_ENV, variable: '10000'})
        attr = 'max_refresh_' + kind
        setattr(peer, attr, getattr(cfg.replay_limits, attr))
        await peer.refresh(force=True)
        assert peer._store.processed('1-docs.jsonl.gz')
    finally:
        await peer.close()


@pytest.mark.parametrize('configured', [5000, 20000])
def test_decoder_capacity_reports_depth_budget_not_generic_parse_error(configured):
    from replay_limits import ReplayBudgetExceeded
    # Valid JSON depth 10,001, assembled without using the recursive encoder.
    payload = b'{"_id":"a","_rev":"1-a","nested":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}\n'
    with pytest.raises(ReplayBudgetExceeded) as caught:
        parse_journal(payload, max_json_depth=configured)
    error = caught.value
    assert error.budget == 'max_json_depth'
    assert error.limit == configured
    assert error.observed > 0
    assert 'LIVESYNC_JOURNAL_MAX_JSON_DEPTH' in str(error)
    if configured == 20000:
        assert error.observed == 10001
        assert 'decoder capacity' in str(error)


async def test_decoder_capacity_diagnostic_survives_refresh_and_tool(tmp_path, monkeypatch):
    import server
    from tests.test_journal_security import backend
    from replay_limits import ReplayBudgetExceeded
    payload = b'{"_id":"a","_rev":"1-a","nested":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}\n'
    key = '1-docs.jsonl.gz'
    peer, storage = await backend(tmp_path, {key: deflate_journal(payload)}, max_json_depth=20000)
    try:
        with pytest.raises(ReplayBudgetExceeded, match='decoder capacity'):
            await peer.refresh(force=True)
        assert not peer._store.processed(key)
        monkeypatch.setattr(server, 'backend', peer)
        result = await server.obsidian_refresh()
        for detail in ['max_json_depth', '10001', '20000', 'LIVESYNC_JOURNAL_MAX_JSON_DEPTH', 'decoder capacity']:
            assert detail in result
    finally:
        await peer.close()
