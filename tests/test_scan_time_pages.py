"""Deterministic peer scan scheduling on both actual backend doubles.

Upstream commonlib serviceModules/ServiceFileAccessBase.ts:287-290 enumerates
all files. Time budgets and cursors are peer transport contracts; these tests
exercise composition, not a new upstream wire-format oracle.
"""
from contextvars import ContextVar
import json

import pytest

import server
from scan_budget import ScanBudget
from tests.test_upload_attachment import backend


TOOLS = [
    ('obsidian_search', ('needle',), 'results'),
    ('obsidian_get_all_tags', (), 'tags'),
    ('obsidian_get_link_graph', (), 'edges'),
    ('obsidian_list_notes', (), 'results'),
]


async def populate(backend):
    for i in range(7):
        await backend.write_note(f'pages/{i}.md',
                                 f'needle #tag [[{(i + 1) % 7}]]', create=True)


def compose(pages, key):
    if key == 'tags':
        counts = {}
        for page in pages:
            for tag, count in page[key].items():
                counts[tag] = counts.get(tag, 0) + count
        return counts
    return [row for page in pages for row in page[key]]


async def collect(fn, args):
    pages, cursor = [], ''
    for _ in range(12):
        page = json.loads(await fn(*args, cursor=cursor, limit=0))
        pages.append(page)
        if not page.get('truncated'):
            return pages
        assert page['scan']['stopped_because'] == 'time'
        assert page['next_cursor'] != cursor, 'timed page made no cursor progress'
        cursor = page['next_cursor']
    pytest.fail('timed scan failed to finish static vault')


@pytest.mark.parametrize('tool,args,key', TOOLS)
async def test_time_pages_compose_with_processing_clock(backend, monkeypatch,
                                                        tool, args, key):
    monkeypatch.setattr(server, 'backend', backend)
    await populate(backend)
    fn = getattr(server, tool)
    full = json.loads(await fn(*args, limit=0))
    now = [0.0]

    class MeasuredBudget(ScanBudget):
        def admit(self, path, size):
            accepted = super().admit(path, size)
            if accepted:
                now[0] += 1
            return accepted

    # Advance the injected clock by one deterministic processing unit after
    # admission. This models CPU work without wall-clock sleeps/load flakiness.
    monkeypatch.setattr(server, 'ScanBudget', MeasuredBudget)
    monkeypatch.setattr(server, '_scan_clock', lambda: now[0])
    monkeypatch.setattr(server, '_active_scan_limits',
                        ContextVar('time_pages', default=(0, 0, 2)))
    pages = await collect(fn, args)
    assert len(pages) > 1
    assert compose(pages, key) == full[key]
    if key in ('results', 'edges'):
        identities = [json.dumps(row, sort_keys=True) for row in compose(pages, key)]
        assert len(identities) == len(set(identities))


@pytest.mark.parametrize('tool,args,key', TOOLS[:3])
async def test_time_cursor_does_not_reconsume_prior_bodies(backend, monkeypatch,
                                                          tool, args, key):
    monkeypatch.setattr(server, 'backend', backend)
    await populate(backend)
    fn = getattr(server, tool)
    full = json.loads(await fn(*args, limit=0))
    now = [0.0]
    original = backend.iterate_texts

    async def timed(*args, **kwargs):
        async for row in original(*args, **kwargs):
            yield row
            now[0] += 1

    monkeypatch.setattr(backend, 'iterate_texts', timed)
    monkeypatch.setattr(server, '_scan_clock', lambda: now[0])
    monkeypatch.setattr(server, '_active_scan_limits',
                        ContextVar('time_bodies', default=(0, 0, 2)))
    pages = await collect(fn, args)
    assert compose(pages, key) == full[key]
