"""Binary upload contracts shared by both storage implementations and MCP."""
import base64
import hashlib
import json

import pytest

import server
from journal_codec import MAX_LINE_BYTES, parse_journal, serialize_journal
from livesync_format import ForbiddenPathError
from storage_base import ConcurrentModification, NoteExists, NoteNotFound
from storage_journal import JournalBackend
from tests.fake_backend import FakeBackend
from tests.test_storage_couchdb import fake_backend
from tests.test_storage_journal import make_backend, healthy_bucket

DEVICE_LIMIT_BYTES = 1024 * 1024
LARGE_FILE_BYTES = 40 * 1024 * 1024
DATA = b"%PDF-1.7\n\x00\xff\x80scan\r\n"


@pytest.fixture(params=["couchdb", "journal"])
async def backend(request, tmp_path):
    if request.param == "couchdb":
        async with fake_backend() as (backend, log):
            yield backend
    else:
        backend, _ = make_backend(tmp_path, healthy_bucket())
        await backend.startup_check()
        try:
            yield backend
        finally:
            await backend.close()


async def test_attachment_roundtrip_and_write_semantics(backend):
    with pytest.raises(NoteNotFound):
        await backend.write_attachment("scan.pdf", DATA)
    meta = await backend.write_attachment("scan.pdf", DATA, create=True)
    assert meta["size"] == len(DATA)
    assert meta["type"] == "newnote"
    note = await backend.read_note("scan.pdf")
    assert note["is_text"] is False
    assert note["data"] == DATA
    assert note["rev"] == meta["rev"]
    with pytest.raises(NoteExists):
        await backend.write_attachment("scan.pdf", b"new", create=True)
    updated = await backend.write_attachment("scan.pdf", b"new", create=True, overwrite=True)
    assert updated["ctime"] == meta["ctime"]
    with pytest.raises(ConcurrentModification):
        await backend.write_attachment("scan.pdf", b"stale", expected_rev=meta["rev"])
    assert (await backend.read_note("scan.pdf"))["data"] == b"new"
    await backend.delete_note("scan.pdf")
    with pytest.raises(NoteNotFound):
        await backend.write_attachment("scan.pdf", DATA)
    await backend.write_attachment("scan.pdf", DATA, create=True)
    assert (await backend.read_note("scan.pdf"))["data"] == DATA


async def test_attachment_forbidden_before_text_or_size(backend):
    backend.sync_max_size_mb = 1
    with pytest.raises(ForbiddenPathError):
        await backend.write_attachment(".hidden.md", b"x" * (DEVICE_LIMIT_BYTES + 1), create=True)


async def test_attachment_obeys_device_limit(backend):
    from storage_base import WriteSkipped
    backend.sync_max_size_mb = 1
    with pytest.raises(WriteSkipped, match=r"syncMaxSizeInMB.*1"):
        await backend.write_attachment("scan.pdf", b"x" * (DEVICE_LIMIT_BYTES + 1), create=True)
    assert await backend.read_note("scan.pdf") is None


async def test_journal_attachment_two_peer_replay(tmp_path):
    writer, storage = make_backend(tmp_path / "writer", healthy_bucket())
    reader = JournalBackend(storage, tmp_path / "reader")
    try:
        await writer.startup_check()
        await writer.write_attachment("scans/scan.pdf", DATA, create=True)
        await reader.startup_check()
        await reader.refresh(force=True)
        note = await reader.read_note("scans/scan.pdf")
        assert note["data"] == DATA
        assert note["is_text"] is False
        assert note["meta"]["size"] == len(DATA)
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.parametrize('encoding', ['base64', 'full-json-escaped-text'])
def test_v3_piece_fits_encoded_journal_line_allowance(encoding):
    if encoding == 'base64':
        encoded = base64.b64encode(b"x" * (4 * 1024 * 1024)).decode("ascii")
        payload = serialize_journal([("h:0123456789abcdef", encoded)], [])
        assert parse_journal(payload).chunks["h:0123456789abcdef"] == encoded
    else:
        # UTF-8 boundary headroom and worst-case JSON escaping (6x per byte).
        encoded = "\x00" * (4 * 1024 * 1024 + 3)
        doc = {"_id": "h:0123456789abcdef", "_rev": "1-a", "data": encoded}
        payload = serialize_journal([], [doc])
        assert parse_journal(payload).docs[0]['data'] == encoded
    assert max(map(len, payload.splitlines())) < 32 * 1024 * 1024 < MAX_LINE_BYTES


@pytest.mark.parametrize("content", ["YQ", "!!!!", "YQ===", "é"])
async def test_tool_invalid_base64(monkeypatch, content):
    fake = FakeBackend()
    monkeypatch.setattr(server, "backend", fake)
    result = await server.obsidian_upload_attachment("scan.pdf", content)
    assert "base64" in result.lower() and "error" in result.lower()
    assert not fake.notes


async def test_tool_upload_response_and_fetch(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(server, "backend", fake)
    result = json.loads(await server.obsidian_upload_attachment("scan.pdf", base64.b64encode(DATA).decode()))
    expected = {"path": "scan.pdf", "size": len(DATA), "sha256": hashlib.sha256(DATA).hexdigest()}
    assert result == expected
    assert fake.notes["scan.pdf"]["data"] == DATA
    fetched = json.loads(await server.obsidian_fetch("scan.pdf"))
    assert all(fetched[k] == v for k, v in expected.items())
    assert "content" not in fetched and "data" not in fetched


async def test_journal_verifies_original_bytes_before_local_apply(tmp_path, monkeypatch):
    from storage_base import StorageError
    backend, _ = make_backend(tmp_path, healthy_bucket())
    try:
        await backend.startup_check()
        monkeypatch.setattr(backend._splitter, "split", lambda data, **kwargs: ["d3Jvbmc="])
        with pytest.raises(StorageError, match="outcome unknown"):
            await backend.write_attachment("scan.pdf", DATA, create=True)
        assert await backend.read_note("scan.pdf") is None
    finally:
        await backend.close()


async def test_tool_has_no_fixed_size_guard(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(server, "backend", fake)
    result = json.loads(await server.obsidian_upload_attachment(
        "scan.pdf", base64.b64encode(b"x" * (LARGE_FILE_BYTES + 1)).decode()))
    assert result['size'] == LARGE_FILE_BYTES + 1
    assert len(fake.notes['scan.pdf']['data']) == LARGE_FILE_BYTES + 1


async def test_tool_overwrite(monkeypatch):
    fake = FakeBackend()
    fake.add("scan.pdf", data=DATA)
    monkeypatch.setattr(server, "backend", fake)
    assert "error" in (await server.obsidian_upload_attachment("scan.pdf", "eA==")).lower()
    result = json.loads(await server.obsidian_upload_attachment("scan.pdf", "eA==", overwrite=True))
    assert result["size"] == 1
    assert fake.notes["scan.pdf"]["data"] == b"x"


async def test_couchdb_stale_attachment_writes_nothing():
    async with fake_backend() as (backend, log):
        meta = await backend.write_attachment("scan.pdf", DATA, create=True)
        await backend.write_attachment("scan.pdf", b"new", expected_rev=meta["rev"])
        log.clear()
        with pytest.raises(ConcurrentModification):
            await backend.write_attachment("scan.pdf", b"stale", expected_rev=meta["rev"])
        assert all(req.method == "GET" for req in log)


async def test_journal_stale_attachment_writes_nothing(tmp_path):
    backend, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await backend.startup_check()
        meta = await backend.write_attachment("scan.pdf", DATA, create=True)
        await backend.write_attachment("scan.pdf", b"new", expected_rev=meta["rev"])
        storage.put_calls.clear()
        with pytest.raises(ConcurrentModification):
            await backend.write_attachment("scan.pdf", b"stale", expected_rev=meta["rev"])
        assert storage.put_calls == []
        assert (await backend.read_note("scan.pdf"))["data"] == b"new"
    finally:
        await backend.close()


@pytest.mark.parametrize("encrypted", [False, True])
async def test_couchdb_attachment_refuses_corrupt_shared_chunk(encrypted):
    from livesync_format import chunk_id_for
    from storage_base import StorageError
    encoded = base64.b64encode(DATA).decode()
    chunk_id = chunk_id_for(encoded)
    docs = {chunk_id: {"_id": chunk_id, "type": "leaf", "data": encoded if encrypted else "corrupt", "e_": encrypted}}
    async with fake_backend(docs) as (backend, log):
        with pytest.raises(StorageError):
            await backend.write_attachment("scan.pdf", DATA, create=True)
        assert all(req.method == "GET" for req in log)


@pytest.mark.parametrize("target", ["chunk", "metadata"])
async def test_couchdb_attachment_requires_201(target):
    from livesync_format import chunk_id_for
    from storage_base import StorageUnavailable
    chunk_id = chunk_id_for(base64.b64encode(DATA).decode())
    async with fake_backend(put_202_ids=(chunk_id if target == "chunk" else "scan.pdf",)) as (backend, _):
        with pytest.raises(StorageUnavailable, match="outcome unknown"):
            await backend.write_attachment("scan.pdf", DATA, create=True)
        assert await backend.read_note("scan.pdf") is None


async def test_tool_upload_uses_plain_disk_path(monkeypatch, backend):
    monkeypatch.setattr(server, "backend", backend)
    result = json.loads(await server.obsidian_upload_attachment("note.md", "eA=="))
    assert result["size"] == 1
    assert json.loads(await server.obsidian_fetch("note.md"))["content"] == "x"
