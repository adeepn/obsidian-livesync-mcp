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
with that same identity. Note writes arrive in Task 7 and raise
``NotImplementedError``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from journal_codec import inflate_journal, parse_journal
from journal_store import JournalStore
from livesync_format import (
    PATH_IDENTITY_LEGACY,
    PATH_IDENTITY_NFC,
    ForbiddenPathError,
    UnsupportedVaultSettings,
    is_note_document,
    path2id,
    resolve_path_identity,
    validate_vault_path,
)
from object_storage import JournalObjectStorage
from storage_base import (
    NoteContent,
    NoteMeta,
    NoteNotFound,
    StorageError,
    UnsupportedVault,
)

logger = logging.getLogger(__name__)

# Control objects, never journals. Upstream commonlib:
# LiveSyncJournalReplicator.ts:64 (MILSTONE_DOCID) and
# common/models/sync.definition.ts:11 (DOCID_JOURNAL_SYNC_PARAMETERS).
MILESTONE_KEY = "_00000000-milestone.json"
SYNC_PARAMETERS_KEY = "_obsidian_livesync_journal_sync_parameters.json"

# Journal file names: "<ms>-docs.jsonl.gz" (journal_codec.journal_filename).
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


def _has_hidden_segment(path: str) -> bool:
    """Whether any path segment is a dotfile (``.obsidian``, ...)."""
    return any(segment.startswith(".") for segment in path.split("/"))


def _matches_extension(path: str, extension: str) -> bool:
    suffix = extension if extension.startswith(".") else "." + extension
    return path.lower().endswith(suffix.lower())


def _as_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


# The store signals E2EE refusals and corruption as ValueError; its E2EE
# message text is the discriminator (kept identical to the CouchDB peer's).
_E2EE_MARKER = "end-to-end encryption"


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
    ):
        self._object_storage = object_storage
        self._state_dir = Path(state_dir)
        self.refresh_min_interval = refresh_min_interval
        self._store: JournalStore | None = None
        # Throttle clock (monotonic) and the wall-clock stamp for diagnostics.
        self._last_refresh: float | None = None
        self._last_refresh_at: datetime | None = None

    # -- StorageBackend: lifecycle -------------------------------------------

    async def startup_check(self) -> None:
        """Verify bucket compatibility and reconcile local state.

        Raises :class:`~storage_base.UnsupportedVault` for a locked
        milestone, missing milestone with journals present, settings this
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
                raise UnsupportedVault(
                    "vault milestone is locked; another client is "
                    "rebuilding or maintaining the vault"
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

        One complete listing per refresh (no StartAfter cursor — Plan B
        Ruling B-8, since lexicographic ordering hides numerically later
        journals); already-processed files are skipped idempotently.
        Refreshes closer than ``refresh_min_interval`` together are
        skipped unless ``force`` is set; a skipped refresh is a no-op.
        Candidates are ordered by their numeric ms prefix and applied one
        by one; the first failing file stops the replay without advancing
        the checkpoint (its predecessors stay processed), so the next
        refresh retries from the same file.
        """
        store = self._require_store("refresh")
        now = time.monotonic()
        if (
            not force
            and self._last_refresh is not None
            and now - self._last_refresh < self.refresh_min_interval
        ):
            return
        candidates = self._list_unprocessed_journals(store)
        for name in candidates:
            self._replay_one(store, name)
        # Arm the throttle only after a completed pass: failed refreshes
        # stay retryable immediately.
        self._last_refresh = time.monotonic()
        self._last_refresh_at = datetime.now(timezone.utc)
        if candidates:
            logger.info(
                "journal replay applied %d file(s), last %r",
                len(candidates),
                candidates[-1],
            )

    async def backend_info(self) -> dict:
        """Diagnostics for ``vault_stats`` and startup logs."""
        store = self._store
        return {
            "backend": "s3-journal",
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
        blob = self._object_storage.get(MILESTONE_KEY)
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
        blob = self._object_storage.get(SYNC_PARAMETERS_KEY)
        if blob is None:
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

    def _probe_remote_wipe(self, epoch: str | None) -> None:
        """Reset local state when the last-sent journal has vanished.

        A missing object for the last file this peer sent or replayed
        means the bucket was wiped or rebuilt (objects are never deleted
        individually, spec section 7), like upstream's
        ``ensureCheckpointCachesAreFresh`` wipe probe. ``epoch``, when
        known, is re-recorded after the reset so the next startup
        compares against the correct value.
        """
        store = self._store
        if store is None:
            return
        key = store.last_sent_key
        if not key or self._object_storage.exists(key):
            return
        logger.warning(
            "journal object %r vanished; assuming a remote wipe and "
            "resetting local state",
            key,
        )
        store.reset()
        if epoch is not None:
            store.epoch = epoch
        self._clear_refresh_clock()

    def _clear_refresh_clock(self) -> None:
        """After a state reset, the next refresh must not be throttled."""
        self._last_refresh = None
        self._last_refresh_at = None

    # -- Replay helpers ----------------------------------------------------------

    def _list_unprocessed_journals(self, store: JournalStore) -> list[str]:
        """Journal names to replay: one complete listing, control keys and
        already-processed files dropped, ordered by numeric ms prefix.

        Deliberately no StartAfter cursor (Plan B Ruling B-8): S3 lists in
        lexicographic order, where ``"10-..."`` sorts *before* ``"9-..."``,
        so a cursor at the greatest processed name would permanently hide
        numerically later journals that sort lexicographically earlier. A
        full listing is one API call on a throttled (>= refresh_min_interval)
        refresh, and the processed-set filter keeps replay idempotent.
        """
        keys = self._object_storage.list("")
        candidates = [
            key
            for key in keys
            if is_journal_key(key) and not store.processed(key)
        ]
        candidates.sort(key=journal_sort_key)
        return candidates

    def _replay_one(self, store: JournalStore, name: str) -> None:
        """Fetch, decode and apply one journal file.

        Storage-layer failures propagate unchanged (they already name the
        key); decoding or store failures become :class:`StorageError`
        naming the file, and the file stays unprocessed so the next
        refresh retries it.
        """
        try:
            blob = self._object_storage.get(name)
            if blob is None:
                raise StorageError(
                    f"journal file {name!r} disappeared during replay"
                )
            journal = parse_journal(inflate_journal(blob), name=name)
            store.apply_journal(name, journal.chunks, journal.docs)
        except StorageError:
            raise
        except (ValueError, OSError, sqlite3.Error) as exc:
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
        """Read one note for a scan, isolating every failure (audit C4)."""
        try:
            content = store.read_note(meta["doc_id"])
        except ValueError as exc:
            logger.warning("%s", exc)
            return meta, None
        if content is None:
            return meta, None
        try:
            fresh = self._meta_from_doc(content[0])
        except StorageError:
            logger.exception(
                "skipping note %s: invalid metadata", meta["path"]
            )
            return meta, None
        return fresh, content[1]

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

        E2EE refusals keep their UnsupportedVault semantics; corrupt-body
        messages already match the CouchDB peer's phrasing and pass
        through unchanged (they name the note path only — audit C7).
        """
        if _E2EE_MARKER in str(exc):
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
        raise NotImplementedError("journal note writes arrive in Task 7")

    async def delete_note(
        self, path: str, expected_rev: str | None = None
    ) -> bool:
        raise NotImplementedError("journal note writes arrive in Task 7")

    async def rename_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        raise NotImplementedError("journal note writes arrive in Task 7")

    async def copy_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        raise NotImplementedError("journal note writes arrive in Task 7")
