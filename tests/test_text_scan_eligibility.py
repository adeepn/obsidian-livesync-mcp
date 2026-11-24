"""Regression coverage for bounded, extension-independent text scans."""
import base64
import json
import pytest
import server
from tests.test_upload_attachment import backend

@pytest.mark.parametrize('suffix', ['.base', '.json', '.yaml'])
async def test_all_textual_newnotes_reach_tools(backend, monkeypatch, suffix):
    monkeypatch.setattr(server, 'backend', backend)
    path = 'source' + suffix
    await backend.write_note(path, 'needle #tag [[target]]', create=True)
    await backend.write_note('target.md', 'target', create=True)
    result = json.loads(await server.obsidian_search('needle'))
    assert [r['path'] for r in result['results']] == [path]
    assert result['skipped'] == []
    tags = json.loads(await server.obsidian_get_all_tags())
    assert 'tag' in str(tags)
    graph = await server._graph()
    assert {'from': path, 'to': 'target.md'} in graph['edges']
    assert graph['skipped'] == []
    assert path not in await server.obsidian_find_orphaned_notes()
    hubs = json.loads(await server.obsidian_find_hub_notes(min_links=1))
    assert path in str(hubs)
    assert json.loads(await server.obsidian_vault_stats())['skipped'] == []

async def test_binary_prefix_reads_one_piece(backend, monkeypatch):
    await backend.write_note('image.png', b'\x89PNG\r\n\x1a\n' + b'x' * 400000, create=True)
    if hasattr(backend, '_store'):
        owner, method = backend._store, '_chunk_datas'
    else:
        owner, method = backend, '_fetch_chunk_datas'
    original = getattr(owner, method)
    reads = []
    if method == '_chunk_datas':
        def counted(children, *args, **kwargs):
            reads.extend(children)
            return original(children, *args, **kwargs)
    else:
        async def counted(children, *args, **kwargs):
            reads.extend(children)
            return await original(children, *args, **kwargs)
    monkeypatch.setattr(owner, method, counted)
    entries = [entry async for entry in backend.iterate_texts(extension='')]
    assert entries[0][1] is None
    assert len(reads) == 1
    entries = [entry async for entry in backend.iterate_texts(extension='')]
    assert len(reads) == 1  # revision verdict cached

@pytest.mark.parametrize('body', [b'%PDF-1.7\nASCII prefix\n\x00binary', b'a\x00b', b'a\x01b', b'A\xffB'])
async def test_nontext_is_skipped_and_counted(backend, monkeypatch, body):
    monkeypatch.setattr(server, 'backend', backend)
    await backend.write_note('skip.base', body, create=True)
    result = json.loads(await server.obsidian_search('binary'))
    assert result['skipped'] == ['skip.base']
    assert json.loads(await server.obsidian_vault_stats())['total_notes'] >= 1

async def test_budget_skip_revision_recheck(backend, monkeypatch):
    monkeypatch.setattr(server, 'backend', backend)
    backend.text_scan_max_bytes = 4
    await backend.write_note('large.base', 'needle', create=True)
    result = json.loads(await server.obsidian_search('needle'))
    assert result['skipped'] == ['large.base']
    await backend.write_note('large.base', 'yes')
    result = json.loads(await server.obsidian_search('yes'))
    assert result['skipped'] == []
    assert len(result['results']) == 1

async def test_device_shaped_pieces(backend, monkeypatch):
    # Inject document/chunks directly, independently of this peer's writer.
    from livesync_format import chunk_id_for
    monkeypatch.setattr(server, 'backend', backend)
    for suffix in ['.base', '.json', '.yaml']:
        path = 'device' + suffix
        pieces = [base64.b64encode(b'needle #device [[target]]').decode()]
        children = [chunk_id_for(piece) for piece in pieces]
        doc = {'_id': path, '_rev': '1-device', 'path': path, 'type': 'newnote',
               'ctime': 1, 'mtime': 1, 'size': 25, 'children': children}
        chunks = [{'_id': child, 'type': 'leaf', 'data': piece}
                  for child, piece in zip(children, pieces)]
        if hasattr(backend, '_store'):
            backend._store.apply_journal(path, dict(zip(children, pieces)), [doc])
        else:
            for chunk in chunks:
                if await backend._get_doc(chunk['_id']) is None:
                    await backend._put_doc(chunk['_id'], chunk)
            await backend._put_doc(path, {k: v for k, v in doc.items() if k != '_rev'})
        assert any([meta['path'] == path and text == 'needle #device [[target]]'
                    async for meta, text in backend.iterate_texts()])
