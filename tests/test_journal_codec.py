"""Tests for the journal file codec (``<ms>-docs.jsonl.gz`` wire format).

Byte-exact vectors mirror obsidian-livesync commonlib: record framing from
``src/replication/journal/journalRecords.ts``, chunk-data escaping from
``src/common/utils.ts`` (escapeNewLineFromString), and raw-DEFLATE blobs from
``fflate.deflate(data, {level: 8})`` (what ``wrappedDeflate`` wraps in
``src/pouchdb/compress.ts``). The fflate hex blobs below were produced by
fflate 0.8.2, the version pinned in commonlib's package.json; they are used
as inflate inputs — DEFLATE output is not canonical across implementations,
so only the record layer and mutual decompressibility are pinned, never
zlib-versus-fflate byte equality.
"""

from pathlib import Path
import zlib

import pytest

from journal_codec import (
    JournalFile,
    deflate_journal,
    inflate_journal,
    journal_filename,
    parse_journal,
    serialize_journal,
)

# --- fflate 0.8.2 deflateSync(..., {level: 8}) reference blobs (raw DEFLATE).

FFLATE_BLOB_SIMPLE = bytes.fromhex(
    "abcbb04a4c4a964f492c49e4ca48cdc9c95728cf2fca49514062730100"
)
FFLATE_PAYLOAD_SIMPLE = b"~h:abc\x1fdata\nhello world hello world\n"

FFLATE_BLOB_FULL = bytes.fromhex(
    "abcbb04a4c4a968f49bbb0ebc28e8b0d17765cd80d843b2eb65dd8a0909399976"
    "a189307a28ce4b353530bb8aa95e2335394ac94d2ac4a528b4bf47253947494e2"
    "8b52cb804286ba407380dc92cc929c5420ffc2f40b1b2eecb9b0f56213d0e40d4"
    "ab55c00"
)


class TestSerializeExactBytes:
    def test_compact_chunk_without_newline_is_verbatim(self):
        assert serialize_journal([("h:abc", "data")], []) == b"~h:abc\x1fdata\n"

    def test_compact_chunk_newline_is_escaped_with_prefix(self):
        assert (
            serialize_journal([("h:abc", "line1\nline2")], [])
            == b"~h:abc\x1f\\fline1\\nline2\n"
        )

    def test_compact_chunk_carriage_return_is_escaped(self):
        assert (
            serialize_journal([("h:x", "a\rb")], []) == b"~h:x\x1f\\fa\\rb\n"
        )

    def test_backslash_without_newline_stays_verbatim(self):
        # Upstream escape skips strings with no \n and no \r entirely, so a
        # lone backslash is neither doubled nor prefixed.
        assert serialize_journal([("h:x", "a\\b")], []) == b"~h:x\x1fa\\b\n"

    def test_backslash_with_newline_is_doubled(self):
        assert (
            serialize_journal([("h:x", "a\\b\nc")], [])
            == b"~h:x\x1f\\fa\\\\b\\nc\n"
        )

    def test_unit_separator_inside_data_is_not_escaped(self):
        # Only \n, \r and \\ are escaped; \x1f passes through (readers split
        # on the first separator only).
        assert (
            serialize_journal([("h:x", "pre\x1fpost")], [])
            == b"~h:x\x1fpre\x1fpost\n"
        )

    def test_metadata_json_is_compact_and_utf8(self):
        doc = {"_id": "f:test.md", "_rev": "1-abc", "title": "Заметка"}
        assert (
            serialize_journal([], [doc])
            == '{"_id":"f:test.md","_rev":"1-abc","title":"Заметка"}\n'.encode()
        )

    def test_chunks_come_before_metadata(self):
        blob = serialize_journal(
            [("h:a", "1")], [{"_id": "m", "_rev": "1-x"}]
        )
        assert blob == b'~h:a\x1f1\n{"_id":"m","_rev":"1-x"}\n'

    def test_empty_inputs_produce_empty_payload(self):
        assert serialize_journal([], []) == b""

    def test_metadata_without_rev_raises(self):
        with pytest.raises(ValueError, match="_rev"):
            serialize_journal([], [{"_id": "m"}])

    def test_metadata_with_non_string_rev_raises(self):
        with pytest.raises(ValueError, match="_rev"):
            serialize_journal([], [{"_id": "m", "_rev": 3}])

    def test_metadata_with_nan_raises(self):
        # JSON.stringify cannot emit NaN; Python's permissive default must
        # not leak it into a file devices will refuse to parse.
        with pytest.raises(ValueError, match="non-finite"):
            serialize_journal(
                [], [{"_id": "m", "_rev": "1-x", "score": float("nan")}]
            )

    def test_metadata_with_infinity_raises(self):
        with pytest.raises(ValueError, match="non-finite"):
            serialize_journal(
                [], [{"_id": "m", "_rev": "1-x", "score": float("inf")}]
            )

    def test_chunk_id_outside_chunk_namespace_raises(self):
        with pytest.raises(ValueError, match="chunk"):
            serialize_journal([("f:test.md", "data")], [])

    def test_bare_chunk_namespace_prefix_raises(self):
        with pytest.raises(ValueError, match="chunk"):
            serialize_journal([("h:", "data")], [])


class TestParse:
    def test_roundtrip_two_chunks_two_docs(self):
        chunks = [
            ("h:abc123", "line1\nline2 with \x1f unit separator"),
            ("h:def456", "plain\rcarriage\tback\\slash\n"),
        ]
        docs = [
            {"_id": "f:кириллица.md", "_rev": "2-xyz", "title": "Заметка"},
            {"_id": "obsydian_livesync_version", "_rev": "1-0", "version": 5},
        ]
        blob = deflate_journal(serialize_journal(chunks, docs))
        parsed = parse_journal(
            inflate_journal(blob), name="1700000000000-docs.jsonl.gz"
        )
        assert isinstance(parsed, JournalFile)
        assert parsed.name == "1700000000000-docs.jsonl.gz"
        assert parsed.chunks == dict(chunks)
        assert parsed.docs == docs

    def test_first_unit_separator_splits_compact_record(self):
        parsed = parse_journal(b"~h:x\x1fpre\x1fpost\n", name="j")
        assert parsed.chunks == {"h:x": "pre\x1fpost"}

    def test_escaped_backslash_then_n_is_not_newline(self):
        # dataPart "\fa\\nb" must decode to "a\\nb" (backslash + letter n),
        # which naive chained str.replace implementations get wrong.
        parsed = parse_journal(b"~h:x\x1f\\fa\\\\nb\n", name="j")
        assert parsed.chunks == {"h:x": "a\\nb"}

    def test_verbatim_backslash_chunk_parses(self):
        parsed = parse_journal(b"~h:x\x1fa\\b\n", name="j")
        assert parsed.chunks == {"h:x": "a\\b"}

    def test_empty_input_gives_empty_journal(self):
        parsed = parse_journal(b"")
        assert parsed.name == ""
        assert parsed.chunks == {}
        assert parsed.docs == []

    def test_missing_trailing_newline_raises_with_line_number(self):
        with pytest.raises(ValueError, match=r"j: line 1"):
            parse_journal(b"~h:abc\x1fdata", name="j")

    def test_missing_trailing_newline_after_records_raises_with_line_number(self):
        with pytest.raises(ValueError, match=r"j: line 2"):
            parse_journal(b"~h:abc\x1fdata\n~h:x\x1fy", name="j")

    def test_blank_line_raises(self):
        with pytest.raises(ValueError, match="line 2"):
            parse_journal(b"~h:abc\x1fdata\n\n", name="j")

    def test_compact_record_without_separator_raises(self):
        with pytest.raises(ValueError, match="line 1"):
            parse_journal(b"~h:abc\n", name="j")

    def test_compact_record_with_non_chunk_id_raises(self):
        with pytest.raises(ValueError, match="line 1"):
            parse_journal(b"~x\x1fdata\n", name="j")

    def test_invalid_utf8_line_raises(self):
        with pytest.raises(ValueError, match="line 1"):
            parse_journal(b"~h:x\x1f\xff\xfe\n", name="j")

    def test_json_array_line_raises(self):
        with pytest.raises(ValueError, match="line 1"):
            parse_journal(b"[1, 2]\n", name="j")

    def test_json_metadata_without_id_raises(self):
        with pytest.raises(ValueError, match="line 1"):
            parse_journal(b'{"_rev": "1-x"}\n', name="j")

    def test_full_json_chunk_record_lands_in_docs(self):
        # Upstream accepts full-JSON chunk (leaf) records; they keep their
        # revision, so they belong with the metadata documents.
        line = b'{"_id":"h:abc","_rev":"3-q","data":"chunk body"}\n'
        parsed = parse_journal(line, name="j")
        assert parsed.docs == [{"_id": "h:abc", "_rev": "3-q", "data": "chunk body"}]
        assert parsed.chunks == {}

    def test_full_json_chunk_record_without_string_data_raises(self):
        with pytest.raises(ValueError, match="line 1"):
            parse_journal(b'{"_id":"h:abc","_rev":"3-q","data":5}\n', name="j")

    def test_parse_rejects_missing_rev(self):
        with pytest.raises(ValueError, match="line 1"):
            parse_journal(b'{"_id":"f:test.md"}\n', name="j")

    def test_parse_rejects_empty_rev(self):
        with pytest.raises(ValueError, match="line 1"):
            parse_journal(b'{"_id":"f:test.md","_rev":""}\n', name="j")

    def test_parse_rejects_nan_token_with_line_number(self):
        # JSON.parse rejects NaN; so must we, with file and line context.
        line = b'{"_id":"m","_rev":"1-x"}\n{"_id":"n","_rev":"1-x","score":NaN}\n'
        with pytest.raises(ValueError, match=r"j: line 2.*NaN"):
            parse_journal(line, name="j")

    def test_parse_rejects_infinity_token_with_line_number(self):
        with pytest.raises(ValueError, match=r"j: line 1.*Infinity"):
            parse_journal(b'{"_id":"m","_rev":"1-x","score":Infinity}\n', name="j")

    def test_parse_line_number_in_error(self):
        payload = (
            b"~h:abc\x1fdata\n"
            b'{"_id":"f:ok.md","_rev":"1-x"}\n'
            b"this is not json\n"
        )
        with pytest.raises(ValueError, match=r"j: line 3"):
            parse_journal(payload, name="j")


class TestDeflateInflate:
    def test_deflate_output_is_raw_deflate(self):
        blob = deflate_journal(FFLATE_PAYLOAD_SIMPLE)
        # Pin our parameters: raw (RFC 1951) stream, no zlib/gzip wrapper.
        assert not blob.startswith((b"\x78", b"\x1f\x8b"))
        assert zlib.decompress(blob, -15) == FFLATE_PAYLOAD_SIMPLE

    def test_inflate_fflate_blob(self):
        assert inflate_journal(FFLATE_BLOB_SIMPLE) == FFLATE_PAYLOAD_SIMPLE

    def test_inflate_fflate_blob_escaped_and_cyrillic(self):
        payload = serialize_journal(
            [("h:abc", "кириллица line1\nline2\x1fkeep")],
            [{"_id": "f:test.md", "_rev": "1-abc", "title": "Заметка"}],
        )
        # A blob devices compressed must decode to exactly our records.
        assert inflate_journal(FFLATE_BLOB_FULL) == payload

    def test_deflate_inflate_roundtrip(self):
        payload = serialize_journal(
            [("h:a", "x\ny")], [{"_id": "m", "_rev": "1-x"}]
        )
        assert inflate_journal(deflate_journal(payload)) == payload

    def test_inflate_garbage_raises_loudly(self):
        with pytest.raises(ValueError, match="incompatible or corrupt journal blob"):
            inflate_journal(b"not a deflate stream")

    def test_inflate_truncated_blob_raises_loudly(self):
        with pytest.raises(ValueError, match="incompatible or corrupt journal blob"):
            inflate_journal(FFLATE_BLOB_SIMPLE[:12])

    def test_inflate_empty_blob_raises_loudly(self):
        # An empty journal serializes to zero bytes, but its DEFLATE stream
        # is never zero bytes; a zero-byte blob is foreign or corrupt.
        with pytest.raises(ValueError, match="incompatible or corrupt journal blob"):
            inflate_journal(b"")

    def test_inflate_trailing_garbage_raises_loudly(self):
        with pytest.raises(ValueError, match="incompatible or corrupt journal blob"):
            inflate_journal(FFLATE_BLOB_SIMPLE + b"\x00garbage")

    def test_inflate_zlib_wrapped_stream_is_rejected(self):
        # Devices write raw DEFLATE; a zlib-wrapped stream (0x78 header) is
        # not the journal format and must fail loudly, not silently decode.
        with pytest.raises(ValueError, match="incompatible or corrupt journal blob"):
            inflate_journal(zlib.compress(FFLATE_PAYLOAD_SIMPLE, 8))

    def test_compression_actually_shrinks_payload(self):
        chunks = [(f"h:{i:06d}", "same textual payload repeated\n" * 4) for i in range(20)]
        payload = serialize_journal(chunks, [])
        assert len(deflate_journal(payload)) < len(payload)


class TestJournalFilename:
    def test_random_suffix_preserves_numeric_prefix(self):
        assert journal_filename(123, suffix='abcdef0123456789') == (
            '123-abcdef0123456789-docs.jsonl.gz'
        )

    def test_filename_monotonic_suffix(self):
        assert journal_filename(1700000000000) == "1700000000000-docs.jsonl.gz"
        assert journal_filename(1700000000000, 1) == "1700000000000-0001-docs.jsonl.gz"
        assert journal_filename(1700000000000, 42) == "1700000000000-0042-docs.jsonl.gz"
        assert journal_filename(1700000000000, 1234) == "1700000000000-1234-docs.jsonl.gz"

    def test_zero_padding_keeps_counter_order(self):
        # Ordering holds among suffixed names (counter >= 1); the base name
        # sorts after them ('d' > '0'), which is fine — peers parse names.
        names = [journal_filename(123, i) for i in range(1, 13)]
        assert names == sorted(names)
        assert journal_filename(123, 9998) < journal_filename(123, 9999)


# Captured by scripts/live_check_s3.py from the real LiveSync client.
LIVE_CLIENT_SAMPLE = Path(__file__).parent / "fixtures/live_client_journal_sample.bin"


@pytest.mark.skipif(
    not LIVE_CLIENT_SAMPLE.exists(), reason="live client sample not captured yet"
)
def test_live_client_golden_decode():
    journal = parse_journal(inflate_journal(LIVE_CLIENT_SAMPLE.read_bytes()))
    assert len(journal.docs) >= 1
