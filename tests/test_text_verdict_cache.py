"""Negative eligibility verdicts are current, bounded and useful for I/O."""
import pytest

from config import ConfigError, load_config
from tests.test_config import COUCHDB_ENV
from tests.test_upload_attachment import backend


def cached_ids(backend):
    if hasattr(backend, '_store'):
        return [row[0] for row in backend._store._conn.execute('SELECT doc_id FROM text_verdicts')]
    return list(backend._text_verdicts)


async def document(backend, path):
    if hasattr(backend, '_store'):
        return backend._store.winner(path)
    return await backend._get_doc(path)


async def scan(backend, doc):
    if hasattr(backend, '_store'):
        return backend._store.scan_newnote(doc, backend.text_scan_max_bytes)
    return await backend._scan_newnote(doc)


def set_limit(backend, limit):
    if hasattr(backend, '_store'):
        backend._store.text_verdict_cache_entries = limit
    else:
        backend.text_verdict_cache_entries = limit


async def test_ten_thousand_revisions_keep_one_verdict(backend):
    backend.text_scan_max_bytes = 1024
    await backend.write_note('file.base', b'\x00', create=True)
    doc = await document(backend, 'file.base')
    for generation in range(1, 10001):
        assert await scan(backend, {**doc, '_rev': f'{generation}-revision'}) is None
    assert cached_ids(backend) == [doc['_id']]


async def test_positive_verdict_is_not_cached(backend):
    backend.text_scan_max_bytes = 1024
    await backend.write_note('text.base', 'needle', create=True)
    doc = await document(backend, 'text.base')
    assert await scan(backend, doc) == 'needle'
    assert cached_ids(backend) == []


async def test_hard_bound_and_lru_hit_avoid_io(backend, monkeypatch):
    backend.text_scan_max_bytes = 1024
    set_limit(backend, 3)
    await backend.write_note('file.base', b'\x00', create=True)
    doc = await document(backend, 'file.base')
    docs = [{**doc, '_id': str(n), 'path': f'{n}.base'} for n in range(30)]
    for item in docs[:3]:
        assert await scan(backend, item) is None
    owner, method = ((backend._store, '_chunk_datas') if hasattr(backend, '_store')
                     else (backend, '_fetch_chunk_datas'))
    original = getattr(owner, method)
    reads = []
    if method == '_chunk_datas':
        def counted(*args, **kwargs):
            reads.append(1)
            return original(*args, **kwargs)
    else:
        async def counted(*args, **kwargs):
            reads.append(1)
            return await original(*args, **kwargs)
    monkeypatch.setattr(owner, method, counted)
    assert await scan(backend, docs[0]) is None  # LRU hit promotes 0, no reads.
    assert reads == []
    assert await scan(backend, docs[3]) is None
    assert set(cached_ids(backend)) == {'0', '2', '3'}
    assert await scan(backend, docs[1]) is None  # Evicted, so must reread.
    assert len(reads) == 2
    for item in docs[4:]:
        await scan(backend, item)
        assert len(cached_ids(backend)) <= 3


async def test_changed_budget_replaces_verdict(backend):
    await backend.write_note('file.base', 'needle', create=True)
    doc = await document(backend, 'file.base')
    for budget in range(1, 6):
        backend.text_scan_max_bytes = budget
        assert await scan(backend, doc) is None
        assert len(cached_ids(backend)) == 1
    backend.text_scan_max_bytes = 100
    assert await scan(backend, doc) == 'needle'
    assert cached_ids(backend) == []


async def test_rewrite_copy_rename_invalidate_before_next_scan(backend):
    await backend.write_note('source.base', 'needle', create=True)
    for path in ['rewritten.base', 'copy.base', 'renamed.base']:
        await backend.write_note(path, b'\x00', create=True)
        assert (await backend.read_text_note(path))['text'] is None
    assert len(cached_ids(backend)) == 3
    await backend.write_note('rewritten.base', 'needle')
    await backend.copy_note('source.base', 'copy.base', overwrite=True)
    await backend.rename_note('source.base', 'renamed.base', overwrite=True)
    assert cached_ids(backend) == []
    for path in ['rewritten.base', 'copy.base', 'renamed.base']:
        assert (await backend.read_text_note(path))['text'] == 'needle'
    assert cached_ids(backend) == []


async def test_reset_clears_verdicts(backend):
    await backend.write_note('file.base', b'\x00', create=True)
    assert (await backend.read_text_note('file.base'))['text'] is None
    assert cached_ids(backend)
    if hasattr(backend, '_store'):
        backend._store.reset()
    else:
        await backend.startup_check()
    assert cached_ids(backend) == []


async def test_remote_revision_invalidates_verdict(backend):
    await backend.write_note('file.base', b'\x00', create=True)
    assert (await backend.read_text_note('file.base'))['text'] is None
    doc = await document(backend, 'file.base')
    if hasattr(backend, '_store'):
        changed = {**doc, '_rev': '2-remote', 'children': [], 'size': 0}
        backend._store.apply_journal('remote', {}, [changed])
    else:
        # Bypass peer writer to model another device updating CouchDB.
        await backend._client.put(backend._doc_path(doc['_id']),
                                  json={**doc, 'children': [], 'size': 0})
        await backend.get_meta('file.base')  # Observe the new revision.
    assert cached_ids(backend) == []
    assert (await backend.read_text_note('file.base'))['text'] == ''


async def test_cache_limit_configuration_and_factory():
    import server
    cfg = load_config({**COUCHDB_ENV, 'LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES': '7'})
    assert cfg.text_verdict_cache_entries == 7
    peer = server.get_backend(cfg)
    assert peer.text_verdict_cache_entries == 7
    await peer.close()
    assert load_config(COUCHDB_ENV).text_verdict_cache_entries == 4096


@pytest.mark.parametrize('value', ['0', '-1', 'no'])
def test_cache_limit_rejects_invalid_configuration(value):
    with pytest.raises(ConfigError, match='LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES'):
        load_config({**COUCHDB_ENV, 'LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES': value})


def test_journal_legacy_cache_is_pruned_on_open(tmp_path):
    import sqlite3
    from journal_store import JournalStore
    from livesync_format import PATH_IDENTITY_LEGACY
    store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
    store.close()
    # Simulate the previous multi-revision/positive-verdict cache schema.
    conn = sqlite3.connect(tmp_path / 'journal.sqlite3')
    conn.execute('DROP TABLE IF EXISTS text_verdicts')
    conn.execute('CREATE TABLE text_verdicts (doc_id TEXT, rev TEXT, budget INTEGER, eligible INTEGER, PRIMARY KEY(doc_id, rev, budget))')
    conn.executemany('INSERT INTO text_verdicts VALUES (?, ?, ?, ?)',
                     [('same', f'{n}-rev', 10, 0) for n in range(100)])
    conn.executemany('INSERT INTO text_verdicts VALUES (?, ?, ?, ?)',
                     [(str(n), '1-rev', 10, 0) for n in range(10)])
    conn.execute("INSERT INTO text_verdicts VALUES ('positive', '1-rev', 10, 1)")
    conn.commit()
    conn.close()
    store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY, text_verdict_cache_entries=3)
    try:
        rows = list(store._conn.execute('SELECT doc_id, eligible FROM text_verdicts'))
        assert len(rows) <= 3
        assert all(eligible == 0 for _, eligible in rows)
        assert len({doc_id for doc_id, _ in rows}) == len(rows)
    finally:
        store.close()


async def test_journal_factory_propagates_cache_limit(tmp_path, monkeypatch):
    import server
    from tests.test_storage_journal import healthy_bucket
    storage = healthy_bucket()
    monkeypatch.setattr(server, 'Boto3ObjectStorage', lambda cfg: storage)
    cfg = load_config({
        'LIVESYNC_BACKEND': 's3',
        'LIVESYNC_S3_URI': 'sls://AK:SK@https://minio.local?bucket=vault&region=auto',
        'S3_STATE_DIR': str(tmp_path), 'MCP_API_KEY': 'test-token',
        'LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES': '7',
    })
    peer = server.get_backend(cfg)
    try:
        await peer.startup_check()
        assert peer._store.text_verdict_cache_entries == 7
    finally:
        await peer.close()
