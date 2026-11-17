"""Tests for the CouchDB storage backend read path (storage_couchdb)."""

from __future__ import annotations

import base64
import contextlib
import inspect
import logging
import urllib.parse

import pytest

from storage_base import (
    NoteNotFound,
    StorageError,
    StorageUnavailable,
    UnsupportedVault,
)
from storage_couchdb import CouchDBBackend
from tests.couchdb_fake import make_fake_couchdb

URL = "http://couchdb.test:5984"
USER = "admin"
PASSWORD = "secret-password"
DATABASE = "obsidian"


@contextlib.asynccontextmanager
async def fake_backend(docs=None, **fake_kwargs):
    """CouchDBBackend wired to a fresh fake CouchDB (auto-closed)."""
    transport, request_log = make_fake_couchdb(docs or {}, **fake_kwargs)
    backend = CouchDBBackend(
        URL, USER, PASSWORD, DATABASE, transport=transport
    )
    try:
        yield backend, request_log
    finally:
        await backend.close()


def plain_note(doc_id: str, path: str, children, **extra) -> dict:
    doc = {
        "_id": doc_id,
        "path": path,
        "type": "plain",
        "children": list(children),
        "ctime": 10,
        "mtime": 20,
        "size": 11,
    }
    doc.update(extra)
    return doc


# --- startup_check ---------------------------------------------------------


async def test_startup_check_ok():
    docs = {"note.md": plain_note("note.md", "note.md", [])}
    async with fake_backend(docs) as (backend, _log):
        await backend.startup_check()  # must not raise


async def test_startup_check_db_missing():
    async with fake_backend({}, db_exists=False) as (backend, _log):
        with pytest.raises(StorageUnavailable) as excinfo:
            await backend.startup_check()
    message = str(excinfo.value)
    assert "couchdb.test" not in message
    assert "5984" not in message
    assert "http" not in message.lower()
    assert DATABASE not in message


async def test_startup_detects_e2ee():
    docs = {
        "_local/obsydian_livesync_milestone": {
            "tweak_values": {"encrypt": True},
        }
    }
    async with fake_backend(docs) as (backend, _log):
        with pytest.raises(UnsupportedVault):
            await backend.startup_check()


async def test_startup_detects_path_obfuscation():
    docs = {
        "_local/obsydian_livesync_milestone": {
            "tweak_values": {"usePathObfuscation": True},
        }
    }
    async with fake_backend(docs) as (backend, _log):
        with pytest.raises(UnsupportedVault):
            await backend.startup_check()


async def test_auth_error_maps_to_storage_unavailable():
    async with fake_backend({}, force_status=401) as (backend, _log):
        with pytest.raises(StorageUnavailable, match="authorization"):
            await backend.startup_check()


async def test_server_error_maps_to_storage_unavailable():
    async with fake_backend({}, force_status=500) as (backend, _log):
        with pytest.raises(StorageUnavailable) as excinfo:
            await backend.startup_check()
    assert "unavailable" in str(excinfo.value).lower()
    assert "couchdb.test" not in str(excinfo.value)


# --- list_notes ------------------------------------------------------------

FILTER_DOCS = {
    "h:0001": {"_id": "h:0001", "data": "chunk-body"},
    "i:machine": {"_id": "i:machine", "type": "plain", "path": "machine"},
    "ix:settings.md": {
        "_id": "ix:settings.md",
        "type": "plain",
        "path": "settings.md",
    },
    "ps:plugin": {"_id": "ps:plugin", "type": "plain", "path": "plugin"},
    "_design/views": {"_id": "_design/views", "language": "javascript"},
    "syncinfo": {"_id": "syncinfo", "node_info": {}},
    "obsydian_livesync_version": {"_id": "obsydian_livesync_version"},
    "note.md": plain_note("note.md", "note.md", ["h:0001"]),
    "img.png": {
        "_id": "img.png",
        "path": "img.png",
        "type": "newnote",
        "children": [],
        "ctime": 1,
        "mtime": 2,
        "size": 9,
    },
}


async def test_list_notes_filters():
    async with fake_backend(dict(FILTER_DOCS)) as (backend, _log):
        metas = await backend.list_notes()
    assert sorted(meta["path"] for meta in metas) == ["img.png", "note.md"]
    by_path = {meta["path"]: meta for meta in metas}
    note = by_path["note.md"]
    assert note["doc_id"] == "note.md"
    assert note["type"] == "plain"
    assert note["size"] == 11
    assert note["ctime"] == 10
    assert note["mtime"] == 20


async def test_list_notes_folder_and_extension():
    docs = {
        "inbox/a.md": plain_note("inbox/a.md", "inbox/a.md", []),
        "inbox/sub/b.md": plain_note("inbox/sub/b.md", "inbox/sub/b.md", []),
        "root.md": plain_note("root.md", "root.md", []),
        "todo.txt": plain_note("todo.txt", "todo.txt", []),
    }
    async with fake_backend(docs) as (backend, _log):
        everything = await backend.list_notes()
        inbox = await backend.list_notes(folder="inbox")
        inbox_flat = await backend.list_notes(folder="inbox", recursive=False)
        markdown = await backend.list_notes(extension=".md")
        markdown_short = await backend.list_notes(extension="md")
    assert [m["path"] for m in everything] == [
        "inbox/a.md",
        "inbox/sub/b.md",
        "root.md",
        "todo.txt",
    ]
    assert [m["path"] for m in inbox] == ["inbox/a.md", "inbox/sub/b.md"]
    assert [m["path"] for m in inbox_flat] == ["inbox/a.md"]
    assert [m["path"] for m in markdown] == [
        "inbox/a.md",
        "inbox/sub/b.md",
        "root.md",
    ]
    assert [m["path"] for m in markdown_short] == [
        "inbox/a.md",
        "inbox/sub/b.md",
        "root.md",
    ]


async def test_list_notes_does_not_fetch_chunk_bodies():
    async with fake_backend(dict(FILTER_DOCS)) as (backend, log):
        await backend.list_notes()
    assert log, "expected requests to be recorded"
    for request in log:
        path = urllib.parse.unquote(str(request.url.path))
        assert "h:0001" not in path
        assert b"h:0001" not in (request.content or b"")
        if path.endswith("/_all_docs") and request.method == "GET":
            assert request.url.params.get("include_docs") != "true"


# --- read_note -------------------------------------------------------------


async def test_read_note_plain_chunks():
    docs = {
        "note.md": plain_note("note.md", "note.md", ["h:aaa", "h:bbb"]),
        "h:aaa": {"_id": "h:aaa", "data": "hello "},
        "h:bbb": {"_id": "h:bbb", "data": "world"},
    }
    async with fake_backend(docs) as (backend, _log):
        content = await backend.read_note("note.md")
    assert content is not None
    assert content["text"] == "hello world"
    assert content["is_text"] is True
    assert content["data"] is None
    assert content["meta"]["path"] == "note.md"
    assert content["meta"]["type"] == "plain"


async def test_read_note_newnote_base64():
    part1 = base64.b64encode(b"\x89PNG\r\n").decode("ascii")
    part2 = base64.b64encode(b"image-bytes").decode("ascii")
    docs = {
        "img.png": {
            "_id": "img.png",
            "path": "img.png",
            "type": "newnote",
            "children": ["h:p1", "h:p2"],
            "ctime": 1,
            "mtime": 2,
            "size": 19,
        },
        "h:p1": {"_id": "h:p1", "data": part1},
        "h:p2": {"_id": "h:p2", "data": part2},
    }
    async with fake_backend(docs) as (backend, _log):
        content = await backend.read_note("img.png")
    assert content is not None
    assert content["data"] == b"\x89PNG\r\nimage-bytes"
    assert content["is_text"] is False
    assert content["text"] is None
    assert content["meta"]["type"] == "newnote"


async def test_read_note_deleted_is_none():
    docs = {
        "note.md": plain_note(
            "note.md", "note.md", ["h:aaa"], deleted=True
        ),
    }
    async with fake_backend(docs) as (backend, _log):
        assert await backend.read_note("note.md") is None
        with pytest.raises(NoteNotFound):
            await backend.get_meta("note.md")


async def test_read_legacy_notes_type():
    docs = {
        "old.md": {"_id": "old.md", "path": "old.md", "type": "notes",
                   "data": "legacy body"}
    }
    async with fake_backend(docs) as (backend, _log):
        content = await backend.read_note("old.md")
    assert content is not None
    assert content["text"] == "legacy body"
    assert content["is_text"] is True
    assert content["meta"]["size"] == len("legacy body")


async def test_read_note_missing_returns_none():
    async with fake_backend({}) as (backend, _log):
        assert await backend.read_note("ghost.md") is None


async def test_read_missing_chunk_raises_storageerror():
    docs = {"note.md": plain_note("note.md", "note.md", ["h:gone"])}
    async with fake_backend(docs) as (backend, _log):
        with pytest.raises(StorageError) as excinfo:
            await backend.read_note("note.md")
    message = str(excinfo.value)
    assert "note.md" in message
    assert "h:gone" not in message


async def test_read_encrypted_chunk_unsupported():
    docs = {
        "sec.md": plain_note("sec.md", "sec.md", ["h:enc"]),
        "h:enc": {"_id": "h:enc", "data": "%ciphertext", "e_": True},
    }
    async with fake_backend(docs) as (backend, _log):
        with pytest.raises(UnsupportedVault):
            await backend.read_note("sec.md")


# --- get_meta --------------------------------------------------------------


async def test_get_meta_ok_and_missing():
    docs = {"note.md": plain_note("note.md", "note.md", [])}
    async with fake_backend(docs) as (backend, _log):
        meta = await backend.get_meta("note.md")
        with pytest.raises(NoteNotFound):
            await backend.get_meta("nope.md")
    assert meta["path"] == "note.md"
    assert meta["size"] == 11


# --- iterate_texts ---------------------------------------------------------


def test_iterate_texts_signature_is_pinned():
    parameters = inspect.signature(CouchDBBackend.iterate_texts).parameters
    assert list(parameters) == ["self", "extension"]
    assert parameters["extension"].default == ".md"


async def test_iterate_texts_skips_corrupt_notes(caplog):
    docs = {
        "good.md": plain_note("good.md", "good.md", ["h:ok"]),
        "h:ok": {"_id": "h:ok", "data": "hello"},
        "bad.md": plain_note("bad.md", "bad.md", ["h:gone"]),
        "data.txt": plain_note("data.txt", "data.txt", ["h:ok"]),
    }
    async with fake_backend(docs) as (backend, _log):
        with caplog.at_level(logging.ERROR, logger="storage_couchdb"):
            results = {}
            async for meta, text in backend.iterate_texts():
                results[meta["path"]] = text
    # extension filter: data.txt is excluded from the ".md" iteration
    assert sorted(results) == ["bad.md", "good.md"]
    assert results["good.md"] == "hello"
    assert results["bad.md"] is None
    assert any("bad.md" in record.message for record in caplog.records)


async def test_iterate_texts_custom_extension_includes_text_types():
    docs = {
        "data.txt": plain_note("data.txt", "data.txt", ["h:ok"]),
        "h:ok": {"_id": "h:ok", "data": "plain text"},
        "note.md": plain_note("note.md", "note.md", ["h:ok"]),
    }
    async with fake_backend(docs) as (backend, _log):
        results = {
            meta["path"]: text
            async for meta, text in backend.iterate_texts(".txt")
        }
    assert results == {"data.txt": "plain text"}


# --- write stubs (Task 6) --------------------------------------------------


async def test_write_methods_are_not_implemented_yet():
    async with fake_backend({}) as (backend, _log):
        with pytest.raises(NotImplementedError):
            await backend.write_note("x.md", "body")
        with pytest.raises(NotImplementedError):
            await backend.delete_note("x.md")
        with pytest.raises(NotImplementedError):
            await backend.rename_note("x.md", "y.md")
        with pytest.raises(NotImplementedError):
            await backend.copy_note("x.md", "y.md")
