"""Integration smoke: ``CouchDBBackend`` against a real CouchDB.

Env-gated: the test runs only when ``COUCHDB_TEST_URL``,
``COUCHDB_TEST_USER`` and ``COUCHDB_TEST_PASSWORD`` are all set; in CI
and plain ``pytest`` runs it skips.

Against a live CouchDB it creates a scratch database
``mcp_test_<random>``, drives the backend through the write lifecycle
(startup -> create -> read -> update -> rename -> duplicate -> soft
delete) and verifies the LiveSync storage invariants directly in the
database after every operation:

- audit B1 — chunks are never deleted: every chunk id seen during the
  run is still a live document in ``_all_docs``;
- audit B2 — deletion is a soft-delete revision, never a CouchDB
  tombstone: ``_all_docs`` contains zero deleted rows, and the
  soft-deleted note keeps ``deleted: true`` with ``type``/``path``/
  ``children`` intact.

The scratch database is dropped even when an assertion fails.
"""

from __future__ import annotations

import os
import secrets
import urllib.parse

import httpx
import pytest

from livesync_format import path2id
from storage_couchdb import CouchDBBackend

_URL = os.environ.get("COUCHDB_TEST_URL", "")
_USER = os.environ.get("COUCHDB_TEST_USER", "")
_PASSWORD = os.environ.get("COUCHDB_TEST_PASSWORD", "")

pytestmark = pytest.mark.skipif(
    not (_URL and _USER and _PASSWORD),
    reason="COUCHDB_TEST_URL / COUCHDB_TEST_USER / COUCHDB_TEST_PASSWORD "
    "not set; skipping real-CouchDB smoke test",
)


async def stored_doc(
    raw: httpx.AsyncClient, database: str, doc_id: str
) -> dict:
    """Fetch a document straight from CouchDB, asserting it is live."""
    response = await raw.get(
        f"{database}/{urllib.parse.quote(doc_id, safe='')}"
    )
    assert response.status_code == 200, (
        doc_id,
        response.status_code,
        response.text,
    )
    return response.json()


async def assert_invariants(
    raw: httpx.AsyncClient, database: str, chunk_ids: set[str]
) -> None:
    """Zero CouchDB DELETE traces; every seen chunk still exists."""
    response = await raw.get(f"{database}/_all_docs")
    assert response.status_code == 200, response.text
    body = response.json()
    rows = body["rows"]
    # The scratch database stays tiny: one unpaged _all_docs response.
    assert body["total_rows"] == len(rows), body
    live_ids = set()
    for row in rows:
        assert not (row.get("value") or {}).get("deleted"), (
            f"CouchDB tombstone in _all_docs (bare DELETE issued): {row}"
        )
        live_ids.add(row["id"])
    for chunk_id in sorted(chunk_ids):
        assert chunk_id in live_ids, (
            f"content chunk {chunk_id} disappeared from _all_docs"
        )


async def test_real_couchdb_write_lifecycle():
    database = f"mcp_test_{secrets.token_hex(4)}"
    raw = httpx.AsyncClient(
        base_url=_URL if _URL.endswith("/") else _URL + "/",
        auth=(_USER, _PASSWORD),
        timeout=30.0,
    )
    backend: CouchDBBackend | None = None
    db_ready = False

    note_path = "smoke/integration.md"
    renamed_path = "smoke/renamed.md"
    copy_path = "smoke/copy.md"
    body_v1 = "integration smoke line one\n"
    body_v2 = "integration smoke line one\nupdated second revision\n"
    chunk_ids: set[str] = set()

    try:
        created = await raw.put(database)
        assert created.status_code == 201, created.text
        db_ready = True
        backend = CouchDBBackend(_URL, _USER, _PASSWORD, database)

        async def doc(path: str) -> dict:
            return await stored_doc(raw, database, path2id(path))

        async def checkpoint() -> None:
            await assert_invariants(raw, database, chunk_ids)

        # -- startup: fresh vault, no E2EE milestone --------------------------
        await backend.startup_check()

        # -- create ------------------------------------------------------------
        meta = await backend.write_note(note_path, body_v1, create=True)
        assert meta["path"] == note_path
        assert meta["size"] == len(body_v1.encode("utf-8"))
        original = await doc(note_path)
        assert original["type"] == "plain"
        chunk_ids.update(original["children"])
        assert chunk_ids
        await checkpoint()

        # -- read: stored text round-trips --------------------------------------
        content = await backend.read_note(note_path)
        assert content is not None
        assert content["text"] == body_v1
        await checkpoint()

        # -- update: new chunk, new size ---------------------------------------
        ctime = meta["ctime"]
        updated = await backend.write_note(note_path, body_v2)
        assert updated["size"] == len(body_v2.encode("utf-8"))
        assert updated["ctime"] == ctime
        assert updated["mtime"] >= meta["mtime"]
        updated_doc = await doc(note_path)
        assert updated_doc["children"] != original["children"]
        chunk_ids.update(updated_doc["children"])
        reread = await backend.read_note(note_path)
        assert reread is not None and reread["text"] == body_v2
        await checkpoint()

        # -- rename: children copied verbatim, ctime preserved ------------------
        renamed = await backend.rename_note(note_path, renamed_path)
        assert renamed["path"] == renamed_path
        assert renamed["ctime"] == ctime
        renamed_doc = await doc(renamed_path)
        assert renamed_doc["children"] == updated_doc["children"]
        assert renamed_doc["ctime"] == ctime
        chunk_ids.update(renamed_doc["children"])
        assert await backend.read_note(note_path) is None  # source retired
        moved = await backend.read_note(renamed_path)
        assert moved is not None and moved["text"] == body_v2
        await checkpoint()

        # -- duplicate: chunk reuse, both copies live ---------------------------
        copied = await backend.copy_note(renamed_path, copy_path)
        assert copied["ctime"] == ctime
        copy_doc = await doc(copy_path)
        assert copy_doc["children"] == renamed_doc["children"]
        chunk_ids.update(copy_doc["children"])
        assert (await backend.read_note(copy_path))["text"] == body_v2
        assert (await backend.read_note(renamed_path))["text"] == body_v2
        await checkpoint()

        # -- soft delete: LiveSync revision, not a CouchDB tombstone ------------
        assert await backend.delete_note(copy_path) is True
        assert await backend.read_note(copy_path) is None
        tombstone = await doc(copy_path)  # still a live CouchDB document
        assert tombstone["deleted"] is True
        assert tombstone["type"] == copy_doc["type"]
        assert tombstone["path"] == copy_path
        assert tombstone["children"] == copy_doc["children"]
        assert await backend.delete_note(copy_path) is False
        listed = [m["path"] for m in await backend.list_notes()]
        assert listed == [renamed_path]
        await checkpoint()

        # Both revisions left distinct chunks, and all survive (audit B1).
        assert len(chunk_ids) >= 2
    finally:
        if backend is not None:
            await backend.close()
        if db_ready:
            dropped = await raw.delete(database)
            assert dropped.status_code == 200, dropped.text
        await raw.aclose()
