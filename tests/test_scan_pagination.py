"""Peer operational pagination; upstream imposes no aggregate file-count cap.

Commonlib serviceModules/ServiceFileAccessBase.ts:287-290 returns every file;
:141-148 reads complete text. Cursors/windows are peer transport controls,
not invented device settings or oracle-derived format expectations.
"""
import asyncio
import json
from contextvars import ContextVar

import pytest
import server
from scan_budget import ScanBudget
from config import load_config
from tests.test_config import COUCHDB_ENV
from tests.test_upload_attachment import backend


def test_unlimited_defaults():
    cfg = load_config(COUCHDB_ENV)
    assert (cfg.scan_max_notes, cfg.scan_max_bytes, cfg.scan_max_seconds) == (0, 0, 20)
    assert cfg.text_verdict_cache_entries == 65536
    for key in ('LIVESYNC_SCAN_MAX_NOTES', 'LIVESYNC_SCAN_MAX_BYTES', 'LIVESYNC_SCAN_MAX_SECONDS'):
        assert getattr(load_config({**COUCHDB_ENV, key: '0'}), key.lower().removeprefix('livesync_')) == 0


def test_budget_clock_and_reasons():
    now = [0.0]
    budget = ScanBudget(0, 0, max_seconds=2, clock=lambda: now[0])
    assert budget.admit('a', 999999999)
    now[0] = 2
    assert not budget.admit('b', 1)
    assert budget.report()['stopped_because'] == 'time'
    assert budget.report()['elapsed_ms'] == 2000
    for limits, reason in [((1, 0), 'notes'), ((0, 2), 'bytes')]:
        b = ScanBudget(*limits, max_seconds=0)
        assert b.admit('a', 2)
        assert not b.admit('b', 1)
        assert b.report()['stopped_because'] == reason


async def seed(backend):
    for path in ('g4b/d.md', 'g4b/B.md', 'g4b/a.md', 'g4b/c.md'):
        await backend.write_note(path, 'needle #tag [[a]]', create=True)


@pytest.mark.parametrize('tool,args,key', [
    ('obsidian_search', ('needle',), 'results'),
    ('obsidian_list_notes', (), 'results'),
    ('obsidian_get_all_tags', (), 'tags'),
    ('obsidian_get_link_graph', (), 'edges'),
])
async def test_static_pages_compose(backend, monkeypatch, tool, args, key):
    monkeypatch.setattr(server, 'backend', backend)
    await seed(backend)
    fn = getattr(server, tool)
    full = json.loads(await fn(*args, limit=0))
    monkeypatch.setattr(server, '_active_scan_limits', ContextVar('limits', default=(2, 0, 0)))
    pages, cursor = [], ''
    for _ in range(5):
        page = json.loads(await fn(*args, cursor=cursor, limit=0))
        pages.append(page)
        if not page.get('truncated'):
            break
        assert page['scan']['stopped_because'] == 'notes'
        assert page['next_cursor'] != cursor
        cursor = page['next_cursor']
    else:
        pytest.fail('cursor made no progress')
    if key == 'tags':
        assert sum(p['tags'].get('tag', 0) for p in pages) == full['tags']['tag']
    else:
        combined = [row for page in pages for row in page[key]]
        assert combined == full[key]


async def test_search_match_limit(backend, monkeypatch):
    monkeypatch.setattr(server, 'backend', backend)
    await seed(backend)
    first = json.loads(await server.obsidian_search('needle', limit=2))
    assert len(first['results']) == 2
    assert first['scan']['stopped_because'] == 'limit'
    second = json.loads(await server.obsidian_search('needle', cursor=first['next_cursor'], limit=2))
    assert len(second['results']) == 2
    assert {r['path'] for r in first['results']}.isdisjoint(r['path'] for r in second['results'])


async def test_time_cursor_and_busy(backend, monkeypatch):
    monkeypatch.setattr(server, 'backend', backend)
    await seed(backend)
    now = [0.0]
    monkeypatch.setattr(server, '_scan_clock', lambda: now[0])
    original = backend.iterate_texts
    async def timed(*args, **kwargs):
        async for row in original(*args, **kwargs):
            yield row
            now[0] += 1
    monkeypatch.setattr(backend, 'iterate_texts', timed)
    monkeypatch.setattr(server, '_active_scan_limits', ContextVar('limits', default=(0, 0, 2)))
    result = json.loads(await server.obsidian_search('needle', limit=0))
    assert result['scan']['stopped_because'] == 'time'
    assert result['scan']['notes'] == 2
    assert result['next_cursor']
    monkeypatch.setattr(server, '_scan_semaphore', asyncio.Semaphore(0))
    monkeypatch.setattr(server, '_active_scan_limits', ContextVar('busy', default=(0, 0, .01)))
    response = json.loads(await server.obsidian_search('PRIVATE_REGEX'))
    assert response['reason'] == 'scans_busy'
    assert 'retry' in response['message'].lower()
    assert 'PRIVATE_REGEX' not in str(response)


@pytest.mark.parametrize('start,end', [(1, 1), (3, None), (2, 99), (None, 2)])
async def test_fetch_windows(backend, monkeypatch, start, end):
    monkeypatch.setattr(server, 'backend', backend)
    raw = 'first\r\nsecond\nlast'
    await backend.write_note('lines.md', raw, create=True)
    full = json.loads(await server.obsidian_fetch('lines.md'))
    assert full['content'] == raw
    assert 'total_lines' not in full  # omitted arguments preserve existing shape
    result = json.loads(await server.obsidian_fetch('lines.md', start_line=start, end_line=end))
    lines = raw.splitlines(keepends=True)
    selected = lines[(start or 1)-1:end]
    assert result['content'] == ''.join(selected)
    assert result['total_lines'] == len(lines)
    assert result['returned_lines'] == len(selected)
    assert result['truncated'] == (len(selected) < len(lines))


async def test_regex_timeout_is_per_note_and_hygienic(monkeypatch):
    from tests.fake_backend import FakeBackend
    fake = FakeBackend()
    fake.add('a.md', 'a' * 1000 + '!PRIVATE_NOTE')
    fake.add('b.md', 'safe a')
    from tests.regex_doubles import timeout_for
    timeout_for(monkeypatch, lambda pattern, text, op: 'PRIVATE_NOTE' in text)
    fake.regex_timeout_seconds = 10
    monkeypatch.setattr(server, 'backend', fake)
    response = json.loads(await server.obsidian_search('(a+)+$', regex=True))
    assert set(response['errors']) == {'a.md'}
    assert [row['path'] for row in response['results']] == ['b.md']
    assert response['scan']['notes'] == 2
    assert 'LIVESYNC_REGEX_TIMEOUT_SECONDS' in response['errors']['a.md']
    assert 'PRIVATE_NOTE' not in str(response)
    assert '(a+)+$' not in str(response)
    before = fake.notes['a.md']['rev']
    refusal = await server.obsidian_update_note('a.md', 'search-replace',
                                               search='(a+)+$', replace='PRIVATE_REPLACEMENT', regex=True)
    assert 'LIVESYNC_REGEX_TIMEOUT_SECONDS' in refusal
    assert 'PRIVATE' not in refusal and '(a+)+$' not in refusal
    assert fake.notes['a.md']['rev'] == before


async def test_stats_exposes_index_and_budgets(backend, monkeypatch):
    monkeypatch.setattr(server, 'backend', backend)
    response = json.loads(await server.obsidian_vault_stats())
    assert response['scan']['max_notes'] == 0
    assert response['scan']['max_bytes'] == 0
    assert response['scan']['max_seconds'] == 20
    if response['backend'] == 'couchdb':
        assert response['index_complete'] is True
        assert type(response['index_docs_indexed']) is int


@pytest.mark.parametrize('key', ['LIVESYNC_SCAN_MAX_SECONDS'])
@pytest.mark.parametrize('value', ['nan', 'inf', '-1', 'bad'])
def test_scan_seconds_rejects_invalid_values(key, value):
    from config import ConfigError
    with pytest.raises(ConfigError, match=key):
        load_config({**COUCHDB_ENV, key: value})


@pytest.mark.parametrize('tool', ['obsidian_get_link_graph', 'obsidian_vault_stats'])
async def test_backend_info_wait_respects_scan_deadline(monkeypatch, tool):
    from tests.fake_backend import FakeBackend
    fake = FakeBackend()
    fake.add('a.md', 'safe')
    async def slow_info():
        await asyncio.Event().wait()
        return {'backend': 'fake'}
    monkeypatch.setattr(fake, 'backend_info', slow_info)
    monkeypatch.setattr(server, 'backend', fake)
    monkeypatch.setattr(server, '_active_scan_limits', ContextVar('info_time', default=(0, 0, .01)))
    response = json.loads(await asyncio.wait_for(getattr(server, tool)(), 30))
    assert response['scan']['stopped_because'] == 'time'


async def test_stats_partial_snapshot_cannot_advertise_complete(monkeypatch):
    from tests.fake_backend import FakeBackend
    fake = FakeBackend()
    fake.add('a.md', '')
    fake.index_truncated = True
    async def newer_info():
        return {'backend': 'couchdb', 'index_complete': True}
    monkeypatch.setattr(fake, 'backend_info', newer_info)
    monkeypatch.setattr(server, 'backend', fake)
    result = json.loads(await server.obsidian_vault_stats())
    assert result['index_complete'] is False
    assert result['scan']['stopped_because'] == 'index'


def test_cursor_roundtrip_has_no_arbitrary_path_length_cap():
    path = 'x' * 30000
    assert server._decode_cursor(server._encode_cursor(path)) == path


async def test_aggregate_expiry_during_regex_validation_is_a_time_page(monkeypatch):
    from tests.fake_backend import FakeBackend
    import regex_runner
    now = [0.0]
    monkeypatch.setattr(server, 'backend', FakeBackend())
    monkeypatch.setattr(server, '_scan_clock', lambda: now[0])
    monkeypatch.setattr(server, '_active_scan_limits', ContextVar('validation_time', default=(0, 0, 1)))
    async def expired_validation(*args):
        now[0] = 1.0
        raise regex_runner.RegexTimeoutError('Increase LIVESYNC_REGEX_TIMEOUT_SECONDS')
    monkeypatch.setattr(server, '_regex_apply', expired_validation)
    response = json.loads(await server.obsidian_search('PRIVATE_PATTERN', regex=True))
    assert response['truncated'] is True
    assert response['scan']['stopped_because'] == 'time'
    assert response['scan']['notes'] == 0
    assert response['next_cursor']
    assert 'PRIVATE_PATTERN' not in str(response)
