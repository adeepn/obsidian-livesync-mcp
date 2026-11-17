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


class UnsupportedVault(StorageError):
    """The vault uses features this server cannot support (E2EE, path
    obfuscation, case-sensitive ids, ...)."""


class StorageUnavailable(StorageError):
    """The storage backend is unreachable or rejected the request."""


class NoteMeta(TypedDict):
    """Metadata of one note. ``ctime``/``mtime`` are Unix milliseconds.

    ``rev`` is the storage-level revision of the note's metadata
    document (CouchDB ``_rev``) at read time — ``None`` when absent.
    ``get_meta`` populates it so read-modify-write callers can pass it
    back as ``expected_rev``.
    """

    path: str
    doc_id: str
    ctime: int
    mtime: int
    size: int
    type: str
    rev: str | None


class NoteContent(TypedDict):
    """Decoded note body together with its metadata.

    Textual notes carry ``text``; binary notes carry ``data``. ``is_text``
    follows LiveSync ``isPlainText`` semantics (audit B3/B4), so a note
    can be textual by type (``plain``) or by extension. ``rev`` is the
    revision of the metadata document the body was decoded from (the
    same value as ``meta["rev"]``); ``None`` for new or absent notes.
    Read-modify-write callers pass it back as ``write_note``'s /
    ``delete_note``'s ``expected_rev`` to prevent lost updates across
    the tool's read and write.
    """

    meta: NoteMeta
    text: str | None
    data: bytes | None
    is_text: bool
    rev: str | None


@runtime_checkable
class StorageBackend(Protocol):
    """Async storage interface consumed by every MCP tool (design §4.2)."""

    async def startup_check(self) -> None:
        """Verify availability and vault compatibility.

        Raises ``StorageUnavailable`` when the backend is unreachable and
        ``UnsupportedVault`` for E2EE/obfuscation/case-sensitive vaults
        (audits B5/B6/B14).
        """
        ...

    async def refresh(self) -> None:
        """Bring the local representation (index/cache) up to date."""
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
        """Metadata of one note; raises ``NoteNotFound`` for absent notes."""
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
        when it is not ``None`` and the current winning revision differs
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
