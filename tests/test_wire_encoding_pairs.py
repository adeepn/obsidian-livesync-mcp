"""A valid UTF-16 pair held as two units is one character to TextEncoder and JSON.stringify.

The inputs hold a pair as two code points, the way a JavaScript string holds it. The
native values come from Node when it is installed; journalRecords.ts:69-77 is the
pinned user of both encodings.
"""
import base64
import json
import shutil
import subprocess

import pytest

from journal_codec import parse_journal, serialize_journal
from wire_encoding import json_stringify, surrogate_count, text_encoder_string

PAIR = '\ud83d\ude42'
INPUTS = {
    'pair': 'a' + PAIR + 'b',
    'pair-only': PAIR,
    'lone-units': 'x\ud83dy\ude42z',
    'high-then-pair': 'x\ud83d' + PAIR,
    'pair-then-lone-low': 'x' + PAIR + '\ude42',
    'not-adjacent': '\ud83da\ude42',
    'plain': 'ab\U0001f642\U0001f642',
}
NATIVE = """
const encoder = new TextEncoder();
const inputs = JSON.parse(process.argv[1]);
const out = {};
for (const [name, units] of Object.entries(inputs)) {
  let text = '';
  for (const unit of units) text += String.fromCharCode(unit);
  out[name] = {utf8: Buffer.from(encoder.encode(text)).toString('base64'), json: JSON.stringify({t: text})};
}
process.stdout.write(JSON.stringify(out));
"""


def units(text):
    data = text.encode('utf-16-le', errors='surrogatepass')
    return [data[i] | data[i + 1] << 8 for i in range(0, len(data), 2)]


@pytest.fixture(scope='module')
def native():
    if shutil.which('node') is None:
        pytest.skip('Node.js is not installed')
    result = subprocess.run(['node', '-e', NATIVE, json.dumps({name: units(text) for name, text in INPUTS.items()})],
                            capture_output=True, text=True, check=True, timeout=60)
    return json.loads(result.stdout)


@pytest.mark.parametrize('name', sorted(INPUTS))
def test_text_encoder_matches_native(native, name):
    assert text_encoder_string(INPUTS[name]) == base64.b64decode(native[name]['utf8']).decode('utf-8')


@pytest.mark.parametrize('name', sorted(INPUTS))
def test_json_stringify_matches_native(native, name):
    assert json_stringify({'t': INPUTS[name]}) == native[name]['json']


def test_lone_units_are_replaced_and_counted():
    assert text_encoder_string('x\ud83dy\ude42z') == 'x\ufffdy\ufffdz'
    assert text_encoder_string('x\ud83d' + PAIR) == 'x\ufffd\U0001f642'
    assert surrogate_count('x\ud83dy\ude42z') == 2
    assert surrogate_count('x\ud83d' + PAIR) == 1
    assert surrogate_count('a' + PAIR + 'b') == 0


def test_journal_frame_keeps_a_valid_pair():
    frame = serialize_journal([('h:piece', 'a' + PAIR + 'b')], [])
    assert parse_journal(frame).chunks == {'h:piece': 'a\U0001f642b'}
