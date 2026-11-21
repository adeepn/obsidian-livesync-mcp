"""Security boundaries for untrusted journal peers (offline only)."""
import json
import os
import stat
import sys
from io import BytesIO
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

import journal_codec as codec
from journal_store import JournalStore, parse_rev
from livesync_format import PATH_IDENTITY_LEGACY
from object_storage import Boto3ObjectStorage
from storage_base import StorageError
from storage_journal import JournalBackend, MILESTONE_KEY, SYNC_PARAMETERS_KEY
from tests.fake_object_storage import FakeObjectStorage
from tests.test_storage_journal import milestone_blob, params_blob, journal_blob, meta_doc


async def backend(tmp_path, objects=None, **kwargs):
    storage = FakeObjectStorage({MILESTONE_KEY: milestone_blob(),
                                 SYNC_PARAMETERS_KEY: params_blob(), **(objects or {})})
    peer = JournalBackend(storage, tmp_path, **kwargs)
    await peer.startup_check()
    return peer, storage


def test_inflate_bomb():
    blob = codec.deflate_journal(b'x' * (1024 * 1024))
    with pytest.raises(ValueError, match='journal inflated output exceeds limit'):
        codec.inflate_journal(blob, max_output_bytes=4096)
    assert codec.inflate_journal(codec.deflate_journal(b'x' * 4096), max_output_bytes=4096) == b'x' * 4096


def test_parse_limits():
    with pytest.raises(ValueError, match='hostile: line 1:.*line.*limit'):
        codec.parse_journal(b'~h:a\x1f' + b'x' * 100 + b'\n', name='hostile', max_line_bytes=16)
    with pytest.raises(ValueError, match='hostile: line 2:.*record.*limit'):
        codec.parse_journal(b'~h:a\x1fx\n' * 2, name='hostile', max_records=1)


def test_deep_json():
    data = b'{"_id":"a","_rev":"1-a","nested":' + b'[' * (sys.getrecursionlimit() + 100) + b'0' + b']' * (sys.getrecursionlimit() + 100) + b'}\n'
    with pytest.raises(ValueError, match='deep: line 1:'):
        codec.parse_journal(data, name='deep')


async def test_revision_bounds_retry_convergence(tmp_path):
    bad = '1-docs.jsonl.gz'
    good = '2-docs.jsonl.gz'
    peer, storage = await backend(tmp_path, {bad: journal_blob([meta_doc('bad.md', '9223372036854775808-a')]), good: journal_blob([meta_doc('good.md', '1-a')])})
    try:
        for _ in range(2):
            with pytest.raises(StorageError, match='1-docs.jsonl.gz.*ValueError'):
                await peer.refresh(force=True)
            assert not peer._store.processed(bad)
        storage._objects.pop(bad)
        await peer.refresh(force=True)
        assert peer._store.processed(good)
    finally:
        await peer.close()


def test_revision_upper_boundary():
    assert parse_rev('9223372036854775807-a')[0] == 2**63-1
    with pytest.raises(ValueError):
        parse_rev('9223372036854775808-a')


@pytest.mark.skipif(os.name != 'posix', reason='POSIX permissions')
def test_private_state(tmp_path):
    root = tmp_path / 'state'
    root.mkdir(mode=0o755)
    store = JournalStore(root, PATH_IDENTITY_LEGACY)
    try:
        store.apply_journal('a', {'h:abcd': 'private'}, [])
        for path in [root, *root.rglob('*')]:
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    finally:
        store.close()


async def test_failed_refresh_throttled(tmp_path):
    peer, storage = await backend(tmp_path, {'1-docs.jsonl.gz': b'bad'})
    try:
        with pytest.raises(StorageError):
            await peer.refresh()
        calls = len(storage.list_calls)
        with pytest.raises(StorageError, match="refresh failed previously"):
            await peer.refresh()
        assert len(storage.list_calls) == calls
        with pytest.raises(StorageError):
            await peer.refresh(force=True)
    finally:
        await peer.close()


async def test_key_budget_continues(tmp_path, caplog):
    objects = {f'{i}-docs.jsonl.gz': journal_blob([meta_doc(f'{i}.md', '1-a')]) for i in range(5)}
    peer, _ = await backend(tmp_path, objects, max_refresh_keys=2)
    try:
        for _ in range(5):
            await peer.refresh(force=True)
        assert peer._store.files_processed_count() == 5
        assert 'budget' in caplog.text
    finally:
        await peer.close()


async def test_nonprogress_listing(tmp_path):
    peer, storage = await backend(tmp_path)
    storage.list = lambda start_after='', limit=None: [f'{i:04d}-docs.jsonl.gz' for i in range(1000)]
    try:
        with pytest.raises(StorageError, match='progress'):
            await peer.refresh(force=True)
    finally:
        await peer.close()


async def test_byte_budget_continues(tmp_path, caplog):
    objects = {f'{i}-docs.jsonl.gz': journal_blob([meta_doc(f'{i}.md', '1-a')]) for i in range(3)}
    # Includes the sync-parameters control read on each refresh.
    budget = len(params_blob()) + len(next(iter(objects.values())))
    peer, _ = await backend(tmp_path, objects, max_refresh_bytes=budget)
    try:
        await peer.refresh(force=True)
        assert peer._store.files_processed_count() == 1
        for _ in range(2):
            await peer.refresh(force=True)
        assert peer._store.files_processed_count() == 3
        assert 'budget' in caplog.text
    finally:
        await peer.close()


async def test_atomic_collision(tmp_path, monkeypatch):
    peer, storage = await backend(tmp_path)
    original = storage.put_if_absent
    rival = b'rival object'
    collision = []
    def race(key, data):
        if not collision:
            collision.append(key)
            storage._objects[key] = rival
        return original(key, data)
    storage.put_if_absent = race
    try:
        await peer.write_note('a.md', 'body', create=True)
        assert storage.get(collision[0]) == rival
        assert peer._store.last_sent_key != collision[0]
        assert storage.get(peer._store.last_sent_key) is not None
    finally:
        await peer.close()


async def test_unsupported_fallback(tmp_path):
    peer, storage = await backend(tmp_path)
    storage.conditional_put_supported = False
    try:
        await peer.write_note('a.md', 'body', create=True)
        assert peer._store.last_sent_key in storage.exists_calls
        assert len(storage.put_calls) == 1
    finally:
        await peer.close()


@pytest.mark.parametrize('declared', [True, False])
def test_download_bound(declared):
    storage = object.__new__(Boto3ObjectStorage)
    storage._bucket, storage._prefix = 'test', ''
    storage.max_object_bytes = 16
    body = BytesIO(b'x' * 100)
    storage._client = Mock()
    storage._client.get_object.return_value = {'Body': body, **({'ContentLength': 100} if declared else {})}
    with pytest.raises(StorageError):
        storage.get('hostile')
    assert body.closed


@pytest.mark.parametrize('code,status,result', [('PreconditionFailed',412,'exists'), ('NotImplemented',501,'unsupported'), ('AccessDenied',403,None)])
def test_conditional_s3_errors(code, status, result):
    storage = object.__new__(Boto3ObjectStorage)
    storage._bucket, storage._prefix = 'test', ''
    storage._conditional_warning_logged = False
    storage._client = Mock()
    storage._client.put_object.side_effect = ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'PutObject')
    if result is None:
        with pytest.raises(StorageError):
            storage.put_if_absent('a', b'b')
    else:
        assert storage.put_if_absent('a', b'b') == result
    assert storage._client.put_object.call_args.kwargs['IfNoneMatch'] == '*'


@pytest.mark.parametrize('kind', ['bomb', 'deep', 'line', 'records', 'download'])
async def test_hostile_file_has_clean_error(tmp_path, kind):
    kwargs = {}
    payload = b'~h:a\x1f' + b'x' * 100_000 + b'\n'
    if kind == 'bomb':
        kwargs['max_inflated_bytes'] = 1024
    elif kind == 'line':
        kwargs['max_line_bytes'] = 64
    elif kind == 'records':
        kwargs['max_records'] = 1
        payload = b'~h:a\x1fx\n' * 2
    elif kind == 'deep':
        payload = b'{"_id":"a","_rev":"1-a","nested":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}\n'
    name = '1-docs.jsonl.gz'
    peer, storage = await backend(tmp_path, {name: codec.deflate_journal(payload)}, **kwargs)
    if kind == 'download':
        storage.max_object_bytes = len(params_blob()) + 1
        storage._objects[name] = b'x' * 200
    try:
        with pytest.raises(StorageError) as caught:
            await peer.refresh(force=True)
        assert str(caught.value) == f"journal file {name!r} could not be replayed: " + ('ObjectSizeLimit' if kind == 'download' else 'ValueError')
        assert not peer._store.processed(name)
    finally:
        await peer.close()


async def test_time_budget_preserves_progress(tmp_path, monkeypatch, caplog):
    import storage_journal
    clock = [0.0]
    monkeypatch.setattr(storage_journal.time, 'monotonic', lambda: clock[0])
    peer, storage = await backend(tmp_path, {f'{i}-docs.jsonl.gz': journal_blob([meta_doc(f'{i}.md', '1-a')]) for i in range(3)}, max_refresh_seconds=1)
    apply = peer._store.apply_journal
    def slow_apply(*args):
        apply(*args)
        clock[0] += 2
    peer._store.apply_journal = slow_apply
    try:
        await peer.refresh(force=True)
        assert peer._store.files_processed_count() == 1
        for _ in range(2):
            await peer.refresh(force=True)
        assert peer._store.files_processed_count() == 3
        assert 'wall-clock' in caplog.text
    finally:
        await peer.close()


def test_recursion_error_normalized(monkeypatch):
    def recursion(*args, **kwargs):
        raise RecursionError('decoder depth')
    monkeypatch.setattr(codec.json, 'loads', recursion)
    with pytest.raises(ValueError, match='nested: line 1:'):
        codec.parse_journal(b'{"_id":"a","_rev":"1-a"}\n', name='nested')


def test_json_depth_guard_ignores_quoted_brackets():
    doc = {'_id': 'a', '_rev': '1-a', 'text': '[{"\\' * 1000}
    assert codec.parse_journal(codec.serialize_journal([], [doc])).docs == [doc]


def test_conditional_success_and_fake_atomicity():
    storage = object.__new__(Boto3ObjectStorage)
    storage._bucket, storage._prefix = 'test', 'prefix/'
    storage._client = Mock()
    assert storage.put_if_absent('a', b'b') == 'created'
    assert storage._client.put_object.call_args.kwargs == {'Bucket': 'test', 'Key': 'prefix/a', 'Body': b'b', 'IfNoneMatch': '*'}
    fake = FakeObjectStorage()
    assert fake.put_if_absent('a', b'first') == 'created'
    assert fake.put_if_absent('a', b'second') == 'exists'
    assert fake.get('a') == b'first'


def test_repeated_s3_continuation_token():
    storage = object.__new__(Boto3ObjectStorage)
    storage._bucket, storage._prefix = 'test', ''
    storage._client = Mock()
    storage._client.list_objects_v2.return_value = {'Contents': [], 'IsTruncated': True, 'NextContinuationToken': 'same'}
    with pytest.raises(StorageError, match='progress'):
        storage.list(limit=1)
    assert storage._client.list_objects_v2.call_count == 2
