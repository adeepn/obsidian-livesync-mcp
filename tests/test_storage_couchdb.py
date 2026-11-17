"""Tests for the CouchDB storage backend (storage_couchdb)."""

from __future__ import annotations

import base64
import contextlib
import inspect
import logging
import urllib.parse

import httpx
import pytest

from livesync_format import chunk_id_for
from storage_base import (
    BinaryNoteError,
    ConcurrentModification,
    NoteExists,
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


@contextlib.asynccontextmanager
async def fake_vault(docs=None, **fake_kwargs):
    """Backend plus a raw client on the same fake, for store inspection.

    Write tests must observe the fake's stored documents (revisions,
    soft-delete flags, surviving chunks), not just the backend's API.
    """
    transport, request_log = make_fake_couchdb(docs or {}, **fake_kwargs)
    backend = CouchDBBackend(
        URL, USER, PASSWORD, DATABASE, transport=transport
    )
    raw = httpx.AsyncClient(
        transport=transport, base_url=URL + "/", auth=(USER, PASSWORD)
    )
    try:
        yield backend, raw, request_log
    finally:
        await raw.aclose()
        await backend.close()


async def stored_doc(raw: httpx.AsyncClient, doc_id: str) -> dict:
    """Fetch a document straight from the fake (asserting it exists)."""
    response = await raw.get(
        f"{DATABASE}/{urllib.parse.quote(doc_id, safe='')}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def mutation_free(log, up_to: int) -> bool:
    """No PUT/POST/DELETE among the first ``up_to`` logged requests."""
    return all(
        request.method not in ("PUT", "POST", "DELETE")
        for request in log[:up_to]
    )


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


async def test_list_notes_root_nonrecursive():
    docs = {
        "root.md": plain_note("root.md", "root.md", []),
        "sub/nested.md": plain_note("sub/nested.md", "sub/nested.md", []),
        "sub/deep/leaf.md": plain_note(
            "sub/deep/leaf.md", "sub/deep/leaf.md", []
        ),
    }
    async with fake_backend(docs) as (backend, _log):
        top = await backend.list_notes(recursive=False)
        everything = await backend.list_notes(recursive=True)
    assert [m["path"] for m in top] == ["root.md"]
    assert [m["path"] for m in everything] == [
        "root.md",
        "sub/deep/leaf.md",
        "sub/nested.md",
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


async def test_read_note_on_missing_db_raises():
    # A vanished database must not look like an absent note (reason
    # "no_db_file" vs "missing" in the 404 body).
    async with fake_backend({}, db_exists=False) as (backend, _log):
        with pytest.raises(StorageUnavailable, match="database"):
            await backend.read_note("note.md")
        with pytest.raises(StorageUnavailable, match="database"):
            await backend.get_meta("note.md")


async def test_all_docs_on_missing_db_raises():
    # Database disappears between the metadata GET and the chunk fetch.
    docs = {
        "note.md": plain_note("note.md", "note.md", ["h:aaa"]),
        "h:aaa": {"_id": "h:aaa", "data": "x"},
    }
    async with fake_backend(docs, db_dies_after=1) as (backend, _log):
        with pytest.raises(StorageUnavailable, match="database"):
            await backend.read_note("note.md")

    # Same for the _all_docs id scan behind list_notes.
    async with fake_backend(dict(docs), db_dies_after=1) as (backend, _log):
        with pytest.raises(StorageUnavailable, match="database"):
            await backend.list_notes()


async def test_startup_check_db_gone_before_milestone_raises():
    async with fake_backend({}, db_dies_after=1) as (backend, _log):
        with pytest.raises(StorageUnavailable, match="database"):
            await backend.startup_check()


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


# --- write path (Task 6) ---------------------------------------------------


def test_write_signatures_are_pinned():
    write = inspect.signature(CouchDBBackend.write_note)
    assert list(write.parameters) == [
        "self", "path", "content", "create", "overwrite", "mtime",
        "expected_rev",
    ]
    assert write.parameters["create"].default is False
    assert write.parameters["overwrite"].default is False
    assert write.parameters["mtime"].default is None
    assert write.parameters["expected_rev"].default is None
    delete = inspect.signature(CouchDBBackend.delete_note).parameters
    assert list(delete) == ["self", "path", "expected_rev"]
    assert delete["expected_rev"].default is None
    for method in (CouchDBBackend.rename_note, CouchDBBackend.copy_note):
        parameters = inspect.signature(method).parameters
        assert list(parameters) == ["self", "path", "new_path", "overwrite"]
        assert parameters["overwrite"].default is False


async def test_write_never_deletes_chunks():
    # B1/C1: rewriting a chunked note PUTs the new chunk before the
    # metadata document and never issues a single DELETE.
    docs = {
        "note.md": plain_note("note.md", "note.md", ["h:old1", "h:old2"]),
        "h:old1": {"data": "old1", "type": "leaf"},
        "h:old2": {"data": "old2", "type": "leaf"},
    }
    async with fake_vault(docs) as (backend, raw, log):
        await backend.write_note("note.md", "brand new body", overwrite=True)
        ops_end = len(log)
        old1 = await stored_doc(raw, "h:old1")
        old2 = await stored_doc(raw, "h:old2")
        content = await backend.read_note("note.md")
    assert not any(r.method == "DELETE" for r in log[:ops_end])
    put_paths = [
        urllib.parse.unquote(str(r.url.path))
        for r in log[:ops_end]
        if r.method == "PUT"
    ]
    chunk_path = f"/{DATABASE}/{chunk_id_for('brand new body')}"
    assert put_paths.index(chunk_path) < put_paths.index(f"/{DATABASE}/note.md")
    # old chunks survive the rewrite untouched (B1)
    assert old1["data"] == "old1"
    assert old2["data"] == "old2"
    assert content["text"] == "brand new body"


async def test_write_409_concurrent():
    # A racing writer wins the metadata revision between our GET and
    # PUT: ConcurrentModification, metadata untouched, chunk orphans
    # acceptable (C1/C2).
    docs = {
        "note.md": plain_note("note.md", "note.md", ["h:old"]),
        "h:old": {"data": "old", "type": "leaf"},
    }
    async with fake_vault(docs, put_conflict_ids=("note.md",)) as (
        backend, raw, log,
    ):
        with pytest.raises(ConcurrentModification, match="concurrent") as excinfo:
            await backend.write_note("note.md", "loser body", overwrite=True)
        ops_end = len(log)
        stored = await stored_doc(raw, "note.md")
        orphan = await stored_doc(raw, chunk_id_for("loser body"))
    assert stored["children"] == ["h:old"]
    assert stored["size"] == 11
    assert stored["_rev"] == "1-fake1"  # metadata revision untouched
    assert orphan["data"] == "loser body"  # orphaned chunk tolerated
    assert not any(r.method == "DELETE" for r in log[:ops_end])
    message = str(excinfo.value)
    assert "http" not in message.lower()  # C7: no URL leak
    assert "fake1" not in message  # C7: no revision leak


async def test_write_conflicts_refused():
    # C3: a note with unresolved conflict revisions is never written.
    # The fake serves _conflicts only when asked (like CouchDB), so the
    # refusal also proves the backend requests conflicts=true on the
    # metadata GET — without the parameter the write would proceed.
    docs = {
        "note.md": plain_note(
            "note.md", "note.md", [], _conflicts=["2-other"], _rev="2-fake2"
        ),
    }
    async with fake_backend(docs) as (backend, log):
        with pytest.raises(ConcurrentModification, match="conflict"):
            await backend.write_note("note.md", "x", overwrite=True)
        metadata_gets = [
            r
            for r in log
            if r.method == "GET"
            and urllib.parse.unquote(str(r.url.path)) == f"/{DATABASE}/note.md"
        ]
    assert metadata_gets
    assert all(
        r.url.params.get("conflicts") == "true" for r in metadata_gets
    )
    assert mutation_free(log, len(log))


async def test_create_existing_refused():
    # D2: create without overwrite on a live note is an error.
    docs = {"a.md": plain_note("a.md", "a.md", [])}
    async with fake_backend(docs) as (backend, log):
        with pytest.raises(NoteExists):
            await backend.write_note("a.md", "x", create=True)
    assert mutation_free(log, len(log))


async def test_overwrite_missing_refused():
    # D2: writing a missing note without create is an error.
    async with fake_backend({}) as (backend, log):
        with pytest.raises(NoteNotFound):
            await backend.write_note("ghost.md", "x", overwrite=True)
    assert mutation_free(log, len(log))


async def test_write_requires_text():
    # Spec §5: binary paths cannot receive textual writes, and the
    # refusal happens before any HTTP request (spec §7.5).
    async with fake_backend({}) as (backend, log):
        with pytest.raises(BinaryNoteError):
            await backend.write_note("img.png", "not really a png")
    assert log == []


async def test_write_size_is_decoded_bytes():
    # Spec §7.4: size is the decoded body length, not the chunk length.
    content = "Съешь ещё этих мягких булок"
    async with fake_backend({}) as (backend, _log):
        meta = await backend.write_note("ru.md", content, create=True)
        roundtrip = await backend.read_note("ru.md")
    assert meta["size"] == len(content.encode("utf-8"))
    assert meta["type"] == "plain"
    assert roundtrip["text"] == content


async def test_write_reuses_existing_chunk():
    # Deterministic chunk ids: a chunk already stored under the
    # computed id with exactly the intended data is reused verbatim —
    # no PUT of the chunk, no spurious conflict.
    docs = {chunk_id_for("same"): {"data": "same", "type": "leaf"}}
    async with fake_vault(docs) as (backend, raw, log):
        await backend.write_note("a.md", "same", create=True)
        ops_end = len(log)
        chunk = await stored_doc(raw, chunk_id_for("same"))
        a = await backend.read_note("a.md")
    chunk_puts = [
        r
        for r in log[:ops_end]
        if r.method == "PUT"
        and chunk_id_for("same") in urllib.parse.unquote(str(r.url.path))
    ]
    assert chunk_puts == []
    assert chunk["_rev"] == "1-fake1"  # untouched since seeding
    assert chunk["data"] == "same"
    assert a["text"] == "same"


async def test_write_conflicting_chunk_refused():
    # A chunk stored under the computed id with DIFFERENT data must
    # never be published as the note's content (corrupt or collided
    # shared chunk — sha256-16 truncation). It is not overwritten or
    # "repaired" — other notes may reference it — and the metadata
    # stays untouched.
    docs = {
        chunk_id_for("collide"): {"data": "other payload", "type": "leaf"}
    }
    async with fake_vault(docs) as (backend, raw, log):
        with pytest.raises(StorageError, match="content chunk"):
            await backend.write_note("note.md", "collide", create=True)
        ops_end = len(log)
        chunk = await stored_doc(raw, chunk_id_for("collide"))
        meta_response = await raw.get(
            f"{DATABASE}/{urllib.parse.quote('note.md', safe='')}"
        )
    assert not any(
        r.method in ("PUT", "POST", "DELETE") for r in log[:ops_end]
    )
    assert chunk["data"] == "other payload"  # chunk left untouched
    assert chunk["_rev"] == "1-fake1"
    assert meta_response.status_code == 404  # metadata never created


async def test_resurrect_requires_create():
    # B12: a soft-deleted note is absent; only an explicit create
    # resurrects it, as a new revision of the same document.
    docs = {"note.md": plain_note("note.md", "note.md", ["h:old"], deleted=True)}
    async with fake_vault(docs) as (backend, raw, log):
        with pytest.raises(NoteNotFound):
            await backend.write_note("note.md", "fresh", overwrite=True)
        meta = await backend.write_note("note.md", "fresh", create=True)
        ops_end = len(log)
        stored = await stored_doc(raw, "note.md")
        content = await backend.read_note("note.md")
    assert "deleted" not in stored
    assert stored["_rev"] == "2-fake2"  # one revision on the old body
    assert stored["path"] == "note.md"
    assert stored["children"] == [chunk_id_for("fresh")]
    assert content["text"] == "fresh"
    assert meta["size"] == len("fresh".encode("utf-8"))


async def test_delete_soft():
    # B2: deletion is a new revision with deleted:true; type/path/
    # children preserved, chunk untouched, zero DELETE requests.
    docs = {
        "note.md": plain_note("note.md", "note.md", ["h:body"]),
        "h:body": {"data": "hello", "type": "leaf"},
    }
    async with fake_vault(docs) as (backend, raw, log):
        deleted = await backend.delete_note("note.md")
        ops_end = len(log)
        stored = await stored_doc(raw, "note.md")
        chunk = await stored_doc(raw, "h:body")
        gone = await backend.read_note("note.md")
    assert deleted is True
    assert not any(r.method == "DELETE" for r in log[:ops_end])
    assert stored["deleted"] is True
    assert stored["type"] == "plain"
    assert stored["path"] == "note.md"
    assert stored["children"] == ["h:body"]
    assert chunk["data"] == "hello"  # chunk survives (B1)
    assert gone is None  # B12: soft-deleted reads as absent


async def test_delete_missing_returns_false():
    async with fake_backend({}) as (backend, _log):
        assert await backend.delete_note("ghost.md") is False
    docs = {"a.md": plain_note("a.md", "a.md", [], deleted=True)}
    async with fake_backend(docs) as (backend, _log):
        assert await backend.delete_note("a.md") is False


async def test_delete_note_stale_rev_raises():
    # A real rev race (no forced status): another writer commits after
    # our GET, so the soft-delete PUT carries a stale rev. The delete
    # must surface ConcurrentModification and leave the note live.
    docs = {"note.md": plain_note("note.md", "note.md", [])}
    async with fake_vault(docs, bump_rev_on_get=("note.md",)) as (
        backend, raw, log,
    ):
        with pytest.raises(ConcurrentModification, match="concurrent"):
            await backend.delete_note("note.md")
        ops_end = len(log)
        stored = await stored_doc(raw, "note.md")
    assert not any(r.method == "DELETE" for r in log[:ops_end])
    assert "deleted" not in stored  # still live after the failed delete
    assert stored["_rev"] == "2-fake2"  # the racing writer's revision


async def test_rename_same_path_noop():
    # D1: renaming onto the identical path mutates nothing.
    #
    # RULING (controller): the binding contract for same-path rename is
    # the plan's "no HTTP MUTATIONS" — a single read-only GET is
    # permitted and unavoidable (the existence check behind the returned
    # meta); the requirement is zero PUT/POST/DELETE requests.
    docs = {"a.md": plain_note("a.md", "a.md", [])}
    async with fake_backend(docs) as (backend, log):
        meta = await backend.rename_note("a.md", "a.md")
        ops_end = len(log)
    assert meta["path"] == "a.md"
    assert meta["doc_id"] == "a.md"
    assert mutation_free(log, ops_end)


async def test_rename_case_only_updates_path():
    # D1: a case-only rename is one revision updating the path field —
    # no new document, no deletion.
    docs = {"a.md": plain_note("a.md", "A.md", ["h:c"])}
    async with fake_vault(docs) as (backend, raw, log):
        meta = await backend.rename_note("A.md", "a.md")
        ops_end = len(log)
        stored = await stored_doc(raw, "a.md")
    puts = [r for r in log[:ops_end] if r.method == "PUT"]
    assert len(puts) == 1
    assert not any(r.method == "DELETE" for r in log[:ops_end])
    assert meta["path"] == "a.md"
    assert stored["path"] == "a.md"
    assert stored["_rev"] == "2-fake2"  # exactly one new revision
    assert stored["ctime"] == 10  # preserved
    assert "deleted" not in stored


async def test_rename_target_exists_refused():
    # D2: moving onto a live note without overwrite is an error.
    docs = {
        "a.md": plain_note("a.md", "a.md", []),
        "b.md": plain_note("b.md", "b.md", []),
    }
    async with fake_backend(docs) as (backend, log):
        with pytest.raises(NoteExists):
            await backend.rename_note("a.md", "b.md")
    assert mutation_free(log, len(log))


async def test_rename_copies_children_and_ctime():
    # B4: a binary note moves chunk-wise — same children ids, no
    # content reassembly, ctime preserved, source soft-deleted.
    payload = base64.b64encode(b"\x89PNG-bytes").decode("ascii")
    docs = {
        "img.png": {
            "_id": "img.png",
            "path": "img.png",
            "type": "newnote",
            "children": ["h:p1"],
            "ctime": 111,
            "mtime": 222,
            "size": 13,
        },
        "h:p1": {"data": payload, "type": "leaf"},
    }
    async with fake_vault(docs) as (backend, raw, log):
        meta = await backend.rename_note("img.png", "moved/pic.png")
        ops_end = len(log)
        new_doc = await stored_doc(raw, "moved/pic.png")
        old_doc = await stored_doc(raw, "img.png")
        content = await backend.read_note("moved/pic.png")
    assert meta["path"] == "moved/pic.png"
    assert meta["ctime"] == 111
    assert new_doc["children"] == ["h:p1"]  # same chunk reference
    assert new_doc["type"] == "newnote"
    assert new_doc["ctime"] == 111
    assert new_doc["_rev"] == "1-fake1"  # brand-new metadata document
    assert content["data"] == b"\x89PNG-bytes"
    assert old_doc["deleted"] is True  # source soft-deleted...
    assert old_doc["children"] == ["h:p1"]  # ...with children preserved
    put_paths = [
        urllib.parse.unquote(str(r.url.path))
        for r in log[:ops_end]
        if r.method == "PUT"
    ]
    # CHRONOLOGICAL order, not sorted: the target is published before
    # the source is retired — retiring first could lose the note if the
    # copy failed. Exactly two metadata PUTs, no chunk rewritten.
    assert put_paths == [
        f"/{DATABASE}/moved/pic.png",
        f"/{DATABASE}/img.png",
    ]
    assert not any(r.method == "DELETE" for r in log[:ops_end])


async def test_rename_target_race_keeps_source_live():
    # Failure window 1 (real rev race, no forced status): the target
    # PUT loses to a concurrent writer. The source must remain live and
    # untouched — the soft delete is never issued before the copy
    # exists.
    docs = {
        "a.md": plain_note("a.md", "a.md", ["h:c"]),
        # soft-deleted target: its stored rev is the one raced away
        "b.md": plain_note("b.md", "b.md", [], deleted=True),
        "h:c": {"data": "x", "type": "leaf"},
    }
    async with fake_vault(docs, bump_rev_on_get=("b.md",)) as (
        backend, raw, log,
    ):
        with pytest.raises(ConcurrentModification, match="concurrent"):
            await backend.rename_note("a.md", "b.md")
        ops_end = len(log)
        source = await stored_doc(raw, "a.md")
    put_paths = [
        urllib.parse.unquote(str(r.url.path))
        for r in log[:ops_end]
        if r.method == "PUT"
    ]
    assert f"/{DATABASE}/a.md" not in put_paths  # no retire attempt
    assert "deleted" not in source  # source still live
    assert source["_rev"] == "1-fake1"  # and untouched
    assert not any(r.method == "DELETE" for r in log[:ops_end])


async def test_rename_retire_race_leaves_both_live():
    # Failure window 2 (real rev race): the copy lands, but the source
    # soft delete carries a stale rev. Both notes stay live — a
    # duplicate is recoverable, lost data is not — and the error
    # surfaces to the caller.
    docs = {"a.md": plain_note("a.md", "a.md", [])}
    async with fake_vault(docs, bump_rev_on_get=("a.md",)) as (
        backend, raw, log,
    ):
        with pytest.raises(ConcurrentModification, match="concurrent"):
            await backend.rename_note("a.md", "b.md")
        source = await stored_doc(raw, "a.md")
        target = await stored_doc(raw, "b.md")
        ops_end = len(log)
    assert "deleted" not in source  # retirement failed, still live
    assert source["_rev"] == "2-fake2"  # racing writer's revision
    assert "deleted" not in target  # the copy exists
    assert target["_id"] == "b.md"
    assert not any(r.method == "DELETE" for r in log[:ops_end])


async def test_copy_note_shares_chunks_and_keeps_source():
    docs = {
        "a.md": plain_note("a.md", "a.md", ["h:c"], ctime=77),
        "h:c": {"data": "body", "type": "leaf"},
    }
    async with fake_vault(docs) as (backend, raw, log):
        meta = await backend.copy_note("a.md", "b.md")
        ops_end = len(log)
        source = await stored_doc(raw, "a.md")
        target = await stored_doc(raw, "b.md")
        copied = await backend.read_note("b.md")
    assert meta["path"] == "b.md"
    assert meta["ctime"] == 77
    assert target["children"] == ["h:c"]  # chunks shared, not duplicated
    assert "deleted" not in source  # copy never deletes the source
    assert copied["text"] == "body"
    assert not any(r.method == "DELETE" for r in log[:ops_end])


async def test_copy_note_onto_existing_refused():
    docs = {
        "a.md": plain_note("a.md", "a.md", []),
        "b.md": plain_note("b.md", "b.md", []),
    }
    async with fake_backend(docs) as (backend, log):
        with pytest.raises(NoteExists):
            await backend.copy_note("a.md", "b.md")
    assert mutation_free(log, len(log))


# --- read-through revision precondition (lost update) ----------------------


async def _device_write(raw: httpx.AsyncClient, doc_id: str, **changes) -> None:
    """Simulate a device committing a new revision of ``doc_id``."""
    doc = await stored_doc(raw, doc_id)
    doc.update(changes)
    response = await raw.put(
        f"{DATABASE}/{urllib.parse.quote(doc_id, safe='')}", json=doc
    )
    assert response.status_code == 201, response.text


async def test_read_note_and_get_meta_expose_revision():
    docs = {"note.md": plain_note("note.md", "note.md", [])}
    async with fake_backend(docs) as (backend, _log):
        content = await backend.read_note("note.md")
        meta = await backend.get_meta("note.md")
    assert content["rev"] == "1-fake1"
    assert content["meta"]["rev"] == "1-fake1"
    assert meta["rev"] == "1-fake1"


async def test_write_stale_expected_rev_refused():
    # A device commit lands between the tool's read and its write: the
    # read-through precondition fails BEFORE any chunk or metadata PUT,
    # and the device's revision survives untouched.
    docs = {
        "note.md": plain_note("note.md", "note.md", ["h:old"]),
        "h:old": {"data": "old", "type": "leaf"},
    }
    async with fake_vault(docs) as (backend, raw, log):
        read = await backend.read_note("note.md")
        assert read["rev"] == "1-fake1"
        await _device_write(
            raw, "note.md", children=[chunk_id_for("device edit")], mtime=999
        )
        with pytest.raises(ConcurrentModification, match="concurrent"):
            await backend.write_note(
                "note.md", "tool edit", expected_rev=read["rev"]
            )
        stored = await stored_doc(raw, "note.md")
        tool_chunk_response = await raw.get(
            f"{DATABASE}/{urllib.parse.quote(chunk_id_for('tool edit'), safe='')}"
        )
    assert stored["children"] == [chunk_id_for("device edit")]  # device won
    assert stored["_rev"] == "2-fake2"
    assert tool_chunk_response.status_code == 404  # refused before chunk PUT


async def test_write_matching_expected_rev_succeeds():
    docs = {
        "note.md": plain_note("note.md", "note.md", ["h:old"]),
        "h:old": {"data": "old", "type": "leaf"},
    }
    async with fake_backend(docs) as (backend, _log):
        read = await backend.read_note("note.md")
        meta = await backend.write_note(
            "note.md", "fresh", expected_rev=read["rev"]
        )
        content = await backend.read_note("note.md")
    assert meta["rev"] == "2-fake2"
    assert content["text"] == "fresh"


async def test_write_expected_rev_on_vanished_note_refused():
    # The note the tool read is gone (hard tombstone): refusing beats
    # silently recreating it (which would resurrect a deleted note).
    async with fake_backend({}) as (backend, log):
        with pytest.raises(ConcurrentModification, match="concurrent"):
            await backend.write_note(
                "ghost.md", "x", create=True, expected_rev="1-fake1"
            )
    assert mutation_free(log, len(log))


async def test_write_without_expected_rev_keeps_last_writer_wins():
    # expected_rev=None is the documented last-writer-wins escape hatch.
    docs = {"note.md": plain_note("note.md", "note.md", [])}
    async with fake_vault(docs) as (backend, raw, _log):
        await backend.read_note("note.md")
        await _device_write(raw, "note.md", mtime=999)
        await backend.write_note("note.md", "tool edit", overwrite=True)
        stored = await stored_doc(raw, "note.md")
    assert stored["mtime"] != 999  # the later write won, by contract


async def test_delete_stale_expected_rev_refused():
    docs = {"note.md": plain_note("note.md", "note.md", [])}
    async with fake_vault(docs) as (backend, raw, log):
        read = await backend.read_note("note.md")
        await _device_write(raw, "note.md", mtime=999)
        log.clear()  # only the delete attempt is scrutinized from here
        with pytest.raises(ConcurrentModification, match="concurrent"):
            await backend.delete_note("note.md", expected_rev=read["rev"])
        stored = await stored_doc(raw, "note.md")
    assert "deleted" not in stored  # the racing delete never landed
    assert stored["mtime"] == 999  # the device's revision survived
    assert mutation_free(log, len(log))  # no write was attempted at all


async def test_delete_matching_expected_rev_succeeds():
    docs = {"note.md": plain_note("note.md", "note.md", [])}
    async with fake_backend(docs) as (backend, _log):
        read = await backend.read_note("note.md")
        deleted = await backend.delete_note(
            "note.md", expected_rev=read["rev"]
        )
        gone = await backend.read_note("note.md")
    assert deleted is True
    assert gone is None
