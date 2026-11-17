"""Shared LiveSync format semantics: paths, namespaces, document and chunk codecs.

Single source of truth for the ``path -> document id`` mapping, the
vault-path namespace rules, and the note/chunk document codecs used by
every storage backend (audit B2/B3/B4/B7/B9/B10).

Mirrors upstream livesync-commonlib ``src/string_and_binary/path.ts`` and
``src/managers/EntryManager/EntryManagerImpls.ts`` for the configuration
this server supports: ``handleFilenameCaseSensitive=false`` (ids are
lowercased) and path obfuscation disabled. Vaults that use obfuscation or
E2EE are rejected at backend startup instead.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

# Upstream isPlainText (path.ts): extensions stored as plain text chunks.
TEXT_EXTENSIONS = frozenset(
    {".md", ".txt", ".svg", ".html", ".csv", ".css", ".js", ".xml", ".canvas"}
)

_TEXT_SUFFIXES = tuple(TEXT_EXTENSIONS)

# Write-side binary denylist. The read classification (TEXT_EXTENSIONS)
# describes how *upstream* stored a document; it must not gate writes,
# or textual-but-unclassified formats (.base, .json, .yaml) become
# unwritable. Writes are therefore allowed for every path except an
# explicit list of binary extensions. SVG is deliberately absent: it
# is XML text.
WRITABLE_BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico",
        ".pdf", ".zip", ".gz",
        ".mp3", ".mp4", ".mov", ".avi", ".wav", ".flac", ".ogg",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".epub", ".mobi",
    }
)

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
    return _normalize_path(path).endswith(_TEXT_SUFFIXES)


def is_writable_text_path(path: str) -> bool:
    """Write-side policy: whether a path may receive a textual body.

    Inverse of the read classification: a path is writable unless its
    extension is in the explicit binary denylist
    (``WRITABLE_BINARY_EXTENSIONS``). Textual formats upstream never
    classifies — ``.base`` (YAML), ``.json``, ``.yaml``, ``.svg`` —
    stay writable, because every write this server performs is plain
    text regardless of extension (audit B3). Case-insensitive: a
    binary ``Photo.PNG`` must not slip through spelling.
    """
    lowered = _normalize_path(path).lower()
    return not any(
        lowered.endswith(extension) for extension in WRITABLE_BINARY_EXTENSIONS
    )


def assemble_text(chunk_datas: list[str]) -> str:
    """Concatenate plain-text chunk data into the document body.

    ``plain`` documents store each chunk verbatim (no encoding), so the
    body is the plain join of the chunk data strings in order.
    """
    return "".join(chunk_datas)


def assemble_binary(chunk_datas: list[str]) -> bytes:
    """Decode base64 newnote chunks and concatenate the raw bytes.

    Audit B4: every chunk is base64-encoded *separately*; the document
    body is the concatenation of the per-chunk decoded bytes, never one
    base64 decode over the joined string. Raises ``ValueError`` when any
    chunk is not valid base64.
    """
    parts: list[bytes] = []
    for index, chunk in enumerate(chunk_datas):
        try:
            parts.append(base64.b64decode(chunk, validate=True))
        except binascii.Error as exc:
            raise ValueError(f"chunk {index} is not valid base64: {exc}") from exc
    return b"".join(parts)


def encode_newnote(data: bytes) -> list[str]:
    """Encode a body as newnote chunk data (v1: a single chunk).

    The inverse of ``assemble_binary``: the whole body becomes one
    base64-encoded chunk. Multi-chunk splitting can be added later without
    changing readers, because decoding is per chunk either way.
    """
    return [base64.b64encode(data).decode("ascii")]


def chunk_id_for(data: str) -> str:
    """Deterministic chunk document id for chunk data.

    ``h:`` plus the first 16 hex characters of sha256 over the chunk
    data's UTF-8 bytes. The ``h:`` namespace is reserved for chunk
    documents (see ``_FORBIDDEN_PREFIXES`` / validate_vault_path).
    """
    return "h:" + hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def new_metadata_doc(
    path: str,
    children: list[str],
    size: int,
    doc_type: str,
    ctime_ms: int,
    mtime_ms: int,
) -> dict:
    """Build a LiveSync file metadata document.

    Upstream ``EntryManagerImpls`` writes exactly the fields ``_id, path,
    children, ctime, mtime, size, type, eden``. Times are millisecond
    ints. ``size`` is the decoded body byte length (spec §7), not the
    encoded chunk length. ``_id`` comes from path2id; ``eden`` starts
    empty (it only carries E2EE-related chunks, which this server does
    not support).
    """
    return {
        "_id": path2id(path),
        "path": path,
        "children": children,
        "ctime": ctime_ms,
        "mtime": mtime_ms,
        "size": size,
        "type": doc_type,
        "eden": {},
    }


def soft_delete_revision(doc: dict, mtime_ms: int) -> dict:
    """Return a soft-deleted copy of a metadata document (audit B2).

    The copy only flips ``deleted`` to True and stamps the new mtime.
    Body-related fields (``children``, ``type``, ``size``) are preserved
    untouched and the original document is not modified. LiveSync marks
    deletions this way instead of writing a bare CouchDB tombstone, so a
    delete can be undone and conflict diffs stay meaningful.
    """
    revision = dict(doc)
    revision["deleted"] = True
    revision["mtime"] = mtime_ms
    return revision


def doc_type_for(path: str) -> str:
    """LiveSync document type for a body written by this server.

    Audit B3: any text is written as type ``plain`` regardless of
    extension; the extension only matters when *reading* documents written
    elsewhere, which is ``is_text_doc``'s job. This server only writes
    textual bodies today, so every path maps to ``plain``; the parameter
    is kept for future binary (``newnote``) support.
    """
    return "plain"
