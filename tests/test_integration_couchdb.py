"""Integration smoke: ``CouchDBBackend`` against a real CouchDB.

Env-gated: the test runs only when ``COUCHDB_TEST_URL``,
``COUCHDB_TEST_USER`` and ``COUCHDB_TEST_PASSWORD`` are all set; in CI
and plain ``pytest`` runs it skips.

Against a live CouchDB it creates a scratch database
``mcp_test_<random>``, drives the backend through the write lifecycle
(startup -> create -> read -> update -> rename -> duplicate -> soft
delete) and verifies the LiveSync storage invariants with
deletion-aware evidence after every operation:

- audit B1 — chunks are never deleted or rewritten: every chunk
  document the run ever wrote (discovered from ``_changes``, including
  chunks referenced only by notes that were later soft-deleted) is
  still a live ``leaf`` document holding exactly the bytes written;
- audit B2 — deletion is always a LiveSync soft-delete revision, never
  a bare CouchDB DELETE: ``_changes?style=all_docs`` is drained at
  every checkpoint and must contain no ``deleted: true`` entry (a bare
  DELETE cannot hide there the way it can in unkeyed ``_all_docs``,
  which excludes deleted documents), and the retired rename source and
  the deleted duplicate are fetched by id and must still carry their
  full LiveSync body — ``deleted: true`` with ``type``/``path``/
  ``children`` intact.

The scratch database is dropped even when an assertion fails: cleanup
lives in fixture teardowns with one ``try/finally`` per resource, so a
failing ``backend.close()`` cannot skip the database drop, and a failed
drop cannot leak the HTTP client.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.parse
from collections.abc import AsyncIterator

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


class ChangeTracker:
    """Deletion-aware checkpoint over the database changes feed.

    Unkeyed ``_all_docs`` excludes CouchDB-deleted documents, so it can
    never prove that nothing was bare-deleted. ``_changes`` is
    deletion-aware by design: a bare CouchDB DELETE surfaces as a
    change entry with ``deleted: true``, while a LiveSync soft delete
    is an ordinary revision. Each checkpoint

    - drains ``_changes?style=all_docs`` since the previous checkpoint
      and fails on any ``deleted: true`` entry — a bare DELETE of a
      note or a chunk anywhere in the run cannot hide;
    - records every chunk document id (``h:`` namespace) the run ever
      wrote — including chunks referenced only by documents that were
      later soft-deleted — and verifies each one is still a live
      ``leaf`` document with string data.
    """

    def __init__(
        self, raw: httpx.AsyncClient, database: str, update_seq: object
    ) -> None:
        self._raw = raw
        self._database = database
        self._since = json.dumps(update_seq)
        self.chunk_ids: set[str] = set()

    async def _drain(self) -> list[dict]:
        results: list[dict] = []
        while True:
            response = await self._raw.get(
                f"{self._database}/_changes",
                params={
                    "since": self._since,
                    "style": "all_docs",
                    "limit": "500",
                },
            )
            assert response.status_code == 200, response.text
            body = response.json()
            batch = body.get("results", [])
            results.extend(batch)
            self._since = json.dumps(body["last_seq"])
            if len(batch) < 500:
                return results

    async def checkpoint(self) -> None:
        for result in await self._drain():
            assert not result.get("deleted"), (
                f"bare CouchDB deletion detected: {result}"
            )
            doc_id = result.get("id")
            if isinstance(doc_id, str) and doc_id.startswith("h:"):
                self.chunk_ids.add(doc_id)
        for chunk_id in sorted(self.chunk_ids):
            chunk = await stored_doc(self._raw, self._database, chunk_id)
            assert chunk.get("type") == "leaf", chunk_id
            assert isinstance(chunk.get("data"), str), chunk_id


@pytest.fixture
async def raw_client() -> AsyncIterator[httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url=_URL if _URL.endswith("/") else _URL + "/",
        auth=(_USER, _PASSWORD),
        timeout=30.0,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def scratch_database(raw_client) -> AsyncIterator[str]:
    database = f"mcp_test_{secrets.token_hex(4)}"
    created = await raw_client.put(database)
    assert created.status_code == 201, created.text
    try:
        yield database
    finally:
        dropped = await raw_client.delete(database)
        assert dropped.status_code == 200, dropped.text


@pytest.fixture
async def backend(scratch_database) -> AsyncIterator[CouchDBBackend]:
    backend = CouchDBBackend(_URL, _USER, _PASSWORD, scratch_database)
    try:
        yield backend
    finally:
        await backend.close()


async def test_real_couchdb_write_lifecycle(
    raw_client, scratch_database, backend
):
    database = scratch_database
    info = await raw_client.get(database)
    assert info.status_code == 200, info.text
    tracker = ChangeTracker(raw_client, database, info.json()["update_seq"])

    note_path = "smoke/integration.md"
    renamed_path = "smoke/renamed.md"
    copy_path = "smoke/copy.md"
    body_v1 = "integration smoke line one\n"
    body_v2 = "integration smoke line one\nupdated second revision\n"

    async def doc(path: str) -> dict:
        return await stored_doc(raw_client, database, path2id(path))

    # -- startup: fresh vault, no E2EE milestone ----------------------------
    await backend.startup_check()

    # -- create --------------------------------------------------------------
    meta = await backend.write_note(note_path, body_v1, create=True)
    assert meta["path"] == note_path
    assert meta["size"] == len(body_v1.encode("utf-8"))
    original = await doc(note_path)
    assert original["type"] == "plain"
    await tracker.checkpoint()
    assert tracker.chunk_ids  # chunk discovery sees the write

    # -- read: stored text round-trips ----------------------------------------
    content = await backend.read_note(note_path)
    assert content is not None
    assert content["text"] == body_v1
    await tracker.checkpoint()

    # -- update: new chunk, new size ------------------------------------------
    ctime = meta["ctime"]
    updated = await backend.write_note(note_path, body_v2)
    assert updated["size"] == len(body_v2.encode("utf-8"))
    assert updated["ctime"] == ctime
    assert updated["mtime"] >= meta["mtime"]
    updated_doc = await doc(note_path)
    assert updated_doc["children"] != original["children"]
    reread = await backend.read_note(note_path)
    assert reread is not None and reread["text"] == body_v2
    await tracker.checkpoint()
    assert len(tracker.chunk_ids) == 2  # both revisions left a chunk

    # -- rename: children copied verbatim, ctime preserved --------------------
    renamed = await backend.rename_note(note_path, renamed_path)
    assert renamed["path"] == renamed_path
    assert renamed["ctime"] == ctime
    renamed_doc = await doc(renamed_path)
    assert renamed_doc["children"] == updated_doc["children"]
    assert renamed_doc["ctime"] == ctime
    assert await backend.read_note(note_path) is None  # source retired
    # The retired source must be a LiveSync soft-delete revision: a live
    # CouchDB document carrying its body (a bare DELETE would leave no
    # retrievable document at all).
    retired = await doc(note_path)
    assert retired["deleted"] is True
    assert retired["type"] == updated_doc["type"]
    assert retired["path"] == note_path
    assert retired["children"] == updated_doc["children"]
    moved = await backend.read_note(renamed_path)
    assert moved is not None and moved["text"] == body_v2
    await tracker.checkpoint()

    # -- duplicate: chunk reuse, both copies live -----------------------------
    copied = await backend.copy_note(renamed_path, copy_path)
    assert copied["ctime"] == ctime
    copy_doc = await doc(copy_path)
    assert copy_doc["children"] == renamed_doc["children"]
    assert (await backend.read_note(copy_path))["text"] == body_v2
    assert (await backend.read_note(renamed_path))["text"] == body_v2
    await tracker.checkpoint()
    assert len(tracker.chunk_ids) == 2  # duplicate reused, no new chunk

    # -- soft delete: LiveSync revision, not a CouchDB tombstone --------------
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
    await tracker.checkpoint()

    # Audit B1 close-out: both chunks ever written — the v1 chunk is by now
    # referenced only by the soft-deleted rename source — are still live
    # leaf documents holding exactly the bytes this test wrote.
    assert len(tracker.chunk_ids) == 2
    datas = {
        (await stored_doc(raw_client, database, chunk_id))["data"]
        for chunk_id in tracker.chunk_ids
    }
    assert datas == {body_v1, body_v2}
