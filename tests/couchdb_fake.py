"""In-memory CouchDB fake built on ``httpx.MockTransport``.

``make_fake_couchdb(docs)`` returns a ``(transport, request_log)`` pair.
The transport emulates the CouchDB surface the storage backend uses:

- ``GET /{db}`` (db info with ``update_seq``/``doc_count``),
- ``GET/PUT/DELETE /{db}/{id}`` with revision conflict semantics (409),
- ``GET/POST /{db}/_all_docs`` including ``{keys}`` batch fetches,
- ``startkey``/``endkey``/``skip``/``limit`` paging with bytewise key
  ordering, exactly like CouchDB's ``_all_docs`` raw collation.

Scenes: ``db_exists=False`` makes ``GET /{db}`` answer 404;
``force_status=N`` makes every request answer ``N`` (401/500/...).

Every incoming request is appended to ``request_log`` so tests can assert
which documents were fetched and with which query parameters.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import httpx


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
    force_status: int | None = None,
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

    def row_for(doc_id: str, include_docs: bool) -> dict:
        doc = store.get(doc_id)
        if doc is None:
            return {"key": doc_id, "error": "not_found"}
        row = {"id": doc_id, "key": doc_id, "value": {"rev": doc["_rev"]}}
        if include_docs:
            row["doc"] = doc
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
            rows = [row_for(key, include_docs) for key in body["keys"]]
            return httpx.Response(200, json={"total_rows": len(store), "rows": rows})

        ordered = sorted(store, key=lambda key: key.encode("utf-8"))
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
        rows = [row_for(key, include_docs) for key in window]
        return httpx.Response(200, json={"total_rows": len(store), "rows": rows})

    def handler(request: httpx.Request) -> httpx.Response:
        request_log.append(request)
        if force_status is not None:
            return httpx.Response(force_status, json={"error": "forced"})

        parts = request.url.path.lstrip("/").split("/", 1)
        database = unquote(parts[0])
        rest = unquote(parts[1]) if len(parts) > 1 else ""

        if not rest:
            # Database-level requests.
            if request.method == "GET":
                if not db_exists:
                    return httpx.Response(404, json={"error": "not_found"})
                return httpx.Response(
                    200,
                    json={
                        "db_name": database,
                        "doc_count": len(store),
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
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json=doc)

        if request.method == "PUT":
            body = json.loads(request.content)
            existing = store.get(doc_id)
            if existing is not None:
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
                return httpx.Response(404, json={"error": "not_found"})
            if request.url.params.get("rev") != doc["_rev"]:
                return httpx.Response(409, json={"error": "conflict"})
            del store[doc_id]
            state["update_seq"] += 1
            new_rev = _next_rev(doc["_rev"])
            return httpx.Response(200, json={"ok": True, "id": doc_id, "rev": new_rev})

        return httpx.Response(405, json={"error": "method_not_allowed"})

    return httpx.MockTransport(handler), request_log
