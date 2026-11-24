"""P-15: peer refusals preserve notes; replay follows remote truth incrementally."""
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


@pytest.mark.parametrize('failure', ['missing-last-sent', 'chunk-install'])
async def test_peer_refusal_or_unapplied_local_commit_preserves_readable_notes(tmp_path, monkeypatch, failure):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    fresh = JournalBackend(storage, tmp_path / 'fresh')
    try:
        await backend.startup_check()
        await backend.write_note('only.md', 'irreplaceable', create=True)
        await backend.write_attachment('other.pdf', b'\x00\xffretained', create=True)
        before = await readable(backend)
        if failure == 'missing-last-sent':
            storage._objects.pop(backend._store.last_sent_key)
            state = snapshot(backend._store)
            objects = dict(storage._objects)
            storage.put_calls.clear()
            with pytest.raises(ConcurrentModification):
                await backend.write_note('new.md', 'new', create=True)
            assert snapshot(backend._store) == state
            assert storage._objects == objects
            assert storage.put_calls == []
        else:
            state = snapshot(backend._store)
            def fail_install(*args):
                raise OSError('injected chunk install failure')
            with monkeypatch.context() as patch:
                patch.setattr(journal_store.os, 'replace', fail_install)
                with pytest.raises(StorageError, match='outcome unknown'):
                    await backend.write_note('only.md', 'replacement with new chunk')
            assert snapshot(backend._store) == state
        await assert_preserved(backend, before)
        await fresh.startup_check()
        await fresh.refresh(force=True)
        # A fresh peer has only surviving remote history. Locally retained
        # survivors of a wipe are intentionally not resurrected remotely.
        for path, note in before.items():
            current = await fresh.read_note(path)
            if current is not None and failure != 'chunk-install':
                assert (current['text'], current['data'], current['rev']) == (note['text'], note['data'], note['rev'])
        if failure == 'chunk-install':
            await backend.refresh(force=True)
            assert (await backend.read_note('only.md'))['text'] == 'replacement with new chunk'
            assert (await fresh.read_note('only.md'))['text'] == 'replacement with new chunk'
    finally:
        await backend.close()
        await fresh.close()


async def test_verbatim_missing_journal_reproducer(tmp_path):
    backend, storage = make_backend(tmp_path)
    await backend.startup_check()
    try:
        await backend.write_note('only.md', 'irreplaceable', create=True)
        before = await backend.read_note('only.md')
        storage._objects.pop(backend._store.last_sent_key)
        state = snapshot(backend._store)
        storage.put_calls.clear()
        with pytest.raises(ConcurrentModification):
            await backend.write_note('new.md', 'new', create=True)
        assert snapshot(backend._store) == state
        assert storage.put_calls == []
        await assert_preserved(backend, {'only.md': before})
        fresh = JournalBackend(storage, tmp_path / 'fresh')
        try:
            await fresh.startup_check()
            await fresh.refresh(force=True)
            assert await fresh.read_note('only.md') is None
        finally:
            await fresh.close()
    finally:
        await backend.close()


@pytest.mark.parametrize('changed_epoch', [False, True])
async def test_wipe_replays_caches_only_and_retains_local_survivors(tmp_path, changed_epoch):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    try:
        await backend.write_note('shared.md', 'shared body', create=True)
        shared_key = backend._store.last_sent_key
        await backend.write_note('local.md', 'only here', create=True)
        before = await readable(backend)
        storage._objects.pop(backend._store.last_sent_key)
        if changed_epoch:
            storage._objects[SYNC_PARAMETERS_KEY] = params_blob(salt='rebuilt')
        backend._store.mark_processed('obsolete-journal')
        await backend.refresh(force=True)
        assert not backend._store.processed('obsolete-journal')
        assert backend._store.processed(shared_key)
        await assert_preserved(backend, before)
        for path in before:
            assert len(backend._store.leaves(path)) == 1
        await backend.refresh(force=True)
        for path in before:
            assert len(backend._store.leaves(path)) == 1
        fresh = JournalBackend(storage, tmp_path / 'fresh')
        try:
            await fresh.startup_check()
            await fresh.refresh(force=True)
            assert (await fresh.read_note('shared.md'))['text'] == 'shared body'
            assert await fresh.read_note('local.md') is None
        finally:
            await fresh.close()
    finally:
        await backend.close()


@pytest.mark.parametrize('send_history', [False, True])
async def test_epoch_only_preserves_dedupe_caches_like_upstream(tmp_path, send_history):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    try:
        await backend.refresh(force=True)
        if send_history:
            await backend.write_note('sent.md', 'body', create=True)
        last_sent = backend._store.last_sent_key
        processed = backend._store.files_processed_count()
        storage._objects[SYNC_PARAMETERS_KEY] = params_blob(salt='protocol-change')
        await backend.refresh(force=True)
        assert backend._store.files_processed_count() == processed
        assert backend._store.last_sent_key == last_sent
        assert backend._store.epoch == '2:protocol-change'
    finally:
        await backend.close()


async def test_preappend_epoch_change_is_read_only(tmp_path):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    try:
        await backend.write_note('only.md', 'irreplaceable', create=True)
        before = await readable(backend)
        state = snapshot(backend._store)
        storage._objects[SYNC_PARAMETERS_KEY] = params_blob(salt='new-epoch')
        storage.put_calls.clear()
        with pytest.raises(ConcurrentModification, match='refresh and retry'):
            await backend.write_note('new.md', 'new', create=True)
        assert snapshot(backend._store) == state
        assert storage.put_calls == []
        await assert_preserved(backend, before)
    finally:
        await backend.close()


@pytest.mark.parametrize('same_backend', [False, True])
async def test_identity_change_quarantines_complete_openable_old_state(tmp_path, caplog, same_backend):
    import logging
    from journal_store import JournalStore
    from storage_journal import MILESTONE_KEY
    from tests.test_storage_journal import milestone_blob
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    await backend.write_note('only.md', 'irreplaceable', create=True)
    await backend.write_attachment('other.pdf', b'\x00\xffretained', create=True)
    before = await readable(backend)
    state = backend._store.state_dir
    (state / 'sentinel').write_bytes(b'keep unknown files too')
    chunks = snapshot(backend._store)['chunks']
    # Close the SQLite WAL before recording stable files for byte comparison.
    await backend.close()
    old_files = {str(p.relative_to(state)): p.read_bytes()
                 for p in state.rglob('*') if p.is_file()}
    if not same_backend:
        backend = JournalBackend(storage, state)
    else:
        await backend.startup_check()  # active connection at identity switch
    storage._objects[MILESTONE_KEY] = milestone_blob(path_version=1)
    try:
        with caplog.at_level(logging.WARNING):
            await backend.startup_check()
        archives = list(state.with_name(state.name + '.quarantine').glob('*/state'))
        assert len(archives) == 1
        archived = archives[0]
        for name, data in old_files.items():
            assert (archived / name).read_bytes() == data, name
        assert not (state / 'sentinel').exists()
        assert backend._store.path_identity == 'nfc'
        assert backend._store.note_ids() == []
        assert str(archived) in caplog.text
        assert 'restore' in caplog.text and 'legacy' in caplog.text
        old = JournalStore(archived, 'legacy')
        try:
            assert snapshot(old)['chunks'] == chunks
            for path, note in before.items():
                doc, text, data, is_text = old.read_note(path)
                assert (text, data, doc['_rev']) == (note['text'], note['data'], note['rev'])
        finally:
            old.close()
        await backend.refresh(force=True)
        await assert_preserved(backend, before)
    finally:
        await backend.close()


async def test_failed_quarantine_move_preserves_old_store(tmp_path, monkeypatch):
    from pathlib import Path
    from journal_store import JournalStore
    backend, _ = make_backend(tmp_path)
    await backend.startup_check()
    await backend.write_note('only.md', 'irreplaceable', create=True)
    state = backend._store.state_dir
    await backend.close()
    def fail_move(*args):
        raise OSError('injected quarantine rename failure')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'rename', fail_move)
        with pytest.raises(OSError, match='injected quarantine rename failure'):
            JournalStore(state, 'nfc')
    old = JournalStore(state, 'legacy')
    try:
        assert old.read_note('only.md')[1] == 'irreplaceable'
    finally:
        old.close()


async def test_epoch_recovery_first_download_failure_retains_local_survivors(tmp_path, monkeypatch):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await backend.startup_check()
        await backend.write_note('only.md', 'irreplaceable', create=True)
        before = await readable(backend)
        storage._objects.pop(backend._store.last_sent_key)
        storage._objects[SYNC_PARAMETERS_KEY] = params_blob(salt='new-world')
        original_get = storage.get
        def fail_download(key, **kwargs):
            if key.endswith('.jsonl.gz'):
                raise StorageError('injected first download failure')
            return original_get(key, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(storage, 'get', fail_download)
            with pytest.raises(StorageError, match='could not be replayed'):
                await backend.refresh(force=True)
        await assert_preserved(backend, before)
    finally:
        await backend.close()
