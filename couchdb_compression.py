"""LiveSync CouchDB document transform (commonlib 484c164, compress.ts:9-101).

Read independently of the writer setting (RemoteService.ts:457). Keep data
immutable here so failed decoding cannot partially modify a caller's document.
"""
import codecs
import math
import base64
import binascii
import re
import zlib

from diagnostic_hygiene import DiagnosticCode, PublicDiagnostic
from storage_base import StorageError

MARK_SHIFT_COMPRESSED = '\x0eLZ\x1d'
_BASE64 = re.compile(r'[A-Za-z0-9+/]+=*', re.ASCII)


class CompressedDataError(StorageError):
    """A marked CouchDB body cannot be safely decoded."""


DEFAULT_INFLATED_BYTES = 32 * 1024**2


def resolved_inflate_budget(splitter) -> int:
    # ContentSplitterBase.ts:68 cap is in bytes (binary) or UTF-16 units
    # (legacy text); chunks.ts:527-545,617-633 caps finite V3 at 4 MiB+3.
    # 4/3 base64 expansion times safety factor 4 also covers 3-byte UTF-16
    # text units. NaN/Infinity can emit whole notes: no finite native maximum;
    # retain the operational floor and let the operator raise the override.
    maximum = getattr(splitter, 'absolute_max_piece_size', 0)
    if not isinstance(maximum, (int, float)) or not math.isfinite(maximum):
        return DEFAULT_INFLATED_BYTES
    maximum = max(4 * 1024**2, int(maximum)) + 3
    return max(DEFAULT_INFLATED_BYTES, ((maximum + 2) // 3) * 4 * 4)


class CouchDBInflateBudgetExceeded(StorageError):
    """Operational refusal with no remote free text."""

    def __init__(self, limit):
        self.limit = limit
        super().__init__()

    def format(self):
        from diagnostic_hygiene import numeric_field
        return (f'CouchDB inflated data exceeds budget {numeric_field(self.limit)} bytes; '
                'raise LIVESYNC_COUCHDB_MAX_INFLATED_BYTES and restart, or restore '
                'the document from a healthy device if corrupt')


def decompress_data(data: object, *, max_inflated_bytes: int = DEFAULT_INFLATED_BYTES) -> object:
    """Mirror compress.ts:26-47,62-71, including Blob.text replacement/BOM.

    Unlike the published decoder's swallowed base64 errors, malformed marked
    values refuse with a closed diagnostic; they must never become empty notes.
    """
    if not isinstance(data, str) or not data.startswith(MARK_SHIFT_COMPRESSED):
        return data
    payload = data[len(MARK_SHIFT_COMPRESSED):]
    binary = payload.startswith('~')
    if binary:
        payload = payload[1:]
    if not payload:
        return ''
    try:
        # atob-compatible ASCII whitespace and optional padding; invalid
        # characters/padding and incomplete raw streams must fail closed.
        compact = re.sub(r'[\t\n\f\r ]', '', payload)
        packed = base64.b64decode(compact + '=' * (-len(compact) % 4), validate=True)
        inflater = zlib.decompressobj(wbits=-15)
        parts = []
        decoder = codecs.getincrementaldecoder('utf-8-sig')(errors='replace')
        raw_size = projected_size = 0

        def accept(chunk, final=False):
            nonlocal raw_size, projected_size
            raw_size += len(chunk)
            if raw_size > max_inflated_bytes:
                raise CouchDBInflateBudgetExceeded(max_inflated_bytes)
            if binary:
                projected_size = ((raw_size + 2) // 3) * 4
                part = chunk
            else:
                part = decoder.decode(chunk, final=final)
                projected_size += len(part.encode('utf-8'))
            if projected_size > max_inflated_bytes:
                raise CouchDBInflateBudgetExceeded(max_inflated_bytes)
            parts.append(part)

        for offset in range(0, len(packed), 64 * 1024):
            pending = packed[offset:offset + 64 * 1024]
            while True:
                # Output cap is mandatory; probe one byte at exhaustion, never
                # retain it. Do not call unbounded flush() on hostile input.
                chunk = inflater.decompress(pending, min(64 * 1024, max(1, max_inflated_bytes - raw_size)))
                accept(chunk)
                pending = inflater.unconsumed_tail
                if inflater.eof or (not pending and not chunk):
                    break
            if inflater.eof:
                break
        if not inflater.eof:
            raise ValueError('incomplete stream')
        accept(b'', final=True)
        return (base64.b64encode(b''.join(parts)).decode('ascii') if binary else ''.join(parts))
    except (ValueError, binascii.Error, zlib.error) as exc:
        raise CompressedDataError(public=PublicDiagnostic(
            DiagnosticCode.COMPRESSED_DATA, cause=type(exc))) from None


def compress_data(data: object) -> object:
    """compress.ts:9-24,49-60; raw level-8 deflate need not equal fflate bytes.

    Published wheels binary/base64.js:367-408 strips CR/LF before its canonical
    base64 check. Deliberately retain CR/LF as text. Blob USVString replaces
    unpaired surrogates and Blob.text strips BOM: publish those pieces verbatim
    instead. Every device can read uncompressed data (compress.ts:62-71).
    """
    if not isinstance(data, str) or data.startswith(MARK_SHIFT_COMPRESSED):
        return data
    if data.startswith('\ufeff') or any(0xd800 <= ord(c) <= 0xdfff for c in data):
        return data
    binary = False
    raw = data.encode('utf-8')
    if '\r' not in data and '\n' not in data and _BASE64.fullmatch(data):
        try:
            decoded = base64.b64decode(data, validate=True)
            if base64.b64encode(decoded).decode('ascii') == data:
                raw, binary = decoded, True
        except (ValueError, binascii.Error):
            pass
    if not raw:
        return data
    encoder = zlib.compressobj(level=8, wbits=-15)
    packed = encoder.compress(raw) + encoder.flush()
    candidate = MARK_SHIFT_COMPRESSED + ('~' if binary else '') + base64.b64encode(packed).decode('ascii')
    # JS String.length counts UTF-16 units, not Python code points.
    return candidate if len(candidate) < len(data.encode('utf-16-le')) // 2 else data


def decompress_doc(doc: dict, *, max_inflated_bytes: int = DEFAULT_INFLATED_BYTES) -> dict:
    if 'data' not in doc:
        return doc
    return {**doc, 'data': decompress_data(doc['data'], max_inflated_bytes=max_inflated_bytes)}


def compress_doc(doc: dict) -> dict:
    if 'data' not in doc:
        return doc
    return {**doc, 'data': compress_data(doc['data'])}
