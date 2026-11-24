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

from scan_budget import ScanBudget
from text_eligibility import TextProjection, DEFAULT_TEXT_SCAN_MAX_BYTES

import httpx

from content_splitter import ContentSplitter, chunk_id_for_piece
from livesync_format import (
    ForbiddenPathError, EncryptedInlineChunk,
    clone_metadata, extract_inline_chunks, legacy_body,
    is_note_document, is_service_document_id,
    assemble_binary,
    assemble_text,
    canonical_path,
    resolve_content_splitter,
    prepare_file_content,
    new_metadata_doc,
    identity_path2id,
    validate_document_identity,
    resolve_path_identity,
    UnsupportedVaultSettings,
    UnsupportedWriterSettings,
    PATH_IDENTITY_LEGACY,
    soft_delete_revision,
    validate_vault_path,
)
from storage_base import (
    MAX_ATTACHMENT_BYTES,
    ConcurrentModification,
    RenamePartialError,
    UnresolvedConflicts,
    NoteContent,
    NoteExists,
    NoteMeta,
    NoteNotFound,
    StorageError,
    StorageUnavailable,
    UnsupportedVault,
    WriterUnsupported,
)

logger = logging.getLogger(__name__)

_MILESTONE_DOC_ID = "_local/obsydian_livesync_milestone"
_VERSION_DOC_ID = "obsydian_livesync_version"


# _all_docs page size for id scans and metadata batch fetches.
_PAGE_SIZE = 500
_TEXT_WINDOW_SIZE = 50

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


def _has_hidden_segment(path: str) -> bool:
    """Whether any path segment is a dotfile (``.obsidian``, ...)."""
    return any(segment.startswith(".") for segment in path.split("/"))


def _matches_extension(path: str, extension: str) -> bool:
    if not extension:
        return True
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
        scan_max_notes: int = 10000,
        scan_max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._scan_limits = (scan_max_notes, scan_max_bytes)
        self.text_scan_max_bytes = DEFAULT_TEXT_SCAN_MAX_BYTES
        self._text_verdicts = {}
        self._database = database
        self._path_identity = PATH_IDENTITY_LEGACY
        self._writer_unsupported_reason = None
        self._splitter = ContentSplitter()
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
            # Log the status and the body's error code only — never the
            # raw response body (log hygiene, audit C7's sibling).
            try:
                body = response.json()
            except ValueError:
                body = None
            error_code = (
                body.get("error") if isinstance(body, dict) else None
            )
            logger.warning(
                "CouchDB %s %s -> %s %s", method, path, code, error_code
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
        if isinstance(body, dict):
            self._validate_identity(body)
            return body
        return None

    def _validate_identity(self, doc: dict) -> None:
        try:
            validate_document_identity(doc, self._path_identity)
        except ValueError as exc:
            raise StorageError(str(exc)) from exc

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
        budget = ScanBudget(*self._scan_limits)
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
                    if isinstance(doc_id, str) and not is_service_document_id(doc_id):
                        if not budget.admit(doc_id, len(doc_id.encode())):
                            self.index_truncated = True
                            return ids
                        ids.append(doc_id)
                if len(rows) < _PAGE_SIZE:
                    break
                skip += _PAGE_SIZE
        return ids

    async def _load_index(self) -> dict[str, dict]:
        self.index_truncated = False
        ids = await self._note_doc_ids()
        budget = ScanBudget(*self._scan_limits)
        index: dict[str, dict] = {}
        for start in range(0, len(ids), _PAGE_SIZE):
            batch = ids[start : start + _PAGE_SIZE]
            rows = await self._post_all_docs(batch, include_docs=True)
            for row in rows:
                doc = row.get("doc") if isinstance(row, dict) else None
                if isinstance(doc, dict) and is_note_document(doc):
                    self._validate_identity(doc)
                    path = doc.get("path")
                    path = (
                        path
                        if isinstance(path, str) and path
                        else str(doc.get("_id", ""))
                    )
                    if _has_hidden_segment(path):
                        continue  # hidden files never surface as notes
                    # Body/chunk/eden payloads must never live in the index.
                    try:
                        meta = self._meta_from_doc(doc)
                    except StorageError:
                        logger.exception("skipping note %s: invalid metadata", path)
                        continue
                    compact = {key: doc[key] for key in
                               ("_id", "_rev", "path", "type", "ctime", "mtime")
                               if key in doc}
                    compact["size"] = meta["size"]
                    if not budget.admit(path, len(json.dumps(compact).encode())):
                        self.index_truncated = True
                        return index
                    index[str(doc["_id"])] = compact
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
        self._writer_unsupported_reason = None
        self._splitter = ContentSplitter()
        await self._db_info()
        version_doc = await self._get_doc(_VERSION_DOC_ID)
        if version_doc is not None:
            # Informational only: any value is accepted (audit B5
            # restricts behavior, not versions).
            logger.info(
                "vault advertises an obsydian_livesync_version document: %r",
                {
                    key: value
                    for key, value in version_doc.items()
                    if not key.startswith("_")
                },
            )
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
        body = body if isinstance(body, dict) else {}
        try:
            self._path_identity = resolve_path_identity(body)
            self._splitter = resolve_content_splitter(body)
        except UnsupportedWriterSettings as exc:
            self._splitter = None
            self._writer_unsupported_reason = str(exc)
        except UnsupportedVaultSettings as exc:
            raise UnsupportedVault(str(exc)) from exc

    def _path2id(self, path: str) -> str:
        return identity_path2id(path, self._path_identity)

    async def refresh(self, force: bool = False) -> None:
        """Reload metadata only on explicit refresh; reads query CouchDB directly.

        Index consumers retain their update-sequence check in _current_index.
        """
        if not force:
            return
        info = await self._db_info()
        self._index = await self._load_index()
        self._index_seq = info.get("update_seq")

    async def backend_info(self) -> dict:
        info = await self._db_info()
        return {
            "writer_supported": self._writer_unsupported_reason is None,
            "writer_unsupported_reason": self._writer_unsupported_reason,
            "backend": "couchdb",
            "path_identity": self._path_identity,
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
                path_key = self._path2id(path)
                folder_key = self._path2id(folder_prefix)
                if not path_key.startswith(folder_key + "/"):
                    continue
                remainder = path_key[len(folder_key) + 1 :]
            else:
                remainder = path
            if not recursive and "/" in remainder:
                continue  # direct children only, also at the vault root
            if extension is not None and not _matches_extension(path, extension):
                continue
            metas.append(meta)
        metas.sort(key=lambda meta: meta["path"])
        return metas

    @staticmethod
    def _validate_read_path(path: str) -> None:
        """Read-side namespace gate (same policy as writes).

        ``.obsidian/...`` documents are plain-typed rows in the
        database; without this check they would be directly readable
        and listed as notes. Forbidden paths report the note as absent
        (``NoteNotFound`` / ``None``) with the reason logged
        server-side, and no HTTP request is issued.
        """
        try:
            validate_vault_path(path)
        except ForbiddenPathError as exc:
            logger.info("read refused for vault path %r: %s", path, exc)
            raise NoteNotFound(f"note not found: {path}") from None

    async def get_meta(self, path: str) -> NoteMeta:
        self._validate_read_path(path)
        doc = await self._get_doc(self._path2id(path))
        if doc is None or not is_note_document(doc):
            raise NoteNotFound(f"note not found: {path}")
        return self._meta_from_doc(doc)

    async def read_note(self, path: str) -> NoteContent | None:
        try:
            validate_vault_path(path)
        except ForbiddenPathError as exc:
            logger.info("read refused for vault path %r: %s", path, exc)
            return None
        doc = await self._get_doc(self._path2id(path))
        if doc is None or not is_note_document(doc):
            return None
        return await self._content_from_doc(doc)

    async def read_text_note(self, path: str) -> NoteContent | None:
        self._validate_read_path(path)
        doc = await self._get_doc(self._path2id(path))
        if doc is None or not is_note_document(doc):
            return None
        if doc.get('type') != 'newnote':
            return await self._content_from_doc(doc)
        text = await self._scan_newnote(doc)
        meta = self._meta_from_doc(doc)
        return NoteContent(meta=meta, text=text, data=None,
                           is_text=text is not None, rev=meta['rev'])

    async def iterate_texts(
        self, extension: str = ""
    ) -> AsyncIterator[tuple[NoteMeta, str | None]]:
        index = await self._current_index()
        ordered = sorted(
            index.values(),
            key=lambda doc: str(doc.get("path") or doc.get("_id") or ""),
        )
        candidates = [doc for doc in ordered if _matches_extension(
            str(doc.get("path") or doc.get("_id") or ""), extension
        )]
        for start in range(0, len(candidates), _TEXT_WINDOW_SIZE):
            window = candidates[start:start + _TEXT_WINDOW_SIZE]
            try:
                rows = await self._post_all_docs(
                    [doc["_id"] for doc in window], include_docs=True
                )
            except Exception:
                logger.exception("unable to fetch scan metadata window")
                for doc in window:
                    yield self._meta_from_doc(doc), None
                continue
            docs = {row["doc"]["_id"]: row["doc"] for row in rows
                    if isinstance(row, dict) and isinstance(row.get("doc"), dict)
                    and is_note_document(row["doc"])}
            missing = set()
            for doc in docs.values():
                if doc.get("type") == "newnote":
                    continue  # Classify lazily; never prefetch a whole binary.
                children = doc.get("children")
                if not isinstance(children, list) or not all(isinstance(c, str) for c in children):
                    continue
                try:
                    self._validate_read_path(self._meta_from_doc(doc)["path"])
                    inline = extract_inline_chunks(doc)
                except (StorageError, ValueError):
                    continue  # decoding below isolates malformed notes
                missing.update(child for child in children if child not in inline)
            chunks = {}
            chunk_rows = []
            content = None
            keys = sorted(missing)
            for offset in range(0, len(keys), _PAGE_SIZE):
                try:
                    chunk_rows = await self._post_all_docs(
                        keys[offset:offset + _PAGE_SIZE], include_docs=True
                    )
                    for row in chunk_rows:
                        chunk = row.get("doc") if isinstance(row, dict) else None
                        if isinstance(chunk, dict) and isinstance(chunk.get("_id"), str):
                            chunks[chunk["_id"]] = chunk
                except Exception:
                    logger.exception("unable to fetch scan chunk batch")
            for indexed in window:
                meta = self._meta_from_doc(indexed)
                try:
                    self._validate_read_path(meta["path"])
                    doc = docs.get(indexed["_id"])
                    if doc is not None:
                        self._validate_read_path(self._meta_from_doc(doc)["path"])
                    if doc is not None and doc.get("type") == "newnote":
                        yield self._meta_from_doc(doc), await self._scan_newnote(doc)
                        continue
                    content = (await self._content_from_doc(doc, chunks)
                               if doc is not None else None)
                    yield (content["meta"], content["text"]) if content else (meta, None)
                except Exception:
                    logger.exception("skipping note %s", meta["path"])
                    yield meta, None
            # No body-bearing window survives into the next HTTP fetch.
            del docs, rows, chunks, chunk_rows, doc, content
            inline = row = chunk = None

    async def _scan_newnote(self, doc: dict) -> str | None:
        key = (doc['_id'], doc['_rev'], self.text_scan_max_bytes)
        if self._text_verdicts.get(key) is False:
            return None
        if doc.get('e_'):
            raise UnsupportedVault('encrypted note')
        children = doc.get('children') or []
        if not isinstance(children, list) or not all(isinstance(c, str) for c in children):
            raise StorageError('invalid children field')
        projection = TextProjection(self.text_scan_max_bytes)
        for index, child in enumerate(children):
            piece = (await self._fetch_chunk_datas([child], doc['path'], doc.get('eden')))[0]
            if not projection.feed(piece, final=index == len(children) - 1):
                self._text_verdicts[key] = False
                return None
        self._text_verdicts[key] = True
        return projection.text()

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
        rev = doc.get("_rev")
        if not isinstance(rev, str) or not rev:
            raise StorageError("existing note has no revision token")
        return NoteMeta(
            path=path,
            doc_id=doc_id,
            ctime=_as_ms(doc.get("ctime")),
            mtime=_as_ms(doc.get("mtime")),
            size=size,
            type=str(doc.get("type") or ""),
            rev=rev,
        )

    async def _content_from_doc(self, doc: dict, chunks: dict | None = None) -> NoteContent | None:
        meta = self._meta_from_doc(doc)
        path = meta["path"]
        doc_type = doc.get("type")

        try:
            data = legacy_body(doc)
        except ValueError:
            raise StorageError(f"note {path!r} is corrupt: missing legacy data field") from None
        if data is not None:
            return NoteContent(
                meta=meta, text=data, data=None, is_text=True,
                rev=meta["rev"],
            )

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
                return NoteContent(
                    meta=meta, text="", data=None, is_text=True,
                    rev=meta["rev"],
                )
            return NoteContent(
                meta=meta, text=None, data=b"", is_text=False,
                rev=meta["rev"],
            )

        datas = await self._fetch_chunk_datas(children, path, doc.get("eden"), chunks)
        if doc_type == "plain":
            text = assemble_text(datas)
            return NoteContent(
                meta=meta, text=text, data=None, is_text=True,
                rev=meta["rev"],
            )

        try:
            body = assemble_binary(datas)
        except ValueError as exc:
            logger.warning("note %s: %s", path, exc)
            raise StorageError(
                f"note {path!r} is corrupt: invalid chunk encoding"
            ) from None
        return NoteContent(
            meta=meta, text=None, data=body, is_text=False,
            rev=meta["rev"],
        )

    async def _fetch_chunk_datas(
        self, children: list[str], path: str, eden: object = None,
        prefetched: dict | None = None
    ) -> list[str]:
        """Fetch chunk documents in one batch, in ``children`` order.

        The raised messages carry the note path only; chunk ids stay in
        the server log (audits C4/C7).
        """
        try:
            inline = extract_inline_chunks({"children": children, "eden": eden})
        except EncryptedInlineChunk:
            raise UnsupportedVault(f"note {path!r} uses end-to-end encryption") from None
        except ValueError:
            raise StorageError(f"note {path!r} is corrupt: invalid eden field") from None
        docs_by_id = {key: {"data": data} for key, data in inline.items()}
        missing = [child for child in children if child not in docs_by_id]
        rows = (await self._post_all_docs(missing, include_docs=True)
                if missing and prefetched is None else [])
        if prefetched is not None:
            docs_by_id.update({key: prefetched[key] for key in missing
                               if key in prefetched})
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
            raise UnresolvedConflicts(
                f"note {path!r} has unresolved conflicts; resolve them "
                "in Obsidian (or another client) before writing"
            )

    @staticmethod
    def _check_expected_rev(
        doc: dict | None, path: str, expected_rev: str | None
    ) -> None:
        """Read-through lost-update precondition.

        ``expected_rev`` is the revision the caller read; when the
        current winner differs, or the document appeared/vanished since,
        the caller must re-read and retry instead of clobbering. Checked
        against the GET performed inside the write, before any PUT.
        """
        if expected_rev is None:
            return
        if doc is None or doc.get("_rev") != expected_rev:
            logger.info(
                "note %s: expected revision no longer current; "
                "refusing the write",
                path,
            )
            raise ConcurrentModification(
                "note changed concurrently; re-read and retry"
            )

    async def _put_doc(self, doc_id: str, body: dict) -> str:
        """PUT a document and return its new revision.

        Durability requires CouchDB's explicit acknowledgement: HTTP 201
        with an ``ok``/``rev`` body (spec §7.6). Any other status — 202
        "Accepted" included — means the write is not confirmed on disk
        and is treated as a failure, so no caller ever reports success
        on an undurable write. Stale ``_rev`` bodies surface as CouchDB
        409s, which ``_request`` maps to ``ConcurrentModification``.
        """
        response = await self._request(
            "PUT", self._doc_path(doc_id), json_body=body
        )
        if response.status_code == 404:
            # A present database never 404s a document PUT.
            logger.warning("CouchDB PUT %s -> 404 (database gone)", doc_id)
            raise StorageUnavailable("CouchDB database not found")
        if response.status_code != 201:
            logger.warning(
                "CouchDB PUT %s -> %s (not 201: not confirmed durable)",
                doc_id,
                response.status_code,
            )
            raise StorageUnavailable("CouchDB did not confirm durable write")
        try:
            result = response.json()
        except ValueError:
            result = None
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or not isinstance(result.get("rev"), str)
            or not result["rev"]
        ):
            logger.warning(
                "CouchDB PUT %s -> unexpected acknowledgement body", doc_id
            )
            raise StorageError("CouchDB rejected the document write")
        return result["rev"]

    async def _write_chunk(self, path: str, data: str) -> str:
        """PUT one content chunk (before any metadata change).

        Chunk ids are deterministic (``chunk_id_for_piece``), so a chunk
        already stored under the computed id *should* hold exactly this
        data — but it is verified, never trusted: a differing or corrupt
        chunk under the id (a hash collision, or a
        damaged shared chunk) must not be published as the note's
        content. On mismatch the write aborts with the metadata
        untouched; the stored chunk is never overwritten or "repaired"
        because other notes may reference it. Chunks are never deleted
        or rewritten (audit B1); a PUT happens only when the chunk is
        absent, so rewriting identical content never conflicts.
        """
        chunk_id = chunk_id_for_piece(data, hash_alg=self._splitter.hash_alg)
        existing = await self._get_doc(chunk_id)
        if existing is not None:
            if (
                not existing.get("e_")
                and not existing.get("deleted")
                and existing.get("type") == "leaf"
                and existing.get("data") == data
            ):
                return chunk_id  # content-addressed reuse, no PUT
            logger.warning(
                "note %s: chunk %r under the computed id holds different "
                "data; refusing to reuse or overwrite it",
                path,
                chunk_id,
            )
            raise StorageError(
                f"note {path!r} cannot be written: an existing shared "
                "content chunk is encrypted or holds different data"
            )
        try:
            await self._put_doc(chunk_id, {"data": data, "type": "leaf"})
        except ConcurrentModification:
            # Another writer may have published the same immutable chunk
            # after our absence check. Never overwrite the winning leaf.
            winner = await self._get_doc(chunk_id)
            if not (
                winner is not None
                and not winner.get("e_")
                and not winner.get("deleted")
                and winner.get("type") == "leaf"
                and winner.get("data") == data
            ):
                raise ConcurrentModification(
                    f"note {path!r} cannot be written: content chunk is encrypted or changed concurrently"
                ) from None
        return chunk_id

    async def _get_note_doc(self, path: str) -> dict:
        """Live metadata document for ``path`` (conflicts refused)."""
        doc = await self._get_doc(self._path2id(path), conflicts=True)
        if doc is None or not is_note_document(doc):
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
        target = await self._get_doc(self._path2id(new_path), conflicts=True)
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
        body = clone_metadata(
            doc, _id=self._path2id(new_path), path=canonical_path(new_path),
            mtime=mtime_ms,
        )
        body.pop("_rev", None)
        body.pop("_conflicts", None)
        return body

    async def _soft_delete(self, doc: dict, path: str, mtime_ms: int) -> None:
        revision = soft_delete_revision(doc, mtime_ms)
        revision.pop("_conflicts", None)
        await self._put_doc(self._path2id(path), revision)

    def check_writer_support(self) -> None:
        if self._writer_unsupported_reason is not None:
            raise WriterUnsupported(self._writer_unsupported_reason)

    async def write_note(
        self,
        path: str,
        content: str | bytes,
        *,
        create: bool = False,
        overwrite: bool = False,
        mtime: int | None = None,
        expected_rev: str | None = None,
    ) -> NoteMeta:
        """Ingest file content using device path-based disk-reading rules.

        Only explicit create resurrects a tombstone. expected_rev protects
        read-modify-write operations before publishing any chunks.
        """
        self.check_writer_support()
        validate_vault_path(path)
        body, doc_type, size = prepare_file_content(path, content)
        return await self._write_content(
            path, body, size, doc_type,
            create=create, overwrite=overwrite, mtime=mtime,
            expected_rev=expected_rev,
        )

    async def write_attachment(
        self, path: str, data: bytes, *, create: bool = False,
        overwrite: bool = False, expected_rev: str | None = None,
    ) -> NoteMeta:
        """Capped byte-input alias for write_note; type still follows path."""
        self.check_writer_support()
        validate_vault_path(path)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment exceeds 32 MiB cap ({MAX_ATTACHMENT_BYTES} bytes); "
                f"actual size {len(data)} bytes"
            )
        return await self.write_note(
            path, data, create=create, overwrite=overwrite,
            expected_rev=expected_rev,
        )

    async def _write_content(
        self, path: str, content: str | bytes, size: int, doc_type: str, *,
        create: bool, overwrite: bool, mtime: int | None = None,
        expected_rev: str | None = None,
    ) -> NoteMeta:
        """Publish encoded chunk data through the shared write transaction."""
        doc_id = self._path2id(path)
        doc = await self._get_doc(doc_id, conflicts=True)
        if doc is not None:
            self._refuse_unresolved_conflicts(doc, path)
        self._check_expected_rev(doc, path, expected_rev)
        live = doc is not None and not doc.get("deleted")
        if live and create and not overwrite:
            raise NoteExists(f"note already exists: {path}")
        if not live and not create:
            raise NoteNotFound(f"note not found: {path}")

        pieces = [piece for piece in self._splitter.split(content, path=canonical_path(path)) if piece]
        # P-10 / EntryManagerImpls.ts:280: retain ingested Blob.size even
        # when a legacy splitter's Blob.text() removes an initial BOM.
        children = [await self._write_chunk(path, piece) for piece in pieces]
        new_mtime = _now_ms() if mtime is None else mtime
        if doc is None:
            body = new_metadata_doc(
                path, children, size, doc_type, new_mtime, new_mtime
            )
        else:
            # New revision of the old body: children/mtime/size/type
            # replaced with the encoded body format,
            # soft-delete flag and conflict shadows dropped.
            body = clone_metadata(doc)
            body.pop("_conflicts", None)
            body.pop("deleted", None)
            body["eden"] = {}
            body["children"] = children
            body["mtime"] = new_mtime
            body["size"] = size
            body["type"] = doc_type
        body["_id"] = doc_id
        body["path"] = canonical_path(path)
        body["_rev"] = await self._put_doc(doc_id, body)
        return self._meta_from_doc(body)

    async def delete_note(
        self, path: str, expected_rev: str | None = None
    ) -> bool:
        """Soft-delete the note; ``True`` iff a live note was deleted.

        Deletion is a new document revision with ``deleted: true``
        (``children``/``type``/``path`` preserved — audit B2), never a
        bare HTTP DELETE: a CouchDB tombstone would drop the body
        LiveSync needs to undo the deletion, and chunk deletion is
        reserved for LiveSync's own GC (audit B1). ``expected_rev``
        guards the read-through race exactly as in ``write_note``.
        """
        self.check_writer_support()
        validate_vault_path(path)
        doc = await self._get_doc(self._path2id(path), conflicts=True)
        self._check_expected_rev(doc, path, expected_rev)
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
        self.check_writer_support()
        validate_vault_path(path)
        validate_vault_path(new_path)
        new_path = canonical_path(new_path)
        if path == new_path:
            return await self.get_meta(path)

        doc = await self._get_note_doc(path)
        if self._path2id(new_path) == self._path2id(path):
            # One document, two spellings: one revision, path updated.
            body = clone_metadata(doc)
            body.pop("_conflicts", None)
            body["path"] = new_path
            body["mtime"] = _now_ms()
            body["_rev"] = await self._put_doc(self._path2id(path), body)
            return self._meta_from_doc(body)

        target = await self._target_doc_for_write(new_path, overwrite=overwrite)
        body = self._moved_body(doc, new_path, _now_ms())
        if target is not None:
            body["_rev"] = target["_rev"]  # replace the old revision
        body["_rev"] = await self._put_doc(self._path2id(new_path), body)
        try:
            await self._soft_delete(doc, path, _now_ms())
        except Exception as exc:
            logger.exception("rename source retirement failed for %s", path)
            source_state = "unknown"
            try:
                source = await self._get_doc(self._path2id(path))
                if source is not None and is_note_document(source):
                    source_state = "live"
            except Exception:
                logger.exception("unable to confirm rename source state for %s", path)
            raise RenamePartialError(path, new_path, source_state=source_state) from exc
        return self._meta_from_doc(body)

    async def copy_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Duplicate a note by re-publishing its chunk references."""
        self.check_writer_support()
        validate_vault_path(path)
        validate_vault_path(new_path)
        doc = await self._get_note_doc(path)
        target = await self._target_doc_for_write(new_path, overwrite=overwrite)
        body = self._moved_body(doc, new_path, _now_ms())
        if target is not None:
            body["_rev"] = target["_rev"]  # replace the old revision
        body["_rev"] = await self._put_doc(self._path2id(new_path), body)
        return self._meta_from_doc(body)
