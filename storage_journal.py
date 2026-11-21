"""S3 journal-peer storage backend: startup checks and replay (Plan B, Task 5).

:class:`JournalBackend` implements the :class:`~storage_base.StorageBackend`
protocol on top of the Plan B building blocks:

- :class:`~object_storage.JournalObjectStorage` — the only S3 access;
- :class:`~journal_store.JournalStore` — local SQLite/chunk state created
  here on the first ``startup_check``;
- :mod:`journal_codec` — inflate/parse of ``<ms>-docs.jsonl.gz`` files;
- :func:`livesync_format.resolve_path_identity` — milestone settings gate.

Startup mirrors upstream ``LiveSyncJournalReplicator``/``JournalSyncCore``:
the milestone (``_00000000-milestone.json``) carries the vault settings and
lock flag; the sync-parameters object
(``_obsidian_livesync_journal_sync_parameters.json``) pins the journal
epoch ``"<protocolVersion>:<pbkdf2salt>"`` (JournalSyncCore.ts:344). An
epoch change or a vanished last-sent file means the remote history was
wiped or re-encrypted, and local state is reset and replayed from scratch.

This module covers ``startup_check``/``refresh``/``backend_info``/``close``
and the read paths — ``list_notes``/``get_meta``/``read_note``/
``iterate_texts`` — mirroring the CouchDB backend's read contracts (audit
tags B8/B12/C4/C5/C7 apply unchanged): display paths come from the doc's
``path`` field, soft-deleted winners are absent, hidden dotfile segments and
service documents never surface, folder filtering is case-insensitive, and
scans isolate corrupt notes instead of failing. Path-to-id resolution uses
the vault's path identity, because journal doc ids were derived by clients
with that same identity. Writes append a journal and verify its contents
before applying it locally; rename publishes both revisions in one file.
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from journal_codec import (
    MAX_INFLATED_BYTES, MAX_RECORDS, MAX_LINE_BYTES,
    deflate_journal,
    inflate_journal,
    journal_filename,
    parse_journal,
    serialize_journal,
)
from journal_store import EncryptedNoteError, JournalStore, parse_rev
from livesync_format import (
    PATH_IDENTITY_LEGACY,
    PATH_IDENTITY_NFC,
    ForbiddenPathError,
    UnsupportedVaultSettings,
    canonical_path,
    chunk_id_for,
    clone_metadata,
    doc_type_for,
    is_note_document,
    is_writable_text_path,
    new_metadata_doc,
    path2id,
    resolve_path_identity,
    soft_delete_revision,
    validate_vault_path,
)
from object_storage import JournalObjectStorage, ObjectSizeLimit, MAX_OBJECT_BYTES
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

# Control objects, never journals. Upstream commonlib:
# LiveSyncJournalReplicator.ts:64 (MILSTONE_DOCID) and
# common/models/sync.definition.ts:11 (DOCID_JOURNAL_SYNC_PARAMETERS).
MILESTONE_KEY = "_00000000-milestone.json"
SYNC_PARAMETERS_KEY = "_obsidian_livesync_journal_sync_parameters.json"

# Journal names have an optional suffix after ms (journal_codec.journal_filename).
# Server publications use a random 16-hex suffix; devices use the base form.
# Replay orders candidates by the integer ms prefix; S3 listing order is
# lexicographic, where "10-..." sorts *before* "9-..." (Plan B Review
# Focus 3). Names without a numeric prefix are foreign to the journal
# format and sort after every well-formed name, deterministically.
_MS_PREFIX_RE = re.compile(r"^([0-9]+)-")


def is_journal_key(key: str) -> bool:
    """Whether a listed key is a journal, not a ``_``-prefixed control object.

    Mirrors upstream ``isJournalKey`` (JournalSyncCore.ts:86): everything
    that does not start with ``_`` is journal data.
    """
    return not key.startswith("_")


def journal_sort_key(name: str) -> tuple[int, int, str]:
    """Sort key ordering journal names by their numeric ms prefix."""
    match = _MS_PREFIX_RE.match(name)
    if match is None:
        return (1, 0, name)
    return (0, int(match.group(1)), name)


# -- Read-path helpers (behavioral parity with storage_couchdb) ---------------
#
# The three helpers below are the CouchDB backend's read-side building
# blocks, kept in sync by tests that mirror the CouchDB suites; the journal
# backend must filter and classify exactly like its CouchDB peer for the
# tool layer to be backend-agnostic.

_TEXT_WINDOW_SIZE = 50

# Aggregate refresh limits, independent of the per-file codec/download caps.
MAX_REFRESH_KEYS = 1_000_000
MAX_REFRESH_BYTES = 4 * 1024**3
MAX_REFRESH_SECONDS = 300.0
_LIST_PAGE_SIZE = 1000


class _RefreshBudgetStop(Exception):
    """Internal clean partial-stop signal; never marks a file processed."""



def _has_hidden_segment(path: str) -> bool:
    """Whether any path segment is a dotfile (``.obsidian``, ...)."""
    return any(segment.startswith(".") for segment in path.split("/"))


def _matches_extension(path: str, extension: str) -> bool:
    suffix = extension if extension.startswith(".") else "." + extension
    return path.lower().endswith(suffix.lower())


def _as_ms(value: object) -> int:
    if value is None:
        return 0  # Legacy documents may omit timestamps.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageError("corrupt note timestamp: expected a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise StorageError("corrupt note timestamp: expected a finite number")
    if value < 0:
        raise StorageError("corrupt note timestamp: expected a non-negative number")
    return int(value)


class JournalBackend:
    """``StorageBackend`` peer for an Obsidian LiveSync journal bucket.

    The store under ``state_dir`` is created on the first
    :meth:`startup_check`, after the milestone has fixed the vault's path
    identity; reopening the same state directory against a vault with a
    different identity is a configuration error (``ValueError`` from the
    store). All object access is synchronous per the
    :class:`JournalObjectStorage` protocol.
    """

    def __init__(
        self,
        object_storage: JournalObjectStorage,
        state_dir: Path,
        refresh_min_interval: float = 5.0,
        *,
        state_size_warn_mb: int = 0,
        max_refresh_keys: int = MAX_REFRESH_KEYS,
        max_refresh_bytes: int = MAX_REFRESH_BYTES,
        max_refresh_seconds: float = MAX_REFRESH_SECONDS,
        max_inflated_bytes: int = MAX_INFLATED_BYTES,
        max_records: int = MAX_RECORDS,
        max_line_bytes: int = MAX_LINE_BYTES,
    ):
        for value in (max_refresh_keys, max_refresh_bytes, max_refresh_seconds,
                      max_inflated_bytes, max_records, max_line_bytes):
            if value <= 0 or not math.isfinite(value):
                raise ValueError("journal limits must be positive and finite")
        self.max_refresh_keys = max_refresh_keys
        self.max_refresh_bytes = max_refresh_bytes
        self.max_refresh_seconds = max_refresh_seconds
        self.max_inflated_bytes = max_inflated_bytes
        self.max_records = max_records
        self.max_line_bytes = max_line_bytes
        self._listing_cursor = ""
        self._refresh_deadline: float | None = None
        self._refresh_downloaded = 0
        self._sync_parameters_bytes: int | None = None
        self._object_storage = object_storage
        self._state_dir = Path(state_dir)
        self.state_size_warn_mb = state_size_warn_mb
        self.refresh_min_interval = refresh_min_interval
        self._store: JournalStore | None = None
        # Throttle clock (monotonic) and the wall-clock stamp for diagnostics.
        self._last_refresh_failure: Exception | None = None
        self._last_refresh: float | None = None
        self._last_refresh_at: datetime | None = None
        self._max_seen_ms = 0

    # -- StorageBackend: lifecycle -------------------------------------------

    async def startup_check(self) -> None:
        """Verify bucket compatibility and reconcile local state.

        A locked milestone is advisory and logged as a warning.
        Raises :class:`~storage_base.UnsupportedVault` for a
        missing milestone with journals present, settings this
        server cannot serve, or missing sync parameters with journals
        present. A bucket without milestone and journals is treated as a
        fresh vault with legacy path identity (logged).
        """
        milestone = self._load_milestone()
        if milestone is None:
            if self._has_remote_journals():
                raise UnsupportedVault(
                    "vault milestone is missing while journal files exist; "
                    "rebuild from the authoritative vault"
                )
            logger.warning(
                "journal bucket has no milestone and no journal files; "
                "treating it as a fresh vault with legacy path identity"
            )
            identity = PATH_IDENTITY_LEGACY
        else:
            if milestone.get("locked") is True:
                logger.warning(
                    "vault milestone has advisory locked: true flag; "
                    "remote marked as rebuilt; peer performs full replay"
                )
            try:
                identity = resolve_path_identity(milestone)
            except UnsupportedVaultSettings as exc:
                raise UnsupportedVault(str(exc)) from exc

        # A differing identity for an existing state directory is a
        # configuration error and propagates as ValueError from the store.
        # A repeated startup replaces the previous connection cleanly.
        if self._store is not None:
            self._store.close()
            self._store = None
        self._store = JournalStore(self._state_dir, identity)

        epoch = self._load_sync_parameters_epoch()
        if epoch is None:
            # No parameters object: legal only without journal data. The
            # local state may still reference a wiped bucket.
            if self._has_remote_journals():
                raise UnsupportedVault(
                    "journal sync parameters are missing while journal "
                    "files exist; the vault may be encrypted or incompatible"
                )
            self._probe_remote_wipe(epoch=None)
            return
        if self._store.epoch != epoch:
            # The setter wipes docs/processed/meta and chunk files, so the
            # next refresh replays the whole history.
            logger.info(
                "journal epoch changed %r -> %r; local state was reset",
                self._store.epoch,
                epoch,
            )
            self._store.epoch = epoch
            self._clear_refresh_clock()
        else:
            self._probe_remote_wipe(epoch)

    async def refresh(self, force: bool = False) -> None:
        """Replay journal files missing from the processed ledger.

        Bounded listing windows form complete, repeating sweeps (B-8).
        Each window is replayed in numeric order; already-processed files
        are skipped idempotently. Budget stops retain completed files and
        retry the unfinished window; key-budget stops continue the sweep.
        Refreshes closer than ``refresh_min_interval`` together are
        skipped unless ``force`` is set. A skipped refresh re-raises an
        unresolved failure; otherwise it is a no-op.
        Candidates are ordered by their numeric ms prefix and applied one
        by one; the first failing file stops replay without marking it
        processed. Completed, partial and failed passes arm the throttle;
        force=True bypasses it.
        """
        store = self._require_store("refresh")
        now = time.monotonic()
        if (
            not force
            and self._last_refresh is not None
            and now - self._last_refresh < self.refresh_min_interval
        ):
            if self._last_refresh_failure is not None:
                raise StorageError(
                    "refresh failed previously; retry after interval or force"
                ) from self._last_refresh_failure
            return
        self._refresh_deadline = now + self.max_refresh_seconds
        self._refresh_downloaded = 0
        try:
            # Resolve resets before choosing the listing continuation.
            epoch = self._load_sync_parameters_epoch()
            if store.epoch != (epoch or ""):
                store.epoch = epoch or ""
                self._clear_refresh_clock()
            self._probe_remote_wipe(epoch)
            candidates, next_cursor = self._list_journals()
            for name in candidates:
                self._check_refresh_time(name)
                if not store.processed(name):
                    self._replay_one(store, name)
            # Advance only after this entire window was replayed. A byte/time
            # stop or failure retries the same window, skipping its ledger.
            self._listing_cursor = next_cursor
        except _RefreshBudgetStop as exc:
            logger.warning("journal refresh budget stop: %s", exc)
        except Exception as exc:
            self._last_refresh_failure = exc
            raise
        else:
            self._last_refresh_failure = None
        finally:
            self._refresh_deadline = None
            # Failures also arm the throttle; force=True bypasses it.
            self._last_refresh = time.monotonic()
            self._last_refresh_at = datetime.now(timezone.utc)

    def _check_refresh_time(self, point: str) -> None:
        if self._refresh_deadline is not None and time.monotonic() >= self._refresh_deadline:
            raise _RefreshBudgetStop(f"wall-clock at {point!r}")

    def _get_object(self, key: str) -> bytes | None:
        if self._refresh_deadline is None:
            return self._object_storage.get(key)
        self._check_refresh_time(key)
        remaining = self.max_refresh_bytes - self._refresh_downloaded
        if remaining <= 0:
            raise _RefreshBudgetStop(f"download bytes at {key!r}")
        try:
            blob = self._object_storage.get(key, max_bytes=remaining)
        except ObjectSizeLimit:
            if remaining < getattr(self._object_storage, "max_object_bytes", MAX_OBJECT_BYTES):
                raise _RefreshBudgetStop(f"download bytes at {key!r}") from None
            raise
        self._refresh_downloaded += len(blob) if blob is not None else 0
        self._check_refresh_time(key)
        return blob

    async def backend_info(self) -> dict:
        """Diagnostics for ``vault_stats`` and startup logs."""
        store = self._store
        return {
            "backend": "s3-journal",
            "state_dir_path": str(self._state_dir),
            "state_dir_bytes": store.state_size_bytes() if store else 0,
            "state_size_warn_mb": self.state_size_warn_mb,
            "path_identity": store.path_identity if store else None,
            "epoch": store.epoch if store else None,
            "files_processed": store.files_processed_count() if store else 0,
            # Live note documents in the store (tombstones and service
            # docs excluded); hidden dotfile notes count here but never
            # surface through list_notes/iterate_texts.
            "notes": len(store.note_ids()) if store else 0,
            "last_refresh": (
                self._last_refresh_at.isoformat()
                if self._last_refresh_at is not None
                else None
            ),
        }

    async def close(self) -> None:
        """Close the local store (idempotent)."""
        if self._store is not None:
            self._store.close()
            self._store = None

    # -- Startup helpers -------------------------------------------------------

    def _load_milestone(self) -> dict | None:
        """The milestone settings object, or ``None`` when absent."""
        blob = self._get_object(MILESTONE_KEY)
        if blob is None:
            return None
        try:
            milestone = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnsupportedVault(
                "vault milestone object is not valid JSON"
            ) from exc
        if not isinstance(milestone, dict):
            raise UnsupportedVault("vault milestone object is malformed")
        return milestone

    def _load_sync_parameters_epoch(self) -> str | None:
        """The journal epoch from sync parameters, ``None`` when absent.

        Mirrors upstream ``getJournalEpochFromSyncParams``
        (JournalSyncCore.ts:343-345): ``"<protocolVersion>:<pbkdf2salt>"``.
        """
        blob = self._get_object(SYNC_PARAMETERS_KEY)
        if blob is None:
            self._sync_parameters_bytes = 0
            return None
        try:
            params = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnsupportedVault(
                "journal sync parameters object is not valid JSON"
            ) from exc
        if not isinstance(params, dict):
            raise UnsupportedVault("journal sync parameters object is malformed")
        protocol_version = params.get("protocolVersion")
        salt = params.get("pbkdf2salt")
        if (
            isinstance(protocol_version, bool)
            or not isinstance(protocol_version, int)
            or not isinstance(salt, str)
        ):
            raise UnsupportedVault("journal sync parameters object is malformed")
        self._sync_parameters_bytes = len(blob)
        return f"{protocol_version}:{salt}"

    def _has_remote_journals(self) -> bool:
        """Whether the bucket holds any journal, cheaply.

        Journal names start with a digit and digits sort before ``_`` in
        UTF-8 binary order, so the first listed key is a journal if and
        only if journals exist — the same one-key trick as upstream
        ``hasRemoteJournals`` (JournalSyncCore.ts:754-756).
        """
        return any(
            is_journal_key(key)
            for key in self._object_storage.list("", limit=1)
        )

    def _probe_remote_wipe(self, epoch: str | None) -> bool:
        """Reset local state when the last-sent journal has vanished.

        Returns whether a reset occurred. A missing object for the last
        file this peer sent
        means the bucket was wiped or rebuilt (objects are never deleted
        individually, spec section 7), like upstream's
        ``ensureCheckpointCachesAreFresh`` wipe probe. ``epoch``, when
        known, is re-recorded after the reset so the next startup
        compares against the correct value.
        """
        store = self._store
        if store is None:
            return False
        key = store.last_sent_key
        if not key or self._object_storage.exists(key):
            return False
        logger.warning(
            "journal object %r vanished; assuming a remote wipe and "
            "resetting local state",
            key,
        )
        store.reset()
        if epoch is not None:
            store.epoch = epoch
        self._clear_refresh_clock()
        return True

    def _clear_refresh_clock(self) -> None:
        """After a state reset, the next refresh must not be throttled."""
        self._listing_cursor = ""
        self._last_refresh = None
        self._last_refresh_at = None

    # -- Replay helpers ----------------------------------------------------------

    def _list_journals(self) -> tuple[list[str], str]:
        """Bounded lexicographic windows; numerically sort each replay window.

        Complete sweeps restart at the beginning (B-8), so late keys below a
        previous continuation cannot remain hidden. The processed ledger
        ensures retries and repeated sweeps converge.
        """
        keys: list[str] = []
        cursor = self._listing_cursor
        next_cursor = ""
        while len(keys) < self.max_refresh_keys:
            self._check_refresh_time(cursor)
            limit = min(_LIST_PAGE_SIZE, self.max_refresh_keys - len(keys))
            page = self._object_storage.list(cursor, limit=limit)
            if not page:
                break
            if len(page) > limit or page[-1] <= cursor or any(k <= cursor for k in page):
                raise StorageError("journal listing made no pagination progress")
            keys.extend(page)
            cursor = page[-1]
            if len(page) < limit:
                break
        else:
            next_cursor = cursor
            logger.warning("journal refresh key budget stop at %r", cursor)
        for key in keys:
            match = _MS_PREFIX_RE.match(key)
            if match is not None:
                self._max_seen_ms = max(self._max_seen_ms, int(match.group(1)))
        return sorted((key for key in keys if is_journal_key(key)), key=journal_sort_key), next_cursor

    def _replay_one(self, store: JournalStore, name: str) -> None:
        """Fetch, decode and apply one journal file.

        Storage-layer failures propagate unchanged (they already name the
        key); decoding or store failures become :class:`StorageError`
        naming the file, and the file stays unprocessed so the next
        refresh retries it.
        """
        try:
            blob = self._get_object(name)
            if blob is None:
                raise StorageError(
                    f"journal file {name!r} disappeared during replay"
                )
            journal = parse_journal(
                inflate_journal(blob, max_output_bytes=self.max_inflated_bytes),
                name=name, max_records=self.max_records, max_line_bytes=self.max_line_bytes,
            )
            store.apply_journal(name, journal.chunks, journal.docs)
        except StorageUnavailable:
            raise
        except (StorageError, ValueError, OSError, sqlite3.Error) as exc:
            # The public message carries the file name and the error class
            # only; details (document ids, paths, revisions) stay in the
            # server log (audit C7).
            logger.warning(
                "journal file %r could not be replayed: %s: %s",
                name,
                type(exc).__name__,
                exc,
            )
            raise StorageError(
                f"journal file {name!r} could not be replayed: "
                f"{type(exc).__name__}"
            ) from exc

    def _require_store(self, operation: str) -> JournalStore:
        if self._store is None:
            raise StorageError(
                f"{operation} requested before startup_check completed"
            )
        return self._store

    # -- StorageBackend: reads (Task 6) and writes (Task 7) -------------------

    async def list_notes(
        self,
        folder: str = "",
        extension: str | None = None,
        recursive: bool = True,
    ) -> list[NoteMeta]:
        """List live notes filtered by folder/extension, sorted by path.

        Same contracts as the CouchDB backend: the folder prefix matches
        case-insensitively (original paths in the response),
        ``recursive=False`` keeps direct children only — also at the vault
        root — and the extension matches as a case-insensitive suffix with
        or without the leading dot. Hidden dotfile segments never surface;
        tombstones and service documents are already excluded by the
        store's note classification. Reads the local replay state only
        (tools own ``refresh``).
        """
        store = self._require_store("list_notes")
        folder_prefix = "/".join(
            segment
            for segment in folder.replace("\\", "/").split("/")
            if segment not in ("", ".")
        )
        metas: list[NoteMeta] = []
        for doc_id in store.note_ids():
            doc = store.winner(doc_id)
            if doc is None:
                continue
            try:
                meta = self._meta_from_doc(doc)
            except StorageError:
                path_for_log = doc.get("path") or doc_id
                logger.exception(
                    "skipping note %s: invalid metadata", path_for_log
                )
                continue
            path = meta["path"]
            if _has_hidden_segment(path):
                continue  # hidden files never surface as notes
            if folder_prefix:
                if not path.lower().startswith(folder_prefix.lower() + "/"):
                    continue
                remainder = path[len(folder_prefix) + 1 :]
            else:
                remainder = path
            if not recursive and "/" in remainder:
                continue  # direct children only, also at the vault root
            if extension is not None and not _matches_extension(
                path, extension
            ):
                continue
            metas.append(meta)
        metas.sort(key=lambda meta: meta["path"])
        return metas

    async def get_meta(self, path: str) -> NoteMeta:
        """Metadata with a mandatory opaque rev; NoteNotFound when absent.

        The path resolves to a doc id through the vault's path identity
        (``path2id`` with the NFC flag fixed at startup), because journal
        doc ids were derived by clients with that same identity — so NFC
        and legacy spellings both hit the document the client wrote.
        """
        self._validate_read_path(path)
        store = self._require_store("get_meta")
        doc = store.winner(self._path2id(store, path))
        if doc is None or not is_note_document(doc):
            raise NoteNotFound(f"note not found: {path}")
        return self._meta_from_doc(doc)

    async def read_note(self, path: str) -> NoteContent | None:
        """Decoded note body, or ``None`` when the note is absent.

        Soft-deleted notes are absent for all read paths (audit B12). A
        corrupt body raises :class:`StorageError` and an E2EE note
        :class:`UnsupportedVault`, both naming the note path only.
        """
        try:
            validate_vault_path(path)
        except ForbiddenPathError as exc:
            logger.info("read refused for vault path %r: %s", path, exc)
            return None
        store = self._require_store("read_note")
        doc_id = self._path2id(store, path)
        try:
            content = store.read_note(doc_id)
        except ValueError as exc:
            raise self._translate_decode_error(exc) from None
        if content is None:
            return None
        winner, text, data, is_text = content
        meta = self._meta_from_doc(winner)
        return NoteContent(
            meta=meta, text=text, data=data, is_text=is_text, rev=meta["rev"]
        )

    async def iterate_texts(
        self, extension: str = ".md"
    ) -> AsyncIterator[tuple[NoteMeta, str | None]]:
        """Stream ``(meta, text)`` for every note matching ``extension``.

        Ordering and consistency: candidates are the live note documents
        of the current replay state, ordered by display path, and each
        note's body is read from the local snapshot at its turn — a
        concurrent ``refresh``/reset may interleave, so cross-note
        snapshot consistency is not guaranteed (per-note only). Referenced
        chunks live in the store until an epoch/wipe reset, whose worst
        case here is a per-note ``(meta, None)`` after logging, never a
        failed scan (audit C4). Never calls ``refresh``: tools own it.
        The extension match is case-insensitive, like the CouchDB peer.
        """
        store = self._require_store("iterate_texts")
        candidates: list[NoteMeta] = []
        for doc_id in store.note_ids():
            doc = store.winner(doc_id)
            if doc is None:
                continue
            path = doc.get("path")
            path = path if isinstance(path, str) and path else doc_id
            if _has_hidden_segment(path):
                continue
            if not _matches_extension(path, extension):
                continue
            try:
                candidates.append(self._meta_from_doc(doc))
            except StorageError:
                logger.exception("skipping note %s: invalid metadata", path)
                continue
        candidates.sort(key=lambda meta: meta["path"])
        # Window over the candidates like the CouchDB peer's fetch windows:
        # bounded work per slice, no cross-window state held alive.
        for start in range(0, len(candidates), _TEXT_WINDOW_SIZE):
            for meta in candidates[start : start + _TEXT_WINDOW_SIZE]:
                yield await self._scan_one_note(store, meta)

    async def _scan_one_note(
        self, store: JournalStore, meta: NoteMeta
    ) -> tuple[NoteMeta, str | None]:
        """Read one note for a scan, isolating every failure (audit C4).

        Any per-note failure — corrupt bodies, encrypted bodies, or an
        unexpected store error (OSError, sqlite) — yields ``(meta, None)``
        with the details logged, so one broken note never ends a
        vault-wide scan. Only backend-wide storage unavailability aborts.
        """
        try:
            content = store.read_note(meta["doc_id"])
            if content is not None:
                return self._meta_from_doc(content[0]), content[1]
        except StorageUnavailable:
            raise  # backend-wide condition, not a per-note failure
        except Exception:
            logger.exception(
                "skipping note %s: scan read failed", meta["path"]
            )
            return meta, None
        return meta, None

    # -- Read helpers ----------------------------------------------------------

    @staticmethod
    def _validate_read_path(path: str) -> None:
        """Read-side namespace gate (same policy as the CouchDB peer).

        Forbidden paths (service namespaces, hidden segments, reserved
        names) report the note as absent with the reason logged
        server-side; no store access happens.
        """
        try:
            validate_vault_path(path)
        except ForbiddenPathError as exc:
            logger.info("read refused for vault path %r: %s", path, exc)
            raise NoteNotFound(f"note not found: {path}") from None

    @staticmethod
    def _path2id(store: JournalStore, path: str) -> str:
        """Resolve a vault path to a doc id with the vault's identity.

        Doc ids in the journal were derived by clients using the vault's
        path identity (NFC normalization for v1 vaults); resolving with
        any other identity would silently miss their documents.
        """
        return path2id(path, nfc=store.path_identity == PATH_IDENTITY_NFC)

    @staticmethod
    def _translate_decode_error(exc: ValueError) -> StorageError:
        """Map the store's ValueError to the public storage exception.

        Classification is structural — the store's
        :class:`~journal_store.EncryptedNoteError` keeps its
        UnsupportedVault semantics — never by message text, which embeds
        the user-controlled note path. Corrupt-body messages already match
        the CouchDB peer's phrasing and pass through unchanged (they name
        the note path only — audit C7).
        """
        if isinstance(exc, EncryptedNoteError):
            return UnsupportedVault(str(exc))
        return StorageError(str(exc))

    @staticmethod
    def _meta_from_doc(doc: dict) -> NoteMeta:
        """Build :class:`NoteMeta` from a winner doc (CouchDB parity).

        Display path is the doc's canonical ``path`` field with the doc id
        as fallback; a missing ``size`` is computed from a legacy ``data``
        body; a winner without a revision token is a contract violation.
        """
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

    # -- Append-only publication ------------------------------------------------

    def _doc_id(self, path: str) -> str:
        store = self._require_store("write")
        return path2id(path, nfc=store.path_identity == PATH_IDENTITY_NFC)

    @staticmethod
    def _check_expected_rev(doc: dict | None, expected_rev: str | None) -> None:
        if expected_rev is not None and (
            doc is None or doc.get("_rev") != expected_rev
        ):
            raise ConcurrentModification(
                "note changed concurrently; re-read and retry"
            )

    @staticmethod
    def _next_revision(body: dict, parent: dict | None) -> dict:
        """Extend the destination's lineage, never a copied source's lineage.

        Older imported documents may lack parentage; in that case only the
        known winner hash can be carried forward. Never invent ancestors.
        """
        generation, ids = 0, []
        if parent is not None:
            try:
                generation, parent_hash = parse_rev(parent.get("_rev"))
            except ValueError as exc:
                raise StorageError("existing note has an invalid revision") from exc
            chain = parent.get("_revisions")
            if chain is None:
                ids = [parent_hash]
            elif (
                not isinstance(chain, dict)
                or type(chain.get("start")) is not int
                or chain["start"] != generation
                or not isinstance(chain.get("ids"), list)
                or not chain["ids"]
                or len(chain["ids"]) > generation
                or chain["ids"][0] != parent_hash
                or not all(isinstance(item, str) and item for item in chain["ids"])
            ):
                raise StorageError("existing note has invalid revision ancestry")
            else:
                ids = chain["ids"]
        result = clone_metadata(body)
        result.pop("_conflicts", None)
        result.pop("_deleted_conflicts", None)
        rev_hash = secrets.token_hex(16)
        result["_rev"] = f"{generation + 1}-{rev_hash}"
        result["_revisions"] = {"start": generation + 1, "ids": [rev_hash, *ids]}
        return result

    def _validate_chunk_reuse(self, chunk_id: str, content: str) -> None:
        """Refuse collisions rather than overwrite a shared immutable chunk.

        Compact chunks live in files; full-JSON leaf records live in SQLite.
        Check both representations, including when both are present.
        """
        store = self._require_store("write_note")
        try:
            chunk_file = store._chunk_path(chunk_id)
            if (
                chunk_file.exists()
                and chunk_file.read_bytes() != content.encode("utf-8")
            ):
                raise StorageError(
                    "existing shared content chunk holds different data"
                )
            doc = store.winner(chunk_id)
            if doc is not None and (
                doc.get("e_") or doc.get("deleted")
                or doc.get("type") != "leaf" or doc.get("data") != content
            ):
                raise StorageError(
                    "existing shared content chunk is encrypted or holds different data"
                )
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise StorageError(
                "existing shared content chunk could not be validated"
            ) from exc

    def _publish(self, chunks: list[tuple[str, str]], docs: list[dict]) -> None:
        """Confirm the exact journal before any local mutation.

        No await separates the precondition, put, verification and apply:
        synchronous object/store operations serialize mutations on the server
        event loop. Success confirms publication, not eventual winner status.
        A failed verification leaves local state untouched. The remote object
        is never deleted; a later refresh may replay a published orphan.
        """
        store = self._require_store("publish")
        try:
            payload = serialize_journal(chunks, docs)
            if len(payload) > self.max_inflated_bytes:
                raise StorageError("journal inflated output exceeds limit")
            try:
                journal = parse_journal(
                    payload, max_records=self.max_records,
                    max_line_bytes=self.max_line_bytes,
                )
            except ValueError as exc:
                raise StorageError(f"journal publication preflight failed: {exc}") from exc
            store.validate_journal(journal.chunks, journal.docs)
            blob = deflate_journal(payload)
            if len(blob) > getattr(self._object_storage, "max_object_bytes", MAX_OBJECT_BYTES):
                raise StorageError("journal compressed object size exceeds limit")
            # A fresh replay reads sync parameters before downloading journals.
            # Leave room for that control read and modest object growth.
            control_reserve = (
                64 * 1024 if self._sync_parameters_bytes is None
                else self._sync_parameters_bytes + 1024
            )
            if len(blob) + control_reserve > self.max_refresh_bytes:
                raise StorageError("journal download bytes exceeds refresh limit")
            if self._probe_remote_wipe(store.epoch or None):
                raise ConcurrentModification(
                    "remote was rebuilt; retry the operation after refresh"
                )
            ms = max(int(time.time() * 1000), self._max_seen_ms + 1)
            # Reserve even failed attempts so a retry cannot reuse their key.
            self._max_seen_ms = ms
            for _ in range(10):
                name = journal_filename(ms, suffix=secrets.token_hex(16))
                outcome = self._object_storage.put_if_absent(name, blob)
                if outcome == "created":
                    break
                if outcome == "exists":
                    continue
                if outcome != "unsupported":
                    raise StorageError("invalid conditional publication result")
                # B-10 compatibility fallback cannot exclude a racing writer.
                if not self._object_storage.exists(name):
                    self._object_storage.put(name, blob)
                    break
            else:
                raise StorageError("could not allocate an unused journal key")
            readback = self._object_storage.get(name)
            if readback is None:
                raise StorageError("journal read-back verification failed")
            decoded = inflate_journal(
                readback, max_output_bytes=self.max_inflated_bytes,
            )
            journal = parse_journal(
                decoded, name=name, max_records=self.max_records,
                max_line_bytes=self.max_line_bytes,
            )
            if (
                decoded != payload
                or journal.chunks != dict(chunks)
                or journal.docs != docs
            ):
                raise StorageError("journal read-back verification failed")
            store.apply_journal(name, journal.chunks, journal.docs)
            store.last_sent_key = name
        except StorageError:
            raise
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise StorageError("journal publication or verification failed") from exc

    async def write_note(
        self,
        path: str,
        content: str,
        *,
        create: bool = False,
        overwrite: bool = False,
        mtime: int | None = None,
        expected_rev: str | None = None,
    ) -> NoteMeta:
        """Replace a text body; only explicit create resurrects a tombstone."""
        validate_vault_path(path)
        if not is_writable_text_path(path):
            raise BinaryNoteError(
                f"note {path!r} has a binary extension; "
                "this server writes textual notes only"
            )
        store = self._require_store("write_note")
        doc_id = self._doc_id(path)
        parent = store.winner(doc_id)
        self._check_expected_rev(parent, expected_rev)
        live = parent is not None and not parent.get("deleted")
        if live and create and not overwrite:
            raise NoteExists(f"note already exists: {path}")
        if not live and not create:
            raise NoteNotFound(f"note not found: {path}")
        chunk_id = chunk_id_for(content)
        self._validate_chunk_reuse(chunk_id, content)
        stamp = int(time.time() * 1000) if mtime is None else mtime
        if parent is None:
            body = new_metadata_doc(
                path, [chunk_id], len(content.encode("utf-8")),
                doc_type_for(path), stamp, stamp,
            )
        else:
            body = clone_metadata(parent)
            body.pop("deleted", None)
            body.update(
                children=[chunk_id], eden={}, mtime=stamp,
                size=len(content.encode("utf-8")), type=doc_type_for(path),
            )
        body.update(_id=doc_id, path=canonical_path(path))
        body = self._next_revision(body, parent)
        self._publish([(chunk_id, content)], [body])
        return self._meta_from_doc(body)

    async def delete_note(
        self, path: str, expected_rev: str | None = None
    ) -> bool:
        """Append a soft deletion, preserving all body references."""
        validate_vault_path(path)
        store = self._require_store("delete_note")
        parent = store.winner(self._doc_id(path))
        self._check_expected_rev(parent, expected_rev)
        if parent is None or parent.get("deleted"):
            return False
        body = self._next_revision(
            soft_delete_revision(parent, int(time.time() * 1000)), parent
        )
        self._publish([], [body])
        return True

    def _source_doc(self, path: str) -> dict:
        doc = self._require_store("move/copy").winner(self._doc_id(path))
        if doc is None or not is_note_document(doc):
            raise NoteNotFound(f"note not found: {path}")
        return doc

    def _copy_revision(self, source: dict, new_path: str, overwrite: bool) -> dict:
        doc_id = self._doc_id(new_path)
        target = self._require_store("move/copy").winner(doc_id)
        if target is not None and not target.get("deleted") and not overwrite:
            raise NoteExists(f"note already exists: {new_path}")
        body = clone_metadata(
            source, _id=doc_id, path=canonical_path(new_path),
            mtime=int(time.time() * 1000),
        )
        return self._next_revision(body, target)

    async def rename_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Publish destination and source soft deletion in ONE journal.

        Same-path is a no-op; identity-preserving renames update only the
        display path and mtime. Single-file publication has no partial phases,
        so RenamePartialError (mapped by the tool layer for contract parity)
        is never raised by this backend. Failed verification reports
        StorageError.
        """
        validate_vault_path(path)
        validate_vault_path(new_path)
        new_path = canonical_path(new_path)
        if path == new_path:
            return await self.get_meta(path)
        source = self._source_doc(path)
        if self._doc_id(path) == self._doc_id(new_path):
            body = self._next_revision(
                clone_metadata(
                    source, path=new_path, mtime=int(time.time() * 1000),
                ),
                source,
            )
            self._publish([], [body])
        else:
            body = self._copy_revision(source, new_path, overwrite)
            retired = self._next_revision(
                soft_delete_revision(source, int(time.time() * 1000)), source
            )
            self._publish([], [body, retired])
        return self._meta_from_doc(body)

    async def copy_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Copy references (including eden), preserving body and ctime."""
        validate_vault_path(path)
        validate_vault_path(new_path)
        source = self._source_doc(path)
        body = self._copy_revision(source, new_path, overwrite)
        self._publish([], [body])
        return self._meta_from_doc(body)
