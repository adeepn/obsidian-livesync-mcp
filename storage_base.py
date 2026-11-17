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
    """Metadata of one note. ``ctime``/``mtime`` are Unix milliseconds."""

    path: str
    doc_id: str
    ctime: int
    mtime: int
    size: int
    type: str


class NoteContent(TypedDict):
    """Decoded note body together with its metadata.

    Textual notes carry ``text``; binary notes carry ``data``. ``is_text``
    follows LiveSync ``isPlainText`` semantics (audit B3/B4), so a note
    can be textual by type (``plain``) or by extension.
    """

    meta: NoteMeta
    text: str | None
    data: bytes | None
    is_text: bool


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
    ) -> NoteMeta:
        """Write a textual body under the central D1/D2 rules."""
        ...

    async def delete_note(self, path: str) -> bool:
        """Soft-delete the note (never chunk deletion, audit B1/B2).

        Returns ``True`` when a live note was deleted, ``False`` when no
        live note exists at the path.
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
