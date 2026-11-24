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

from diagnostic_hygiene import invalid_setting, diagnostic, PublicDiagnostic, DiagnosticCode

from dataclasses import dataclass, field
from collections.abc import Mapping
from types import MappingProxyType

import base64
import binascii
import math
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
    """Codec-owned reason and fields; backends project these without str()."""

    def __init__(self, reason):
        self.diagnostic = diagnostic(reason)
        if self.diagnostic.code is DiagnosticCode.FAILED:
            self.diagnostic = PublicDiagnostic(DiagnosticCode.SETTINGS_UNREADABLE, cause=type(self))
        super().__init__(reason)


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
        "syncMaxSizeInMB",
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
        if (_RECOGNIZED_TWEAK_KEYS | {rule.key for rule in TWEAK_COMPATIBILITY_RULES}) & settings.keys()
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
            error = UnsupportedVaultSettings(
                "unsupported " + invalid_setting("pathNormalizationVersion", version) + "; "
                "this server supports 0 (legacy) and 1 (NFC)"
            )
            error.setting_keys = ('pathNormalizationVersion',)
            raise error
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
    """Reject malformed byte ingress with a typed invalid-byte count."""

    def __init__(self, invalid_bytes: int):
        self.invalid_bytes = invalid_bytes
        super().__init__(self.format())

    def format(self):
        return (
            f'invalid UTF-8: {self.invalid_bytes} invalid bytes; the path extension selects '
            'plain text. A device text read can store malformed bytes lossily '
            '(native host semantics remain unverified). Use a non-plain path '
            'for binary content or explicitly convert with lossy_text=true.'
        )


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
            raise InvalidPlainTextBytes(invalid_bytes) from None
        return raw.decode('utf-8', errors='replace'), invalid_bytes


def prepare_file_content(
    path: str, content: str | bytes, *, lossy_text: bool = False,
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
        text, _ = plain_ingress_utf8(content, lossy_text=lossy_text)
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


def resolve_sync_max_size(milestone_settings: dict) -> float:
    """Use the identity settings resolution; ambiguous device values fail closed.

    C:common/models/setting.const.defaults.ts:147 supplies missing value 50.
    C:services/base/VaultService.ts:73-81 defines the predicate, not a cap.
    """
    import math
    values = [settings.get('syncMaxSizeInMB', 50)
              for settings in _milestone_setting_maps(milestone_settings)]
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) for v in values):
        raise UnsupportedVaultSettings('syncMaxSizeInMB must be a finite number')
    if any(v != values[0] for v in values[1:]):
        raise UnsupportedVaultSettings('conflicting syncMaxSizeInMB settings')
    return values[0]


def resolve_content_splitter(milestone_settings: dict) -> ContentSplitter:
    """Resolve preferred tweaks, refusing ambiguous per-device split settings."""
    try:
        splitters = [ContentSplitter(settings, normalize_settings=True)
                     for settings in _milestone_setting_maps(milestone_settings)]
    except UnsupportedWriterMode as exc:
        error = UnsupportedWriterSettings(str(exc))
        error.setting_keys = getattr(exc, "setting_keys", ())
        error.setting_issues = getattr(exc, "setting_issues", ())
        raise error from exc
    except ValueError as exc:
        error = UnsupportedVaultSettings(str(exc))
        error.setting_keys = getattr(exc, "setting_keys", ())
        raise error from exc
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


@dataclass(frozen=True)
class TargetVerdict:
    """Device selection decision, including fail-closed engine diagnostics."""

    allowed: bool
    rule: str | None = None
    writer_supported: bool = True
    reason: str | None = None


def parse_custom_regexp_list(source: str) -> tuple[str, ...]:
    """common/utils.ts: remove only LF and ASCII space; preserve duplicates."""
    return tuple(item for item in source.replace('\n', '').replace(' ', '').split('|[]|') if item)


def is_target_file(path: str, *, only_patterns: str = '', ignore_patterns: str = '',
                   case_sensitive: bool = False, ignore_files: dict | None = None,
                   ignore_names: tuple[str, ...] = ()) -> TargetVerdict:
    """Execute pinned device selection using ECMAScript and upstream minimatch.

    No storage access or mutation. Node is needed only for active filters;
    Python regex is deliberately never substituted for ECMAScript RegExp.
    """
    from selection_engine import evaluate, EngineFailure

    if not only_patterns and not ignore_patterns and ignore_files is None:
        return TargetVerdict(True)
    payload = dict(path=path, only_patterns=only_patterns, ignore_patterns=ignore_patterns,
                   case_sensitive=case_sensitive, ignore_files=ignore_files,
                   ignore_names=ignore_names)
    try:
        return evaluate(payload)
    except EngineFailure as exc:
        return TargetVerdict(False, writer_supported=False, reason=str(exc))


# C:common/models/tweak.definition.ts:24-67, pinned in docs/upstream-pin.md.
# (key, template value, reconstruction, known default, compatible lossy order)
_TWEAK_UNDEFINED = object()


@dataclass(frozen=True)
class TweakRule:
    key: str
    template_value: object
    reconstruction: str = 'none'
    known_default: object = _TWEAK_UNDEFINED
    compatible_lossy_order: int | None = None


TWEAK_COMPATIBILITY_RULES = (
    TweakRule('minimumChunkSize', 20), TweakRule('longLineThreshold', 250),
    TweakRule('encrypt', False, 'required'),
    TweakRule('usePathObfuscation', False, 'required'),
    TweakRule('idDerivationVersion', 0, 'required', 0),
    TweakRule('pathNormalizationVersion', 0, 'required', 0),
    TweakRule('encryptInternalMetadata', False, 'none', False),
    TweakRule('enableCompression', False), TweakRule('useEden', False),
    TweakRule('customChunkSize', 0, 'recommended', compatible_lossy_order=1),
    TweakRule('useDynamicIterationCount', False, 'required'),
    TweakRule('hashAlg', 'xxhash64', 'recommended', compatible_lossy_order=0),
    TweakRule('enableChunkSplitterV2', False), TweakRule('maxChunksInEden', 10),
    TweakRule('maxTotalLengthInEden', 1024), TweakRule('maxAgeInEden', 10),
    TweakRule('usePluginSyncV2', False, 'none', False),
    TweakRule('handleFilenameCaseSensitive', False, 'required', False),
    TweakRule('useSegmenter', False), TweakRule('E2EEAlgorithm', 'v2', 'none', 'v2'),
    TweakRule('chunkSplitterVersion', 'v3-rabin-karp', 'recommended',
              'v3-rabin-karp', 2),
)

# C:tweak.definition.ts:113-150. Recommendations are snapshotted for adoption,
# but never participate in semantic comparison.
_TWEAK_TEMPLATE_KEYS = frozenset(rule.key for rule in TWEAK_COMPATIBILITY_RULES) | {
    'useIgnoreFiles', 'useCustomRequestHandler', 'batch_size', 'batches_limit',
    'useTimeouts', 'readChunksOnline', 'hashCacheMaxCount', 'hashCacheMaxAmount',
    'concurrencyOfReadChunksOnline', 'minimumIntervalOfReadChunksOnline',
    'ignoreFiles', 'syncMaxSizeInMB', 'tweakModified',
}


def _tweak_number(value: int | float) -> float:
    """JSON numbers have binary64 precision in the upstream JS runtime."""
    try:
        return float(value)
    except OverflowError:
        return -math.inf if value < 0 else math.inf


def _tweak_equal(left: object, right: object) -> bool:
    """JS strict equality for JSON primitives; objects compare by identity."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return _tweak_number(left) == _tweak_number(right)
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return left is right
    return type(left) is type(right) and left == right


@dataclass(frozen=True)
class TweakValue:
    present: bool
    raw_value: object
    effective_value: object


@dataclass(frozen=True)
class TweakEntry:
    key: str
    current: TweakValue
    preferred: TweakValue
    relation: str


@dataclass(frozen=True)
class TweakTransition:
    changes: Mapping
    reconstruction: str
    reasons: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TweakAssessment:
    status: str
    differing_keys: tuple[str, ...]
    alignment: str
    representation_differs: bool
    entries: tuple[TweakEntry, ...]
    adopt_preferred: TweakTransition
    adopt_current: TweakTransition
    only_compatible_lossy_differences: bool
    current_values: Mapping = field(repr=False)
    preferred_values: Mapping = field(repr=False)

    def summary(self) -> dict:
        return dict(status=self.status, differing_keys=list(self.differing_keys))


def _tweak_transition(entries, source_side, target_side) -> TweakTransition:
    changes, reasons = {}, []
    reconstruction = 'none'
    for entry, rule in zip(entries, TWEAK_COMPATIBILITY_RULES):
        source, target = getattr(entry, source_side), getattr(entry, target_side)
        differs = not _tweak_equal(source.effective_value, target.effective_value)
        fill = not target.present and source.present
        if source.effective_value is _TWEAK_UNDEFINED or not (differs or fill):
            continue
        if entry.key != 'idDerivationVersion':
            changes[entry.key] = source.effective_value
        if entry.relation == 'equal' or rule.reconstruction == 'none':
            continue
        reasons.append((entry.key, rule.reconstruction))
        if reconstruction != 'required':
            reconstruction = rule.reconstruction
    return TweakTransition(MappingProxyType(changes), reconstruction, tuple(reasons))


def assess_tweak_compatibility(preferred: Mapping, candidate: Mapping) -> TweakAssessment:
    """Assess one-pass normalized settings without mutating or exposing secrets.

    C:common/models/tweak.compatibility.ts:47-65,148-191. The public argument
    order is preferred, candidate (upstream takes current, preferred).
    SettingService.ts:141-158 migration runs once on each input first, using
    the Task 3c helper. Template values are NOT implied compatibility defaults.
    """
    from content_splitter import normalize_splitter_settings
    return _assess_normalized_tweaks(normalize_splitter_settings(preferred),
                                     normalize_splitter_settings(candidate))


def _assess_normalized_tweaks(preferred: Mapping, candidate: Mapping) -> TweakAssessment:
    def snapshot(values):
        return {k: _tweak_number(v) if type(v) is int else v
                for k, v in values.items() if k in _TWEAK_TEMPLATE_KEYS}
    current, remote = snapshot(candidate), snapshot(preferred)
    current_keyed = current.get('encrypt') is not False and current.get('usePathObfuscation') is not False
    remote_keyed = remote.get('encrypt') is not False and remote.get('usePathObfuscation') is not False
    entries = []
    representation_differs = False
    for rule in TWEAK_COMPATIBILITY_RULES:
        def value(side):
            raw = side.get(rule.key, _TWEAK_UNDEFINED)
            return TweakValue(rule.key in side, raw,
                              rule.known_default if raw is _TWEAK_UNDEFINED else raw)
        local, preferred_value = value(current), value(remote)
        if rule.key == 'idDerivationVersion' and not current_keyed and not remote_keyed:
            relation = 'equal'
        elif (not local.present or not preferred_value.present) and rule.known_default is _TWEAK_UNDEFINED:
            relation = 'unadvertised'
        else:
            relation = 'equal' if _tweak_equal(local.effective_value, preferred_value.effective_value) else 'different'
        representation_differs |= (local.present != preferred_value.present or
            not _tweak_equal(local.raw_value, preferred_value.raw_value))
        entries.append(TweakEntry(rule.key, local, preferred_value, relation))
    entries = tuple(entries)
    keys = tuple(e.key for e in entries if e.relation == 'different')
    lossy_keys = {r.key for r in TWEAK_COMPATIBILITY_RULES if r.compatible_lossy_order is not None}
    lossy = bool(keys) and set(keys) <= lossy_keys and not any(e.relation == 'unadvertised' for e in entries)
    status = ('ID_KEY_MISMATCH' if 'idDerivationVersion' in keys else
              'PATH_IDENTITY_MISMATCH' if 'pathNormalizationVersion' in keys else
              'LOSSY_AUTO_ACCEPTABLE' if lossy else 'MISMATCHED' if keys else 'OK')
    return TweakAssessment(status, keys, 'mismatched' if keys else 'matched',
        representation_differs, entries,
        _tweak_transition(entries, 'preferred', 'current'),
        _tweak_transition(entries, 'current', 'preferred'), lossy,
        MappingProxyType(current), MappingProxyType(remote))


def select_newer_tweak_side(current: Mapping, preferred: Mapping) -> str:
    """P:ModuleResolveMismatchedTweaks.ts:120-132; booleans aren't numbers."""
    def modified(side):
        value = side.get('tweakModified')
        return _tweak_number(value) if type(value) in (int, float) and value > 0 else 0
    return 'CURRENT' if modified(current) > modified(preferred) else 'REMOTE'


@dataclass(frozen=True)
class TweakStartup:
    settings: Mapping = field(repr=False)
    status: str = 'OK'
    differing_keys: tuple[str, ...] = ()
    winners: tuple[str, ...] = ()

    def summary(self):
        return dict(status=self.status, differing_keys=list(self.differing_keys))


def _effective_peer_tweaks(settings: Mapping, *, couchdb_mode: bool) -> dict:
    """Concrete writer defaults; never advertise an unavailable adapter feature."""
    effective = dict(settings)
    for key, value in dict(minimumChunkSize=20, customChunkSize=0, hashAlg='xxhash64',
            idDerivationVersion=0, pathNormalizationVersion=0, encrypt=False,
            usePathObfuscation=False, handleFilenameCaseSensitive=False).items():
        effective.setdefault(key, value)
    effective['encryptInternalMetadata'] = False
    if couchdb_mode:
        # C:replication/couchdb/LiveSyncReplicator.ts:332,2097 passes this
        # to the CouchDB compression adapter; this peer has no such adapter.
        # Journal framing does not consult enableCompression.
        effective['enableCompression'] = False
    return effective


def resolve_tweak_startup(milestone: Mapping, *, couchdb_mode: bool = False,
        disable_checking_config_mismatch: bool = False,
        auto_accept_compatible_tweak: bool = True) -> TweakStartup:
    """Resolve writer settings and assess every advertised device before I/O.

    C:LiveSyncDBFunctions.ts:79-118 uses PREFERRED or the current settings in
    both transports. Checking other advertised devices is this peer's explicit
    startup policy; it does not edit their snapshots or publish PREFERRED.
    """
    maps = _milestone_setting_maps(milestone)
    preferred = maps[0]
    devices = milestone.get('tweak_values', {})
    # getEffectiveTweakValues advertises internal metadata encryption only
    # for an encrypted, obfuscated CouchDB vault. This peer serves plaintext
    # only; journal devices always advertise false (C:LiveSyncDBFunctions:19-24).
    peer = _effective_peer_tweaks(preferred, couchdb_mode=couchdb_mode)
    candidates = [peer]
    if isinstance(devices, dict):
        candidates.extend(v for k, v in devices.items()
                          if k != _PREFERRED_DEVICE_ID and isinstance(v, dict))
    winner = preferred
    winner_assessment = None
    has_lossy_alignment = False
    winners, keys, blocked = [], set(), []
    priority = {'OK': 0, 'LOSSY_AUTO_ACCEPTABLE': 1, 'MISMATCHED': 2,
                'PATH_IDENTITY_MISMATCH': 3, 'ID_KEY_MISMATCH': 4}
    overall = 'OK'
    for candidate in candidates:
        assessment = assess_tweak_compatibility(preferred, candidate)
        keys.update(assessment.differing_keys)
        if priority[assessment.status] > priority[overall]:
            overall = assessment.status
        gated = (disable_checking_config_mismatch is True and
                 assessment.status not in ('ID_KEY_MISMATCH', 'PATH_IDENTITY_MISMATCH'))
        if assessment.status != 'OK' and not gated:
            if assessment.only_compatible_lossy_differences and auto_accept_compatible_tweak is True:
                has_lossy_alignment = True
                winners.append(select_newer_tweak_side(candidate, preferred))
            else:
                blocked.append(assessment)
    if has_lossy_alignment:
        for candidate in candidates:
            if select_newer_tweak_side(candidate, winner) == 'CURRENT':
                winner = candidate
                winner_assessment = assess_tweak_compatibility(preferred, candidate)

    differing_keys = tuple(rule.key for rule in TWEAK_COMPATIBILITY_RULES if rule.key in keys)
    if blocked:
        error = UnsupportedVaultSettings(
            'vault tweak settings differ: ' + ', '.join(differing_keys) +
            '; devices require alignment with PREFERRED before synchronizing')
        error.tweak_assessment = dict(status=overall, differing_keys=list(differing_keys))
        raise error
    # Encryption and obfuscation remain unsupported even with the gate off.
    for candidate in candidates:
        for key, reason in _UNSUPPORTED_SETTINGS:
            if candidate.get(key):
                # Preserve the separate cryptographic-ID classification above.
                raise UnsupportedVaultSettings(reason)
    settings = dict(preferred)
    if winner_assessment is not None:
        # P:ModuleResolveMismatchedTweaks.ts:66-81: preserve omitted target
        # settings, adopt recommendations, and never transfer the ID key/mode.
        compared = {rule.key for rule in TWEAK_COMPATIBILITY_RULES}
        settings.update({k: v for k, v in winner.items()
                         if k in _TWEAK_TEMPLATE_KEYS and k not in compared})
        for key, value in winner_assessment.adopt_current.changes.items():
            if key == 'chunkSplitterVersion' and key not in winner:
                settings.pop(key, None)
            else:
                # Keep the raw splitter input so the writer migration runs
                # exactly once, including unknown-version -> empty cases.
                settings[key] = winner.get(key, value)
    effective_peer = _effective_peer_tweaks(settings, couchdb_mode=couchdb_mode)
    for device in candidates[1:]:
        assessment = assess_tweak_compatibility(device, effective_peer)
        keys.update(assessment.differing_keys)
        if priority[assessment.status] > priority[overall]:
            overall = assessment.status
        gated = (disable_checking_config_mismatch is True and
                 assessment.status not in ('ID_KEY_MISMATCH', 'PATH_IDENTITY_MISMATCH'))
        if assessment.status != 'OK' and not gated and not (
                assessment.only_compatible_lossy_differences and auto_accept_compatible_tweak is True):
            blocked.append(assessment)
    differing_keys = tuple(rule.key for rule in TWEAK_COMPATIBILITY_RULES if rule.key in keys)
    if blocked:
        error = UnsupportedVaultSettings(
            'vault tweak settings differ: ' + ', '.join(differing_keys) +
            '; devices require alignment with PREFERRED before synchronizing')
        error.tweak_assessment = dict(status=overall, differing_keys=list(differing_keys))
        raise error
    return TweakStartup(MappingProxyType(settings), overall, differing_keys, tuple(winners))
