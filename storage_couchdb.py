"""CouchDB implementation of the ``StorageBackend`` protocol — read path.

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

The write path (write/delete/rename/copy) is Task 6 and raises
``NotImplementedError`` here.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import AsyncIterator

import httpx

from livesync_format import (
    assemble_binary,
    assemble_text,
    is_text_doc,
    path2id,
)
from storage_base import (
    ConcurrentModification,
    NoteContent,
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

    async def _get_doc(self, doc_id: str) -> dict | None:
        response = await self._request("GET", self._doc_path(doc_id))
        if response.status_code == 404:
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
                if not recursive and "/" in path[len(folder_prefix) + 1 :]:
                    continue
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

    # -- StorageBackend: writes (Task 6) --------------------------------------

    async def write_note(
        self,
        path: str,
        content: str,
        *,
        create: bool = False,
        overwrite: bool = False,
        mtime: int | None = None,
    ) -> NoteMeta:
        raise NotImplementedError("write_note is not implemented yet")

    async def delete_note(self, path: str) -> None:
        raise NotImplementedError("delete_note is not implemented yet")

    async def rename_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        raise NotImplementedError("rename_note is not implemented yet")

    async def copy_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        raise NotImplementedError("copy_note is not implemented yet")
