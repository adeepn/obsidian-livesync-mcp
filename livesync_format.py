"""Shared LiveSync format semantics: paths and namespaces.

Single source of truth for the ``path -> document id`` mapping and the
vault-path namespace rules used by every storage backend (audit B7/B9/B10).

Mirrors upstream livesync-commonlib ``src/string_and_binary/path.ts`` for the
configuration this server supports: ``handleFilenameCaseSensitive=false``
(ids are lowercased) and path obfuscation disabled. Vaults that use
obfuscation or E2EE are rejected at backend startup instead.
"""

from __future__ import annotations

# Upstream isPlainText (path.ts): extensions stored as plain text chunks.
TEXT_EXTENSIONS = frozenset(
    {".md", ".txt", ".svg", ".html", ".csv", ".css", ".js", ".xml", ".canvas"}
)

_TEXT_SUFFIXES = tuple(TEXT_EXTENSIONS)

# LiveSync metadata namespaces (upstream fileaccess.const.ts): hidden files,
# chunk documents, machine info, obfuscated ids. Case is ignored on purpose
# (audit B7), since ids are lowercased anyway.
_FORBIDDEN_PREFIXES = ("i:", "ix:", "ps:", "h:", "f:")

# CouchDB reserves document ids ``_design/...`` and ``_local/...`` (audit B10).
# Other leading-underscore paths are valid vault paths; path2id escapes them
# to ``/_...`` exactly like upstream.
_RESERVED_FIRST_SEGMENTS = frozenset({"_design", "_local"})

# LiveSync service documents that live at the vault root (db.const.ts).
_RESERVED_PATHS = frozenset({"obsydian_livesync_version", "syncinfo"})


class ForbiddenPathError(Exception):
    """Vault path collides with a LiveSync or CouchDB service namespace."""


def _normalize_path(path: str) -> str:
    """Normalize separators before id mapping or validation.

    Backslashes become forward slashes, empty segments (duplicated or
    leading/trailing slashes) and exact ``.`` segments are dropped, so
    ``Inbox/x.md``, ``/Inbox//x.md``, ``./Inbox/x.md`` and ``Inbox\\x.md``
    are one path (audit B9). ``..`` segments are *not* resolved; they are
    rejected by validate_vault_path.
    """
    segments = [
        segment
        for segment in path.replace("\\", "/").split("/")
        if segment not in ("", ".")
    ]
    return "/".join(segments)


def path2id(path: str) -> str:
    """Map a vault path to a LiveSync document id.

    Upstream ``path2id_base`` with ``caseInsensitive=true`` and no
    obfuscation: lowercase, and a leading ``_`` is escaped to ``/_`` because
    CouchDB reserves ids starting with ``_``.
    """
    normalized = _normalize_path(path).lower()
    if normalized.startswith("_"):
        return "/" + normalized
    return normalized


def validate_vault_path(path: str) -> None:
    """Reject paths that would reach LiveSync or CouchDB service namespaces.

    Checked case-insensitively, after stripping any leading ``/`` (audit
    B7/B10): metadata prefixes (``i:``, ``ix:``, ``ps:``, ``h:``, ``f:``),
    CouchDB-reserved first segments (``_design``, ``_local``), dot-prefixed
    segments (``.obsidian`` and other hidden files), the service documents
    ``obsydian_livesync_version`` and ``syncinfo``, and empty paths.
    """
    normalized = _normalize_path(path)
    lowered = normalized.lower()

    if not lowered:
        raise ForbiddenPathError("vault path is empty")

    for prefix in _FORBIDDEN_PREFIXES:
        if lowered.startswith(prefix):
            raise ForbiddenPathError(
                f"vault path uses the reserved prefix {prefix!r}"
            )

    first_segment = normalized.split("/", 1)[0]
    if first_segment.lower() in _RESERVED_FIRST_SEGMENTS:
        raise ForbiddenPathError(
            f"vault path uses the CouchDB-reserved name {first_segment!r}"
        )

    for segment in normalized.split("/"):
        if segment.startswith("."):
            raise ForbiddenPathError(
                f"vault path segment {segment!r} is hidden (starts with '.')"
            )

    if lowered in _RESERVED_PATHS:
        raise ForbiddenPathError(
            f"vault path is the reserved service document {lowered!r}"
        )


def is_text_doc(doc_type: str | None, path: str) -> bool:
    """Whether a document is textual and safe for text tools.

    Upstream ``isPlainText`` semantics: doc type ``plain`` is always text
    (it was stored as text chunks regardless of extension); otherwise the
    extension decides. The check is case-sensitive like upstream.
    """
    if doc_type == "plain":
        return True
    return path.endswith(_TEXT_SUFFIXES)
