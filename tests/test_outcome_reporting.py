"""P-15: completed writes survive bookkeeping failure; uncertain sends are explicit."""
import sqlite3
import json

import pytest

from tests.test_upload_attachment import backend
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
        newest = max((key for key in storage.list('') if key.endswith('-docs.jsonl.gz')), key=lambda key: int(key.split('-')[0]))
        assert store.last_sent_key == newest
        for name in storage.list(''):
            if name.endswith('-docs.jsonl.gz'):
                assert store.processed(name)
        await backend.write_note('next.md', 'next', create=True)
        assert store.last_sent_key and store.processed(store.last_sent_key)
    finally:
        await backend.close()


@pytest.mark.parametrize('operation', ['delete', 'overwrite', 'copy', 'rename'])
@pytest.mark.parametrize('failure', ['accepted', 'accepted_timeout', 'accepted_reset', 'before', 'connect_timeout', 'pool_timeout', 400, 403, 404, 409])
async def test_couchdb_mutation_transport_outcomes(operation, failure, monkeypatch):
    import httpx
    from storage_base import StorageError
    from tests.test_storage_couchdb import fake_backend
    async with fake_backend() as (backend, log):
        await backend.write_note('only.md', 'old', create=True)
        original = backend._client.request
        target = 'only.md' if operation in ('delete', 'overwrite') else 'target.md'
        async def request(method, path, **kwargs):
            if method == 'PUT' and path.endswith('/' + target):
                if failure == 'before':
                    raise httpx.ConnectError('before send')
                if failure == 'connect_timeout':
                    raise httpx.ConnectTimeout('before send')
                if failure == 'pool_timeout':
                    raise httpx.PoolTimeout('before send')
                if isinstance(failure, int):
                    return httpx.Response(failure, json={'error': 'refused'})
                await original(method, path, **kwargs)
                error = {'accepted': httpx.ReadError, 'accepted_timeout': httpx.ReadTimeout, 'accepted_reset': httpx.WriteError}[failure]
                raise error('response lost after acceptance')
            return await original(method, path, **kwargs)
        monkeypatch.setattr(backend._client, 'request', request)
        calls = {
            'delete': lambda: backend.delete_note('only.md'),
            'overwrite': lambda: backend.write_note('only.md', 'new'),
            'copy': lambda: backend.copy_note('only.md', 'target.md'),
            'rename': lambda: backend.rename_note('only.md', 'target.md'),
        }
        with pytest.raises(StorageError) as caught:
            await calls[operation]()
        if failure in ('accepted', 'accepted_timeout', 'accepted_reset'):
            assert type(caught.value).__name__ == 'OutcomeUnknown'
            assert str(caught.value).startswith('outcome unknown')
            assert 're-read' in str(caught.value)
            if operation == 'delete':
                assert await backend.read_note('only.md') is None
            else:
                assert (await backend.read_note(target))['text'] == ('new' if operation == 'overwrite' else 'old')
        else:
            assert type(caught.value).__name__ != 'OutcomeUnknown'
            assert (await backend.read_note('only.md'))['text'] == 'old'
            assert await backend.read_note('target.md') is None


@pytest.mark.parametrize('phase', ['lost_response', 'local_apply', 'readback'])
async def test_journal_published_uncertainty_replays_to_convergence(tmp_path, monkeypatch, phase):
    from storage_base import StorageError, StorageUnavailable
    backend, storage = make_backend(tmp_path, healthy_bucket())
    await backend.startup_check()
    try:
        await backend.write_note('only.md', 'old', create=True)
        original = storage.put_if_absent
        def lost(name, blob):
            original(name, blob)
            raise StorageUnavailable('response lost')
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError('local apply failed')
        with monkeypatch.context() as patch:
            if phase == 'lost_response':
                patch.setattr(storage, 'put_if_absent', lost)
            elif phase == 'local_apply':
                patch.setattr(backend._store, 'apply_journal', fail)
            else:
                get = storage.get
                patch.setattr(storage, 'get', lambda key: None if key.endswith('-docs.jsonl.gz') else get(key))
            with pytest.raises(StorageError) as caught:
                await backend.delete_note('only.md')
            assert type(caught.value).__name__ == 'OutcomeUnknown'
        await backend.refresh(force=True)
        assert await backend.read_note('only.md') is None
        newest = max((key for key in storage.list('') if key.endswith('-docs.jsonl.gz')), key=lambda key: int(key.split('-')[0]))
        assert backend._store.last_sent_key == newest
        before = backend._store.leaves('only.md')
        await backend.refresh(force=True)
        assert backend._store.leaves('only.md') == before
        for name in storage.list(''):
            if name.endswith('-docs.jsonl.gz'):
                assert backend._store.processed(name)
    finally:
        await backend.close()


@pytest.mark.parametrize('operation', ['create', 'upload', 'copy', 'move', 'delete',
    'overwrite', 'append', 'prepend', 'after-heading', 'search-replace',
    'properties_set', 'properties_delete', 'tags_add', 'tags_remove',
    'node_add', 'node_remove', 'edge_add', 'edge_remove', 'template'])
async def test_every_mutating_tool_reports_exact_retry_class(backend, monkeypatch, operation):
    import json
    import server
    import storage_base
    await backend.write_note('invariant.md', '---\nkey: value\ntags: [tag]\n---\n# heading\nold', create=True)
    await backend.write_note('invariant.canvas', json.dumps({
        'nodes': [{'id': 'a', 'type': 'text', 'text': 'old'}, {'id': 'b', 'type': 'text', 'text': 'other'}],
        'edges': [{'id': 'edge', 'fromNode': 'a', 'toNode': 'b'}],
    }), create=True)
    monkeypatch.setattr(server, 'backend', backend)
    unknown = storage_base.OutcomeUnknown()
    async def fail(*args, **kwargs):
        raise unknown
    for method in ['write_note', 'write_attachment', 'copy_note', 'rename_note', 'delete_note']:
        monkeypatch.setattr(backend, method, fail)
    calls = {
        'create': lambda: server.obsidian_create_note('new.md', 'new'),
        'upload': lambda: server.obsidian_upload_attachment('new.pdf', 'bGFyZ2U='),
        'copy': lambda: server.obsidian_duplicate_note('invariant.md', 'target.md'),
        'move': lambda: server.obsidian_move_note('invariant.md', 'target.md'),
        'delete': lambda: server.obsidian_delete_note('invariant.md'),
        'properties_set': lambda: server.obsidian_manage_properties('invariant.md', 'set', '{"key":"larger"}'),
        'properties_delete': lambda: server.obsidian_manage_properties('invariant.md', 'delete', keys='key'),
        'tags_add': lambda: server.obsidian_manage_tags('invariant.md', 'add', 'new'),
        'tags_remove': lambda: server.obsidian_manage_tags('invariant.md', 'remove', 'tag'),
        'node_add': lambda: server.obsidian_canvas_node('invariant.canvas', text='new'),
        'node_remove': lambda: server.obsidian_canvas_node('invariant.canvas', 'remove', node_id='a'),
        'edge_add': lambda: server.obsidian_canvas_edge('invariant.canvas', from_node='a', to_node='b'),
        'edge_remove': lambda: server.obsidian_canvas_edge('invariant.canvas', 'remove', edge_id='edge'),
        'template': lambda: server.obsidian_create_from_template('invariant.md', 'new.md'),
    }
    if operation in ['overwrite', 'append', 'prepend', 'after-heading', 'search-replace']:
        result = await server.obsidian_update_note('invariant.md', operation, 'new',
            **({'heading':'heading'} if operation == 'after-heading' else
               {'search':'old','replace':'larger'} if operation == 'search-replace' else {}))
    else:
        result = await calls[operation]()
    for path in (['invariant.canvas'] if operation.startswith(('node_', 'edge_'))
                 else ['invariant.md', 'target.md'] if operation in ('copy', 'move')
                 else ['invariant.md', 'new.md'] if operation == 'template'
                 else ['new.pdf'] if operation == 'upload'
                 else ['new.md'] if operation == 'create' else ['invariant.md']):
        assert path in result
    assert result.startswith('outcome unknown')
    assert 'MAY have been applied' in result and 're-read' in result
    labels = {
        'create': 'note create', 'upload': 'attachment upload',
        'copy': 'copy', 'move': 'move', 'delete': 'delete',
        'overwrite': 'overwrite', 'append': 'append', 'prepend': 'prepend',
        'after-heading': 'after-heading insertion', 'search-replace': 'search-replace',
        'properties_set': 'properties set', 'properties_delete': 'properties delete',
        'tags_add': 'tags add', 'tags_remove': 'tags remove',
        'node_add': 'canvas node add', 'node_remove': 'canvas node remove',
        'edge_add': 'canvas edge add', 'edge_remove': 'canvas edge remove',
        'template': 'template create',
    }
    assert labels[operation] in result
    if operation in ('append', 'prepend', 'search-replace', 'after-heading', 'node_add', 'edge_add'):
        assert 'NON-IDEMPOTENT' in result
        assert 'blind retry can duplicate the change' in result
        assert 're-read the note and apply the remaining change manually rather than retrying blindly' in result
        assert 'retrying the same request is safe' not in result
    else:
        assert 'IDEMPOTENT FOR THE SAME INPUT' in result
        assert 're-read to see whether it applied; retrying the same request is safe' in result
        assert 'blind retry can duplicate' not in result


@pytest.mark.parametrize('method', ['put', 'put_if_absent'])
@pytest.mark.parametrize('failure', ['connect', 'read', 'reset', 400, 403, 404, 409, 500])
def test_s3_put_classifies_refusal_before_send_and_unknown(method, failure, monkeypatch):
    from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError, ConnectionClosedError
    from object_storage import Boto3ObjectStorage
    from tests.test_object_storage import make_cfg
    from storage_base import StorageError
    storage = Boto3ObjectStorage(make_cfg())
    if failure == 'connect':
        error = EndpointConnectionError(endpoint_url='http://test.invalid')
    elif failure == 'read':
        error = ReadTimeoutError(endpoint_url='http://test.invalid')
    elif failure == 'reset':
        error = ConnectionClosedError(endpoint_url='http://test.invalid')
    else:
        error = ClientError({'Error': {'Code': str(failure)}, 'ResponseMetadata': {'HTTPStatusCode': failure}}, 'PutObject')
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(storage._client, 'put_object', fail)
    with pytest.raises(StorageError) as caught:
        getattr(storage, method)('1-docs.jsonl.gz', b'payload')
    unknown = failure in ('read', 'reset', 500)
    assert (type(caught.value).__name__ == 'OutcomeUnknown') == unknown


async def test_couchdb_accepted_write_with_malformed_ack_is_unknown(monkeypatch):
    import httpx
    from storage_base import OutcomeUnknown
    from tests.test_storage_couchdb import fake_backend
    async with fake_backend() as (backend, log):
        original = backend._client.request
        async def request(method, path, **kwargs):
            response = await original(method, path, **kwargs)
            if method == 'PUT' and path.endswith('/new.md'):
                return httpx.Response(201, json={})
            return response
        monkeypatch.setattr(backend._client, 'request', request)
        with pytest.raises(OutcomeUnknown):
            await backend.write_note('new.md', 'new', create=True)
        assert (await backend.read_note('new.md'))['text'] == 'new'


async def test_incoming_replay_keeps_remote_truth_applied_before_next_download_failure(tmp_path, monkeypatch):
    from storage_base import StorageError
    from storage_journal import JournalBackend
    writer, storage = make_backend(tmp_path / 'writer', healthy_bucket())
    reader = JournalBackend(storage, tmp_path / 'reader')
    try:
        await writer.startup_check()
        await writer.write_note('only.md', 'old', create=True)
        await reader.startup_check()
        await writer.write_note('only.md', 'remote replacement')
        applied = writer._store.last_sent_key
        await writer.write_note('later.md', 'later', create=True)
        blocked = writer._store.last_sent_key
        get = storage.get
        def fail(key, **kwargs):
            if key == blocked:
                raise StorageError('next download failed')
            return get(key, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(storage, 'get', fail)
            with pytest.raises(StorageError):
                await reader.refresh(force=True)
        assert (await reader.read_note('only.md'))['text'] == 'remote replacement'
        assert reader._store.processed(applied)
        assert not reader._store.processed(blocked)
        await reader.refresh(force=True)
        assert (await reader.read_note('later.md'))['text'] == 'later'
    finally:
        await writer.close()
        await reader.close()


async def test_journal_proven_pre_send_failure_is_ordinary_and_preserves_notes(tmp_path, monkeypatch):
    from storage_base import MutationNotApplied, StorageError, OutcomeUnknown
    backend, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await backend.startup_check()
        await backend.write_note('only.md', 'old', create=True)
        before = await backend.read_note('only.md')
        objects = dict(storage._objects)
        def refuse(*args):
            raise MutationNotApplied('not sent')
        monkeypatch.setattr(storage, 'put_if_absent', refuse)
        with pytest.raises(StorageError) as caught:
            await backend.delete_note('only.md')
        assert not isinstance(caught.value, OutcomeUnknown)
        assert await backend.read_note('only.md') == before
        assert storage._objects == objects
    finally:
        await backend.close()


async def test_rename_source_retirement_response_lost_stays_unknown(monkeypatch):
    import httpx
    from storage_base import OutcomeUnknown
    from tests.test_storage_couchdb import fake_backend
    async with fake_backend() as (backend, log):
        await backend.write_note('only.md', 'old', create=True)
        original = backend._client.request
        async def request(method, path, **kwargs):
            response = await original(method, path, **kwargs)
            if method == 'PUT' and path.endswith('/only.md'):
                raise httpx.ReadError('source deletion response lost')
            return response
        monkeypatch.setattr(backend._client, 'request', request)
        import server
        monkeypatch.setattr(server, 'backend', backend)
        result = await server.obsidian_move_note('only.md', 'target.md')
        assert result.startswith('outcome unknown')
        assert 'only.md' in result
        assert 'target.md' in result
        assert await backend.read_note('only.md') is None
        assert (await backend.read_note('target.md'))['text'] == 'old'


@pytest.mark.parametrize('operation,phase', [
    ('write', 'chunk'), ('attachment', 'chunk'),
    ('write', 'metadata'), ('attachment', 'metadata'),
    ('copy', 'metadata'), ('rename', 'metadata'), ('delete', 'metadata'),
    ('rename', 'retirement'),
])
async def test_metadata_publication_uncertainty_boundary(monkeypatch, operation, phase):
    import httpx
    from storage_base import OutcomeUnknown, MutationNotApplied
    from tests.test_storage_couchdb import fake_backend
    async with fake_backend() as (peer, _):
        path = 'only.pdf' if operation == 'attachment' else 'only.md'
        await peer.write_note(path, b'old' if operation == 'attachment' else 'old', create=True)
        before = await peer.read_note(path)
        original = peer._client.request
        puts = []
        async def lost(method, url, **kwargs):
            response = await original(method, url, **kwargs)
            if method == 'PUT':
                body = json.loads(kwargs['content'])
                puts.append(body)
                hit = (body.get('type') == 'leaf' if phase == 'chunk' else
                       bool(body.get('deleted')) if phase == 'retirement' else
                       body.get('type') != 'leaf')
                if hit:
                    raise httpx.ReadError('accepted; response lost')
            return response
        monkeypatch.setattr(peer._client, 'request', lost)
        calls = {
            'write': lambda: peer.write_note(path, 'replacement'),
            'attachment': lambda: peer.write_attachment(path, b'replacement'),
            'copy': lambda: peer.copy_note(path, 'target.md'),
            'rename': lambda: peer.rename_note(path, 'target.md'),
            'delete': lambda: peer.delete_note(path),
        }
        expected = MutationNotApplied if phase == 'chunk' else OutcomeUnknown
        with pytest.raises(expected) as caught:
            await calls[operation]()
        assert type(caught.value) is expected
        if phase == 'chunk':
            assert 'note is unchanged' in str(caught.value)
            assert await peer.read_note(path) == before
            assert all(body.get('type') == 'leaf' for body in puts)
        elif phase == 'retirement':
            assert 'destination published' in str(caught.value)
            assert path in str(caught.value)
            assert 'target.md' in str(caught.value)
            assert await peer.read_note(path) is None
            assert (await peer.read_note('target.md'))['text'] == 'old'
        else:
            if operation in ('copy', 'rename', 'delete'):
                assert all(body.get('type') != 'leaf' for body in puts)
            if operation == 'delete':
                assert await peer.read_note(path) is None
            elif operation in ('copy', 'rename'):
                assert (await peer.read_note('target.md'))['text'] == 'old'
                assert await peer.read_note(path) == before
            else:
                note = await peer.read_note(path)
                assert (note['data'] if operation == 'attachment' else note['text']) == (b'replacement' if operation == 'attachment' else 'replacement')


async def test_canvas_add_lost_response_warns_before_blind_retry_duplicates(backend, monkeypatch):
    import json
    import server
    from storage_base import OutcomeUnknown
    monkeypatch.setattr(server, 'backend', backend)
    await backend.write_note('retry.canvas', '{"nodes": [], "edges": []}', create=True)
    write = backend.write_note
    async def accepted(*args, **kwargs):
        await write(*args, **kwargs)
        raise OutcomeUnknown()
    with monkeypatch.context() as patch:
        patch.setattr(backend, 'write_note', accepted)
        result = await server.obsidian_canvas_node('retry.canvas', text='once')
    assert 'canvas node add: NON-IDEMPOTENT' in result
    assert 're-read the note and apply the remaining change manually rather than retrying blindly' in result
    first = json.loads((await backend.read_note('retry.canvas'))['text'])
    assert len(first['nodes']) == 1
    await server.obsidian_canvas_node('retry.canvas', text='once')
    second = json.loads((await backend.read_note('retry.canvas'))['text'])
    assert len(second['nodes']) == 2
    assert second['nodes'][0]['id'] != second['nodes'][1]['id']


@pytest.mark.parametrize('remove_old_before_refresh', [False, True])
@pytest.mark.parametrize('restart', [False, True])
async def test_outbound_replay_repairs_stale_last_sent_and_still_detects_wipe(
    tmp_path, monkeypatch, remove_old_before_refresh, restart,
):
    from storage_journal import JournalBackend
    from storage_base import ConcurrentModification
    peer, storage = make_backend(tmp_path, healthy_bucket())
    await peer.startup_check()
    try:
        await peer.write_note('only.md', 'old', create=True)
        store = peer._store
        old_key = store.last_sent_key
        original = store._meta_set
        def fail_last_sent(key, value):
            if key == 'last_sent_key':
                raise sqlite3.OperationalError('bookkeeping unavailable')
            return original(key, value)
        with monkeypatch.context() as patch:
            patch.setattr(store, '_meta_set', fail_last_sent)
            await peer.delete_note('only.md')
        newest = max((key for key in storage.list('') if key.endswith('-docs.jsonl.gz')), key=lambda key: int(key.split('-')[0]))
        assert store.last_sent_key == old_key
        if remove_old_before_refresh:
            storage._objects.pop(old_key)
        if restart:
            await peer.close()
            peer = JournalBackend(storage, tmp_path / 'state')
            await peer.startup_check()
            store = peer._store
        await peer.refresh(force=True)
        assert store.last_sent_key == newest
        assert store.processed(newest)
        assert await peer.read_note('only.md') is None
        state = list(store._conn.iterdump())
        leaves = store.leaves('only.md')
        await peer.refresh(force=True)
        assert list(store._conn.iterdump()) == state
        assert store.leaves('only.md') == leaves
        if not remove_old_before_refresh:
            storage._objects.pop(old_key)
        # The newest outbound journal, rather than stale send history, guards writes.
        await peer.write_note('next.md', 'next', create=True)
        latest = store.last_sent_key
        storage._objects.pop(latest)
        with pytest.raises(ConcurrentModification):
            await peer.write_note('refused.md', 'no', create=True)
        await peer.refresh(force=True)
        assert store.last_sent_key == ''
        assert (await peer.read_note('next.md'))['text'] == 'next'
    finally:
        await peer.close()


async def test_crash_after_pending_record_before_publish_is_cleaned_on_replay(tmp_path, monkeypatch):
    from storage_journal import JournalBackend
    class Crash(BaseException):
        pass
    peer, storage = make_backend(tmp_path, healthy_bucket())
    await peer.startup_check()
    try:
        await peer.write_note('only.md', 'old', create=True)
        last_sent = peer._store.last_sent_key
        objects = dict(storage._objects)
        def crash(*args):
            raise Crash()
        with monkeypatch.context() as patch:
            patch.setattr(storage, 'put_if_absent', crash)
            with pytest.raises(Crash):
                await peer.write_note('only.md', 'never published')
        pending = [row for row in peer._store.outbound_journals() if not row[2]]
        assert len(pending) == 1
        assert pending[0][0] not in storage._objects
        assert storage._objects == objects
        await peer.close()
        peer = JournalBackend(storage, tmp_path / 'state')
        await peer.startup_check()
        await peer.refresh(force=True)
        assert not [row for row in peer._store.outbound_journals() if not row[2]]
        assert peer._store.last_sent_key == last_sent
        assert (await peer.read_note('only.md'))['text'] == 'old'
        await peer.write_note('only.md', 'next')
    finally:
        await peer.close()


async def test_pending_collision_is_replayed_but_not_claimed_as_our_send(tmp_path, monkeypatch):
    from journal_codec import deflate_journal, serialize_journal
    from storage_journal import JournalBackend
    class Crash(BaseException):
        pass
    peer, storage = make_backend(tmp_path, healthy_bucket())
    await peer.startup_check()
    collision = []
    try:
        await peer.write_note('only.md', 'old', create=True)
        last_sent = peer._store.last_sent_key
        rival = deflate_journal(serialize_journal([], []))
        def collide(name, blob):
            storage._objects[name] = rival
            collision.append(name)
            raise Crash()
        with monkeypatch.context() as patch:
            patch.setattr(storage, 'put_if_absent', collide)
            with pytest.raises(Crash):
                await peer.write_note('only.md', 'our unpublished edit')
        await peer.close()
        peer = JournalBackend(storage, tmp_path / 'state')
        await peer.startup_check()
        await peer.refresh(force=True)
        assert peer._store.last_sent_key == last_sent
        assert peer._store.processed(collision[0])
        assert all(row[0] != collision[0] for row in peer._store.outbound_journals())
        assert (await peer.read_note('only.md'))['text'] == 'old'
    finally:
        await peer.close()


async def test_completed_publication_does_not_fail_on_tracking_read(tmp_path, monkeypatch):
    peer, _ = make_backend(tmp_path, healthy_bucket())
    await peer.startup_check()
    try:
        await peer.write_note('only.md', 'old', create=True)
        store = peer._store
        apply = store.apply_journal
        get = store._meta_get
        committed = False
        def applied(*args):
            nonlocal committed
            apply(*args)
            committed = True
        def broken_read(key):
            if committed and key == 'last_sent_key':
                raise sqlite3.OperationalError('post-commit tracking read failed')
            return get(key)
        with monkeypatch.context() as patch:
            patch.setattr(store, 'apply_journal', applied)
            patch.setattr(store, '_meta_get', broken_read)
            assert await peer.delete_note('only.md') is True
        await peer.refresh(force=True)
        assert await peer.read_note('only.md') is None
        assert store.processed(store.last_sent_key)
    finally:
        await peer.close()


@pytest.mark.parametrize('outcome,operation', [
    *[('skipped', op) for op in ('create', 'overwrite', 'upload', 'copy')],
    ('refused', 'move'),
    *[('outcome unknown', op) for op in ('create', 'overwrite', 'upload', 'copy', 'move', 'delete')],
])
async def test_structured_outcomes_name_affected_paths(backend, monkeypatch, outcome, operation):
    import json
    import httpx
    import server
    from storage_base import OutcomeUnknown
    from storage_journal import JournalBackend

    await backend.write_note('only.md', 'old', create=True)
    monkeypatch.setattr(server, 'backend', backend)
    if outcome == 'outcome unknown':
        if isinstance(backend, JournalBackend):
            publish = backend._object_storage.put_if_absent
            def lost(*args):
                publish(*args)
                raise OutcomeUnknown()
            monkeypatch.setattr(backend._object_storage, 'put_if_absent', lost)
        else:
            request = backend._client.request
            async def lost(method, path, **kwargs):
                response = await request(method, path, **kwargs)
                if method == 'PUT' and json.loads(kwargs['content']).get('type') != 'leaf':
                    raise httpx.ReadError('accepted; response lost')
                return response
            monkeypatch.setattr(backend._client, 'request', lost)
    else:
        backend.sync_max_size_mb_override = 1 / 1048576
        backend.sync_max_size_mb = 1 / 1048576
    calls = {
        'create': lambda: server.obsidian_create_note('target.md', 'larger'),
        'overwrite': lambda: server.obsidian_update_note('only.md', 'overwrite', 'larger'),
        'upload': lambda: server.obsidian_upload_attachment('target.md', 'bGFyZ2Vy'),
        'copy': lambda: server.obsidian_duplicate_note('only.md', 'target.md'),
        'move': lambda: server.obsidian_move_note('only.md', 'target.md'),
        'delete': lambda: server.obsidian_delete_note('only.md'),
    }
    result = await calls[operation]()
    if outcome == 'skipped':
        response = json.loads(result)
        assert response['status'] == outcome
        result = response['message']
    elif outcome == 'refused':
        assert 'move was not performed' in result
    else:
        assert result.startswith(outcome)
    paths = (['only.md', 'target.md'] if operation in ('copy', 'move')
             else ['only.md'] if operation in ('overwrite', 'delete') else ['target.md'])
    for path in paths:
        assert path in result
