"""Typed, content-free disclosure of successful write transformations."""
from contextvars import ContextVar
import json
from typing import Literal, TypedDict

class PublicationNotice(TypedDict, total=False):
    code: Literal['legacy_bom_removed', 'invalid_utf8_replaced',
                  'input_surrogates_replaced', 'journal_surrogates_replaced']
    removed_bytes: int
    metadata_size_preserved: bool
    invalid_bytes: int
    count: int

publication_notices: ContextVar[list[PublicationNotice] | None] = ContextVar('publication_notices', default=None)

_SENTENCES = {
    'legacy_bom_removed': 'The legacy splitter removed the leading UTF-8 BOM but retained the original metadata size; receiving devices refuse filesystem reflection unless processSizeMismatchedFiles is enabled.',
    'invalid_utf8_replaced': 'Invalid UTF-8 bytes were replaced with U+FFFD during text ingestion.',
    'input_surrogates_replaced': 'Blob text ingestion replaced unpaired surrogate units with U+FFFD before splitting.',
    'journal_surrogates_replaced': 'The LiveSync journal transport replaces unpaired surrogate units with U+FFFD; the note is stored exactly as devices would store it.',
}
_SCHEMAS = {
    'legacy_bom_removed': {'removed_bytes': int, 'metadata_size_preserved': bool},
    'invalid_utf8_replaced': {'invalid_bytes': int},
    'input_surrogates_replaced': {'count': int},
    'journal_surrogates_replaced': {'count': int},
}


def typed_notices(notices):
    """Closed codes and exact numeric/boolean types; never echo arbitrary fields."""
    result = []
    for notice in notices:
        if not isinstance(notice, dict):
            continue
        code = notice.get('code')
        if not isinstance(code, str) or code not in _SCHEMAS:
            continue
        schema = _SCHEMAS[code]
        if all(type(notice.get(k)) is t and (notice[k] > 0 if t is int else notice[k])
               for k, t in schema.items()):
            result.append({'code': code, **{k: notice[k] for k in schema}})
    return result


def complete_publication(result, notices):
    """Call only after durable success; metadata reads never gain notices."""
    notices = typed_notices(notices)
    if notices:
        result.setdefault('notices', []).extend(notices)
        collector = publication_notices.get()
        if collector is not None:
            collector.extend(notices)
    return result


def notice_text(result, notices):
    """Append a separate typed record and fixed prose; preserve result verbatim."""
    notices = typed_notices(notices)
    if not notices or not isinstance(result, str):
        return result
    codes = dict.fromkeys(n['code'] for n in notices)
    return result + '\n' + json.dumps({'notices': notices}) + '\n' + '\n'.join(_SENTENCES[c] for c in codes)


def ingress_notices(original):
    """common/utils.ts:100-115 Blob USVString conversion; explicit UTF-8 opt-in."""
    if isinstance(original, str):
        converted = original.encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='replace')
        count = converted.count('\ufffd') - original.count('\ufffd')
        return [{'code': 'input_surrogates_replaced', 'count': count}] if count else []
    invalid = len(original) - len(original.decode('utf-8', errors='ignore').encode('utf-8'))
    return [{'code': 'invalid_utf8_replaced', 'invalid_bytes': invalid}] if invalid else []


def split_notices(content, version):
    """chunks.ts:228,350-360 Blob.text; EntryManagerImpls.ts:280 size retention.

    reflection.ts:272-283 refuses mismatched nonzero size unless the device
    enables processSizeMismatchedFiles. V3 uses wheels readString, not Blob.text.
    """
    from content_splitter import _TextBlob, _js_utf8
    if not isinstance(content, (str, _TextBlob)):
        return []
    notices = ingress_notices(content) if isinstance(content, str) or version != 'v3-rabin-karp' else []
    raw = _js_utf8(content) if isinstance(content, str) else content
    if version != 'v3-rabin-karp' and raw.startswith(b'\xef\xbb\xbf'):
        notices.append({'code': 'legacy_bom_removed', 'removed_bytes': 3, 'metadata_size_preserved': True})
    return notices
