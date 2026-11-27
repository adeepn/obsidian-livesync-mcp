"""Device revision trees: the published PouchDB 9.0.0 code a LiveSync device runs
when it receives a journal file.

A device applies one journal file in two batches (commonlib
``src/replication/journal/JournalSyncCore.ts:778-840``). Chunk documents whose
identifier it does not hold yet are created with ``new_edits: true``. Metadata
is then filtered by ``revsDiff`` and imported with ``new_edits: false``:
pouchdb-core sorts the batch by ``_id`` and ``_revisions.start``,
pouchdb-adapter-utils ``parseDoc`` derives each record's revision from
``_revisions``, and pouchdb-merge merges the record's path into the document's
revision tree and stems it to ``revs_limit`` (100 on devices,
``src/pouchdb/LiveSyncLocalDB.ts:293-298``). The winning revision is the leaf
that is not revision-deleted, then the one at the highest position, then the
greatest hash (``winningRev``).

This module ports those functions line by line over JSON values with ECMAScript
semantics: truthiness, strict equality, ToNumber, ToString and the UTF-16
string order. The idb, indexeddb and level adapters call the same functions
for all of this; where they differ from each other (a malformed revision that
already has a stored body) is recorded in the oracle fixture and in
docs/peer-limitations.md. A revision tree has the published shape: a list of
``[pos, [id, opts, [branches]]]`` roots where every branch is again
``[id, opts, [branches]]``. Numbers are kept as doubles and ``opts`` are
shared by reference exactly where JavaScript shares them.

The file's records arrive in order, the documents the plugin's decoder makes
of compact chunk records included. A record a device refuses fails its file
(:class:`RecordRefused`, with the receive stage): the plugin's decoder
contract for chunk records, the reserved ``_``-prefixed keys, a revision that
is not a generation and a hash, a document named like a member of
``Object.prototype`` (the plugin's own selection function throws), an object
hash with a ``toString`` that is not a function, an attachment name starting
with an underscore, an attachment that is not an object, attachment data that
is not base64 and a stub naming an attachment the store does not hold. Three
kinds of input are refused although a device accepts them, because what it
then holds depends on its engine, its adapter or its build rather than on a
specification (docs/peer-limitations.md): a revision position or hash that
converts to a number that is not finite (any other value of a body is held
as a device holds it), an array-like ``ids`` that lacks an element, and a
supplied ``_rev_tree`` or an attachment that is not well-formed. Attachment data is decoded with the
forgiving base64 of the browser's ``atob``, the function the build a device
loads uses.

Every walk over a record keeps a stack of its own (ToString of nested arrays,
the JSON.stringify the revision is hashed over, the check of a supplied
tree): the decoder admits a record nested as deep as its budget, and a
device's conversions do not depend on the depth of any call stack.

Every record passes core's own checks before it is parsed (:func:`_prepared`):
the attachment names and objects of a batch member, a deleted one included,
then ``cleanDocs``, which drops the attachments of a deleted record and keeps
only the six attachment keys PouchDB knows on every other one, in its order.

Nothing here touches storage. ``scripts/extract_winner_vectors.mjs`` receives
the same files through the plugin's own decoder and selection functions and the
published database, and records the engine's ``Number`` and ``parseInt`` of a
corpus of strings; ``tests/test_journal_winner.py`` compares the two.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections import deque
from typing import NamedTuple

from content_splitter import _js_truthy
from wire_encoding import escape_json_surrogates, js_number_string

# LiveSyncLocalDB.ts:293-298 opens every local database with this limit.
REVS_LIMIT = 100

# Distinguishes an absent property from JSON null.
UNDEFINED = object()

# Own property names of Object.prototype (ECMAScript): inherited by every
# plain object, none of them iterable.
_OBJECT_PROTOTYPE = frozenset((
    "constructor", "__defineGetter__", "__defineSetter__", "hasOwnProperty",
    "__lookupGetter__", "__lookupSetter__", "isPrototypeOf",
    "propertyIsEnumerable", "toString", "valueOf", "__proto__", "toLocaleString",
))

_VALID_REV = re.compile(r"[0-9]+-[^-]*")
_INDEX_KEY = re.compile(r"0|[1-9][0-9]*")
_STRING_NUMBER = re.compile(
    r"(?P<sign>[+-]?)(?:(?P<inf>Infinity)|(?P<dec>(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?))\Z"
    r"|(?P<radix>0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+)\Z"
)
# ECMAScript WhiteSpace and LineTerminator: the characters ToNumber strips
# from both ends of a string and parseInt from its start, one by one. A range
# cannot be spelled inside a plain string; the hyphen would join the set.
_JS_WHITESPACE = "".join(map(chr, (
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
    0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
)))

# pouchdb-adapter-utils lib/index.js:101-133: keys that never reach a stored body.
_RESERVED_WORDS = frozenset((
    "_id", "_rev", "_access", "_attachments", "_deleted", "_revisions",
    "_revs_info", "_conflicts", "_deleted_conflicts", "_local_seq", "_rev_tree",
    "_replication_id", "_replication_state", "_replication_state_time",
    "_replication_state_reason", "_replication_stats", "_removed",
))
_DATA_WORDS = frozenset((
    "_access", "_attachments", "_replication_id", "_replication_state",
    "_replication_state_time", "_replication_state_reason", "_replication_stats",
))

REASONS = frozenset((
    "not-an-object", "invalid-rev", "missing-id", "invalid-id", "reserved-id",
    "doc-validation", "type-error", "non-finite-number", "missing-revision-id",
    "malformed-tree", "object-key-id", "invalid-chunk-record", "attachment-name",
    "invalid-base64", "malformed-attachment", "missing-stub", "conflict",
))


class RecordRefused(ValueError):
    """A record the device's database rejects; its whole batch fails.

    ``reason`` is one of :data:`REASONS` and ``stage`` the receive stage a
    device fails at: ``metadata-selection`` in the plugin's own selection of
    the metadata batch, ``chunk-write`` or ``metadata-write`` in the
    database's commit of a batch. Neither carries record content.
    """

    def __init__(self, reason: str, stage: str = "metadata-write"):
        if reason not in REASONS:
            raise ValueError(f"unknown refusal reason {reason!r}")
        self.reason = reason
        self.stage = stage
        super().__init__(f"journal record refused ({reason}, {stage})")


# -- ECMAScript value semantics -------------------------------------------------


def _truthy(value) -> bool:
    return value is not UNDEFINED and _js_truthy(value)


def _number(value) -> float:
    """ECMAScript ToNumber of a JSON value; numbers are doubles like JSON.parse."""
    if value is UNDEFINED:
        return math.nan
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except OverflowError:
            return math.inf if value > 0 else -math.inf
    if isinstance(value, str):
        return _string_number(value)
    return _number(_to_primitive(value))


def _string_number(text: str) -> float:
    """ToNumber of a string (StringNumericLiteral)."""
    body = text.strip(_JS_WHITESPACE)
    if not body:
        return 0.0
    match = _STRING_NUMBER.fullmatch(body)
    if match is None:
        return math.nan
    if match.group("radix") is not None:
        # The integer value may exceed every double; _number rounds to Infinity.
        return _number(int(match.group("radix"), 0))
    if match.group("inf") is not None:
        return -math.inf if match.group("sign") == "-" else math.inf
    return float(body)


def _parse_int(text) -> float:
    """ECMAScript ``parseInt(text, 10)``."""
    body = text.lstrip(_JS_WHITESPACE)
    sign = ""
    if body[:1] in ("+", "-"):
        sign, body = body[0], body[1:]
    digits = re.match(r"[0-9]+", body)
    if digits is None:
        return math.nan
    number = _decimal_integer(sign + digits.group())
    return -0.0 if number == 0 and sign == "-" else number


def _decimal_integer(digits: str) -> float:
    """The double of a decimal integer's digits, rounded as JavaScript rounds.

    The interpreter converts a decimal string of at most a configured number
    of digits; a longer one is parsed by ``float`` alone, which rounds the
    same way (``"9" * 5000`` is Infinity and ``"0" * 5000 + "1"`` is 1).
    """
    try:
        return _number(int(digits))
    except ValueError:
        return float(digits)


def _primitive(value) -> bool:
    return value is UNDEFINED or value is None or isinstance(value, (bool, int, float, str))


def _to_primitive(value):
    """ECMAScript ToPrimitive of a JSON value (the default and number hints
    agree for JSON objects: valueOf, then toString).

    ``Array.prototype.valueOf`` returns the array itself, so an array goes on
    to its string. So does an ordinary object, to ``Object.prototype.toString``
    unless it carries its own ``toString``: JSON can only make that a
    non-function, and using it is a TypeError on a device.
    """
    if _primitive(value):
        return value
    return _to_string(value)


def _to_string_atom(value) -> str:
    """ToString of a JSON value that is not an array."""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return js_number_string(value)
    if isinstance(value, dict) and "toString" in value:
        raise RecordRefused("type-error")
    return "[object Object]"


def _to_string(value) -> str:
    """ECMAScript ToString of a JSON value.

    Array.prototype.toString joins with commas, and null and undefined become
    empty. The walk keeps its own stack: the decoder admits a record nested
    as deep as its budget, and a device's conversion does not depend on the
    depth of any call stack.
    """
    if not isinstance(value, list):
        return _to_string_atom(value)
    out = []
    stack = [(value, True)]
    while stack:
        item, expand = stack.pop()
        if not expand:
            out.append(item)
        elif item is None or item is UNDEFINED:
            continue
        elif isinstance(item, list):
            for position in range(len(item) - 1, -1, -1):
                if position < len(item) - 1:
                    stack.append((",", False))
                stack.append((item[position], True))
        else:
            out.append(_to_string_atom(item))
    return "".join(out)


def _concat(*parts) -> str:
    return "".join(_to_string(part) for part in parts)


def _strict(left, right) -> bool:
    """ECMAScript ``===``: objects only equal themselves, never by content."""
    if left is UNDEFINED or right is UNDEFINED or left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return _number(left) == _number(right)
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return left is right


def _units(text: str) -> bytes:
    """UTF-16 code units: the order JavaScript compares strings in."""
    return text.encode("utf-16-be", "surrogatepass")


def _less(left, right) -> bool:
    """ECMAScript ``left < right`` on JSON values."""
    left, right = _to_primitive(left), _to_primitive(right)
    if isinstance(left, str) and isinstance(right, str):
        return _units(left) < _units(right)
    left, right = _number(left), _number(right)
    return False if math.isnan(left) or math.isnan(right) else left < right


def _array_index(key: str) -> int | None:
    """The array index a property key names, or ``None`` for an ordinary key.

    An index is a canonical decimal below ``2**32 - 1``, so at most ten
    digits. A longer digit string is an ordinary key in JavaScript and is
    never converted here: the interpreter refuses thousands of digits.
    """
    if _INDEX_KEY.fullmatch(key) is None or len(key) > 10:
        return None
    index = int(key)
    return index if index < 2**32 - 1 else None


def _get(value, key):
    """JavaScript property access ``value[key]``; UNDEFINED when absent."""
    if value is UNDEFINED or value is None:
        raise RecordRefused("type-error")
    if isinstance(value, dict):
        return value.get(key if isinstance(key, str) else _to_string(key), UNDEFINED)
    if isinstance(value, (list, str)):
        length = len(value) if isinstance(value, list) else len(value.encode(
            "utf-16-be", "surrogatepass")) // 2
        if isinstance(key, str):
            if key == "length":
                return float(length)
            index = _array_index(key)
            if index is None:
                return UNDEFINED
            key = index
        if not 0 <= key < length:
            return UNDEFINED
        if isinstance(value, list):
            return value[key]
        units = value.encode("utf-16-be", "surrogatepass")
        return units[2 * key:2 * key + 2].decode("utf-16-be", "surrogatepass")
    return UNDEFINED


def _json_number(number: float):
    """The JSON value a device prints for a double: its shortest round-trip decimal."""
    text = js_number_string(number)
    try:
        return int(text)
    except ValueError:
        return float(text)


def _finite(number: float) -> float:
    if not math.isfinite(number):
        raise RecordRefused("non-finite-number")
    return number


# -- parseDoc: the record's revision and path -------------------------------------


def is_local_id(doc_id) -> bool:
    """pouchdb-merge ``isLocalId``."""
    return isinstance(doc_id, str) and doc_id.startswith("_local/")


def valid_rev(rev) -> bool:
    """pouchdb-core ``isValidRev``: ``/\\d+-[^-]*$/``."""
    return isinstance(rev, str) and _VALID_REV.fullmatch(rev) is not None


def forced_ancestor(rev: str) -> tuple[float, str]:
    """The base a ``put`` with ``force`` gives the new revision
    (pouchdb-core ``transformForceOptionToNewEditsOption``): the base's
    generation, read with ``parseInt``, and its hash, which is all the tree
    of the new revision holds until the merge finds that hash in the stored
    tree. The revision is one ``valid_rev`` accepts; the generation is a
    JSON number as the device prints it, an integer while a double holds it
    exactly.
    """
    parts = rev.split("-")
    return _json_number(_parse_int(parts[0])), parts[1]


def _parse_revision_info(rev) -> tuple[float, object]:
    """pouchdb-adapter-utils ``parseRevisionInfo``."""
    if not isinstance(rev, str) or re.match(r"[0-9]+-", rev) is None:
        raise RecordRefused("invalid-rev")
    cut = rev.find("-")
    return _finite(_parse_int(rev[:cut])), rev[cut + 1:]


def _check_id(doc_id) -> None:
    """pouchdb-utils ``invalidIdError``."""
    if not _truthy(doc_id):
        raise RecordRefused("missing-id")
    if not isinstance(doc_id, str):
        raise RecordRefused("invalid-id")
    if doc_id.startswith("_") and not doc_id.startswith(("_design", "_local")):
        raise RecordRefused("reserved-id")


def _ordered_items(value: dict) -> list:
    """JSON.stringify key order: array indices ascending, then insertion order."""
    return sorted(value.items(), key=lambda item: (
        (0, index) if (index := _array_index(item[0])) is not None else (1, 0)))


def _json_atom(value) -> str:
    """JSON.stringify of a JSON value that is not a container."""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return escape_json_surrogates(json.dumps(value, ensure_ascii=False))
    # JSON.stringify writes a number that is not finite as null, at any depth.
    return "null" if not math.isfinite(_number(value)) else js_number_string(value)


def _json_stringify(value) -> str:
    """JSON.stringify, including the native Number::toString.

    The walk keeps its own stack, so a record nested as deep as the decoder's
    budget admits is spelled the same on every interpreter.
    """
    if not isinstance(value, (dict, list)):
        return _json_atom(value)
    out = []
    stack = [(value, True)]
    while stack:
        item, expand = stack.pop()
        if not expand:
            out.append(item)
        elif isinstance(item, list):
            out.append("[")
            stack.append(("]", False))
            for position in range(len(item) - 1, -1, -1):
                if position < len(item) - 1:
                    stack.append((",", False))
                stack.append((item[position], True))
        elif isinstance(item, dict):
            out.append("{")
            stack.append(("}", False))
            items = _ordered_items(item)
            for position in range(len(items) - 1, -1, -1):
                key, member = items[position]
                if position < len(items) - 1:
                    stack.append((",", False))
                stack.append((member, True))
                stack.append((escape_json_surrogates(json.dumps(key, ensure_ascii=False)) + ":", False))
        else:
            out.append(_json_atom(item))
    return "".join(out)


def _revision_hash(doc: dict) -> str:
    """pouchdb-utils ``rev`` with deterministic revisions: MD5 of the JSON body."""
    mutable = dict(doc)
    mutable.pop("_rev_tree", None)
    return hashlib.md5(_json_stringify(mutable).encode("utf-8")).hexdigest()


def _tree_from_revisions(revisions, opts: dict) -> list:
    """pouchdb-adapter-utils ``makeRevTreeFromRevisions``."""
    ids = _get(revisions, "ids")
    pos = _finite(_number(_get(revisions, "start")) - _number(_get(ids, "length")) + 1)
    length = _number(_get(ids, "length"))
    tree = [_identifier(ids, 0), opts, []]
    for index in range(1, math.ceil(length)):
        tree = [_identifier(ids, index), {"status": "missing"}, [tree]]
    return [[pos, tree]]


def _identifier(ids, index):
    """One hash of a chain; absent elements and non-finite numbers are engine-defined."""
    value = _get(ids, index)
    if value is UNDEFINED:
        raise RecordRefused("missing-revision-id")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _finite(_number(value))
    return value


def _supplied_tree(value) -> list:
    """A record's own ``_rev_tree`` in the tree shape this port uses.

    Only a well-formed tree is replayed: a list of ``{pos, ids}`` roots whose
    positions are finite numbers and whose branches have the published
    ``[id, opts, branches]`` shape with primitives or strings as hashes.
    Anything else fails inside a device's adapter, where the outcome depends
    on the adapter, and is refused here.
    """
    def node(ids) -> bool:
        # A branch's shape is checked over a stack of its own: a supplied
        # chain is as deep as the record it arrives in.
        pending = [ids]
        while pending:
            branch = pending.pop()
            if not (isinstance(branch, list) and len(branch) == 3 and isinstance(branch[1], dict)
                    and isinstance(branch[2], list) and _coercible(branch[0])):
                return False
            pending.extend(branch[2])
        return True

    def _coercible(rev_id) -> bool:
        try:
            _to_primitive(rev_id)
            return True
        except RecordRefused:
            return False

    if not isinstance(value, list) or not value:
        raise RecordRefused("malformed-tree")
    roots = []
    for root in value:
        pos, ids = _get(root, "pos"), _get(root, "ids")
        if (not isinstance(pos, (int, float)) or isinstance(pos, bool)
                or not math.isfinite(_number(pos)) or not node(ids)):
            raise RecordRefused("malformed-tree")
        roots.append([_number(pos), ids])
    return roots


def parse_record(doc: object, *, new_edits: bool) -> dict:
    """pouchdb-adapter-utils ``parseDoc(doc, newEdits, {deterministic_revs: true})``.

    Returns ``{id, rev, tree, data, raw_deleted}``: ``rev`` is the revision the
    device keeps (``_revisions`` wins over ``_rev``), ``tree`` the record's
    single path, ``data`` the keys a stored body keeps and ``raw_deleted`` the
    record's ``_deleted`` value, UNDEFINED when absent. Raises
    :class:`RecordRefused` where the device fails the batch.
    """
    if not isinstance(doc, dict):
        raise RecordRefused("not-an-object")
    opts = {"status": "available"}
    if _truthy(doc.get("_deleted", UNDEFINED)):
        opts["deleted"] = True
    tree = None
    if new_edits:
        new_rev_id = _revision_hash(doc)
        if _truthy(doc.get("_rev", UNDEFINED)):
            prefix, rev_id = _parse_revision_info(doc["_rev"])
            tree = [[prefix, [rev_id, {"status": "missing"}, [[new_rev_id, opts, []]]]]]
            generation = prefix + 1
        else:
            tree = [[1.0, [new_rev_id, opts, []]]]
            generation = 1.0
    else:
        revisions = doc.get("_revisions", UNDEFINED)
        if _truthy(revisions):
            tree = _tree_from_revisions(revisions, opts)
            generation = _get(revisions, "start")
            new_rev_id = _get(_get(revisions, "ids"), 0)
        if tree is None and _truthy(doc.get("_rev_tree", UNDEFINED)):
            # parseDoc keeps a supplied tree and leaves the revision unset.
            tree = _supplied_tree(doc["_rev_tree"])
            generation = new_rev_id = UNDEFINED
        elif tree is None:
            generation, new_rev_id = _parse_revision_info(doc.get("_rev", UNDEFINED))
            tree = [[generation, [new_rev_id, opts, []]]]
    doc_id = doc.get("_id", UNDEFINED)
    _check_id(doc_id)
    data, raw_deleted = {}, UNDEFINED
    for key, value in doc.items():
        if key[:1] != "_":
            data[key] = value
        elif key not in _RESERVED_WORDS:
            raise RecordRefused("doc-validation")
        elif key in _DATA_WORDS:
            data[key] = value
        elif key == "_deleted":
            raw_deleted = value
    return {"id": doc_id, "rev": _concat(generation, "-", new_rev_id), "tree": tree,
            "data": data, "raw_deleted": raw_deleted}


_BASE64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
_ASCII_WHITESPACE = "\t\n\x0c\r "


def _decode_base64(data: str) -> bytes:
    """The forgiving-base64 decode of the browser's ``atob``.

    The build of PouchDB a browser loads decodes attachment data with the
    platform's ``atob``: ASCII whitespace is ignored and missing padding is
    accepted, but padding is dropped only from a multiple of four characters,
    so a stray ``=`` fails like any other character outside the alphabet.
    """
    body = "".join(char for char in data if char not in _ASCII_WHITESPACE)
    if len(body) % 4 == 0 and body.endswith("="):
        body = body[:-2] if body.endswith("==") else body[:-1]
    if len(body) % 4 == 1 or any(char not in _BASE64_ALPHABET for char in body):
        raise RecordRefused("invalid-base64")
    return base64.b64decode(body + "=" * (-len(body) % 4), validate=True)


def _own_keys(value) -> list:
    """``Object.keys`` of a JSON value: nothing for numbers and booleans."""
    if isinstance(value, dict):
        return list(value)
    if isinstance(value, (list, str)):
        return [str(index) for index in range(len(value))]
    return []


# pouchdb-core lib/index.js cleanDocs: the keys an attachment keeps, in its order.
_ATTACHMENT_KEYS = ("data", "digest", "content_type", "length", "revpos", "stub")


def _pick(attachment) -> dict:
    """pouchdb-utils ``pick``: the known keys of an attachment object, in order.

    ``in`` over a primitive is a TypeError on a device.
    """
    if not isinstance(attachment, (dict, list)):
        raise RecordRefused("malformed-attachment")
    known = attachment if isinstance(attachment, dict) else {}
    return {key: known[key] for key in _ATTACHMENT_KEYS if key in known}


def _prepared(record: dict) -> dict:
    """A record as the database's ``bulkDocs`` hands it to the adapter.

    core checks every record of a batch first, a deleted one included: an
    attachment name may not start with an underscore, and each attachment is
    read for its content type. ``cleanDocs`` then drops the attachments of a
    deleted record and keeps only the six keys PouchDB knows on every other
    attachment, in its order, which is the order the deterministic revision
    hash is taken over.
    """
    attachments = record.get("_attachments", UNDEFINED)
    if _truthy(attachments):
        misnamed = False
        for name in _own_keys(attachments):
            attachment = _get(attachments, name)
            if attachment is None or attachment is UNDEFINED:
                # Reading .content_type of null is a TypeError on a device.
                raise RecordRefused("malformed-attachment")
            misnamed = misnamed or name[:1] == "_"
        if misnamed:
            raise RecordRefused("attachment-name")
    if _truthy(record.get("_deleted", UNDEFINED)):
        return {key: value for key, value in record.items() if key != "_attachments"}
    if not _truthy(attachments) or isinstance(attachments, (bool, int, float)):
        return record
    if isinstance(attachments, list):
        return {**record, "_attachments": [_pick(attachment) for attachment in attachments]}
    if isinstance(attachments, dict):
        return {**record, "_attachments": {name: _pick(attachments[name]) for name in list(attachments)}}
    # Elements of a string cannot be assigned; `in` over its characters fails.
    raise RecordRefused("malformed-attachment")


def _admit_attachments(info: dict, has_attachment) -> list:
    """``preprocessAttachments`` and ``verifyAttachments`` for one parsed
    record; returns the digests it stores.

    A device validates every attachment of a batch before it writes any of
    it: a stub must name an attachment it already holds (never one of the
    same batch), and data must be a string of base64, from which the length
    and digest are taken. The attachment objects prepared here are the ones
    ``cleanDocs`` made, so the caller's record is left as it was. Every
    attachment is an object of the six known keys: ``cleanDocs`` has run.
    """
    data = info["data"]
    if not _truthy(data.get("_attachments", UNDEFINED)):
        return []
    stored = data["_attachments"]
    digests = []
    for name in _own_keys(stored):
        attachment = _get(stored, name)
        if _truthy(attachment.get("stub", UNDEFINED)):
            if not has_attachment(attachment.get("digest", UNDEFINED)):
                raise RecordRefused("missing-stub")
            continue
        if not isinstance(attachment.get("data", UNDEFINED), str):
            # Data that is not a string is taken for a Blob, which JSON cannot carry.
            raise RecordRefused("malformed-attachment")
        raw = _decode_base64(attachment["data"])
        attachment["length"] = _json_number(float(len(raw)))
        attachment["digest"] = "md5-" + base64.b64encode(hashlib.md5(raw).digest()).decode("ascii")
        digests.append(attachment["digest"])
    return digests


def _store_attachments(body: dict, winning: str) -> list:
    """``writeAttachments``: the body keeps no data; returns the digests it stores."""
    attachments = body.get("_attachments")
    if not isinstance(attachments, (dict, list)):
        return []
    digests = []
    for name in _own_keys(attachments):
        attachment = _get(attachments, name)
        if isinstance(attachment, dict) and not _truthy(attachment.get("stub", UNDEFINED)):
            attachment.pop("data", None)
            attachment["revpos"] = _json_number(_parse_int(winning))
            digests.append(attachment.get("digest"))
    return [digest for digest in digests if digest is not None]


# -- pouchdb-merge 9.0.0 ----------------------------------------------------------


def winning_rev(tree: list) -> str:
    """``winningRev``: not deleted, then the highest position, then the greatest hash."""
    winning_id, winning_pos, winning_deleted = UNDEFINED, UNDEFINED, UNDEFINED
    to_visit = list(tree)
    while to_visit:
        node = to_visit.pop()
        ids, pos = node[1], node[0]
        if ids[2]:
            to_visit.extend([[pos + 1, branch] for branch in ids[2]])
            continue
        deleted = _truthy(_get(ids[1], "deleted"))
        if (not _truthy(winning_id)
                or winning_deleted != deleted and winning_deleted
                or winning_deleted == deleted and winning_pos != pos and winning_pos < pos
                or winning_deleted == deleted and winning_pos == pos and _less(winning_id, ids[0])):
            winning_id, winning_pos, winning_deleted = ids[0], pos, deleted
    return _concat(winning_pos, "-", winning_id)


def _traverse(tree: list, callback) -> None:
    """``traverseRevTree``: the order every tree walk visits nodes in."""
    to_visit = list(tree)
    while to_visit:
        node = to_visit.pop()
        ids = node[1]
        callback(not ids[2], node[0], ids[0], ids[1])
        to_visit.extend([[node[0] + 1, branch] for branch in ids[2]])


def collect_leaves(tree: list) -> list[tuple[str, bool]]:
    """``collectLeaves``: ``(rev, deleted)`` of every leaf, highest position first."""
    leaves = []
    _traverse(tree, lambda is_leaf, pos, rev_id, opts: leaves.append(
        (pos, _concat(pos, "-", rev_id), _truthy(_get(opts, "deleted")))) if is_leaf else None)
    leaves.sort(key=lambda leaf: leaf[0])
    return [(rev, deleted) for _, rev, deleted in reversed(leaves)]


def collect_conflicts(tree: list) -> list[str]:
    """``collectConflicts``: live leaves other than the winner, in leaf order."""
    win = winning_rev(tree)
    return [rev for rev, deleted in collect_leaves(tree) if not _strict(rev, win) and not deleted]


def is_deleted(tree: list, rev: str) -> bool:
    """``isDeleted``: the flag of the first node whose hash the revision names."""
    rev_id = rev[rev.find("-") + 1:]
    to_visit = [root[1] for root in tree]
    while to_visit:
        ids = to_visit.pop()
        if _strict(ids[0], rev_id):
            return _truthy(_get(ids[1], "deleted"))
        to_visit += ids[2]
    return False


def rev_exists(tree: list, rev: str) -> bool:
    """``revExists``: whether a revision is anywhere in the tree."""
    parts = rev.split("-")
    target_pos, target_id = _parse_int(parts[0]), parts[1] if len(parts) > 1 else UNDEFINED
    to_visit = list(tree)
    while to_visit:
        node = to_visit.pop()
        if _strict(node[0], target_pos) and _strict(node[1][0], target_id):
            return True
        to_visit.extend([[node[0] + 1, branch] for branch in node[1][2]])
    return False


def _compare_tree(a, b) -> int:
    return -1 if _less(a[0], b[0]) else 1


def _insert_sorted(items: list, item) -> None:
    low, high = 0, len(items)
    while low < high:
        mid = (low + high) // 2
        if _compare_tree(items[mid], item) < 0:
            low = mid + 1
        else:
            high = mid
    items.insert(low, item)


def _merge_tree(tree1: list, tree2: list):
    """``mergeTree``: folds ``tree2`` into ``tree1`` in place; equal roots required."""
    root = tree1
    queue = [(tree1, tree2)]
    conflicts = False
    while queue:
        tree1, tree2 = queue.pop()
        status1, status2 = _get(tree1[1], "status"), _get(tree2[1], "status")
        if _truthy(status1) or _truthy(status2):
            tree1[1]["status"] = "available" if (_strict(status1, "available")
                                                  or _strict(status2, "available")) else "missing"
        for index in range(len(tree2[2])):
            if not tree1[2]:
                conflicts = "new_leaf"
                tree1[2].append(tree2[2][index])
                continue
            merged = False
            for other in range(len(tree1[2])):
                if _strict(tree1[2][other][0], tree2[2][index][0]):
                    queue.append((tree1[2][other], tree2[2][index]))
                    merged = True
            if not merged:
                conflicts = "new_branch"
                _insert_sorted(tree1[2], tree2[2][index])
    return conflicts, root


def _do_merge(tree: list, path: list, dont_expand: bool = False):
    """``doMerge``: merge one root path into the tree, or attach it as a new root."""
    restree, conflicts, merged = [], False, False
    if not tree:
        return [path], "new_leaf"
    for branch in tree:
        if _strict(branch[0], path[0]) and _strict(branch[1][0], path[1][0]):
            result, ids = _merge_tree(branch[1], path[1])
            restree.append([branch[0], ids])
            conflicts, merged = conflicts or result, True
        elif dont_expand is not True:
            # The paths start at different positions: walk the earlier one down
            # to the later one's root and merge there when the keys match.
            earlier, later = (branch, path) if _less(branch[0], path[0]) else (path, branch)
            difference = _number(later[0]) - _number(earlier[0])
            candidates = []
            trees = [[earlier[1], difference, None, None]]
            while trees:
                item = trees.pop()
                if item[1] == 0:
                    if _strict(item[0][0], later[1][0]):
                        candidates.append(item)
                    continue
                for index, element in enumerate(item[0][2]):
                    trees.append([element, item[1] - 1, item[0], index])
            if not candidates:
                restree.append(branch)
            else:
                found = candidates[0]
                result, ids = _merge_tree(found[0], later[1])
                found[2][2][found[3]] = ids
                restree.append([earlier[0], earlier[1]])
                conflicts, merged = conflicts or result, True
        else:
            restree.append(branch)
    if not merged:
        restree.append(path)
    restree.sort(key=lambda node: node[0])
    return restree, conflicts or "internal_node"


def _root_to_leaf(tree: list) -> list:
    """``rootToLeaf``: every root-to-leaf path as ``(root_pos, [(id, opts), ...])``."""
    paths = []
    to_visit = [(root[0], root[1], None) for root in tree]
    while to_visit:
        pos, ids, history = to_visit.pop()
        history = (list(history) if history is not None else []) + [(ids[0], ids[1])]
        if not ids[2]:
            paths.append((pos + 1 - len(history), history))
        for branch in ids[2]:
            to_visit.append((pos + 1, branch, history))
    paths.reverse()
    return paths


def _path_to_tree(path: list, num_stemmed: int) -> list:
    """``pathToTree``: a flat path as one branch, keeping opts by reference."""
    root = leaf = None
    for rev_id, opts in path[num_stemmed:]:
        current = [rev_id, opts, []]
        if leaf is None:
            root = leaf = current
        else:
            leaf[2].append(current)
            leaf = current
    return root


def stem(tree: list, depth: int) -> tuple[list, list]:
    """``stem``: cut every path to its last ``depth`` revisions.

    Returns the tree and the revisions cut away (``stemmedRevs``), each spelled
    ``<pos>-<id>`` as the adapter names the by-sequence slot it removes for it.
    """
    result, stemmed = None, []
    for pos, history in _root_to_leaf(tree):
        if len(history) > depth:
            cut = len(history) - depth
            node = [pos + cut, _path_to_tree(history, cut)]
            stemmed += [_concat(pos + step, "-", history[step][0]) for step in range(cut)]
        else:
            node = [pos, _path_to_tree(history, 0)]
        result = _do_merge(result, node, True)[0] if result is not None else [node]
    if not stemmed:
        return result, []
    # Some revisions may have been removed in a branch but not in another.
    present = set()
    _traverse(result, lambda is_leaf, pos, rev_id, opts: present.add(_concat(pos, "-", rev_id)))
    return result, [rev for rev in dict.fromkeys(stemmed) if rev not in present]


def merge(tree: list, path: list, depth: int) -> tuple[list, str, list]:
    """``merge``: attach a path and stem; returns the tree, the conflict kind and
    the revisions stemmed away."""
    tree, conflicts = _do_merge(tree, path)
    tree, stemmed = stem(tree, depth)
    return tree, conflicts, stemmed


def revision_chain(tree: list, rev: str) -> dict:
    """The ``_revisions`` a device reports for ``rev`` (pouchdb-core ``get``, ``revs: true``).

    Devices publish this chain: the stemmed path from the tree's root to the
    revision. Raises ``ValueError`` when the revision is not in the tree.
    """
    # core get splits the revision at every hyphen: the first part is the
    # position and the second the hash; anything after a further hyphen is
    # not compared, so "1-a-b" looks for the hash "a" and finds none.
    parts = rev.split("-")
    rev_no, rev_hash = _parse_int(parts[0]), parts[1] if len(parts) > 1 else UNDEFINED
    path = None
    for current in _root_to_leaf(tree):
        history = current[1]
        index = next((i for i, node in enumerate(history) if _strict(node[0], rev_hash)), -1)
        # A path starting at position 1 outranks one that merely contains the hash.
        if _strict(index, rev_no - 1) or (path is None and index != -1):
            path = current
    if path is None:
        raise ValueError("revision is not in the tree")
    history = path[1]
    kept = history[:next((i for i, node in enumerate(history) if _strict(node[0], rev_hash)), -1) + 1]
    kept.reverse()
    return {"start": _json_number(path[0] + len(kept) - 1), "ids": [node[0] for node in kept]}


# -- Receiving one journal file ------------------------------------------------------


class _Written:
    """A record the device wrote: ``writeDoc`` stores these values with it,
    ``stemmed`` naming the revisions of the document a merge cut away."""

    def __init__(self, info, winning, winning_deleted, new_rev_deleted, stemmed):
        self.info, self.winning = info, winning
        self.winning_deleted, self.new_rev_deleted = winning_deleted, new_rev_deleted
        self.stemmed = stemmed


_KNOWN = object()


def _metadata(tree: list) -> dict:
    """The fields an adapter persists with a document between writes."""
    winning = winning_rev(tree)
    return {"tree": tree, "winning": winning, "deleted": is_deleted(tree, winning)}


def _update_doc(rev_limit: int, previous: dict, info: dict, new_edits: bool):
    """pouchdb-adapter-utils ``updateDoc``."""
    if rev_exists(previous["tree"], info["rev"]) and not new_edits:
        return _KNOWN
    previously_deleted = previous["deleted"]
    deleted = info["raw_deleted"] if info["raw_deleted"] is not UNDEFINED else is_deleted(
        info["tree"], info["rev"])
    if previously_deleted and not _truthy(deleted) and new_edits and info["rev"].startswith("1-"):
        # The first write after a deletion reattaches to the deleted tree.
        new_doc = dict(info["data"])
        new_doc["_rev"], new_doc["_id"] = previous["winning"], info["id"]
        info = parse_record(new_doc, new_edits=new_edits)
    tree, conflicts, stemmed = merge(previous["tree"], info["tree"][0], rev_limit)
    in_conflict = new_edits and (
        (previously_deleted and _truthy(deleted) and conflicts != "new_leaf")
        or (not previously_deleted and conflicts != "new_leaf")
        or (previously_deleted and not _truthy(deleted) and conflicts == "new_branch"))
    if in_conflict:
        return RecordRefused("conflict")
    info["tree"] = tree
    winning = winning_rev(tree)
    winning_deleted = is_deleted(tree, winning)
    new_rev_deleted = winning_deleted if _strict(info["rev"], winning) else is_deleted(tree, info["rev"])
    return _Written(info, winning, winning_deleted, new_rev_deleted, stemmed)


def _insert_doc(info: dict, new_edits: bool, rev_limit: int):
    """``insertDoc``: a document the device does not hold yet; new trees are stemmed too."""
    info["tree"], _, stemmed = merge([], info["tree"][0], rev_limit)
    winning = winning_rev(info["tree"])
    # 4712: a new document written with a revision whose root is unknown.
    if new_edits and _strict(_get(info["tree"][0][1][1], "status"), "missing"):
        return RecordRefused("conflict")
    return _Written(info, winning, is_deleted(info["tree"], winning), is_deleted(info["tree"], winning), stemmed)


def _import_docs(infos: list, fetched: dict, new_edits: bool, rev_limit: int):
    """``processDocs``: apply a parsed batch in order.

    Returns one outcome per record and the final tree of every document the
    batch changed. Documents of one id are applied in turn against the tree the
    previous record left. A refused record is an outcome, not an exception: the
    other records of the batch are still written.
    """
    results, trees = [None] * len(infos), {}
    groups: dict = {}
    for index, info in enumerate(infos):
        groups.setdefault(info["id"], []).append(index)
    for doc_id, indexes in groups.items():
        for index in indexes:
            info = infos[index]
            if doc_id in fetched:
                outcome = _update_doc(rev_limit, fetched[doc_id], info, new_edits)
                if outcome is _KNOWN:
                    results[index] = info
                    continue
            else:
                outcome = _insert_doc(info, new_edits, rev_limit)
            if isinstance(outcome, RecordRefused):
                results[index] = outcome
                continue
            fetched[doc_id] = _metadata(outcome.info["tree"])
            trees[doc_id] = fetched[doc_id]["tree"]
            results[index] = outcome
    return results, trees


class StoredBody(NamedTuple):
    """A body ``writeDoc`` stores: under ``rev`` of ``doc_id``, with the
    attachments it maps, the attachment data it stores and the slots a merge
    stemmed away."""

    doc_id: object
    rev: object
    body: dict
    attachments: list
    stemmed: list
    data: list


def _attachment_digests(body: dict) -> list:
    """``insertAttachmentMappings``: the digest of every attachment of a stored body."""
    attachments = body.get("_attachments")
    if not isinstance(attachments, (dict, list)):
        return []
    return [_get(_get(attachments, name), "digest") for name in _own_keys(attachments)]


def _written_rows(infos: list, results: list) -> tuple[list, list]:
    """``writeDoc``: ``(rows, attachment digests)`` each written record stores,
    keyed by its kept revision; the digests are those whose data was stored."""
    rows, digests = [], []
    for info, outcome in zip(infos, results):
        if isinstance(outcome, _Written):
            body = dict(outcome.info["data"])
            body["_id"], body["_rev"] = outcome.info["id"], outcome.info["rev"]
            if outcome.new_rev_deleted:
                body["_deleted"] = True
            stored = _store_attachments(body, outcome.winning)
            digests += stored
            rows.append(StoredBody(outcome.info["id"], outcome.info["rev"], body,
                                   _attachment_digests(body), outcome.stemmed, stored))
    return rows, digests


def write_batch(rows: list, slots) -> None:
    """Store one batch in the order the IndexedDB adapter's transaction stores
    it: pouchdb-adapter-idb 9.0.0 ``writeAttachments`` and ``saveAttachment``,
    ``writeDoc``/``finishDoc``, ``compactRevs``, ``insertAttachmentMappings``,
    driven by pouchdb-adapter-utils ``processDocs``.

    ``processDocs`` starts the records of every document together and continues
    a document's records only once the write of the one before is done. A
    transaction serves its requests in the order they are made, and each one's
    success or error event is dispatched before the next request is served;
    what a handler issues takes its place after every request already issued,
    and what a handler decides it decides from what the store holds then. So
    the body of a document's second record is stored after the first record of
    every other document, a record that stores attachment data is served after
    one that stores none, and a slot two documents write holds the body stored
    last in that order, which is not the last record of the batch. The trees
    are not touched here: a document's records are still applied one after
    another.

    ``slots`` is the store the transaction writes to: ``slot(doc_id, rev)`` is
    the key of a slot; ``stored(doc_id, rev)`` returns it where a body is
    stored in that slot, else ``None``; ``store_body(doc_id, rev, body)``;
    ``drop_body(key)``; ``following(key, after)`` is the first mapping of a
    slot after the one visited last, ``(position, digest)``, in the order the
    store holds them, or ``None``; ``map(digest, key)`` and ``unmap(digest,
    key)``; ``mapped(digest)`` for the number of mappings of a digest;
    ``hold(digest)``, ``held(digest)`` and ``forget(digest)`` for attachment
    data.
    """
    queue = deque()

    def request(action, succeeded=None):
        # A request does its work when it is served, and its success event is
        # dispatched before the next request is served.
        def serve():
            result = action()
            if succeeded is not None:
                succeeded(result)
        queue.append(serve)

    def write_doc(row, written):
        doc_id, rev = row.doc_id, row.rev
        key = slots.slot(doc_id, rev)

        def finish():
            # finishDoc: bySeqStore.put(doc). A taken key is a constraint error,
            # and afterPutDocError looks the key up and puts the body over it.
            def put_body():
                if slots.stored(doc_id, rev) is None:
                    slots.store_body(doc_id, rev, row.body)
                    after_put_doc()
                else:
                    taken()

            def taken():
                request(lambda: slots.stored(doc_id, rev),
                        lambda _found: request(lambda: slots.store_body(doc_id, rev, row.body), after_put_doc))
            queue.append(put_body)

        # writeAttachments: the data of every attachment that is not a stub is
        # stored before the body, one request each, a second one where it is new.
        pending = [len(row.data)]

        def saved(_result=None):
            pending[0] -= 1
            if not pending[0]:
                finish()

        for digest in row.data:
            def counted(count, digest=digest):
                if count:
                    saved()
                else:
                    request(lambda: slots.hold(digest), saved)
            request(lambda digest=digest: slots.held(digest), counted)
        if not row.data:
            finish()

        def after_put_doc(_result=None):
            # revsToDelete are compacted first, then the metadata is put.
            compact(row.stemmed)
            request(lambda: None, after_put_metadata)

        def compact(revs):
            # compactRevs: per revision a getKey request; where a body is
            # stored, it is deleted and a cursor walks the mappings of the slot.
            remaining, orphaned = [len(revs)], []

            def done():
                remaining[0] -= 1
                if not remaining[0]:
                    # deleteOrphanedAttachments: an attachment no slot maps is dropped.
                    for digest in orphaned:
                        def counted_orphaned(count, digest=digest):
                            if not count:
                                request(lambda: slots.forget(digest))
                        request(lambda digest=digest: slots.mapped(digest), counted_orphaned)

            def looked_up(key):
                # getKey: where a body is stored under the key, it is deleted and a
                # cursor opens over the mappings of the slot.
                if key is None:
                    done()
                    return
                request(lambda: slots.drop_body(key))
                request(lambda: slots.following(key, None), lambda first: stepped(key, first))

            def stepped(key, row):
                if row is None:
                    done()
                    return
                position, digest = row
                orphaned.append(digest)
                request(lambda: slots.unmap(digest, key))
                # cursor.continue(): the next row as the store holds it when that request is served.
                request(lambda: slots.following(key, position), lambda following: stepped(key, following))

            for rev in revs:
                request(lambda rev=rev: slots.stored(doc_id, rev), looked_up)

        def after_put_metadata(_result=None):
            # insertAttachmentMappings: a request per attachment, the document's
            # records continue once every one succeeded.
            digests = [digest for digest in row.attachments if digest is not None]
            pending = [len(digests)]

            def mapped(_result=None):
                pending[0] -= 1
                if not pending[0]:
                    written()

            for digest in digests:
                request(lambda digest=digest: slots.map(digest, key), mapped)
            if not digests:
                written()

    # processDocs: every document's records start together, in the order the
    # batch names the documents; each document's next record waits for the one before.
    groups: dict = {}
    for row in rows:
        groups.setdefault(row.doc_id, []).append(row)

    def start(records):
        position = [0]

        def next_doc():
            if position[0] < len(records):
                write_doc(records[position[0]], next_doc)
                position[0] += 1
        return next_doc

    for records in groups.values():
        start(records)()

    while queue:
        queue.popleft()()


def revs_diff(request: dict, tree_of) -> dict:
    """pouchdb-core ``revsDiff`` over stored trees: the revisions a device says it lacks."""
    missing: dict = {}
    for doc_id, revs in request.items():
        tree = tree_of(doc_id)
        if tree is None:
            missing[doc_id] = list(revs)
            continue
        absent, reported = list(revs), []

        def visit(is_leaf, pos, rev_hash, opts):
            rev = _concat(pos, "-", rev_hash)
            if rev in absent:
                absent.remove(rev)
                if not _strict(_get(opts, "status"), "available"):
                    reported.append(rev)

        _traverse(tree, visit)
        if reported or absent:
            missing[doc_id] = reported + absent
    return missing


class FilePlan:
    """What receiving one journal file does to the store.

    ``chunk_rows``/``chunk_trees`` are the chunk batch and ``rows``/``trees``
    the metadata batch. A device commits each batch before it goes on, so
    whatever is in ``chunk_rows`` is stored even when ``failed`` is set: the
    receive stage a device failed at, with ``refusal`` to raise afterwards.
    A file that applies has ``failed`` ``None``.
    """

    def __init__(self):
        self.chunk_rows: list = []
        self.chunk_trees: dict = {}
        self.chunk_attachments: list = []
        self.rows: list = []
        self.trees: dict = {}
        self.failed: str | None = None
        self.refusal: RecordRefused | None = None


def _is_chunk(record: dict) -> bool:
    return isinstance(record.get("_id"), str) and record["_id"].startswith("h:")


def plan_file(records: list, tree_of, limit: int = REVS_LIMIT, has_attachment=None) -> FilePlan:
    """Plan one journal file exactly as ``processDocumentsForReceive`` receives it.

    ``tree_of(doc_id)`` returns the document's stored tree or ``None`` and
    ``has_attachment(digest)`` whether the store already holds an attachment
    a stub names. Raises :class:`RecordRefused` where a device fails before
    writing anything (the chunk batch); everything a device fails after that
    commit is reported through :attr:`FilePlan.failed` and
    :attr:`FilePlan.refusal`, with the chunk batch in place.
    """
    plan = FilePlan()
    has_attachment = has_attachment or (lambda digest: False)
    for record in records:
        # The journal decoder (journalRecords.ts:19-27) hands the database
        # objects with a string _id: a chunk record has more than "h:" and a
        # string _rev, empty allowed, and string data; anything else needs a
        # non-empty string _rev.
        if not isinstance(record, dict):
            raise RecordRefused("not-an-object")
        doc_id = record.get("_id")
        if not isinstance(doc_id, str) or not doc_id:
            raise RecordRefused("invalid-id")
        if _is_chunk(record):
            if len(doc_id) <= 2 or not isinstance(record.get("_rev"), str) \
                    or not isinstance(record.get("data"), str):
                raise RecordRefused("invalid-chunk-record")
        elif not isinstance(record.get("_rev"), str) or not record["_rev"]:
            raise RecordRefused("invalid-rev")
    # Chunk batch: only identifiers the device does not hold, in journal order.
    absent = [record for record in records if _is_chunk(record) and tree_of(record["_id"]) is None]
    try:
        # selectChunksToSave drops _rev; core's bulkDocs then checks and cleans.
        infos = [parse_record(_prepared({key: value for key, value in record.items() if key != "_rev"}),
                              new_edits=True) for record in absent]
        for info in infos:
            _admit_attachments(info, has_attachment)
        results, plan.chunk_trees = _import_docs(infos, {}, True, limit)
    except RecordRefused as refused:
        raise RecordRefused(refused.reason, "chunk-write") from None
    plan.chunk_rows, plan.chunk_attachments = _written_rows(infos, results)
    if any(isinstance(result, RecordRefused) for result in results):
        plan.failed, plan.refusal = "chunk-write", RecordRefused("conflict", "chunk-write")
        return plan
    # From here on a device has committed its chunk batch: whatever fails in
    # the metadata phase leaves that batch in place and the file unprocessed.
    # Its attachments are in the store when the metadata batch is verified;
    # the metadata batch's own are not (verifyAttachments runs before any write).
    committed = frozenset(digest for digest in plan.chunk_attachments if isinstance(digest, str))

    def held(digest) -> bool:
        return (isinstance(digest, str) and digest in committed) or has_attachment(digest)

    try:
        plan = _plan_metadata(records, plan, tree_of, limit, held)
    except RecordRefused as refused:
        plan.failed, plan.refusal = refused.stage, refused
    return plan


def _plan_metadata(records: list, plan: FilePlan, tree_of, limit: int, has_attachment) -> FilePlan:
    # journalMetadataRevisions spreads an accumulated plain object: a document
    # named like a member of Object.prototype is read from the prototype and
    # is not iterable, which fails the receive before any metadata is judged.
    metadata = [record for record in records if not _is_chunk(record)]
    if any(record["_id"] in _OBJECT_PROTOTYPE for record in metadata):
        raise RecordRefused("object-key-id", "metadata-selection")
    request: dict = {}
    for record in metadata:
        request.setdefault(record["_id"], []).append(record["_rev"])
    missing = revs_diff(request, tree_of)
    save = [record for record in metadata if record["_id"] in missing and any(
        _strict(record["_rev"], rev) for rev in missing[record["_id"]])]
    for record in save:
        if not valid_rev(record["_rev"]):
            raise RecordRefused("invalid-rev")
    # _putLocal: a local document cannot arrive with a revision, so the record
    # is refused after the batch; the other records of the batch are still written.
    local = [record for record in save if is_local_id(record["_id"])]
    save = [record for record in save if not is_local_id(record["_id"])]
    infos = [parse_record(_prepared(record), new_edits=False) for record in save]
    for info in infos:
        _admit_attachments(info, has_attachment)

    def sort_key(record):
        revisions = record.get("_revisions", UNDEFINED)
        start = _finite(_number(_get(revisions, "start"))) if _truthy(revisions) else 0.0
        return _units(record["_id"]), start

    order = sorted(range(len(save)), key=lambda index: sort_key(save[index]))
    infos = [infos[index] for index in order]
    fetched = {}
    for doc_id in {info["id"] for info in infos}:
        tree = tree_of(doc_id)
        if tree is not None:
            fetched[doc_id] = _metadata(tree)
    results, plan.trees = _import_docs(infos, fetched, False, limit)
    plan.rows, _ = _written_rows(infos, results)
    if local or any(isinstance(result, RecordRefused) for result in results):
        plan.failed, plan.refusal = "metadata-write", RecordRefused("conflict", "metadata-write")
    return plan
