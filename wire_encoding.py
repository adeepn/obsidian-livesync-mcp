"""Device JSON and TextEncoder wire encodings at the audited upstream pin."""
import json
import re

_SURROGATES = re.compile('[\ud800-\udfff]')


def json_stringify(value: object) -> str:
    """Compact Unicode JSON with well-formed JSON.stringify surrogate escapes.

    PouchDB adapter-http 9.0.0 lib/index.es.js:765-773 sends JSON.stringify(doc).
    commonlib journalRecords.ts:76 uses the same encoding for metadata.
    """
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return escape_json_surrogates(text)


def escape_json_surrogates(text: str) -> str:
    """Escape surrogate units in serialized JSON without changing other fields."""
    return _SURROGATES.sub(lambda m: '\\u%04x' % ord(m[0]), text)


def text_encoder_string(value: str) -> str:
    """journalRecords.ts:70-74: TextEncoder replaces unpaired UTF-16 units."""
    return _SURROGATES.sub('\ufffd', value)


def surrogate_count(value: str) -> int:
    return sum(1 for _ in _SURROGATES.finditer(value))


def js_number_string(value: int | float) -> str:
    """Native Number::toString used by array ToPrimitive / chunks.ts:370 +=.

    ContentSplitterBase.ts:68 and chunks.ts:368-370 operate on JSON Numbers,
    including lexical JSON overflow to Infinity. Oracle: native ECMAScript.
    """
    import math
    from decimal import Decimal
    try:
        number = float(value)
    except OverflowError:
        return '-Infinity' if value < 0 else 'Infinity'
    if math.isnan(number):
        return 'NaN'
    if math.isinf(number):
        return '-Infinity' if number < 0 else 'Infinity'
    if not number:
        return '0'
    if 1e-6 <= abs(number) < 1e21:
        text = format(Decimal(repr(number)), 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    mantissa, exponent = repr(number).lower().split('e')
    exponent = int(exponent)
    return mantissa.removesuffix('.0') + 'e' + ('+' if exponent >= 0 else '-') + str(abs(exponent))
