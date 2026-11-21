"""Storage backend interface for the Obsidian LiveSync MCP server.

MCP tools interact with vault storage exclusively through the
``StorageBackend`` protocol (design doc §4.2); concrete implementations
(CouchDB today, the S3 journal peer later) translate it into their wire
format. Exception messages must never leak internal URLs or revisions
(audit C7) — implementations log the details instead.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, TypedDict, runtime_checkable


class StorageError(Exception):
    """Base class for every storage-level failure."""


class NoteNotFound(StorageError):
    """No live note exists at the requested vault path."""


class NoteExists(StorageError):
    """A note already exists at the target path of a create/move."""


class BinaryNoteError(StorageError):
    """The operation only applies to textual notes."""


class ConcurrentModification(StorageError):
    """The note changed concurrently; the caller must re-read and retry."""


class UnresolvedConflicts(ConcurrentModification):
    """Sibling revisions require resolution before a mutation can proceed."""


class RenamePartialError(StorageError):
    """Destination published, but source retirement failed or is unconfirmed.

    source_state is "live" only after observing a surviving source; callers
    must verify both paths and contents before cleanup, even in that case.
    """

    def __init__(self, source: str, destination: str, *, source_state: str = "live"):
        self.source = source
        self.destination = destination
        self.completed_phase = "target_written"
        self.source_state = source_state
        self.hint = (
            "Verify both paths and their contents before cleanup. "
            "Delete one copy, or retry the move after removing the destination."
            if source_state == "live" else
            "Verify both paths and their contents; source retirement is unconfirmed."
        )
        super().__init__("rename target published; source retirement failed")


class UnsupportedVault(StorageError):
    """The vault uses features this server cannot support (E2EE, path
    obfuscation, case-sensitive ids, ...)."""


class StorageUnavailable(StorageError):
    """The storage backend is unreachable or rejected the request."""


class NoteMeta(TypedDict):
    """Metadata of one note. ``ctime``/``mtime`` are Unix milliseconds.

    ``rev`` is a mandatory opaque backend token for an existing note.
    CouchDB passes through ``_rev``; the Plan B journal backend uses its
    local winner lineage token. An absent note has no metadata result.
    ``get_meta`` populates it so read-modify-write callers can pass it
    back as ``expected_rev``.
    """

    path: str
    doc_id: str
    ctime: int
    mtime: int
    size: int
    type: str
    rev: str


class NoteContent(TypedDict):
    """Decoded note body together with its metadata.

    Textual notes carry ``text``; binary notes carry ``data``. ``is_text``
    follows LiveSync ``isPlainText`` semantics (audit B3/B4), so a note
    can be textual by type (``plain``) or by extension. ``rev`` is the
    mandatory opaque token of the metadata the body was decoded from
    (the same value as ``meta["rev"]``). Existing notes never carry None.
    Read-modify-write callers pass it back as ``write_note``'s /
    ``delete_note``'s ``expected_rev`` to prevent lost updates across
    the tool's read and write.
    """

    meta: NoteMeta
    text: str | None
    data: bytes | None
    is_text: bool
    rev: str


@runtime_checkable
class StorageBackend(Protocol):
    """Async storage interface consumed by every MCP tool (design §4.2).

    Revision preconditions compare against the refreshed local winner under
    the backend's mutation lock (or equivalent conditional publication).
    Unseen remote concurrent writes may still land: journal appends commute
    and conflicts resolve by a deterministic winner. CouchDB provides stronger
    conditional-write semantics through _rev. Success means durable
    publication, not eventual winner status.
    """

    async def startup_check(self) -> None:
        """Verify availability and vault compatibility.

        Raises ``StorageUnavailable`` when the backend is unreachable and
        ``UnsupportedVault`` for E2EE/obfuscation/case-sensitive vaults
        (audits B5/B6/B14).
        """
        ...

    async def refresh(self, force: bool = False) -> None:
        """Refresh local state; force bypasses throttling or reloads the index."""
        ...

    async def list_notes(
        self,
        folder: str = "",
        extension: str | None = None,
        recursive: bool = True,
    ) -> list[NoteMeta]:
        """List live notes, optionally filtered by folder and extension."""
        ...

    async def get_meta(self, path: str) -> NoteMeta:
        """Metadata with a mandatory opaque rev; NoteNotFound when absent."""
        ...

    async def read_note(self, path: str) -> NoteContent | None:
        """Decoded note body, or ``None`` when the note is absent.

        Soft-deleted notes are absent for all read paths (audit B12).
        """
        ...

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
        """Write a textual body under the central D1/D2 rules.

        ``expected_rev`` is the read-through lost-update precondition:
        when it is not ``None`` and the refreshed local winning token differs
        (or the document appeared/vanished since the read), the write is
        refused with ``ConcurrentModification`` before anything is
        written. ``None`` keeps last-writer-wins semantics and must be
        reserved for callers that genuinely intend an unconditional
        overwrite or a plain create.
        """
        ...

    async def delete_note(
        self, path: str, expected_rev: str | None = None
    ) -> bool:
        """Soft-delete the note (never chunk deletion, audit B1/B2).

        ``expected_rev`` guards the same read-through race as in
        ``write_note``. Returns ``True`` when a live note was deleted,
        ``False`` when no live note exists at the path.
        """
        ...

    async def rename_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Move a note by copying ``children`` (no content re-encoding)."""
        ...

    async def copy_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Duplicate a note by re-publishing its chunk references."""
        ...

    def iterate_texts(
        self, extension: str = ".md"
    ) -> AsyncIterator[tuple[NoteMeta, str | None]]:
        """Stream ``(meta, text)`` for every note with the extension.

        Scans are best-effort in the backend's iteration order; snapshot
        consistency is not guaranteed across a scan. CouchDB may observe
        interleaved changes. The Plan B journal backend must document its
        own ordering/consistency and protect referenced chunks from
        reclamation for the duration of active scans.

        Corrupt or unreadable notes yield ``(meta, None)`` after logging,
        so one broken note never fails a vault-wide tool (audit C4).
        """
        ...

    async def backend_info(self) -> dict:
        """Diagnostics for ``vault_stats`` and startup logs."""
        ...

    async def close(self) -> None:
        """Release underlying resources (HTTP clients, connections)."""
        ...
