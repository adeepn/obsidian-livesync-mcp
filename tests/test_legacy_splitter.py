"""Differential oracle: pinned commonlib, direct and real worker handler under Node."""
import base64
import hashlib
import json
from pathlib import Path
import zlib

import pytest

from content_splitter import ContentSplitter

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/legacy_splitter_upstream.json').read_text())


@pytest.mark.parametrize('v', FIXTURE['vectors'], ids=lambda v: v['version'] + '-' + v['name'])
def test_pinned_legacy_differential(v):
    raw = zlib.decompress(base64.b64decode(v['input_deflate_b64']))
    splitter = ContentSplitter({'chunkSplitterVersion': v['version'], 'customChunkSize': v['custom'],
                                'minimumChunkSize': v['minimum']})
    pieces = splitter.split(raw.decode('utf-8', errors='replace') if v['text'] else raw, path=v['path'])
    assert [len(p.encode('utf-16-le', errors='surrogatepass')) // 2 if v['text'] else
            len(base64.b64decode(p)) for p in pieces] == v['expected_lengths']
    assert [splitter.chunk_id_for_piece(p) for p in pieces] == v['expected_ids']
    assert [hashlib.sha256(p.encode('utf-16-le', errors='surrogatepass')).hexdigest()
            for p in pieces] == v['expected_piece_sha256']


@pytest.mark.parametrize('version', ['v1', '', None])
def test_legacy_dispatch_missing_and_empty(version):
    settings = {} if version is None else {'chunkSplitterVersion': version}
    assert ContentSplitter(settings).split('#\na\nb', path='a.md') == ['#\n', 'a\nb']


@pytest.mark.parametrize('version', ['v1', 'v2', 'v2-segmenter', 'v3-rabin-karp', ''])
@pytest.mark.parametrize('hash_alg', ['xxhash64', 'xxhash32', 'mixed-purejs', 'sha1', ''])
@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
async def test_all_ui_writer_settings_supported(tmp_path, version, hash_alg, kind):
    from tests.test_writer_support import vault
    async with vault(tmp_path, kind, {'chunkSplitterVersion': version, 'hashAlg': hash_alg}) as (backend, _, _):
        await backend.startup_check()
        assert (await backend.backend_info())['writer_supported'] is True
        await backend.write_note('new.md', '#\n🙂\r\n' * 50, create=True)
        assert (await backend.read_note('new.md'))['text'] == '#\n🙂\r\n' * 50


@pytest.mark.parametrize('version', ['v1', 'v2', 'v2-segmenter'])
@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
async def test_legacy_writes_children_size_and_replay(tmp_path, version, kind):
    from tests.test_writer_support import vault
    from storage_journal import JournalBackend
    text = '#\n```\na;b,c:d\n```\n🙂\r\n' * 500
    binary = (bytes(range(256)) * 1200) + b'\xff\xfe\x80'
    settings = {'chunkSplitterVersion': version, 'minimumChunkSize': 20}
    splitter = ContentSplitter(settings)
    async with vault(tmp_path, kind, settings) as (backend, _, _):
        await backend.startup_check()
        for path, data in [('new.md', text), ('new.pdf', binary)]:
            if isinstance(data, str):
                await backend.write_note(path, data, create=True)
            else:
                await backend.write_attachment(path, data, create=True)
            if kind == 'journal':
                doc = backend._store.winner(path)
            else:
                doc = await backend._get_doc(path)
            pieces = [p for p in splitter.split(data, path=path) if p]
            assert doc['children'] == [splitter.chunk_id_for_piece(p) for p in pieces]
            assert len(set(doc['children'])) < len(doc['children']) or path.endswith('.pdf')
            assert doc['size'] == len(data.encode() if isinstance(data, str) else data)
        assert (await backend.read_note('new.md'))['text'] == text
        assert (await backend.read_note('new.pdf'))['data'] == binary
        # Direct/worker empty differences vanish at EntryManagerImpls.ts:142.
        await backend.write_note('new.md', '')
        await backend.write_attachment('new.pdf', b'')
        for path in ['new.md', 'new.pdf']:
            doc = backend._store.winner(path) if kind == 'journal' else await backend._get_doc(path)
            assert doc['children'] == []
        if kind == 'journal':
            await backend.write_note('new.md', text)
            await backend.write_attachment('new.pdf', binary)
            peer = JournalBackend(backend._object_storage, tmp_path / 'peer')
            try:
                await peer.startup_check()
                await peer.refresh(force=True)
                assert (await peer.read_note('new.md'))['text'] == text
                assert (await peer.read_note('new.pdf'))['data'] == binary
                assert peer._store.winner('new.pdf')['children'] == backend._store.winner('new.pdf')['children']
            finally:
                await peer.close()


def test_resolver_rejects_conflicting_versions():
    from livesync_format import resolve_content_splitter, UnsupportedVaultSettings
    with pytest.raises(UnsupportedVaultSettings, match='conflicting chunk splitter'):
        resolve_content_splitter({'tweak_values': {'a': {'chunkSplitterVersion': 'v1'},
                                                  'b': {'chunkSplitterVersion': 'v2'}}})


@pytest.mark.parametrize('version', ['v1', 'v2', 'v2-segmenter'])
def test_long_line_setting_is_unused(version):
    text = '#\n```\na;b,c:d\n```\n' * 30
    settings = {'chunkSplitterVersion': version}
    assert ContentSplitter({**settings, 'longLineThreshold': 1}).split(text, path='x.md') == ContentSplitter({**settings, 'longLineThreshold': 99999}).split(text, path='x.md')


def test_v1_heading_and_literal_lf_rules():
    splitter = ContentSplitter({'chunkSplitterVersion': 'v1', 'minimumChunkSize': 100})
    assert splitter.split('a\r\n#\nb\r\nc', path='x.md') == ['a\r\n', '#\n', 'b\r\nc']
    assert splitter.split('a\r\n#\nb\r\nc', path='x.MD') == ['a\r\n#\nb\r\nc']


@pytest.mark.parametrize('indent', ['', ' ', '  ', '   '])
def test_v1_fence_capturing_regex_and_unusual_loop(indent):
    splitter = ContentSplitter({'chunkSplitterVersion': 'v1'})
    assert splitter.split(indent + '```\na;b,c:d<e\n```\nz', path='x.md') == ['', indent + '```\n', 'a;', 'b,', 'c:', 'd<', 'e\n```\n', 'z']
    # Opening fence consumes an immediately following fence before looking ahead.
    assert splitter.split('```\n```\nx;y', path='x.md') == ['', '```\n```\n', 'x;', 'y']


def test_v1_base64_and_large_fences_skip_regex():
    splitter = ContentSplitter({'chunkSplitterVersion': 'v1'})
    for text in ['```\na;b=', '```\n' + 'a;' * 1100 + '\n```']:
        assert splitter.split(text, path='x.md') == ['', text]


def test_v2_strict_newline_threshold_and_adaptive_minimum():
    splitter = ContentSplitter({'chunkSplitterVersion': 'v2', 'minimumChunkSize': 2})
    assert splitter.split('a\nb\nc', path='x.md') == ['a\nb\n', 'c']
    assert splitter.split('a\n' * 101, path='x.md')[:2] == ['a\n' * 3] * 2
    assert splitter.split('a\nb\nc', path='x.MD') == ['a\nb\nc']


@pytest.mark.parametrize('extension,delimiter', [('pdf', b'/'), ('json', b','), ('bin', b'\0')])
def test_binary_before_delimiter_and_distant_preference(extension, delimiter):
    raw = bytearray(b'\xff' * 200000)
    raw[11000] = 10
    raw[150000] = delimiter[0]
    v1 = ContentSplitter({'chunkSplitterVersion': 'v1'})
    v2 = ContentSplitter({'chunkSplitterVersion': 'v2'})
    # Distant preferred suppresses earlier LF in V2; V1 searches only slice.
    for splitter in [v1, v2]:
        pieces = splitter.split(bytes(raw), path='x.' + extension)
        assert len(base64.b64decode(pieces[0])) == 102400
        assert base64.b64decode(pieces[1])[-1:] != delimiter
        assert base64.b64decode(pieces[2]).startswith(delimiter)
    raw[150000] = 255
    assert len(base64.b64decode(v1.split(bytes(raw), path='x.' + extension)[0])) == 102400
    assert len(base64.b64decode(v2.split(bytes(raw), path='x.' + extension)[0])) == 11000


@pytest.mark.parametrize('version,empty', [('v1', ['']), ('v2', []), ('v2-segmenter', [])])
def test_empty_binary_and_worker_sentinel_are_not_stored(version, empty):
    assert ContentSplitter({'chunkSplitterVersion': version}).split(b'') == empty
    vectors = [v for v in FIXTURE['vectors'] if v['version'] == version and v['name'] == 'binary-bin-0-c0']
    assert vectors[0]['worker_empty_sentinel'] == (empty == [])


def test_resolver_accepts_identical_v2_aliases():
    from livesync_format import resolve_content_splitter
    splitter = resolve_content_splitter({'tweak_values': {
        'a': {'chunkSplitterVersion': 'v2'}, 'b': {'chunkSplitterVersion': 'v2-segmenter'}}})
    assert splitter.split('a\nb', path='x.md') == ['a\nb']


def test_v25_equals_v2_on_entire_oracle_corpus():
    v2 = {v['name']: v for v in FIXTURE['vectors'] if v['version'] == 'v2'}
    for alias in (v for v in FIXTURE['vectors'] if v['version'] == 'v2-segmenter'):
        for key in ['expected_lengths', 'expected_ids', 'expected_piece_sha256', 'worker_empty_sentinel']:
            assert alias[key] == v2[alias['name']][key]


def test_v1_fence_regex_uses_javascript_line_terminators():
    splitter = ContentSplitter({'chunkSplitterVersion': 'v1'})
    assert splitter.split('```\na\rb;c\u2028d,e\u2029f:<\n```', path='x.md') == [
        '', '```\na\r', 'b;', 'c\u2028', 'd,', 'e\u2029', 'f:', '<', '\n```']


def test_v2_fractional_minimum_preserves_repeated_addition_rounding():
    splitter = ContentSplitter({'chunkSplitterVersion': 'v2', 'minimumChunkSize': .2})
    assert splitter.split('\n' * 199, path='x.md') == ['\n\n'] * 99 + ['\n']
