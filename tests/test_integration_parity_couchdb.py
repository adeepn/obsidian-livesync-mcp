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
    # chunks.ts:243-252 V1 slices UTF-16 units; JSON retains lone units.
    await seed(raw_client, scratch_database, W['settings'])
    b = peer(scratch_database)
    try:
        await b.startup_check()
        await b._write_content(W['path'], W['input'], len(W['input'].encode()), 'plain', create=True, overwrite=False)
        bodies = []
        for child, piece in zip(W['ids'], W['pieces']):
            r = await raw_client.get(b._doc_path(child))
            assert r.status_code == 200
            assert r.json()['data'] == piece
            bodies.append(r.content)
        assert any(b'\\ud83d' in body for body in bodies)
        fresh = peer(scratch_database)
        try:
            await fresh.startup_check()
            assert (await fresh.read_note(W['path']))['text'] == W['input']
        finally:
            await fresh.close()
    finally:
        await b.close()


@pytest.mark.parametrize('algorithm', ['HS256', 'ES256'])
async def test_real_jwt(raw_client, scratch_database, algorithm):
    # Pinned httplib.ts:110-142 signs raw ECDSA or HMAC, :166-196 caches it.
    required = ['COUCHDB_TEST_JWT_URL', 'COUCHDB_TEST_JWT_SUBJECT',
                'COUCHDB_TEST_JWT_HS_KEY' if algorithm == 'HS256' else 'COUCHDB_TEST_JWT_ES_KEY']
    if not all(os.environ.get(k) for k in required):
        pytest.skip('JWT acceptance environment absent')
    cfg = load_config(dict(LIVESYNC_BACKEND='couchdb', MCP_API_KEY='acceptance-only',
        COUCHDB_URL=os.environ[required[0]], COUCHDB_DATABASE=scratch_database,
        COUCHDB_JWT_ALGORITHM=algorithm, COUCHDB_JWT_SUBJECT=os.environ[required[1]],
        COUCHDB_JWT_KEY=os.environ[required[2]])).couchdb
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
        assert caught.value.diagnostic.code == DiagnosticCode.COUCHDB_AUTHORIZATION_ERROR
    finally:
        await b.close()
