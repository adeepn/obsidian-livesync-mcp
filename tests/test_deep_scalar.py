"""Native VaultService.ts:73-81, ContentSplitterBase.ts:68, chunks.ts:368-370."""
import hashlib
import json
from pathlib import Path

import pytest

from content_splitter import ContentSplitter, _js_add, _minimum_number, _splitter_property_key
from livesync_format import resolve_sync_max_size
from tests.test_selection_settings import milestone
from tests.test_tweak_startup import vault
from tests.test_storage_journal import make_backend, healthy_bucket, MILESTONE_KEY
from wire_encoding import js_number_string

FIXTURE = json.loads(Path('tests/fixtures/deep_scalar_upstream.json').read_text())


def value_for(recipe):
    value = recipe.get('seed')
    for _ in range(recipe.get('depth', 0)):
        value = [value]
    if recipe.get('cycle'):
        value = [1]
        value.extend([value, None, 2])
    if recipe.get('shared'):
        shared = [1, None]
        value = [shared, shared]
    if recipe.get('undefined'):
        # JSON has no undefined; both null and undefined join as empty fields.
        value = [None, None, [None, 1]]
    return value


@pytest.mark.parametrize('row', FIXTURE['rows'])
def test_native_deep_scalar_coercion(row):
    value = value_for(row['recipe'])
    assert _splitter_property_key(value) == row['primitive']
    assert js_number_string(_minimum_number(value)) == row['number']
    assert _js_add(value, value) == row['addition']
    splitter = ContentSplitter({'chunkSplitterVersion': 'v2', 'customChunkSize': value,
                                'minimumChunkSize': value})
    assert js_number_string(splitter.absolute_max_piece_size) == row['piece_size']
    limit = resolve_sync_max_size(milestone(syncMaxSizeInMB=value))
    for predicate in row['predicates']:
        assert (limit > 0 and limit * 1024 * 1024 < predicate['bytes']) == predicate['too_large']
    if row['adjusted'] is not None:
        # Native chunks.ts:368-370 compares and repeatedly adds the original
        # array minimum; compare its final primitive against executed upstream.
        source = value
        while 1000 / _minimum_number(source) > 100:
            source = _js_add(source, value)
        assert _splitter_property_key(source) == row['adjusted']


ADMITTED = [r for r in FIXTURE['rows'] if r['recipe'].get('depth') in (480, 500)]


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
@pytest.mark.parametrize('row', ADMITTED)
async def test_native_deep_scalar_startup(tmp_path, kind, row):
    # Actual transport JSON admission, without changing interpreter limits.
    async with vault(tmp_path, kind, milestone(syncMaxSizeInMB=value_for(row['recipe']),
            customChunkSize=value_for(row['recipe']), minimumChunkSize=value_for(row['recipe']))) as (b, writes):
        await b.startup_check()
        assert js_number_string(b.sync_max_size_mb) == row['number']
        assert not writes()


@pytest.mark.parametrize('row', ADMITTED)
async def test_native_deep_scalar_refresh(tmp_path, row):
    b, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await b.startup_check()
        storage._objects[MILESTONE_KEY] = json.dumps(milestone(syncMaxSizeInMB=value_for(row['recipe']),
            customChunkSize=value_for(row['recipe']), minimumChunkSize=value_for(row['recipe']))).encode()
        await b.refresh(force=True)
        assert js_number_string(b.sync_max_size_mb) == row['number']
    finally:
        await b.close()


def test_deep_scalar_provenance():
    assert FIXTURE['upstream_commit'] == '484c164450d61deb2e301582b60cb9b5f9fa977f'
    assert FIXTURE['dependencies'] == []  # Actual expressions use only JS builtins.
    assert FIXTURE['generator_sha256'] == hashlib.sha256(Path('scripts/extract_deep_scalar_vectors.mjs').read_bytes()).hexdigest()
