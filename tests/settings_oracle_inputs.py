"""Deterministic inputs shared by offline tests and the Node oracle generator.

No production imports: this module defines inputs, never expected partitions.
"""
from itertools import product
import base64
import json

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


def grid_payload(kind):
    if kind == 'text-short':
        return '.md', 'helloWorld123456789\nshort text\n'
    if kind == 'text-long':
        return '.md', 'x' * 230001 + '\n' + 'line abc;def\n' * 100
    if kind == 'bom-text':
        return '.md', '\ufeff' + 'BOM text\n' * 30
    if kind == 'crlf':
        return '.md', 'a\r\nb;cd\r\n' * 100
    if kind == 'unicode':
        return '.md', 'Привет🙂 мир🚀\n' * 100
    if kind == 'binary-small':
        return '.png', bytes(range(256))
    if kind == 'binary-large':
        return '.png', bytes(range(256)) * 900
    return '.md', ''


def grid_cases():
    for version, custom, minimum, kind in product(
            FACTORS[1][1], ('default', 0, 1, 5, 60, 100),
            ('default', 5, 20, 100), GRID_KINDS):
        case = ('legacy', version, 'xxhash64', custom, minimum, 'default', kind)
        yield case_id(case), settings_for(case), kind


def matrix_inputs(case):
    suffix, content = payload(case[-1])
    append = b'\x00\xfftail' if isinstance(content, bytes) else '\nДобавлено🙂'
    return {'initial': ('Папка/Cafe\u0301' + suffix, content),
            'updated': ('Папка/Cafe\u0301' + suffix, content + append),
            'hash': ('oracle.md', 'helloWorld'),
            'blob': ('blob-size.md', '\ufeffabc')}


if __name__ == '__main__':
    def pack(path, data):
        return {'path': path, 'text': isinstance(data, str) and path.endswith(('.md', '.txt', '.canvas')),
                'base64': base64.b64encode(data.encode() if isinstance(data, str) else data).decode()}
    matrix = [{'id': case_id(c), 'settings': settings_for(c),
               'payloads': {k: pack(*v) for k, v in matrix_inputs(c).items()}} for c in CASES]
    grid = [{'id': name, 'settings': settings, 'payload': kind}
            for name, settings, kind in grid_cases()]
    print(json.dumps({'matrix': matrix, 'grid': grid,
                      'auxiliary_payloads': {
                          'boundary': pack('boundary.png', b'\x00' * 1048576),
                          'boundary-plus': pack('boundary.png', b'\x00' * 1048576 + b'x'),
                          'uncertain': pack('uncertain.md', 'accepted')},
                      'payloads': {k: pack('grid' + grid_payload(k)[0], grid_payload(k)[1])
                                   for k in GRID_KINDS}}))
