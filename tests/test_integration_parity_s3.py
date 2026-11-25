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


@pytest.mark.parametrize('case', MATRIX, ids=case_id)
async def test_real_s3_each_row_text_and_binary(minio, case):
    # journalCompatibility.ts:60-71 and EntryManagerImpls.ts:133-159.
    peer, keys, _, _, _ = minio
    await seed_settings(peer, settings_for(case))
    writer, spy = await peer('writer-extra')
    previous = set(keys())
    for name, path, content in [('uncertain', 'uncertain.md', 'accepted'),
                                 ('boundary', 'boundary.png', b'\x00' * 1048576)]:
        await writer.write_note(path, content, create=True)
        metadata = writer._store.winner(writer._doc_id(path))
        pieces = writer._store._chunk_datas(metadata['children'], {}, path)
        assert_partition(pieces, metadata['children'], matrix_vector(case)['payloads'][name])
        fresh, _ = await peer('extra-' + name)
        await fresh.refresh(force=True)
        result = await fresh.read_note(path)
        assert fresh._store.winner(fresh._doc_id(path))['children'] == metadata['children']
        assert result['text' if isinstance(content, str) else 'data'] == content
        assert previous <= set(keys())
        previous = set(keys())
    published_keys(spy)


async def test_real_s3_signed_headers(minio):
    # MinioStorageAdapter.ts:86-100 adds headers in finalizeRequest, priority low.
    from object_storage import Boto3ObjectStorage
    peer, _, _, _, _ = minio
    b, spy = await peer('header-owner')
    from config import S3Config
    from tests.test_object_storage import MINIO_ENV
    cfg = S3Config(endpoint=MINIO_ENV['MINIO_TEST_ENDPOINT'],
        access_key=MINIO_ENV['MINIO_TEST_KEY'], secret_key=MINIO_ENV['MINIO_TEST_SECRET'],
        bucket=MINIO_ENV['MINIO_TEST_BUCKET'], region='us-east-1',
        prefix=minio[4], uri='', custom_headers=(('X-Parity-Probe', 'f4-public'),))
    adapter = Boto3ObjectStorage(cfg)
    seen = []
    def capture(request, **kwargs):
        assert request.headers.get('X-Parity-Probe') == b'f4-public'
        assert 'Authorization' in request.headers
        seen.append(request.method)
    adapter._client.meta.events.register_last('before-send.s3', capture)
    try:
        assert adapter._client.meta.config.s3['addressing_style'] == 'path'
        adapter.put('f4-header-probe', b'public probe')
        assert adapter.get('f4-header-probe') == b'public probe'
        assert adapter.exists('f4-header-probe')
        adapter.list()
        assert {'PUT', 'GET', 'HEAD'} <= set(seen)
    finally:
        adapter._client.close()


@pytest.mark.parametrize('mib', [40, 100])
async def test_real_s3_large_binary(minio, mib, record_property):
    # VaultService.ts:73-81 permits <=200 MiB; native chunks.ts:342-433.
    import hashlib
    from pathlib import Path
    from storage_base import WriteSkipped, RenameRefused
    oracle = json.loads(Path('tests/fixtures/acceptance_upstream.json').read_text())
    vector = next(v for v in oracle['rows'] if v['size_bytes'] == mib * 1048576)
    peer, keys, _, _, _ = minio
    await seed_settings(peer, vector['settings'])
    writer, spy = await peer('large-writer')
    data = bytearray(b'a' * vector['size_bytes'])
    for offset in range(99999, len(data), 100000):
        data[offset] = 0
    data = bytes(data)
    record_property('input_sha256', hashlib.sha256(data).hexdigest())
    await writer.write_attachment(vector['path'], data, create=True)
    metadata = writer._store.winner(writer._doc_id(vector['path']))
    assert metadata['children'] == vector['children']
    pieces = writer._store._chunk_datas(metadata['children'], {}, vector['path'])
    assert [hashlib.sha256(p.encode()).hexdigest() for p in pieces] == [p['data_sha256'] for p in vector['pieces']]
    reader, _ = await peer('large-fresh-reader')
    await reader.refresh(force=True)
    assert (await reader.read_note(vector['path']))['data'] == data
    remote_metadata = reader._store.winner(reader._doc_id(vector['path']))
    assert remote_metadata['children'] == vector['children']
    remote_pieces = reader._store._chunk_datas(remote_metadata['children'], {}, vector['path'])
    assert [hashlib.sha256(p.encode()).hexdigest() for p in remote_pieces] == [p['data_sha256'] for p in vector['pieces']]
    previous = set(keys())
    if mib == 40:
        too_large = b'\x00' * (201 * 1048576)
        record_property('oversize_sha256', hashlib.sha256(too_large).hexdigest())
        with pytest.raises(WriteSkipped):
            await writer.write_attachment('oversize.png', too_large, create=True)
        assert set(keys()) == previous
        assert await writer.read_note('oversize.png') is None
        writer.sync_max_size_mb = 1
        with pytest.raises(RenameRefused):
            await writer.rename_note(vector['path'], 'renamed.png')
        assert set(keys()) == previous
        assert (await writer.read_note(vector['path']))['data'] == data
        assert await writer.read_note('renamed.png') is None
    published_keys(spy)


async def test_real_s3_large_text_mcp(minio, monkeypatch):
    # EntryManagerImpls.ts:272-280 types .json as newnote; tools use create_app.
    import server
    from config import load_config
    from tests.test_config import COUCHDB_ENV
    peer, _, _, _, _ = minio
    await seed_settings(peer, {'syncMaxSizeInMB':200, 'chunkSplitterVersion':'v2'})
    writer, _ = await peer('text-mcp')
    monkeypatch.setattr(server, 'get_backend', lambda cfg: writer)
    app = server.create_app(load_config(COUCHDB_ENV))
    marker = '\nNEAR_END_F4\n'
    text = 'a' * (12 * 1048576 - len(marker)) + marker
    assert len(text.encode()) == 12 * 1048576
    async with app.settings.lifespan(app):
        async def call(name, **arguments):
            result = await app.call_tool(name, arguments)
            return result[1]['result']
        created = await call('obsidian_create_note', path='large.json', content=text)
        assert 'Error' not in created
        assert writer._store.winner(writer._doc_id('large.json'))['type'] == 'newnote'
        assert json.loads(await call('obsidian_fetch', path='large.json'))['content'] == text
        assert 'NEAR_END_F4' in await call('obsidian_fetch', path='large.json', start_line=2, end_line=2)
        assert 'large.json' in await call('obsidian_search', query='NEAR_END_F4')
        assert 'Error' not in await call('obsidian_update_note', path='large.json', mode='append', content='F4_APPEND')
        assert 'Error' not in await call('obsidian_update_note', path='large.json', mode='search-replace', search='NEAR_END_F4', replace='F4_REPLACED')
        result = json.loads(await call('obsidian_fetch', path='large.json'))['content']
        assert 'F4_REPLACED' in result and 'NEAR_END_F4' not in result and result.endswith('F4_APPEND')
    fresh, _ = await peer('text-fresh')
    await fresh.refresh(force=True)
    assert (await fresh.read_text_note('large.json'))['text'] == result


async def test_real_s3_recovery_cache_identity(minio):
    # JournalSyncCore.ts:275 resets transfer caches; journalCompatibility.ts:60-75
    # constrains path identity. The peer quarantines incompatible local state.
    import hashlib
    from storage_journal import SYNC_PARAMETERS_KEY
    from tests.test_storage_journal import params_blob, milestone_blob
    from journal_store import JournalStore
    peer, _, wipe, seed, _ = minio
    writer, _ = await peer('recovery-writer')
    await writer.write_note('recover.md', 'cached recovery body', create=True)
    reader, _ = await peer('recovery-reader')
    await reader.refresh(force=True)
    await reader.write_note('sentinel.md', 'own outbound wipe sentinel', create=True)
    assert reader._store.last_sent_key
    original = reader._store.winner(reader._doc_id('recover.md'))
    def chunks():
        directory = reader._state_dir / 'chunks'
        return {str(p.relative_to(directory)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.rglob('*') if p.is_file()}
    before = chunks()
    assert before
    reader._object_storage.put(SYNC_PARAMETERS_KEY, params_blob(protocol=3))
    await reader.startup_check()
    assert reader._store.winner(reader._doc_id('recover.md')) == original
    assert chunks() == before
    # Remove the outbound wipe sentinel, preserving the same local state.
    wipe()
    seed(protocol=3)
    await reader.startup_check()
    assert (await reader.backend_info())['files_processed'] == 0
    assert reader._store.winner(reader._doc_id('recover.md')) == original
    assert chunks() == before
    assert (await reader.read_note('recover.md'))['text'] == 'cached recovery body'
    await reader.close()
    reader._object_storage.put(MILESTONE_KEY, milestone_blob(path_version=0))
    await reader.startup_check()
    assert reader._store.path_identity == 'legacy'
    quarantines = list(reader._state_dir.with_name(reader._state_dir.name + '.quarantine').glob('*/state'))
    assert len(quarantines) == 1
    assert (quarantines[0] / 'journal.sqlite3').is_file()
    old = JournalStore(quarantines[0], 'nfc')
    try:
        assert old.winner('recover.md') == original
        assert {str(p.relative_to(quarantines[0] / 'chunks')):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (quarantines[0] / 'chunks').rglob('*') if p.is_file()} == before
    finally:
        old.close()
    fresh, _ = await peer('recovery-new-identity')
    await fresh.write_note('rebuilt.md', 'rebuilt remote body', create=True)
    await reader.refresh(force=True)
    assert (await reader.read_note('rebuilt.md'))['text'] == 'rebuilt remote body'
    assert await reader.read_note('recover.md') is None


@pytest.mark.parametrize('mode', ['identical', 'absent', 'different', 'unreadable'])
async def test_real_s3_lost_conditional_acknowledgement(minio, monkeypatch, mode):
    """C:MinioStorageAdapter.ts:70-80,132-145 retries native uploads.

    Drop a real accepted PUT response; let SeaweedFS answer the retry. A
    separate harness client simulates disappearance/replacement after 412.
    No oracle is claimed for this peer-only uncertainty safety contract.
    """
    import boto3
    from botocore.config import Config as SDKConfig
    from botocore.exceptions import ReadTimeoutError
    from urllib.parse import unquote, urlsplit
    from config import S3Config
    from object_storage import Boto3ObjectStorage
    from storage_base import OutcomeUnknown, StorageUnavailable
    from tests.test_object_storage import MINIO_ENV

    peer, keys, _, _, prefix = minio
    writer, spy = await peer('retry-writer')
    cfg = S3Config(endpoint=MINIO_ENV['MINIO_TEST_ENDPOINT'],
        access_key=MINIO_ENV['MINIO_TEST_KEY'], secret_key=MINIO_ENV['MINIO_TEST_SECRET'],
        bucket=MINIO_ENV['MINIO_TEST_BUCKET'], region='us-east-1', prefix=prefix,
        uri='', max_attempts=4)
    sdk = Boto3ObjectStorage(cfg)
    # This harness-only client may alter the test object's state, never the peer.
    admin = boto3.client('s3', endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key, aws_secret_access_key=cfg.secret_key,
        region_name=cfg.region, config=SDKConfig(s3={'addressing_style': 'path'},
            connect_timeout=5, read_timeout=30, retries={'total_max_attempts': 1}))
    send = sdk._client._endpoint.http_session.send
    calls = []

    def lose_response(request):
        response = send(request)
        if request.method != 'PUT':
            return response
        key = unquote(urlsplit(request.url).path).split('/', 2)[2]
        calls.append(key)
        if len(calls) == 1:
            assert response.status_code == 200
            raise ReadTimeoutError(endpoint_url='http://synthetic.invalid')
        assert response.status_code == 412
        if mode == 'absent':
            admin.delete_object(Bucket=cfg.bucket, Key=key)
        elif mode == 'different':
            admin.put_object(Bucket=cfg.bucket, Key=key, Body=b'different')
        return response

    monkeypatch.setattr(sdk._client._endpoint.http_session, 'send', lose_response)
    monkeypatch.setattr('botocore.endpoint.time.sleep', lambda _: None)
    if mode == 'unreadable':
        def unreadable(key):
            raise StorageUnavailable()
        monkeypatch.setattr(sdk, 'get', unreadable)
    monkeypatch.setattr(spy, 'put_if_absent', sdk.put_if_absent)
    before = set(keys())
    try:
        if mode == 'identical':
            await writer.write_note('lost.md', 'synthetic payload', create=True)
            assert (await writer.read_note('lost.md'))['text'] == 'synthetic payload'
            assert writer._store.outbound_journals()[0][2] == 1
        else:
            with pytest.raises(OutcomeUnknown):
                await writer.write_note('lost.md', 'synthetic payload', create=True)
            assert writer._store.outbound_journals()[0][2] == 0
            assert writer._store.last_sent_key == ''
        assert len(calls) == 2
        assert calls[0] == calls[1]
        assert len(set(keys()) - before) == (0 if mode == 'absent' else 1)
    finally:
        sdk._client.close()
        sdk._write_client.close()
        admin.close()
