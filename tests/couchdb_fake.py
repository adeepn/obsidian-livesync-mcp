"""In-memory CouchDB fake built on ``httpx.MockTransport``.

``make_fake_couchdb(docs)`` returns a ``(transport, request_log)`` pair.
The transport emulates the CouchDB surface the storage backend uses:

- ``GET /{db}`` (db info with ``update_seq``/``doc_count``),
- ``GET/PUT/DELETE /{db}/{id}`` with revision conflict semantics (409),
- ``GET/POST /{db}/_all_docs`` including ``{keys}`` batch fetches,
- ``startkey``/``endkey``/``skip``/``limit`` paging with bytewise key
  ordering, exactly like CouchDB's ``_all_docs`` raw collation.

Deletion semantics match CouchDB, not a dict: a bare ``DELETE`` keeps a
tombstone revision (the id stays in the store with ``_deleted: true``
and a bumped rev). ``GET`` on a tombstone answers 404 ``"deleted"``,
default ``_all_docs`` scans omit it, but a ``{keys}`` batch fetch
returns its row with ``value.deleted: true`` — the read path's "hard
CouchDB tombstone" case. A ``PUT`` recreating a tombstoned id must
carry the tombstone's rev, exactly like CouchDB's rev tree.
``deleted: true`` *inside* a document body still models a LiveSync
soft delete: the document stays fully live for CouchDB.

Scenes: ``db_exists=False`` makes every request against the database
answer 404 ``{"error": "not_found", "reason": "no_db_file"}`` like a
real CouchDB with a missing database (document-level 404s carry
reason ``"missing"`` instead); ``db_dies_after=N`` lets the first N
requests succeed and kills the database from request N+1 on;
``force_status=N`` makes every request answer ``N`` (401/500/...);
``put_conflict_ids`` makes every PUT to the listed document ids answer
409 regardless of the rev — a racing writer won the rev tree between
our GET and our PUT; ``bump_rev_on_get`` serves the requested revision
of the listed ids but commits a newer revision to the store right
after the read, so a subsequent PUT carrying the seen rev fails as a
genuine stale-revision 409 (a real rev race, not a forced status);
``put_202_ids`` makes every PUT to the listed document ids answer
202 ``ok:true`` without committing — a write the caller must not
treat as durable.

Like CouchDB, a stored ``_conflicts`` field is only served when the
request carries ``conflicts=true`` (query parameter); every other
response omits it.

Every incoming request is appended to ``request_log`` so tests can assert
which documents were fetched and with which query parameters.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import httpx


def _json_response(status_code: int, body: dict) -> httpx.Response:
    # CouchDB responses can escape UTF-16 surrogates on the _all_docs route.
    return httpx.Response(status_code, content=json.dumps(body, ensure_ascii=True).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})


def _next_rev(rev: str) -> str:
    generation = rev.split("-", 1)[0]
    try:
        number = int(generation) + 1
    except ValueError:
        number = 2
    return f"{number}-fake{number}"


def make_fake_couchdb(
    docs: dict[str, dict],
    *,
    db_exists: bool = True,
    db_dies_after: int | None = None,
    force_status: int | None = None,
    put_conflict_ids: tuple[str, ...] = (),
    bump_rev_on_get: tuple[str, ...] = (),
    put_202_ids: tuple[str, ...] = (),
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Build a fake CouchDB over ``docs`` (id -> document body).

    Documents are copied; a ``_rev`` is added when absent. ``deleted`` in a
    document body models a LiveSync soft delete (the document stays in
    ``_all_docs``), which is what the backend must treat as absent.
    """
    store: dict[str, dict] = {}
    for doc_id, body in docs.items():
        doc = dict(body)
        doc.setdefault("_id", doc_id)
        doc.setdefault("_rev", "1-fake1")
        store[doc_id] = doc
    state = {"update_seq": len(store)}
    request_log: list[httpx.Request] = []

    def live_count() -> int:
        # CouchDB's doc_count/total_rows exclude tombstones.
        return sum(1 for doc in store.values() if not doc.get("_deleted"))

    def row_for(
        doc_id: str,
        include_docs: bool,
        *,
        by_keys: bool,
        with_conflicts: bool = False,
    ) -> dict:
        doc = store.get(doc_id)
        if doc is None:
            return {"key": doc_id, "error": "not_found"}
        if doc.get("_deleted"):
            if by_keys:
                # Keys queries expose tombstone rows; scans never do.
                return {
                    "id": doc_id,
                    "key": doc_id,
                    "value": {"rev": doc["_rev"], "deleted": True},
                }
            return {"key": doc_id, "error": "not_found"}
        row = {"id": doc_id, "key": doc_id, "value": {"rev": doc["_rev"]}}
        if include_docs:
            row["doc"] = doc
            if not with_conflicts and "_conflicts" in doc:
                row["doc"] = {
                    key: value
                    for key, value in doc.items()
                    if key != "_conflicts"
                }
        return row

    def all_docs_response(request: httpx.Request) -> httpx.Response:
        body = {}
        if request.content:
            body = json.loads(request.content)
        params = dict(request.url.params)
        include_docs = bool(body.get("include_docs")) or (
            params.get("include_docs") == "true"
        )
        if "keys" in body:
            with_conflicts = params.get("conflicts") == "true"
            rows = [
                row_for(
                    key, include_docs, by_keys=True, with_conflicts=with_conflicts
                )
                for key in body["keys"]
            ]
            return _json_response(200, {"total_rows": live_count(), "rows": rows})

        ordered = sorted(store, key=lambda key: key.encode("utf-8"))
        ordered = [k for k in ordered if not store[k].get("_deleted")]
        start = params.get("startkey")
        end = params.get("endkey")
        if start is not None:
            start_key = json.loads(start).encode("utf-8")
            ordered = [k for k in ordered if k.encode("utf-8") >= start_key]
        if end is not None:
            end_key = json.loads(end).encode("utf-8")
            ordered = [k for k in ordered if k.encode("utf-8") <= end_key]
        skip = int(params.get("skip", "0"))
        limit = params.get("limit")
        window = ordered[skip:] if limit is None else ordered[skip : skip + int(limit)]
        rows = [row_for(key, include_docs, by_keys=False) for key in window]
        return _json_response(200, {"total_rows": live_count(), "rows": rows})

    def handler(request: httpx.Request) -> httpx.Response:
        request.extensions["raw_body"] = bytes(request.content)
        request_log.append(request)
        if force_status is not None:
            # "reason" doubles as a raw-body sentinel for log-hygiene
            # tests: error logs may carry the error code, never the body.
            return httpx.Response(
                force_status,
                json={"error": "forced", "reason": "sentinel-raw-body"},
            )

        db_alive = db_exists and (
            db_dies_after is None or len(request_log) <= db_dies_after
        )
        if not db_alive:
            # Missing database: every request fails like CouchDB does.
            return httpx.Response(
                404, json={"error": "not_found", "reason": "no_db_file"}
            )

        parts = request.url.path.lstrip("/").split("/", 1)
        database = unquote(parts[0])
        rest = unquote(parts[1]) if len(parts) > 1 else ""

        if not rest:
            # Database-level requests.
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "db_name": database,
                        "doc_count": live_count(),
                        "update_seq": state["update_seq"],
                    },
                )
            if request.method in ("PUT", "DELETE"):
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(405, json={"error": "method_not_allowed"})

        if rest == "_all_docs":
            if request.method == "POST":
                return all_docs_response(request)
            if request.method == "GET":
                return all_docs_response(request)
            return httpx.Response(405, json={"error": "method_not_allowed"})

        doc_id = rest
        if request.method == "GET":
            doc = store.get(doc_id)
            if doc is None:
                return httpx.Response(
                    404, json={"error": "not_found", "reason": "missing"}
                )
            if doc.get("_deleted"):
                # Tombstone: plain GET answers 404 "deleted" like CouchDB.
                return httpx.Response(
                    404, json={"error": "not_found", "reason": "deleted"}
                )
            body = dict(doc)
            if doc_id in bump_rev_on_get:
                # Serve the revision that was read, but commit a newer
                # one right after the read — a concurrent writer won
                # the rev tree, so a PUT with the seen rev is a genuine
                # stale-revision conflict.
                bumped = dict(doc)
                bumped["_rev"] = _next_rev(doc["_rev"])
                store[doc_id] = bumped
                state["update_seq"] += 1
            if request.url.params.get("conflicts") != "true":
                body.pop("_conflicts", None)  # served only on request
            return httpx.Response(200, content=json.dumps(body, ensure_ascii=True).encode("utf-8"),
                                  headers={"content-type": "application/json"})

        if request.method == "PUT":
            if doc_id in put_conflict_ids:
                return httpx.Response(409, json={"error": "conflict"})
            if doc_id in put_202_ids:
                # "Accepted", not durable: CouchDB never does this for
                # document PUTs, but a misbehaving proxy might. Nothing
                # is committed to the store.
                return httpx.Response(
                    202, json={"ok": True, "id": doc_id, "rev": "0-none"}
                )
            body = json.loads(request.content)
            existing = store.get(doc_id)
            if existing is not None:
                # A tombstone still owns its rev tree slot: recreating
                # the id requires the tombstone's rev, like CouchDB.
                rev = request.url.params.get("rev") or body.get("_rev")
                if rev != existing["_rev"]:
                    return httpx.Response(409, json={"error": "conflict"})
                new_rev = _next_rev(existing["_rev"])
            else:
                if body.get("_rev"):
                    return httpx.Response(409, json={"error": "conflict"})
                new_rev = "1-fake1"
            body["_id"] = doc_id
            body["_rev"] = new_rev
            store[doc_id] = body
            state["update_seq"] += 1
            return httpx.Response(201, json={"ok": True, "id": doc_id, "rev": new_rev})

        if request.method == "DELETE":
            doc = store.get(doc_id)
            if doc is None:
                return httpx.Response(
                    404, json={"error": "not_found", "reason": "missing"}
                )
            if doc.get("_deleted"):
                return httpx.Response(
                    404, json={"error": "not_found", "reason": "deleted"}
                )
            if request.url.params.get("rev") != doc["_rev"]:
                return httpx.Response(409, json={"error": "conflict"})
            # CouchDB keeps a tombstone revision instead of dropping the
            # id; only _all_docs/GET hide it (see row_for).
            tombstone = dict(doc)
            tombstone["_deleted"] = True
            tombstone["_rev"] = _next_rev(doc["_rev"])
            store[doc_id] = tombstone
            state["update_seq"] += 1
            return httpx.Response(
                200, json={"ok": True, "id": doc_id, "rev": tombstone["_rev"]}
            )

        return httpx.Response(405, json={"error": "method_not_allowed"})

    return httpx.MockTransport(handler), request_log
