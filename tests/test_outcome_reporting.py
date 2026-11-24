"""P-15: completed writes survive bookkeeping failure; uncertain sends are explicit."""
import sqlite3

import pytest

from tests.test_storage_journal import make_backend, healthy_bucket


@pytest.mark.parametrize('tracking', ['processed', 'last_sent'])
@pytest.mark.parametrize('failures', [1, 100])
async def test_completed_delete_bookkeeping_self_heals(tmp_path, monkeypatch, caplog, tracking, failures):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    try:
        await backend.write_note('only.md', 'old', create=True)
        store = backend._store
        calls = 0
        original = store.mark_processed if tracking == 'processed' else store._meta_set
        def fail(*args):
            nonlocal calls
            if tracking == 'processed' or args[0] == 'last_sent_key':
                calls += 1
                if calls <= failures:
                    raise sqlite3.OperationalError('injected bookkeeping failure')
            return original(*args)
        with monkeypatch.context() as patch:
            patch.setattr(store, 'mark_processed' if tracking == 'processed' else '_meta_set', fail)
            assert await backend.delete_note('only.md') is True
        assert 1 < calls <= 3
        if failures == 100:
            assert 'WARNING' in caplog.text
        assert await backend.read_note('only.md') is None
        winner = store.winner('only.md')
        leaves = store.leaves('only.md')
        await backend.refresh(force=True)
        assert await backend.read_note('only.md') is None
        assert store.winner('only.md') == winner
        assert store.leaves('only.md') == leaves
        for name in storage.list(''):
            if name.endswith('-docs.jsonl.gz'):
                assert store.processed(name)
        await backend.write_note('next.md', 'next', create=True)
        assert store.last_sent_key and store.processed(store.last_sent_key)
    finally:
        await backend.close()
