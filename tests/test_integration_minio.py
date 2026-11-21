"""Controller-run MinIO integration; no credentials means no network I/O."""

import asyncio
import unicodedata
from types import SimpleNamespace
from uuid import uuid4

import boto3
from botocore.config import Config
import pytest

import storage_journal

from config import S3Config
from journal_store import pick_winner
from livesync_format import path2id
from object_storage import Boto3ObjectStorage
from storage_base import StorageError, StorageUnavailable
from storage_journal import (
    JournalBackend, MILESTONE_KEY, SYNC_PARAMETERS_KEY, journal_sort_key,
)
from tests.test_object_storage import MINIO_CONFIGURED, MINIO_ENV
from tests.test_storage_journal import milestone_blob, params_blob

requires_minio = pytest.mark.skipif(
    not MINIO_CONFIGURED,
    reason="MinIO integration requires MINIO_TEST_ENDPOINT, MINIO_TEST_KEY, "
    "MINIO_TEST_SECRET, MINIO_TEST_BUCKET (legacy credential aliases accepted)",
)


class StorageSpy:
    """Record protocol calls and inject a replay failure before a real GET."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.calls = []
        self.fail_key = None

    def get(self, key, *, max_bytes=None):
        self.calls.append(("get", key))
        if key == self.fail_key:
            raise StorageUnavailable("injected replay interruption")
        return self.adapter.get(key, max_bytes=max_bytes)

    def put(self, key, data):
        existed = self.adapter.exists(key)
        result = self.adapter.put(key, data)
        self.calls.append(("put", key, "overwritten" if existed else "created"))
        return result

    def put_if_absent(self, key, data):
        result = self.adapter.put_if_absent(key, data)
        self.calls.append(("put_if_absent", key, result))
        return result

    def exists(self, key):
        self.calls.append(("exists", key))
        return self.adapter.exists(key)

    def list(self, start_after="", limit=None):
        self.calls.append(("list", start_after))
        return self.adapter.list(start_after=start_after, limit=limit)

    def delete(self, *args, **kwargs):
        self.calls.append(("delete", None))
        raise AssertionError("backend must never delete remote objects")


def published_keys(*spies):
    """Count successful creations, rejecting duplicate keys and plain overwrites."""
    publications = [
        call for spy in spies for call in spy.calls
        if call[0] in ("put_if_absent", "put")
    ]
    assert all(result == "created" for op, _, result in publications if op == "put")
    assert all(
        result in ("created", "exists", "unsupported")
        for op, _, result in publications if op == "put_if_absent"
    )
    keys = [key for _, key, result in publications if result == "created"]
    assert len(keys) == len(set(keys)), "journal key was created more than once"
    return keys


@pytest.fixture
async def minio(tmp_path):
    cfg = S3Config(
        endpoint=MINIO_ENV["MINIO_TEST_ENDPOINT"],
        access_key=MINIO_ENV["MINIO_TEST_KEY"],
        secret_key=MINIO_ENV["MINIO_TEST_SECRET"],
        bucket=MINIO_ENV["MINIO_TEST_BUCKET"],
        region="us-east-1", prefix=f"it/{uuid4().hex}/", uri="",
    )
    # This separate harness client alone may delete; backend clients cannot.
    admin = boto3.client(
        "s3", endpoint_url=cfg.endpoint, aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key, region_name=cfg.region,
        config=Config(s3={"addressing_style": "path"},
                      connect_timeout=5, read_timeout=30,
                      retries={"max_attempts": 0}),
    )
    peers = []
    delete_attempts = []

    def forbid_delete(model, **kwargs):
        if model.name.lower().startswith("delete"):
            delete_attempts.append(model.name)
            raise AssertionError("backend client attempted remote deletion")

    def keys():
        return [
            item["Key"]
            for page in admin.get_paginator("list_objects_v2").paginate(
                Bucket=cfg.bucket, Prefix=cfg.prefix)
            for item in page.get("Contents", [])
        ]

    def wipe():
        for key in keys():
            admin.delete_object(Bucket=cfg.bucket, Key=key)
        assert keys() == []

    def seed(protocol=2):
        for key, body in (
            (MILESTONE_KEY, milestone_blob(path_version=1)),
            (SYNC_PARAMETERS_KEY, params_blob(protocol=protocol)),
        ):
            admin.put_object(Bucket=cfg.bucket, Key=cfg.prefix + key, Body=body)

    async def peer(name):
        adapter = Boto3ObjectStorage(cfg)
        adapter._client.meta.events.register("before-call.s3.*", forbid_delete)
        spy = StorageSpy(adapter)
        backend = JournalBackend(spy, tmp_path / name)
        peers.append((backend, spy))
        await backend.startup_check()
        return backend, spy

    try:
        seed()
        yield peer, keys, wipe, seed, cfg.prefix
    finally:
        try:
            for backend, spy in peers:
                await backend.close()
                spy.adapter._client.close()
            wipe()
        finally:
            admin.close()
        assert delete_attempts == []
        assert all(call[0] != "delete" for _, spy in peers for call in spy.calls)


@requires_minio
async def test_roundtrip(minio):
    peer, _, _, _, _ = minio
    writer, _ = await peer("writer")
    notes = {
        "root.md": "---\ntitle: Roundtrip\ntags: [minio]\n---\nBody\n",
        "folder-a/one.md": "First folder\n",
        "folder-a/два.md": "Кириллица в тексте\n",
        "папка/йога.md": "Имя в NFC\n",
        "папка/five.md": "Пятая заметка\n",
    }
    assert all(unicodedata.is_normalized("NFC", path) for path in notes)
    for path, text in notes.items():
        await writer.write_note(path, text, create=True)
    reader, _ = await peer("reader")
    await reader.refresh(force=True)
    assert {meta["path"] for meta in await reader.list_notes()} == set(notes)
    for path, text in notes.items():
        expected = await writer.read_note(path)
        actual = await reader.read_note(path)
        assert expected is not None and actual is not None
        assert actual["text"] == expected["text"] == text
        assert actual["data"] == expected["data"]
        assert actual["rev"] == expected["rev"]


@requires_minio
async def test_wipe_epoch(minio):
    peer, _, wipe, seed, _ = minio
    writer, _ = await peer("writer")
    await writer.write_note("old.md", "Old epoch", create=True)
    reader, _ = await peer("reader")
    await reader.refresh(force=True)
    assert await reader.read_note("old.md") is not None
    old_epoch = (await reader.backend_info())["epoch"]
    wipe()
    seed(protocol=3)
    await reader.startup_check()
    info = await reader.backend_info()
    assert info["epoch"] != old_epoch
    assert info["epoch"] == "3:salt-a"
    assert info["files_processed"] == 0
    assert await reader.list_notes() == []
    assert await reader.read_note("old.md") is None
    fresh, _ = await peer("fresh-writer")
    await fresh.write_note("new.md", "New epoch", create=True)
    await reader.refresh(force=True)
    assert (await reader.read_note("new.md"))["text"] == "New epoch"
    assert await reader.read_note("old.md") is None


@requires_minio
async def test_interrupted_replay(minio):
    peer, _, _, _, _ = minio
    writer, writer_spy = await peer("writer")
    for index in range(3):
        await writer.write_note(f"{index}.md", f"Body {index}", create=True)
    journals = sorted(
        published_keys(writer_spy),
        key=journal_sort_key,
    )
    assert len(journals) == 3
    reader, spy = await peer("reader")
    spy.fail_key = journals[1]
    with pytest.raises(StorageError, match="injected replay interruption"):
        await reader.refresh(force=True)
    assert [reader._store.processed(key) for key in journals] == [True, False, False]
    assert (await reader.backend_info())["files_processed"] == 1
    assert (await reader.read_note("0.md"))["text"] == "Body 0"
    assert await reader.read_note("1.md") is None
    assert await reader.read_note("2.md") is None
    spy.fail_key = None
    # Reopen the same local checkpoint as well as retrying the failed GET.
    await reader.close()
    await reader.startup_check()
    await reader.refresh(force=True)
    assert (await reader.backend_info())["files_processed"] == 3
    for index in range(3):
        assert (await reader.read_note(f"{index}.md"))["text"] == f"Body {index}"


@requires_minio
async def test_sibling_writers(minio, monkeypatch):
    peer, keys, _, _, prefix = minio
    first, first_spy = await peer("first")
    second, second_spy = await peer("second")
    # Both peers start before either publishes and allocate at the same ms.
    monkeypatch.setattr(
        storage_journal, "time",
        SimpleNamespace(time=lambda: 1700000000.0,
                        monotonic=storage_journal.time.monotonic),
    )
    revisions = await asyncio.gather(
        first.write_note("shared.md", "First sibling", create=True),
        second.write_note("shared.md", "Second sibling", create=True),
    )
    shared_keys = published_keys(first_spy, second_spy)
    assert len(set(shared_keys)) == 2
    assert len({key.split("-", 1)[0] for key in shared_keys}) == 1
    assert {prefix + key for key in shared_keys} <= set(keys())
    await first.write_note("first-only.md", "First survives", create=True)
    await second.write_note("second-only.md", "Second survives", create=True)
    assert len(published_keys(first_spy, second_spy)) == 4
    reader, _ = await peer("reader")
    await reader.refresh(force=True)
    doc_id = path2id("shared.md", nfc=True)
    leaves = reader._store.leaves(doc_id)
    assert {leaf["_rev"] for leaf in leaves} == {meta["rev"] for meta in revisions}
    assert len(leaves) == 2
    assert reader._store.winner(doc_id) == pick_winner(leaves)
    actual = await reader.read_note("shared.md")
    assert actual["rev"] == pick_winner(leaves)["_rev"]
    bodies = {
        reader._store._chunk_path(leaf["children"][0]).read_text(encoding="utf-8")
        for leaf in leaves
    }
    assert bodies == {"First sibling", "Second sibling"}
    assert actual["text"] in bodies
    assert (await reader.read_note("first-only.md"))["text"] == "First survives"
    assert (await reader.read_note("second-only.md"))["text"] == "Second survives"
    assert (await reader.backend_info())["files_processed"] == 4


def test_storage_spy_protocol_contract_offline():
    from inspect import signature
    from object_storage import JournalObjectStorage, ObjectSizeLimit
    from tests.fake_object_storage import FakeObjectStorage

    adapter = FakeObjectStorage()
    spy = StorageSpy(adapter)
    for name in ("get", "put", "put_if_absent", "list", "exists"):
        expected = signature(getattr(JournalObjectStorage, name)).parameters
        actual = signature(getattr(StorageSpy, name)).parameters
        assert list(actual) == list(expected)
        assert [p.kind for p in actual.values()] == [p.kind for p in expected.values()]
    spy.put("a", b"abc")
    assert spy.get("a", max_bytes=3) == b"abc"
    with pytest.raises(ObjectSizeLimit):
        spy.get("a", max_bytes=2)
    assert spy.put_if_absent("a", b"changed") == "exists"
    assert spy.put_if_absent("b", b"new") == "created"
    assert spy.exists("b")
    assert spy.list(start_after="a", limit=1) == ["b"]
    assert spy.get("a") == b"abc"
    assert ("put_if_absent", "a", "exists") in spy.calls
    assert ("put_if_absent", "b", "created") in spy.calls
    assert published_keys(spy) == ["a", "b"]
    adapter.conditional_put_supported = False
    assert spy.put_if_absent("c", b"fallback") == "unsupported"
    spy.put("c", b"fallback")
    assert published_keys(spy) == ["a", "b", "c"]
    spy.put("a", b"overwrite")
    with pytest.raises(AssertionError):
        published_keys(spy)
    duplicate = StorageSpy(FakeObjectStorage())
    duplicate.put_if_absent("b", b"duplicate")
    other = StorageSpy(FakeObjectStorage())
    other.put_if_absent("b", b"another")
    with pytest.raises(AssertionError, match="created more than once"):
        published_keys(duplicate, other)
