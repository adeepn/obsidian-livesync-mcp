"""Peer index scheduling; upstream pagination: LiveSyncLocalDB.ts:481-540.

The pinned upstream enumerates every page rather than imposing a vault cap.
Scheduling and resumability are peer API contracts, not upstream codec oracles.
"""
import pytest

from tests.test_storage_couchdb import fake_backend, fake_vault, plain_note


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def test_index_defaults_are_complete_beyond_old_note_cap():
    # LiveSyncLocalDB.ts:520-536 visits all ranges and pages.
    docs = {f'{i:05}.md': plain_note(f'{i:05}.md', f'{i:05}.md', [])
            for i in range(10001)}
    async with fake_backend(docs) as (backend, _):
        notes = await backend.list_notes()
        assert len(notes) == 10001
        assert backend.index_truncated is False


async def test_index_time_budget_resumes_pages_without_refetch(monkeypatch):
    import storage_couchdb
    monkeypatch.setattr(storage_couchdb, '_PAGE_SIZE', 2)
    docs = {f'{i}.md': plain_note(f'{i}.md', f'{i}.md', []) for i in range(8)}
    async with fake_backend(docs) as (backend, log):
        clock = Clock()
        backend._scan_clock = clock
        backend.scan_max_seconds = 1
        original = backend._request

        async def request(*args, **kwargs):
            result = await original(*args, **kwargs)
            clock.now += .2
            return result

        monkeypatch.setattr(backend, '_request', request)
        first = await backend.list_notes()
        assert 0 < len(first) < 8
        assert backend.index_truncated is True
        seen = len(first)
        for _ in range(10):
            page = await backend.list_notes()
            assert len(page) >= seen
            assert [n['path'] for n in page] == sorted(n['path'] for n in page)
            seen = len(page)
            if not backend.index_truncated:
                break
        assert seen == 8
        assert backend.index_truncated is False
        offsets = [int(r.url.params['skip']) for r in log
                   if r.method == 'GET' and r.url.path.endswith('/_all_docs')
                   and 'endkey' in r.url.params]
        assert offsets == sorted(set(offsets))
        info = await backend.backend_info()
        assert info['index_complete'] is True
        assert info['index_ids_collected'] == 8
        assert info['index_docs_indexed'] == 8


async def test_update_sequence_change_discards_partial_progress(monkeypatch):
    import storage_couchdb
    monkeypatch.setattr(storage_couchdb, '_PAGE_SIZE', 2)
    docs = {f'{i}.md': plain_note(f'{i}.md', f'{i}.md', []) for i in range(6)}
    async with fake_vault(docs) as (backend, raw, log):
        backend._scan_limits = (2, 0)
        first = await backend.list_notes()
        assert len(first) == 2
        assert backend.index_truncated
        old_seq = backend._index_seq
        response = await raw.put('obsidian/0.md', json={
            **docs['0.md'], '_rev': '1-fake1', 'mtime': 99})
        assert response.status_code == 201
        second = await backend.list_notes()
        assert backend._index_seq != old_seq
        assert len(second) == 2
        assert next(n for n in second if n['path'] == '0.md')['mtime'] == 99
        assert backend.index_truncated


async def test_index_positive_caps_resume_instead_of_hiding_later_notes():
    docs = {f'{i}.md': plain_note(f'{i}.md', f'{i}.md', []) for i in range(5)}
    async with fake_backend(docs) as (backend, _):
        backend._scan_limits = (2, 0)
        counts = []
        for _ in range(4):
            counts.append(len(await backend.list_notes()))
            if not backend.index_truncated:
                break
        assert counts == [2, 4, 5]
        assert backend.index_truncated is False


async def test_index_info_cannot_claim_complete_after_remote_update():
    docs = {'a.md': plain_note('a.md', 'a.md', [])}
    async with fake_vault(docs) as (backend, raw, _):
        await backend.list_notes()
        assert (await backend.backend_info())['index_complete'] is True
        response = await raw.put('obsidian/b.md', json=plain_note('b.md', 'b.md', []))
        assert response.status_code == 201
        assert (await backend.backend_info())['index_complete'] is False


async def test_slow_metadata_page_is_cached_compactly_and_resumes(monkeypatch):
    import storage_couchdb
    monkeypatch.setattr(storage_couchdb, '_PAGE_SIZE', 2)
    docs = {f'{i}.md': plain_note(f'{i}.md', f'{i}.md', [],
                                eden={'h:inline': {'data': 'sentinel-body'}})
            for i in range(3)}
    async with fake_backend(docs) as (backend, log):
        clock = Clock()
        backend._scan_clock = clock
        backend.scan_max_seconds = .1
        original = backend._post_all_docs

        async def delayed(*args, **kwargs):
            rows = await original(*args, **kwargs)
            clock.now += 1
            return rows

        monkeypatch.setattr(backend, '_post_all_docs', delayed)
        counts = [len(await backend.list_notes()) for _ in range(4)]
        assert counts == [0, 2, 3, 3]
        assert backend.index_truncated is False
        assert 'sentinel-body' not in repr(backend._index_pending)
        assert len([r for r in log if r.method == 'POST']) == 2


async def test_graph_cannot_claim_complete_after_mid_scan_index_resume(monkeypatch):
    import json
    import server
    from livesync_format import chunk_id_for

    body = '[[2]]'
    chunk = chunk_id_for(body)
    docs = {f'{i}.md': plain_note(f'{i}.md', f'{i}.md', [chunk])
            for i in range(3)}
    docs[chunk] = {'_id': chunk, 'type': 'leaf', 'data': body}
    async with fake_backend(docs) as (backend, _):
        backend._scan_limits = (2, 0)
        monkeypatch.setattr(server, 'backend', backend)
        result = json.loads(await server.obsidian_get_link_graph(limit=0))
        # The first metadata pass has only paths 0/1. A subsequent body pass
        # must not grow to 0/1/2 while retaining a 0/1 resolution namespace and
        # then advertise an untruncated graph with silently missing out-links.
        if result.get('truncated'):
            assert result['scan']['stopped_because'] == 'index'
            assert result['index_incomplete'] is True
        else:
            assert result['nodes'] == ['0.md', '1.md', '2.md']
            assert {'from': '0.md', 'to': '2.md'} in result['edges']


async def test_same_scan_reuses_one_index_view_and_next_scan_resumes():
    from scan_budget import ScanBudget, active_scan_budget

    docs = {f'{i}.md': plain_note(f'{i}.md', f'{i}.md', []) for i in range(3)}
    async with fake_backend(docs) as (backend, _):
        backend._scan_limits = (2, 0)
        budget = ScanBudget(max_seconds=0)
        token = active_scan_budget.set(budget)
        try:
            assert len(await backend.list_notes()) == 2
            assert len([meta async for meta, _ in backend.iterate_texts()]) == 2
            assert budget.index_views[id(backend)][1] is True
        finally:
            active_scan_budget.reset(token)
        token = active_scan_budget.set(ScanBudget(max_seconds=0))
        try:
            assert len(await backend.list_notes()) == 3
            assert backend.index_truncated is False
        finally:
            active_scan_budget.reset(token)


async def test_backlinks_page_resolves_target_behind_cursor(monkeypatch):
    import json
    import server
    from livesync_format import chunk_id_for

    body = '[[0]]'
    chunk = chunk_id_for(body)
    docs = {'0.md': plain_note('0.md', '0.md', []),
            '1.md': plain_note('1.md', '1.md', [chunk]),
            chunk: {'_id': chunk, 'type': 'leaf', 'data': body}}
    async with fake_backend(docs) as (backend, _):
        monkeypatch.setattr(server, 'backend', backend)
        cursor = server._encode_cursor('0.md')
        result = json.loads(await server.obsidian_get_links(
            '0.md', direction='in', cursor=cursor, limit=0))
        assert result['backlinks'] == [{'path': '1.md'}]


async def test_stats_partial_view_stays_incomplete_if_other_scan_finishes(monkeypatch):
    import json
    import server
    from scan_budget import active_scan_budget

    docs = {f'{i}.md': plain_note(f'{i}.md', f'{i}.md', []) for i in range(3)}
    async with fake_backend(docs) as (backend, _):
        backend._scan_limits = (2, 0)
        monkeypatch.setattr(server, 'backend', backend)
        original = backend.backend_info

        async def concurrently_completed_info():
            # Model another aggregate completing the shared index after this
            # response has captured its own partial metadata/body view.
            token = active_scan_budget.set(None)
            try:
                await backend.list_notes()
                return await original()
            finally:
                active_scan_budget.reset(token)

        monkeypatch.setattr(backend, 'backend_info', concurrently_completed_info)
        result = json.loads(await server.obsidian_vault_stats())
        assert result['total_notes'] == 2
        assert result['truncated'] is True
        assert result['scan']['stopped_because'] == 'index'
        assert result['index_complete'] is False
