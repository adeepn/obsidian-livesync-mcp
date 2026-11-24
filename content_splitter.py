"""LiveSync content splitting and unencrypted chunk identities.

Source: vrtmrz/livesync-commonlib@484c164450d61deb2e301582b60cb9b5f9fa977f
src/string_and_binary/chunks.ts:13-72,218-493 (V1/V2),
:494-650 (Rabin-Karp rolling state and thresholds),
src/ContentSplitter/ContentSplitterBase.ts:65-94 (settings and path hint),
src/managers/HashManager/XXHashHashManager.ts:73-99 (hash input/base36).
See docs/superpowers/specs/2026-11-23-settings-parity.md sections а/б.
"""
from __future__ import annotations

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


class ContentSplitter:
    """Configured upstream dispatch; str is a text/plain Blob, bytes binary.

    longLineThreshold is unused by all algorithms at the compatibility pin.
    """

    def __init__(self, tweak_values: Mapping | None = None):
        settings = dict(tweak_values or {})
        self.minimum_chunk_size = self._number(settings, 'minimumChunkSize', 20)
        custom = self._number(settings, 'customChunkSize', 0, allow_negative=True)
        try:
            self.absolute_max_piece_size = math.floor(102400 * (custom + 1))
        except (OverflowError, ValueError) as exc:
            raise ValueError('invalid customChunkSize') from exc
        if settings.get('encrypt'):
            raise ValueError('encrypted chunk IDs are unsupported')
        unsupported = []
        self.hash_alg = settings.get('hashAlg', 'xxhash64')
        if self.hash_alg is None:
            self.hash_alg = 'xxhash32'
        if self.hash_alg not in ('xxhash64', 'xxhash32', 'mixed-purejs', 'sha1', ''):
            unsupported.append(f'hashAlg={self.hash_alg}')
        self.version = settings.get('chunkSplitterVersion', '')
        if self.version == 'v2-segmenter':
            self.version = 'v2'
        if self.version == '':
            self.version = 'v1'
        if self.version not in ('v1', 'v2', 'v2-segmenter', 'v3-rabin-karp'):
            unsupported.append(f'chunkSplitterVersion={self.version}')
        # configuredIdKey runs even without encryption (idDerivation.ts:53-67).
        version = settings.get('idDerivationVersion', 0)
        key = settings.get('idDerivationKey', '')
        if type(version) is not int or version not in (0, 1):
            unsupported.append(f'idDerivationVersion={version}')
        if (version == 0 and key != '') or (version == 1 and (
            not isinstance(key, str) or re.fullmatch('[0-9a-f]{64}', key) is None
        )):
            # Never expose the configured key in diagnostics or tool errors.
            unsupported.append(f'idDerivationVersion={version}')
            unsupported.append('idDerivationKey=<invalid configuration>')
        if unsupported:
            raise UnsupportedWriterMode(
                'vault uses ' + ', '.join(unsupported) +
                '; peer writes are not implemented for this mode yet — '
                'reads, search and replay work'
            )

    @staticmethod
    def _number(settings: dict, key: str, default: int, *, allow_negative=False) -> float:
        value = settings.get(key, default)
        try:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and (value >= 0 or allow_negative))
        except (OverflowError, ValueError) as exc:
            raise ValueError(f'invalid {key}') from exc
        if not valid:
            raise ValueError(f'invalid {key}')
        return value

    def chunk_id_for_piece(self, piece: bytes | str) -> str:
        return chunk_id_for_piece(piece, hash_alg=self.hash_alg)

    def split(self, data: str | bytes, *, path: str = '', plain_split: bool = False) -> list[str]:
        # ContentSplitterBase.ts:65-94; ContentSplitters.ts:8-30.
        algorithm = {'v1': LegacySplitter, 'v2': V2Splitter,
                     'v2-segmenter': V2Splitter, 'v3-rabin-karp': RabinKarpSplitter}[self.version]
        plain = plain_split or path.endswith(('.md', '.txt', '.canvas'))
        return algorithm(self).split(data, path=path, plain_split=plain)


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
        is_text = isinstance(data, str)
        raw = _js_utf8(data) if is_text else data
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
        maximum = min(unit * 16 if plain else 4 * 1024 * 1024,
                      max(self.absolute_max_piece_size, 30 * 1024))
        gap = max(1, maximum // 4)
        minimum = min(max(unit * 2 if plain else gap, self.minimum_chunk_size), maximum)
        modulus = min(max(unit * 4, minimum), maximum) if plain else gap
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
            # TextDecoder removes a leading BOM on each separately read piece.
            return [piece.decode('utf-8-sig', errors='replace') for piece in pieces]
        return [base64.b64encode(piece).decode('ascii') for piece in pieces]


def _unit_string(text: str) -> str:
    # Python indices become JS UTF-16 code-unit indices, without a list of ints.
    def pair(match):
        value = ord(match[0]) - 0x10000
        return chr(0xd800 + (value >> 10)) + chr(0xdc00 + (value & 1023))
    return re.sub('[\U00010000-\U0010ffff]', pair, text)


def _from_units(text: str) -> str:
    return text.encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='surrogatepass')


def _capped(text: str, maximum: int, preserve_pairs: bool = True):
    # chunks.ts:218-254,296-323. V1 non-plain intentionally splits pairs.
    start = 0
    while True:
        end = start + maximum
        if (preserve_pairs and end < len(text) and 0xd800 <= ord(text[end - 1]) <= 0xdbff
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
        if maximum <= 0:
            raise ValueError('customChunkSize must produce a positive piece size')
        if isinstance(data, bytes):
            return self._binary(data, path)
        raw = _js_utf8(data)
        if not raw:
            return []
        text = _unit_string(raw.decode('utf-8-sig', errors='replace'))
        if not plain_split:
            return list(_capped(text, maximum))
        minimum = self.minimum_chunk_size
        if minimum <= 0:
            raise ValueError('minimumChunkSize must be positive for plain V2 text')
        if minimum % 1 == 0:
            # Exact fast path for the integer UI setting; O(1).
            adjusted = max(1, math.ceil(len(text) / (100 * minimum))) * minimum
        else:
            # Preserve JS binary64 repeated-addition rounding for fractional
            # tweaks too; multiplication can change strict newline boundaries.
            adjusted = minimum
            while len(text) / adjusted > 100:
                adjusted += minimum
        pieces = []
        start = 0
        for match in re.finditer('\n', text):
            end = match.end()
            if end - start > adjusted:
                pieces.extend(_capped(text[start:end], maximum))
                start = end
        # splitByDelimiterWithMinLength yields even an empty final tail.
        pieces.extend(_capped(text[start:], maximum))
        return pieces

    def _binary(self, data: bytes, path: str):
        if not data:
            return []
        delimiter = b'/' if path.endswith('.pdf') else b',' if path.endswith('.json') else b'\0'
        minimum = _binary_minimum(len(data), path)
        maximum = self.absolute_max_piece_size
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
        if maximum <= 0:
            raise ValueError('customChunkSize must produce a positive piece size')
        if isinstance(data, bytes):
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
        text = _unit_string(_js_utf8(data).decode('utf-8-sig', errors='replace'))
        if not plain_split:
            return list(_capped(text, maximum, False))
        return [piece for group in self._groups(text) for piece in _capped(group, maximum)]

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
                if (length >= self.minimum_chunk_size or index == count
                        or lines[index] == '#' or buffer[0].startswith('#')):
                    yield ''.join(buffer)
                    buffer = []
                    length = 0
