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
  internal URLs and revisions; default logs omit payloads — audit C7/P-19.

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

from publication_notices import complete_publication, ingress_notices, split_notices

from diagnostic_hygiene import (recommendation_fields, writer_diagnostic, diagnostic, settings_exception_diagnostic, PublicDiagnostic, DiagnosticPath, setting_key,
    DiagnosticCode, assessment_fields, setting_value, fingerprint, unsafe_debug,
    cause_name, operation_field, numeric_field)

from collections import OrderedDict, deque
import asyncio
from dataclasses import dataclass
from device_selection import DeviceSelection

import json
import logging
import time
import zlib
import urllib.parse
from typing import AsyncIterator, Callable
from config import validate_transport_headers, CouchDBConfig
from couchdb_jwt import JWTTokenGenerator

from scan_budget import ScanBudget, active_scan_budget
from text_eligibility import TextAccessPolicy, project_note, scan_text
from storage_base import TextBudgetExceeded
from text_eligibility import (TextProjection, DEFAULT_TEXT_SCAN_MAX_BYTES,
                              DEFAULT_TEXT_VERDICT_CACHE_ENTRIES)

from couchdb_compression import (compress_doc, decompress_doc, decompress_data,
                                 CompressedDataError, CouchDBInflateBudgetExceeded, MARK_SHIFT_COMPRESSED, resolved_inflate_budget)
from wire_encoding import json_stringify

import base64

import httpx

from content_splitter import ContentSplitter, SplitterRefusal, _js_truthy, chunk_id_for_piece
from livesync_format import (
    ForbiddenPathError, EncryptedInlineChunk,
    clone_metadata, extract_inline_chunks, legacy_body,
    is_note_document, is_service_document_id,
    assemble_binary,
    assemble_text,
    canonical_path,
    resolve_content_splitter, resolve_tweak_startup,
    resolve_writer_policy, writer_policy_summary, resolve_enable_compression,
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
    WriterParameterRefused,
    check_file_size,
    WriteSkipped,
    RenameRefused,
    ConcurrentModification,
    RenamePartialError,
    UnresolvedConflicts,
    NoteContent,
    NoteExists,
    NoteMeta,
    NoteNotFound,
    StorageError, OutcomeUnknown, MutationNotApplied,
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


# C:pouchdb/LiveSyncLocalDB.ts:638-656 separates get/allDocs reads from
# put/bulkDocs publication. RemoteService.ts:135-159,373-381 observes body
# failures after fetch; HTTP POST alone does not establish publication.
# Published pouchdb-adapter-http 9.0.0 lib/index.es.js:597-654,743-773,
# 851-861,1013-1019: DELETE/attachment reads, bulk/put, allDocs POST reads,
# and changes feeds; :278-287,1141-1145 creates/deletes databases. This table is the peer safety contract, not a wire oracle.
# One closed inventory for all request/error paths; no verb-based fallback.
_REQUEST_PURPOSES = {
    ('root', 'GET'): 'read', ('root', 'HEAD'): 'read',
    ('session', 'GET'): 'read', ('session', 'HEAD'): 'read',
    ('database', 'GET'): 'read', ('database', 'HEAD'): 'read',
    ('database', 'PUT'): 'write', ('database', 'POST'): 'write',
    ('database', 'DELETE'): 'write',
    ('db-info', 'GET'): 'read',
    ('changes', 'GET'): 'read', ('changes', 'POST'): 'read',
    ('all-docs', 'GET'): 'read', ('all-docs', 'POST'): 'read',
    ('bulk-docs', 'GET'): 'read', ('bulk-docs', 'POST'): 'write',
    ('document', 'GET'): 'read', ('document', 'HEAD'): 'read',
    ('document', 'PUT'): 'write', ('document', 'POST'): 'write',
    ('document', 'DELETE'): 'write',
    ('local', 'GET'): 'read', ('local', 'HEAD'): 'read',
    ('local', 'PUT'): 'write', ('local', 'DELETE'): 'write',
    ('leaf', 'GET'): 'read', ('leaf', 'HEAD'): 'read',
    ('leaf', 'PUT'): 'write-leaf', ('leaf', 'DELETE'): 'write-leaf',
    ('attachment', 'GET'): 'read', ('attachment', 'HEAD'): 'read',
    ('attachment', 'PUT'): 'write', ('attachment', 'DELETE'): 'write',
}


class CouchDBRequestPurposeRefused(StorageUnavailable):
    """An undeclared operation cannot be sent safely; no remote fields escape."""

    def format(self):
        return (super().format() + '; request purpose is undeclared; no request sent; '
                'upgrade the peer to declare this operation before retrying')


class CouchDBHTTPBudgetExceeded(CouchDBInflateBudgetExceeded):
    """HTTP budget refusal, including possibly committed write responses."""

    def format(self):
        return (super().format() + '; HTTP wire/decoded response budget; '
                'if publishing, re-read the note before retrying')


class CouchDBHTTPEncodingRefused(StorageError):
    """Closed transport diagnostic; never echo a remote encoding or body."""

    def format(self):
        return ('CouchDB HTTP Content-Encoding is unsupported or malformed; '
                'configure the server/proxy to return identity encoding and retry; '
                'if publishing, re-read the note before retrying')


@dataclass(frozen=True)
class _HTTPReadDiagnostic(PublicDiagnostic):
    """HTTP 408 on a read-only POST cannot imply a publication outcome."""

    def status_guidance(self):
        if self.status == 408:
            return 'the read request timed out; retry the read'
        return super().status_guidance()


@dataclass(frozen=True)
class _HTTPBodyDiagnostic(PublicDiagnostic):
    """Closed response recovery fields survive source-removal reclassification."""

    budget_bytes: int | None = None

    def format(self):
        text = super().format()
        if self.cause is CouchDBHTTPBudgetExceeded:
            text += '; ' + CouchDBHTTPBudgetExceeded(self.budget_bytes).format()
        elif self.cause is CouchDBHTTPEncodingRefused:
            text += '; ' + CouchDBHTTPEncodingRefused().format()
        return text


async def _bounded_http_body(response: httpx.Response, limit: int) -> bytes:
    """Bound wire input and zlib output *before* retaining decoded bytes.

    RemoteService.ts:340-380 delegates to fetch; these are peer operational
    protections, not device format limits. aiter_raw bypasses HTTPX decoders.
    No flush() is used: its output allocation cannot be length-bounded.
    """
    encoding = response.headers.get('content-encoding', 'identity').strip().lower()
    if encoding not in {'identity', 'gzip', 'deflate'}:
        raise CouchDBHTTPEncodingRefused()
    # In-memory MockTransport responses may already have been consumed by
    # their constructor. Real streaming HTTP transports never take this path.
    if response.is_stream_consumed:
        if len(response.content) > limit:
            raise CouchDBHTTPBudgetExceeded(limit)
        return response.content
    decoder = None if encoding == 'identity' else zlib.decompressobj(
        31 if encoding == 'gzip' else 15)
    raw_size = 0
    decoded = bytearray()
    first_deflate = True
    deflate_prefix = bytearray()
    try:
        async for block in response.aiter_raw():
            raw_size += len(block)
            if raw_size > limit:
                raise CouchDBHTTPBudgetExceeded(limit)
            if decoder is None:
                decoded.extend(block)
                if len(decoded) > limit:
                    raise CouchDBHTTPBudgetExceeded(limit)
                continue
            # HTTP deflate can use either RFC1950 or raw RFC1951 (HTTPX
            # DeflateDecoder does the same). Detect from the two-byte header
            # without replaying an arbitrarily large first compressed block.
            if encoding == 'deflate' and first_deflate:
                used = min(len(block), 2 - len(deflate_prefix))
                deflate_prefix.extend(block[:used])
                # Hold a one-byte initial block until the header is available.
                if len(deflate_prefix) < 2:
                    continue
                cmf, flg = deflate_prefix
                wrapped = (cmf & 15 == 8 and cmf >> 4 <= 7 and
                           (cmf * 256 + flg) % 31 == 0)
                decoder = zlib.decompressobj(15 if wrapped else -15)
                block = bytes(deflate_prefix) + block[used:]
                first_deflate = False
            pending = block
            while pending:
                if decoder.eof:
                    if encoding != 'gzip':
                        raise CouchDBHTTPEncodingRefused()
                    decoder = zlib.decompressobj(31)  # concatenated gzip members
                piece = decoder.decompress(pending, min(65536, limit - len(decoded) + 1))
                decoded.extend(piece)
                if len(decoded) > limit:
                    raise CouchDBHTTPBudgetExceeded(limit)
                pending = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
        if decoder is not None and not decoder.eof:
            raise CouchDBHTTPEncodingRefused()
    except zlib.error:
        raise CouchDBHTTPEncodingRefused() from None
    return bytes(decoded)


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


class CouchDBBackend(TextAccessPolicy, DeviceSelection):
    """LiveSync vault storage served by a CouchDB instance."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        database: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        custom_headers: tuple[tuple[str, str], ...] = (),
        jwt_config: CouchDBConfig | None = None,
        jwt_clock: Callable[[], float] = time.time,
        text_verdict_cache_entries: int = DEFAULT_TEXT_VERDICT_CACHE_ENTRIES,
        connect_timeout_seconds: float = 5.0,
        io_timeout_seconds: float = 30.0,
        scan_max_notes: int = 0,
        scan_max_bytes: int = 0,
        scan_max_seconds: float = 20,
        scan_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        validate_transport_headers(custom_headers, "COUCHDB_CUSTOM_HEADERS")
        if text_verdict_cache_entries <= 0:
            raise ValueError("text verdict cache entries must be positive")
        self.couchdb_max_inflated_bytes = None
        self.text_verdict_cache_entries = text_verdict_cache_entries
        self._scan_limits = (scan_max_notes, scan_max_bytes)
        self.scan_max_seconds = scan_max_seconds
        self._scan_clock = scan_clock
        self.text_scan_max_bytes = None
        self._text_verdicts = OrderedDict()
        self._jwt = JWTTokenGenerator(jwt_config, clock=jwt_clock) if jwt_config is not None else None
        self._custom_headers = dict(custom_headers)
        self._auth_header = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self._database = database
        self._path_identity = PATH_IDENTITY_LEGACY
        self._tweak_assessment = {"status": "OK", "differing_keys": []}
        self._writer_unsupported_reason = None
        self._splitter = ContentSplitter()
        self.sync_max_size_mb = 50
        self.enable_compression = False
        self.sync_max_size_mb_override = None
        self._client = httpx.AsyncClient(
            base_url=url if url.endswith("/") else url + "/",
            auth=None if self._jwt else (username, password),
            transport=transport,
            timeout=httpx.Timeout(connect_timeout_seconds, read=io_timeout_seconds,
                                  write=io_timeout_seconds, pool=io_timeout_seconds),
        )
        if custom_headers or self._jwt:
            # AsyncClient imports httpcore lazily: install filters only after
            # constructing its transport, including on the first connection.
            from diagnostic_hygiene import silence_transport_logging
            silence_transport_logging()
        # Note metadata index (doc id -> metadata doc), cached per
        # update_seq (audit C5).
        self._index: dict[str, dict] | None = None
        self._index_seq: object | None = None
        self._index_lock = asyncio.Lock()
        self._reset_index_progress()

    # -- low-level HTTP -----------------------------------------------------

    @property
    def _db_path(self) -> str:
        return urllib.parse.quote(self._database, safe="")

    def _doc_path(self, doc_id: str) -> str:
        return f"{self._db_path}/{urllib.parse.quote(doc_id, safe='')}"

    def _request_purpose(self, method: str, path: str) -> str:
        # HTTPX accepts both database-relative and root-relative paths. Strip
        # one leading separator for classification only, never a network path.
        path = path.removeprefix("/")
        # Match wire-encoded document IDs before unquoting: _local/id is
        # encoded by _doc_path, while attachment separators remain literal.
        if path in {'', '/'}:
            family = 'root'
        elif path == '_session':
            family = 'session'
        elif path == self._db_path:
            family = 'database'
        elif path.startswith(self._db_path + '/'):
            tail = path[len(self._db_path) + 1:]
            special = {'_all_docs': 'all-docs', '_bulk_docs': 'bulk-docs',
                       '_changes': 'changes', '_db_info': 'db-info'}
            decoded = urllib.parse.unquote(tail)
            if tail in special:
                family = special[tail]
            elif decoded.startswith('_local/') and decoded != '_local/':
                family = 'local'
            elif decoded.startswith('h:') and '/' not in tail:
                family = 'leaf'
            elif tail and not decoded.startswith('_'):
                family = 'attachment' if '/' in tail else 'document'
            else:
                family = 'undeclared'
        else:
            family = 'undeclared'
        purpose = _REQUEST_PURPOSES.get((family, method))
        if purpose is None:
            raise CouchDBRequestPurposeRefused(PublicDiagnostic(
                operation=method, transport='CouchDB'))
        return purpose

    @staticmethod
    def _preflight_documents(value: object) -> None:
        # chunks.ts:243-252 cuts V1 non-plain UTF-16 units; :698-699 protects
        # Rabin-Karp text boundaries. CouchDB 3.5.3, live 2026-11-25, rejects
        # JSON.stringify's lone escapes with 400 invalid UTF-8 JSON (devices too).
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                    raise WriterParameterRefused(PublicDiagnostic(DiagnosticCode.COUCHDB_SURROGATE_REFUSED))
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend(item)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: object | None = None,
        _checking_auth: bool = False,
        _jwt_token: str | None = None,
    ) -> httpx.Response:
        """Perform a request; the single error-mapping point (audit C7).

        404 responses are returned for the caller to interpret; every
        other error status and every transport failure raises a storage
        exception whose message carries no URL, database or revision.
        """
        purpose = self._request_purpose(method, path)
        if json_body is not None:
            self._preflight_documents(json_body)
        # LiveSyncReplicator.ts:2071-2098 passes custom headers to the
        # database connection; diagnostics/couchdb.ts:45 uses them as well.
        # Do not allow absolute/network-path URLs to send credentials elsewhere.
        target = self._client.base_url.join(path)
        if (target.scheme, target.host, target.port) != (
            self._client.base_url.scheme, self._client.base_url.host, self._client.base_url.port
        ):
            raise StorageUnavailable(PublicDiagnostic(operation=method, transport='CouchDB'))
        try:
            # PouchDB adapter-http 9.0.0 lib/index.es.js:765-773.
            body = ({} if json_body is None else {
                "content": json_stringify(json_body).encode("utf-8"),
                "headers": {"Content-Type": "application/json"},
            })
            # RemoteService.ts:360-369 uses Headers.append, including auth
            # and Content-Type. Fetch headers are case-insensitive ByteStrings.
            headers = httpx.Headers(body.pop("headers", {}), encoding="latin-1")
            token = (_jwt_token if _jwt_token is not None else self._jwt.token()) if self._jwt else None
            headers["Authorization"] = "Bearer " + token if self._jwt else self._auth_header
            for key, value in self._custom_headers.items():
                value = value.strip(" \t")
                if key in headers:
                    value = headers[key] + ", " + value
                headers[key] = value
            # Transport-owned even when supplied through custom headers.
            headers["Accept-Encoding"] = "identity"
            limit = self.inflated_budget_bytes
            # _all_docs batches contain independently bounded documents. No
            # untrusted response count can enlarge the budget. Control reads,
            # attachments and write acknowledgements use one document budget.
            if (path == f"{self._db_path}/_all_docs" and isinstance(json_body, dict)
                    and json_body.get('include_docs') is True
                    and isinstance(json_body.get('keys'), list)):
                limit *= max(1, len(json_body['keys']))
            async with self._client.stream(
                method, path, params=params, headers=headers, auth=None, **body
            ) as streamed:
                content = await _bounded_http_body(streamed, limit)
                # Already decoded. Prevent a second HTTPX decode, including
                # when callers access .json(), .text or .content later.
                response = httpx.Response(
                    streamed.status_code,
                    headers=[(key, value) for key, value in streamed.headers.raw
                             if key.lower() not in {b'content-encoding', b'content-length'}],
                    content=content, request=streamed.request,
                    extensions=streamed.extensions,
                )
        except (CouchDBHTTPBudgetExceeded, CouchDBHTTPEncodingRefused) as exc:
            # C:services/base/RemoteService.ts:135-159,373-381 observes body
            # failures after fetch settles. An unreadable acknowledgement cannot
            # prove non-publication. Apply the peer uncertainty contract to all writes,
            # including bulk/local/leaf PUTs; reads retain the typed refusal.
            if purpose != "read":
                raise OutcomeUnknown(_HTTPBodyDiagnostic(
                    operation=method, transport='CouchDB', cause=type(exc),
                    budget_bytes=exc.limit if type(exc) is CouchDBHTTPBudgetExceeded else None,
                )) from None
            raise
        except httpx.HTTPError as exc:
            logger.warning('CouchDB <omitted> <omitted> failed: <omitted>; cause: %s', cause_name(type(exc)))
            if purpose != "read" and not isinstance(
                exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
            ):
                raise OutcomeUnknown(PublicDiagnostic(operation=method, transport='CouchDB', cause=type(exc))) from None
            raise StorageUnavailable(PublicDiagnostic(operation=method, transport='CouchDB', cause=type(exc))) from None

        code = response.status_code
        if code == 404 and method in {'GET', 'HEAD'}:
            return response  # missing notes are an ordinary absence
        if code >= 400:
            if not self._custom_headers and not self._jwt:
                unsafe_debug(logger, response.text)
            logger.warning('CouchDB %s status: %s', operation_field(method), numeric_field(code))
            # CouchDB 3.5.3 returns bad-signature 400 and expired-token 401.
            # Never parse/echo reason text. Probe the SAME token to distinguish
            # ordinary bad-request 400 from failed authentication (F4 live).
            jwt_failure = self._jwt is not None and (
                code == 401 or (code == 400 and (
                    _checking_auth or not await self._jwt_authenticated(token)
                ))
            )
            reason = (DiagnosticCode.COUCHDB_JWT_AUTHORIZATION_ERROR if jwt_failure else
                      DiagnosticCode.COUCHDB_DATABASE_NOT_FOUND if code == 404 else
                      DiagnosticCode.COUCHDB_AUTHORIZATION_ERROR if code in {401, 403} else
                      DiagnosticCode.THE_NOTE_WAS_CHANGED_CONCURRENTLY_RE_READ_IT_AND_RETRY if code == 409 else
                      DiagnosticCode.COUCHDB_UNAVAILABLE if code >= 500 else DiagnosticCode.FAILED)
            diagnostic_type = _HTTPReadDiagnostic if purpose == 'read' else PublicDiagnostic
            public = diagnostic_type(reason, status=code, operation=method, transport='CouchDB', cause=httpx.HTTPStatusError)
            if code == 409:
                raise ConcurrentModification(public) from None
            if (code == 408 or code >= 500) and purpose != 'read':
                raise OutcomeUnknown(public) from None
            raise StorageUnavailable(public) from None
        return response

    async def _jwt_authenticated(self, token: str | None = None) -> bool:
        # RemoteService.ts:479-485 checks db.info after connecting. Preserve
        # that intent but explicitly prove identity: anonymous access to an
        # open database must not disguise JWT misconfiguration.
        try:
            response = await self._request('GET', '_session', _checking_auth=True, _jwt_token=token)
        except StorageUnavailable as error:
            if error.diagnostic.code == DiagnosticCode.COUCHDB_JWT_AUTHORIZATION_ERROR:
                return False
            raise
        try:
            body = response.json()
        except ValueError:
            return False
        user = body.get('userCtx') if isinstance(body, dict) else None
        return (response.status_code == 200 and isinstance(user, dict)
                and user.get('name') == self._jwt._subject)

    async def _db_info(self) -> dict:
        response = await self._request("GET", self._db_path)
        if response.status_code == 404:
            raise StorageUnavailable(PublicDiagnostic(DiagnosticCode.COUCHDB_DATABASE_NOT_FOUND, status=404, operation=response.request.method, transport='CouchDB', cause=httpx.HTTPStatusError))
        body = response.json()
        return body if isinstance(body, dict) else {}

    @property
    def inflated_budget_bytes(self):
        return (self.couchdb_max_inflated_bytes if self.couchdb_max_inflated_bytes is not None
                else resolved_inflate_budget(self._splitter))

    async def _get_doc(
        self, doc_id: str, *, conflicts: bool = False
    ) -> dict | None:
        params = {"conflicts": "true"} if conflicts else None
        response = await self._request(
            "GET", self._doc_path(doc_id), params=params
        )
        if response.status_code == 404:
            if _is_db_missing(response):
                raise StorageUnavailable(PublicDiagnostic(DiagnosticCode.COUCHDB_DATABASE_NOT_FOUND, status=404, operation=response.request.method, transport='CouchDB', cause=httpx.HTTPStatusError))
            self._text_verdicts.pop(doc_id, None)
            return None
        body = response.json()
        if isinstance(body, dict):
            body = decompress_doc(body, max_inflated_bytes=self.inflated_budget_bytes)
            self._validate_identity(body)
            cached = self._text_verdicts.get(doc_id)
            if cached is not None and cached[0] != body.get("_rev"):
                self._text_verdicts.pop(doc_id, None)
            return body
        return None

    def _validate_identity(self, doc: dict) -> None:
        try:
            validate_document_identity(doc, self._path_identity)
        except ValueError as exc:
            raise StorageError(DiagnosticCode.METADATA_PATH_IDENTITY_MISMATCH_OR_COLLISION_REBUILD_FETCH_REQUIRED, cause=exc) from exc

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
            raise StorageUnavailable(PublicDiagnostic(DiagnosticCode.COUCHDB_DATABASE_NOT_FOUND, status=404, operation=response.request.method, transport='CouchDB', cause=httpx.HTTPStatusError))
        rows = response.json().get("rows", [])
        if not isinstance(rows, list):
            return []
        # RemoteService.ts:457 / compress.ts:94-96 applies to all entry docs,
        # including leaf batches. Decode before any data use or equality check.
        return [{**row, 'doc': decompress_doc(row['doc'], max_inflated_bytes=self.inflated_budget_bytes)}
                if isinstance(row, dict) and isinstance(row.get('doc'), dict)
                else row for row in rows]

    # -- index (audit C5) ----------------------------------------------------

    def _reset_index_progress(self) -> None:
        self._index = {}
        self._index_ids: list[str] = []
        self._index_range = 0
        self._index_skip = 0
        self._index_doc_offset = 0
        self._index_pending: deque[dict | None] = deque()
        self.index_truncated = True

    async def _note_doc_ids(self) -> list[str]:
        """Collect one page, retaining the next range/skip across calls.

        Upstream commonlib/pouchdb/LiveSyncLocalDB.ts:481-536 enumerates
        all pages and ranges. The peer schedules this work across requests;
        it never treats a partial metadata index as a complete vault.
        Two ranges exclude the entire h: chunk namespace (audit C5).
        """
        range_params = (
            {"endkey": json.dumps("h:")},
            {"startkey": json.dumps("h;")},
        )
        if self._index_range == len(range_params):
            return self._index_ids
        params = {"limit": str(_PAGE_SIZE), "skip": str(self._index_skip),
                  **range_params[self._index_range]}
        response = await self._request(
            "GET", f"{self._db_path}/_all_docs", params=params)
        if response.status_code == 404:
            raise StorageUnavailable(PublicDiagnostic(DiagnosticCode.COUCHDB_DATABASE_NOT_FOUND, status=404, operation=response.request.method, transport='CouchDB', cause=httpx.HTTPStatusError))
        rows = response.json().get("rows", [])
        for row in rows:
            if not isinstance(row, dict) or row.get("error") is not None:
                continue
            if (row.get("value") or {}).get("deleted"):
                continue
            doc_id = row.get("id") or row.get("key")
            if isinstance(doc_id, str) and not is_service_document_id(doc_id):
                self._index_ids.append(doc_id)
        if len(rows) < _PAGE_SIZE:
            self._index_range += 1
            self._index_skip = 0
        else:
            self._index_skip += len(rows)
        return self._index_ids

    def _compact_index_doc(self, row: object) -> dict | None:
        doc = row.get("doc") if isinstance(row, dict) else None
        if not isinstance(doc, dict) or not is_note_document(doc):
            return None
        self._validate_identity(doc)
        path = doc.get("path")
        path = path if isinstance(path, str) and path else str(doc.get("_id", ""))
        if _has_hidden_segment(path):
            return None
        try:
            meta = self._meta_from_doc(doc)
        except StorageError:
            logger.error('skipping note <omitted>: invalid metadata')
            return None
        # Never retain body, children, or eden in the index or resume state.
        compact = {key: doc[key] for key in
                   ("_id", "_rev", "path", "type", "ctime", "mtime")
                   if key in doc}
        compact["size"] = meta["size"]
        return compact

    async def _load_index(self) -> dict[str, dict]:
        """Continue the current update-sequence's compact index.

        IDs, enumeration offset and processed-document offset are cached
        in memory. A timeout does not discard already fetched metadata.
        The active aggregate deadline includes index I/O and slot waiting.
        """
        budget = ScanBudget(*self._scan_limits,
                            max_seconds=self.scan_max_seconds,
                            clock=self._scan_clock)
        active = active_scan_budget.get()

        def expired():
            return budget.expired() or (active is not None and active.expired())

        self.index_truncated = True
        while not expired():
            if self._index_pending:
                compact = self._index_pending[0]
                if compact is not None:
                    path = compact.get("path") or compact["_id"]
                    if not budget.admit(path, len(json.dumps(compact).encode())):
                        break
                    self._index[compact["_id"]] = compact
                self._index_pending.popleft()
                self._index_doc_offset += 1
                continue
            if self._index_doc_offset == len(self._index_ids):
                if self._index_range == 2:
                    self.index_truncated = False
                    break
                await self._note_doc_ids()
                continue
            batch = self._index_ids[self._index_doc_offset:
                                    self._index_doc_offset + _PAGE_SIZE]
            rows = await self._post_all_docs(batch, include_docs=True)
            # _all_docs {keys} emits one row per requested key in key order.
            # Compact the bounded page even if network I/O used its deadline:
            # the next call can then progress without refetching a slow page.
            self._index_pending.extend(self._compact_index_doc(row)
                                       for row in rows[:len(batch)])
            self._index_pending.extend(None for _ in range(len(batch) - len(rows)))
        return self._index

    async def _current_index(self) -> dict[str, dict]:
        active = active_scan_budget.get()
        if active is not None:
            views = getattr(active, 'index_views', None)
            if views is None:
                views = active.index_views = {}
            if id(self) in views:
                return views[id(self)][0]
        async with self._index_lock:
            # Recheck after waiting: another consumer in this aggregate may
            # already have established its metadata/resolution namespace.
            if active is not None and id(self) in views:
                return views[id(self)][0]
            info = await self._db_info()
            update_seq = info.get("update_seq")
            if self._index_seq != update_seq:
                self._reset_index_progress()
                self._index_seq = update_seq
            if self.index_truncated:
                await self._load_index()
            for doc_id, (rev, _) in list(self._text_verdicts.items()):
                if self._index.get(doc_id, {}).get('_rev') != rev:
                    self._text_verdicts.pop(doc_id, None)
            if active is not None:
                # Graph resolution and body traversal must use one available
                # namespace. A second index consumer must not silently finish
                # the index while the first consumer retains a partial view.
                # Keep incompleteness sticky for the aggregate response even
                # when another concurrent scan advances the shared index.
                view = dict(self._index)
                views[id(self)] = (view, self.index_truncated)
                return view
            return self._index

    # -- StorageBackend: lifecycle -------------------------------------------

    async def startup_check(self) -> None:
        if self._jwt is not None and not await self._jwt_authenticated():
            raise StorageUnavailable(PublicDiagnostic(DiagnosticCode.COUCHDB_JWT_AUTHORIZATION_ERROR))
        self._text_verdicts.clear()
        self._tweak_assessment = {"status": "OK", "differing_keys": []}
        self._writer_unsupported_reason = None
        self._splitter = ContentSplitter()
        self.enable_compression = False
        self.sync_max_size_mb = (50 if self.sync_max_size_mb_override is None
                                 else self.sync_max_size_mb_override)
        await self._db_info()
        version_doc = await self._get_doc(_VERSION_DOC_ID)
        if version_doc is not None:
            # Informational only: any value is accepted (audit B5
            # restricts behavior, not versions).
            logger.info('vault advertises an obsydian_livesync_version document: <omitted>')
        milestone = await self._request(
            "GET", self._doc_path(_MILESTONE_DOC_ID)
        )
        if milestone.status_code != 200:
            if _is_db_missing(milestone):
                raise StorageUnavailable(PublicDiagnostic(DiagnosticCode.COUCHDB_DATABASE_NOT_FOUND, status=404, operation=milestone.request.method, transport='CouchDB', cause=httpx.HTTPStatusError))
            self.resolve_selection_settings({'pathNormalizationVersion': 0})
            self._recommendation_policy = writer_policy_summary(resolve_writer_policy(
                {'pathNormalizationVersion': 0}, size_override=self.sync_max_size_mb_override,
                use_override=self._selection_overrides[0], names_override=self._selection_overrides[1]))
            await self.selection_startup()
            await self.refresh_selection_files()
            return  # no milestone document: pre-E2EE or fresh vault
        try:
            body = milestone.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        try:
            self.enable_compression = resolve_enable_compression(body)
            self.resolve_selection_settings(body)
            policies = resolve_writer_policy(body, size_override=self.sync_max_size_mb_override,
                use_override=self._selection_overrides[0], names_override=self._selection_overrides[1])
            self.sync_max_size_mb = policies[0].value
            self._recommendation_policy = writer_policy_summary(policies)
            resolved = resolve_tweak_startup(body, couchdb_mode=True,
                disable_checking_config_mismatch=getattr(self, 'disable_checking_config_mismatch', False),
                auto_accept_compatible_tweak=getattr(self, 'auto_accept_compatible_tweak', True))
            self._tweak_assessment = resolved.summary()
            for side in resolved.winners:
                logger.info("tweak alignment: %s; keys: %s", side if side in ("CURRENT", "REMOTE") else "<value hidden>", ", ".join(setting_key(k) for k in resolved.differing_keys))
            body = dict(resolved.settings)
            self._path_identity = resolve_path_identity(body)
            self._splitter = resolve_content_splitter(body)
        except UnsupportedWriterSettings as exc:
            self._splitter = None
            self._writer_unsupported_reason = PublicDiagnostic(DiagnosticCode.WRITER, issues=tuple(getattr(exc, "setting_issues", ())))
        except UnsupportedVaultSettings as exc:
            self._tweak_assessment = getattr(exc, "tweak_assessment", self._tweak_assessment)
            raise UnsupportedVault(settings_exception_diagnostic(exc)) from exc

        await self.selection_startup()
        await self.refresh_selection_files()

    def _path2id(self, path: str) -> str:
        return identity_path2id(path, self._path_identity)

    async def refresh(self, force: bool = False) -> None:
        """Reload metadata only on explicit refresh; reads query CouchDB directly.

        Index consumers retain their update-sequence check in _current_index.
        """
        if not force:
            return
        async with self._index_lock:
            info = await self._db_info()
            self._reset_index_progress()
            self._index_seq = info.get("update_seq")
            await self._load_index()
        await self.refresh_selection_files()

    async def backend_info(self) -> dict:
        info = await self._db_info()
        selection = await self.selection_diagnostics()
        return {
            **selection,
            **self.text_limits(),
            "recommendation_policy": recommendation_fields(getattr(self, "_recommendation_policy", {})),
            "tweak_assessment": assessment_fields(self._tweak_assessment),
            "writer_supported": self._writer_unsupported_reason is None and selection["selection_writer_supported"],
            "writer_unsupported_reason": writer_diagnostic(self._writer_unsupported_reason or selection["selection_unsupported_reason"]),
            "backend": "couchdb",
            "index_complete": not self.index_truncated and self._index_seq == info.get("update_seq"),
            "index_ids_collected": len(self._index_ids),
            "index_docs_indexed": len(self._index),
            "index_next_skip": self._index_skip,
            "auth_mode": "jwt" if self._jwt else "basic",
            "jwt_algorithm": self._jwt.algorithm if self._jwt else None,
            "custom_headers_configured": bool(self._custom_headers),
            "custom_headers_count": len(self._custom_headers),
            "enableCompression": self.enable_compression is True,
            "path_identity": setting_value(self._path_identity),
            "database": "<value hidden>",
            "doc_count": info["doc_count"] if type(info.get("doc_count")) is int and info["doc_count"] >= 0 else "<value hidden>",
            "update_seq": fingerprint(str(info["update_seq"])) if type(info.get("update_seq")) in (str, int) else None,
        }

    async def close(self) -> None:
        await self.selection_close()
        self._text_verdicts.clear()
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
            logger.info('read refused for vault path <omitted>: <omitted>; cause: %s', cause_name(type(exc)))
            raise NoteNotFound(PublicDiagnostic(DiagnosticCode.NOT_FOUND, paths=(DiagnosticPath(path, remote=False),))) from None

    async def get_meta(self, path: str) -> NoteMeta:
        self._validate_read_path(path)
        doc = await self._get_doc(self._path2id(path))
        if doc is None or not is_note_document(doc):
            raise NoteNotFound(PublicDiagnostic(DiagnosticCode.NOT_FOUND, paths=(DiagnosticPath(path, remote=False),)))
        return self._meta_from_doc(doc)

    async def read_note(self, path: str) -> NoteContent | None:
        try:
            validate_vault_path(path)
        except ForbiddenPathError as exc:
            logger.info('read refused for vault path <omitted>: <omitted>; cause: %s', cause_name(type(exc)))
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
            return project_note(await self._content_from_doc(doc), self.text_scan_max_bytes)
        text = await self._scan_newnote(doc, strict_budget=True)
        meta = self._meta_from_doc(doc)
        return NoteContent(meta=meta, text=text, data=None,
                           is_text=text is not None, rev=meta['rev'])

    async def iterate_texts(
        self, extension: str = "", *, after: str = ""
    ) -> AsyncIterator[tuple[NoteMeta, str | None]]:
        index = await self._current_index()
        ordered = sorted(
            index.values(),
            key=lambda doc: str(doc.get("path") or doc.get("_id") or ""),
        )
        candidates = [doc for doc in ordered
                      if str(doc.get("path") or doc.get("_id") or "") > after
                      and _matches_extension(
            str(doc.get("path") or doc.get("_id") or ""), extension
        )]
        for start in range(0, len(candidates), _TEXT_WINDOW_SIZE):
            window = candidates[start:start + _TEXT_WINDOW_SIZE]
            try:
                rows = await self._post_all_docs(
                    [doc["_id"] for doc in window], include_docs=True
                )
            except Exception:
                logger.error('unable to fetch scan metadata window')
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
                    logger.error('unable to fetch scan chunk batch')
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
                    yield (content["meta"], scan_text(content["text"], self.text_scan_max_bytes)) if content else (meta, None)
                except Exception:
                    logger.error('skipping note <omitted>')
                    yield meta, None
            # No body-bearing window survives into the next HTTP fetch.
            del docs, rows, chunks, chunk_rows, doc, content
            inline = row = chunk = None

    async def _scan_newnote(self, doc: dict, *, strict_budget=False) -> str | None:
        doc_id = doc['_id']
        key = (doc['_rev'], self.text_scan_max_bytes)
        cached = self._text_verdicts.pop(doc_id, None)
        if cached == key and not strict_budget:
            self._remember_nontext(doc_id, key)
            return None
        if doc.get('e_'):
            raise UnsupportedVault('encrypted note')
        children = doc.get('children') or []
        if not isinstance(children, list) or not all(isinstance(c, str) for c in children):
            raise StorageError('invalid children field')
        projection = TextProjection(self.text_scan_max_bytes)
        eligible = True
        for index, child in enumerate(children):
            piece = (await self._fetch_chunk_datas([child], doc['path'], doc.get('eden')))[0]
            if not projection.feed(piece, final=index == len(children) - 1):
                eligible = False
                projection.parts.clear()
                if not strict_budget:
                    self._remember_nontext(doc_id, key)
                    return None
        if strict_budget and self.text_scan_max_bytes and projection.bytes > self.text_scan_max_bytes:
            raise TextBudgetExceeded(projection.bytes, self.text_scan_max_bytes)
        if not eligible:
            self._remember_nontext(doc_id, key)
        return projection.text() if eligible else None

    def _remember_nontext(self, doc_id: str, key: tuple[str, int]) -> None:
        # Positive verdicts cannot avoid the body I/O needed for a text query.
        self._text_verdicts[doc_id] = key
        self._text_verdicts.move_to_end(doc_id)
        while len(self._text_verdicts) > self.text_verdict_cache_entries:
            self._text_verdicts.popitem(last=False)

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
            raise StorageError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_MISSING_LEGACY_DATA_FIELD, paths=(DiagnosticPath(path, remote=True),))) from None
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
            raise StorageError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_INVALID_CHILDREN_FIELD, paths=(DiagnosticPath(path, remote=True),)))
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
            logger.warning('note <omitted>: <omitted>; cause: %s', cause_name(type(exc)))
            raise StorageError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_INVALID_CHUNK_ENCODING, paths=(DiagnosticPath(path, remote=True),))) from None
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
            raise UnsupportedVault(PublicDiagnostic(DiagnosticCode.USES_END_TO_END_ENCRYPTION, paths=(DiagnosticPath(path, remote=True),))) from None
        except ValueError:
            raise StorageError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_INVALID_EDEN_FIELD, paths=(DiagnosticPath(path, remote=True),))) from None
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
                or _js_truthy(chunk.get("deleted")) or _js_truthy(chunk.get("_deleted"))
                or not isinstance(chunk.get("data"), str)
            ):
                logger.warning('note <omitted>: chunk <omitted> is missing or corrupt')
                raise StorageError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_A_CONTENT_CHUNK_IS_MISSING, paths=(DiagnosticPath(path, remote=True),)))
            if chunk.get("e_"):
                logger.warning('note <omitted>: chunk <omitted> is E2EE-encrypted')
                raise UnsupportedVault(PublicDiagnostic(DiagnosticCode.USES_END_TO_END_ENCRYPTION_WHICH_THIS_SERVER_DOES_NOT_SUPPORT, paths=(DiagnosticPath(path, remote=True),)))
            datas.append(chunk["data"])
        return datas

    # -- StorageBackend: writes ----------------------------------------------

    @staticmethod
    def _refuse_unresolved_conflicts(doc: dict, path: str) -> None:
        """Refuse writes to a document with sibling revisions (C3)."""
        conflicts = doc.get("_conflicts")
        if conflicts:
            logger.warning('note <omitted> has <omitted> unresolved conflicting revisions')
            raise UnresolvedConflicts(PublicDiagnostic(DiagnosticCode.HAS_UNRESOLVED_CONFLICTS_RESOLVE_THEM_IN_OBSIDIAN_OR_ANOTHER_CLIENT_BEFORE_WRITING, paths=(DiagnosticPath(path, remote=False),)))

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
            logger.info('note <omitted>: expected revision no longer current; refusing the write')
            raise ConcurrentModification(
                "note changed concurrently; re-read and retry"
            )

    async def _put_doc(self, doc_id: str, body: dict) -> str:
        """PUT a document and return its new revision.

        Durability requires CouchDB's explicit acknowledgement: HTTP 201
        with an ``ok``/``rev`` body (spec §7.6). A non-refusal response
        without that confirmation (including 202 "Accepted") raises
        OutcomeUnknown; the caller must re-read before retrying. Explicit
        4xx refusals remain ordinary errors. Stale ``_rev`` bodies surface
        as CouchDB 409s, mapped to ``ConcurrentModification``.
        """
        self._preflight_documents(body)
        response = await self._request(
            "PUT", self._doc_path(doc_id),
            json_body=compress_doc(body) if self.enable_compression else body
        )
        if response.status_code == 404:
            # A present database never 404s a document PUT.
            logger.warning('CouchDB PUT <omitted> -> 404 (database gone)')
            raise StorageUnavailable(PublicDiagnostic(DiagnosticCode.COUCHDB_DATABASE_NOT_FOUND, status=404, operation=response.request.method, transport='CouchDB', cause=httpx.HTTPStatusError))
        if response.status_code != 201:
            logger.warning('CouchDB PUT <omitted> -> <omitted> (not 201: not confirmed durable)')
            raise OutcomeUnknown()
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
            logger.warning('CouchDB PUT <omitted> -> unexpected acknowledgement body')
            raise OutcomeUnknown()
        self._text_verdicts.pop(doc_id, None)
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
        # C:pouchdb/compress.ts:49-71: stored leaves are always read through
        # one decompression pass, including already-marked caller pieces.
        expected_data = decompress_data(data, max_inflated_bytes=self.inflated_budget_bytes)
        existing = await self._get_doc(chunk_id)
        if existing is not None:
            if (
                not existing.get("e_")
                and not _js_truthy(existing.get("deleted")) and not _js_truthy(existing.get("_deleted"))
                and existing.get("type") == "leaf"
                and existing.get("data") == expected_data
            ):
                return chunk_id  # content-addressed reuse, no PUT
            logger.warning('note <omitted>: chunk <omitted> under the computed id holds different data; refusing to reuse or overwrite it')
            raise StorageError(PublicDiagnostic(DiagnosticCode.CANNOT_BE_WRITTEN_AN_EXISTING_SHARED_CONTENT_CHUNK_IS_ENCRYPTED_OR_HOLDS_DIFFERENT_DATA, paths=(DiagnosticPath(path, remote=False),)))
        try:
            await self._put_doc(chunk_id, {"data": data, "type": "leaf"})
        except OutcomeUnknown:
            raise MutationNotApplied(
                "note is unchanged: content chunk publication was not confirmed; "
                "a harmless orphan chunk may exist"
            ) from None
        except ConcurrentModification:
            # Another writer may have published the same immutable chunk
            # after our absence check. Never overwrite the winning leaf.
            winner = await self._get_doc(chunk_id)
            if not (
                winner is not None
                and not winner.get("e_")
                and not _js_truthy(winner.get("deleted")) and not _js_truthy(winner.get("_deleted"))
                and winner.get("type") == "leaf"
                and winner.get("data") == expected_data
            ):
                raise ConcurrentModification(PublicDiagnostic(DiagnosticCode.CANNOT_BE_WRITTEN_CONTENT_CHUNK_IS_ENCRYPTED_OR_CHANGED_CONCURRENTLY, paths=(DiagnosticPath(path, remote=False),))) from None
        return chunk_id

    async def _get_note_doc(self, path: str) -> dict:
        """Live metadata document for ``path`` (conflicts refused)."""
        doc = await self._get_doc(self._path2id(path), conflicts=True)
        if doc is None or not is_note_document(doc):
            raise NoteNotFound(PublicDiagnostic(DiagnosticCode.NOT_FOUND, paths=(DiagnosticPath(path, remote=False),)))
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
            if not (_js_truthy(target.get("deleted")) or _js_truthy(target.get("_deleted"))) and not overwrite:
                raise NoteExists(PublicDiagnostic(DiagnosticCode.ALREADY_EXISTS, paths=(DiagnosticPath(new_path, remote=False),)))
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
            raise WriterUnsupported(self._writer_unsupported_reason if isinstance(self._writer_unsupported_reason, PublicDiagnostic) else None)

    async def write_note(
        self,
        path: str,
        content: str | bytes,
        *,
        create: bool = False,
        overwrite: bool = False,
        mtime: int | None = None,
        expected_rev: str | None = None,
        lossy_text: bool = False,
    ) -> NoteMeta:
        """Ingest file content using device path-based disk-reading rules.

        Only explicit create resurrects a tombstone. expected_rev protects
        read-modify-write operations before publishing any chunks.
        """
        self.check_writer_support()
        validate_vault_path(path)
        await self.check_selection(path)
        # Ordinary CREATE/CHANGED skips before content acquisition/publication.
        # C:serviceModules/ServiceFileHandlerBase/events.ts:63-69.
        if isinstance(content, bytes):
            check_file_size(path, len(content), self.sync_max_size_mb)
        body, doc_type, size = prepare_file_content(path, content, lossy_text=lossy_text)
        # Device admission uses raw host stat.size; metadata uses Blob.size
        # (C:EntryManagerImpls.ts:253-274). Lossy decoding can expand bytes.
        # P-13: also gate the converted Blob before any chunks/publication.
        check_file_size(path, size, self.sync_max_size_mb)
        result = await self._write_content(
            path, body, size, doc_type,
            create=create, overwrite=overwrite, mtime=mtime,
            expected_rev=expected_rev,
        )
        return complete_publication(result, ingress_notices(content)
                                    if isinstance(content, str) or doc_type == 'plain' else [])

    async def write_attachment(
        self, path: str, data: bytes, *, create: bool = False,
        overwrite: bool = False, expected_rev: str | None = None,
        lossy_text: bool = False,
    ) -> NoteMeta:
        """Byte-input alias for write_note; type still follows path."""
        self.check_writer_support()
        validate_vault_path(path)
        return await self.write_note(
            path, data, create=create, overwrite=overwrite,
            expected_rev=expected_rev, lossy_text=lossy_text,
        )

    async def _write_content(
        self, path: str, content: str | bytes, size: int, doc_type: str, *,
        create: bool, overwrite: bool, mtime: int | None = None,
        expected_rev: str | None = None,
    ) -> NoteMeta:
        """Publish encoded chunk data through the shared write transaction."""
        try:
            pieces = [piece for piece in self._splitter.split(content, path=canonical_path(path)) if piece]
        except SplitterRefusal as error:
            # Keep codec modules storage-independent; project only typed fields.
            raise WriterParameterRefused(
                error.diagnostic, setting_keys=getattr(error, 'setting_keys', error.diagnostic.keys)
            ) from None
        # Validate the entire transaction before even the metadata lookup; an
        # inherited metadata string must not fail after publishing any leaves.
        self._preflight_documents([path, pieces])
        doc_id = self._path2id(path)
        doc = await self._get_doc(doc_id, conflicts=True)
        if doc is not None:
            self._refuse_unresolved_conflicts(doc, path)
        self._check_expected_rev(doc, path, expected_rev)
        # Both deletion flags with JavaScript truthiness, as the shared read predicate reads them.
        live = doc is not None and is_note_document(doc)
        if live and create and not overwrite:
            raise NoteExists(PublicDiagnostic(DiagnosticCode.ALREADY_EXISTS, paths=(DiagnosticPath(path, remote=False),)))
        if not live and not create:
            raise NoteNotFound(PublicDiagnostic(DiagnosticCode.NOT_FOUND, paths=(DiagnosticPath(path, remote=False),)))

        # P-10 / EntryManagerImpls.ts:280: retain ingested Blob.size even
        # when a legacy splitter's Blob.text() removes an initial BOM.
        # EntryManagerImpls.ts:133-159 retains all references, while
        # DatabaseWriteLayer.ts:24-49 accepts duplicate 409s. Lossless leaves
        # cannot represent distinct data under one id: preflight before PUTs.
        unique = {}
        marker_chunks = 0
        for piece in pieces:
            # C:pouchdb/compress.ts:54,68-69 reserves this leaf prefix even
            # with writes uncompressed. Validate ALL pieces before any PUT.
            if piece.startswith(MARK_SHIFT_COMPRESSED):
                try:
                    decoded = decompress_data(piece, max_inflated_bytes=self.inflated_budget_bytes)
                except CompressedDataError:
                    raise StorageError(PublicDiagnostic(
                        DiagnosticCode.COMPRESSION_PREFIX_INPUT,
                        paths=(DiagnosticPath(path, remote=False),),
                    )) from None
                marker_chunks += int(decoded != piece)
            chunk_id = self._splitter.chunk_id_for_piece(piece)
            if chunk_id in unique and unique[chunk_id] != piece:
                raise StorageError(PublicDiagnostic(DiagnosticCode.CHUNK_COLLISION_RECOVERY))
            unique[chunk_id] = piece
        children = [self._splitter.chunk_id_for_piece(piece) for piece in pieces]
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
        self._preflight_documents(body)
        for piece in pieces:
            await self._write_chunk(path, piece)
        body["_rev"] = await self._put_doc(doc_id, body)
        notices = split_notices(content, self._splitter.version)
        if marker_chunks:
            notices.append({'code': 'couchdb_marker_interpreted', 'chunks': marker_chunks,
                            'metadata_size_preserved': True})
        return complete_publication(self._meta_from_doc(body), notices)

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
        await self.check_selection(path)
        doc = await self._get_doc(self._path2id(path), conflicts=True)
        self._check_expected_rev(doc, path, expected_rev)
        if doc is None or not is_note_document(doc):
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
        copy can duplicate a note but never lose one. Oversized destinations
        are refused before publication (P-13).
        """
        self.check_writer_support()
        validate_vault_path(path)
        validate_vault_path(new_path)
        await self.check_selection(new_path)
        source_selection = await self.selection_for(path)
        if not source_selection.writer_supported:
            raise WriterUnsupported(source_selection.reason)
        preserve_source = not source_selection.allowed
        new_path = canonical_path(new_path)
        if path == new_path:
            return await self.get_meta(path)

        doc = await self._get_note_doc(path)
        # Device admission converts oversized RENAME to DELETE(source).
        # P-13: this peer lacks the device's retained host file; refuse first.
        try:
            check_file_size(new_path, doc.get('size', 0), self.sync_max_size_mb)
        except WriteSkipped:
            raise RenameRefused(path, new_path, doc.get('size', 0),
                                self.sync_max_size_mb) from None

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
        if preserve_source:
            return self._meta_from_doc(body)
        try:
            await self._soft_delete(doc, path, _now_ms())
        except OutcomeUnknown as exc:
            raise OutcomeUnknown(exc.diagnostic, source_removal=True, paths=(path, new_path)) from None
        except Exception as exc:
            logger.warning('rename source retirement failed for <omitted>; cause: %s', cause_name(type(exc)))
            source_state = "unknown"
            try:
                source = await self._get_doc(self._path2id(path))
                if source is not None and is_note_document(source):
                    source_state = "live"
            except Exception:
                logger.warning('unable to confirm rename source state for <omitted>; cause: %s', cause_name(type(exc)))
            raise RenamePartialError(path, new_path, source_state=source_state) from exc
        return self._meta_from_doc(body)

    async def copy_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Duplicate a note by re-publishing its chunk references."""
        self.check_writer_support()
        validate_vault_path(path)
        validate_vault_path(new_path)
        await self.check_selection(new_path)
        doc = await self._get_note_doc(path)
        # Copy creates a new storage file -> CREATE size gate (C:events.ts:63-69).
        # C:serviceModules/ServiceFileHandlerBase/events.ts:63-77.
        # Rename has a separate admission rule; deletes have no size gate.
        check_file_size(new_path, doc.get('size', 0), self.sync_max_size_mb)
        target = await self._target_doc_for_write(new_path, overwrite=overwrite)
        body = self._moved_body(doc, new_path, _now_ms())
        if target is not None:
            body["_rev"] = target["_rev"]  # replace the old revision
        body["_rev"] = await self._put_doc(self._path2id(new_path), body)
        return self._meta_from_doc(body)

    async def _read_selection_file(self, path):
        from livesync_format import selection_lookup_path
        path = selection_lookup_path(path)
        if path is None:
            return None
        # Hidden ignore files are ordinary stored metadata, outside tool read gates.
        candidates = ['i:' + path, path] if _has_hidden_segment(path) else [path]
        doc = None
        for candidate in candidates:
            doc = await self._get_doc(self._path2id(candidate))
            if doc is not None:
                break
        if doc is None or _js_truthy(doc.get('deleted')) or _js_truthy(doc.get('_deleted')) or doc.get('e_'):
            return None
        content = await self._content_from_doc(doc)
        if content is None:
            return None
        return content['text'] if content['is_text'] else content['data'].decode('utf-8', errors='replace')
