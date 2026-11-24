"""P-13 preservation at journal wipe, epoch, and chunk-install boundaries."""
import pytest

import journal_store
from storage_base import ConcurrentModification, StorageError
from storage_journal import JournalBackend, SYNC_PARAMETERS_KEY
from tests.test_storage_journal import make_backend, healthy_bucket, params_blob


def snapshot(store):
    return {
        'sql': list(store._conn.iterdump()),
        'chunks': {str(p.relative_to(store.state_dir)): p.read_bytes()
                   for p in store._chunks_dir.rglob('*') if p.is_file()},
    }


async def readable(backend):
    return {n['path']: await backend.read_note(n['path'])
            for n in await backend.list_notes()}


async def assert_preserved(backend, before):
    for path, note in before.items():
        after = await backend.read_note(path)
        assert after is not None, path
        assert (after['text'], after['data'], after['rev']) == (note['text'], note['data'], note['rev']), path


async def test_chunk_install_failure_preserves_every_readable_note(tmp_path, monkeypatch):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    fresh = JournalBackend(storage, tmp_path / 'fresh')
    try:
        await backend.startup_check()
        await backend.write_note('only.md', 'irreplaceable', create=True)
        await backend.write_attachment('other.pdf', b'\x00\xffretained', create=True)
        before = await readable(backend)
        state = snapshot(backend._store)
        def fail_install(*args):
            raise OSError('injected chunk install failure')
        with monkeypatch.context() as patch:
            patch.setattr(journal_store.os, 'replace', fail_install)
            with pytest.raises(StorageError, match='journal publication or verification failed'):
                await backend.write_note('only.md', 'replacement with new chunk')
        assert snapshot(backend._store) == state
        await assert_preserved(backend, before)
        await fresh.startup_check()
        await fresh.refresh(force=True)
        await backend.refresh(force=True)
        assert (await backend.read_note('only.md'))['text'] == 'replacement with new chunk'
        assert (await fresh.read_note('only.md'))['text'] == 'replacement with new chunk'
    finally:
        await backend.close()
        await fresh.close()
