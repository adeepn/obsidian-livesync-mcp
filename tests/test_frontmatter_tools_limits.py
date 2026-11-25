"""Frontmatter is a peer tool projection; upstream stores Blob bytes unchanged.

C:managers/EntryManager/EntryManagerImpls.ts:253-280 and
C:services/base/VaultService.ts:73-81 (pin in admission fixture).
"""
import json
import pytest
import server
from config import load_config, ConfigError
from tests.test_config import COUCHDB_ENV
from tests.test_reserved_compression_prefix import marker_vault


@pytest.mark.parametrize('name', ('LIVESYNC_FRONTMATTER_MAX_NODES', 'LIVESYNC_FRONTMATTER_MAX_DEPTH', 'LIVESYNC_FRONTMATTER_MAX_BYTES'))
@pytest.mark.parametrize('value', ('0', '-1', '1.5', 'nan', 'inf', 'private-marker', True, None))
def test_frontmatter_budget_validation(name, value):
    with pytest.raises(ConfigError) as error:
        load_config({**COUCHDB_ENV, name: value})
    assert name in str(error.value)
    assert 'private-marker' not in str(error.value)


def test_frontmatter_budget_config_defaults_and_overrides():
    cfg = load_config(COUCHDB_ENV)
    assert (cfg.frontmatter_max_nodes, cfg.frontmatter_max_depth) == (20_000, 200)
    cfg = load_config({**COUCHDB_ENV, 'LIVESYNC_FRONTMATTER_MAX_NODES': '2000000',
                       'LIVESYNC_FRONTMATTER_MAX_DEPTH': '300'})
    assert (cfg.frontmatter_max_nodes, cfg.frontmatter_max_depth) == (2_000_000, 300)


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
async def test_large_properties_and_tags_on_real_backend_tools(tmp_path, kind):
    async with marker_vault(tmp_path, kind, False) as (backend, _, mutations):
        text = '---\nitems: [' + ','.join('0' for _ in range(10001)) + ']\n---\nbody\n'
        await backend.write_note('large.md', text, create=True)
        token = server._active_backend.set(backend)
        try:
            result = json.loads(await server.obsidian_manage_properties('large.md'))
            assert len(result['items']) == 10001
            assert 'Updated properties' in await server.obsidian_manage_properties('large.md', 'set', '{"new":"value"}')
            assert 'Updated frontmatter tags' in await server.obsidian_manage_tags('large.md', 'add', 'tag')
            assert json.loads(await server.obsidian_manage_tags('large.md'))['frontmatter_tags'] == ['tag']
            assert len(json.loads(await server.obsidian_manage_properties('large.md'))['items']) == 10001
        finally:
            server._active_backend.reset(token)


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
@pytest.mark.parametrize('raw,reason', [
    ('items: [one, two, three, four, five]\n', 'nodes'),
    ('items: ' + '[' * 34 + '0' + ']' * 34 + '\n', 'depth'),
    ('private-marker: &a [*a]\n', 'alias expansion'),
    ('private-marker: [broken\n', 'invalid YAML'),
])
async def test_tools_refuse_typed_without_mutating_or_echoing_note(tmp_path, kind, raw, reason):
    async with marker_vault(tmp_path, kind, False) as (backend, _, mutations):
        text = '---\n' + raw + '---\nprivate-marker\n'
        await backend.write_note('raw.md', text, create=True)
        backend.frontmatter_max_nodes = 5 if reason == 'nodes' else 1_000_000
        backend.frontmatter_max_depth = 10 if reason == 'depth' else 200
        before = mutations()
        original_rev = (await backend.read_note('raw.md'))['rev']
        token = server._active_backend.set(backend)
        try:
            for operation in (
                lambda: server.obsidian_manage_properties('raw.md'),
                lambda: server.obsidian_manage_properties('raw.md', 'set', '{"new":1}'),
                lambda: server.obsidian_manage_tags('raw.md', 'add', 'tag')):
                result = await operation()
                assert f'frontmatter {reason}' in result
                assert 'intact and synced' in result
                assert 'raw note text' in result
                assert 'private-marker' not in result
            if kind == 'journal':
                assert mutations() == before
            # CouchDB refresh issues POST _all_docs reads; count neither those
            # nor other reads as mutations. The durable note revision must agree.
            stored = await backend.read_note('raw.md')
            assert stored['rev'] == original_rev
            assert stored['text'] == text
        finally:
            server._active_backend.reset(token)


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
async def test_create_budget_refusal_does_not_claim_note_synced(tmp_path, kind):
    async with marker_vault(tmp_path, kind, False) as (backend, _, mutations):
        backend.frontmatter_max_nodes = 3
        token = server._active_backend.set(backend)
        try:
            result = await server.obsidian_create_note('new.md', 'body', '{"items":[1,2,3,4]}')
            assert 'frontmatter nodes' in result
            assert 'no note was created' in result
            assert 'intact and synced' not in result
            assert await backend.read_note('new.md') is None
        finally:
            server._active_backend.reset(token)


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
async def test_scalar_alias_bytes_refused_by_tools(tmp_path, kind):
    # C:EntryManagerImpls.ts:253-280 syncs bytes; this is only a JSON tool guard.
    async with marker_vault(tmp_path, kind, False) as (backend, _, mutations):
        text = '---\nbase: &big ' + 'x' * 32768 + '\n' + ''.join(f'copy{i}: *big\n' for i in range(2048)) + '---\nbody\n'
        await backend.write_note('aliases.md', text, create=True)
        revision = (await backend.read_note('aliases.md'))['rev']
        token = server._active_backend.set(backend)
        try:
            for action in ('get', 'set'):
                result = await server.obsidian_manage_properties('aliases.md', action, '{"new":1}')
                assert 'frontmatter bytes' in result
                assert 'LIVESYNC_FRONTMATTER_MAX_BYTES' in result
                assert 'raw note text' in result
            assert (await backend.read_note('aliases.md'))['rev'] == revision
        finally:
            server._active_backend.reset(token)


def test_frontmatter_byte_configuration_wiring():
    cfg = load_config(COUCHDB_ENV)
    assert cfg.frontmatter_max_bytes == 4 * 1024**2
    cfg = load_config({**COUCHDB_ENV, 'LIVESYNC_FRONTMATTER_MAX_BYTES': '12345'})
    assert server.get_backend(cfg).frontmatter_max_bytes == 12345
