"""Storage backend interface for the Obsidian LiveSync MCP server.

MCP tools interact with vault storage exclusively through the
``StorageBackend`` protocol (design doc §4.2); concrete implementations
(CouchDB today, the S3 journal peer later) translate it into their wire
format. Public diagnostics use typed fields; default logs omit remote
payloads and tracebacks (audit C7, P-19).
"""

from __future__ import annotations

from diagnostic_hygiene import (PublicDiagnostic, DiagnosticPath, diagnostic, path_field, setting_key, numeric_field, numeric_text, cause_name)

from typing import AsyncIterator, Protocol, TypedDict, runtime_checkable, NotRequired



class StorageError(Exception):
    """Storage failure with an explicit public projection, never free text."""

    def __init__(self, reason=None, *, cause=None, public=None):
        self.diagnostic = public if isinstance(public, PublicDiagnostic) else diagnostic(reason, cause=cause)
        super().__init__(reason if type(reason) is str else self.format())

    @property
    def status(self):
        return self.diagnostic.status

    @property
    def operation(self):
        return self.diagnostic.operation

    @property
    def cause_class(self):
        public = self.diagnostic
        from diagnostic_hygiene import identifier
        return (identifier(public.cause_class, '<unexpected cause>') if public.cause_class is not None
                else cause_name(public.cause) if public.cause is not None else None)

    def format(self):
        value = getattr(self, 'diagnostic', None)
        return value.format() if isinstance(value, PublicDiagnostic) else cause_name(type(self))


class TextBudgetExceeded(StorageError):
    """A whole-note text operation exceeded the configured memory budget."""
    def __init__(self, size_bytes: int, budget_bytes: int):
        self.size_bytes = size_bytes
        self.budget_bytes = budget_bytes
        super().__init__()

    def as_dict(self):
        size = getattr(self, 'size_bytes', None)
        budget = getattr(self, 'budget_bytes', None)
        return dict(content_available=False,
                    size_bytes=size if type(size) is int and size >= 0 else None,
                    budget_bytes=budget if type(budget) is int and budget >= 0 else None,
                    reason='text_budget_exceeded',
                    remedy='Increase LIVESYNC_TEXT_SCAN_MAX_BYTES or set it to 0; '
                           'use obsidian_download_attachment for complete bytes.')

    def format(self):
        import json
        return json.dumps(self.as_dict())


class SelectionRefused(StorageError):
    """A device selection rule excludes a vault path."""

    def __init__(self, path: str, rule: str):
        self.path, self.rule = DiagnosticPath(path, remote=False), setting_key(rule)
        super().__init__()

    def format(self):
        return (f"path '{path_field(self.path)}' is excluded by the vault's {setting_key(getattr(self, 'rule', None))}; "
                "devices would not synchronize it.")



class WriteSkipped(StorageError):
    """Device policy outcome, not a failed/unsupported write. No revision exists.

    Backends signal this before publication; tools return status=skipped.
    C:services/base/VaultService.ts:73-81; C:serviceModules/
    ServiceFileHandlerBase/events.ts:58-69 (pins in docs/upstream-pin.md).
    """

    def __init__(self, path: str, size: int, maximum: float):
        self.path, self.size, self.maximum = DiagnosticPath(path, remote=False), numeric_field(size), numeric_field(maximum, kind='measured')
        super().__init__()

    def format(self):
        size, maximum = numeric_field(self.size), numeric_text(self.maximum, kind='measured')
        mib = f"{size / 1024**2:.3g}" if type(size) is int else size
        return (
            f"skipped: {mib} MiB ({size} bytes) exceeds the vault's "
            f"syncMaxSizeInMB ({maximum}); new content was NOT saved; "
            "raise syncMaxSizeInMB in the plugin settings "
            "or pass a smaller file"
        )

    def as_dict(self, *, caller_path=None):
        path = caller_path or self.path
        return dict(status="skipped", path=path.value if isinstance(path, DiagnosticPath) else '<path unavailable>', size=numeric_field(self.size),
                    syncMaxSizeInMB=numeric_field(self.maximum, kind='measured'), message=self.format())


class RenameRefused(StorageError):
    """P-13: refuse device source retirement without a retained host copy."""

    def __init__(self, source: str, destination: str, size: int, maximum: float):
        self.source, self.destination = DiagnosticPath(source, remote=False), DiagnosticPath(destination, remote=False)
        self.size, self.maximum = numeric_field(size), numeric_field(maximum, kind='measured')
        super().__init__()

    def format(self):
        size, maximum = numeric_field(self.size), numeric_text(self.maximum, kind='measured')
        return (
            f"move was not performed: {size} bytes exceeds the vault's "
            f"syncMaxSizeInMB ({maximum}). A device would drop the source "
            "from sync because it still has the file on disk; this peer "
            "would lose the only copy."
        )


def check_file_size(path: str, size: int, maximum: float) -> None:
    # C:services/base/VaultService.ts:73-81: strict >, <=0 disables.
    if maximum > 0 and size > 0 and size > maximum * 1024 * 1024:
        raise WriteSkipped(path, size, maximum)


class NoteNotFound(StorageError):
    """No live note exists at the requested vault path."""


class NoteExists(StorageError):
    """A note already exists at the target path of a create/move."""


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
        self.source, self.destination = source, destination
        self.source_path, self.destination_path = DiagnosticPath(source, remote=False), DiagnosticPath(destination, remote=False)
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
    obfuscation, ...)."""


class WriterUnsupported(StorageError):
    """A writer-only compatibility refusal safe to display to tool clients."""


class StorageUnavailable(StorageError):
    """The storage backend is unreachable or rejected the request."""


class MutationNotApplied(StorageUnavailable):
    """The adapter can prove the mutation was refused or never sent."""


# Operation names are shared by runtime messages and MCP tool descriptions.
RETRY_IDEMPOTENT = {
    "note create": True, "attachment upload": True,
    "overwrite": True, "delete": True, "move": True, "copy": True,
    "template create": True, "properties set": True, "properties delete": True,
    "tags add": True, "tags remove": True,
    "canvas node remove": True, "canvas edge remove": True,
    "append": False, "prepend": False, "search-replace": False,
    "after-heading insertion": False, "canvas node add": False, "canvas edge add": False,
}


def retry_guidance(operation: str) -> str:
    """Describe a retry of the same tool input, after inspecting remote truth."""
    if RETRY_IDEMPOTENT[operation]:
        return (
            f"{operation}: IDEMPOTENT FOR THE SAME INPUT; "
            "re-read to see whether it applied; retrying the same request is safe."
        )
    return (
        f"{operation}: NON-IDEMPOTENT; a blind retry can duplicate the change; "
        "re-read the note and apply the remaining change manually rather than retrying blindly."
    )


class OutcomeUnknown(StorageUnavailable):
    """Metadata publication may have happened; retain the uncertain step."""

    def __init__(self, reason=None, *, cause=None, source_removal=False, paths=()):
        self.source_removal = source_removal
        self.paths = tuple(DiagnosticPath(p, remote=False) for p in paths)
        super().__init__(reason, cause=cause)
        self.args = (self.message(),)

    def message(self, operation: str | None = None, *, context: str = "") -> str:
        prefix = "outcome unknown: "
        if not context:
            context = " -> ".join(path_field(p) for p in getattr(self, "paths", ()))
        if context:
            prefix += context + ": "
        if getattr(self, 'source_removal', False):
            prefix += "destination published; source removal unknown — re-read both paths. "
        public = getattr(self, 'diagnostic', None)
        if isinstance(public, PublicDiagnostic) and public.operation is not None:
            prefix += public.format() + '. '
        elif isinstance(public, PublicDiagnostic) and public.cause is not None:
            prefix += 'cause: ' + cause_name(public.cause) + '. '
        prefix += "the change MAY have been applied. "
        if operation is not None:
            return prefix + retry_guidance(operation)
        return prefix + "Inspect and re-read the affected paths before deciding how to retry."


class NoteMeta(TypedDict):
    """Metadata of one note. ``ctime``/``mtime`` are Unix milliseconds.

    ``rev`` is a mandatory opaque backend token for an existing note.
    CouchDB passes through ``_rev``; the Plan B journal backend uses its
    local winner lineage token. An absent note has no metadata result.
    ``get_meta`` populates it so read-modify-write callers can pass it
    back as ``expected_rev``. Successful journal writes additionally carry
    ``transport_replacements`` only when emitted compact leaves replaced
    surrogate units; ordinary metadata reads do not carry a publication count.
    """

    path: str
    doc_id: str
    ctime: int
    mtime: int
    size: int
    type: str
    rev: str
    transport_replacements: NotRequired[int]
    notices: NotRequired[list[dict]]


class NoteContent(TypedDict):
    """Decoded note body together with its metadata.

    Plain (and legacy notes) bodies carry ``text``; newnote bodies carry
    bytes in ``data`` and ``is_text=False`` regardless of path. The tool
    layer projects strictly valid UTF-8 bytes as text separately. ``rev`` is the
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
    publication, not eventual winner status. Mutations raise OutcomeUnknown
    when remote application cannot be confirmed; re-read before retrying.
    Incoming refresh/replay may legitimately apply remote changes before failing.
    """

    async def startup_check(self) -> None:
        """Verify availability and vault compatibility.

        Raises ``StorageUnavailable`` when the backend is unreachable and
        ``UnsupportedVault`` for E2EE/obfuscation vaults
        (audits B5/B6/B14).
        """
        ...

    def check_writer_support(self) -> None:
        """Raise WriterUnsupported before tool refresh or any mutation.

        This check inspects resolved startup settings without storage I/O.
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
        content: str | bytes,
        *,
        create: bool = False,
        overwrite: bool = False,
        mtime: int | None = None,
        expected_rev: str | None = None,
        lossy_text: bool = False,
    ) -> NoteMeta:
        """Ingest file content using the device disk-reading path rule.

        Oversized ordinary input signals WriteSkipped without publication.
        Byte input is selected by its original size, before opt-in lossy_text
        conversion for plain paths; strings use Blob UTF-8 byte length.

        ``expected_rev`` is the read-through lost-update precondition:
        when it is not ``None`` and the refreshed local winning token differs
        (or the document appeared/vanished since the read), the write is
        refused with ``ConcurrentModification`` before anything is
        written. ``None`` keeps last-writer-wins semantics and must be
        reserved for callers that genuinely intend an unconditional
        overwrite or a plain create.
        """
        ...

    async def write_attachment(
        self,
        path: str,
        data: bytes,
        *,
        create: bool = False,
        overwrite: bool = False,
        expected_rev: str | None = None,
        lossy_text: bool = False,
    ) -> NoteMeta:
        """Write binary data with write_note create/overwrite/resurrection rules.

        Thin byte-input alias: path selects plain/newnote as in write_note.
        Validate ForbiddenPath first. Stale expected_rev refuses all writes.
        Oversized input signals WriteSkipped before mutation, as write_note does.
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
        """Move references; refuse oversized destinations before any mutation."""
        ...

    async def copy_note(
        self, path: str, new_path: str, *, overwrite: bool = False
    ) -> NoteMeta:
        """Duplicate a note by re-publishing its chunk references."""
        ...

    def iterate_texts(
        self, extension: str = "", *, after: str = ""
    ) -> AsyncIterator[tuple[NoteMeta, str | None]]:
        """Stream ``(meta, text)`` for every note with the extension.

        Display paths are sorted strictly after ``after`` before body reads.
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
        """Diagnostics for ``vault_stats`` and startup logs.

        Concrete backends report ``writer_supported`` and an optional
        ``writer_unsupported_reason`` independently of read compatibility.
        """
        ...

    async def close(self) -> None:
        """Release underlying resources (HTTP clients, connections)."""
        ...
