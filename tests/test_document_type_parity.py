"""Device disk-ingestion typing, separate from UTF-8 tool representation.

Pinned commonlib 484c164: path.ts:164-174; FileAccessBase.unit.spec.ts:
687-707,746-769 (auto.txt string, auto.bin ArrayBuffer). EntryManagerImpls
unit.spec.ts:1658-1691 tests programmatic text Blobs, not disk ingestion.
"""
import base64
import hashlib
import json

import pytest

import server
from storage_journal import JournalBackend
from tests.test_upload_attachment import backend
from tests.test_storage_couchdb import fake_vault, stored_doc
from tests.test_storage_journal import make_backend, healthy_bucket

# Literal device isPlainText list: intentionally independent of production constants.
PLAIN = ['.md', '.txt', '.svg', '.html', '.csv', '.css', '.js', '.xml', '.canvas']
BINARY = ['.MD', '.Json', '.json', '.base', '.yaml', '.yml', '.toml', '.png', '']
TEXT = 'Привет\nviews: []\r\n'


@pytest.mark.parametrize('suffix,doc_type',
                         [(e, 'plain') for e in PLAIN] +
                         [(e.upper(), 'newnote') for e in PLAIN] +
                         [(e, 'newnote') for e in BINARY])
@pytest.mark.parametrize('as_bytes', [False, True])
async def test_disk_type_and_roundtrip(backend, suffix, doc_type, as_bytes):
    path = 'file' + suffix
    content = TEXT.encode() if as_bytes else TEXT
    meta = await backend.write_note(path, content, create=True)
    assert meta['type'] == doc_type
    assert meta['size'] == len(TEXT.encode())
    note = await backend.read_note(path)
    assert note['is_text'] is (doc_type == 'plain')
    if doc_type == 'plain':
        assert note['text'] == TEXT
    else:
        assert note['text'] is None
        assert note['data'] == TEXT.encode()


async def test_plain_host_decode_replaces_invalid_utf8(backend):
    from content_splitter import ContentSplitter
    backend._splitter = ContentSplitter({'chunkSplitterVersion': 'v3-rabin-karp'})
    meta = await backend.write_note('broken.md', b'\xef\xbb\xbfA\xffB', create=True)
    note = await backend.read_note('broken.md')
    assert meta['type'] == 'plain'
    assert note['text'] == '\ufeffA\ufffdB'
    # EntryManager stores the text Blob's byte length, after host decoding.
    assert meta['size'] == len('\ufeffA\ufffdB'.encode())


@pytest.mark.parametrize('method', ['write_note', 'write_attachment'])
async def test_bytes_alias_uses_disk_type(backend, method):
    meta = await getattr(backend, method)('auto.txt', b'Text content', create=True)
    assert meta['type'] == 'plain'
    assert (await backend.read_note('auto.txt'))['text'] == 'Text content'


async def test_couchdb_independent_base64_pieces():
    text = TEXT * 25000
    async with fake_vault() as (store, raw, _):
        await store.write_note('db.base', text, create=True)
        doc = await stored_doc(raw, 'db.base')
        assert doc['type'] == 'newnote'
        pieces = [(await stored_doc(raw, child))['data'] for child in doc['children']]
        assert len(pieces) > 1
        assert b''.join(base64.b64decode(p, validate=True) for p in pieces) == text.encode()
        assert (await store.read_note('db.base'))['data'] == text.encode()


async def test_journal_textual_base_two_peer_replay(tmp_path):
    writer, storage = make_backend(tmp_path / 'writer', healthy_bucket())
    reader = JournalBackend(storage, tmp_path / 'reader')
    text = TEXT * 25000
    try:
        await writer.startup_check()
        await writer.write_note('db.base', text, create=True)
        await reader.startup_check()
        await reader.refresh(force=True)
        note = await reader.read_note('db.base')
        assert note['meta']['type'] == 'newnote'
        assert note['is_text'] is False
        assert note['data'] == text.encode()
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.parametrize('suffix', ['.base', '.json', '.yaml', '.png', '.MD', ''])
async def test_tool_utf8_newnote_edit(backend, monkeypatch, suffix):
    monkeypatch.setattr(server, 'backend', backend)
    path = 'file' + suffix
    assert 'Created' in await server.obsidian_create_note(path, 'views: []')
    assert (await backend.get_meta(path))['type'] == 'newnote'
    assert json.loads(await server.obsidian_fetch(path))['content'] == 'views: []'
    assert 'Updated' in await server.obsidian_update_note(path, 'append', content='edited')
    assert json.loads(await server.obsidian_fetch(path))['content'] == 'views: []\nedited'
    note = await backend.read_note(path)
    assert note['meta']['type'] == 'newnote'
    assert note['data'] == b'views: []\nedited'


async def test_tool_real_binary_metadata_and_download(backend, monkeypatch):
    monkeypatch.setattr(server, 'backend', backend)
    data = b'\x89PNG\r\n\x1a\n\x00\xff'
    await backend.write_note('image.png', data, create=True)
    fetched = json.loads(await server.obsidian_fetch('image.png'))
    assert 'content' not in fetched
    assert fetched['size'] == len(data)
    assert fetched['sha256'] == hashlib.sha256(data).hexdigest()
    downloaded = json.loads(await server.obsidian_download_attachment('image.png'))
    assert base64.b64decode(downloaded['content_b64']) == data
    assert 'requires a text note' in await server.obsidian_update_note('image.png', 'append', content='edit')


async def test_tools_properties_tags_and_templates_on_utf8_newnote(backend, monkeypatch):
    monkeypatch.setattr(server, 'backend', backend)
    await backend.write_note('note.MD', 'body #inline', create=True)
    assert 'Updated properties' in await server.obsidian_manage_properties(
        'note.MD', 'set', properties='{"title":"Текст"}')
    assert json.loads(await server.obsidian_manage_properties('note.MD'))['title'] == 'Текст'
    assert 'Updated frontmatter tags' in await server.obsidian_manage_tags(
        'note.MD', 'add', tags='tag')
    assert json.loads(await server.obsidian_manage_tags('note.MD')) == {
        'frontmatter_tags': ['tag'], 'inline_tags': ['inline']}
    assert (await backend.get_meta('note.MD'))['type'] == 'newnote'
    await backend.write_note('template.base', 'hello {{name}}', create=True)
    assert 'Created' in await server.obsidian_create_from_template(
        'template.base', 'created.base', variables='{"name":"Мир"}')
    assert json.loads(await server.obsidian_fetch('created.base'))['content'] == 'hello "Мир"'
    assert (await backend.read_note('created.base'))['data'] == 'hello "Мир"'.encode()


async def test_canvas_reads_programmatic_binary_blob(monkeypatch):
    # Incoming non-text Blob can be newnote even at .canvas (spec §в).
    # The tool must decode it; subsequent disk-ingestion writes become plain.
    from tests.fake_backend import FakeBackend
    store = FakeBackend()
    store.add('board.canvas', data=b'{"nodes":[],"edges":[]}')
    monkeypatch.setattr(server, 'backend', store)
    assert 'Added' in await server.obsidian_canvas_node('board.canvas', text='hello')
    fetched = json.loads(await server.obsidian_fetch('board.canvas'))
    assert json.loads(fetched['content'])['nodes'][0]['text'] == 'hello'


async def test_writer_uses_canonical_splitter_hint(backend):
    text = 'short line\n' * 100
    await backend.write_note('canonical.md', text, create=True)
    await backend.write_note('./alias.md/', text, create=True)
    async def children(path):
        if isinstance(backend, JournalBackend):
            return backend._store.winner(path)['children']
        return (await backend._get_doc(path))['children']
    assert await children('canonical.md') == await children('alias.md')


async def test_text_scan_projects_utf8_newnotes(backend, monkeypatch):
    monkeypatch.setattr(server, 'backend', backend)
    await backend.write_note('note.MD', 'needle #tag', create=True)
    result = json.loads(await server.obsidian_search('needle'))
    assert [item['path'] for item in result['results']] == ['note.MD']
    assert result['skipped'] == []


@pytest.mark.parametrize('version', ['v1', 'v2', 'v2-segmenter', 'v3-rabin-karp'])
@pytest.mark.parametrize('hash_alg', ['xxhash64', 'xxhash32', 'mixed-purejs', 'sha1', ''])
async def test_file_ingress_across_vault_writer_modes(backend, version, hash_alg):
    from content_splitter import ContentSplitter
    backend._splitter = ContentSplitter({
        'chunkSplitterVersion': version, 'hashAlg': hash_alg})
    text = 'строка\n' * 100
    for path, content, doc_type in [('note.md', text.encode(), 'plain'),
                                    ('db.base', text, 'newnote')]:
        meta = await backend.write_note(path, content, create=True)
        assert meta['type'] == doc_type
        assert meta['size'] == len(text.encode())
        note = await backend.read_note(path)
        if doc_type == 'plain':
            assert note['text'] == text
        else:
            assert note['data'] == text.encode()


async def test_scan_utf8_projection_isolates_unexpected_store_failure(tmp_path, monkeypatch):
    store, _ = make_backend(tmp_path, healthy_bucket())
    try:
        await store.startup_check()
        await store.write_note('bad.MD', 'needle', create=True)
        await store.write_note('good.md', 'needle', create=True)
        original = store._store.read_note
        def read(path):
            if path.lower() == 'bad.md':
                raise OSError('unreadable chunk')
            return original(path)
        monkeypatch.setattr(store._store, 'read_note', read)
        monkeypatch.setattr(server, 'backend', store)
        result = json.loads(await server.obsidian_search('needle'))
        assert [item['path'] for item in result['results']] == ['good.md']
        assert result['skipped'] == ['bad.MD']
    finally:
        await store.close()


@pytest.mark.parametrize('path', ['note.md', 'db.base'])
@pytest.mark.parametrize('text,expected', [('A\ud800B', 'A\ufffdB'),
                                          ('A\udc00B', 'A\ufffdB'),
                                          ('\ud83d\ude42', '🙂')])
async def test_string_input_uses_blob_unicode_scalar_values(backend, path, text, expected):
    # Pinned createBlob/toBlob creates a Web Blob: strings are USVString.
    # This does not affect typing, which still depends only on the path.
    meta = await backend.write_note(path, text, create=True)
    assert meta['size'] == len(expected.encode())
    note = await backend.read_note(path)
    assert note['text'] == (expected if path.endswith('.md') else None)
    if path.endswith('.base'):
        assert note['data'] == expected.encode()
