"""Published wheels helpers; source citations live beside each vector family."""
import base64
from itertools import product

import content_splitter as splitter
from tests.settings_oracle import ORACLE


def test_helper_oracle_covers_direct_boundaries():
    probes = ORACLE['helper_probes']
    assert {(v['code'], v['count']) for v in probes['encoder']} == set(product(
        (0, 0x7f, 0x80, 0x7ff, 0x800, 0xffff, 0x10000, 0x10ffff,
         0xd7ff, 0xd800, 0xdbff, 0xdc00, 0xdfff),
        (127, 128, 129, 255, 256, 257)))
    assert {v['value'] for v in probes['hash_conversion']} == {
        0, -1, 2**31 - 1, 2**31, -(2**31)}
    assert ORACLE['decoder_dependency']['version'] == '0.1.57'
    assert ORACLE['decoder_dependency']['integrity'].startswith('sha512-')
    assert 'package/dist/binary/base64.js' in ORACLE['source_sha256']
    assert 'managers/HashManager/XXHashHashManager.ts' in ORACLE['source_sha256']


def test_helpers_match_published_dependency():
    # wheels binary/base64.js:198-240; commonlib
    # managers/HashManager/XXHashHashManager.ts:73-74 (real raw conversion).
    for vector in ORACLE['helper_probes']['encoder']:
        assert splitter._write_string(vector['text']) == base64.b64decode(vector['base64'])
    for vector in ORACLE['helper_probes']['hash_conversion']:
        value = vector['value']
        assert splitter._base36(value) == vector['base36']
        assert splitter._int32(value) == int(vector['raw_hash'], 36)


def test_splitter_helpers_are_reachable(monkeypatch):
    # RabinKarpSplitter -> wheels readString (chunks.ts:577-583), and
    # SHA1HashManager.computeHashWithoutEncryption -> sha1 -> writeString
    # (PureJSHashManager.ts:86-87, wheels hash/purejs.js:60-65).
    calls = set()
    for name in ('_wheels_read_string', '_write_string', '_base36', '_int32'):
        original = getattr(splitter, name)

        def traced(*args, _name=name, _original=original):
            calls.add(_name)
            return _original(*args)
        monkeypatch.setattr(splitter, name, traced)
    for algorithm in ('sha1', ''):
        peer = splitter.ContentSplitter({'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': algorithm})
        for piece in peer.split('hello', path='hello.md'):
            peer.chunk_id_for_piece(piece)
    assert calls == {'_wheels_read_string', '_write_string', '_base36', '_int32'}
