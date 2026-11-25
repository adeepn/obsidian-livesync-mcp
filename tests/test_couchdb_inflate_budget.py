"""Native compress.ts:26-47,62-71 witnesses, P-4 local inflate protection.

Budget is operational, not an invented upstream file admission predicate.
"""
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from config import load_config
from tests.test_config import COUCHDB_ENV
from content_splitter import ContentSplitter
from couchdb_compression import decompress_data
from storage_base import StorageError
from storage_couchdb import CouchDBBackend
from tests.test_storage_couchdb import fake_vault

VECTORS = json.loads(Path('tests/fixtures/compression_upstream.json').read_text())['budget_vectors']


@pytest.mark.parametrize('vector', VECTORS, ids=lambda v: f"{v['size']}-{v['binary']}")
def test_bounded_decode_boundary(vector):
    limit = vector['result_bytes']
    result = decompress_data(vector['wire'], max_inflated_bytes=limit)
    assert hashlib.sha256(result.encode()).hexdigest() == vector['result_sha256']
    with pytest.raises(StorageError) as caught:
        decompress_data(vector['wire'], max_inflated_bytes=limit - 1)
    assert 'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES' in caught.value.format()
    assert vector['wire'] not in caught.value.format()


@pytest.mark.parametrize('batch', [False, True])
async def test_reviewer_bomb_refused_before_text_admission(batch):
    vector = next(v for v in VECTORS if v['size'] == 16*1024**2 and not v['binary'])
    doc = {'_id': 'h:a', 'type': 'leaf', 'data': vector['wire']}
    body = {'rows': [{'doc': doc}]} if batch else doc
    backend = CouchDBBackend('http://fake', 'u', 'p', 'vault',
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    backend.couchdb_max_inflated_bytes = 4096
    backend.text_scan_max_bytes = 4096
    try:
        with pytest.raises(StorageError) as caught:
            await (backend._post_all_docs(['h:a'], include_docs=True) if batch else backend._get_doc('h:a'))
        assert 'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES' in caught.value.format()
    finally:
        await backend.close()


async def test_reserved_prefix_budget_precedes_publication():
    wire = next(v['wire'] for v in VECTORS if v['size'] == 4097 and not v['binary'])
    async with fake_vault() as (backend, raw, log):
        backend.couchdb_max_inflated_bytes = 4096
        before = list(log)
        with pytest.raises(StorageError) as caught:
            await backend.write_note('inflate.md', wire, create=True)
        assert 'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES' in caught.value.format()
        assert not [r for r in log[len(before):] if r.method in ('PUT', 'DELETE')]


def test_budget_resolution_and_config():
    from couchdb_compression import resolved_inflate_budget
    assert resolved_inflate_budget(ContentSplitter()) == 32*1024**2
    # ContentSplitterBase.ts:68 derives cap; a larger legacy setting needs
    # proportionally more budget, independently of vault file admission.
    assert resolved_inflate_budget(ContentSplitter({'chunkSplitterVersion': 'v1', 'customChunkSize': 999})) > 102400000
    assert load_config(COUCHDB_ENV).couchdb_max_inflated_bytes is None
    cfg = load_config({**COUCHDB_ENV, 'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES': '4096'})
    import server
    backend = server.get_backend(cfg)
    assert backend.inflated_budget_bytes == 4096


def test_inflater_never_uses_unbounded_output(monkeypatch):
    import couchdb_compression as codec
    original = codec.zlib.decompressobj
    lengths = []

    class Bounded:
        def __init__(self, *args, **kwargs):
            self.inner = original(*args, **kwargs)

        def decompress(self, data, max_length=0):
            assert 0 < max_length <= 64 * 1024
            result = self.inner.decompress(data, max_length)
            lengths.append(len(result))
            return result

        def __getattr__(self, name):
            assert name != 'flush'
            return getattr(self.inner, name)

    monkeypatch.setattr(codec.zlib, 'decompressobj', Bounded)
    vector = next(v for v in VECTORS if v['size'] == 16*1024**2 and not v['binary'])
    with pytest.raises(StorageError):
        codec.decompress_data(vector['wire'], max_inflated_bytes=4096)
    assert sum(lengths) <= 4097  # only a discarded one-byte exhaustion probe
