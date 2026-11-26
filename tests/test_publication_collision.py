"""EntryManagerImpls.ts:133-159; DatabaseWriteLayer.ts:24-49; journalRecords.ts:69-108."""
import json
from pathlib import Path
import pytest
from storage_base import StorageError
from storage_journal import JournalBackend
from tests.test_settings_matrix import matrix_vault

V = json.loads((Path(__file__).parent / 'fixtures/collision_upstream.json').read_text())

@pytest.mark.parametrize('kind', ['journal', 'couchdb'])
async def test_transport_collision(tmp_path, kind, monkeypatch):
    async with matrix_vault(tmp_path, kind, V['settings']) as (backend, doc, chunks, log, storage):
        await backend.startup_check()
        # Native lone-unit leaves bypass Blob projection to test wire equality.
        monkeypatch.setattr(backend._splitter, 'split', lambda *a, **kw: V['pieces'])
        before = await chunks()
        if kind == 'couchdb':
            with pytest.raises(StorageError, match='unpaired UTF-16 surrogate'):
                await backend.write_note(V['path'], V['input'], create=True)
            assert await chunks() == before
            return
        result = await backend.write_note(V['path'], V['input'], create=True)
        assert result['transport_replacements'] == 1
        stored = backend._store.winner(backend._doc_id(V['path']))
        assert stored['children'] == V['children']
        reader = JournalBackend(storage, tmp_path / 'fresh')
        try:
            await reader.startup_check()
            await reader.refresh(force=True)
            assert (await reader.read_note(V['path']))['text'] == ''.join(d['data'] for d in V['decoded'])
        finally:
            await reader.close()

async def test_different_projected_collision_refuses(tmp_path, monkeypatch):
    async with matrix_vault(tmp_path, 'journal', V['settings']) as (backend, doc, chunks, log, storage):
        await backend.startup_check()
        monkeypatch.setattr('storage_journal.chunk_id_for_piece', lambda *a, **kw: 'h:collision')
        before = await chunks()
        with pytest.raises(StorageError, match='different data'):
            await backend.write_note('gate.md', 'AB', create=True)
        assert await chunks() == before

async def test_literal_replacement_before_surrogates_still_reports(tmp_path, monkeypatch):
    # Same upstream partition/ID multiset, reversed input order; TextEncoder
    # changes the surrogate pieces regardless of which raw piece dedupes first.
    async with matrix_vault(tmp_path,'journal',V['settings']) as (backend,*_):
        await backend.startup_check()
        monkeypatch.setattr(backend._splitter, 'split', lambda *a, **kw: list(reversed(V['pieces'])))
        result=await backend.write_note('permuted.md','\ufffd\ud83d',create=True)
        assert result['transport_replacements']==1
        assert {'code':'journal_surrogates_replaced','count':1} in result['notices']
        assert (await backend.read_note('permuted.md'))['text']==''.join(d['data'] for d in V['decoded'])

@pytest.mark.parametrize('kind,settings,path,data,expected',[
    ('couchdb',V['settings'],'gate.md',V['input'],'unpaired UTF-16 surrogate'),
    ('couchdb',{'chunkSplitterVersion':'v1','customChunkSize':-2},'gate.md','PRIVATE_INPUT','customChunkSize=0'),
    ('journal',{'chunkSplitterVersion':'v1','customChunkSize':-2},'gate.md','PRIVATE_INPUT','customChunkSize=0'),
    ('couchdb',{'chunkSplitterVersion':'v2','customChunkSize':'PRIVATE_CAP'},'gate.png',b'PRIVATE_INPUT','customChunkSize=0'),
    ('journal',{'chunkSplitterVersion':'v2','customChunkSize':'PRIVATE_CAP'},'gate.png',b'PRIVATE_INPUT','customChunkSize=0'),
    ('journal',{'chunkSplitterVersion':'v2','minimumChunkSize':0},'gate.md','PRIVATE_INPUT','minimumChunkSize=20'),
])
async def test_mcp_refusal_keeps_typed_recovery(tmp_path,kind,settings,path,data,expected,monkeypatch):
    import server
    settings={'pathNormalizationVersion':0,'handleFilenameCaseSensitive':False,**settings}
    async with matrix_vault(tmp_path,kind,settings) as (backend,doc,chunks,*_):
        await backend.startup_check()
        before=await chunks()
        if data == V['input']:
            monkeypatch.setattr(backend._splitter, 'split', lambda *a, **kw: V['pieces'])
        token=server._active_backend.set(backend)
        @server._tool_errors(writer=True)
        async def publish():
            await backend.write_note(path,data,create=True)
            return 'Created.'
        try:
            result=await publish()
            assert expected in result,result
            assert 'PRIVATE' not in result
            assert await chunks()==before
        finally:server._active_backend.reset(token)


@pytest.mark.parametrize('representation', ['compact', 'json', 'encrypted', 'both'])
async def test_shared_leaf_refusal_has_typed_recovery_before_publication(tmp_path, representation):
    # EntryManager/EntryManagerImpls.ts:133-159 shares content-addressed leaves;
    # LayeredChunkManager/DatabaseWriteLayer.ts:24-49 treats duplicate IDs as
    # shared chunks. journalRecords.ts:69-75 transports compact leaf records.
    # Keep both peer pre-publication guards: existing data cannot be replaced.
    import server
    settings = {'pathNormalizationVersion': 0,
                'handleFilenameCaseSensitive': False, 'hashAlg': 'sha1'}
    async with matrix_vault(tmp_path, 'journal', settings) as (backend, doc, chunks, log, storage):
        await backend.startup_check()
        chunk_id = backend._splitter.chunk_id_for_piece('PRIVATE_NOTE')
        foreign = 'PRIVATE_REMOTE_LEAF'
        leaf = {'_id': chunk_id, '_rev': '1-foreign', 'type': 'leaf', 'data': foreign}
        if representation == 'encrypted':
            leaf['data'] = 'PRIVATE_NOTE'
            leaf['e_'] = True
        compact = {chunk_id: ('PRIVATE_NOTE' if representation == 'both' else foreign)}
        backend._store.apply_journal(
            'collision', compact if representation in ('compact', 'both') else {},
            [leaf] if representation != 'compact' else [])
        before = await chunks()
        before_puts = list(storage.put_calls)
        before_leaf = backend._store.winner(chunk_id)
        chunk_file = backend._store._chunk_path(chunk_id)
        before_bytes = chunk_file.read_bytes() if chunk_file.exists() else None
        token = server._active_backend.set(backend)
        @server._tool_errors(writer=True)
        async def publish():
            await backend.write_note('collision.md', 'PRIVATE_NOTE', create=True)
            return 'Created.'
        try:
            result = await publish()
        finally:
            server._active_backend.reset(token)
        assert result.startswith('Error: existing shared content chunk')
        assert 'inspect and resolve the conflicting leaf in Obsidian' in result
        assert 'coordinate an alternate hashAlg or a positive customChunkSize' in result
        assert 'PRIVATE' not in result
        assert await chunks() == before
        assert storage.put_calls == before_puts
        assert backend._store.winner(chunk_id) == before_leaf
        assert (chunk_file.read_bytes() if chunk_file.exists() else None) == before_bytes
        assert backend._store.winner(backend._doc_id('collision.md')) is None
