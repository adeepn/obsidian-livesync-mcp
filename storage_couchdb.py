"""CouchDB implementation of the ``StorageBackend`` protocol.

Documents follow Obsidian LiveSync semantics (audit 2026-11-10):

- a document is a note iff its ``type`` is ``plain``/``newnote``/``notes``
  and its id is outside the service namespaces (``i:``, ``ix:``, ``ps:``,
  ``h:``, ``f:``, ``_design/``, ``_local``, ``syncinfo``,
  ``obsydian_livesync_version``) — audit B8;
- soft-deleted documents (``deleted: true``) are absent for every read
  path — audit B12;
- ``plain`` chunks are verbatim text, ``newnote`` chunks are per-chunk
  base64 — audit B4; legacy ``notes`` documents keep the body in the
  document itself — audit B13;
- E2EE vaults are detected from ``_local/obsydian_livesync_milestone``
  (and defensively per chunk ``e_``) and rejected — audits B5/B14;
- ``list_notes`` never pulls chunk bodies: ids come from ``_all_docs``
  without ``include_docs`` in two ranges that skip the whole ``h:``
  namespace, metadata is then batch-fetched via ``POST _all_docs {keys}``
  — audit C5;
- all HTTP errors are mapped to storage exceptions with messages free of
  internal URLs and revisions (details go to the server log) — audit C7.

Write path (spec §7): every write validates the vault path before any
HTTP request; new content chunks are PUT before the metadata document,
so a failed metadata update can only orphan chunks, never point a note
at missing content (audits C1/C2); a note with unresolved CouchDB
conflicts is refused (audit C3); success is reported only after the
metadata revision is persisted. Deletion is always a LiveSync soft
delete (a new revision with ``deleted: true``, body preserved — audit
B2); chunk documents are never deleted or rewritten (audit B1), and no
bare HTTP DELETE is ever issued. Renames/copies move chunk references
without reassembling content, so binary notes travel chunk-wise
(audit B4).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from typing import AsyncIterator

import httpx

from livesync_format import (
    assemble_binary,
    assemble_text,
    chunk_id_for,
    doc_type_for,
    is_text_doc,
    new_metadata_doc,
    path2id,
    soft_delete_revision,
    validate_vault_path,
)
from storage_base import (
    BinaryNoteError,
    ConcurrentModification,
    NoteContent,
    NoteExists,
    NoteMeta,
    NoteNotFound,
    StorageError,
    StorageUnavailable,
    UnsupportedVault,
)

logger = logging.getLogger(__name__)

# LiveSync note document types (audits B3/B8/B13).
_NOTE_TYPES = frozenset({"plain", "newnote", "notes"})

# Service namespaces that must never surface as notes (audit B8/B10).
# Keep in sync with livesync_format._FORBIDDEN_PREFIXES / _RESERVED_PATHS.
_SERVICE_PREFIXES = ("i:", "ix:", "ps:", "h:", "f:")
_SERVICE_DOC_IDS = frozenset({"obsydian_livesync_version", "syncinfo"})

_MILESTONE_DOC_ID = "_local/obsydian_livesync_milestone"

# _all_docs page size for id scans and metadata batch fetches.
_PAGE_SIZE = 500

# CouchDB 404 body when the *database* is gone (a missing document
# answers with reason "missing" instead); conflating the two turns a
# dead database into apparent note absence.
_DB_MISSING_REASON = "no_db_file"


def _is_db_missing(response: httpx.Response) -> bool:
    """Whether a 404 response means the database is gone, not the doc."""
    if response.status_code != 404:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("reason") == _DB_MISSING_REASON


def _is_service_id(doc_id: str) -> bool:
    lowered = doc_id.lower()
    return (
        lowered.startswith(_SERVICE_PREFIXES)
        or lowered in _SERVICE_DOC_IDS
        or doc_id.startswith("_design/")
        or doc_id.startswith("_local")
    )


def _is_note_doc(doc: dict) -> bool:
    if doc.get("deleted"):
        return False
    if doc.get("type") not in _NOTE_TYPES:
        return False
    return not _is_service_id(str(doc.get("_id", "")))


def _matches_extension(path: str, extension: str) -> bool:
    suffix = extension if extension.startswith(".") else "." + extension
    return path.lower().endswith(suffix.lower())


def _as_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _now_ms() -> int:
    return int(time.time() * 1000)


class CouchDBBackend:
    """LiveSync vault storage served by a CouchDB instance."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        database: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._database = database
        self._client = httpx.AsyncClient(
            base_url=url if url.endswith("/") else url + "/",
            auth=(username, password),
            transport=transport,
            timeout=httpx.Timeout(5.0, read=30.0, write=30.0, pool=5.0),
        )
        # Note metadata index (doc id -> metadata doc), cached per
        # update_seq (audit C5).
        self._index: dict[str, dict] | None = None
        self._index_seq: object | None = None

    # -- low-level HTTP -----------------------------------------------------

    @property
    def _db_path(self) -> str:
        return urllib.parse.quote(self._database, safe="")

    def _doc_path(self, doc_id: str) -> str:
        return f"{self._db_path}/{urllib.parse.quote(doc_id, safe='')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        """Perform a request; the single error-mapping point (audit C7).

        404 responses are returned for the caller to interpret; every
        other error status and every transport failure raises a storage
        exception whose message carries no URL, database or revision.
        """
        try:
            response = await self._client.request(
                method, path, params=params, json=json_body
            )
        except httpx.HTTPError as exc:
            logger.warning("CouchDB %s %s failed: %r", method, path, exc)
            raise StorageUnavailable("CouchDB unavailable") from None

        code = response.status_code
        if code == 404:
            return response  # callers decide: missing doc vs missing db
        if code >= 400:
            logger.warning(
                "CouchDB %s %s -> %s %s",
                method,
                path,
                code,
                response.text[:200],
            )
            if code in (401, 403):
                raise StorageUnavailable("CouchDB authorization error")
            if code == 409:
                raise ConcurrentModification(
                    "the note was changed concurrently; re-read it and retry"
                )
            if code >= 500:
                raise StorageUnavailable("CouchDB unavailable")
            raise StorageUnavailable(f"CouchDB request failed (HTTP {code})")
        return response

    async def _db_info(self) -> dict:
        response = await self._request("GET", self._db_path)
        if response.status_code == 404:
            raise StorageUnavailable("CouchDB database not found")
        body = response.json()
        return body if isinstance(body, dict) else {}

    async def _get_doc(
        self, doc_id: str, *, conflicts: bool = False
    ) -> dict | None:
        params = {"conflicts": "true"} if conflicts else None
        response = await self._request(
            "GET", self._doc_path(doc_id), params=params
        )
        if response.status_code == 404:
            if _is_db_missing(response):
                raise StorageUnavailable("CouchDB database not found")
            return None
        body = response.json()
        return body if isinstance(body, dict) else None

    async def _post_all_docs(
        self, keys: list[str], *, include_docs: bool
    ) -> list[dict]:
        response = await self._request(
            "POST",
            f"{self._db_path}/_all_docs",
            json_body={"keys": keys, "include_docs": include_docs},
        )
        if response.status_code == 404:
            # A present database never 404s on _all_docs, regardless of
            # the keys; only the database path can be missing.
            raise StorageUnavailable("CouchDB database not found")
        rows = response.json().get("rows", [])
        return rows if isinstance(rows, list) else []

    # -- index (audit C5) ----------------------------------------------------

    async def _note_doc_ids(self) -> list[str]:
        """All candidate note ids without pulling any chunk bodies.

        Two ``_all_docs`` ranges skip the entire ``h:`` chunk namespace:
        CouchDB orders ``_all_docs`` bytewise, every chunk id sorts inside
        ``"h:"``..``"h;"`` (``:`` is 0x3A, ``;`` is 0x3B), and no note id
        can start with ``h:`` (validate_vault_path forbids it). Service
        ids outside that range are filtered here.
        """
        ids: list[str] = []
        range_params = (
            {"endkey": json.dumps("h:")},
            {"startkey": json.dumps("h;")},
        )
        for extra in range_params:
            skip = 0
            while True:
                params = {"limit": str(_PAGE_SIZE), "skip": str(skip), **extra}
                response = await self._request(
                    "GET", f"{self._db_path}/_all_docs", params=params
                )
                if response.status_code == 404:
                    # A present database never 404s on _all_docs.
                    raise StorageUnavailable("CouchDB database not found")
                rows = response.json().get("rows", [])
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if row.get("error") is not None:
                        continue
                    if (row.get("value") or {}).get("deleted"):
                        continue  # hard CouchDB tombstone
                    doc_id = row.get("id") or row.get("key")
                    if isinstance(doc_id, str) and not _is_service_id(doc_id):
                        ids.append(doc_id)
                if len(rows) < _PAGE_SIZE:
                    break
                skip += _PAGE_SIZE
        return ids

    async def _load_index(self) -> dict[str, dict]:
        ids = await self._note_doc_ids()
        index: dict[str, dict] = {}
        for start in range(0, len(ids), _PAGE_SIZE):
            batch = ids[start : start + _PAGE_SIZE]
            rows = await self._post_all_docs(batch, include_docs=True)
            for row in rows:
                doc = row.get("doc") if isinstance(row, dict) else None
                if isinstance(doc, dict) and _is_note_doc(doc):
                    index[str(doc["_id"])] = doc
        return index

    async def _current_index(self) -> dict[str, dict]:
        info = await self._db_info()
        update_seq = info.get("update_seq")
        if self._index is None or self._index_seq != update_seq:
            self._index = await self._load_index()
            self._index_seq = update_seq
        return self._index

    # -- StorageBackend: lifecycle -------------------------------------------

    async def startup_check(self) -> None:
        await self._db_info()
        milestone = await self._request(
            "GET", self._doc_path(_MILESTONE_DOC_ID)
        )
        if milestone.status_code != 200:
            if _is_db_missing(milestone):
                raise StorageUnavailable("CouchDB database not found")
            return  # no milestone document: pre-E2EE or fresh vault
        try:
            body = milestone.json()
        except ValueError:
            body = None
        tweak = body.get("tweak_values") if isinstance(body, dict) else None
        tweak = tweak if isinstance(tweak, dict) else {}
        if tweak.get("encrypt"):
            raise UnsupportedVault(
                "the vault uses LiveSync end-to-end encryption, "
                "which this server does not support"
            )
        if tweak.get("usePathObfuscation"):
            raise UnsupportedVault(
                "the vault uses path obfuscation, "
                "which this server does not support"
            )

    async def refresh(self) -> None:
        info = await self._db_info()
        self._index = await self._load_index()
        self._index_seq = info.get("update_seq")

    async def backend_info(self) -> dict:
        info = await self._db_info()
        return {
            "backend": "couchdb",
            "database": self._database,
            "doc_count": info.get("doc_count"),
            "update_seq": info.get("update_seq"),
        }

    async def close(self) -> None:
        await self._client.aclose()

    # -- StorageBackend: reads -----------------------------------------------

    async def list_notes(
        self,
        folder: str = "",
        extension: str | None = None,
        recursive: bool = True,
    ) -> list[NoteMeta]:
        index = await self._current_index()
        folder_prefix = "/".join(
            segment
            for segment in folder.replace("\\", "/").split("/")
            if segment not in ("", ".")
        )
        metas: list[NoteMeta] = []
        for doc in index.values():
            meta = self._meta_from_doc(doc)
            path = meta["path"]
            if folder_prefix:
                if not path.startswith(folder_prefix + "/"):
                    continue
                remainder = path[len(folder_prefix) + 1 :]
            else:
                remainder = path
            if not recursive and "/" in remainder:
                continue  # direct children only, also at the vault root
            if extension is not None and not _matches_extension(path, extension):
                continue
            metas.append(meta)
        metas.sort(key=lambda meta: meta["path"])
        return metas

    async def get_meta(self, path: str) -> NoteMeta:
        doc = await self._get_doc(path2id(path))
        if doc is None or not _is_note_doc(doc):
            raise NoteNotFound(f"note not found: {path}")
        return self._meta_from_doc(doc)

    async def read_note(self, path: str) -> NoteContent | None:
        doc = await self._get_doc(path2id(path))
        if doc is None or not _is_note_doc(doc):
            return None
        return await self._content_from_doc(doc)

    async def iterate_texts(
        self, extension: str = ".md"
    ) -> AsyncIterator[tuple[NoteMeta, str | None]]:
        index = await self._current_index()
        ordered = sorted(
            index.values(),
            key=lambda doc: str(doc.get("path") or doc.get("_id") or ""),
        )
        for doc in ordered:
            meta = self._meta_from_doc(doc)
            if not _matches_extension(meta["path"], extension):
                continue
            if not is_text_doc(doc.get("type"), meta["path"]):
                yield meta, None
                continue
            try:
                content = await self._content_from_doc(doc)
            except StorageError as exc:
                logger.error("skipping note %s: %s", meta["path"], exc)
                yield meta, None
                continue
            except Exception:
                logger.exception("skipping note %s: unexpected error", meta["path"])
                yield meta, None
                continue
            if content is None or content["text"] is None:
                yield meta, None
                continue
            yield meta, content["text"]

    # -- note decoding -------------------------------------------------------

    def _meta_from_doc(self, doc: dict) -> NoteMeta:
        doc_id = str(doc.get("_id", ""))
        path = doc.get("path")
        if not isinstance(path, str) or not path:
            path = doc_id
        size = doc.get("size")
        if isinstance(size, bool) or not isinstance(size, int):
            # Audit B8: legacy/service-ish docs may carry a null size.
            data = doc.get("data")
            size = len(data.encode("utf-8")) if isinstance(data, str) else 0
        return NoteMeta(
            path=path,
            doc_id=doc_id,
            ctime=_as_ms(doc.get("ctime")),
            mtime=_as_ms(doc.get("mtime")),
            size=size,
            type=str(doc.get("type") or ""),
        )

    async def _content_from_doc(self, doc: dict) -> NoteContent | None:
        meta = self._meta_from_doc(doc)
        path = meta["path"]
        doc_type = doc.get("type")

        if doc_type == "notes":
            # Legacy vaults keep the body in the document (audit B13).
            data = doc.get("data")
            if not isinstance(data, str):
                raise StorageError(
                    f"note {path!r} is corrupt: missing legacy data field"
                )
            return NoteContent(meta=meta, text=data, data=None, is_text=True)

        if doc_type not in ("plain", "newnote"):
            return None

        children = doc.get("children")
        if children is None:
            children = []
        if not isinstance(children, list) or not all(
            isinstance(child, str) for child in children
        ):
            raise StorageError(
                f"note {path!r} is corrupt: invalid children field"
            )
        if not children:
            if doc_type == "plain":
                return NoteContent(meta=meta, text="", data=None, is_text=True)
            return NoteContent(meta=meta, text=None, data=b"", is_text=False)

        datas = await self._fetch_chunk_datas(children, path)
        if doc_type == "plain":
            text = assemble_text(datas)
            return NoteContent(meta=meta, text=text, data=None, is_text=True)

        try:
            body = assemble_binary(datas)
        except ValueError as exc:
            logger.warning("note %s: %s", path, exc)
            raise StorageError(
                f"note {path!r} is corrupt: invalid chunk encoding"
            ) from None
        is_text = is_text_doc(doc_type, path)
        text: str | None = None
        if is_text:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                is_text = False
        return NoteContent(meta=meta, text=text, data=body, is_text=is_text)

    async def _fetch_chunk_datas(
        self, children: list[str], path: str
    ) -> list[str]:
        """Fetch chunk documents in one batch, in ``children`` order.

        The raised messages carry the note path only; chunk ids stay in
        the server log (audits C4/C7).
        """
        rows = await self._post_all_docs(children, include_docs=True)
        docs_by_id: dict[str, dict] = {}
        for row in rows:
            chunk = row.get("doc") if isinstance(row, dict) else None
            if isinstance(chunk, dict) and isinstance(chunk.get("_id"), str):
                docs_by_id[chunk["_id"]] = chunk
        datas: list[str] = []
        for child_id in children:
            chunk = docs_by_id.get(child_id)
            if (
                chunk is None
                or chunk.get("deleted")
                or not isinstance(chunk.get("data"), str)
            ):
                logger.warning(
                    "note %s: chunk %r is missing or corrupt", path, child_id
                )
                raise StorageError(
                    f"note {path!r} is corrupt: a content chunk is missing"
                )
            if chunk.get("e_"):
                logger.warning(
                    "note %s: chunk %r is E2EE-encrypted", path, child_id
                )
                raise UnsupportedVault(
                    f"note {path!r} uses end-to-end encryption, "
                    "which this server does not support"
                )
            datas.append(chunk["data"])
        return datas

    # -- StorageBackend: writes ----------------------------------------------

    @staticmethod
    def _refuse_unresolved_conflicts(doc: dict, path: str) -> None:
        """Refuse writes to a document with sibling revisions (C3)."""
        conflicts = doc.get("_conflicts")
        if conflicts:
            logger.warning(
                "note %s has %d unresolved conflicting revisions",
                path,
                len(conflicts) if isinstance(conflicts, list) else 1,
            )
            raise ConcurrentModification(
                f"note {path!r} has unresolved conflicts; resolve them "
                "in Obsidian (or another client) before writing"
            )

    async def _put_doc(self, doc_id: str, body: dict) -> str:
        """PUT a document and return its new revision.

        Success is a 2xx response whose body confirms the write; the
        caller may report success only once this returns (spec §7.6).
        Stale ``_rev`` bodies surface as CouchDB 409s, which ``_request``
        maps to ``ConcurrentModification``.
        """
        response = await self._request(
            "PUT", self._doc_path(doc_id), json_body=body
        )
        if response.status_code == 404:
            # A present database never 404s a document PUT.
            logger.warning("CouchDB PUT %s -> 404 %s", doc_id, response.text[:200])
            raise StorageUnavailable("CouchDB database not found")
        try:
            result = response.json()
        except ValueError:
            result = None
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or "rev" not in result
        ):
            logger.warning(
                "CouchDB PUT %s -> unexpected body %s", doc_id, response.text[:200]
            )
            raise StorageError("CouchDB rejected the document write")
        return str(result["rev"])

    async def _write_chunk(self, data: str) -> str:
        """PUT one content chunk (before any metadata change).

        Chunk ids are deterministic (``chunk_id_for``), so an existing
        chunk with the same id already holds exactly this data and is
        reused — upstream's content-addressed dedup. Chunks are never
        deleted or rewritten (audit B1); a PUT happens only when the
        chunk is absent, so rewriting identical content never conflicts.
        """
        chunk_id = chunk_id_for(data)
        if await self._get_doc(chunk_id) is not None:
            return chunk_id
        await self._put_doc(chunk_id, {"data": data, "type": "leaf"})
        return chunk_id

    async def _get_note_doc(self, path: str) -> dict:
        """Live metadata document for ``path`` (conflicts refused)."""
        doc = await self._get_doc(path2id(path), conflicts=True)
        if doc is None or not _is_note_doc(doc):
            raise NoteNotFound(f"note not found: {path}")
        self._refuse_unresolved_conflicts(doc, path)
        return doc

    async def _target_doc_for_write(
        self, new_path: str, *, overwrite: bool
    ) -> dict | None:
        """Existing target document, or ``None``; guards D2.

        A soft-deleted or missing target is free to write; its stored
        ``_rev`` is still needed to replace the revision.
        """
        target = await self._get_doc(path2id(new_path), conflicts=True)
        if target is not None:
            self._refuse_unresolved_conflicts(target, new_path)
            if not target.get("deleted") and not overwrite:
                raise NoteExists(f"note already exists: {new_path}")
        return target

    def _moved_body(self, doc: dict, new_path: str, mtime_ms: int) -> dict:
        """Metadata document publishing ``doc`` under ``new_path``.

        The ``children`` ids are copied verbatim — content never
        round-trips through decode/encode, so binary notes move
        chunk-wise (audit B4) — and ``ctime``/``size``/``type`` keep
        describing the same body.
        """
        meta = self._meta_from_doc(doc)
        children = [
            child
            for child in (doc.get("children") or [])
            if isinstance(child, str)
        ]
        body = new_metadata_doc(
            new_path,
            children,
            meta["size"],
            meta["type"] or doc_type_for(new_path),
            meta["ctime"],
            mtime_ms,
        )
        if doc.get("type") == "notes" and isinstance(doc.get("data"), str):
            body["data"] = doc["data"]  # legacy body lives in the doc
        return body

    async def _soft_delete(self, doc: dict, path: str, mtime_ms: int) -> None:
        revision = soft_delete_revision(doc, mtime_ms)
        revision.pop("_conflicts", None)
        await self._put_doc(path2id(path), revision)

    async def write_note(
        self,
        path: str,
        content: str,
        *,
        create: bool = False,
        overwrite: bool = False,
        mtime: int | None = None,
    ) -> NoteMeta:
        """Write a textual body under the central D1/D2 rules.

        Namespace validation (spec §7.5) and the binary-path refusal
        (spec §5) precede every HTTP request. New chunks are PUT before
        the metadata document; the metadata revision carries the old
        ``_rev`` (optimistic locking), and a soft-deleted note is only
        resurrected through an explicit ``create`` (audit B12).
        """
        validate_vault_path(path)
        if not is_text_doc(None, path):
            raise BinaryNoteError(
                f"note {path!r} has a binary extension; "
                "this server writes textual notes only"
            )

        doc_id = path2id(path)
        doc = await self._get_doc(doc_id, conflicts=True)
        if doc is not None:
            self._refuse_unresolved_conflicts(doc, path)
        live = doc is not None and not doc.get("deleted")
        if live and create and not overwrite:
            raise NoteExists(f"note already exists: {path}")
        if not live and not create:
            raise NoteNotFound(f"note not found: {path}")

        children = [await self._write_chunk(content)]
        new_mtime = _now_ms() if mtime is None else mtime
        size = len(content.encode("utf-8"))
        if doc is None:
            body = new_metadata_doc(
                path, children, size, doc_type_for(path), new_mtime, new_mtime
            )
        else:
            # New revision of the old body: children/mtime/size/type
            # replaced (a write always stores plain text — audit B3),
            # soft-delete flag and conflict shadows dropped.
            body = dict(doc)
            body.pop("_conflicts", None)
            body.pop("deleted", None)
            body["children"] = children
            body["mtime"] = new_mtime
            body["size"] = size
            body["type"] = doc_type_for(path)
        body["_rev"] = await self._put_doc(doc_id, body)
        return self._meta_from_doc(body)

    async def delete_note(self, path: str) -> bool:
        """Soft-delete the note; ``True`` iff a live note was deleted.

        Deletion is a new document revision with ``deleted: true``
        (``children``/``type``/``path`` preserved — audit B2), never a
        bare HTTP DELETE: a CouchDB tombstone would drop the body
        LiveSync needs to undo the deletion, and chunk deletion is
        reserved for LiveSync's own GC (audit B1).
        """
        validate_vault_path(path)
        doc = await self._get_doc(path2id(path), conflicts=True)
        if doc is None or doc.get("deleted"):
            return False
        self._refuse_unresolved_conflicts(doc, path)
        await self._soft_delete(doc, path, _now_ms())
        return True

    async def rename_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Move a note: republish its chunk references, then retire it.

        Same-path renames are no-ops and case-only renames update the
        ``path`` field in a single revision (D1 — no delete); otherwise
        a new metadata document with the *same* ``children`` ids is PUT
        first, and only then is the source soft-deleted, so a failed
        copy can duplicate a note but never lose one.
        """
        validate_vault_path(path)
        validate_vault_path(new_path)
        if path == new_path:
            return await self.get_meta(path)

        doc = await self._get_note_doc(path)
        if path2id(new_path) == path2id(path):
            # One document, two spellings: one revision, path updated.
            body = dict(doc)
            body.pop("_conflicts", None)
            body["path"] = new_path
            body["mtime"] = _now_ms()
            body["_rev"] = await self._put_doc(path2id(path), body)
            return self._meta_from_doc(body)

        target = await self._target_doc_for_write(new_path, overwrite=overwrite)
        body = self._moved_body(doc, new_path, _now_ms())
        if target is not None:
            body["_rev"] = target["_rev"]  # replace the old revision
        body["_rev"] = await self._put_doc(path2id(new_path), body)
        await self._soft_delete(doc, path, _now_ms())
        return self._meta_from_doc(body)

    async def copy_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Duplicate a note by re-publishing its chunk references."""
        validate_vault_path(path)
        validate_vault_path(new_path)
        doc = await self._get_note_doc(path)
        target = await self._target_doc_for_write(new_path, overwrite=overwrite)
        body = self._moved_body(doc, new_path, _now_ms())
        if target is not None:
            body["_rev"] = target["_rev"]  # replace the old revision
        body["_rev"] = await self._put_doc(path2id(new_path), body)
        return self._meta_from_doc(body)
