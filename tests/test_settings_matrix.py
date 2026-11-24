"""Pairwise device settings through real backends and in-memory transports.

The fixed covering array below was selected by deterministic greedy set cover
(seed 20261124), with seven independent factors. Each backend runs the SAME
array. A separate test recomputes the Cartesian requirements for every pair,
including the four legacy migration flag combinations, omitted defaults, BOM,
all textual-newnote suffixes, binary and empty documents. Backend is crossed
with the array, so backend/value and backend/pair coverage are both complete.
This proves pairwise interactions, not the exhaustive Cartesian product.

Avoid only the unnecessary v3/custom=60/binary triple: its >4 MiB multi-piece
input cannot be admitted at limit=1. All pairs remain covered. Binary cases
use delimiter-rich PNG-like bytes and must publish multiple pieces. For size
policy, every row also probes exact 1 MiB admission and 1 MiB+1 skip/unlimited
admission (default 50 admits both). No default-50 boundary allocation is needed:
Task 5's boundary tests own the 50 MiB edge.

Every row asserts exact ordered pieces and chunk IDs from the independent
pinned Node oracle, on both backends, without a fallback. Inputs are shared
with the generator; expected partitions never use production splitting/hash.
No Node or network is needed to run tests.
"""
import base64
from contextlib import asynccontextmanager
from itertools import combinations, product
import json
from pathlib import Path
import unicodedata

import httpx
import pytest

import server
from content_splitter import ContentSplitter
from livesync_format import path2id
from storage_base import ConcurrentModification, NoteExists, OutcomeUnknown, WriteSkipped, RenameRefused, StorageUnavailable, check_file_size
from storage_journal import JournalBackend, MILESTONE_KEY, SYNC_PARAMETERS_KEY
from tests.test_storage_couchdb import fake_vault, stored_doc
from tests.test_storage_journal import make_backend, params_blob

from tests.settings_oracle_inputs import (
    BACKENDS, FACTORS, ROWS, CASES, case_id, settings_for, payload,
)
from tests.settings_oracle import matrix_vector, assert_partition

FIXTURES = Path(__file__).parent / 'fixtures'
HASHES = json.loads((FIXTURES / 'hash_algorithms_upstream.json').read_text())
HASH_HELLO = next(v['ids'] for v in HASHES['vectors'] if v['recipe'] == {'text': 'helloWorld'})
BLOB_ORACLE = json.loads((FIXTURES / 'ingested_blob_size_upstream.json').read_text())['vectors']


def test_covering_array_is_complete_on_each_backend():
    # Deliberately independent of the selection algorithm; adding a level fails
    # until the actual parametrization covers it. Both backends use CASES below.
    assert set(BACKENDS) == {'couchdb', 'journal'}
    for backend in BACKENDS:
        assert len(set(CASES)) == len(CASES), backend
        for i, (_, values) in enumerate(FACTORS):
            assert {row[i] for row in CASES} == set(values), (backend, i)
        for i, j in combinations(range(len(FACTORS)), 2):
            assert {(row[i], row[j]) for row in CASES} == set(product(
                FACTORS[i][1], FACTORS[j][1])), (backend, FACTORS[i][0], FACTORS[j][0])


@asynccontextmanager
async def matrix_vault(tmp_path, kind, settings):
    milestone = {'tweak_values': {'PREFERRED': settings, 'device': dict(settings)}}
    if kind == 'couchdb':
        async with fake_vault({'_local/obsydian_livesync_milestone': milestone}) as (backend, raw, log):
            async def doc(path):
                return await stored_doc(raw, path if path.startswith('h:') else path2id(
                    path, nfc=bool(settings['pathNormalizationVersion']),
                    case_sensitive=settings['handleFilenameCaseSensitive']))
            async def chunks():
                response = await raw.get('obsidian/_all_docs', params={'include_docs': 'true'})
                return {r['id']: r['doc'] for r in response.json()['rows'] if r['id'].startswith('h:')}
            yield backend, doc, chunks, log, None
    else:
        backend, storage = make_backend(tmp_path / 'writer', {
            MILESTONE_KEY: json.dumps(milestone).encode(), SYNC_PARAMETERS_KEY: params_blob()})
        async def doc(path):
            if path.startswith('h:'):
                return {'data': backend._store._chunk_datas([path], {}, path)[0]}
            return backend._store.winner(backend._doc_id(path))
        async def chunks():
            return dict(storage._objects)
        try:
            yield backend, doc, chunks, storage.put_calls, storage
        finally:
            await backend.close()


def projected(content, plain, version):
    if not plain:
        return content.encode() if isinstance(content, str) else content
    # Device legacy Blob.text drops BOM; Task 4's independent size oracle.
    oracle = next(v for v in BLOB_ORACLE if v['version'] == version and v['input'] == '\ufeffabc')
    if content.startswith('\ufeff') and oracle['text'] == 'abc':
        return content[1:]
    return content


async def assert_note(backend, doc, path, content, version, expected):
    plain = path.endswith('.md')
    note = await backend.read_note(path)
    assert note is not None
    assert note['meta']['type'] == ('plain' if plain else 'newnote')
    assert note['meta']['size'] == len(content.encode() if isinstance(content, str) else content)
    assert note['text' if plain else 'data'] == projected(content, plain, version)
    metadata = await doc(path)
    assert metadata['eden'] == {}
    children = metadata['children']
    pieces = await pieces_for(backend, children)
    # Children are ordered, including repeated IDs; physical chunks are deduped.
    assert_partition(pieces, children, expected)
    if plain:
        assert ''.join(pieces) == projected(content, True, version)
    else:
        assert b''.join(base64.b64decode(p, validate=True) for p in pieces) == projected(content, False, version)
    return note


async def pieces_for(backend, children):
    if isinstance(backend, JournalBackend):
        return backend._store._chunk_datas(children, {}, 'matrix')
    return [(await backend._get_doc(child))['data'] for child in children]


async def readable_snapshot(backend):
    return {m['path']: await backend.read_note(m['path']) for m in await backend.list_notes()}


async def assert_preserved(backend, before):
    for name, note in before.items():
        assert await backend.read_note(name) == note, name


async def size_and_outcomes(backend, doc, reader, storage, monkeypatch, writes, oracle):
    """Real boundary writes plus temporary one-byte policy refusal probes.

    The low temporary limit exercises skip/refused reporting even in unlimited
    rows; it is restored before all other operations. No incoming mutations or
    direct metadata-size edits are used to manufacture an oversized source.
    """
    def mutations():
        return len(writes) if storage else sum(
            r.method in ('PUT', 'DELETE') or
            (r.method == 'POST' and not r.url.path.endswith('/_all_docs'))
            for r in writes)

    limit = backend.sync_max_size_mb
    assert limit in (0, 1, 50)
    # Count-only default-50 edge avoids allocating another 50 MiB in every row.
    check_file_size('policy.png', int((limit or 50) * 1048576), limit)
    if limit:
        with pytest.raises(WriteSkipped):
            check_file_size('policy.png', int(limit * 1048576) + 1, limit)
    else:
        check_file_size('policy.png', 51 * 1048576, limit)
    data = b'\x00' * 1048576
    await backend.write_attachment('boundary.png', data, create=True)
    assert (await doc('boundary.png'))['size'] == len(data)
    children = (await doc('boundary.png'))['children']
    assert_partition(await pieces_for(backend, children), children, oracle['payloads']['boundary'])
    before = await readable_snapshot(backend)
    if limit == 1:
        writes_before = mutations()
        with pytest.raises(WriteSkipped) as caught:
            await backend.write_attachment('boundary.png', data + b'x')
        assert caught.value.as_dict()['status'] == 'skipped'
        assert 'new content was NOT saved' in server._storage_message(caught.value)
        await assert_preserved(backend, before)
        assert mutations() == writes_before
    else:
        await backend.write_attachment('boundary.png', data + b'x')
        data += b'x'
        children = (await doc('boundary.png'))['children']
        assert_partition(await pieces_for(backend, children), children, oracle['payloads']['boundary-plus'])
    assert (await backend.read_note('boundary.png'))['data'] == data
    if reader:
        await reader.refresh(force=True)
        assert (await reader.read_note('boundary.png'))['data'] == data

    before = await readable_snapshot(backend)
    backend.sync_max_size_mb = 1 / 1048576
    writes_before = mutations()
    try:
        with pytest.raises(WriteSkipped):
            await backend.write_note('oracle.md', 'larger')
        response = json.loads(await server.obsidian_update_note('oracle.md', 'overwrite', 'larger'))
        assert response['status'] == 'skipped'
        assert 'syncMaxSizeInMB' in response['message']
        with pytest.raises(RenameRefused) as caught:
            await backend.rename_note('oracle.md', 'refused.md')
        assert 'move was not performed' in server._storage_message(caught.value)
        response = await server.obsidian_move_note('oracle.md', 'refused.md')
        assert 'move was not performed' in response
        await assert_preserved(backend, before)
        assert await backend.read_note('refused.md') is None
        assert mutations() == writes_before
    finally:
        backend.sync_max_size_mb = limit

    # Lose acknowledgement AFTER acceptance of a new peer write. Existing notes
    # must survive; the destination is allowed to appear and converges on replay.
    with monkeypatch.context() as patch:
        if storage:
            publish = storage.put_if_absent
            def lost(*args):
                publish(*args)
                raise StorageUnavailable('injected lost acknowledgement')
            patch.setattr(storage, 'put_if_absent', lost)
        else:
            request = backend._client.request
            async def lost(method, path, **kwargs):
                result = await request(method, path, **kwargs)
                if method == 'PUT' and path.endswith('/uncertain.md'):
                    raise httpx.ReadError('injected lost acknowledgement')
                return result
            patch.setattr(backend._client, 'request', lost)
        with pytest.raises(OutcomeUnknown) as caught:
            await backend.write_note('uncertain.md', 'accepted', create=True)
        response = server._storage_message(caught.value)
        assert response.startswith('outcome unknown') and 're-read' in response
    await backend.refresh(force=True)
    assert (await backend.read_note('uncertain.md'))['text'] == 'accepted'
    children = (await doc('uncertain.md'))['children']
    assert_partition(await pieces_for(backend, children), children, oracle['payloads']['uncertain'])
    await assert_preserved(backend, before)
    if reader:
        await reader.refresh(force=True)
        await assert_preserved(reader, before)
        assert (await reader.read_note('uncertain.md'))['text'] == 'accepted'


@pytest.mark.parametrize('kind', BACKENDS)
@pytest.mark.parametrize('case', CASES, ids=case_id)
async def test_settings_full_cycle(tmp_path, monkeypatch, kind, case):
    def no_subprocess(*args, **kwargs):
        pytest.fail('default selection must never launch Node/a subprocess')
    monkeypatch.setattr('subprocess.Popen', no_subprocess)
    monkeypatch.setattr(server, 'backend', None)
    settings = settings_for(case)
    async with matrix_vault(tmp_path, kind, settings) as (backend, doc, snapshot, writes, storage):
        await backend.startup_check()
        info = await backend.backend_info()
        assert info['writer_supported'] is True
        assert info['tweak_assessment'] == {'status': 'OK', 'differing_keys': []}
        if kind == 'journal':
            assert not writes
        else:
            assert all(r.method == 'GET' for r in writes)
        monkeypatch.setattr(server, 'backend', backend)
        splitter = ContentSplitter(settings)
        version = 'v2-segmenter' if splitter.normalized_settings['chunkSplitterVersion'] == 'v2-segmenter' else splitter.version
        suffix, content = payload(case[-1])
        path = 'Папка/Cafe\u0301' + suffix
        copy = 'Копия/Cafe\u0301' + suffix
        moved = 'Перенос/Cafe\u0301' + suffix
        # Independent pinned hash oracle through storage, with every algorithm.
        result = await server.obsidian_create_note('oracle.md', 'helloWorld')
        assert 'Created' in result
        assert (await doc('oracle.md'))['children'] == [HASH_HELLO[case[2]]]
        oracle = matrix_vector(case)
        children = (await doc('oracle.md'))['children']
        assert_partition(await pieces_for(backend, children), children, oracle['payloads']['hash'])
        # Exact Task 4 Blob-size/BOM fixture, independently of splitter output.
        blob = next(v for v in BLOB_ORACLE if v['version'] == version and v['input'] == '\ufeffabc')
        meta = await backend.write_note('blob-size.md', blob['input'], create=True)
        assert meta['size'] == blob['size']
        assert (await backend.read_note('blob-size.md'))['text'] == blob['text']
        children = (await doc('blob-size.md'))['children']
        assert_partition(await pieces_for(backend, children), children, oracle['payloads']['blob'])
        immutable = await snapshot()

        async def unchanged_chunks():
            current = await snapshot()
            assert all(current.get(key) == value for key, value in immutable.items())
            immutable.update(current)
            if kind == 'journal':
                keys = [key for key, _ in writes]
                assert len(keys) == len(set(keys)), 'journal object overwritten'
            else:
                assert not any(r.method == 'DELETE' and '/h%3A' in str(r.url) for r in writes)

        reader = JournalBackend(storage, tmp_path / 'reader') if storage else None
        if reader:
            await reader.startup_check()

        phase = 'initial'

        async def check(path, content):
            note = await assert_note(backend, doc, path, content, version, oracle['payloads'][phase])
            if reader:
                await reader.refresh(force=True)
                other = await reader.read_note(path)
                assert other == note
            await unchanged_chunks()
            return note

        try:
            await backend.write_note(path, content, create=True)
            await check(path, content)
            metadata = await doc(path)
            expected_id = unicodedata.normalize('NFC', path) if 'nfc' in case[0] else path
            if 'sensitive' not in case[0]:
                expected_id = expected_id.lower()
            assert metadata['_id'] == expected_id
            alias = path.swapcase()
            assert (await backend.read_note(alias) is not None) is ('sensitive' not in case[0])
            if case[-1] == 'binary':
                assert len((await doc(path))['children']) > 1
            append = b'\x00\xfftail' if isinstance(content, bytes) else '\nДобавлено🙂'
            content += append
            phase = 'updated'
            await backend.write_note(path, content)
            await check(path, content)
            await backend.copy_note(path, copy)
            await check(copy, content)
            await backend.rename_note(copy, moved)
            await check(moved, content)
            assert await backend.read_note(copy) is None
            if reader:
                assert await reader.read_note(copy) is None
            # Peer-initiated refusal/failure must preserve EVERY readable note.
            before = {m['path']: await backend.read_note(m['path']) for m in await backend.list_notes()}
            with pytest.raises(NoteExists):
                await backend.write_note(path, 'collision', create=True)
            with pytest.raises(ConcurrentModification):
                await backend.write_note(path, 'stale', expected_rev='0-stale')
            for name, note in before.items():
                assert await backend.read_note(name) == note
            await unchanged_chunks()
            assert await backend.delete_note(moved) is True
            assert await backend.read_note(moved) is None
            assert (await doc(moved))['deleted'] is True
            await check(path, content)
            if reader:
                assert await reader.read_note(moved) is None
            await size_and_outcomes(backend, doc, reader, storage, monkeypatch, writes, oracle)
            await unchanged_chunks()
            if reader:
                fresh = JournalBackend(storage, tmp_path / 'fresh-final')
                try:
                    await fresh.startup_check()
                    await fresh.refresh(force=True)
                    assert await readable_snapshot(fresh) == await readable_snapshot(backend)
                    assert await fresh.read_note(copy) is None
                    assert await fresh.read_note(moved) is None
                finally:
                    await fresh.close()
        finally:
            if reader:
                await reader.close()
