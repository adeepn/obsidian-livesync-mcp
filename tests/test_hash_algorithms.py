"""Pinned Node manager differential evidence; no Node needed at test time."""
import json
from pathlib import Path

import pytest

from content_splitter import ContentSplitter, chunk_id_for_piece

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/hash_algorithms_upstream.json').read_text())
ALGORITHMS = ['xxhash64', 'xxhash32', 'mixed-purejs', 'sha1', '']


def piece_for(recipe):
    return recipe['text'] if 'text' in recipe else recipe['repeat'] * recipe['count'] + recipe.get('tail', '')


@pytest.mark.parametrize('alg,expected', list(zip(ALGORITHMS, [
    '37me21jj44pcc', '1n069eb', '1b68nom123qfyd', '2pGPxorWFQvCZq2k1uh76tH9ABc=', '-vyucau'])))
def test_mandatory_hashes(alg, expected):
    assert chunk_id_for_piece('helloWorld', hash_alg=alg) == 'h:' + expected


@pytest.mark.parametrize('alg', ALGORITHMS)
@pytest.mark.parametrize('vector', FIXTURE['vectors'], ids=lambda v: str(FIXTURE['vectors'].index(v)))
def test_node_manager_corpus(vector, alg):
    piece = piece_for(vector['recipe'])
    assert len(piece.encode('utf-16-le', errors='surrogatepass')) // 2 == vector['utf16_length']
    assert chunk_id_for_piece(piece, hash_alg=alg) == vector['ids'][alg]


def test_manager_selection_missing_and_null():
    assert ContentSplitter().chunk_id_for_piece('helloWorld') == 'h:37me21jj44pcc'
    assert ContentSplitter({'hashAlg': None}).chunk_id_for_piece('helloWorld') == 'h:1n069eb'
    assert chunk_id_for_piece('helloWorld', hash_alg=None) == 'h:1n069eb'


@pytest.mark.parametrize('alg', ALGORITHMS)
@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
async def test_backend_hash_setting_roundtrip(tmp_path, alg, kind):
    from tests.test_storage_couchdb import fake_vault, stored_doc
    from tests.test_storage_journal import make_backend, MILESTONE_KEY, SYNC_PARAMETERS_KEY, params_blob
    from storage_journal import JournalBackend
    milestone = {'tweak_values': {'PREFERRED': {'pathNormalizationVersion': 0, 'hashAlg': alg}}}
    content = 'Привет🙂-123\n' * 300
    expected = [chunk_id_for_piece(p, hash_alg=alg) for p in ContentSplitter().split(content, path='hash.md')]
    if kind == 'couchdb':
        async with fake_vault({'_local/obsydian_livesync_milestone': milestone}) as (backend, raw, _):
            await backend.startup_check()
            assert (await backend.backend_info())['writer_supported'] is True
            await backend.write_note('hash.md', content, create=True)
            assert (await stored_doc(raw, 'hash.md'))['children'] == expected
            assert (await backend.read_note('hash.md'))['text'] == content
    else:
        backend, storage = make_backend(tmp_path, {MILESTONE_KEY: json.dumps(milestone).encode(),
                                                  SYNC_PARAMETERS_KEY: params_blob()})
        peer = JournalBackend(storage, tmp_path / 'peer-state')
        try:
            await backend.startup_check()
            assert (await backend.backend_info())['writer_supported'] is True
            await backend.write_note('hash.md', content, create=True)
            assert backend._store.winner('hash.md')['children'] == expected
            assert (await backend.read_note('hash.md'))['text'] == content
            await peer.startup_check()
            await peer.refresh()
            assert (await peer.read_note('hash.md'))['text'].encode() == content.encode()
            assert peer._store.winner('hash.md')['children'] == expected
        finally:
            await backend.close()
            await peer.close()


def test_conflicting_device_hash_settings_refused():
    from livesync_format import resolve_content_splitter, UnsupportedVaultSettings
    with pytest.raises(UnsupportedVaultSettings, match='conflicting'):
        resolve_content_splitter({'tweak_values': {'a': {'hashAlg': 'sha1'}, 'b': {'hashAlg': ''}}})


@pytest.mark.parametrize('piece,mixed,sha', [
    ('a' * 124, 'r3d7yi8vxya2', 'rdEOFJwBvOvbrpweK4cY7Doj6BI='),
    ('a' * 125, 'eb1hpf14nn9mj', 'm3dXEx+wjShK9nzOaiIJ7iOKbWs='),
    ('a' * 126, 'dr6jvb1sk9thz', 'wHSwXCqXXV3K1z1IJi6vUB6eJlg='),
    ('', 'bohyh61eitvns', 'D+/SfK1JFZRgSfA1K+3A+lnWAeI='),
    ('🙂🚀', '1ocgsqs1wd6fae', 'HSzm4lL8kDmfThR0lSvUFl7ejx0='),
    ('\ud800A', '1ee3kjwv9e20n', 'JojwFL3B4vEFtJej5Fpn5ygAzQM='),
])
def test_fixed_mixed_and_sha_cases(piece, mixed, sha):
    assert chunk_id_for_piece(piece, hash_alg='mixed-purejs') == 'h:' + mixed
    assert chunk_id_for_piece(piece, hash_alg='sha1') == 'h:' + sha


@pytest.mark.parametrize('length', [127, 128, 129, 130])
def test_sha_write_string_branch_with_invalid_surrogate(length):
    import hashlib
    import base64
    # Hash input includes '-L': these are encoder input lengths, not piece lengths.
    piece = 'a' * (length - 5) + '\ud800'
    suffix = '-' + str(length - 4)
    assert len(piece) + len(suffix) == length
    # Short manual path consumes the '-' as the second surrogate unit.
    encoded = (b'a' * (length - 5) +
               (bytes.fromhex('f08290ad') + suffix[1:].encode() if length <= 128
                else b'\xef\xbf\xbd' + suffix.encode()))
    expected = base64.b64encode(hashlib.sha1(encoded).digest()).decode()
    assert chunk_id_for_piece(piece, hash_alg='sha1') == 'h:' + expected


def test_sha_cache_keeps_id_and_escapes_slash(tmp_path):
    from journal_store import JournalStore
    from tests.test_journal_store import meta_doc
    # Valid SHA-1 base64 with slash in the shard and plus in the filename.
    chunk = 'h:/+abcdefghijklmnopqrstuvwxy='
    assert len(chunk[2:]) == 28
    store = JournalStore(tmp_path / 'sha-cache', 'legacy')
    try:
        store.apply_journal('1-test', {chunk: 'payload'}, [meta_doc('sha.md', '1-a', children=[chunk])])
        assert store.read_note('sha.md')[1] == 'payload'
        assert store._chunk_path(chunk).is_relative_to(store._chunks_dir)
        assert store._chunk_path(chunk).parent.name == '_+'
        assert store.winner('sha.md')['children'] == [chunk]
    finally:
        store.close()
