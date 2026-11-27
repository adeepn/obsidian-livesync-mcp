"""P-10: metadata describes the ingested Blob, independently of pieces."""
import json
import logging
from pathlib import Path

import pytest

from content_splitter import ContentSplitter
from storage_journal import JournalBackend
from tests.test_upload_attachment import backend

ORACLE = json.loads((Path(__file__).parent / 'fixtures/ingested_blob_size_upstream.json').read_text())

VERSIONS = ['v1', 'v2', 'v2-segmenter', 'v3-rabin-karp']


@pytest.mark.parametrize('version', VERSIONS)
@pytest.mark.parametrize('text,size', [('abc', 3), ('\ufeffabc', 6), ('Привет', 12), ('A🙂B', 6)])
async def test_metadata_is_presplit_blob_size(backend, version, text, size, caplog):
    # Executed pinned host-read/createBlob/putDBEntryInternal oracle:
    # scripts/probe_ingested_blob_size.mjs; commonlib 484c164.
    backend._splitter = ContentSplitter({'chunkSplitterVersion': version})
    oracle = next(v for v in ORACLE['vectors'] if v['version'] == version and v['input'] == text)
    assert size == oracle['size']
    expected = oracle['text']
    assert oracle['reflected'] == ('size-mismatch' if expected != text else 'accepted')
    with caplog.at_level(logging.WARNING):
        meta = await backend.write_note('note.md', text, create=True)
        assert meta['size'] == size
        note = await backend.read_note('note.md')
        assert note['text'] == expected
        assert note['meta']['size'] == size
        assert (await backend.read_text_note('note.md'))['text'] == expected
        assert [(m['size'], t) async for m, t in backend.iterate_texts()] == [(size, expected)]
    assert not caplog.records


@pytest.mark.parametrize('version', VERSIONS)
@pytest.mark.parametrize('path,body,size', [('note.md', '\ufeffabc', 6),
                                         ('note.base', b'\x00\xff\xef\xbb\xbf', 5)])
async def test_update_copy_move_preserve_blob_size(backend, version, path, body, size):
    backend._splitter = ContentSplitter({'chunkSplitterVersion': version})
    await backend.write_note(path, 'old', create=True)
    assert (await backend.write_note(path, body))['size'] == size
    copied = 'copy/' + path
    moved = 'moved/' + path
    assert (await backend.copy_note(path, copied))['size'] == size
    assert (await backend.rename_note(copied, moved))['size'] == size
    assert (await backend.read_note(moved))['meta']['size'] == size
    assert await backend.read_note(copied) is None


@pytest.mark.parametrize('version', VERSIONS[:3])
async def test_legacy_bom_two_peer_replay(backend, tmp_path, version, caplog):
    backend._splitter = ContentSplitter({'chunkSplitterVersion': version})
    await backend.write_note('bom.md', '\ufeffabc', create=True)
    if isinstance(backend, JournalBackend):
        peer = JournalBackend(backend._object_storage, tmp_path / 'reader')
        await peer.startup_check()
        await peer.refresh(force=True)
    else:
        from storage_couchdb import CouchDBBackend
        from tests.test_storage_couchdb import URL, USER, PASSWORD, DATABASE
        peer = CouchDBBackend(URL, USER, PASSWORD, DATABASE, transport=backend._client._transport)
    try:
        caplog.clear()  # Startup reset logging is unrelated to content validation.
        with caplog.at_level(logging.WARNING):
            note = await peer.read_note('bom.md')
            assert note['meta']['size'] == 6
            assert note['text'] == 'abc'  # Actual device local DB read, not file reflection.
        assert not caplog.records
    finally:
        await peer.close()
