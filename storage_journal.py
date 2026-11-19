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

This task covers ``startup_check``/``refresh``/``backend_info``/``close``;
note reads and writes arrive in Tasks 6/7 and raise ``NotImplementedError``.
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
    UnsupportedVaultSettings,
    resolve_path_identity,
)
from object_storage import JournalObjectStorage
from storage_base import (
    NoteContent,
    NoteMeta,
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
        else:
            self._probe_remote_wipe(epoch)

    async def refresh(self, force: bool = False) -> None:
        """Replay journal files that arrived after the processed ledger.

        Refreshes closer than ``refresh_min_interval`` together are skipped
        unless ``force`` is set; a skipped refresh is a no-op. Candidates
        are ordered by their numeric ms prefix and applied one by one; the
        first failing file stops the replay without advancing the
        checkpoint (its predecessors stay processed), so the next refresh
        retries from the same file.
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

    # -- Replay helpers ----------------------------------------------------------

    def _list_unprocessed_journals(self, store: JournalStore) -> list[str]:
        """Journal names to replay: listed after the greatest processed
        name, control keys and already-processed files dropped, ordered by
        numeric ms prefix.

        The listing cursor uses the *numerically* greatest processed name
        (upstream ``_getRemoteJournals``, JournalSyncCore.ts:758-764), and
        the already-processed filter guards candidates that sort between
        the lexicographic cursor and the numeric maximum.
        """
        processed_names = store.processed_names()
        start_after = (
            max(processed_names, key=journal_sort_key)
            if processed_names
            else ""
        )
        keys = self._object_storage.list(start_after)
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
            raise StorageError(
                f"journal file {name!r} could not be replayed: {exc}"
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
        raise NotImplementedError("journal note reads arrive in Task 6")

    async def get_meta(self, path: str) -> NoteMeta:
        raise NotImplementedError("journal note reads arrive in Task 6")

    async def read_note(self, path: str) -> NoteContent | None:
        raise NotImplementedError("journal note reads arrive in Task 6")

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

    def iterate_texts(
        self, extension: str = ".md"
    ) -> AsyncIterator[tuple[NoteMeta, str | None]]:
        raise NotImplementedError("journal note reads arrive in Task 6")
