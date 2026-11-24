"""Deterministic inputs shared by offline tests and the Node oracle generator.

No production imports: this module defines inputs, never expected partitions.
"""
from itertools import product
import base64
import json
import random
from functools import lru_cache

# Named levels remain distinct even when omission equals an explicit default.
BACKENDS = ('couchdb', 'journal')
FACTORS = (
    ('identity', ('legacy', 'nfc', 'sensitive', 'nfc-sensitive')),
    ('splitter', ('v3-rabin-karp', 'v2', 'v2-segmenter', 'v1',
                 'legacy-00', 'legacy-01', 'legacy-10', 'legacy-11')),
    ('hash', ('xxhash64', 'xxhash32', 'mixed-purejs', 'sha1', '')),
    ('custom', ('default', 0, 1, 60)),
    ('minimum', ('default', 5, 100)),
    ('limit', ('default', 0, 1)),
    ('document', ('md', 'bom', 'base', 'json', 'yaml', 'binary', 'empty')),
)
ROWS = (
    (3, 7, 4, 3, 2, 0, 2),
    (3, 6, 2, 2, 1, 2, 4),
    (3, 5, 0, 1, 0, 1, 3),
    (2, 4, 3, 3, 1, 1, 1),
    (2, 0, 0, 0, 2, 2, 0),
    (1, 4, 2, 2, 0, 0, 5),
    (1, 5, 1, 0, 1, 0, 6),
    (0, 3, 1, 1, 0, 2, 1),
    (0, 1, 2, 2, 2, 1, 6),
    (2, 6, 3, 1, 0, 0, 4),
    (1, 7, 4, 1, 1, 1, 0),
    (1, 2, 3, 1, 2, 2, 5),
    (0, 2, 4, 0, 0, 0, 2),
    (1, 1, 0, 3, 1, 2, 2),
    (2, 3, 4, 2, 2, 2, 3),
    (0, 6, 1, 3, 2, 1, 4),
    (3, 1, 3, 0, 0, 1, 0),
    (2, 0, 2, 3, 0, 2, 6),
    (0, 3, 0, 0, 1, 1, 5),
    (1, 0, 1, 2, 1, 1, 2),
    (3, 2, 1, 0, 1, 1, 3),
    (1, 5, 3, 2, 2, 0, 1),
    (0, 7, 2, 0, 0, 2, 1),
    (0, 4, 3, 0, 2, 0, 3),
    (2, 5, 2, 3, 1, 2, 5),
    (0, 3, 2, 3, 2, 0, 0),
    (2, 4, 1, 1, 0, 2, 2),
    (3, 0, 0, 1, 0, 0, 6),
    (3, 1, 4, 1, 0, 0, 1),
    (2, 2, 0, 2, 2, 2, 4),
    (1, 6, 4, 3, 1, 2, 3),
    (0, 5, 4, 0, 0, 2, 4),
    (3, 7, 3, 2, 0, 1, 6),
    (3, 7, 1, 3, 2, 0, 5),
    (3, 4, 0, 2, 2, 2, 0),
    (2, 6, 0, 0, 0, 0, 1),
    (3, 3, 2, 1, 2, 1, 2),
    (0, 0, 4, 0, 2, 1, 5),
    (1, 3, 3, 2, 1, 1, 4),
    (3, 2, 2, 3, 2, 1, 3),
    (2, 7, 0, 2, 0, 2, 4),
    (2, 4, 4, 3, 2, 1, 6),
    (2, 1, 1, 1, 0, 2, 4),
    (3, 6, 3, 1, 2, 1, 2),
    (3, 5, 1, 2, 2, 2, 0),
    (3, 0, 3, 3, 0, 1, 1),
    (3, 7, 4, 1, 1, 0, 3),
    (3, 6, 4, 2, 1, 0, 5),
    (3, 6, 4, 0, 2, 1, 0),
    (3, 6, 4, 0, 2, 1, 6),
    (3, 5, 1, 2, 1, 1, 2),
    (3, 4, 4, 1, 0, 2, 4),
    (3, 3, 4, 2, 0, 2, 6),
    (3, 2, 4, 3, 1, 1, 1),
    (3, 2, 4, 3, 2, 0, 0),
    (3, 2, 4, 3, 1, 0, 6),
    (3, 1, 4, 3, 1, 2, 5),
    (3, 1, 4, 3, 0, 0, 3),
    (3, 0, 4, 3, 0, 2, 4),
    (3, 0, 4, 3, 2, 1, 3),
)
CASES = [tuple(levels[index] for (_, levels), index in zip(FACTORS, row)) for row in ROWS]

BRANCH_CASES = [('legacy', v, 'xxhash64', 0, 5, 'default', 'fenced')
                for v in ('v1', 'v2', 'v3-rabin-karp')] + [
                    ('legacy', 'v3-rabin-karp', 'xxhash64', 0, 5, 'default', 'upper'),
                    ('legacy', 'v1', 'xxhash64', 0, 5, 'default', 'fenced4'),
                    ('legacy', 'v2', 'xxhash64', 0, 5, 'default', 'boundary:102401:png:none:0'),
                    ('legacy', 'v2', 'xxhash64', 0, 5, 'default', 'pdf-far-lf'),
                    ('legacy', 'v2', 'xxhash64', 0, 5, 'default', 'json-clamp'),
                    ('legacy', 'v3-rabin-karp', 'xxhash64', 0, 102400, 'default', 'override')]


def case_id(case):
    return '-'.join(f'{name}={"empty" if value == "" else value}' for (name, _), value in zip(FACTORS, case))


def settings_for(case):
    identity, version, alg, custom, minimum, limit, _ = case
    settings = {'pathNormalizationVersion': int('nfc' in identity),
                'handleFilenameCaseSensitive': 'sensitive' in identity,
                'hashAlg': alg}
    if version.startswith('legacy-'):
        v2, segmenter = version[-2:]
        settings.update(chunkSplitterVersion='', enableChunkSplitterV2=v2 == '1',
                        useSegmenter=segmenter == '1')
    else:
        settings['chunkSplitterVersion'] = version
    for key, value in [('customChunkSize', custom), ('minimumChunkSize', minimum),
                       ('syncMaxSizeInMB', limit)]:
        if value != 'default':
            settings[key] = value
    return settings


def payload(kind):
    if kind in ('pdf-far-lf', 'json-clamp', 'override') or kind.startswith('boundary:'):
        return grid_payload(kind)
    if kind == 'fenced4':
        return grid_payload('fence-4-js')
    if kind == 'fenced':
        return grid_payload('fence-0-js')
    if kind == 'upper':
        return '.MD', b'x' * 5000
    if kind == 'binary':
        return '.png', b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 1024
    if kind == 'empty':
        return '.md', ''
    text = 'Привет🙂\nhelloWorld\n' * 3
    if kind == 'bom':
        text = '\ufeff' + text
    return ('.md' if kind in ('md', 'bom') else '.' + kind), text



GRID_KINDS = ('text-short', 'text-long', 'bom-text', 'crlf', 'unicode',
              'binary-small', 'binary-large', 'empty')


def _base_grid_payload(kind):
    if kind == 'text-short':
        return '.md', 'helloWorld123456789\nshort text\n'
    if kind == 'text-long':
        return '.md', 'x' * 230001 + '\n' + 'line abc;def\n' * 100
    if kind == 'bom-text':
        return '.md', '\ufeff' + 'BOM Привет🙂\n' * 30
    if kind == 'crlf':
        return '.md', 'a\r\nb;cd\r\n' * 100
    if kind == 'unicode':
        return '.md', 'Привет🙂 мир🚀\n' * 100
    if kind == 'binary-small':
        return '.png', bytes(range(256))
    if kind == 'binary-large':
        return '.png', bytes(range(256)) * 900
    return '.md', ''


# Reduced branch grid: fence grammar and path hints cross every dispatch mode;
# hash semantics cross non-ASCII classes independently of size factors.
FENCE_KINDS = tuple(f'fence-{n}-{lang}' for n in range(5) for lang in ('js', 'none')) + (
    'fence-open', 'fence-equals', 'fence-long', 'fence-headings', 'starts-heading')
HINT_KINDS = tuple('hint-' + ext for ext in ('MD', 'Canvas', 'TXT', 'json', 'bare', 'md', 'txt', 'canvas'))
BASE_GRID_KINDS = GRID_KINDS
GRID_KINDS += FENCE_KINDS + HINT_KINDS + ('cyrillic-emoji', 'binary-empty', 'binary-nul', 'json-clamp', 'pdf-far-lf')

def _grouping_input(seed, variant):
    char = chr(97 + seed)
    inputs = [char + '\n\n' + char * 2, char + '\n#\n' + char * 2,
              char + '\n##\n' + char * 2, char + '\nend',
              '#' + char + '\nend', char + '#\nend']
    inputs += [' ' * n + '`' * ticks + '\n' + char + ',;:<' + char + '\n' + ' ' * n + '`' * ticks + '\ntail'
               for n, ticks in product(range(5), (2, 3, 4))]
    inputs += ['```\n' + char + ',;:<' + char + suffix for suffix in ('', '=', '=x')]
    inputs += [char + '\nend' + suffix for suffix in ('', '\n', '\n\n')]
    return inputs[variant]


@lru_cache(maxsize=3)
def _candidate_patch(seed):
    # Input construction only: choose a 48-byte window whose unsigned base-31
    # polynomial is 1 modulo the default binary gap (25600). Expected pieces
    # and IDs still come exclusively from the actual pinned upstream splitter.
    state = 0
    for _ in range(45):
        state = (state * 31 + 97 + seed) & 0xffffffff
    for first in range(256):
        for second in range(256):
            base = (((state * 31 + first) * 31 + second) * 31) & 0xffffffff
            last = (1 - base) % 25600
            if last < 256 and ((base + last) & 0xffffffff) % 25600 == 1:
                return bytes((first, second, last))
    raise AssertionError('cannot construct binary candidate input')


@lru_cache(maxsize=8)
def grid_payload(kind):
    if kind.startswith('decoder:'):
        _, size, byte, position = kind.split(':')
        size, position = int(size), int(position)
        raw = bytearray(b'a' * size)
        patch = bytes.fromhex(byte)
        raw[position:position + len(patch)] = patch
        return '.md', bytes(raw)
    if kind.startswith('fractional:'):
        _, size, newline = kind.split(':')
        size, newline = int(size), int(newline)
        return '.md', 'a' * (newline - 1) + '\n' + 'a' * (size - newline)
    if kind == 'fence-only':
        return '.md', '```'
    if kind == 'encoder-exact128':
        return '.MD', 'a' * 123 + '🙂z'
    if kind.startswith('eq:'):
        _, mode, size, seed, position = kind.split(':')
        size, seed, position = int(size), int(seed), int(position)
        char = chr(97 + seed)
        if mode == 'grouping':
            return '.md', _grouping_input(seed, position)
        if mode == 'candidate-binary':
            data = bytearray(char.encode() * size)
            data[position - 3:position] = _candidate_patch(seed)
            return '.png', bytes(data)
        if mode.startswith('lf-'):
            data = bytearray(char.encode() * size)
            data[position] = 10
            return '.' + mode[3:], bytes(data)
        if mode.startswith('hint-'):
            _, ext, variant, content_kind = mode.split('-')
            ext = (ext[:-1] if variant == 'minus' else ext + 'x' if variant == 'plus'
                   else ext.upper() if variant == 'upper' else ext)
            content = (char.encode() * size if content_kind == 'binary' else
                       'before\n```js\n' + char + ',' + char + ';' + char + '\n```\nafter\n')
            return '.' + ext, content
        if mode in ('json', 'png', 'pdf'):
            data = bytearray(char.encode() * size)
            if size >= 3000000:
                data[99999::100000] = bytes([{'json': 44, 'png': 0, 'pdf': 47}[mode]]) * len(data[99999::100000])
            if 0 <= position < size:
                data[position] = {'json': 44, 'png': 0, 'pdf': 47}[mode]
            return '.' + mode, bytes(data)
        if mode == 'fence':
            body = char * (size - 4)
            middle = len(body) // 2
            return '.md', '```\n' + body[:middle] + ',' + body[middle + 1:] + '\n```\ntail'
        if mode == 'line':
            return '.md', char * (position - 1) + '\n' + char * (size - position)
        if mode in ('pair', 'pair-low', 'pair-high'):
            scalar = {'pair': '🙂', 'pair-low': '\U00010000', 'pair-high': '\U0010ffff'}[mode]
            return '.md', char * position + scalar + char * (size - position)
        return ('.MD' if mode == 'upper' else '.md'), char * size
    if kind.startswith("random:"):
        _, alphabet, seed, length = kind.split(":")
        rng = random.Random(int(seed))
        if alphabet == "binary":
            return ".png", bytes(rng.randrange(256) for _ in range(int(length)))
        if alphabet == "ascii":
            return ".md", "".join(chr(rng.randrange(32, 127)) for _ in range(int(length)))
        chars = {"mixed": "abcАБВпривет🙂🚀\n",
                 "newline": "abcde\n\n\r\n",
                 "punctuation": "ab,;:<.!?=\n"}[alphabet]
        return ".md", "".join(rng.choice(chars) for _ in range(int(length)))

    if kind == 'z1':
        return '.json', b'x' * 50 + b',' + b'y' * 74
    if kind.startswith('utf8:'):
        _, width, offset = kind.split(':')
        return '.md', 'a' * int(offset) + {2: 'é', 3: '€', 4: '🙂'}[int(width)] + 'z'
    if kind.startswith('hint-path:'):
        return '.md', 'before\n```js\na,b,c;d<e\n```\nafter\n'
    if kind.startswith('surrogate:'):
        return '.md', 'a' * int(kind.split(':')[1]) + '🙂' + 'z'
    if kind.startswith('large-text:'):
        return '.md', 'x' * int(kind.split(':')[1])
    if kind.startswith('fence-'):
        if kind.split('-')[1].isdigit():
            _, spaces, lang = kind.split('-')
            fence = ' ' * int(spaces) + '```'
            return '.md', 'before\n' + fence + (lang if lang != 'none' else '') + '\na,b,c;d:e<f\nlonger,punctuation;at:other<offsets\n' + fence + '\nafter\n'
        bodies = {
            'fence-open': 'before\n```js\na,b;c:d<e',
            'fence-equals': 'before\n```js\na,b;c:d<e=',
            'fence-long': 'before\n```\n' + '🙂,;:<' * 500 + '\n```\nafter',
            'fence-headings': '#\n# heading\n```js\na,b,c;d<e\n```\n#\n# heading\nend',
        }
        return '.md', bodies[kind]
    if kind == 'starts-heading':
        return '.md', '#heading\nshort\n#\n# heading\ntail'
    if kind.startswith('hint-'):
        ext = kind[5:]
        return ('' if ext == 'bare' else '.' + ext), 'before\n```js\na,b,c;d<e\n```\nafter\n'
    if kind == 'cyrillic-emoji':
        return '.md', 'Кириллица🙂🚀\n' * 3
    if kind == 'binary-empty':
        return '.png', b''
    if kind == 'binary-nul':
        return '.png', b'x' * 10001 + b'\0' + b'y' * 10001
    if kind == 'json-clamp':
        return '.json', b'a' * 101 + b',' + b'b' * 300
    if kind == 'pdf-far-lf':
        return '.pdf', b'x' * 10001 + b'\n' + b'y' * 100000 + b'/' + b'z' * 500
    if kind == 'override':
        return '.png', random.Random(20261124).randbytes(102401)
    if kind.startswith('floor-binary:'):
        return '.png', b'x' * int(kind.split(':')[1])
    if kind.startswith('boundary:'):
        _, size, ext, delimiter, offset = kind.split(':')
        data = bytearray(b'x' * int(size))
        if delimiter != 'none':
            data[int(offset)] = ord(delimiter)
        return '.' + ext, bytes(data)
    return _base_grid_payload(kind)


def grid_cases():
    for case in TEXT_BOUNDARY_CASES + RANDOM_CASES + SWEEP_CASES + EQUALITY_CASES:
        yield case_id(case), settings_for(case), case[-1]
    # Keep the original exhaustive settings grid unchanged.
    for version, custom, minimum, kind in product(FACTORS[1][1], ('default', 0, 1, 5, 60, 100), ('default', 5, 20, 100), BASE_GRID_KINDS):
        case = ('legacy', version, 'xxhash64', custom, minimum, 'default', kind)
        yield case_id(case), settings_for(case), kind
    for version, minimum, kind in product(FACTORS[1][1], (5, 100), GRID_KINDS[len(BASE_GRID_KINDS):]):
        case = ('legacy', version, 'xxhash64', 0, minimum, 'default', kind)
        yield case_id(case), settings_for(case), kind
    for version, alg, kind in product(FACTORS[1][1], FACTORS[2][1], ('unicode', 'bom-text', 'cyrillic-emoji')):
        case = ('legacy', version, alg, 1, 20, 'default', kind)
        yield case_id(case), settings_for(case), kind
    case = ('legacy', 'v3-rabin-karp', 'xxhash64', 0, 102400, 'default', 'override')
    yield case_id(case), settings_for(case), 'override'
    # Boundaries independently transcribed from pinned chunks.ts:535-548 and
    # ContentSplitterBase.ts:67. Generator verifies these against live upstream
    # delimiter-free split output before emitting expectations.
    for version in FACTORS[1][1]:
        # Only V3 needs a >4 MiB request to exercise its independent ceiling.
        customs = (-0.9, 0, 1, 60) if version == 'v3-rabin-karp' else (-0.9, 0, 1)
        for custom in customs:
            cap = int(102400 * (custom + 1))
            if version == 'v3-rabin-karp':
                cap = min(4 * 1024 * 1024, max(cap, 30 * 1024))
            for delta in (-1, 0, 1):
                kind = f'boundary:{cap + delta}:png:none:0'
                case = ('legacy', version, 'xxhash64', custom, cap, 'default', kind)
                yield case_id(case), settings_for(case), kind
            if custom == 0:
                for ext, delta in product(('pdf', 'json'), (-1, 0, 1)):
                    char = '/' if ext == 'pdf' else ','
                    kind = f'boundary:{cap + 100}:{ext}:{char}:{cap + delta}'
                    case = ('legacy', version, 'xxhash64', custom, 5, 'default', kind)
                    yield case_id(case), settings_for(case), kind


# Narrow text-only probes: do not cross these large inputs with the settings
# product or the full backend lifecycle.
TEXT_BOUNDARY_CASES = [
    ('legacy', version, 'xxhash64', 0, 5, 'default', f'surrogate:{offset}')
    for version in ('v1', 'v2', 'v2-segmenter')
    for offset in (102398, 102399, 102400)
] + [
    ('legacy', 'v3-rabin-karp', 'xxhash64', 60, 20, 'default', f'large-text:{size}')
    for size in (4194303, 4194304, 4194305)
]
# Small focused probes keep byte caps independent of UTF-16/code-point caps.
ROUND4_CASES = [
    ('legacy', 'v3-rabin-karp', 'xxhash64', 0, 1024, 'default', f'utf8:{width}:{offset}')
    for width in (2, 3, 4) for offset in (1023, 1024, 1025)
] + [
    ('legacy', 'v2', 'xxhash64', 0, 5, 'default', f'hint-path:{index}')
    for index in range(4)
] + [
    ('legacy', 'v3-rabin-karp', 'xxhash64', -0.9, 30721, 'default', f'floor-binary:{size}')
    for size in (30719, 30720, 30721)
]
TEXT_BOUNDARY_CASES += ROUND4_CASES
PARTITION_CASES = [c for c in TEXT_BOUNDARY_CASES
                   if c[-1] in ('surrogate:102399', 'large-text:4194304')] + [
    ('legacy', version, 'xxhash64', 0, 5, 'default', kind)
    for version, kind in (('v2', 'hint-MD'), ('v2', 'hint-txt'),
                          ('v2', 'hint-canvas'), ('v1', 'fence-long'))
]
PARTITION_CASES += ROUND4_CASES + [
    ('legacy', 'v1', 'xxhash64', 0, 5, 'default', 'binary-empty'),
    ('legacy', 'v2', 'xxhash64', 0, 5, 'default', 'empty'),
]


# Seed 198 includes the independently discovered equality-at-minimum witness.
# These recipes contain inputs only; pinned upstream supplies every expectation.
RANDOM_CASES = [
    ('legacy', 'v3-rabin-karp', 'xxhash64', custom, minimum, 'default',
     f'random:{alphabet}:{seed}:{length}')
    for custom, minimum in product((0, 1, 60), ('default', 5, 20, 100))
    for alphabet, seeds in (('ascii', (198, 17, 42)), ('binary', (17, 42)))
    for seed, length in product(seeds, (64, 128, 200, 300, 512, 1000, 2048))
] + [
    ('legacy', 'v3-rabin-karp', 'xxhash64', custom, minimum, 'default',
     'random:mixed:198:2048')
    for custom, minimum in product((0, 1, 60), ('default', 5, 20, 100))
] + [
    ('legacy', version, 'xxhash64', 0, minimum, 'default',
     f'random:{alphabet}:{seed}:{length}')
    for version, minimum, alphabet, seed, length in product(
        ('v1', 'v2'), (5, 100), ('newline', 'punctuation'), (17, 42), (200, 512, 2048))
]
SWEEP_CASES = [
    ('legacy', 'v3-rabin-karp', 'xxhash64', 0, minimum, 'default',
     'random:ascii:198:2048')
    for minimum in range(1, 301, 7)
]
PARTITION_CASES += [
    ('legacy', 'v3-rabin-karp', 'xxhash64', 0, 5, 'default',
     'random:ascii:198:300')
]


# Equality catalogue: inputs only. Labels name actual source decisions, not
# expected results. Three deterministic seeds vary content without randomness
# at regeneration time. Scalar probes cover fractional/unreachable Blob sizes.
EQUALITY_CASES = []
EQUALITY_OPERATORS = {}

def _equality(label, version, custom, minimum, mode, size, seed, position=-1):
    case = ('legacy', version, 'xxhash64', custom, minimum, 'default',
            f'eq:{mode}:{size}:{seed}:{position}')
    if case not in EQUALITY_CASES:
        EQUALITY_CASES.append(case)
    EQUALITY_OPERATORS.setdefault(label, []).append(case_id(case))

for seed in (0, 1, 2):
    for delta in (-1, 0, 1):
        for version in ('v1', 'v2', 'v2-segmenter'):
            for mode in ('md', 'upper', 'png'):
                _equality('legacy maximum / terminal offset', version, 0, 5,
                          mode, 102400 + delta, seed)
            for mode in ('pair', 'pair-low', 'pair-high'):
                _equality('UTF16 surrogate protection', version, 0, 5,
                          mode, 102402, seed, 102399 + delta)
            for ext in ('json', 'pdf', 'png'):
                for position in (100 + delta, 10000 + delta, 102400 + delta):
                    _equality('binary delimiter min/max/search', version, 0, 5,
                              ext, 102410, seed, position)
            _equality('V2 strict newline flush / V1 minimum grouping', version,
                      0, 5, 'line', 40, seed, 5 + delta)
            _equality('V2 100-item adjustment', version, 0, 5,
                      'line', 500 + delta, seed, 5)
            _equality('V1 fence 2048', version, 0, 5,
                      'fence', 2048 + delta - 1, seed)
        for mode in ('md', 'png'):
            _equality('V3 text switch / ceiling', 'v3-rabin-karp', 60,
                      4194304, mode, 4194304 + delta, seed)
            _equality('V3 floor / minimum override', 'v3-rabin-karp',
                      (30720 + delta) / 102400 - 1, 30721,
                      mode, 30722, seed)
            _equality('V3 maximum / minimum override', 'v3-rabin-karp', 0,
                      1024 + delta, mode, 2049, seed)
        _equality('V3 500-item unit adjustment', 'v3-rabin-karp', 0, 20,
                  'md', 128000 + delta, seed)
        _equality('UTF8 max+3 / continuation protection', 'v3-rabin-karp',
                  0, 1024, 'pair', 1028, seed, 1023 + delta)
        for version in ('v1', 'v2', 'v2-segmenter'):
            for ext, clamp in (('json', 100), ('png', 100000)):
                for threshold in (clamp, 125, 1562, 19531, 244140, 3051757,
                                  38146972, 100000000):
                    if threshold < clamp:
                        continue
                    _equality('binary size clamp / derived minimum steps',
                              version, 0, 5, ext, threshold + delta, seed, 50)

# Hit the content-defined candidate exactly at its text minimum on three
# independent ASCII streams (inputs selected by a rolling-fingerprint search,
# never by the Python splitter). Upstream still supplies partitions and IDs.
# Near-4 MiB text makes the 30 KiB floor observable: short text's adaptive
# maximum is below that floor and cannot distinguish a wrong floor value.
for seed in (0, 1, 2):
    for delta in (-1, 0, 1):
        _equality('V3 floor / minimum override', 'v3-rabin-karp',
                  (30720 + delta) / 102400 - 1, 30721, 'md', 4000000, seed)

for seed in (198, 262, 861):
    for minimum in (127, 128, 129):
        case = ('legacy', 'v3-rabin-karp', 'xxhash64', 0, minimum, 'default',
                f'random:ascii:{seed}:300')
        EQUALITY_CASES.append(case)
        EQUALITY_OPERATORS.setdefault('V3 candidate minimum equality', []).append(case_id(case))
for seed in (0, 1, 2):
    for delta in (-1, 0, 1):
        for threshold in (128, 256, 1024):
            _equality('V3 text minimum / modulus clamps', 'v3-rabin-karp', 0,
                      threshold + delta, 'md', 2049, seed)
        for threshold in (25600, 102400):
            _equality('V3 binary minimum / modulus clamps', 'v3-rabin-karp', 0,
                      threshold + delta, 'png', 102402, seed)
        for mode in ('md', 'png'):
            for threshold in (0, 48, 128):
                _equality('empty / rolling window / decoder short path',
                          'v3-rabin-karp', 0, 20, mode,
                          max(0, threshold + delta), seed)
# Positive suffix and one-character-short/long neighbors, text and binary
# Blobs. Binary .md/.txt/.canvas exposes V3's path hint independently of MIME;
# ordinary backend ingress converts these paths to text before the splitter.
for version, ext, variant, seed, content_kind in product(
        FACTORS[1][1],
        ('md', 'txt', 'canvas'), ('minus', 'equal', 'plus', 'upper'), (0, 1, 2), ('text', 'binary')):
    _equality('plainSplit suffix comparison', version, 0, 5,
              f'hint-{ext}-{variant}-{content_kind}', 2049, seed)

# Preferred delimiters are absent: reach the actual LF fallback, including
# its minimum search position and maximum cut. V1 intentionally ignores LF.
for version, ext, delta, seed in product(
        ('v1', 'v2', 'v2-segmenter', 'legacy-00', 'legacy-01', 'legacy-10', 'legacy-11'),
        ('json', 'pdf', 'png'), (-1, 0, 1), (0, 1, 2)):
    size, minimum = (125, 10) if ext == 'json' else (102410, 10000)
    _equality('binary LF fallback min/max/search', version, 0, 5,
              'lf-' + ext, size, seed, minimum + delta)
    _equality('binary LF fallback min/max/search', version, 0, 5,
              'lf-' + ext, 102410, seed, 102400 + delta)
for seed, delta in product((0, 1, 2), (-1, 0, 1)):
    _equality('V3 binary candidate minimum equality', 'v3-rabin-karp', 0,
              25600 + delta, 'candidate-binary', 26001, seed, 25600)

# Discrete string decisions also need seeded neighbors: exact '#' vs empty/
# '##', first-column '#', supported fence indents vs four spaces, terminal '=',
# and EOF/trailing-line conditions. All versions here actually dispatch V1.
for version, seed, variant in product(('v1', 'legacy-00', 'legacy-01'), (0, 1, 2), range(27)):
    _equality('V1 headings/fence/suffix/EOF decisions', version, 0, 100,
              'grouping', 0, seed, variant)

# The reported Z1 witness is byte-for-byte fixed, independently of seeds.
Z1_CASE = ('legacy', 'v2', 'xxhash64', 0, 5, 'default', 'z1')
EQUALITY_CASES.append(Z1_CASE)
PARTITION_CASES.append(Z1_CASE)
# Write-first tests establish actual persisted differences, without a raw-split
# assertion stopping execution before either backend is opened.
PERSISTED_EQUALITY_CASES = [Z1_CASE,
    ('legacy', 'v3-rabin-karp', 'xxhash64', 0, 1024, 'default', 'eq:md:2049:0:-1'),
    ('legacy', 'v1', 'xxhash64', 0, 5, 'default', 'eq:fence:2047:0:-1')]

PERSISTED_EQUALITY_CASES += [
    ('legacy', 'v3-rabin-karp', 'xxhash64', 0, 25600, 'default',
     f'eq:candidate-binary:26001:{seed}:25600') for seed in (0, 1, 2)]
PARTITION_CASES += [
    ('legacy', 'v2', 'xxhash64', 0, 5, 'default', f'eq:{mode}:102402:0:102399')
    for mode in ('pair-low', 'pair-high')]
PARTITION_CASES += [c for c in EQUALITY_CASES if c[-1] == 'eq:png:2049:0:-1']



# octagonal-wheels@0.1.57 binary/base64.js:250-290: short decoding and
# truncated continuation reads. Raw text/plain Blobs are programmatic inputs.
OBSERVABLE_CASES = [
    # chunks.ts:29-32: EOF immediately after the opening fence.
    ('legacy', 'v1', 'xxhash64', 0, 5, 'default', 'fence-only'),
] + [
    ('legacy', 'v3-rabin-karp', 'xxhash64', 0, 20, 'default',
     f'decoder:{size}:{byte}:{position}')
    for size in (126, 127, 128, 129)
    for byte in ('ff', '80', 'c2', 'e0', 'f0', 'f0908080')
    for position in (0, size // 2, size - 1)
] + [
    # chunks.ts:368-370: fractional repeated addition, ratio below/equal/above 100.
    ('legacy', 'v2', 'xxhash64', 0, minimum, 'default', f'fractional:{size}:{int(minimum) + 1}')
    for minimum in (0.5, 1.25)
    for size in (int(100 * minimum) - 1, int(100 * minimum), int(100 * minimum) + 1)
] + [
    # PureJSHashManager.ts:86-87; wheels base64.js:198-240: cut surrogate
    # followed by the decimal length suffix reaches the 128-unit short encoder.
    ('legacy', 'v1', 'sha1', -0.9987841796875, 5, 'default', 'encoder-exact128'),
    # HashManager.ts:11-19,65-74: null selects fallback XXHash32.
    ('legacy', 'v3-rabin-karp', None, 0, 20, 'default', 'unicode'),
    ('legacy', 'v3-rabin-karp', 'sha1', 0, 257, 'default', 'eq:md:2049:0:-1'),
]
EQUALITY_CASES += OBSERVABLE_CASES
PERSISTED_EQUALITY_CASES += OBSERVABLE_CASES


def is_text_blob(kind):
    return kind.startswith('decoder:')


def grid_path(kind):
    if kind.startswith('eq:'):
        mode = kind.split(':')[1]
        if mode == 'candidate-binary':
            return 'equality.png'
        if mode.startswith('lf-'):
            return 'equality.' + mode[3:]
        if mode.startswith('hint-'):
            return 'equality' + grid_payload(kind)[0]
        return 'equality.' + (mode if mode in ('json', 'png', 'pdf') else 'MD' if mode == 'upper' else 'md')
    if kind == 'random:ascii:198:300':
        return 'witness.md'
    if kind.startswith('utf8:'):
        return 'utf8.md'
    if kind.startswith('hint-path:'):
        return ('trailing.md ', ' leading.md', 'weird.md\t', 'x.MD ')[int(kind.split(':')[1])]
    if kind.startswith('floor-binary:'):
        return 'floor.png'
    if kind.startswith('surrogate:'):
        return 'surrogate.md'
    if kind.startswith('large-text:'):
        return 'large.md'
    return 'grid' + grid_payload(kind)[0]


def matrix_inputs(case):
    suffix, content = payload(case[-1])
    append = b'\x00\xfftail' if isinstance(content, bytes) else '\nДобавлено🙂'
    return {'initial': ('Папка/Cafe\u0301' + suffix, content),
            'updated': ('Папка/Cafe\u0301' + suffix, content + append),
            'hash': ('oracle.md', 'helloWorld'),
            'blob': ('blob-size.md', '\ufeffabc')}


if __name__ == '__main__':
    def pack(path, data, *, grid=False, text_blob=False):
        return {'path': path, 'text': text_blob or isinstance(data, str) and (grid or path.endswith(('.md', '.txt', '.canvas'))),
                'base64': base64.b64encode(data.encode() if isinstance(data, str) else data).decode()}
    matrix = [{'id': case_id(c), 'settings': settings_for(c),
               'payloads': {k: pack(*v) for k, v in matrix_inputs(c).items()}} for c in CASES + BRANCH_CASES]
    grid = [{'id': name, 'settings': settings, 'payload': kind}
            for name, settings, kind in grid_cases()]
    print(json.dumps({'matrix': matrix, 'grid': grid,
                      'auxiliary_payloads': {
                          'boundary': pack('boundary.png', b'\x00' * 1048576),
                          'boundary-plus': pack('boundary.png', b'\x00' * 1048576 + b'x'),
                          'uncertain': pack('uncertain.md', 'accepted')},
                      'payloads': {k: ({'recipe': k, 'path': grid_path(k), 'text': k.split(':')[1] not in ('json', 'png', 'pdf') and not k.split(':')[1].endswith('-binary') and not k.split(':')[1].startswith('lf-')} if k.startswith('eq:') else pack(grid_path(k), grid_payload(k)[1], grid=True, text_blob=is_text_blob(k)))
                                   for k in dict.fromkeys(k for _, _, k in grid_cases())}}))
