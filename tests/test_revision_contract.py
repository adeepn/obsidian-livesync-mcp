"""Revision tokens must be real, and malformed notes must stay isolated."""

import httpx
import pytest

from storage_base import StorageError
from tests.test_storage_couchdb import fake_backend, plain_note


@pytest.mark.parametrize('revision', [None, '', 42, False, [], {}])
async def test_publication_refuses_invalid_revision_ack(monkeypatch, revision):
    async with fake_backend() as (backend, _):
        async def request(*args, **kwargs):
            return httpx.Response(201, json={'ok': True, 'rev': revision})
        monkeypatch.setattr(backend, '_request', request)
        with pytest.raises(StorageError):
            await backend._put_doc('n.md', {'type': 'plain'})


@pytest.mark.parametrize('revision', [None, '', 42])
async def test_scan_isolates_invalid_revision_in_index(revision, caplog):
    docs = {'good.md': plain_note('good.md', 'good.md', []),
            'bad.md': plain_note('bad.md', 'bad.md', [], _rev=revision)}
    async with fake_backend(docs) as (backend, _):
        listed = await backend.list_notes()
        scanned = [item async for item in backend.iterate_texts()]
        with pytest.raises(StorageError):
            await backend.read_note('bad.md')
    assert [m['path'] for m in listed] == ['good.md']
    assert [(m['path'], text) for m, text in scanned] == [('good.md', '')]
    assert 'skipping' in caplog.text
