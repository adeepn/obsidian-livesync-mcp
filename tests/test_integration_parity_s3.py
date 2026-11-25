"""Real S3 acceptance; C:journalCompatibility.ts:60-71; chunks.ts:218-252.

Uses the committed, integrity-checked published-dependency oracle, never peer
splitting as expected output. No configured credentials means no network I/O.
"""
import base64
import json
import os

import pytest

from storage_journal import MILESTONE_KEY
from tests.test_integration_minio import minio, requires_minio, published_keys
from tests.settings_oracle_inputs import CASES, BRANCH_CASES, case_id, settings_for, payload
from tests.settings_oracle import matrix_vector, assert_partition, expected_pieces
from tests.test_surrogate_wire import FIXTURE, W, REPLACEMENTS

pytestmark = requires_minio
MATRIX = CASES + BRANCH_CASES if os.environ.get('MINIO_TEST_MATRIX') == 'full' else CASES


async def seed_settings(peer, settings):
    seed, _ = await peer('seed')
    seed._object_storage.put(MILESTONE_KEY, json.dumps({'tweak_values': {'PREFERRED': settings}}).encode())


@pytest.mark.parametrize('case', MATRIX, ids=case_id)
async def test_real_s3_matrix(minio, case):
    peer, keys, _, _, _ = minio
    settings = settings_for(case)
    await seed_settings(peer, settings)
    writer, spy = await peer('writer')
    oracle = matrix_vector(case)
    suffix, content = payload(case[-1])
    path = 'Папка/Cafe\u0301' + suffix
    previous = set(keys())

    async def check(path, phase):
        metadata = writer._store.winner(writer._doc_id(path))
        children = metadata['children']
        pieces = writer._store._chunk_datas(children, {}, path)
        assert_partition(pieces, children, oracle['payloads'][phase])
        expected = ''.join(expected_pieces(oracle['payloads'][phase])) if metadata['type'] == 'plain' else b''.join(base64.b64decode(p) for p in expected_pieces(oracle['payloads'][phase]))
        reader, _ = await peer('reader-' + phase + '-' + str(len(keys())))
        await reader.refresh(force=True)
        note = await reader.read_note(path)
        assert note['text' if metadata['type'] == 'plain' else 'data'] == expected
        nonlocal previous
        current = set(keys())
        assert previous <= current
        previous = current
        published_keys(spy)

    await writer.write_note(path, content, create=True)
    await check(path, 'initial')
    content += b'\x00\xfftail' if isinstance(content, bytes) else '\nДобавлено🙂'
    await writer.write_note(path, content)
    await check(path, 'updated')
    await writer.copy_note(path, 'copy' + suffix)
    await check('copy' + suffix, 'updated')
    await writer.rename_note('copy' + suffix, 'moved' + suffix)
    await check('moved' + suffix, 'updated')
    assert await writer.read_note('copy' + suffix) is None
    assert await writer.delete_note(path)
    reader, _ = await peer('final-reader')
    await reader.refresh(force=True)
    assert await reader.read_note(path) is None
    assert await reader.read_note('copy' + suffix) is None
    assert await reader.read_note('moved' + suffix) is not None
    published_keys(spy)


async def test_real_s3_surrogate(minio):
    # Native journalRecords.ts:69-108 replaces lone units with U+FFFD.
    peer, _, _, _, _ = minio
    await seed_settings(peer, W['settings'])
    writer, spy = await peer('writer')
    result = await writer._write_content(W['path'], W['input'], len(W['input'].encode()), 'plain', create=True, overwrite=False)
    assert result['transport_replacements'] == REPLACEMENTS
    reader, _ = await peer('reader')
    await reader.refresh(force=True)
    pieces = reader._store._chunk_datas(W['ids'], {}, W['path'])
    assert pieces == [d['data'] for d in FIXTURE['vectors'][0]['decoded']]
    assert (await reader.read_note(W['path']))['text'] == ''.join(pieces)
    published_keys(spy)
