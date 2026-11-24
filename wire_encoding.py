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
