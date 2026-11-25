"""Real CouchDB acceptance at pinned commonlib 484c164.

compress.ts:49-71, chunks.ts:218-252, httplib.ts:110-196. Expectations
reuse committed executions with verified published dependencies.
"""
import os

import pytest

from config import load_config
from storage_couchdb import CouchDBBackend
from tests.test_integration_couchdb import (
    raw_client, scratch_database, stored_doc, ChangeTracker, _URL, _USER, _PASSWORD,
    pytestmark,
)
from tests.test_integration_parity_s3 import MATRIX
from tests.settings_oracle_inputs import case_id, settings_for, payload
from tests.settings_oracle import matrix_vector, assert_partition, expected_pieces
from tests.test_surrogate_wire import W


async def seed(raw, database, settings):
    r = await raw.put(database + '/_local/obsydian_livesync_milestone', json={'tweak_values': {'PREFERRED': settings}})
    assert r.status_code == 201


def peer(database, **kwargs):
    return CouchDBBackend(_URL, _USER, _PASSWORD, database, **kwargs)


@pytest.mark.parametrize('case', MATRIX, ids=case_id)
async def test_real_couchdb_matrix(raw_client, scratch_database, case):
    db = scratch_database
    await seed(raw_client, db, settings_for(case))
    b = peer(db)
    info = await raw_client.get(db)
    tracker = ChangeTracker(raw_client, db, info.json()['update_seq'])
    oracle = matrix_vector(case)
    for phase in ('initial', 'updated'):
        vector = oracle['payloads'][phase]
        tracker.expected.update(zip(vector['children'], expected_pieces(vector)))
    suffix, content = payload(case[-1])
    path = 'Папка/Cafe\u0301' + suffix
    try:
        await b.startup_check()
        async def check(path, phase):
            doc = await b._get_doc(b._path2id(path))
            pieces = [(await stored_doc(raw_client, db, c))['data'] for c in doc['children']]
            assert_partition(pieces, doc['children'], oracle['payloads'][phase])
            assert doc['size'] == len(content.encode() if isinstance(content, str) else content)
            assert doc['type'] == ('plain' if suffix == '.md' else 'newnote')
            await tracker.checkpoint()
        await b.write_note(path, content, create=True)
        await check(path, 'initial')
        content += b'\x00\xfftail' if isinstance(content, bytes) else '\nДобавлено🙂'
        await b.write_note(path, content)
        await check(path, 'updated')
        await b.copy_note(path, 'copy' + suffix)
        await check('copy' + suffix, 'updated')
        await b.rename_note('copy' + suffix, 'moved' + suffix)
        await check('moved' + suffix, 'updated')
        await b.delete_note(path)
        for old in (path, 'copy' + suffix):
            doc = await b._get_doc(b._path2id(old))
            assert doc['deleted'] is True and doc['children'] == oracle['payloads']['updated']['children']
            assert await b.read_note(old) is None
        await tracker.checkpoint()
        assert tracker.chunk_ids == tracker.referenced == set(tracker.expected)
        fresh = peer(db)
        try:
            await fresh.startup_check()
            assert await fresh.read_note('moved' + suffix) == await b.read_note('moved' + suffix)
        finally:
            await fresh.close()
    finally:
        await b.close()


async def test_real_couchdb_surrogate(raw_client, scratch_database):
    # chunks.ts:243-252 cuts UTF-16; real CouchDB 3.5.3 rejects lone escapes.
    from storage_base import WriterParameterRefused
    from diagnostic_codes import DiagnosticCode
    await seed(raw_client, scratch_database, W['settings'])
    b = peer(scratch_database)
    requests = []
    async def capture(request):
        requests.append(request.method)
    try:
        await b.startup_check()
        b._client.event_hooks['request'].append(capture)
        before = (await raw_client.get(scratch_database)).json()['update_seq']
        with pytest.raises(WriterParameterRefused) as caught:
            await b._write_content(W['path'], W['input'], len(W['input'].encode()), 'plain', create=True, overwrite=False)
        assert caught.value.diagnostic.code == DiagnosticCode.COUCHDB_SURROGATE_REFUSED
        assert requests == []
        assert (await raw_client.get(scratch_database)).json()['update_seq'] == before
        # Device-shaped JSON.stringify body, independent of peer serialization.
        response = await raw_client.put(scratch_database + '/device-surrogate',
            content=b'{"_id":"device-surrogate","type":"leaf","data":"\\ud800"}',
            headers={'Content-Type': 'application/json'})
        assert response.status_code == 400
        assert response.json() == {'error':'bad_request','reason':'invalid UTF-8 JSON'}
        assert (await raw_client.get(scratch_database + '/device-surrogate')).status_code == 404
    finally:
        await b.close()


@pytest.mark.parametrize('algorithm', ['HS256', 'HS512', 'ES256', 'ES512'])
async def test_real_jwt(raw_client, scratch_database, algorithm):
    # Pinned httplib.ts:110-142 signs raw ECDSA or HMAC, :166-196 caches it.
    required = ['COUCHDB_TEST_JWT_URL', 'COUCHDB_TEST_JWT_SUBJECT',
                'COUCHDB_TEST_JWT_HS_KEY' if algorithm.startswith('HS') else ('COUCHDB_TEST_JWT_ES_KEY' if algorithm == 'ES256' else 'COUCHDB_TEST_JWT_ES512_KEY')]
    if not all(os.environ.get(k) for k in required):
        pytest.skip('JWT acceptance environment absent')
    cfg = load_config(dict(LIVESYNC_BACKEND='couchdb', MCP_API_KEY='acceptance-only',
        COUCHDB_URL=os.environ[required[0]], COUCHDB_DATABASE=scratch_database,
        COUCHDB_JWT_ALGORITHM=algorithm, COUCHDB_JWT_SUBJECT=os.environ[required[1]],
        COUCHDB_JWT_KEY=os.environ[required[2]],
        COUCHDB_JWT_KEY_ID=os.environ.get('COUCHDB_TEST_JWT_KEY_ID_ES512', '') if algorithm == 'ES512' else '')).couchdb
    b = CouchDBBackend(cfg.url, '', '', scratch_database, jwt_config=cfg)
    try:
        await b.startup_check()
        await b.write_note('jwt.md', 'JWT roundtrip', create=True)
        assert (await b.read_note('jwt.md'))['text'] == 'JWT roundtrip'
    finally:
        await b.close()


async def test_real_jwt_wrong_key(raw_client, scratch_database):
    from diagnostic_codes import DiagnosticCode
    from storage_base import StorageUnavailable
    import base64
    if not all(os.environ.get(k) for k in ('COUCHDB_TEST_JWT_URL', 'COUCHDB_TEST_JWT_SUBJECT', 'COUCHDB_TEST_JWT_HS_KEY')):
        pytest.skip('JWT acceptance environment absent')
    # A synthetic wrong key, never the controller key or any part of it.
    wrong = base64.b64encode(b'acceptance-wrong-key-32-bytes-long').decode()
    cfg = load_config(dict(LIVESYNC_BACKEND='couchdb', MCP_API_KEY='acceptance-only',
        COUCHDB_URL=os.environ['COUCHDB_TEST_JWT_URL'], COUCHDB_DATABASE=scratch_database,
        COUCHDB_JWT_ALGORITHM='HS256', COUCHDB_JWT_SUBJECT=os.environ['COUCHDB_TEST_JWT_SUBJECT'],
        COUCHDB_JWT_KEY=wrong)).couchdb
    b = CouchDBBackend(cfg.url, '', '', scratch_database, jwt_config=cfg)
    try:
        with pytest.raises(StorageUnavailable) as caught:
            await b.startup_check()
        assert wrong not in caught.value.format()
        assert caught.value.diagnostic.code == DiagnosticCode.COUCHDB_JWT_AUTHORIZATION_ERROR
    finally:
        await b.close()


@pytest.mark.parametrize('case', MATRIX, ids=case_id)
async def test_real_couchdb_each_row_classes(raw_client, scratch_database, case):
    # EntryManagerImpls.ts:133-159 persists plain/newnote plus leaf documents.
    # The _local milestone is also seeded and consumed independently per row.
    await seed(raw_client, scratch_database, settings_for(case))
    b = peer(scratch_database)
    try:
        await b.startup_check()
        for name, path, content, kind in [('uncertain', 'uncertain.md', 'accepted', 'plain'),
                ('boundary', 'boundary.png', b'\x00' * 1048576, 'newnote')]:
            await b.write_note(path, content, create=True)
            metadata = await b._get_doc(b._path2id(path))
            assert metadata['type'] == kind
            pieces = [(await stored_doc(raw_client, scratch_database, c))['data'] for c in metadata['children']]
            assert_partition(pieces, metadata['children'], matrix_vector(case)['payloads'][name])
            fresh = peer(scratch_database)
            try:
                await fresh.startup_check()
                result = await fresh.read_note(path)
                assert result['text' if kind == 'plain' else 'data'] == content
            finally:
                await fresh.close()
    finally:
        await b.close()


async def test_real_jwt_expired(raw_client, scratch_database):
    # httplib.ts:166-196 signs the supplied clock and expiration in seconds.
    from diagnostic_codes import DiagnosticCode
    from storage_base import StorageUnavailable
    if not os.environ.get('COUCHDB_TEST_JWT_URL'):
        pytest.skip('JWT acceptance environment absent')
    cfg = load_config(dict(LIVESYNC_BACKEND='couchdb', MCP_API_KEY='acceptance-only',
        COUCHDB_URL=os.environ['COUCHDB_TEST_JWT_URL'], COUCHDB_DATABASE=scratch_database,
        COUCHDB_JWT_ALGORITHM='HS256', COUCHDB_JWT_SUBJECT=os.environ['COUCHDB_TEST_JWT_SUBJECT'],
        COUCHDB_JWT_KEY=os.environ['COUCHDB_TEST_JWT_HS_KEY'])).couchdb
    b = CouchDBBackend(cfg.url, '', '', scratch_database, jwt_config=cfg)
    b._jwt._clock = lambda: 1_600_000_000
    try:
        with pytest.raises(StorageUnavailable) as caught:
            await b.startup_check()
        assert caught.value.diagnostic.code == DiagnosticCode.COUCHDB_JWT_AUTHORIZATION_ERROR
    finally:
        await b.close()


async def test_real_compression(raw_client, scratch_database):
    # compress.ts:49-71 always decompresses, independent of write setting.
    from tests.test_compression import VECTORS, MARKER
    await seed(raw_client, scratch_database, {'enableCompression': True})
    b = peer(scratch_database)
    try:
        await b.startup_check()
        await b.write_note('compressed.md', 'compressible' * 4000, create=True)
        metadata = await b._get_doc(b._path2id('compressed.md'))
        leaves = [await stored_doc(raw_client, scratch_database, c) for c in metadata['children']]
        assert any(d['data'].startswith(MARKER) for d in leaves)
        fresh = peer(scratch_database)
        try:
            await fresh.startup_check()
            assert (await fresh.read_note('compressed.md'))['text'] == 'compressible' * 4000
        finally:
            await fresh.close()
        vectors = [v for v in VECTORS if not v['error'] and v['name'] != 'marked' and isinstance(v['compressed'], str)]
        for index, v in enumerate(vectors):
            response = await raw_client.put(scratch_database + '/preseed-' + str(index),
                json={'_id': 'preseed-' + str(index), 'type': 'leaf', 'data': v['compressed']})
            assert response.status_code == 201
        for enabled in (False, True):
            fresh = peer(scratch_database)
            try:
                await fresh.startup_check()
                fresh.enable_compression = enabled
                for index, v in enumerate(vectors):
                    assert (await fresh._get_doc('preseed-' + str(index)))['data'] == v['decompressed']
            finally:
                await fresh.close()
    finally:
        await b.close()


async def test_real_couchdb_request_headers(raw_client, scratch_database):
    # LiveSyncReplicator.ts:2071-2098 passes headers to the entire connection.
    b = peer(scratch_database, custom_headers=(('X-Parity-Probe', 'f4-public'),))
    seen = set()
    async def capture(request):
        assert request.headers.get('X-Parity-Probe') == 'f4-public'
        seen.add((request.method, request.url.path.rsplit('/', 1)[-1]))
    b._client.event_hooks['request'].append(capture)
    try:
        await b.startup_check()
        await b.write_note('headers.md', 'header probe', create=True)
        await b.read_note('headers.md')
        await b.list_notes()
        await b.copy_note('headers.md', 'headers-copy.md')
        await b.rename_note('headers-copy.md', 'headers-moved.md')
        await b.delete_note('headers.md')
        await b._put_doc('_local/f4-header-probe', {'value':'public'})
        await b._get_doc('_local/f4-header-probe')
        await b._request('GET', '_session')
        assert {'GET', 'POST', 'PUT'} <= {method for method, _ in seen}
        assert any(name.startswith('_local') or name == 'obsydian_livesync_milestone' for _, name in seen)
        assert any(name in ('_all_docs', '_changes') for _, name in seen)
    finally:
        await b.close()
