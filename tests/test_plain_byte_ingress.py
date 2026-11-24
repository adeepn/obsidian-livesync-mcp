"""Keep disk byte ingress distinct from wheels Blob-to-string decoding."""
import base64
import hashlib
import json
import pytest
import server
from livesync_format import prepare_file_content
from tests.test_upload_attachment import backend

INVALID = [b'A\xffB', b'A\xe2\x82', b'A\xc0\xafB', b'A\x80B', b'A\xed\xa0\x80B']

@pytest.mark.parametrize('raw', INVALID)
async def test_plain_invalid_bytes_refused_before_mutation(backend, raw):
    before = await backend.list_notes()
    with pytest.raises(ValueError, match='invalid UTF-8'):
        await backend.write_note('loss.md', raw, create=True)
    assert await backend.list_notes() == before
    assert await backend.read_note('loss.md') is None

@pytest.mark.parametrize('raw', INVALID)
async def test_upload_lossy_opt_in_reports_stored_bytes(backend, monkeypatch, raw):
    monkeypatch.setattr(server, 'backend', backend)
    content_b64 = base64.b64encode(raw).decode()
    refused = await server.obsidian_upload_attachment('loss.md', content_b64)
    assert 'invalid UTF-8' in refused
    assert await backend.read_note('loss.md') is None
    response = json.loads(await server.obsidian_upload_attachment(
        'loss.md', content_b64, lossy_text=True))
    stored = raw.decode('utf-8', errors='replace').encode()
    assert response['size'] == len(stored)
    assert response['sha256'] == hashlib.sha256(stored).hexdigest()
    assert response['replaced_bytes'] == len(raw) - len(raw.decode('utf-8', errors='ignore').encode())
    assert (await backend.get_meta('loss.md'))['size'] == len(stored)
    assert (await backend.read_note('loss.md'))['text'].encode() == stored

@pytest.mark.parametrize('raw', INVALID)
async def test_newnote_upload_never_changes_bytes(backend, monkeypatch, raw):
    monkeypatch.setattr(server, 'backend', backend)
    response = json.loads(await server.obsidian_upload_attachment(
        'loss.base', base64.b64encode(raw).decode(), lossy_text=True))
    assert (await backend.read_note('loss.base'))['data'] == raw
    assert response['size'] == len(raw)
    assert response['sha256'] == hashlib.sha256(raw).hexdigest()
    assert response.get('replaced_bytes', 0) == 0

@pytest.mark.parametrize('hex_value,units', [
    ('41ff42', [65,66]), ('41e282', [65,8320]),
    ('41c0af42', [65,47,66]), ('418042', [65,66]),
    ('41eda08042', [65,55296,66]),
])
def test_pinned_wheels_wheels_read_string_contract(hex_value, units):
    # Real octagonal-wheels@0.1.57 execution, SHA256 checked; artifact:
    # task-4-fix1-decoder-oracle.json. No host reader semantics inferred.
    from content_splitter import _wheels_read_string
    result = _wheels_read_string(bytes.fromhex(hex_value))
    encoded = result.encode('utf-16-le', errors='surrogatepass')
    assert [int.from_bytes(encoded[i:i+2], 'little') for i in range(0,len(encoded),2)] == units

@pytest.mark.parametrize('version', ['v1', 'v2', 'v2-segmenter', 'v3-rabin-karp'])
async def test_plain_metadata_counts_ingested_blob_bytes(backend, monkeypatch, version):
    from content_splitter import ContentSplitter
    backend._splitter = ContentSplitter({'chunkSplitterVersion': version})
    monkeypatch.setattr(server, 'backend', backend)
    response = json.loads(await server.obsidian_upload_attachment(
        'bom.md', base64.b64encode(b'\xef\xbb\xbfA').decode()))
    note = await backend.read_note('bom.md')
    stored = note['text'].encode()
    assert note['meta']['size'] == len(b'\xef\xbb\xbfA')
    assert response['size'] == len(stored)
    assert response['sha256'] == hashlib.sha256(stored).hexdigest()
