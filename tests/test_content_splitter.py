"""Pinned upstream expectations, extracted by scripts/extract_splitter_vectors.mjs."""
import base64
import json
from pathlib import Path
import zlib

import pytest

from livesync_format import chunk_id_for

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/content_splitter_upstream.json').read_text())


def test_hash_manager_fixed_plaintext_vector():
    vector = next(v for v in FIXTURE['hashes'] if v['algorithm'] == 'XXHASH64')
    assert chunk_id_for(vector['piece']) == 'h:' + vector['expected']


@pytest.mark.parametrize('vector', FIXTURE['vectors'] + FIXTURE['differential'], ids=lambda v: v['name'])
def test_upstream_boundary_vectors(vector):
    from content_splitter import ContentSplitter
    raw = zlib.decompress(base64.b64decode(vector['input_deflate_b64']))
    data = raw.decode() if vector['text'] else raw
    splitter = ContentSplitter({'customChunkSize': vector['maximum'] / 102400 - 1,
                                'minimumChunkSize': vector['minimum']})
    pieces = splitter.split(data, plain_split=vector['plain'])
    decoded = [p.encode() if vector['text'] else base64.b64decode(p, validate=True) for p in pieces]
    assert [len(p) for p in decoded] == vector['expected_lengths']
    assert b''.join(decoded) == raw
    assert [chunk_id_for(piece) for piece in pieces] == vector['expected_ids']


@pytest.mark.parametrize('data', ['', b''])
def test_empty_input_has_no_children(data):
    from content_splitter import ContentSplitter
    assert ContentSplitter().split(data) == []


def test_hash_spec_consistency_and_distinctness():
    ids = [chunk_id_for(p) for p in FIXTURE['repeat_inputs']]
    assert len(set(ids)) == len(ids)
    assert ids == [chunk_id_for(p) for p in FIXTURE['repeat_inputs']]


def test_direct_worker_spec_text_survives():
    from content_splitter import ContentSplitter
    vector = FIXTURE['direct_worker']
    pieces = ContentSplitter({'minimumChunkSize': 1}).split(vector['input'])
    for expected in vector['contains']:
        assert expected in ''.join(pieces)

@pytest.mark.parametrize('settings', [
    {'chunkSplitterVersion': 'v2'}, {'hashAlg': 'sha1'},
    {'minimumChunkSize': -1}, {'minimumChunkSize': float('nan')},
    {'customChunkSize': float('inf')}, {'customChunkSize': True},
    {'idDerivationVersion': 1}, {'idDerivationVersion': 2},
    {'idDerivationKey': 'bad'}, {'encrypt': True},
])
def test_unsupported_settings_refused(settings):
    from content_splitter import ContentSplitter
    with pytest.raises(ValueError):
        ContentSplitter(settings)


def test_valid_unencrypted_independent_key_retains_plain_hash():
    from content_splitter import ContentSplitter
    splitter = ContentSplitter({'idDerivationVersion': 1, 'idDerivationKey': 'ab' * 32})
    assert splitter.chunk_id_for_piece('helloWorld') == 'h:37me21jj44pcc'


def test_long_line_threshold_has_no_v3_effect():
    from content_splitter import ContentSplitter
    text = 'x' * 10000
    assert ContentSplitter({'longLineThreshold': 1}).split(text) == ContentSplitter({'longLineThreshold': 99999}).split(text)


@pytest.mark.parametrize('backend_kind', ['couchdb', 'journal'])
@pytest.mark.parametrize('binary', [False, True])
async def test_writers_publish_ordered_pieces_with_repeated_ids(tmp_path, backend_kind, binary):
    from tests.test_storage_couchdb import fake_vault, stored_doc
    from tests.test_storage_journal import make_backend, params_blob, MILESTONE_KEY, SYNC_PARAMETERS_KEY
    settings = {'pathNormalizationVersion': 0, 'customChunkSize': 1}
    milestone = {'tweak_values': {'PREFERRED': settings}}
    path = 'split.png' if binary else 'split.md'
    data = bytes(420 * 1024) if binary else '\0' * 3000
    lengths = [200 * 1024, 200 * 1024, 20 * 1024] if binary else [1024, 1024, 952]

    async def check(backend, get_doc, get_chunk):
        await backend.startup_check()
        if binary:
            await backend.write_attachment(path, data, create=True)
        else:
            await backend.write_note(path, data, create=True)
        doc = await get_doc(path)
        assert len(doc['children']) == 3
        assert doc['children'][0] == doc['children'][1]
        chunks = [await get_chunk(cid) for cid in doc['children']]
        raw = [base64.b64decode(p, validate=True) if binary else p.encode() for p in chunks]
        assert [len(p) for p in raw] == lengths
        assert b''.join(raw) == (data if binary else data.encode())
        assert doc['size'] == len(data)
        # Empty bodies publish metadata without any leaf, including newnote.
        if binary:
            await backend.write_attachment(path, b'')
        else:
            await backend.write_note(path, '')
        assert (await get_doc(path))['children'] == []

    if backend_kind == 'couchdb':
        async with fake_vault({'_local/obsydian_livesync_milestone': milestone}) as (backend, raw, _):
            async def get_doc(key):
                return await stored_doc(raw, key)
            async def get_chunk(key):
                return (await get_doc(key))['data']
            await check(backend, get_doc, get_chunk)
    else:
        backend, _ = make_backend(tmp_path, {MILESTONE_KEY: json.dumps(milestone).encode(), SYNC_PARAMETERS_KEY: params_blob()})
        try:
            async def get_doc(key):
                return backend._store.winner(key)
            async def get_chunk(key):
                return backend._store._chunk_path(key).read_text()
            await check(backend, get_doc, get_chunk)
        finally:
            await backend.close()

@pytest.mark.parametrize('piece,payload', [
    ('🙂', '🙂-2'.encode()), ('漢🙂', '漢🙂-3'.encode()),
    ('\ud800', '�-1'.encode()), ('\ud83d\ude42', '🙂-2'.encode()),
])
def test_chunk_hash_uses_js_utf16_length_and_text_encoder(piece, payload):
    import xxhash
    from content_splitter import chunk_id_for_piece
    assert int(chunk_id_for_piece(piece)[2:], 36) == xxhash.xxh64_intdigest(payload)


def test_raw_binary_piece_is_hashed_after_base64_encoding():
    import xxhash
    from content_splitter import chunk_id_for_piece
    assert int(chunk_id_for_piece(b'hello')[2:], 36) == xxhash.xxh64_intdigest(b'aGVsbG8=-8')


@pytest.mark.parametrize('size,first', [(128000, 1024), (128001, 1536),
                                          (4194303, 33792), (4194304, 102400)])
def test_adaptive_text_thresholds_and_exact_four_mib_switch(size, first):
    from content_splitter import ContentSplitter
    pieces = ContentSplitter().split('\0' * size)
    assert len(pieces[0].encode()) == first
    assert sum(len(p) for p in pieces) == size


def test_binary_path_hint_changes_thresholds_but_preserves_base64():
    from content_splitter import ContentSplitter
    pieces = ContentSplitter().split(bytes(3000), path='binary.md')
    assert [len(base64.b64decode(p, validate=True)) for p in pieces] == [1024, 1024, 952]


@pytest.mark.parametrize('backend_kind', ['couchdb', 'journal'])
async def test_writers_skip_empty_decoded_text_pieces(tmp_path, backend_kind):
    from tests.test_storage_couchdb import fake_vault, stored_doc
    from tests.test_storage_journal import make_backend
    # TextDecoder consumes the BOM; EntryManager skips the resulting empty piece.
    if backend_kind == 'couchdb':
        async with fake_vault() as (backend, raw, _):
            await backend.write_note('bom.md', '\ufeff', create=True)
            assert (await stored_doc(raw, 'bom.md'))['children'] == []
    else:
        backend, _ = make_backend(tmp_path)
        try:
            await backend.startup_check()
            await backend.write_note('bom.md', '\ufeff', create=True)
            assert backend._store.winner('bom.md')['children'] == []
        finally:
            await backend.close()


@pytest.mark.parametrize('setting', [{'hashAlg': 'sha1'}, {'chunkSplitterVersion':'v2'},
                                     {'idDerivationVersion':1, 'idDerivationKey':'invalid'}])
@pytest.mark.parametrize('backend_kind', ['couchdb', 'journal'])
async def test_startup_records_unsupported_chunk_writer(tmp_path, setting, backend_kind):
    from tests.test_storage_couchdb import fake_vault
    from tests.test_storage_journal import make_backend, MILESTONE_KEY
    milestone = {'tweak_values': {'PREFERRED': {'pathNormalizationVersion':0, **setting}}}
    if backend_kind == 'couchdb':
        async with fake_vault({'_local/obsydian_livesync_milestone':milestone}) as (backend, _, log):
            await backend.startup_check()
            assert (await backend.backend_info())['writer_supported'] is False
            assert all(r.method == 'GET' for r in log)
    else:
        backend, storage = make_backend(tmp_path, {MILESTONE_KEY:json.dumps(milestone).encode()})
        try:
            await backend.startup_check()
            assert (await backend.backend_info())['writer_supported'] is False
            assert storage.put_calls == []
        finally:
            await backend.close()

async def test_journal_refuses_colliding_pieces_before_publication(tmp_path, monkeypatch):
    import storage_journal
    from storage_base import StorageError
    from tests.test_storage_journal import make_backend
    backend, storage = make_backend(tmp_path)
    try:
        await backend.startup_check()
        monkeypatch.setattr(storage_journal, 'chunk_id_for_piece', lambda piece: 'h:collision')
        with pytest.raises(StorageError, match='different data'):
            await backend.write_note('collision.md', '\0' * 1024 + '\x01' * 1024, create=True)
        assert not storage.put_calls
        assert backend._store.winner('collision.md') is None
    finally:
        await backend.close()


@pytest.mark.parametrize('vector', FIXTURE['additional_hash_vectors'])
def test_independent_node_hash_vectors(vector):
    from content_splitter import chunk_id_for_piece
    assert chunk_id_for_piece(vector['piece']) == vector['expected']


def test_derivation_only_device_settings_are_not_silently_skipped():
    from livesync_format import resolve_content_splitter, UnsupportedVaultSettings
    with pytest.raises(UnsupportedVaultSettings, match='idDerivationKey'):
        resolve_content_splitter({'tweak_values': {
            'a': {'hashAlg':'xxhash64'}, 'b': {'idDerivationVersion':1}}})


def test_valid_derivation_only_preferred_is_readable():
    from livesync_format import resolve_content_splitter
    splitter = resolve_content_splitter({'tweak_values': {'PREFERRED': {
        'idDerivationVersion':1, 'idDerivationKey':'ab'*32}}})
    assert splitter.chunk_id_for_piece('helloWorld') == 'h:37me21jj44pcc'
