"""LiveSync v3 Rabin–Karp splitting and unencrypted xxhash64 chunk identities.

Source: vrtmrz/livesync-commonlib@484c164450d61deb2e301582b60cb9b5f9fa977f
src/string_and_binary/chunks.ts:494-650 (rolling state and thresholds),
src/ContentSplitter/ContentSplitterBase.ts:65-94 (settings and path hint),
src/managers/HashManager/XXHashHashManager.ts:73-99 (hash input/base36).
See docs/superpowers/specs/2026-11-23-settings-parity.md sections а/б.
"""
from __future__ import annotations

import base64
import math
import re
from collections.abc import Mapping

import xxhash


def _js_utf8(text: str) -> bytes:
    # Blob/TextEncoder replace lone JS surrogates with U+FFFD and combine pairs.
    return text.encode('utf-16-le', errors='surrogatepass').decode(
        'utf-16-le', errors='replace').encode('utf-8')


def chunk_id_for_piece(piece: bytes | str) -> str:
    """Hash an emitted string; raw binary pieces are first base64-encoded.

    HashManager uses JS string.length (UTF-16 units), not UTF-8 byte length.
    Default xxhash64 seed is zero, with unsigned, unpadded lowercase base36.
    """
    if isinstance(piece, bytes):
        piece = base64.b64encode(piece).decode('ascii')
    length = len(piece.encode('utf-16-le', errors='surrogatepass')) // 2
    value = xxhash.xxh64_intdigest(_js_utf8(f'{piece}-{length}'))
    digits = ''
    while value:
        value, remainder = divmod(value, 36)
        digits = '0123456789abcdefghijklmnopqrstuvwxyz'[remainder] + digits
    return 'h:' + (digits or '0')


class ContentSplitter:
    """The supported v3/xxhash64 writer, configured with resolved tweak values.

    Explicit legacy splitter/hash modes are refused rather than approximated.
    longLineThreshold is deliberately unused by upstream v3. Empty input emits
    no pieces. str models a text/plain Blob; bytes models a binary Blob.
    """

    def __init__(self, tweak_values: Mapping | None = None):
        settings = dict(tweak_values or {})
        if settings.get('chunkSplitterVersion', 'v3-rabin-karp') != 'v3-rabin-karp':
            raise ValueError('unsupported chunkSplitterVersion; v3-rabin-karp required')
        if settings.get('hashAlg', 'xxhash64') != 'xxhash64':
            raise ValueError('unsupported hashAlg; xxhash64 required')
        if settings.get('encrypt'):
            raise ValueError('encrypted chunk IDs are unsupported')
        # configuredIdKey runs even without encryption (idDerivation.ts:53-67).
        version = settings.get('idDerivationVersion', 0)
        key = settings.get('idDerivationKey', '')
        if type(version) is not int or version not in (0, 1):
            raise ValueError('invalid idDerivationVersion')
        if (version == 0 and key != '') or (version == 1 and (
            not isinstance(key, str) or re.fullmatch('[0-9a-f]{64}', key) is None
        )):
            raise ValueError('invalid idDerivationKey configuration')
        self.minimum_chunk_size = self._number(settings, 'minimumChunkSize', 20)
        custom = self._number(settings, 'customChunkSize', 0, allow_negative=True)
        self.absolute_max_piece_size = math.floor(102400 * (custom + 1))

    @staticmethod
    def _number(settings: dict, key: str, default: int, *, allow_negative=False) -> float:
        value = settings.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or (value < 0 and not allow_negative)):
            raise ValueError(f'invalid {key}')
        return value

    chunk_id_for_piece = staticmethod(chunk_id_for_piece)

    def split(self, data: str | bytes, *, path: str = '', plain_split: bool = False) -> list[str]:
        """Split bytes before encoding; preserve UTF-8 boundaries for text Blobs.

        A binary .md/.txt/.canvas path uses text thresholds but still emits
        base64. A text Blob always uses text thresholds below 4 MiB.
        """
        is_text = isinstance(data, str)
        raw = _js_utf8(data) if is_text else data
        if not raw:
            return []
        plain = is_text or plain_split or path.endswith(('.md', '.txt', '.canvas'))
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
