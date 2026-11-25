"""LiveSync content splitting and unencrypted chunk identities.

Source: vrtmrz/livesync-commonlib@484c164450d61deb2e301582b60cb9b5f9fa977f
src/string_and_binary/chunks.ts:13-72,218-493 (V1/V2),
:494-650 (Rabin-Karp rolling state and thresholds),
src/ContentSplitter/ContentSplitterBase.ts:65-94 (settings and path hint),
src/managers/HashManager/XXHashHashManager.ts:73-99 (hash input/base36).
See docs/superpowers/specs/2026-11-23-settings-parity.md sections а/б.
"""
from __future__ import annotations

from diagnostic_hygiene import invalid_setting, SettingIssue

import base64
import hashlib
import math
import re
from collections.abc import Mapping

import xxhash


class UnsupportedWriterMode(ValueError):
    """Readable vault settings whose writer is not implemented yet."""


def _js_utf8(text: str) -> bytes:
    # Blob/TextEncoder replace lone JS surrogates with U+FFFD and combine pairs.
    return text.encode('utf-16-le', errors='surrogatepass').decode(
        'utf-16-le', errors='replace').encode('utf-8')


class _TextBlob(bytes):
    """Raw text/plain Blob bytes, including malformed UTF-8 (oracle input)."""


def _wheels_read_string(raw: bytes) -> str:
    """octagonal-wheels@0.1.57 readString, including its <=128-byte path.

    Long buffers use nonfatal TextDecoder(ignoreBOM=true). Short buffers
    manually consume unchecked continuation bytes; absent bytes coerce to 0,
    unrecognised lead bytes disappear, and fromCharCode truncates to uint16.
    """
    if len(raw) > 128:
        return raw.decode('utf-8', errors='replace')
    units = []
    index = 0

    def continuation():
        nonlocal index
        value = raw[index] if index < len(raw) else 0
        index += 1
        return value & 63

    while index < len(raw):
        c = raw[index]
        index += 1
        if c < 128:
            units.append(c)
        elif c & 0xe0 == 0xc0:
            units.append(((c & 31) << 6) | continuation())
        elif c & 0xf0 == 0xe0:
            units.append(((c & 15) << 12) | (continuation() << 6) | continuation())
        elif c & 0xf8 == 0xf0:
            code = ((c & 7) << 18) | (continuation() << 12) | (continuation() << 6) | continuation()
            if code < 0x10000:
                units.append(code)
            else:
                code -= 0x10000
                units.extend(((code >> 10) + 0xd800, (code & 1023) + 0xdc00))
    return b''.join((unit & 0xffff).to_bytes(2, 'little') for unit in units).decode(
        'utf-16-le', errors='surrogatepass')


def _utf16_units(text: str) -> list[int]:
    raw = text.encode('utf-16-le', errors='surrogatepass')
    return [raw[i] | raw[i + 1] << 8 for i in range(0, len(raw), 2)]


def _base36(value: int) -> str:
    if value < 0:
        return '-' + _base36(-value)
    digits = ''
    while value:
        value, remainder = divmod(value, 36)
        digits = '0123456789abcdefghijklmnopqrstuvwxyz'[remainder] + digits
    return digits or '0'


def _int32(value: int) -> int:
    value &= 0xffffffff
    return value if value < 0x80000000 else value - 0x100000000


def _rotate32(value: int, shift: int) -> int:
    value &= 0xffffffff
    return _int32((value << shift) | (value >> (32 - shift)))


def _mixed_hash(text: str) -> str:
    # octagonal-wheels@0.1.57 dist/hash/purejs.js:7-58.
    # Ordinary JS products round to binary64; Math.imul keeps low 32 bits.
    units = _utf16_units(str(len(_utf16_units(text))) + text)
    h, fnv = 1, 2166136261
    for unit in units:
        fnv = ((fnv ^ unit) * 0x01000193) & 0xffffffff
        k = _rotate32(int(float(unit) * 0xcc9e2d51), 15)
        k = int(float(k) * 0x1b873593)
        h = _rotate32(_int32(h) ^ _int32(k), 13)
        h = h * 5 + 0xe6546b64
    h = _int32(h) ^ len(units)
    h = _int32((h ^ ((h & 0xffffffff) >> 16)) * 0x85ebca6b)
    h = _int32((h ^ ((h & 0xffffffff) >> 13)) * 0xc2b2ae35)
    h ^= (h & 0xffffffff) >> 16
    return _base36(h & 0xffffffff) + _base36(fnv)


def _write_string(text: str) -> bytes:
    # octagonal-wheels@0.1.57 dist/binary/base64.js:198-240.
    # The short manual path consumes ANY surrogate and its next unit as a
    # pair, without validation. Missing charCodeAt is NaN, coerced to zero
    # by bitwise OR. Do not replace this with a normal UTF-8 encoder.
    units = _utf16_units(text)
    if len(units) > 128:
        return _js_utf8(text)
    result = bytearray()
    i = 0
    while i < len(units):
        c = units[i]
        i += 1
        if c < 0x80:
            result.append(c)
        elif c < 0x800:
            result.extend((0xc0 | (c >> 6), 0x80 | (c & 63)))
        elif c < 0xd800 or c > 0xdfff:
            result.extend((0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63)))
        else:
            next_unit = units[i] - 0xdc00 if i < len(units) else 0
            i += 1
            c = (_int32(((c - 0xd800) << 10) | next_unit) + 0x10000) & 0xffffffff
            result.extend(((0xf0 | (c >> 18)) & 255, 0x80 | ((c >> 12) & 63),
                           0x80 | ((c >> 6) & 63), 0x80 | (c & 63)))
    return bytes(result)


def chunk_id_for_piece(piece: bytes | str, *, hash_alg: str | None = 'xxhash64') -> str:
    """Derive an unencrypted ID using JS UTF-16 length and manager semantics.

    commonlib@484c164450d61deb2e301582b60cb9b5f9fa977f:
    managers/HashManager/XXHashHashManager.ts:39-134,
    managers/HashManager/PureJSHashManager.ts:34-44,77-87,
    managers/HashManager/HashManager.ts:11-19,65-74 (null uses fallback XXH32).
    """
    if isinstance(piece, bytes):
        piece = base64.b64encode(piece).decode('ascii')
    length = len(piece.encode('utf-16-le', errors='surrogatepass')) // 2
    text = f'{piece}-{length}'
    if hash_alg == 'xxhash64':
        digest = _base36(xxhash.xxh64_intdigest(_js_utf8(text)))
    elif hash_alg in ('xxhash32', None):
        digest = _base36(xxhash.xxh32_intdigest(_js_utf8(text)))
    elif hash_alg == '':
        digest = _base36(_int32(xxhash.xxh32_intdigest(_js_utf8(piece)) ^ length))
    elif hash_alg == 'mixed-purejs':
        digest = _mixed_hash(text)
    elif hash_alg == 'sha1':
        digest = base64.b64encode(hashlib.sha1(_write_string(text)).digest()).decode('ascii')
    else:
        raise UnsupportedWriterMode('unsupported hashAlg')
    return 'h:' + digest


def _js_truthy(value) -> bool:
    # Settings are JSON values; empty arrays/objects are truthy in JS.
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    return value != ''


def _splitter_property_key(value) -> str:
    # Only relevant for malformed JSON setting values: JS == / `in` coerce
    # arrays to comma-joined strings and plain objects to [object Object].
    if isinstance(value, list):
        return ','.join('' if item is None else _splitter_property_key(item) for item in value)
    if isinstance(value, dict):
        return '[object Object]'
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def normalize_splitter_settings(tweak_values: Mapping | None = None) -> dict:
    """Copy the one-pass SettingService.ts:141-158 splitter adjustment.

    Missing version retains this peer's legacy default. Empty version uses
    legacy flags; an unknown non-empty version becomes empty in the else-if
    branch and MUST NOT run migration again. Keep the upstream V2.5 spelling
    here so tweak compatibility can consume the same normalized values.
    """
    settings = dict(tweak_values or {})
    version = settings.get('chunkSplitterVersion')
    # Upstream uses == "", including false and numeric zero from JSON.
    key = _splitter_property_key(version)
    empty = ((isinstance(version, (str, list)) and key == '') or
             (isinstance(version, (int, float)) and version == 0))
    if empty:
        version = ''
        if _js_truthy(settings.get('enableChunkSplitterV2')):
            version = 'v2-segmenter' if _js_truthy(settings.get('useSegmenter')) else 'v2'
    elif key not in (
            'v1', 'v2', 'v2-segmenter', 'v3-rabin-karp',
            # ChunkAlgorithmNames is a JS object literal; `in` also sees
            # Object.prototype. These survive adjustment but fail dispatch.
            'constructor', '__defineGetter__', '__defineSetter__',
            'hasOwnProperty', '__lookupGetter__', '__lookupSetter__',
            'isPrototypeOf', 'propertyIsEnumerable', 'toString',
            'valueOf', '__proto__', 'toLocaleString'):
        version = ''
    settings['chunkSplitterVersion'] = version
    return settings


class ContentSplitter:
    """Configured upstream dispatch; str is a text/plain Blob, bytes binary.

    longLineThreshold is unused by all algorithms at the compatibility pin.
    """

    def __init__(self, tweak_values: Mapping | None = None, *, normalize_settings: bool = False):
        settings = dict(tweak_values or {})
        # Vault resolution runs SettingService normalization once; direct
        # dispatch still rejects unknown versions like ContentSplitters.ts.
        if normalize_settings or settings.get('chunkSplitterVersion', '') == '':
            settings = normalize_splitter_settings(settings)
        self.normalized_settings = dict(settings)
        # ContentSplitterBase.ts:71 passes minimumChunkSize without validation.
        self.minimum_chunk_size = settings.get('minimumChunkSize', 20)
        # ContentSplitterBase.ts:68: Math.floor(MAX_DOC_SIZE_BIN *
        # ((settings.customChunkSize || 0) * 1 + 1)); arrays/objects are truthy.
        custom = settings.get('customChunkSize', 0)
        custom = _minimum_number(custom if _js_truthy(custom) else 0)
        maximum = 102400 * (custom + 1)
        self.absolute_max_piece_size = math.floor(maximum) if math.isfinite(maximum) else maximum
        if settings.get('encrypt'):
            raise ValueError('encrypted chunk IDs are unsupported')
        unsupported = []
        setting_issues = []
        self.hash_alg = settings.get('hashAlg', 'xxhash64')
        if self.hash_alg is None:
            self.hash_alg = 'xxhash32'
        if self.hash_alg not in ('xxhash64', 'xxhash32', 'mixed-purejs', 'sha1', ''):
            unsupported.append(invalid_setting('hashAlg', self.hash_alg))
            setting_issues.append(SettingIssue.from_value('hashAlg', self.hash_alg))
        self.version = settings.get('chunkSplitterVersion', '')
        if self.version == 'v2-segmenter':
            self.version = 'v2'
        if self.version == '':
            self.version = 'v1'
        if self.version not in ('v1', 'v2', 'v2-segmenter', 'v3-rabin-karp'):
            unsupported.append(invalid_setting('chunkSplitterVersion', self.version))
            setting_issues.append(SettingIssue.from_value('chunkSplitterVersion', self.version))
        # configuredIdKey runs even without encryption (idDerivation.ts:53-67).
        version = settings.get('idDerivationVersion', 0)
        key = settings.get('idDerivationKey', '')
        if type(version) is not int or version not in (0, 1):
            unsupported.append(invalid_setting('idDerivationVersion', version))
            setting_issues.append(SettingIssue.from_value('idDerivationVersion', version))
        if (version == 0 and key != '') or (version == 1 and (
            not isinstance(key, str) or re.fullmatch('[0-9a-f]{64}', key) is None
        )):
            # Never expose the configured key in diagnostics or tool errors.
            unsupported.append(invalid_setting('idDerivationVersion', version))
            setting_issues.append(SettingIssue.from_value('idDerivationVersion', version))
            unsupported.append('idDerivationKey=<invalid configuration>')
            setting_issues.append(SettingIssue('idDerivationKey', 'configuration'))
        if unsupported:
            error = UnsupportedWriterMode(
                'vault uses ' + ', '.join(unsupported) +
                '; peer writes are not implemented for this mode yet — '
                'reads, search and replay work'
            )
            error.setting_keys = tuple(key for key in ('hashAlg', 'chunkSplitterVersion', 'idDerivationVersion', 'idDerivationKey')
                                       if any(item.startswith(key + '=') for item in unsupported))
            error.setting_issues = tuple(setting_issues)
            raise error

    def chunk_id_for_piece(self, piece: bytes | str) -> str:
        return chunk_id_for_piece(piece, hash_alg=self.hash_alg)

    def split(self, data: str | bytes, *, path: str = '', plain_split: bool = False,
              text_blob: bool = False) -> list[str]:
        # Raw bytes normally denote binary; text_blob exposes a text/plain Blob
        # without first decoding malformed input through a Python string.
        if text_blob and isinstance(data, bytes):
            data = _TextBlob(data)
        # ContentSplitterBase.ts:65-94; ContentSplitters.ts:8-30.
        algorithm = {'v1': LegacySplitter, 'v2': V2Splitter,
                     'v2-segmenter': V2Splitter, 'v3-rabin-karp': RabinKarpSplitter}[self.version]
        plain = plain_split or path.endswith(('.md', '.txt', '.canvas'))
        return algorithm(self).split(data, path=path, plain_split=plain)


def _minimum_number(value):
    """JS numeric coercion in chunks.ts:66,279,369,537 (not validation)."""
    if value is None:
        return 0.0
    if isinstance(value, (list, dict)):
        value = _splitter_property_key(value)
    try:
        if isinstance(value, str):
            # ECMAScript StringNumericLiteral, used by the comparisons and
            # Math.max in chunks.ts:66,279,369,537; float() alone accepts
            # Python's "inf" and rejects JavaScript's radix-prefixed forms.
            value = value.strip('\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003'
                                '\u2004\u2005\u2006\u2007\u2008\u2009\u200a'
                                '\u2028\u2029\u202f\u205f\u3000\ufeff')
            if not value:
                return 0.0
            if re.fullmatch(r'0(?:[xX][0-9a-fA-F]+|[bB][01]+|[oO][0-7]+)', value):
                return float(int(value, 0))
            if not re.fullmatch(r'[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|Infinity)', value):
                return math.nan
        return float(value)
    except OverflowError:
        return -math.inf if str(value).startswith('-') else math.inf
    except (ValueError, TypeError):
        return math.nan


def _loop_refusal(setting, remedy):
    error = ValueError(
        f'{setting}: the plugin itself does not terminate for this input; use {remedy}')
    error.setting_keys = (setting,)
    return error


class _Splitter:
    def __init__(self, config: ContentSplitter):
        self.absolute_max_piece_size = config.absolute_max_piece_size
        self.minimum_chunk_size = config.minimum_chunk_size


class RabinKarpSplitter(_Splitter):
    def split(self, data: str | bytes, *, path: str = '', plain_split: bool = False) -> list[str]:
        """Split bytes before encoding; preserve UTF-8 boundaries for text Blobs.

        A binary .md/.txt/.canvas path uses text thresholds but still emits
        base64. A text Blob always uses text thresholds below 4 MiB.
        """
        is_text = isinstance(data, (str, _TextBlob))
        raw = _js_utf8(data) if isinstance(data, str) else data
        if not raw:
            return []
        plain = is_text or plain_split
        unit = 64
        if plain:
            if len(raw) >= 4 * 1024 * 1024:
                plain = False
            else:
                while len(raw) / (unit * 4) > 500:
                    unit += 32
        maximum = min(max(self.absolute_max_piece_size, 30 * 1024),
                      unit * 16 if plain else 4 * 1024 * 1024)
        gap = max(maximum // 4, 1)
        # chunks.ts:535-545: Math.min/max propagate NaN; place the setting
        # first so Python min/max preserve it rather than discarding it.
        minimum = min(max(_minimum_number(self.minimum_chunk_size), unit * 2 if plain else gap), maximum)
        modulus = min(max(minimum, unit * 4), maximum) if plain else gap
        weight = pow(31, 47, 2**32)
        fingerprint = 0
        start = 0
        pieces = []
        # Unsigned modular arithmetic gives the same bit pattern as Math.imul
        # and signed |0; upstream converts to unsigned before taking modulus.
        for index, byte in enumerate(raw):
            size = index - start
            candidate = size >= maximum or (size >= minimum and fingerprint % modulus == 1)
            if candidate and (not is_text or byte & 0xc0 != 0x80):
                pieces.append(raw[start:index])
                start = index
            old = raw[index - 48] if index >= 48 else 0
            fingerprint = ((fingerprint - old * weight) * 31 + byte) & 0xffffffff
        pieces.append(raw[start:])
        if is_text:
            # V3 uses readString, unlike the legacy Blob.text() path.
            return [_wheels_read_string(piece) for piece in pieces]
        return [base64.b64encode(piece).decode('ascii') for piece in pieces]


def _unit_string(text: str) -> str:
    # Python indices become JS UTF-16 code-unit indices, without a list of ints.
    def pair(match):
        value = ord(match[0]) - 0x10000
        return chr(0xd800 + (value >> 10)) + chr(0xdc00 + (value & 1023))
    return re.sub('[\U00010000-\U0010ffff]', pair, text)


def _from_units(text: str) -> str:
    return text.encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='surrogatepass')


def _capped(text: str, maximum: int, preserve_pairs: bool = True, *, v1_plain=False):
    # chunks.ts:218-254,296-323. V1 non-plain intentionally splits pairs.
    maximum = min(maximum, len(text))  # substring(+Infinity) clamps to length.
    start = 0
    while True:
        # chunks.ts:234-240: charCodeAt(-1)=NaN != undefined, so V1
        # increments a zero cap to one, intentionally cutting surrogate pairs.
        if v1_plain and maximum == 0:
            end = start + 1
        else:
            end = start + maximum
        if (preserve_pairs and maximum > 0 and end < len(text) and 0xd800 <= ord(text[end - 1]) <= 0xdbff
                and 0xdc00 <= ord(text[end]) <= 0xdfff):
            end += 1
        yield _from_units(text[start:end])
        start = end
        if start >= len(text):
            break


def _binary_minimum(size: int, path: str) -> int:
    # chunks.ts:384-396,455-467; independent of minimumChunkSize setting.
    w = max(100 if path.endswith('.json') else 100000, min(100000000, size))
    step = 1
    while w > 10:
        w /= 12.5
        step += 1
    return math.floor(10 ** (step - 1))


class V2Splitter(_Splitter):
    """chunks.ts:256-434. useSegmenter is never read (V2.5 is identical)."""

    def split(self, data, *, path='', plain_split=False):
        maximum = self.absolute_max_piece_size
        if isinstance(data, bytes) and not isinstance(data, _TextBlob):
            return self._binary(data, path)
        raw = _js_utf8(data) if isinstance(data, str) else data
        if not raw:
            return []
        text = _unit_string(raw.decode('utf-8-sig', errors='replace'))
        if not plain_split:
            return self._cap(text)
        minimum = _minimum_number(self.minimum_chunk_size)
        # chunks.ts:367-371: only numeric zero (including null/empty string)
        # cannot advance xMinimumChunkSize. Negative zero divides to -Infinity;
        # negative/NaN minima also skip this loop.
        if text and minimum == 0 and math.copysign(1, minimum) > 0:
            raise _loop_refusal('minimumChunkSize', 'minimumChunkSize=20 (or another positive minimumChunkSize)')
        if minimum > 0 and not isinstance(self.minimum_chunk_size, str) and minimum % 1 == 0:
            # Exact fast path for the integer UI setting; O(1).
            adjusted = max(1, math.ceil(len(text) / (100 * minimum))) * minimum
        else:
            # Preserve JS binary64 repeated-addition rounding for fractional
            # tweaks too; multiplication can change strict newline boundaries.
            adjusted = minimum
            source = self.minimum_chunk_size
            while adjusted > 0 and len(text) / adjusted > 100:
                # JS += concatenates when the setting is a string.
                source = source + self.minimum_chunk_size if isinstance(source, str) else source + minimum
                adjusted = _minimum_number(source)
        pieces = []
        start = 0
        for match in re.finditer('\n', text):
            end = match.end()
            if end - start > adjusted:
                pieces.extend(self._cap(text[start:end]))
                start = end
        # splitByDelimiterWithMinLength yields even an empty final tail.
        pieces.extend(self._cap(text[start:]))
        return pieces

    def _cap(self, text):
        # chunks.ts:297-313: nonpositive maxLength grows end to 1 once,
        # then stalls at the first code point boundary. A single code point
        # (including a surrogate pair) still terminates.
        if self.absolute_max_piece_size <= 0:
            if len(_from_units(text)) > 1 or (not text and self.absolute_max_piece_size < 0):
                # chunks.ts:299-309: a negative cap on an empty source (BOM
                # or flushed newline tail) grows end forever, not just once.
                raise _loop_refusal('customChunkSize', 'customChunkSize=0 (positive pieceSize)')
            return [_from_units(text)]
        # chunks.ts:297: strLen > NaN is false; yield the whole source.
        if math.isnan(self.absolute_max_piece_size):
            return [_from_units(text)]
        return list(_capped(text, self.absolute_max_piece_size))

    def _binary(self, data: bytes, path: str):
        if not data:
            return []
        if math.isnan(self.absolute_max_piece_size):
            # chunks.ts:418-432: slice(i, NaN) emits nothing and loses bytes.
            raise ValueError("customChunkSize would discard binary data; use customChunkSize=0 or another numeric value")
        if self.absolute_max_piece_size <= 0:
            # chunks.ts:418-432: i = splitEnd <= i.
            raise _loop_refusal('customChunkSize', 'customChunkSize=0 (positive pieceSize)')
        delimiter = b'/' if path.endswith('.pdf') else b',' if path.endswith('.json') else b'\0'
        minimum = _binary_minimum(len(data), path)
        maximum = min(self.absolute_max_piece_size, len(data))
        pieces = []
        offset = 0
        # Cache next search positions: upstream searches the entire suffix each
        # time, which is quadratic for a multi-MiB buffer lacking delimiters.
        preferred = data.find(delimiter, minimum)
        newline = data.find(b'\n', minimum)
        while offset < len(data):
            search = offset + minimum
            if preferred != -1 and preferred < search:
                preferred = data.find(delimiter, search)
            found = preferred
            if found == -1:
                if newline != -1 and newline < search:
                    newline = data.find(b'\n', search)
                found = newline
            end = min(offset + maximum, found) if found != -1 else offset + maximum
            pieces.append(base64.b64encode(data[offset:end]).decode('ascii'))
            offset = end
        return pieces


class LegacySplitter(_Splitter):
    """chunks.ts:13-72,218-254,436-493 (including historical fence loop)."""

    def split(self, data, *, path='', plain_split=False):
        maximum = self.absolute_max_piece_size
        if isinstance(data, bytes) and not isinstance(data, _TextBlob):
            if data and (maximum <= 0 or math.isnan(maximum)):
                # chunks.ts:473-491: i += piece.length stays zero.
                raise _loop_refusal('customChunkSize', 'customChunkSize=0 (positive pieceSize)')
            if not data:
                return ['']
            delimiter = b'/' if path.endswith('.pdf') else b',' if path.endswith('.json') else b'\0'
            minimum = _binary_minimum(len(data), path)
            pieces = []
            offset = 0
            while True:
                end = min(offset + maximum, len(data))
                found = data.find(delimiter, offset + minimum, end)
                if found != -1:
                    end = found
                # LF fallback does not update splitSize in upstream.
                pieces.append(base64.b64encode(data[offset:end]).decode('ascii'))
                offset = end
                if offset >= len(data):
                    return pieces
        raw = _js_utf8(data) if isinstance(data, str) else data
        text = _unit_string(raw.decode('utf-8-sig', errors='replace'))
        # chunks.ts:234-250: substring clamps negative offsets to zero.
        if text and (math.isnan(maximum) or maximum < 0 or (not plain_split and maximum == 0)):
            raise _loop_refusal('customChunkSize',
                                'customChunkSize=0 (zero pieceSize also works for V1 plain text)')
        if not text:
            return ['']
        if not plain_split:
            return list(_capped(text, maximum, False))
        return [piece for group in self._groups(text) for piece in _capped(group, maximum, v1_plain=True)]

    def _groups(self, text):
        lines = text.split('\n')
        count = len(lines)
        index = 0
        buffer = []
        length = 0

        def fence(line):
            return line.startswith(('```', ' ```', '  ```', '   ```'))

        def take():
            nonlocal index
            line = lines[index]
            index += 1
            return line + ('\n' if index < count else '')

        while index < count:
            if fence(lines[index]):
                yield ''.join(buffer)
                buffer = [take()]
                # Deliberately consume one line before checking the NEXT fence.
                while index < count:
                    buffer.append(take())
                    if index == count or fence(lines[index]):
                        break
                pre = ''.join(buffer)
                unsplit = pre.endswith('=') or len(pre) > 2048
                if index < count:
                    buffer.append(take())
                code = ''.join(buffer)
                if unsplit:
                    yield code
                else:
                    # JS dot excludes CR, LF, U+2028 and U+2029.
                    yield from (part for part in re.split(r'([^\r\n\u2028\u2029]*?[;,:<])', code) if part)
                buffer = []
                length = 0
            else:
                part = take()
                buffer.append(part)
                length += len(part)
                if (length >= _minimum_number(self.minimum_chunk_size) or index == count
                        or lines[index] == '#' or buffer[0].startswith('#')):
                    yield ''.join(buffer)
                    buffer = []
                    length = 0
