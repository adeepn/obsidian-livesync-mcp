"""Purpose is independent of HTTP verb.

C:pouchdb/LiveSyncLocalDB.ts:642-656 delegates allDocs reads and bulkDocs
writes separately; C:services/base/RemoteService.ts:135-159,373-381 observes
body failures after fetch settles. Published pouchdb-adapter-http 9.0.0
lib/index.es.js:851-861 issues POST for allDocs keys without mutation. Faults below are peer protection contracts,
not invented plugin-format oracle outputs.
"""
import httpx
import pytest

from storage_base import ConcurrentModification, OutcomeUnknown, StorageUnavailable
from storage_couchdb import CouchDBHTTPBudgetExceeded, CouchDBHTTPEncodingRefused
from tests.test_couchdb_http_budget import backend
from tests.test_couchdb_response_outcome import broken_ack, KINDS, assert_recovery
from tests.test_storage_couchdb import fake_backend

# Independent inventory: all production calls plus supported low-level endpoints.
ENDPOINTS = [
    ('GET', '', 'read'), ('HEAD', '', 'read'),
    ('GET', '_session', 'read'), ('HEAD', '_session', 'read'),
    ('GET', 'vault', 'read'), ('HEAD', 'vault', 'read'),
    *[(method, path, purpose) for path in ('vault', '/vault')
      for method, purpose in [('PUT', 'write'), ('POST', 'write'), ('DELETE', 'write')]],
    ('GET', '/vault', 'read'), ('HEAD', '/vault', 'read'),
    ('GET', 'vault/_db_info', 'read'),
    ('GET', 'vault/_changes', 'read'), ('POST', 'vault/_changes', 'read'),
    ('GET', 'vault/_all_docs', 'read'), ('POST', 'vault/_all_docs', 'read'),
    ('GET', 'vault/_bulk_docs', 'read'), ('POST', 'vault/_bulk_docs', 'write'),
    *[(m, p, purpose) for p in ('vault/a', 'vault/_local/check',
                               'vault/_local%2Fcheck', 'vault/a/attachment')
      for m, purpose in [('GET', 'read'), ('HEAD', 'read'), ('PUT', 'write'),
                         ('DELETE', 'write')]],
    ('POST', 'vault/a', 'write'),
    ('GET', 'vault/h:leaf', 'read'), ('HEAD', 'vault/h:leaf', 'read'),
    ('PUT', 'vault/h:leaf', 'write-leaf'), ('DELETE', 'vault/h:leaf', 'write-leaf'),
]


@pytest.mark.parametrize('method,path,purpose', ENDPOINTS)
@pytest.mark.parametrize('failure', [*KINDS, 409, 412, 400, 401, 404, 408, 500])
async def test_endpoint_failure_matrix(method, path, purpose, failure):
    peer, _ = backend(b'', 'identity')
    def send(request):
        if isinstance(failure, int):
            return httpx.Response(failure, content=b'{}', request=request)
        return broken_ack(failure, request)
    peer._client._transport = httpx.MockTransport(send)
    try:
        if failure == 404 and method in {'GET', 'HEAD'}:
            assert (await peer._request(method, path)).status_code == 404
            return
        expected = (ConcurrentModification if failure == 409 else
                    OutcomeUnknown if purpose != 'read' and (not isinstance(failure, int) or failure in {408, 500}) else
                    StorageUnavailable if isinstance(failure, int) or failure == 'dropped' else
                    CouchDBHTTPBudgetExceeded if failure == 'budget' else CouchDBHTTPEncodingRefused)
        with pytest.raises(expected) as caught:
            await peer._request(method, path)
        assert type(caught.value) is expected
        if purpose == 'read':
            assert 'unknown' not in caught.value.format()
        if expected is OutcomeUnknown and not isinstance(failure, int):
            assert_recovery(caught.value, failure)
    finally:
        await peer.close()


@pytest.mark.parametrize('method,path', [('POST', 'vault/_unknown_PRIVATE'),
                                        ('GET', 'vault/_design/x/_view/y'),
                                        ('POST', 'vault/_find'), ('PATCH', 'vault/a')])
async def test_unclassified_request_refused_before_send(method, path):
    peer, stream = backend(b'{}', 'identity')
    try:
        with pytest.raises(StorageUnavailable) as caught:
            await peer._request(method, path)
        assert type(caught.value).__name__ == 'CouchDBRequestPurposeRefused'
        assert 'purpose' in caught.value.format() and 'upgrade' in caught.value.format()
        assert 'PRIVATE' not in caught.value.format()
        assert stream.seen == 0
    finally:
        await peer.close()


@pytest.mark.parametrize('kind', [*KINDS, 408])
@pytest.mark.parametrize('tool', ['fetch', 'list_notes', 'search'])
async def test_public_read_has_no_publication_uncertainty(kind, tool, monkeypatch):
    import server
    async with fake_backend() as (peer, _):
        await peer.write_note('only.md', 'old', create=True)
        original = peer._client._transport.handle_async_request
        async def send(request):
            response = await original(request)
            if request.method == 'POST' and request.url.path.endswith('/_all_docs'):
                return (httpx.Response(408, content=b'{}', request=request)
                        if kind == 408 else broken_ack(kind, request))
            return response
        with monkeypatch.context() as patch:
            patch.setattr(peer._client._transport, 'handle_async_request', send)
            patch.setattr(server, 'backend', peer)
            peer.couchdb_max_inflated_bytes = 4096
            result = (await server.obsidian_fetch('only.md') if tool == 'fetch' else
                      await server.obsidian_list_notes() if tool == 'list_notes' else
                      await server.obsidian_search('old'))
        assert 'MAY' not in result and 'outcome unknown' not in result
        assert 'PRIVATE' not in result
        assert ('LIVESYNC_COUCHDB_MAX_INFLATED_BYTES' if kind == 'budget' else
                'ReadError' if kind == 'dropped' else
                'retry the read' if kind == 408 else 'identity') in result
        assert (await peer.read_note('only.md'))['text'] == 'old'


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('operation', ['append', 'create', 'overwrite', 'replace',
                                     'move', 'copy', 'delete', 'properties', 'tags', 'upload'])
async def test_public_write_retains_recovery(kind, operation, monkeypatch):
    import server
    from tests.test_couchdb_response_outcome import intercept
    async with fake_backend() as (peer, _):
        await peer.write_note('only.md', 'old', create=True)
        dest = ('new.md' if operation in {'create', 'move', 'copy'} else
                'asset.bin' if operation == 'upload' else 'only.md')
        with monkeypatch.context() as patch:
            intercept(patch, peer, kind, 'PUT', '/' + dest)
            patch.setattr(server, 'backend', peer)
            if operation == 'append':
                result = await server.obsidian_update_note('only.md', 'append', 'extra')
            elif operation == 'create':
                result = await server.obsidian_create_note(dest, 'new')
            elif operation == 'overwrite':
                result = await server.obsidian_update_note('only.md', 'overwrite', 'new')
            elif operation == 'replace':
                result = await server.obsidian_update_note('only.md', 'search-replace', search='old', replace='new')
            elif operation == 'move':
                result = await server.obsidian_move_note('only.md', dest)
            elif operation == 'copy':
                result = await server.obsidian_duplicate_note('only.md', dest)
            elif operation == 'delete':
                result = await server.obsidian_delete_note('only.md')
            elif operation == 'properties':
                result = await server.obsidian_manage_properties('only.md', 'set', '{"probe":17}')
            elif operation == 'tags':
                result = await server.obsidian_manage_tags('only.md', 'add', 'probe')
            else:
                result = await server.obsidian_upload_attachment(dest, '/wEC')
        assert 'MAY have been applied' in result and 're-read' in result
        assert ('NON-IDEMPOTENT' if operation in {'append', 'replace'} else
                'IDEMPOTENT FOR THE SAME INPUT') in result
        assert 'PRIVATE' not in result
        assert ('LIVESYNC_COUCHDB_MAX_INFLATED_BYTES' if kind == 'budget' else
                'ReadError' if kind == 'dropped' else 'identity') in result
        note = await peer.read_note(dest)
        if operation == 'delete':
            assert note is None
        else:
            assert note is not None
        if operation == 'move':
            assert await peer.read_note('only.md') is not None


@pytest.mark.parametrize('kind', KINDS)
async def test_leaf_fault_preserves_existing_note_and_sibling(kind, monkeypatch):
    from storage_base import MutationNotApplied
    async with fake_backend() as (peer, _):
        await peer.write_note('only.md', 'old', create=True)
        old = await peer._get_doc('only.md')
        sibling = await peer._write_chunk('only.md', 'sibling')
        original = peer._client._transport.handle_async_request
        leaves = []
        async def send(request):
            response = await original(request)
            if request.method == 'PUT' and '/h:' in request.url.path:
                leaves.append(request.url.path.rsplit('/', 1)[1])
                return broken_ack(kind, request)
            return response
        with monkeypatch.context() as patch:
            patch.setattr(peer._client._transport, 'handle_async_request', send)
            peer.couchdb_max_inflated_bytes = 4096
            with pytest.raises(MutationNotApplied) as caught:
                await peer._write_chunk('only.md', 'new orphan')
        assert 'orphan' in caught.value.format()
        assert await peer._get_doc('only.md') == old
        assert (await peer._get_doc(sibling))['data'] == 'sibling'
        assert leaves and await peer._get_doc(leaves[0]) is not None
