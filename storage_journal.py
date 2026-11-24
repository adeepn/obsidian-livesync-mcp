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

from wire_encoding import text_encoder_string, surrogate_count

from diagnostic_hygiene import (writer_diagnostic, diagnostic, settings_exception_diagnostic, PublicDiagnostic, setting_key,
    DiagnosticCode, assessment_fields, setting_value, fingerprint, cause_name)

from text_eligibility import TextAccessPolicy, project_note, scan_text
from text_eligibility import (DEFAULT_TEXT_SCAN_MAX_BYTES,
                              DEFAULT_TEXT_VERDICT_CACHE_ENTRIES)

import asyncio
from device_selection import DeviceSelection

import hashlib
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
from content_splitter import ContentSplitter, chunk_id_for_piece
from livesync_format import (
    PATH_IDENTITY_LEGACY,
    ForbiddenPathError,
    UnsupportedVaultSettings,
    UnsupportedWriterSettings,
    assemble_binary,
    canonical_path,
    resolve_content_splitter, resolve_tweak_startup,
    resolve_sync_max_size,
    clone_metadata,
    prepare_file_content,
    is_note_document,
    new_metadata_doc,
    identity_path2id,
    validate_document_identity,
    resolve_path_identity,
    soft_delete_revision,
    validate_vault_path,
)
from replay_limits import ReplayLimits, ReplayBudgetExceeded
from object_storage import JournalObjectStorage, ObjectSizeLimit, MAX_OBJECT_BYTES
from storage_base import (
    check_file_size,
    WriteSkipped,
    RenameRefused,
    ConcurrentModification,
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

# Control objects, never journals. Upstream commonlib:
# LiveSyncJournalReplicator.ts:64 (MILSTONE_DOCID) and
# common/models/sync.definition.ts:11 (DOCID_JOURNAL_SYNC_PARAMETERS).
MILESTONE_KEY = "_00000000-milestone.json"
SYNC_PARAMETERS_KEY = "_obsidian_livesync_journal_sync_parameters.json"

# Journal names have an optional suffix after ms (journal_codec.journal_filename).
# Server publications use a random 32-hex suffix; devices use the base form.
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

# File metadata fields from livesync_format.new_metadata_doc and the live golden
# journal, including revision ancestry and both tombstone representations.
KNOWN_METADATA_KEYS = frozenset({
    "_id", "_rev", "_revisions", "path", "ctime", "mtime", "size", "type",
    "children", "eden", "deleted", "_deleted",
})

# Operational refresh budgets; derivation and deliberate parity difference
# are documented in replay_limits.py. No dependence on local file selection.
MAX_REFRESH_KEYS = ReplayLimits.max_refresh_keys
MAX_REFRESH_BYTES = ReplayLimits.max_refresh_bytes
MAX_REFRESH_SECONDS = ReplayLimits.max_refresh_seconds
_LIST_PAGE_SIZE = 1000


class _RefreshBudgetStop(ReplayBudgetExceeded):
    """Public budget failure after preserving completed replay progress."""



def _has_hidden_segment(path: str) -> bool:
    """Whether any path segment is a dotfile (``.obsidian``, ...)."""
    return any(segment.startswith(".") for segment in path.split("/"))


def _matches_extension(path: str, extension: str) -> bool:
    if not extension:
        return True
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


class JournalBackend(TextAccessPolicy, DeviceSelection):
    """``StorageBackend`` peer for an Obsidian LiveSync journal bucket.

    The store under ``state_dir`` is created on the first
    :meth:`startup_check`, after the milestone has fixed the vault's path
    identity; reopening the same state directory against a vault with a
    different identity quarantines the old directory and starts fresh.
    All object access is synchronous per the :class:`JournalObjectStorage`
    protocol.
    """

    def __init__(
        self,
        object_storage: JournalObjectStorage,
        state_dir: Path,
        refresh_min_interval: float = 5.0,
        *,
        text_verdict_cache_entries: int = DEFAULT_TEXT_VERDICT_CACHE_ENTRIES,
        state_size_warn_mb: int = 0,
        max_refresh_keys: int = MAX_REFRESH_KEYS,
        max_refresh_bytes: int = MAX_REFRESH_BYTES,
        max_refresh_seconds: float = MAX_REFRESH_SECONDS,
        max_inflated_bytes: int = MAX_INFLATED_BYTES,
        max_records: int = MAX_RECORDS,
        max_line_bytes: int = MAX_LINE_BYTES,
        max_json_depth: int = ReplayLimits.max_json_depth,
    ):
        for value in (max_refresh_keys, max_refresh_bytes, max_refresh_seconds,
                      max_inflated_bytes, max_records, max_line_bytes, max_json_depth):
            if value <= 0 or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError("journal limits must be positive and finite")
        if text_verdict_cache_entries <= 0:
            raise ValueError("text verdict cache entries must be positive")
        self.text_verdict_cache_entries = text_verdict_cache_entries
        self.max_refresh_keys = max_refresh_keys
        self.max_refresh_bytes = max_refresh_bytes
        self.max_refresh_seconds = max_refresh_seconds
        self.max_inflated_bytes = max_inflated_bytes
        self.max_records = max_records
        self.max_line_bytes = max_line_bytes
        self.max_json_depth = max_json_depth
        self._listing_cursor = ""
        self._refresh_deadline: float | None = None
        self._refresh_downloaded = 0
        self._sync_parameters_bytes: int | None = None
        self._object_storage = object_storage
        self._state_dir = Path(state_dir)
        self.state_size_warn_mb = state_size_warn_mb
        self.refresh_min_interval = refresh_min_interval
        self._store: JournalStore | None = None
        self._tweak_assessment = {"status": "OK", "differing_keys": []}
        self._writer_unsupported_reason = None
        self._splitter = ContentSplitter()
        self.sync_max_size_mb = 50
        self.sync_max_size_mb_override = None
        # Throttle clock (monotonic) and the wall-clock stamp for diagnostics.
        self._last_refresh_failure: Exception | None = None
        self._last_refresh: float | None = None
        self._last_refresh_at: datetime | None = None
        self._protocol_notes: list[str] = []
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
        self._tweak_assessment = {"status": "OK", "differing_keys": []}
        self._writer_unsupported_reason = None
        milestone = self._load_milestone()
        if milestone is None:
            if self._has_remote_journals():
                raise UnsupportedVault(
                    "vault milestone is missing while journal files exist; "
                    "rebuild from the authoritative vault"
                )
            logger.warning('journal bucket has no milestone and no journal files; treating it as a fresh vault with legacy path identity')
            self.resolve_selection_settings({'pathNormalizationVersion': 0})
            identity = PATH_IDENTITY_LEGACY
            self._splitter = ContentSplitter()
            self.sync_max_size_mb = (50 if self.sync_max_size_mb_override is None
                                     else self.sync_max_size_mb_override)
        else:
            if milestone.get("locked") is True:
                logger.warning('vault milestone has advisory locked: true flag; remote marked as rebuilt; peer performs full replay')
            try:
                self.resolve_selection_settings(milestone)
                if self.sync_max_size_mb_override is None:
                    resolve_sync_max_size(milestone)  # retain all-device fallback guards
                resolved = resolve_tweak_startup(milestone,
                    disable_checking_config_mismatch=getattr(self, 'disable_checking_config_mismatch', False),
                    auto_accept_compatible_tweak=getattr(self, 'auto_accept_compatible_tweak', True))
                self._tweak_assessment = resolved.summary()
                for side in resolved.winners:
                    logger.info("tweak alignment: %s; keys: %s", side if side in ("CURRENT", "REMOTE") else "<value hidden>", ", ".join(setting_key(k) for k in resolved.differing_keys))
                milestone = dict(resolved.settings)
                identity = resolve_path_identity(milestone)
                self.sync_max_size_mb = (resolve_sync_max_size(milestone)
                    if self.sync_max_size_mb_override is None else self.sync_max_size_mb_override)
                self._splitter = resolve_content_splitter(milestone)
            except UnsupportedWriterSettings as exc:
                self._splitter = None
                self._writer_unsupported_reason = PublicDiagnostic(DiagnosticCode.WRITER, issues=tuple(getattr(exc, "setting_issues", ())))
            except UnsupportedVaultSettings as exc:
                self._tweak_assessment = getattr(exc, "tweak_assessment", self._tweak_assessment)
                raise UnsupportedVault(settings_exception_diagnostic(exc)) from exc

        await self.selection_startup()

        # Identity changes quarantine the old state without mixing IDs.
        # A repeated startup replaces the previous connection cleanly.
        if self._store is not None:
            self._store.close()
            self._store = None
        self._store = JournalStore(
            self._state_dir, identity,
            text_verdict_cache_entries=self.text_verdict_cache_entries)

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
            await self.refresh_selection_files()
            return
        if self._store.epoch != epoch:
            self._store.epoch = epoch
            self._clear_refresh_clock()
        self._probe_remote_wipe(epoch)

        await self.refresh_selection_files()

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
                if isinstance(self._last_refresh_failure, ReplayBudgetExceeded):
                    raise self._last_refresh_failure
                raise StorageError(
                    "refresh failed previously; retry after interval or force"
                ) from self._last_refresh_failure
            return
        # C:serviceFeatures/targetFilter.ts:129-131 reloads realised settings.
        # Journal control objects live outside the replay stream. Resolve on
        # every unthrottled refresh, including recovery to the previous bytes
        # after a rejected milestone. Only effective config changes recheck Node.
        milestone = self._load_milestone()
        try:
            self.resolve_selection_settings(milestone if milestone is not None else
                                            {'pathNormalizationVersion': 0})
            if self._selection_checked_configuration != self._selection_configuration():
                await self.selection_startup()
        except UnsupportedVaultSettings as exc:
            raise UnsupportedVault(settings_exception_diagnostic(exc)) from exc
        self._refresh_deadline = now + self.max_refresh_seconds
        self._refresh_downloaded = 0
        recent_docs: list[dict] | None = None
        try:
            # Resolve resets before choosing the listing continuation.
            epoch = self._load_sync_parameters_epoch()
            if store.epoch != (epoch or ""):
                store.epoch = epoch or ""
                self._clear_refresh_clock()
            self._reconcile_outbound(store)
            self._probe_remote_wipe(epoch)
            candidates, next_cursor = self._list_journals()
            for name in candidates:
                self._check_refresh_time(name)
                try:
                    name.encode('utf-8')  # S3 names must be Unicode scalar values.
                except UnicodeError as exc:
                    raise StorageError(PublicDiagnostic(DiagnosticCode.REPLAY_FAILED,
                        journal=name, cause=type(exc))) from None
                if not store.processed(name):
                    recent_docs = self._replay_one(store, name)
            # Advance only after this entire window was replayed. A byte/time
            # stop or failure retries the same window, skipping its ledger.
            self._listing_cursor = next_cursor
            if next_cursor:
                raise _RefreshBudgetStop('max_refresh_keys', self.max_refresh_keys,
                                         self.max_refresh_keys)
        except Exception as exc:
            self._last_refresh_failure = exc
            raise
        else:
            self._last_refresh_failure = None
        finally:
            # Inspect only the last successful journal, once per refresh.
            # No-op and failed replay retain the last successful observation.
            if recent_docs is not None:
                # Full-JSON chunk records share journal.docs with metadata.
                keys = {
                    key for doc in recent_docs
                    if not doc["_id"].startswith("h:")
                    for key in doc
                }
                unknown = sorted(keys - KNOWN_METADATA_KEYS)
                self._protocol_notes = (
                    [f"unknown metadata fields in recent journals: {len(unknown)}"]
                    if unknown else []
                )
            self._refresh_deadline = None
            # Failures also arm the throttle; force=True bypasses it.
            self._last_refresh = time.monotonic()
            self._last_refresh_at = datetime.now(timezone.utc)

        await self.refresh_selection_files()

    def _check_refresh_time(self, point: str) -> None:
        if self._refresh_deadline is not None and time.monotonic() >= self._refresh_deadline:
            raise _RefreshBudgetStop('max_refresh_seconds',
                self.max_refresh_seconds + time.monotonic() - self._refresh_deadline,
                self.max_refresh_seconds)

    def _get_object(self, key: str) -> bytes | None:
        if self._refresh_deadline is None:
            return self._object_storage.get(key)
        self._check_refresh_time(key)
        remaining = self.max_refresh_bytes - self._refresh_downloaded
        if remaining <= 0:
            raise _RefreshBudgetStop('max_refresh_bytes',
                self._refresh_downloaded + 1, self.max_refresh_bytes)
        try:
            blob = self._object_storage.get(key, max_bytes=remaining)
        except ObjectSizeLimit as exc:
            if remaining < getattr(self._object_storage, "max_object_bytes", MAX_OBJECT_BYTES):
                raise _RefreshBudgetStop('max_refresh_bytes',
                    self._refresh_downloaded + exc.observed, self.max_refresh_bytes) from None
            raise
        self._refresh_downloaded += len(blob) if blob is not None else 0
        self._check_refresh_time(key)
        return blob

    async def backend_info(self) -> dict:
        """Diagnostics for ``vault_stats`` and startup logs."""
        store = self._store
        selection = await self.selection_diagnostics()
        return {
            **selection,
            **self.text_limits(),
            "tweak_assessment": assessment_fields(self._tweak_assessment),
            "writer_supported": self._writer_unsupported_reason is None and selection["selection_writer_supported"],
            "writer_unsupported_reason": writer_diagnostic(self._writer_unsupported_reason or selection["selection_unsupported_reason"]),
            "backend": "s3-journal",
            "custom_headers_configured": bool(getattr(self._object_storage, "custom_headers_count", 0)),
            "custom_headers_count": getattr(self._object_storage, "custom_headers_count", 0),
            "protocol_notes": list(self._protocol_notes),
            "state_dir_path": "<value hidden>",
            "state_dir_bytes": store.state_size_bytes() if store else 0,
            "state_size_warn_mb": self.state_size_warn_mb,
            "path_identity": setting_value(store.path_identity) if store else None,
            "epoch": fingerprint(store.epoch) if store else None,
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
        await self.selection_close()
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

    def _outbound_checkpoint(self, store: JournalStore) -> str:
        keys = [name for name, _, confirmed in store.outbound_journals() if confirmed]
        if store.last_sent_key:
            keys.append(store.last_sent_key)
        return max(keys, key=journal_sort_key) if keys else ""

    def _reconcile_outbound(self, store: JournalStore) -> None:
        """Recover send ownership independently of fallible last-sent bookkeeping.

        A pending key alone is not ownership: a conditional PUT may have lost
        to another peer. Match the exact compressed payload before confirming.
        Missing intents were never confirmed published and can be discarded.
        Confirmed missing keys remain wipe sentinels, never replaced by older
        surviving history. All replay downloads use the normal refresh budget.
        """
        for name, digest, confirmed in sorted(store.outbound_journals(), key=lambda row: journal_sort_key(row[0])):
            self._check_refresh_time(name)
            if not confirmed:
                blob = self._get_object(name)
                if blob is None or hashlib.sha256(blob).hexdigest() != digest:
                    store.discard_outbound(name)
                    continue
                self._replay_one(store, name, blob=blob)
                store.confirm_outbound(name)
            elif not store.processed(name) and self._object_storage.exists(name):
                self._replay_one(store, name)
        newest = self._outbound_checkpoint(store)
        if newest and any(name == newest and confirmed for name, _, confirmed in store.outbound_journals()):
            store.last_sent_key = newest
            store.compact_outbound(newest)

    def _probe_remote_wipe(self, epoch: str | None) -> bool:
        """Confirm a missing last-sent journal and clear only dedupe caches.

        Refresh/startup use this mutating reconciliation; publication uses a
        separate read-only precondition so a refused write changes nothing.
        """
        store = self._store
        if store is None:
            return False
        key = self._outbound_checkpoint(store)
        # Startup may precede replay of an accepted send with a lost response.
        # Defer a wipe decision until refresh has reconciled these intents.
        if any(not confirmed and self._object_storage.exists(name)
               for name, _, confirmed in store.outbound_journals()):
            return False
        if not key or self._object_storage.exists(key):
            return False
        logger.warning('journal object <omitted> vanished; assuming a remote wipe and clearing dedupe caches while retaining local notes')
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
            logger.warning('journal refresh key budget stop at <omitted>')
        for key in keys:
            match = _MS_PREFIX_RE.match(key)
            if match is not None:
                self._max_seen_ms = max(self._max_seen_ms, int(match.group(1)))
        return sorted((key for key in keys if is_journal_key(key)), key=journal_sort_key), next_cursor

    def _replay_one(self, store: JournalStore, name: str, *, blob: bytes | None = None) -> list[dict]:
        """Fetch, decode and apply one journal file.

        Storage-layer failures propagate unchanged (they already name the
        key); decoding or store failures become :class:`StorageError`
        naming the file, and the file stays unprocessed so the next
        refresh retries it.
        """
        try:
            if blob is None:
                blob = self._get_object(name)
            if blob is None:
                raise StorageError(
                    f"journal file {name!r} disappeared during replay"
                )
            journal = parse_journal(
                inflate_journal(blob, max_output_bytes=self.max_inflated_bytes),
                name=name, max_records=self.max_records, max_line_bytes=self.max_line_bytes, max_json_depth=self.max_json_depth,
            )
            for doc in journal.docs:
                validate_document_identity(doc, store.path_identity)
            store.apply_journal(name, journal.chunks, journal.docs)
            return journal.docs[-100:]
        except (StorageUnavailable, ReplayBudgetExceeded):
            raise
        except (StorageError, ValueError, OSError, sqlite3.Error) as exc:
            # Public diagnostics admit only a validated journal name and cause
            # class. Default logs omit remote names, payloads and tracebacks.
            logger.warning('journal file <omitted> could not be replayed: <omitted>: <omitted>; cause: %s', cause_name(type(exc)))
            raise StorageError(PublicDiagnostic(DiagnosticCode.REPLAY_FAILED, journal=name, cause=type(exc))) from exc

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
        using the vault identity (original paths in the response),
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
                logger.error('skipping note <omitted>: invalid metadata')
                continue
            path = meta["path"]
            if _has_hidden_segment(path):
                continue  # hidden files never surface as notes
            if folder_prefix:
                path_key = self._path2id(store, path)
                folder_key = self._path2id(store, folder_prefix)
                if not path_key.startswith(folder_key + "/"):
                    continue
                remainder = path_key[len(folder_key) + 1 :]
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
            logger.info('read refused for vault path <omitted>: <omitted>; cause: %s', cause_name(type(exc)))
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

    async def read_text_note(self, path: str) -> NoteContent | None:
        self._validate_read_path(path)
        store = self._require_store('read_text_note')
        doc = store.winner(self._path2id(store, path))
        if doc is None or not is_note_document(doc):
            return None
        if doc.get('type') != 'newnote':
            return project_note(await self.read_note(path), self.text_scan_max_bytes)
        text = store.scan_newnote(doc, self.text_scan_max_bytes, strict_budget=True)
        meta = self._meta_from_doc(doc)
        return NoteContent(meta=meta, text=text, data=None,
                           is_text=text is not None, rev=meta['rev'])

    async def iterate_texts(
        self, extension: str = "", *, after: str = ""
    ) -> AsyncIterator[tuple[NoteMeta, str | None]]:
        """Stream ``(meta, text)`` for every note matching ``extension``.

        Ordering and consistency: candidates are the live note documents
        of the current replay state, ordered by display path, and each
        note's body is read from the local snapshot at its turn — a
        concurrent ``refresh``/reset may interleave, so cross-note
        snapshot consistency is not guaranteed (per-note only). Referenced
        chunks survive epoch/wipe dedupe resets. An unreadable note yields
        per-note ``(meta, None)`` after logging rather than failing the
        scan (audit C4). Never calls ``refresh``: tools own it.
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
            if path <= after:
                continue
            if _has_hidden_segment(path):
                continue
            if not _matches_extension(path, extension):
                continue
            try:
                candidates.append(self._meta_from_doc(doc))
            except StorageError:
                logger.error('skipping note <omitted>: invalid metadata')
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
        with a class-only event logged, so one broken note never ends a
        vault-wide scan. Only backend-wide storage unavailability aborts.
        """
        try:
            doc = store.winner(meta["doc_id"])
            if doc is not None and doc.get('type') == 'newnote':
                return self._meta_from_doc(doc), store.scan_newnote(
                    doc, getattr(self, 'text_scan_max_bytes', DEFAULT_TEXT_SCAN_MAX_BYTES))
            content = store.read_note(meta["doc_id"])
            if content is not None:
                return self._meta_from_doc(content[0]), scan_text(content[1], self.text_scan_max_bytes)
        except (StorageUnavailable, ReplayBudgetExceeded):
            raise  # backend-wide condition, not a per-note failure
        except Exception:
            logger.error('skipping note <omitted>: scan read failed')
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
            logger.info('read refused for vault path <omitted>: <omitted>; cause: %s', cause_name(type(exc)))
            raise NoteNotFound(f"note not found: {path}") from None

    @staticmethod
    def _path2id(store: JournalStore, path: str) -> str:
        """Resolve a vault path to a doc id with the vault's identity.

        Doc ids in the journal were derived by clients using the vault's
        path identity (NFC normalization for v1 vaults); resolving with
        any other identity would silently miss their documents.
        """
        return identity_path2id(path, store.path_identity)

    @staticmethod
    def _translate_decode_error(exc: ValueError) -> StorageError:
        """Map the store's ValueError to the public storage exception.

        Classification is structural — the store's
        :class:`~journal_store.EncryptedNoteError` keeps its
        UnsupportedVault semantics — never by message text, which embeds
        the remote note path. Corrupt-body diagnostics carry a closed reason
        and an explicitly remote path; exception text never passes through.
        """
        if isinstance(exc, EncryptedNoteError):
            return UnsupportedVault(getattr(exc, "diagnostic", None), cause=exc)
        return StorageError(getattr(exc, "diagnostic", None), cause=exc)

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
        return identity_path2id(path, store.path_identity)

    def _validate_identity(self, doc: dict | None) -> None:
        if doc is not None:
            try:
                validate_document_identity(
                    doc, self._require_store("identity").path_identity)
            except ValueError as exc:
                raise StorageError(DiagnosticCode.METADATA_PATH_IDENTITY_MISMATCH_OR_COLLISION_REBUILD_FETCH_REQUIRED, cause=exc) from exc

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
                and chunk_file.read_bytes() != text_encoder_string(content).encode("utf-8")
            ):
                raise StorageError(
                    "existing shared content chunk holds different data"
                )
            doc = store.winner(chunk_id)
            if doc is not None and (
                doc.get("e_") or doc.get("deleted")
                or doc.get("type") != "leaf" or doc.get("data") != text_encoder_string(content)
            ):
                raise StorageError(
                    "existing shared content chunk is encrypted or holds different data"
                )
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise StorageError(
                "existing shared content chunk could not be validated"
            ) from exc

    def _publish(
        self, chunks: list[tuple[str, str]], docs: list[dict], *,
        attachment_data: bytes | None = None,
    ) -> int:
        """Confirm the exact journal before any local mutation.

        No await separates the precondition, put, verification and apply:
        synchronous object/store operations serialize mutations on the server
        event loop. Success confirms publication, not eventual winner status.
        A failed verification leaves local state untouched. The remote object
        is never deleted; a later refresh may replay a published orphan.
        Return the number of surrogate units replaced in emitted compact frames.
        """
        store = self._require_store("publish")
        published = False
        try:
            payload = serialize_journal(chunks, docs)
            if len(payload) > self.max_inflated_bytes:
                raise ReplayBudgetExceeded("max_inflated_bytes", len(payload), self.max_inflated_bytes)
            try:
                journal = parse_journal(
                    payload, max_records=self.max_records,
                    max_line_bytes=self.max_line_bytes, max_json_depth=self.max_json_depth,
                )
            except ReplayBudgetExceeded:
                raise
            except ValueError as exc:
                raise StorageError("journal publication preflight failed: invalid journal") from exc
            for doc in journal.docs:
                validate_document_identity(doc, store.path_identity)
            store.validate_journal(journal.chunks, journal.docs)
            # journalRecords.ts:70-76: compact leaves are TextEncoder-lossy;
            # metadata JSON is lossless. Validate the exact projected frame.
            expected_chunks = {chunk_id: text_encoder_string(data)
                               for chunk_id, data in chunks}
            expected_docs = docs
            replacements = sum(surrogate_count(chunk_id) + surrogate_count(data)
                               for chunk_id, data in chunks)
            blob = deflate_journal(payload)
            if len(blob) > getattr(self._object_storage, "max_object_bytes", MAX_OBJECT_BYTES):
                raise ReplayBudgetExceeded("max_object_bytes", len(blob), self._object_storage.max_object_bytes)
            # A fresh replay reads sync parameters before downloading journals.
            # Leave room for that control read and modest object growth.
            control_reserve = (
                64 * 1024 if self._sync_parameters_bytes is None
                else self._sync_parameters_bytes + 1024
            )
            if len(blob) + control_reserve > self.max_refresh_bytes:
                raise ReplayBudgetExceeded("max_refresh_bytes", len(blob) + control_reserve, self.max_refresh_bytes)
            remote_epoch = self._load_sync_parameters_epoch()
            last_sent = self._outbound_checkpoint(store)
            if (store.epoch != (remote_epoch or "") or
                    (last_sent and not self._object_storage.exists(last_sent))):
                raise ConcurrentModification(
                    "remote was rebuilt or journals are missing; refresh and retry"
                )
            ms = max(int(time.time() * 1000), self._max_seen_ms + 1)
            # Reserve even failed attempts so a retry cannot reuse their key.
            self._max_seen_ms = ms
            for _ in range(10):
                name = journal_filename(ms, suffix=secrets.token_hex(16))
                store.record_outbound(name, hashlib.sha256(blob).hexdigest())
                try:
                    outcome = self._object_storage.put_if_absent(name, blob)
                except MutationNotApplied:
                    store.discard_outbound(name)
                    raise
                except (StorageError, OSError) as exc:
                    raise OutcomeUnknown(getattr(exc, "diagnostic", None)) from exc
                if outcome == "created":
                    published = True
                    break
                if outcome == "exists":
                    store.discard_outbound(name)
                    continue
                if outcome != "unsupported":
                    raise StorageError("invalid conditional publication result")
                # B-10 compatibility fallback cannot exclude a racing writer.
                if not self._object_storage.exists(name):
                    try:
                        self._object_storage.put(name, blob)
                    except MutationNotApplied:
                        store.discard_outbound(name)
                        raise
                    except (StorageError, OSError) as exc:
                        raise OutcomeUnknown(getattr(exc, "diagnostic", None)) from exc
                    published = True
                    break
                store.discard_outbound(name)
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
                max_line_bytes=self.max_line_bytes, max_json_depth=self.max_json_depth,
            )
            if (
                decoded != payload
                or journal.chunks != expected_chunks
                or journal.docs != expected_docs
            ):
                raise StorageError("journal read-back verification failed")
            if attachment_data is not None and assemble_binary([
                journal.chunks[chunk_id]
                for chunk_id in journal.docs[0]["children"]
            ]) != attachment_data:
                raise StorageError("journal read-back verification failed")
            for doc in journal.docs:
                validate_document_identity(doc, store.path_identity)
            store.confirm_outbound(name)
            store.apply_journal(name, journal.chunks, journal.docs)
            def finish_send_tracking():
                store.last_sent_key = name
                store.compact_outbound(name)
            store.finish_bookkeeping(finish_send_tracking)
            return replacements
        except (StorageError, ValueError, OSError, sqlite3.Error) as exc:
            if published:
                raise OutcomeUnknown(getattr(exc, "diagnostic", None)) from exc
            if isinstance(exc, StorageError):
                raise
            raise StorageError("journal publication or verification failed") from exc

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
        return await self._write_content(
            path, body, size, doc_type,
            attachment_data=body if isinstance(body, bytes) else None,
            create=create, overwrite=overwrite, mtime=mtime,
            expected_rev=expected_rev,
        )

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
        expected_rev: str | None = None, attachment_data: bytes | None = None,
    ) -> NoteMeta:
        """Publish encoded chunk data through the shared write transaction."""
        store = self._require_store("write_note")
        doc_id = self._doc_id(path)
        parent = store.winner(doc_id)
        self._validate_identity(parent)
        self._check_expected_rev(parent, expected_rev)
        live = parent is not None and not parent.get("deleted")
        if live and create and not overwrite:
            raise NoteExists(f"note already exists: {path}")
        if not live and not create:
            raise NoteNotFound(f"note not found: {path}")
        pieces = [piece for piece in self._splitter.split(content, path=canonical_path(path)) if piece]
        # P-10 / EntryManagerImpls.ts:280: retain ingested Blob.size even
        # when a legacy splitter's Blob.text() removes an initial BOM.
        children = [chunk_id_for_piece(piece, hash_alg=self._splitter.hash_alg) for piece in pieces]
        unique = {}
        for chunk_id, piece in zip(children, pieces):
            if chunk_id in unique and unique[chunk_id] != piece:
                raise StorageError('content chunks with the same id hold different data')
            unique[chunk_id] = piece
        chunks = list(unique.items())
        for chunk_id, piece in chunks:
            self._validate_chunk_reuse(chunk_id, piece)
        stamp = int(time.time() * 1000) if mtime is None else mtime
        if parent is None:
            body = new_metadata_doc(
                path, children, size,
                doc_type, stamp, stamp,
            )
        else:
            body = clone_metadata(parent)
            body.pop("deleted", None)
            body.update(
                children=children, eden={}, mtime=stamp,
                size=size, type=doc_type,
            )
        body.update(_id=doc_id, path=canonical_path(path))
        body = self._next_revision(body, parent)
        replacements = self._publish(chunks, [body], attachment_data=attachment_data)
        result = self._meta_from_doc(body)
        if replacements:
            result["transport_replacements"] = replacements
            from storage_base import publication_replacements
            collector = publication_replacements.get()
            if collector is not None:
                collector.append(replacements)
        return result

    async def delete_note(
        self, path: str, expected_rev: str | None = None
    ) -> bool:
        """Append a soft deletion, preserving all body references."""
        self.check_writer_support()
        validate_vault_path(path)
        await self.check_selection(path)
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
        self._validate_identity(target)
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

        Oversized destinations are refused before publication (P-13).

        Same-path is a no-op; identity-preserving renames update only the
        display path and mtime. Single-file publication has no partial phases,
        so RenamePartialError (mapped by the tool layer for contract parity)
        is never raised by this backend. Unconfirmed publication reports
        OutcomeUnknown.
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
        source = self._source_doc(path)
        # Device admission converts oversized RENAME to DELETE(source).
        # P-13: this peer lacks the device's retained host file; refuse first.
        try:
            check_file_size(new_path, source.get('size', 0), self.sync_max_size_mb)
        except WriteSkipped:
            raise RenameRefused(path, new_path, source.get('size', 0),
                                self.sync_max_size_mb) from None

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
            self._publish([], [body] if preserve_source else [body, retired])
        return self._meta_from_doc(body)

    async def copy_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Copy references (including eden), preserving body and ctime."""
        self.check_writer_support()
        validate_vault_path(path)
        validate_vault_path(new_path)
        await self.check_selection(new_path)
        source = self._source_doc(path)
        # Copy creates a new storage file -> CREATE size gate (C:events.ts:63-69).
        # C:serviceModules/ServiceFileHandlerBase/events.ts:63-77.
        # Rename has a separate admission rule; deletes have no size gate.
        check_file_size(new_path, source.get('size', 0), self.sync_max_size_mb)
        body = self._copy_revision(source, new_path, overwrite)
        self._publish([], [body])
        return self._meta_from_doc(body)

    async def _read_selection_file(self, path):
        store = self._require_store('ignore files')
        candidates = ['i:' + path, path] if _has_hidden_segment(path) else [path]
        content = None
        for candidate in candidates:
            doc_id = self._path2id(store, candidate)
            if store.winner(doc_id) is not None:
                content = store.read_ignore_file(doc_id)
                break
        if content is None:
            return None
        _, text, data, is_text = content
        return text if is_text else data.decode('utf-8', errors='replace')
