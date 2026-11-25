"""EntryManagerImpls.ts:133-159; DatabaseWriteLayer.ts:24-49; journalRecords.ts:69-108."""
import json
from pathlib import Path
import pytest
from storage_base import StorageError
from storage_journal import JournalBackend
from tests.test_settings_matrix import matrix_vault

V = json.loads((Path(__file__).parent / 'fixtures/collision_upstream.json').read_text())

@pytest.mark.parametrize('kind', ['journal', 'couchdb'])
async def test_transport_collision(tmp_path, kind):
    async with matrix_vault(tmp_path, kind, V['settings']) as (backend, doc, chunks, log, storage):
        await backend.startup_check()
        before = await chunks()
        if kind == 'couchdb':
            with pytest.raises(StorageError, match='positive customChunkSize.*sha1'):
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

async def test_literal_replacement_before_surrogates_still_reports(tmp_path):
    # Same upstream partition/ID multiset, reversed input order; TextEncoder
    # changes the surrogate pieces regardless of which raw piece dedupes first.
    async with matrix_vault(tmp_path,'journal',V['settings']) as (backend,*_):
        await backend.startup_check()
        result=await backend.write_note('permuted.md','\ufffd🙂',create=True)
        assert result['transport_replacements']==1
        assert {'code':'journal_surrogates_replaced','count':1} in result['notices']
        assert (await backend.read_note('permuted.md'))['text']==''.join(d['data'] for d in V['decoded'])
