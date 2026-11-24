"""LiveSync CouchDB document transform (commonlib 484c164, compress.ts:9-101).

Read independently of the writer setting (RemoteService.ts:457). Keep data
immutable here so failed decoding cannot partially modify a caller's document.
"""
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


def decompress_data(data: object) -> object:
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
        inflated = inflater.decompress(packed) + inflater.flush()
        if not inflater.eof:
            raise ValueError('incomplete stream')
        return (base64.b64encode(inflated).decode('ascii') if binary else
                inflated.decode('utf-8-sig', errors='replace'))
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


def decompress_doc(doc: dict) -> dict:
    if 'data' not in doc:
        return doc
    return {**doc, 'data': decompress_data(doc['data'])}


def compress_doc(doc: dict) -> dict:
    if 'data' not in doc:
        return doc
    return {**doc, 'data': compress_data(doc['data'])}
