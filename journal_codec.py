"""Codec for Obsidian LiveSync journal files (``<ms>-docs.jsonl.gz``).

Wire format mirrors upstream commonlib exactly:

- Record framing (``src/replication/journal/journalRecords.ts``): the file is
  a UTF-8 JSONL stream; every record ends with ``\\n``. Chunk (leaf) records
  are compact lines ``~<id>\\x1f<escaped data>`` where ``<id>`` starts with
  ``h:``; every other record is a full-JSON metadata document that must carry
  a string ``_rev``. Parsing splits compact records on the *first*
  ``\\x1f`` only, so ``\\x1f`` is legal inside chunk data. JSON is strict in
  both directions, like ``JSON.stringify``/``JSON.parse``: NaN/Infinity are
  neither emitted nor accepted.
- Chunk-data escaping (``src/common/utils.ts``:
  ``escapeNewLineFromString``/``unescapeNewLineFromString``): strings that
  contain ``\\n`` or ``\\r`` are prefixed with ``\\f`` and have ``\\n``,
  ``\\r`` and ``\\`` escaped to ``\\n``, ``\\r``, ``\\\\``; strings without
  newlines are stored verbatim, except that this writer also escapes values
  starting with literal ``\\f`` to prevent upstream readers stripping it.
  The reader retains byte-exact upstream unescape semantics.
- Compression (``src/replication/journal/JournalSyncCore.ts`` via
  ``src/pouchdb/compress.ts``): payloads are deflated with
  ``wrappedDeflate(..., {level: 8})``, which wraps ``fflate.deflate``.
  fflate's ``deflate`` (0.8.2, the version commonlib pins) is *raw* DEFLATE
  (RFC 1951) — no zlib, gzip, or other container, despite the ``.gz`` file
  extension. The Python equivalent is ``zlib.compressobj(level=8,
  wbits=-15)``; for the vectors in this repo's tests (zlib 1.3.1) the
  output is byte-identical to fflate's.

Because raw DEFLATE has no checksum, :func:`inflate_journal` cannot detect
in-place corruption beyond stream-structure errors; truncated or foreign
streams raise :class:`ValueError` loudly instead of returning garbage.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field
from replay_limits import ReplayLimits, ReplayBudgetExceeded

# journalRecords.ts: RECORD_SPLIT and UNIT_SPLIT.
_RECORD_SPLIT = "\n"
_UNIT_SPLIT = "\x1f"
_COMPACT_PREFIX = "~"
# utils.ts escapeNewLineFromString: the literal two-character prefix "\f".
_ESCAPE_PREFIX = "\\f"

# fflate.deflate / fflate.inflate operate on unwrapped DEFLATE streams;
# wbits=-15 selects that mode for the zlib module.
_RAW_WBITS = -15
_DEFLATE_LEVEL = 8

# Per-file replay limits; callers may override keyword arguments. Byte limits
# apply before decoding UTF-8 or constructing JSON objects.
MAX_INFLATED_BYTES = ReplayLimits.max_inflated_bytes
MAX_RECORDS = ReplayLimits.max_records
MAX_JSON_DEPTH = ReplayLimits.max_json_depth
MAX_LINE_BYTES = ReplayLimits.max_line_bytes

_CORRUPT_BLOB_MESSAGE = "incompatible or corrupt journal blob"


@dataclass
class JournalFile:
    """A parsed journal file: chunk bodies keyed by id, plus metadata docs.

    Compact chunk records do not carry a revision on the wire (upstream
    decodes them with ``_rev: ""``), so only the unescaped chunk *data* is
    kept. Full-JSON records — metadata and any chunk record that arrived in
    JSON form — keep their ``_rev`` and land in :attr:`docs` untouched.
    """

    name: str
    chunks: dict[str, str] = field(default_factory=dict)
    docs: list[dict] = field(default_factory=list)


def _escape_chunk_data(value: str) -> str:
    """Escape for upstream readers, including ambiguous literal ``\\f`` starts.

    Upstream skips newline-free strings, but its reader always strips the
    prefix. Conservatively escape these values too (Ruling B-14).
    """
    if "\n" not in value and "\r" not in value and not value.startswith(_ESCAPE_PREFIX):
        return value
    escaped = value.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    return _ESCAPE_PREFIX + escaped


def _unescape_chunk_data(value: str) -> str:
    """Mirror upstream ``unescapeNewLineFromString`` (common/utils.ts:379).

    Implemented as a single left-to-right scan, like the upstream regex:
    chained ``str.replace`` calls would mis-decode ``\\\\n`` (escaped
    backslash followed by a literal ``n``) into a newline.
    """
    if not value.startswith(_ESCAPE_PREFIX):
        return value
    body = value[len(_ESCAPE_PREFIX) :]
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(char)
        i += 1
    return "".join(out)


def serialize_journal(chunks: list[tuple[str, str]], docs: list[dict]) -> bytes:
    """Serialize journal records to JSONL bytes: chunk lines, then metadata.

    Chunk pairs are ``(chunk_id, data)``. Metadata documents must carry a
    string ``_rev`` (non-chunk records without one are rejected upstream, so
    they are rejected here at write time). Output matches
    ``JSON.stringify``: compact separators, non-ASCII kept as UTF-8.
    """
    lines: list[str] = []
    for chunk_id, data in chunks:
        if not chunk_id.startswith("h:") or len(chunk_id) <= 2:
            raise ValueError(f"Invalid journal chunk identifier: {chunk_id!r}")
        if not isinstance(data, str):
            raise ValueError(f"Journal chunk {chunk_id!r} data must be a string")
        lines.append(f"{_COMPACT_PREFIX}{chunk_id}{_UNIT_SPLIT}{_escape_chunk_data(data)}")
    for doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get("_rev"), str):
            raise ValueError("Journal metadata document requires a string _rev")
        try:
            lines.append(
                json.dumps(
                    doc,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except ValueError as exc:
            # JSON.stringify cannot emit NaN/Infinity; Python's permissive
            # default must not leak them into a file devices refuse to parse.
            raise ValueError(
                f"Journal metadata document {doc.get('_id')!r}"
                " contains non-finite numbers"
            ) from exc
    if not lines:
        return b""
    return (_RECORD_SPLIT.join(lines) + _RECORD_SPLIT).encode("utf-8")


def deflate_journal(payload: bytes) -> bytes:
    """Deflate journal payload the way devices do: raw DEFLATE, level 8."""
    compressor = zlib.compressobj(_DEFLATE_LEVEL, zlib.DEFLATED, _RAW_WBITS)
    return compressor.compress(payload) + compressor.flush()


def inflate_journal(blob: bytes, *, max_output_bytes: int = MAX_INFLATED_BYTES) -> bytes:
    """Inflate a journal blob; raise ``ValueError`` on any corruption.

    Only the raw-DEFLATE format devices write is accepted (zlib- or
    gzip-wrapped streams are foreign input and fail loudly).
    """
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    decompressor = zlib.decompressobj(_RAW_WBITS)
    try:
        out = decompressor.decompress(blob, max_output_bytes + 1)
        if len(out) > max_output_bytes or decompressor.unconsumed_tail:
            raise ReplayBudgetExceeded("max_inflated_bytes", len(out), max_output_bytes)
    except zlib.error as exc:
        raise ValueError(_CORRUPT_BLOB_MESSAGE) from exc
    if not decompressor.eof or decompressor.unused_data:
        # Truncated stream or trailing garbage: raw DEFLATE has no checksum,
        # so structural completeness is the only corruption signal.
        raise ValueError(_CORRUPT_BLOB_MESSAGE)
    return out


def parse_journal(
    data: bytes, *, name: str = "", max_records: int = MAX_RECORDS,
    max_line_bytes: int = MAX_LINE_BYTES, max_json_depth: int = MAX_JSON_DEPTH,
) -> JournalFile:
    """Parse inflated journal bytes into a :class:`JournalFile`.

    ``name`` is optional error context (the journal file name) carried onto
    the result. Any invalid record raises ``ValueError`` naming the file and
    the 1-based line number, mirroring upstream's per-frame decode/parse
    failures. JSON is strict, like ``JSON.parse``: the NaN/Infinity literals
    Python's ``json`` module accepts by default are rejected.
    """
    result = JournalFile(name=name)
    if not data:
        return result
    if data[-1:] != _RECORD_SPLIT.encode("utf-8"):
        lineno = data.count(b"\n") + 1
        raise ValueError(
            f"{name}: line {lineno}: journal record must end with {_RECORD_SPLIT!r}"
        )
    # Scan offsets instead of splitting/copying every line before limits apply.
    start = 0
    lineno = 0
    while start < len(data):
        lineno += 1
        if lineno > max_records:
            raise ReplayBudgetExceeded("max_records", lineno, max_records)
        end = data.find(b"\n", start, start + max_line_bytes + 1)
        if end == -1:
            raise ReplayBudgetExceeded("max_line_bytes",
                                       data.find(b"\n", start) - start, max_line_bytes)
        raw_line = data[start:end]
        start = end + 1
        if not raw_line:
            raise ValueError(f"{name}: line {lineno}: empty journal record")
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{name}: line {lineno}: invalid UTF-8 record") from exc
        if line.startswith(_COMPACT_PREFIX):
            _parse_compact_line(result, name, lineno, line)
        else:
            _parse_metadata_line(result, name, lineno, line, max_json_depth)
    return result


def _parse_compact_line(
    target: JournalFile, name: str, lineno: int, line: str
) -> None:
    separator = line.find(_UNIT_SPLIT)
    chunk_id = line[1:separator] if separator != -1 else ""
    if separator == -1 or not chunk_id.startswith("h:") or len(chunk_id) <= 2:
        raise ValueError(f"{name}: line {lineno}: invalid journal chunk record")
    target.chunks[chunk_id] = _unescape_chunk_data(line[separator + 1 :])


def _reject_json_constant(name: str, lineno: int, token: str) -> None:
    """Reject NaN/Infinity/-Infinity like JSON.parse (callers get context)."""
    raise ValueError(f"{name}: line {lineno}: invalid JSON constant {token}")


def _metadata_depth(line: str, limit: int) -> int:
    """Measure nesting outside strings without using the recursive JSON parser."""
    depth = peak = 0
    quoted = escaped = False
    for char in line:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            peak = max(peak, depth)
            if depth > limit:
                raise ReplayBudgetExceeded("max_json_depth", depth, limit)
        elif char in "]}":
            depth -= 1
    return peak


def _parse_metadata_line(
    target: JournalFile, name: str, lineno: int, line: str, max_json_depth: int
) -> None:
    # The configured admission budget and Python decoder capacity are distinct.
    # Avoid scanning ordinary records twice when their bracket count is small.
    observed_depth = None
    if line.count("[") + line.count("{") > max_json_depth:
        observed_depth = _metadata_depth(line, max_json_depth)
    try:
        doc = json.loads(
            line,
            parse_constant=lambda token: _reject_json_constant(name, lineno, token),
        )
    except RecursionError as exc:
        import sys
        if observed_depth is None:
            observed_depth = _metadata_depth(line, max_json_depth)
        raise ReplayBudgetExceeded(
            "max_json_depth", observed_depth, max_json_depth,
            decoder_capacity=sys.getrecursionlimit(),
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name}: line {lineno}: invalid journal record") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{name}: line {lineno}: invalid journal record")
    doc_id = doc.get("_id")
    if not isinstance(doc_id, str) or not doc_id:
        raise ValueError(f"{name}: line {lineno}: invalid journal document identifier")
    rev = doc.get("_rev")
    if doc_id.startswith("h:"):
        # Upstream accepts full-JSON chunk records; they must look like leaf
        # documents (string _rev, string data). They keep their revision, so
        # they are stored alongside the metadata documents.
        looks_like_leaf = isinstance(rev, str) and isinstance(doc.get("data"), str)
        if len(doc_id) <= 2 or not looks_like_leaf:
            raise ValueError(f"{name}: line {lineno}: invalid journal chunk record")
    elif not isinstance(rev, str) or not rev:
        raise ValueError(f"{name}: line {lineno}: invalid journal metadata revision")
    target.docs.append(doc)


def journal_filename(
    ms: int, counter: int = 0, *, suffix: str | None = None,
) -> str:
    """Build a journal name with a numeric timestamp and optional suffix.

    Devices upload ``<ms>-docs.jsonl.gz`` (JournalSyncCore.ts:675,
    ``Date.now()``-based). Server publications use
    ``<ms>-<random 32 hex>-docs.jsonl.gz`` (Plan B ruling B-10), checking
    existence before upload and regenerating an occupied suffix. Replay
    orders by numeric ms prefix, then full name for deterministic ties.
    The zero-padded counter form remains available for legacy callers;
    it does not provide uniqueness across independent writers.
    """
    if suffix is not None:
        return f"{ms}-{suffix}-docs.jsonl.gz"
    if counter == 0:
        return f"{ms}-docs.jsonl.gz"
    return f"{ms}-{counter:04d}-docs.jsonl.gz"
