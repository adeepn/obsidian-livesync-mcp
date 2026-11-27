"""Native chunks.ts:224-252,297-313,617-633 decoder and F13 boundary vectors."""
import base64
import json
from pathlib import Path
import zlib

import pytest
from content_splitter import ContentSplitter


def test_v3_bom_only_is_a_piece():
    assert ContentSplitter({'chunkSplitterVersion': 'v3-rabin-karp'}).split('\ufeff') == ['\ufeff']


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
@pytest.mark.parametrize('text', ['\ufeff', '\ufeff# heading\nhello\0world\ufeff'])
async def test_v3_bom_note_roundtrips_both_backends_and_two_peer_replay(tmp_path, kind, text):
    from tests.test_writer_support import vault
    from storage_journal import JournalBackend
    async with vault(tmp_path, kind, {'chunkSplitterVersion': 'v3-rabin-karp'}) as (backend, _, _):
        await backend.startup_check()
        await backend.write_note('bom.md', text, create=True)
        assert (await backend.read_note('bom.md'))['text'].encode() == text.encode()
        if kind == 'journal':
            peer = JournalBackend(backend._object_storage, tmp_path / 'peer')
            try:
                await peer.startup_check()
                await peer.refresh(force=True)
                assert (await peer.read_note('bom.md'))['text'].encode() == text.encode()
            finally:
                await peer.close()


DECODING = json.loads((Path(__file__).parent / 'fixtures/splitter_decoding_upstream.json').read_text())


@pytest.mark.parametrize('vector', DECODING['vectors'], ids=lambda v: v['version'] + '-' + v['name'])
def test_real_published_decoder_differential(vector):
    raw = zlib.decompress(base64.b64decode(vector['input_deflate_b64']))
    pieces = ContentSplitter({'chunkSplitterVersion': vector['version']}).split(
        raw, text_blob=True, path=vector['path'])
    assert [p.encode('utf-16-le', errors='surrogatepass') for p in pieces] == [
        zlib.decompress(base64.b64decode(p)) for p in vector['lossless_expected_utf16_deflate_b64']]


def test_decoder_lossless_oracle_provenance():
    import hashlib
    from tests.settings_oracle import ORACLE
    assert DECODING['generator_sha256'] == hashlib.sha256(Path('scripts/extract_decoder_vectors.mjs').read_bytes()).hexdigest()
    assert DECODING['harness_sha256'] == hashlib.sha256(Path('scripts/extract_settings_oracle.mjs').read_bytes()).hexdigest()
    for field in ('decoder_dependency','wasm_dependency','source_sha256'):
        assert DECODING['provenance'][field] == ORACLE[field]
    for vector in DECODING['vectors']:
        raw = ''.join(vector['pieces']).encode('utf-16-le','surrogatepass')
        assert raw == ''.join(vector['lossless_pieces']).encode('utf-16-le','surrogatepass')
        if not any(0xD800 <= ord(c) <= 0xDFFF for p in vector['pieces'] for c in p):
            assert vector['pieces'] == vector['lossless_pieces']
            assert vector['children'] == vector['lossless_children']
