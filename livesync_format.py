"""Shared LiveSync format semantics: paths, namespaces, document and chunk codecs.

Single source of truth for the ``path -> document id`` mapping, the
vault-path namespace rules, and the note/chunk document codecs used by
every storage backend (audit B2/B3/B4/B7/B9/B10).

Mirrors upstream livesync-commonlib ``src/string_and_binary/path.ts``,
``src/common/pathNormalization.ts``, and
``src/managers/EntryManager/EntryManagerImpls.ts`` for the configuration
this server supports: either filename case setting and path obfuscation
disabled. Vaults that use obfuscation or
E2EE are rejected at backend startup instead. Path identity comes in two
flavors (upstream ``pathNormalizationVersion``): legacy v0 keeps paths as
typed, v1 compares Unicode NFC forms — see ``path2id`` and
``resolve_path_identity``.
"""

from __future__ import annotations

import base64
import binascii
import unicodedata
from copy import deepcopy

from content_splitter import ContentSplitter, UnsupportedWriterMode, chunk_id_for_piece

# Upstream isPlainText (path.ts): extensions stored as plain text chunks.
TEXT_EXTENSIONS = frozenset(
    {".md", ".txt", ".svg", ".html", ".csv", ".css", ".js", ".xml", ".canvas"}
)

_TEXT_SUFFIXES = tuple(TEXT_EXTENSIONS)

# LiveSync metadata namespaces (upstream fileaccess.const.ts): hidden files,
# chunk documents, machine info, obfuscated ids. Case is ignored on purpose
# (audit B7), since ids are lowercased anyway.
SERVICE_NAMESPACE_PREFIXES = ("i:", "ix:", "ps:", "h:", "f:")

# CouchDB reserves document ids ``_design/...`` and ``_local/...`` (audit B10).
# Other leading-underscore paths are valid vault paths; path2id escapes them
# to ``/_...`` exactly like upstream.
_RESERVED_FIRST_SEGMENTS = frozenset({"_design", "_local"})

# LiveSync service documents that live at the vault root (db.const.ts).
SERVICE_DOC_IDS = frozenset({"obsydian_livesync_version", "syncinfo"})

# Path identity modes (upstream common/pathNormalization.ts): version 0
# keeps the path as typed when deriving document ids; version 1 compares
# Unicode NFC forms. Backends fix one mode per vault at startup via
# ``resolve_path_identity`` and apply both axes via ``identity_path2id``.
PATH_IDENTITY_LEGACY = "legacy"
PATH_IDENTITY_NFC = "nfc"
PATH_IDENTITY_CASE_SENSITIVE = "case-sensitive"
PATH_IDENTITY_NFC_CASE_SENSITIVE = "nfc-case-sensitive"


def identity_path2id(path: str, identity: str) -> str:
    """Apply the independent NFC and case axes of a resolved identity."""
    return path2id(
        path,
        nfc=identity in (PATH_IDENTITY_NFC, PATH_IDENTITY_NFC_CASE_SENSITIVE),
        case_sensitive=identity in (
            PATH_IDENTITY_CASE_SENSITIVE, PATH_IDENTITY_NFC_CASE_SENSITIVE,
        ),
    )


def validate_document_identity(doc: dict, identity: str) -> None:
    """Refuse metadata from another identity before it can alias a note.

    common/pathIdentity.ts:25-51 requires matching stored IDs; rebuild or
    fetch under the selected identity instead of merging distinct entries.
    """
    path = doc.get("path")
    if isinstance(path, str):
        try:
            validate_vault_path(path)
        except ForbiddenPathError:
            return  # hidden/service paths are handled by the existing read gate
    metadata = (doc.get("type") in ("plain", "newnote", "notes")
                and not is_service_document_id(str(doc.get("_id", ""))))
    if metadata and isinstance(path, str) and path:
        if doc.get("_id") != identity_path2id(path, identity):
            raise ValueError(
                "metadata path identity mismatch or collision; rebuild/fetch required"
            )


class ForbiddenPathError(Exception):
    """Vault path collides with a LiveSync or CouchDB service namespace."""


class UnsupportedVaultSettings(ValueError):
    """Milestone settings describe a vault this server cannot serve.

    Raised by ``resolve_path_identity`` with the reason in the message.
    This module must stay dependency-free (storage layers import it, not
    the other way round), so the exception is a plain ``ValueError``.

    Mapping rule for backends: catch and re-raise as the storage-layer
    ``UnsupportedVault`` preserving the text, e.g.
    ``except UnsupportedVaultSettings as exc:
    raise UnsupportedVault(str(exc)) from exc``.
    """


def _normalize_path(path: str) -> str:
    """Normalize separators before id mapping or validation.

    Backslashes become forward slashes, empty segments (duplicated or
    leading/trailing slashes) and exact ``.`` segments are dropped, so
    ``Inbox/x.md``, ``/Inbox//x.md``, ``./Inbox/x.md`` and ``Inbox\\x.md``
    are one path (audit B9). Internal ``..`` segments resolve to the parent;
    paths escaping the vault root remain invalid.
    """
    segments = []
    for segment in path.replace("\\", "/").split("/"):
        if segment in ("", "."):
            continue
        if segment == ".." and segments and segments[-1] != "..":
            segments.pop()
        else:
            segments.append(segment)
    return "/".join(segments)


def canonical_path(path: str) -> str:
    """Return the validated, case-preserving display path persisted by writes."""
    validate_vault_path(path)
    return _normalize_path(path)


def path2id(path: str, *, nfc: bool = False, case_sensitive: bool = False) -> str:
    """Map a vault path to a LiveSync document id.

    Upstream ``path2id_base`` with no obfuscation: lowercase unless
    ``case_sensitive=True``, and a leading ``_`` is escaped to ``/_`` because
    CouchDB reserves ids starting with ``_``.

    ``nfc=True`` selects the v1 path identity (``pathNormalizationVersion``
    1): the raw path is normalized to Unicode NFC *before* the existing
    separator normalization and lowercase — the same composition as
    upstream ``normalisePathForIdentity`` feeding ``path2id_base`` — so
    decomposed and precomposed spellings share one id. The default keeps
    the legacy (v0) behavior: decomposed spellings stay distinct.
    """
    if path.startswith("f:"):
        return path
    if nfc:
        path = unicodedata.normalize("NFC", path)
    normalized = _normalize_path(path)
    if not case_sensitive:
        normalized = normalized.lower()
    if normalized.startswith("_"):
        return "/" + normalized
    return normalized


# Milestone settings key holding per-device settings maps (upstream
# common/models/tweak.definition.ts:153 and journal/
# LiveSyncJournalReplicator.ts:135: tweak_values is keyed by device id).
_PREFERRED_DEVICE_ID = "PREFERRED"

# Tweak keys this server knows how to read. A settings map counts as
# readable only when it holds at least one of them; anything else is
# metadata or an empty map, and resolution fails closed instead of
# silently selecting legacy.
_RECOGNIZED_TWEAK_KEYS = frozenset(
    {
        "pathNormalizationVersion",
        "encrypt",
        "usePathObfuscation",
        "handleFilenameCaseSensitive",
        "chunkSplitterVersion",
        "enableChunkSplitterV2",
        "useSegmenter",
        "hashAlg",
        "minimumChunkSize",
        "customChunkSize",
        "idDerivationVersion",
        "idDerivationKey",
    }
)

_UNREADABLE_SETTINGS_REASON = "vault settings missing or unreadable"

# Settings this server cannot serve, with the human-readable refusal for
# each. The reason is reported for the first matching setting found.
_UNSUPPORTED_SETTINGS = (
    (
        "encrypt",
        "the vault uses LiveSync end-to-end encryption, "
        "which this server does not support",
    ),
    (
        "usePathObfuscation",
        "the vault uses path obfuscation, "
        "which this server does not support",
    ),
)


def _milestone_setting_maps(milestone_settings: dict) -> list[dict]:
    """Resolve preferred settings, or conservatively every device.

    Same resolution as the CouchDB backend startup check: without a
    ``tweak_values`` key the settings sit at the top level (older
    milestone shapes); otherwise the ``PREFERRED`` entry wins and, when
    absent, every per-device map is read. Non-map legacy device entries
    are skipped, but a present non-map PREFERRED entry is refused.
    A map only counts as settings
    when it holds at least one recognized tweak key, and resolution fails
    closed (``UnsupportedVaultSettings``) when no readable settings exist.
    """
    if not isinstance(milestone_settings, dict):
        raise UnsupportedVaultSettings(_UNREADABLE_SETTINGS_REASON)
    if "tweak_values" not in milestone_settings:
        candidates = [milestone_settings]
    else:
        entries = milestone_settings["tweak_values"]
        if not isinstance(entries, dict):
            raise UnsupportedVaultSettings(_UNREADABLE_SETTINGS_REASON)
        if _PREFERRED_DEVICE_ID in entries:
            preferred = entries[_PREFERRED_DEVICE_ID]
            if not isinstance(preferred, dict):
                raise UnsupportedVaultSettings(
                    "PREFERRED settings must be a mapping; observed type "
                    f"{type(preferred).__name__}"
                )
            candidates = [preferred]
        else:
            candidates = [
                value for value in entries.values() if isinstance(value, dict)
            ]
    maps = [
        settings
        for settings in candidates
        if _RECOGNIZED_TWEAK_KEYS & settings.keys()
    ]
    if not maps:
        raise UnsupportedVaultSettings(_UNREADABLE_SETTINGS_REASON)
    return maps


def resolve_path_identity(milestone_settings: dict) -> str:
    """Derive the vault's path identity mode from milestone settings.

    Case-sensitive settings select ``PATH_IDENTITY_CASE_SENSITIVE`` or
    ``PATH_IDENTITY_NFC_CASE_SENSITIVE`` independently of normalization.
    Otherwise returns ``PATH_IDENTITY_NFC`` when any resolved settings map sets
    ``pathNormalizationVersion`` to 1 (upstream v1 compares NFC forms when
    deriving document ids) and ``PATH_IDENTITY_LEGACY`` for 0 or absent
    (upstream treats an undefined version as 0). Like upstream
    ``assertSupportedPathNormalizationVersion``, every other value — other
    numbers, non-numbers, explicit null — is refused.

    Refuses with ``UnsupportedVaultSettings`` — reason in the message —
    when a resolved map turns on ``encrypt``, ``usePathObfuscation``,
    when the version is not 0/1/absent,
    or when no readable settings exist; backends re-raise that as their
    storage-layer ``UnsupportedVault`` (see the mapping rule on
    ``UnsupportedVaultSettings``).
    """
    # Case and NFC are independent axes (PathService.ts:51-63,91-106).
    nfc = False
    case_sensitive = False
    for settings in _milestone_setting_maps(milestone_settings):
        for key, reason in _UNSUPPORTED_SETTINGS:
            if settings.get(key):
                raise UnsupportedVaultSettings(reason)
        case_sensitive |= bool(settings.get("handleFilenameCaseSensitive"))
        if "pathNormalizationVersion" not in settings:
            continue  # absent means legacy (upstream: undefined -> 0)
        version = settings["pathNormalizationVersion"]
        if (
            isinstance(version, bool)
            or not isinstance(version, (int, float))
            or version not in (0, 1)
        ):
            raise UnsupportedVaultSettings(
                f"unsupported pathNormalizationVersion {version!r}; "
                "this server supports 0 (legacy) and 1 (NFC)"
            )
        if version == 1:
            nfc = True
    if case_sensitive:
        return PATH_IDENTITY_NFC_CASE_SENSITIVE if nfc else PATH_IDENTITY_CASE_SENSITIVE
    return PATH_IDENTITY_NFC if nfc else PATH_IDENTITY_LEGACY


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

    for prefix in SERVICE_NAMESPACE_PREFIXES:
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

    if lowered in SERVICE_DOC_IDS:
        raise ForbiddenPathError(
            f"vault path is the reserved service document {lowered!r}"
        )


def is_text_doc(doc_type: str | None, path: str) -> bool:
    """Classify storage representation: plain text or newnote bytes.

    The path never overrides an explicit newnote type. UTF-8 presentation
    belongs to the tool layer, not the chunk decoder.
    """
    return doc_type == "plain"


def resolve_doc_type(path: str, data: str | bytes) -> str:
    """Resolve device *disk ingestion*, independent of content or Python type.

    Pinned commonlib 484c164: string_and_binary/path.ts:164-174 and
    serviceModules/FileAccessBase.ts:162-167,201-206 select host text reading
    only for the exact case-sensitive suffixes. ServiceFileAccessBase.ts:
    265-276 wraps that result in createBlob; common/utils.ts:97-98 and
    managers/EntryManager/EntryManagerImpls.ts:252-256 then classify the
    text/plain Blob as plain, every other Blob as newnote. The programmatic
    determineType entry point is deliberately not used here.
    """
    return "plain" if _normalize_path(path).endswith(_TEXT_SUFFIXES) else "newnote"


class InvalidPlainTextBytes(ValueError):
    """Reject malformed byte ingress before publishing a plain document."""


def plain_ingress_utf8(raw: bytes, *, lossy_text: bool = False) -> tuple[str, int]:
    """Our explicit byte-input policy, NOT wheels readString.

    FileAccessBase.ts:201-206 delegates disk text reads to the native host;
    its malformed-byte behavior remains a Task 8 live question. Replacement
    here is opt-in policy, not a verified emulation of that host adapter.
    Wheels Blob-to-string decoding inside chunk preparation is independently
    implemented by content_splitter._wheels_read_string and must remain unchanged.
    """
    try:
        return raw.decode('utf-8'), 0
    except UnicodeDecodeError:
        invalid_bytes = len(raw) - len(raw.decode('utf-8', errors='ignore').encode('utf-8'))
        if not lossy_text:
            raise InvalidPlainTextBytes(
                f'invalid UTF-8: {invalid_bytes} invalid bytes; the path extension selects '
                'plain text. A device text read can store malformed bytes lossily '
                '(native host semantics remain unverified). Use a non-plain path '
                'for binary content or explicitly convert with lossy_text=true.'
            ) from None
        return raw.decode('utf-8', errors='replace'), invalid_bytes


def prepare_file_content(
    path: str, content: str | bytes,
) -> tuple[str | bytes, str, int]:
    """Model host reading followed by Blob creation; return body, type, size.

    Host decoding is outside commonlib (FileAccessBase.ts:201-206).
    Refuse malformed plain byte input by default (P-9); explicit lossy
    conversion belongs to the upload tool. This is separate from wheels
    readString used by chunk preparation. See plain_ingress_utf8.
    """
    doc_type = resolve_doc_type(path, content)
    if isinstance(content, str):
        try:
            raw = content.encode("utf-8")
        except UnicodeEncodeError:
            # createBlob/toBlob (common/utils.ts:100-115) uses Blob's
            # USVString conversion: combine pairs and replace lone surrogates.
            content = content.encode("utf-16-le", errors="surrogatepass").decode(
                "utf-16-le", errors="replace")
            raw = content.encode("utf-8")
        return (content if doc_type == "plain" else raw), doc_type, len(raw)
    if doc_type == "plain":
        text, _ = plain_ingress_utf8(content)
        return text, doc_type, len(text.encode("utf-8"))
    return content, doc_type, len(content)


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
    """Split raw binary bytes and base64-encode each upstream v3 piece."""
    return ContentSplitter().split(data)


def chunk_id_for(data: str) -> str:
    """Compatibility name for the upstream xxhash64 piece ID derivation."""
    return chunk_id_for_piece(data)


class UnsupportedWriterSettings(UnsupportedVaultSettings):
    """Writer-only incompatibility; existing content remains readable."""


def resolve_content_splitter(milestone_settings: dict) -> ContentSplitter:
    """Resolve preferred tweaks, refusing ambiguous per-device split settings."""
    try:
        splitters = [ContentSplitter(settings, normalize_settings=True)
                     for settings in _milestone_setting_maps(milestone_settings)]
    except UnsupportedWriterMode as exc:
        raise UnsupportedWriterSettings(str(exc)) from exc
    except ValueError as exc:
        raise UnsupportedVaultSettings(str(exc)) from exc
    first = splitters[0]
    if any((s.version, s.minimum_chunk_size, s.absolute_max_piece_size, s.hash_alg) !=
           (first.version, first.minimum_chunk_size, first.absolute_max_piece_size, first.hash_alg)
           for s in splitters[1:]):
        raise UnsupportedVaultSettings('conflicting chunk splitter settings')
    return first


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
        "path": canonical_path(path),
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
    return clone_metadata(doc, deleted=True, mtime=mtime_ms)


class EncryptedInlineChunk(ValueError):
    """An inline leaf requires unsupported end-to-end decryption."""


def is_service_document_id(doc_id: str) -> bool:
    """Classify reserved LiveSync/CouchDB document ids."""
    lowered = doc_id.lower()
    return (lowered.startswith(SERVICE_NAMESPACE_PREFIXES)
            or lowered in SERVICE_DOC_IDS
            or doc_id.startswith("_design/") or doc_id.startswith("_local"))


def is_note_document(doc: dict) -> bool:
    """Whether a document is live note metadata, excluding service ids."""
    return (not doc.get("deleted")
            and doc.get("type") in ("plain", "newnote", "notes")
            and not is_service_document_id(str(doc.get("_id", ""))))


def extract_inline_chunks(doc: dict) -> dict[str, str]:
    """Resolve referenced eden leaves (strings or upstream {data: ...}).

    Ignore unreferenced leaves, but refuse corrupt or encrypted referenced
    leaves rather than falling back to a remote chunk with the same id.
    """
    eden = doc.get("eden")
    if eden is None:
        eden = {}
    if not isinstance(eden, dict):
        raise ValueError("invalid eden field")
    result = {}
    for child in doc.get("children") or []:
        if child not in eden:
            continue
        value = eden[child]
        if isinstance(value, dict):
            if value.get("deleted") or not isinstance(value.get("data"), str):
                raise ValueError("invalid inline chunk")
            if value.get("e_"):
                raise EncryptedInlineChunk("encrypted inline chunk")
            value = value["data"]
        if not isinstance(value, str):
            raise ValueError("invalid inline chunk")
        result[child] = value
    return result


def legacy_body(doc: dict) -> str | None:
    """Return the in-document legacy notes body; refuse malformed bodies."""
    if doc.get("type") != "notes":
        return None
    data = doc.get("data")
    if not isinstance(data, str):
        raise ValueError("missing legacy data field")
    return data


def clone_metadata(doc: dict, **overrides) -> dict:
    """Clone all metadata/content fields, including unknown future fields.

    Publication identity, revision and conflict shadows are backend concerns;
    callers explicitly override or remove them for the intended operation.
    """
    return deepcopy({**doc, **overrides})
