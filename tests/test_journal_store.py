"""Tests for the journal local store: SQLite revision trees + chunk files.

Covers the interface consumed by the backend: ``JournalStore.apply_journal``
(files replayed as a device receives them, idempotent, refused as a whole
where a device refuses them), the resulting winner, leaves and conflicts,
dedupe resets that retain tables and chunk files, path identity quarantine on
reopen, and note reads (children order, eden inline chunks, legacy ``notes``
bodies, binary ``newnote`` bodies, E2EE refusal, per-note corruption isolation
in ``iterate_notes``). The revision rule itself is pinned against the published
PouchDB database in ``tests/test_journal_winner.py``; the cases here keep the
readable shape of it.
"""

import base64
import gzip
import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
from pathlib import Path
import unicodedata

import pytest

import journal_store
from journal_codec import parse_journal
from journal_store import JournalStore, _KEY_ESCAPED, _db_key, _db_unkey
from livesync_format import clone_metadata
from replay_limits import ReplayBudgetExceeded
from livesync_format import PATH_IDENTITY_LEGACY, PATH_IDENTITY_NFC
from revision_tree import (
    REVS_LIMIT, UNDEFINED, RecordRefused, _array_index, _get, _json_stringify, _JS_WHITESPACE,
    _parse_int, _string_number,
)
from storage_base import StorageError

JOURNAL_1 = "1700000000000-docs.jsonl.gz"
JOURNAL_2 = "1700000001000-docs.jsonl.gz"
JOURNAL_3 = "1700000002000-docs.jsonl.gz"

DB_PATH = "journal.sqlite3"


def meta_doc(doc_id, rev, *, path=None, children=(), doc_type="plain", **extra):
    """Minimal LiveSync metadata document as journal records carry it."""
    doc = {
        "_id": doc_id,
        "_rev": rev,
        "type": doc_type,
        "path": doc_id if path is None else path,
        "children": list(children),
        "eden": {},
    }
    doc.update(extra)
    return doc


@pytest.fixture()
def store(tmp_path):
    instance = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
    yield instance
    instance.close()


def same_number(actual, expected):
    return (math.isnan(expected) and math.isnan(actual)) or (
        actual == expected and math.copysign(1, actual) == math.copysign(1, expected))


def revision(rev, ids, **extra):
    """A record as journals carry it: its own hash first, then its ancestors."""
    return {"_id": "n.md", "_rev": rev, "_revisions": {"start": int(rev.split("-", 1)[0]), "ids": ids},
            **extra}


def leaf_revs(docs):
    return [doc["_rev"] for doc in docs]


class TestRevisionTree:
    def test_leaves_are_listed_highest_position_first_and_the_winner_is_live(self, store):
        # Two devices edit the same note offline and one edit is then discarded
        # by a revision tombstone under a third sibling.
        store.apply_journal(JOURNAL_1, {}, [
            revision("2-bbb", ["bbb", "aaa"]),
            revision("2-ccc", ["ccc", "aaa"]),
            revision("2-ddd", ["ddd", "aaa"]),
            revision("3-eee", ["eee", "ddd", "aaa"], _deleted=True),
        ])
        assert leaf_revs(store.leaves("n.md")) == ["3-eee", "2-bbb", "2-ccc"]
        assert [(doc["_rev"], doc.get("_deleted")) for doc in store.leaves("n.md")] == [
            ("3-eee", True), ("2-bbb", None), ("2-ccc", None)]
        assert store.winner("n.md")["_rev"] == "2-ccc"
        assert leaf_revs(store.conflicts("n.md")) == ["2-bbb"]
        assert [doc["_rev"] for doc in store.revisions("n.md")] == ["2-bbb", "2-ccc", "2-ddd", "3-eee"]

    def test_ancestors_named_by_a_later_revision_are_not_leaves(self, store):
        store.apply_journal(JOURNAL_1, {}, [
            revision("1-aaa", ["aaa"]), revision("3-ccc", ["ccc", "bbb", "aaa"]),
        ])
        assert leaf_revs(store.leaves("n.md")) == ["3-ccc"]

    def test_revision_comes_from_the_chain_not_from_rev(self, store):
        # parseDoc keeps the revision _revisions names, whatever _rev says.
        store.apply_journal(JOURNAL_1, {}, [revision("1-aaa", ["aaa"])])
        store.apply_journal(JOURNAL_2, {}, [revision("2-zzz", ["xxx", "aaa"])])
        assert leaf_revs(store.leaves("n.md")) == ["2-xxx"]

    def test_leaf_named_under_another_root_is_kept(self, store):
        # A record cannot hide a branch it does not descend from.
        store.apply_journal(JOURNAL_1, {}, [revision("2-bbb", ["bbb", "aaa"])])
        store.apply_journal(JOURNAL_2, {}, [revision("3-ccc", ["ccc", "bbb", "zzz"], _deleted=True)])
        assert leaf_revs(store.leaves("n.md")) == ["3-ccc", "2-bbb"]
        assert store.winner("n.md")["_rev"] == "2-bbb"

    def test_hash_order_is_the_utf16_order(self, store):
        store.apply_journal(JOURNAL_1, {}, [revision("1-\ue000", ["\ue000"])])
        store.apply_journal(JOURNAL_2, {}, [revision("1-\U0001f600", ["\U0001f600"])])
        assert store.winner("n.md")["_rev"] == "1-\ue000"

    def test_an_array_or_object_deletes_like_true(self, store):
        for flag in ([], {}):
            store.apply_journal(JOURNAL_1, {}, [revision(f"2-b{len(flag)}b", [f"b{len(flag)}b", "aaa"], _deleted=flag)])
        assert store.winner("n.md")["_deleted"] is True

    def test_history_is_stemmed_to_the_device_limit(self, store):
        # After the device's limit, a late ancestor is a new root and can win again.
        chain = [f"h{index}" for index in range(101, -1, -1)]
        store.apply_journal(JOURNAL_1, {}, [{"_id": "n.md", "_rev": "102-h101", "type": "plain",
                                             "path": "n.md", "children": [], "_deleted": True,
                                             "_revisions": {"start": 102, "ids": chain}}])
        assert store.winner("n.md")["_rev"] == "102-h101"
        store.apply_journal(JOURNAL_2, {}, [revision("1-h0", ["h0"], type="plain", path="n.md", children=[])])
        assert leaf_revs(store.leaves("n.md")) == ["102-h101", "1-h0"]
        assert store.winner("n.md")["_rev"] == "1-h0"
        # A device reports the stemmed path of a revision as its chain.
        assert store.revision_chain("n.md", "102-h101") == {"start": 102, "ids": chain[:REVS_LIMIT]}

    def test_numbers_and_strings_are_different_hashes(self, store):
        store.apply_journal(JOURNAL_1, {}, [revision("1-7", ["7"])])
        store.apply_journal(JOURNAL_2, {}, [revision("2-b", ["b", 7])])
        assert leaf_revs(store.leaves("n.md")) == ["2-b", "1-7"]

    @pytest.mark.parametrize("record, reason", [
        ({"_id": "n.md", "_rev": "bogus"}, "invalid-rev"),
        (revision("2-a-b", ["a-b", "aaa"]), "invalid-rev"),
        ({**revision("2-b", ["b", "aaa"]), "_custom": 1}, "doc-validation"),
        ({"_id": "_foo", "_rev": "1-a"}, "reserved-id"),
        ({"_id": "n.md", "_rev": "1-x", "_revisions": {"ids": ["x"]}}, "non-finite-number"),
        ({"_id": "n.md", "_rev": "1-x", "_revisions": {"start": 1, "ids": []}}, "missing-revision-id"),
        ({"_id": "n.md", "_rev": "1-x", "_revisions": {"start": float("inf"), "ids": ["x"]}},
         "non-finite-number"),
        ({"_id": "n.md", "_rev": "1-x", "_revisions": {"start": 1, "ids": [float("nan")]}},
         "non-finite-number"),
        ({"_id": "n.md", "_rev": "1-x", "_revisions": ["x"]}, "type-error"),
        # JavaScript's \d is ASCII only: an Arabic-Indic digit before the dash
        # is not a generation. (After the dash any character but "-" is a hash.)
        ({"_id": "n.md", "_rev": "\u0661-aaa"}, "invalid-rev"),
        ({"_id": "n.md", "_rev": "\u0661-aaa", "_revisions": {"start": 1, "ids": ["aaa"]}}, "invalid-rev"),
        # An object hash with its own toString has no primitive value.
        (revision("1-zzz", [{"toString": 1}]), "type-error"),
        (revision("1-zzz", [{"toString": {}, "valueOf": 1}]), "type-error"),
        # parseDoc keeps a supplied tree; only a well-formed one is replayed.
        ({"_id": "n.md", "_rev": "1-bbb", "_rev_tree": [{"pos": 1, "ids": "zzz"}]}, "malformed-tree"),
        ({"_id": "n.md", "_rev": "1-bbb", "_rev_tree": [{"pos": "1", "ids": ["x", {}, []]}]}, "malformed-tree"),
        ({"_id": "n.md", "_rev": "1-bbb", "_rev_tree": [{"pos": 1, "ids": ["x", {}, [{"toString": 1}, {}, []]]}]},
         "malformed-tree"),
    ])
    def test_record_a_device_refuses_leaves_nothing_behind(self, store, record, reason):
        store.apply_journal(JOURNAL_1, {}, [revision("1-aaa", ["aaa"])])
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal(JOURNAL_2, {}, [record, revision("2-bbb", ["bbb", "aaa"])])
        assert refused.value.reason == reason
        assert not store.processed(JOURNAL_2)
        assert leaf_revs(store.leaves("n.md")) == ["1-aaa"]


class TestNumberConversion:
    """Numbers as the engine holds them: ToNumber, parseInt and JSON's literals.

    The receive path converts ``_revisions.start`` with ToNumber and a revision's
    position with parseInt, and concatenates the start it was given. Every
    conversion and every limit the interpreter has is covered by the corpus in
    ``tests/test_journal_winner.py``; the cases here keep the readable shape.
    """

    @pytest.mark.parametrize("text, expected", [
        ("-2", -2.0), ("+2", 2.0), ("\u20092\u2009", 2.0), ("\u00a0-3\u2028", -3.0), ("\ufeff2", 2.0),
        ("-0", -0.0), ("-1e400", -math.inf), ("1e400", math.inf), ("-Infinity", -math.inf),
        ("0x10", 16.0), ("0x20000000000001", 9007199254740992.0), ("-0x1", math.nan),
        ("0x" + "f" * 300, math.inf), ("0b" + "1" * 1100, math.inf),
        ("9" * 5000, math.inf), ("-" + "9" * 5000, -math.inf), ("0" * 5000 + "1", 1.0),
        ("\u200b2", math.nan), ("1_0", math.nan), ("\u0662", math.nan),
    ])
    def test_number_of_a_string(self, text, expected):
        assert same_number(_string_number(text), expected)

    @pytest.mark.parametrize("text, expected", [
        ("-2", -2.0), ("-0", -0.0), ("\u2009-5", -5.0), (" 42abc", 42.0), ("1e3", 1.0), ("x", math.nan),
        ("9" * 5000, math.inf), ("-" + "9" * 5000, -math.inf), ("0" * 5000 + "1", 1.0),
    ])
    def test_parse_int_of_a_string(self, text, expected):
        assert same_number(_parse_int(text), expected)

    def test_the_trimmed_characters_are_not_a_range(self):
        # Spelled as "\u2000-\u200a" inside a string, the hyphen would be trimmed
        # as well: every negative number lost its sign.
        assert len(_JS_WHITESPACE) == 25 and "-" not in _JS_WHITESPACE
        assert all(0x2000 <= ord(char) <= 0x200A for char in _JS_WHITESPACE[8:19])

    def test_a_long_index_key_is_an_ordinary_key(self):
        # Only a canonical decimal below 2**32 - 1 is an array index; a longer
        # digit string is never converted, which the interpreter could not do.
        assert [_array_index(key) for key in ("0", "4294967294", "4294967295", "9" * 5000, "01")] == [
            0, 4294967294, None, None, None]
        assert _get([1, 2], "9" * 5000) is UNDEFINED and _get([1, 2], "4294967295") is UNDEFINED
        # JSON.stringify orders indices first and keeps every other key where it is.
        assert _json_stringify({"b": 1, "9" * 5000: "x", "10": "y", "2": "z", "a": 3}) == (
            '{"2":"z","10":"y","b":1,"' + "9" * 5000 + '":"x","a":3}')

    @pytest.mark.parametrize("start", ["1e400", "-1e400", "0x" + "f" * 300, "Infinity", "9" * 5000, "NaN"])
    def test_a_position_beyond_every_double_is_refused_after_the_chunk_batch(self, store, start):
        chunk = {"_id": "h:own", "_rev": "1-x", "type": "leaf", "data": "own"}
        record = {"_id": "n.md", "_rev": "1-x", "type": "plain", "path": "n.md", "children": [],
                  "_revisions": {"start": start, "ids": ["x"]}}
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal(JOURNAL_1, {}, [chunk, record])
        assert (refused.value.reason, refused.value.stage) == ("non-finite-number", "metadata-write")
        assert store._tree("h:own") is not None and store._tree("n.md") is None
        assert not store.processed(JOURNAL_1)

    def test_a_negative_position_is_held_and_cannot_be_read(self, store):
        # A device keeps the start it was given in the revision and the converted
        # one in the tree; core get splits "-2-x" at every hyphen and looks for
        # the hash "2", which no tree names.
        store.apply_journal(JOURNAL_1, {}, [{"_id": "n.md", "_rev": "1-x", "type": "plain", "path": "n.md",
                                             "children": [], "_revisions": {"start": "-2", "ids": ["x"]}}])
        assert store._tree("n.md") == [[-2.0, ["x", {"status": "available"}, []]]]
        assert leaf_revs(store.leaves("n.md")) == ["-2-x"]
        with pytest.raises(journal_store.NoteDecodeError):
            store.winner("n.md")

    def test_a_hash_with_a_hyphen_is_named_by_its_first_part(self, store):
        # The record's own _rev is valid; the tree's leaf is named "1-a-b".
        store.apply_journal(JOURNAL_1, {}, [{"_id": "n.md", "_rev": "1-a", "type": "plain",
                                             "_revisions": {"start": 1, "ids": ["a-b"]}}])
        assert leaf_revs(store.leaves("n.md")) == ["1-a-b"]
        with pytest.raises(journal_store.NoteDecodeError):
            store.winner("n.md")
        # The revision of a child is "2-c"; its hash "c" is named.
        store.apply_journal(JOURNAL_2, {}, [{"_id": "n.md", "_rev": "2-c", "type": "plain",
                                             "_revisions": {"start": 2, "ids": ["c", "a-b"]}}])
        assert store.winner("n.md")["_rev"] == "2-c"
        assert store.revision_chain("n.md", "2-c") == {"start": 2, "ids": ["c", "a-b"]}

    @pytest.mark.parametrize("record, revision", [
        ({"_id": "h:abc", "_rev": "", "type": "leaf", "data": "abc", "mtime": float("1e999")},
         "1-c549f3de5ae50375e440bc43a86f0582"),
        ({"_id": "h:neg", "_rev": "", "type": "leaf", "data": "abc", "mtime": float("-1e999")},
         "1-48c2e07f01542099973eeec5907e7fc2"),
        ({"_id": "h:arr", "_rev": "", "type": "leaf", "data": "abc", "x": [float("inf")]},
         "1-ef47be99d7de003cbc9cb3f4da7e23b2"),
        ({"_id": "h:obj", "_rev": "", "type": "leaf", "data": "abc", "x": {"y": float("inf")}},
         "1-6e5676f0608aac3d53b24cd79d3b318e"),
        ({"_id": "h:big", "_rev": "", "type": "leaf", "data": "abc", "n": float("inf")},
         "1-7ec4f454f1d551c9530e2d21ed86715b"),
    ])
    def test_a_chunk_revision_hashes_a_number_that_is_not_finite_as_null(self, store, record, revision):
        # The deterministic revision is the hash of JSON.stringify's spelling
        # of the record, and JSON.stringify writes such a number as null, at
        # any depth. The revisions are the published database's own.
        store.apply_journal(JOURNAL_1, {}, [record])
        assert store.winner(record["_id"])["_rev"] == revision

    @pytest.mark.parametrize("literal, expected", [
        (b"9" * 5000, math.inf), (b"-" + b"9" * 5000, -math.inf), (b"1e999", math.inf),
        # Inside the interpreter's digit limit and beyond every double.
        (b"9" * 400, math.inf), (b"-" + b"9" * 400, -math.inf), (b"-" + b"1" + b"0" * 309, -math.inf),
        (b"9007199254740993", 9007199254740992.0), (b"12345678901234567890", 12345678901234567000.0),
        (b"123456789012345680000", 1.2345678901234568e+20), (b"9007199254740992", 9007199254740992),
        (b"1700000000000", 1700000000000),
    ])
    def test_an_integer_literal_is_the_double_json_parse_holds(self, store, literal, expected):
        # The interpreter converts at most a configured number of decimal digits;
        # a device rounds every number to a double whatever its length.
        line = b'{"_id":"n.md","_rev":"1-a","type":"plain","path":"n.md","children":[],"mtime":' + literal + b'}'
        assert parse_journal(line + b"\n").docs[0]["mtime"] == expected
        store.apply_journal(JOURNAL_1, {}, parse_journal(line + b"\n").docs)
        # IndexedDB keeps a number beyond the double range in the body it stores.
        assert store.winner("n.md")["mtime"] == expected


class TestStoredNames:
    """Document, revision and digest names as the state file holds them."""

    @pytest.mark.parametrize("text", [
        "1-a", "1-\ud800", "\x00units:x", "\x00units:\\u0041", "a\\b\udfff", "\ud83d\ude00",
        "\udbff\udbffx", "\x00units:", "x\udcff\x00units:\\",
    ])
    def test_a_key_with_a_lone_utf16_unit_is_stored_losslessly(self, text):
        # Only a name with a lone unit, or one that starts like a spelled-out
        # name, is spelled out; every other name is stored as it is.
        spelled = text.startswith("\x00units:") or any(0xD800 <= ord(char) <= 0xDFFF for char in text)
        assert (_db_key(text) != text) == spelled
        _db_key(text).encode("utf-8")  # What SQLite can hold.
        assert _db_unkey(_db_key(text)) == text

    def test_names_with_a_lone_utf16_unit_survive_the_state_file(self, store):
        # A string used as ids names its UTF-16 units; a hash or an id may be one.
        store.apply_journal(JOURNAL_1, {}, [
            meta_doc("\ud800", "1-\udcff"), meta_doc("n.md", "1-a"),
            meta_doc("astral.md", "2-\ud83d", _revisions={"start": 2, "ids": "\ud83d\ude00"})])
        assert leaf_revs(store.leaves("\ud800")) == ["1-\udcff"]
        assert leaf_revs(store.leaves("astral.md")) == ["2-\ud83d"]
        assert store.note_ids() == ["astral.md", "n.md", "\ud800"]
        store.close()
        reopened = JournalStore(store.state_dir, PATH_IDENTITY_LEGACY)
        try:
            assert reopened.winner("\ud800")["_rev"] == "1-\udcff"
            assert reopened.note_ids() == ["astral.md", "n.md", "\ud800"]
        finally:
            reopened.close()


def nested(depth: int, leaf="x"):
    """A value wrapped in ``depth`` one-element arrays."""
    for _ in range(depth):
        leaf = [leaf]
    return leaf


class TestDeepRecords:
    """A record nested as deep as the decoder admits is held as a device holds it.

    The decoder's budget is 512 container levels. The port's walks keep their
    own stacks, so the depth of the interpreter's call stack takes no part.
    The revisions are the published database's own.
    """

    @staticmethod
    def frames(*records):
        return "".join(json.dumps(record) + "\n" for record in records).encode()

    @pytest.mark.parametrize("depth, revision", [
        (505, "1-c5bc70f44f6efb87e3b4f0d9b5bbff87"),
        (507, "1-5651420950509c53620de5f4650508ce"),
    ])
    def test_a_deep_chunk_gets_its_revision_and_survives_reopening(self, tmp_path, depth, revision):
        # The fifth-round architecture review's case.
        record = {"_id": "h:deep", "_rev": "", "type": "leaf", "data": "x", "extra": nested(depth)}
        journal = parse_journal(self.frames(record), name="deep")
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
        try:
            store.apply_journal("deep", journal.chunks, journal.docs, records=journal.records)
            assert store.winner("h:deep")["_rev"] == revision
            assert store.winner("h:deep")["extra"] == nested(depth)
            assert store.processed("deep")
        finally:
            store.close()
        reopened = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
        try:
            assert reopened.winner("h:deep")["_rev"] == revision
            assert reopened.winner("h:deep")["extra"] == nested(depth)
            assert reopened.revision_chain("h:deep", revision) == {"start": 1, "ids": [revision[2:]]}
        finally:
            reopened.close()

    def test_a_deep_hash_keeps_the_chunk_batch_and_names_its_revision_as_the_engine_does(self, tmp_path):
        # The hash is an array nested 505 deep: its string is "x". The chunk
        # before it is held, as a device holds it; the tree names the array,
        # not the string, so the note is not served, as a device's get fails.
        chunk = {"_id": "h:abc", "_rev": "", "type": "leaf", "data": "x"}
        metadata = {"_id": "n.md", "_rev": "1-a", "type": "plain", "path": "n.md", "children": [],
                    "_revisions": {"start": 1, "ids": [nested(505)]}}
        journal = parse_journal(self.frames(chunk, metadata), name="deep")
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
        try:
            store.apply_journal("deep", journal.chunks, journal.docs, records=journal.records)
            assert store.winner("h:abc")["data"] == "x"
            assert [leaf["_rev"] for leaf in store.leaves("n.md")] == ["1-x"]
            with pytest.raises(ValueError, match="does not name"):
                store.winner("n.md")
            assert store.processed("deep")
        finally:
            store.close()

    def test_an_array_hash_names_its_revision_as_the_engine_does(self, store):
        store.apply_journal(JOURNAL_1, {}, [{"_id": "n.md", "_rev": "1-a", "type": "plain", "path": "n.md",
                                             "children": [], "_revisions": {"start": 1, "ids": [[None, "a", [True, [], 1.5]]]}}])
        assert leaf_revs(store.leaves("n.md")) == ["1-,a,true,,1.5"]

    @pytest.mark.parametrize("depth", [200, 420, 505, 511])
    def test_nesting_to_the_decoder_budget_is_held(self, store, depth):
        record = meta_doc("deep.md", "1-a", extra=nested(depth, 7))
        store.apply_journal(JOURNAL_1, {}, parse_journal(self.frames(record), name="deep").docs)
        assert store.winner("deep.md")["extra"] == nested(depth, 7)

    def test_nesting_beyond_the_decoder_budget_is_refused_before_the_port(self):
        with pytest.raises(ReplayBudgetExceeded):
            parse_journal(self.frames(meta_doc("deep.md", "1-a", extra=nested(513))), name="deep")

    def test_a_deep_value_behind_an_attachment_is_held(self, store):
        digest = "md5-" + base64.b64encode(hashlib.md5(b"x").digest()).decode("ascii")
        store.apply_journal(JOURNAL_1, {}, [{"_id": "deep.md", "_rev": "1-a", "type": "plain", "path": "deep.md", "children": [],
                                             "_attachments": {"x": {"content_type": "text/plain", "data": "eA==", "digest": nested(505, "d")}}}])
        assert store.winner("deep.md")["_attachments"]["x"]["digest"] == digest

    def test_a_deep_field_does_not_stop_a_write_over_it(self, store):
        store.apply_journal(JOURNAL_1, {}, [meta_doc("deep.md", "1-a", extra=nested(505), data="old")])
        stored = store.winner("deep.md")
        copied = clone_metadata(stored, data="new")
        assert copied["extra"] == nested(505)
        # The copy shares no nested value with the body it was made from.
        assert copied["extra"] is not stored["extra"] and copied["extra"][0] is not stored["extra"][0]
        copied["extra"][-1] = "changed"
        assert stored["extra"] == nested(505)


class TestLegacyState:
    """State written by releases that stored every record under its ``_rev``."""

    @staticmethod
    def legacy(state, records):
        conn = sqlite3.connect(state / DB_PATH)
        conn.executescript("""
            CREATE TABLE docs (doc_id TEXT NOT NULL, gen INTEGER NOT NULL, rev_hash TEXT NOT NULL,
                doc_json TEXT NOT NULL, PRIMARY KEY (doc_id, gen, rev_hash));
            CREATE TABLE meta (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
        """)
        conn.execute("INSERT INTO meta VALUES ('path_identity', ?)", (PATH_IDENTITY_LEGACY,))
        for record in records:
            generation, _, rev_hash = record["_rev"].partition("-")
            conn.execute("INSERT INTO docs VALUES (?, ?, ?, ?)",
                         (record["_id"], int(generation), rev_hash, json.dumps(record)))
        conn.commit()
        conn.close()

    def test_records_are_replayed_again_under_the_kept_revision(self, tmp_path):
        self.legacy(tmp_path, [
            revision("1-aaa", ["aaa"]),
            revision("2-bbb", ["bbb", "aaa"]),
            revision("2-ccc", ["ccc", "aaa"], deleted=True),
            revision("3-dddd", ["dddd", "ccc", "aaa"], _deleted=True),
            # Kept by a device as "2-mmm": _revisions names the revision.
            revision("2-zzz", ["mmm", "aaa"]),
        ])
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert leaf_revs(store.leaves("n.md")) == ["3-dddd", "2-bbb", "2-mmm"]
            assert store.winner("n.md")["_rev"] == "2-mmm"
            assert [doc["_rev"] for doc in store.revisions("n.md")] == [
                "1-aaa", "2-bbb", "2-ccc", "3-dddd", "2-mmm", "2-zzz"]
            assert store.revision_chain("n.md", "3-dddd") == {"start": 3, "ids": ["dddd", "ccc", "aaa"]}
        finally:
            store.close()
        # The migrated layout is what later releases open directly.
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert store.winner("n.md")["_rev"] == "2-mmm"
            columns = {row[1] for row in store._conn.execute("PRAGMA table_info(docs)")}
            assert columns == {"seq_key", "doc_id", "rev", "doc_json"}
        finally:
            store.close()

    def test_a_refused_legacy_batch_moves_the_state_aside(self, tmp_path):
        # A local document fails every batch it is in; the migration must not
        # keep such a record as migrated state.
        self.legacy(tmp_path, [revision("1-aaa", ["aaa"]), {"_id": "_local/x", "_rev": "0-1", "value": 1}])
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert store.winner("n.md") is None
            archives = list((tmp_path.parent / (tmp_path.name + ".quarantine")).glob("*/state"))
            assert len(archives) == 1
        finally:
            store.close()

    def test_a_stub_may_name_an_attachment_an_earlier_row_stored(self, tmp_path):
        # A row carrying an attachment and a later row holding its stub are
        # records a device accepts in that order; the migration sees the
        # digest it has just written.
        digest = "md5-" + base64.b64encode(hashlib.md5(b"x").digest()).decode("ascii")
        self.legacy(tmp_path, [
            {"_id": "a", "_rev": "1-a", "type": "plain", "path": "a.md", "children": [],
             "_attachments": {"x": {"content_type": "text/plain", "data": "eA=="}}},
            {"_id": "b", "_rev": "1-b", "type": "plain", "path": "b.md", "children": [],
             "_attachments": {"x": {"stub": True, "content_type": "text/plain", "digest": digest}}},
        ])
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert store.winner("a")["_rev"] == "1-a" and store.winner("b")["_rev"] == "1-b"
            assert store.winner("a")["_attachments"]["x"]["digest"] == digest
            assert not (tmp_path.parent / (tmp_path.name + ".quarantine")).exists()
        finally:
            store.close()

    @staticmethod
    def stored_bodies(store):
        """The markers of every body the state holds for ``n.md``: those under
        a stored revision, and those kept beside."""
        bodies = [json.loads(row[0]) for row in store._conn.execute(
            "SELECT doc_json FROM docs WHERE doc_id = ?", (_db_key("n.md"),))]
        kept = store._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'legacy_bodies'"
        ).fetchone()
        beside = [json.loads(row[0]) for row in store._conn.execute(
            "SELECT doc_json FROM legacy_bodies WHERE doc_id = ?", ("n.md",))] if kept else []
        return (sorted(body.get("marker") for body in bodies),
                sorted(body.get("marker") for body in beside))

    def test_a_body_a_merge_stems_away_is_kept_as_it_was(self, tmp_path):
        # The later row's replay stems the first row's revision out of the tree. IDB
        # compacts that slot; the body the old state held is kept beside before the
        # old table goes.
        chain = ["h" + str(step) for step in range(100, 0, -1)]
        rows = [
            {"_id": "n.md", "_rev": "1-aaa", "type": "plain", "path": "n.md", "children": [], "marker": "ROOT"},
            {"_id": "n.md", "_rev": "101-h100", "type": "plain", "path": "n.md", "children": [], "marker": "LEAF",
             "_revisions": {"start": 101, "ids": [*chain, "aaa"]}},
        ]
        self.legacy(tmp_path, rows)
        for _ in range(2):
            store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
            try:
                assert self.stored_bodies(store) == (["LEAF"], ["ROOT"])
                assert store.winner("n.md")["_rev"] == "101-h100" and store._body("n.md", "1-aaa") is None
            finally:
                store.close()

    def test_an_escaped_slot_name_is_tracked_in_one_spelling(self, tmp_path):
        # The seventh-round parity review's case: a document whose name the state
        # file stores escaped. A later row's replay stems its first revision away;
        # the body of that row is kept beside, as for any other name.
        name = _KEY_ESCAPED + "n.md"
        chain = ["h" + str(step) for step in range(100, 0, -1)]
        self.legacy(tmp_path, [
            {"_id": name, "_rev": "1-aaa", "type": "plain", "path": "n.md", "children": [], "marker": "ROOT"},
            {"_id": name, "_rev": "101-h100", "type": "plain", "path": "n.md", "children": [], "marker": "LEAF",
             "_revisions": {"start": 101, "ids": [*chain, "aaa"]}},
        ])
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            stored = {json.loads(row[0])["marker"] for row in store._conn.execute("SELECT doc_json FROM docs")}
            kept = {json.loads(row[0])["marker"] for row in store._conn.execute(
                "SELECT doc_json FROM legacy_bodies WHERE doc_id = ?", (name,))}
            assert stored == {"LEAF"} and kept == {"ROOT"}
        finally:
            store.close()

    def test_a_body_the_replay_changes_is_kept_as_it_was(self, tmp_path):
        # The seventh-round parity review's case: the replay makes a stub of a record's
        # attachment data, so the record as the old state held it is kept beside, its
        # bytes included. A record the replay stores as it was is not kept twice.
        digest = "md5-" + base64.b64encode(hashlib.md5(b"x").digest()).decode("ascii")
        with_data = {"_id": "n.md", "_rev": "1-aaa", "type": "plain", "path": "n.md", "children": [], "marker": "ATT",
                     "_attachments": {"x": {"data": "eA==", "content_type": "text/plain"}}}
        plain = {key: value for key, value in with_data.items() if key != "_attachments"}
        self.legacy(tmp_path, [with_data])
        (tmp_path / "plain").mkdir()
        self.legacy(tmp_path / "plain", [plain])
        store, other = JournalStore(tmp_path, PATH_IDENTITY_LEGACY), JournalStore(tmp_path / "plain", PATH_IDENTITY_LEGACY)
        try:
            assert "data" not in store.winner("n.md")["_attachments"]["x"] and store._has_attachment(digest)
            originals = [json.loads(row[0]) for row in store._conn.execute("SELECT doc_json FROM legacy_bodies")]
            assert originals == [with_data]
            assert other.winner("n.md")["marker"] == "ATT" and self.stored_bodies(other) == (["ATT"], [])
        finally:
            store.close()
            other.close()

    def test_a_record_with_a_kept_field_is_not_kept_twice(self, tmp_path):
        # The eighth-round architecture review's case: a data word such as _access, or an
        # attachment that already is a stub, is stored as it was written; that is not a change.
        digest = "md5-" + base64.b64encode(hashlib.md5(b"x").digest()).decode("ascii")
        held = {"_id": "k.md", "_rev": "1-kkk", "type": "plain", "path": "k.md", "children": [], "marker": "DATA",
                "_attachments": {"x": {"data": "eA==", "content_type": "text/plain"}}}
        record = {"_id": "n.md", "_rev": "1-aaa", "type": "plain", "path": "n.md", "children": [], "marker": "KEPT",
                  "_access": True, "_attachments": {"x": {"stub": True, "digest": digest, "content_type": "text/plain"}}}
        self.legacy(tmp_path, [held, record])
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert store.winner("n.md")["_access"] is True
            # The data the replay made a stub of is kept beside; the stub is not kept twice.
            assert self.stored_bodies(store) == (["KEPT"], [])
            kept = [json.loads(row[0])["marker"] for row in store._conn.execute("SELECT doc_json FROM legacy_bodies")]
            assert kept == ["DATA"]
        finally:
            store.close()

    def test_an_unchanged_deletion_is_not_kept_twice(self, tmp_path):
        # The ninth-round architecture review's case: a record that carries a revision
        # deletion is stored with that stamp from its tree, as it was written, and is
        # not kept twice.
        self.legacy(tmp_path, [
            revision("1-aaa", ["aaa"]),
            revision("2-ccc", ["ccc", "aaa"]),
            revision("3-ddd", ["ddd", "ccc", "aaa"], marker="TOMB", _deleted=True),
        ])
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert store.winner("n.md")["_deleted"] is True
            assert [json.loads(row[0]).get("marker") for row in store._conn.execute(
                "SELECT doc_json FROM docs WHERE doc_id = ?", (_db_key("n.md"),))].count("TOMB") == 1
            kept = store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'legacy_bodies'").fetchone()
            assert kept is None or store._conn.execute("SELECT COUNT(*) FROM legacy_bodies").fetchone()[0] == 0
        finally:
            store.close()

    def test_a_layout_no_release_wrote_is_kept_aside(self, tmp_path, caplog):
        # A development build between the releases stored bodies by revision. The
        # directory is moved beside a fresh one, which the journal rebuilds.
        conn = sqlite3.connect(tmp_path / DB_PATH)
        conn.executescript("""
            CREATE TABLE docs (doc_id TEXT NOT NULL, rev TEXT NOT NULL, doc_json TEXT NOT NULL,
                PRIMARY KEY (doc_id, rev));
            CREATE TABLE meta (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
        """)
        conn.execute("INSERT INTO meta VALUES ('path_identity', ?)", (PATH_IDENTITY_LEGACY,))
        conn.execute("INSERT INTO docs VALUES ('n.md', '1-a', ?)", (json.dumps({"_id": "n.md", "_rev": "1-a"}),))
        conn.commit()
        conn.close()
        with caplog.at_level(logging.WARNING):
            store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert store.winner("n.md") is None and store._tree("n.md") is None
            kept = list(tmp_path.with_name(tmp_path.name + ".quarantine").glob("*/state"))
            assert len(kept) == 1
            old = sqlite3.connect(kept[0] / DB_PATH)
            try:
                assert [row[1] for row in old.execute("PRAGMA table_info(docs)")] == ["doc_id", "rev", "doc_json"]
                assert old.execute("SELECT doc_id, rev FROM docs").fetchall() == [("n.md", "1-a")]
            finally:
                old.close()
            assert "written by an unreleased build" in caplog.text and "n.md" not in caplog.text
        finally:
            store.close()

    def test_a_body_the_replay_displaces_is_kept_as_it_was(self, tmp_path):
        # Two rows whose replay lands on one kept revision. The later row takes
        # that revision, as the device's own rule has it; the body that was
        # there is kept beside, and survives the reopening, before the old
        # table goes (the fourth-round parity review's case).
        numeric = {"start": 1, "ids": [7]}
        rows = [
            {"_id": "n.md", "_rev": "1-7", "type": "plain", "path": "n.md", "children": [],
             "marker": "FIRST", "_revisions": numeric},
            {"_id": "n.md", "_rev": "1-b", "type": "plain", "path": "n.md", "children": [],
             "marker": "SECOND", "_revisions": numeric},
        ]
        self.legacy(tmp_path, rows)
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            held = {rev: json.loads(body)["marker"]
                    for rev, body in store._conn.execute("SELECT rev, doc_json FROM docs")}
            assert held == {"1-7": "SECOND", "1-b": "SECOND"}
            assert self.stored_bodies(store) == (["SECOND", "SECOND"], ["FIRST"])
        finally:
            store.close()
        reopened = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert self.stored_bodies(reopened) == (["SECOND", "SECOND"], ["FIRST"])
            assert not (tmp_path.parent / (tmp_path.name + ".quarantine")).exists()
        finally:
            reopened.close()

    def test_a_row_the_replay_already_knows_keeps_its_body(self, tmp_path):
        # The second row names a revision the tree holds: the replay writes
        # nothing, and the revision it was stored under holds another body.
        self.legacy(tmp_path, [
            {"_id": "n.md", "_rev": "1-b", "type": "plain", "path": "n.md", "children": [],
             "marker": "X", "_revisions": {"start": 1, "ids": ["a"]}},
            {"_id": "n.md", "_rev": "1-a", "type": "plain", "path": "n.md", "children": [],
             "marker": "Y"},
        ])
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert self.stored_bodies(store) == (["X", "X"], ["Y"])
        finally:
            store.close()

    def test_ordinary_histories_are_kept_beside_nothing(self, tmp_path):
        self.legacy(tmp_path, [revision("1-aaa", ["aaa"]), revision("2-bbb", ["bbb", "aaa"])])
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            tables = {row[0] for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert "legacy_bodies" not in tables and "docs_legacy" not in tables
            assert leaf_revs(store.leaves("n.md")) == ["2-bbb"]
            assert store.winner("n.md")["_rev"] == "2-bbb"
        finally:
            store.close()

    def test_schema_is_inspected_under_the_write_lock(self, tmp_path):
        # A second process opening during a migration waits here and then
        # finds the migrated schema instead of renaming the new table.
        self.legacy(tmp_path, [revision("1-aaa", ["aaa"])])
        statements = []
        connect = journal_store.sqlite3.connect

        def tracing(database):
            conn = connect(database)
            conn.set_trace_callback(statements.append)
            return conn

        journal_store.sqlite3.connect = tracing
        try:
            store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        finally:
            journal_store.sqlite3.connect = connect
        try:
            begin = statements.index("BEGIN IMMEDIATE")
            assert next(i for i, text in enumerate(statements) if "table_info(docs)" in text) > begin
            assert store.winner("n.md")["_rev"] == "1-aaa"
        finally:
            store.close()

    def test_a_record_a_device_refuses_moves_the_state_aside(self, tmp_path):
        # The first release accepted keys a device refuses the whole file for.
        self.legacy(tmp_path, [revision("1-aaa", ["aaa"]), {**revision("2-bbb", ["bbb", "aaa"]), "_custom": 1}])
        (tmp_path / "chunks").mkdir()
        (tmp_path / "chunks" / "keep").write_text("kept", encoding="utf-8")
        store = JournalStore(tmp_path, PATH_IDENTITY_LEGACY)
        try:
            assert store.winner("n.md") is None
            archives = list((tmp_path.parent / (tmp_path.name + ".quarantine")).glob("*/state"))
            assert len(archives) == 1
            assert (archives[0] / "chunks" / "keep").read_text(encoding="utf-8") == "kept"
            conn = sqlite3.connect(archives[0] / DB_PATH)
            try:
                assert conn.execute("SELECT COUNT(*) FROM docs").fetchone() == (2,)
            finally:
                conn.close()
        finally:
            store.close()


class TestDeviceQuirks:
    """What the plugin's own receive code and parseDoc do with odd records."""

    @pytest.mark.parametrize("name", ["constructor", "__proto__", "toString", "hasOwnProperty"])
    def test_document_named_like_an_object_member_fails_the_file(self, store, name):
        # journalMetadataRevisions spreads a plain object and cannot iterate
        # the inherited member. A device fails the file after its chunk batch.
        store.apply_journal(JOURNAL_1, {}, [
            {"_id": "h:own", "_rev": "1-x", "type": "leaf", "data": "kept"}, revision("1-aaa", ["aaa"])])
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal(JOURNAL_2, {}, [{"_id": name, "_rev": "1-b", "type": "plain"}])
        # The plugin's own selection of the metadata batch fails, before the
        # database is given anything of that batch.
        assert (refused.value.reason, refused.value.stage) == ("object-key-id", "metadata-selection")
        assert not store.processed(JOURNAL_2)
        assert leaf_revs(store.leaves("n.md")) == ["1-aaa"]

    def test_refused_metadata_keeps_the_committed_chunk_batch(self, store):
        chunk = {"_id": "h:own", "_rev": "1-x", "type": "leaf", "data": "kept"}
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal(JOURNAL_1, {}, [chunk, {**revision("1-aaa", ["aaa"]), "_custom": 1}])
        assert refused.value.reason == "doc-validation"
        assert store.winner("h:own")["data"] == "kept"
        assert store.winner("n.md") is None
        assert not store.processed(JOURNAL_1)
        # The retry finds the chunk present and applies the metadata.
        store.apply_journal(JOURNAL_1, {}, [chunk, revision("1-aaa", ["aaa"])])
        assert leaf_revs(store.leaves("n.md")) == ["1-aaa"]

    def test_record_with_one_refused_member_keeps_the_others(self, store):
        # _putLocal refuses a local document that arrives with a revision;
        # the other records of the batch are written before the file fails.
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal(JOURNAL_1, {}, [
                {"_id": "_local/x", "_rev": "0-1", "value": 1}, revision("1-aaa", ["aaa"])])
        assert refused.value.reason == "conflict"
        assert leaf_revs(store.leaves("n.md")) == ["1-aaa"]
        assert not store.processed(JOURNAL_1)

    def test_supplied_tree_is_kept_and_the_revision_stays_unset(self, store):
        # parseDoc only builds a tree when none is supplied; the body is then
        # stored under "undefined-undefined", which the winner never names.
        store.apply_journal(JOURNAL_1, {}, [{
            "_id": "n.md", "_rev": "1-aaa", "type": "plain",
            "_rev_tree": [{"pos": 1, "ids": ["zzz", {"status": "available"}, []]}],
        }])
        assert leaf_revs(store.leaves("n.md")) == ["1-zzz"]
        assert store.winner("n.md") is None
        assert [doc["_rev"] for doc in store.revisions("n.md")] == ["undefined-undefined"]

    def test_only_the_first_supplied_root_is_merged(self, store):
        # processDocs merges rev_tree[0] of a record, whatever else it carries.
        store.apply_journal(JOURNAL_1, {}, [{
            "_id": "n.md", "_rev": "1-aaa", "type": "plain",
            "_rev_tree": [{"pos": 1, "ids": ["zzz", {"status": "available"}, []]},
                          {"pos": 2, "ids": ["yyy", {"status": "available"}, []]}],
        }])
        assert leaf_revs(store.leaves("n.md")) == ["1-zzz"]

    def test_object_hash_without_a_primitive_value_fails_the_file(self, store):
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal(JOURNAL_1, {}, [revision("1-aaa", [{"toString": 1}])])
        assert refused.value.reason == "type-error"
        assert store.winner("n.md") is None
        # A plain object or an array has a primitive value and is replayed; an
        # object as a hash equals only itself, so naming one names no ancestor.
        store.apply_journal(JOURNAL_2, {}, [revision("1-aaa", [{}]), revision("2-bbb", [[1], {}])])
        assert leaf_revs(store.leaves("n.md")) == ["2-1", "1-[object Object]"]
        # Neither names its revision by string: a device's get fails on both.
        with pytest.raises(ValueError, match="does not name"):
            store.winner("n.md")


class TestBatchOrder:
    """The records of one batch are stored in the order the IDB adapter's transaction stores them."""

    FIRST = {"_id": "n", "_rev": "1-a", "type": "plain", "path": "n.md", "children": [], "_revisions": {"start": 1, "ids": ["a"]}}
    SHARED = {"_id": "n", "_rev": "1-q", "type": "plain", "path": "n.md", "children": [], "_deleted": True,
              "_revisions": {"start": 1, "ids": ["x::1-y"]}}
    OTHER = {"_id": "n::1-x", "_rev": "1-y", "type": "plain", "path": "n.md::1-x", "children": [], "_revisions": {"start": 1, "ids": ["y"]}}

    def test_a_documents_second_record_is_stored_after_another_documents(self, store):
        # processDocs starts every document's records together and continues a document's
        # records once the write of the one before is done; a transaction serves its requests
        # in order. The shared slot ends with the first document's second record, which is
        # not the last record of the batch (the seventh-round architecture review's case).
        store.apply_journal(JOURNAL_1, {}, [self.FIRST, self.SHARED, self.OTHER])
        assert store.raw_get("n::1-x")["_deleted"] is True
        assert store.raw_get("n")["_rev"] == "1-a"

    def test_equal_revisions_keep_their_arrival_order(self, store):
        # The batch is sorted by id and start; records equal there keep their order, so the
        # tombstone is the first document's first record here and the other document is stored last.
        store.apply_journal(JOURNAL_1, {}, [self.OTHER, self.SHARED, self.FIRST])
        assert "_deleted" not in store.raw_get("n::1-x")

    def test_attachment_data_is_stored_before_its_body(self, tmp_path):
        # writeAttachments stores the data of a record's attachments before its body: a
        # record with data is served after one without. The other document with data is
        # stored after the first document's second record; with data on the first record's
        # first record too, that document is served later again and its second record last.
        data = {"_attachments": {"x": {"data": "eA==", "content_type": "text/plain"}}}

        def held_in_the_shared_slot(index, records):
            store = JournalStore(tmp_path / f"state{index}", PATH_IDENTITY_LEGACY)
            try:
                store.apply_journal(JOURNAL_1, {}, records)
                body = store.raw_get("n::1-x")
                return "_deleted" in body
            finally:
                store.close()

        assert held_in_the_shared_slot(0, [self.FIRST, self.SHARED, {**self.OTHER, **data}]) is False
        assert held_in_the_shared_slot(1, [{**self.FIRST, **data}, self.SHARED, self.OTHER]) is True
        assert held_in_the_shared_slot(2, [{**self.FIRST, **data}, self.SHARED, {**self.OTHER, **data}]) is True


class TestAttachments:
    """PouchDB attachments, which LiveSync never writes, admitted as a device admits them."""

    @pytest.fixture()
    def vectors(self):
        fixture = gzip.decompress((Path(__file__).parent / "fixtures/journal_winner_upstream.json.gz").read_bytes())
        return json.loads(fixture)["base64"]

    @pytest.mark.parametrize("index", range(34))
    def test_base64_is_decoded_as_the_platform_atob_does(self, vectors, index):
        from revision_tree import _decode_base64, RecordRefused
        vector = vectors[index]
        if not vector["ok"]:
            with pytest.raises(RecordRefused) as refused:
                _decode_base64(vector["text"])
            assert refused.value.reason == "invalid-base64"
            return
        raw = _decode_base64(vector["text"])
        assert len(raw) == vector["length"]
        assert "md5-" + base64.b64encode(hashlib.md5(raw).digest()).decode("ascii") == vector["digest"]

    @staticmethod
    def record(attachments):
        return {"_id": "n.md", "_rev": "1-aaa", "type": "plain", "path": "n.md", "children": [],
                "_attachments": attachments}

    def test_a_merge_that_stems_a_revision_away_removes_its_slot_and_attachments(self, store):
        # finishDoc and compactRevs (pouchdb-adapter-idb lib/index.js:476-485,173-241): the
        # body of a stemmed revision goes from its by-sequence slot with the attachment
        # mappings; an attachment no slot maps any more is gone, and a stub naming it is
        # refused before anything of its batch is written.
        digest = "md5-" + base64.b64encode(hashlib.md5(b"x").digest()).decode("ascii")
        chain = [f"h{step}" for step in range(100, 0, -1)]
        store.apply_journal(JOURNAL_1, {}, [
            self.record({"x": {"content_type": "text/plain", "data": "eA=="}}),
            {"_id": "n.md", "_rev": "101-h100", "type": "plain", "path": "n.md", "children": [],
             "_revisions": {"start": 101, "ids": [*chain, "aaa"]}}])
        assert store.winner("n.md")["_rev"] == "101-h100"
        assert store._body("n.md", "1-aaa") is None
        assert [rev for (rev,) in store._conn.execute("SELECT rev FROM docs")] == ["101-h100"]
        assert not store._has_attachment(digest)
        stub = {"_id": "m.md", "_rev": "1-s", "type": "plain", "path": "m.md", "children": [],
                "_attachments": {"x": {"stub": True, "content_type": "text/plain", "digest": digest}},
                "_revisions": {"start": 1, "ids": ["s"]}}
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal("second", {}, [stub])
        assert refused.value.reason == "missing-stub" and refused.value.stage == "metadata-write"
        assert store.winner("m.md") is None and not store.processed("second")

    def test_a_request_is_answered_before_the_next_one_is_served(self, store):
        # The eighth-round architecture review's witness. A merge stems the slot another
        # document's body, with attachments, is put into in the same batch. Each request's
        # event is dispatched before the next request is served: the cursor over the slot's
        # mappings opens before the other document's mappings are put, so it removes none,
        # and both attachments stay held.
        digest = lambda text: "md5-" + base64.b64encode(hashlib.md5(text.encode()).digest()).decode("ascii")
        chain = [f"h{step}" for step in range(100, 0, -1)]
        store.apply_journal(JOURNAL_1, {}, [
            {"_id": "n", "_rev": "101-h100", "type": "plain", "path": "n.md", "children": [],
             "_revisions": {"start": 101, "ids": [*chain, "x::1-y"]}},
            {"_id": "n::1-x", "_rev": "1-y", "type": "plain", "path": "n::1-x", "children": [],
             "_attachments": {"0": {"data": "eQ==", "content_type": "text/plain"},
                              "1": {"data": "eA==", "content_type": "text/plain"}},
             "_revisions": {"start": 1, "ids": ["y"]}},
            {"_id": "n", "_rev": "1-z", "type": "plain", "path": "n.md", "children": [],
             "_revisions": {"start": 1, "ids": ["z"]}}])
        assert store.winner("n")["_rev"] == "101-h100" and store._body("n", "1-x::1-y") is None
        assert store._has_attachment(digest("x")) and store._has_attachment(digest("y"))

        def stub(name, held):
            return {"_id": name, "_rev": "1-s", "type": "plain", "path": name + ".md", "children": [],
                    "_attachments": {"x": {"stub": True, "digest": held, "content_type": "text/plain"}},
                    "_revisions": {"start": 1, "ids": ["s"]}}

        for name, held in (("stubx", digest("x")), ("stuby", digest("y"))):
            store.apply_journal(name, {}, [stub(name, held)])
            assert store.winner(name)["_rev"] == "1-s"
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal("stubz", {}, [stub("stubz", digest("z"))])
        assert refused.value.reason == "missing-stub" and refused.value.stage == "metadata-write"

    def test_data_that_is_not_a_string_is_refused(self, store):
        # A device takes it for a Blob, which JSON cannot carry.
        for data in (7, None, True, [], {}):
            with pytest.raises(RecordRefused) as refused:
                store.apply_journal(JOURNAL_1, {}, [self.record({"x": {"content_type": "t", "data": data}})])
            assert refused.value.reason == "malformed-attachment"
            assert not store.processed(JOURNAL_1)

    def test_an_attachment_that_is_not_an_object_is_refused(self, store):
        for attachment in ("text", None, 7, []):
            with pytest.raises(RecordRefused) as refused:
                store.apply_journal(JOURNAL_1, {}, [self.record({"x": attachment})])
            assert refused.value.reason == "malformed-attachment"

    def test_record_breaking_the_decoder_contract_fails_before_anything(self, store):
        for record in ({"_id": "h:own", "_rev": "1-x", "type": "leaf"},
                       {"_id": "h:", "_rev": "1-x", "type": "leaf", "data": "own"},
                       {"_id": "h:own", "_rev": 1, "type": "leaf", "data": "own"}):
            with pytest.raises(RecordRefused) as refused:
                store.apply_journal(JOURNAL_1, {}, [record, revision("1-aaa", ["aaa"])])
            assert refused.value.reason == "invalid-chunk-record"
            assert store.winner("n.md") is None
        # An empty revision is a chunk record's normal shape on the wire.
        store.apply_journal(JOURNAL_1, {}, [{"_id": "h:own", "_rev": "", "type": "leaf", "data": "own"}])
        assert store.winner("h:own")["data"] == "own"


class TestApplyJournal:
    def test_note_and_chunk_file_stored(self, tmp_path):
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
        store.apply_journal(
            JOURNAL_1,
            {"h:0123456789abcdef": "hi"},
            [meta_doc("note.md", "1-x", children=["h:0123456789abcdef"])],
        )
        assert store.note_ids() == ["note.md"]
        assert store.winner("note.md")["_rev"] == "1-x"
        assert store.processed(JOURNAL_1) is True
        store.close()

    def test_a_chunk_is_held_as_a_document_and_no_file_is_written(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        store.apply_journal(JOURNAL_1, {"h:0123456789abcdef": "привет"}, [])
        assert store.winner("h:0123456789abcdef")["data"] == "привет"
        assert store.winner("h:0123456789abcdef")["type"] == "leaf"
        assert not any(p.is_file() for p in (state / "chunks").rglob("*"))
        store.close()

    def test_reapplying_same_file_changes_nothing(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        chunks = {"h:0123456789abcdef": "body"}
        docs = [meta_doc("note.md", "1-x", children=["h:0123456789abcdef"])]
        store.apply_journal(JOURNAL_1, chunks, docs)
        files_before = sorted(
            p for p in (state / "chunks").rglob("*") if p.is_file()
        )
        store.apply_journal(JOURNAL_1, chunks, docs)
        assert store.files_processed_count() == 1
        assert store.note_ids() == ["note.md"]
        assert len(store.leaves("note.md")) == 1
        files_after = sorted(
            p for p in (state / "chunks").rglob("*") if p.is_file()
        )
        assert files_after == files_before
        store.close()

    def test_merge_sibling_leaf_keeps_both_leaves_single_winner(self, tmp_path):
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
        store.apply_journal(
            JOURNAL_1,
            {"h:a": "first "},
            [meta_doc("note.md", "2-aaa", children=["h:a"])],
        )
        store.apply_journal(
            JOURNAL_2,
            {"h:b": "second"},
            [meta_doc("note.md", "2-bbb", children=["h:b"])],
        )
        assert sorted(d["_rev"] for d in store.leaves("note.md")) == [
            "2-aaa",
            "2-bbb",
        ]
        assert store.winner("note.md")["_rev"] == "2-bbb"
        assert store.note_ids() == ["note.md"]
        doc, text, data, is_text = store.read_note("note.md")
        assert doc["_rev"] == "2-bbb"
        assert (text, data, is_text) == ("second", None, True)
        store.close()

    def test_identical_chunk_in_two_journals_is_held_once(self, store):
        store.apply_journal(JOURNAL_1, {"h:same": "data"}, [])
        first = store.winner("h:same")["_rev"]
        store.apply_journal(JOURNAL_2, {"h:same": "data"}, [])
        assert len(store.leaves("h:same")) == 1
        assert store.winner("h:same")["_rev"] == first
        assert len(store.revisions("h:same")) == 1

    def test_tombstone_winner_hides_note_but_keeps_leaf(self, tmp_path):
        # These records carry no _revisions, so each is an independent leaf.
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_LEGACY)
        store.apply_journal(
            JOURNAL_1, {"h:a": "body"}, [meta_doc("note.md", "1-a", children=["h:a"])]
        )
        store.apply_journal(
            JOURNAL_2,
            {},
            [meta_doc("note.md", "2-x", children=["h:a"], deleted=True)],
        )
        assert store.winner("note.md")["_rev"] == "2-x"
        assert store.note_ids() == []
        assert store.read_note("note.md") is None
        assert sorted(d["_rev"] for d in store.leaves("note.md")) == ["1-a", "2-x"]
        store.close()

    def test_full_json_chunk_record_serves_children(self, store):
        chunk_doc = {"_id": "h:full", "_rev": "1-q", "data": "from json record"}
        note = meta_doc("viajson.md", "1-x", children=["h:full"])
        store.apply_journal(JOURNAL_1, {}, [chunk_doc, note])
        doc, text, data, is_text = store.read_note("viajson.md")
        assert (text, data, is_text) == ("from json record", None, True)

    def test_malformed_rev_rejects_whole_file_atomically(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        # Prior, successfully applied state must survive untouched.
        store.apply_journal(
            JOURNAL_1,
            {"h:0123456789abcdef": "keep"},
            [meta_doc("seed.md", "1-a", children=["h:0123456789abcdef"])],
        )
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal(
                JOURNAL_2,
                {"h:fedcba9876543210": "staged"},
                [meta_doc("a.md", "1-x"), {"_id": "b.md", "_rev": "bogus"}],
            )
        assert refused.value.reason == "invalid-rev"
        assert store.processed(JOURNAL_2) is False
        assert store.note_ids() == ["seed.md"]
        # The chunk batch a device commits first stays, and its chunks are
        # reusable; the refused metadata batch holds nothing.
        assert store.winner("h:0123456789abcdef")["data"] == "keep"
        assert store.winner("h:fedcba9876543210")["data"] == "staged"
        assert store.winner("a.md") is None and store.winner("b.md") is None
        assert not any(p.is_file() for p in (state / "chunks").rglob("*"))
        assert store.read_note("seed.md")[1] == "keep"
        store.close()

    def test_publication_failure_leaves_journal_replayable(self, store, monkeypatch):
        chunks = {"h:first": "first body", "h:second": "second body"}
        docs = [meta_doc("retry.md", "1-x", children=list(chunks))]
        real_write = JournalStore._write_plan
        calls = 0

        def fail_second_batch(inner, rows, trees):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected publication failure")
            return real_write(inner, rows, trees)

        with monkeypatch.context() as patch:
            patch.setattr(JournalStore, "_write_plan", fail_second_batch)
            with pytest.raises(OSError, match="injected publication failure"):
                store.apply_journal(JOURNAL_1, chunks, docs)

        assert store.processed(JOURNAL_1) is False
        assert store.leaves("retry.md") == []
        # The chunk batch, committed first as a device commits it, stays whole.
        assert store.winner("h:first")["data"] == "first body"
        assert store.winner("h:second")["data"] == "second body"

        store.apply_journal(JOURNAL_1, chunks, docs)

        assert store.processed(JOURNAL_1) is True
        assert store.files_processed_count() == 1
        assert store.leaves("retry.md") == docs
        assert store.read_note("retry.md")[1] == "first bodysecond body"
        for chunk_id, data in chunks.items():
            assert store.winner(chunk_id)["data"] == data
        assert not any(p.is_file() for p in store._chunks_dir.rglob("*"))

    @pytest.mark.parametrize("same_journal", [False, True])
    def test_concurrent_apply_same_chunk_both_succeed(
        self, tmp_path, monkeypatch, same_journal
    ):
        # Two live store instances replaying journals that carry the same
        # content-addressed chunk: neither may fail and the chunk is held
        # once. The barriers make both plan the same chunk as absent and both
        # check the processed ledger before either marks the file, which is
        # the state in which a second copy or a failed write would appear.
        state = tmp_path / "state"
        JournalStore(state, PATH_IDENTITY_LEGACY).close()  # establish WAL once
        planned = threading.Barrier(2)
        checked = threading.Barrier(2)
        real_processed = JournalStore.processed
        real_plan = JournalStore._plan

        def rendezvous_processed(instance, name):
            result = real_processed(instance, name)
            checked.wait(timeout=60)
            return result

        def rendezvous_plan(instance, records):
            plan = real_plan(instance, records)
            planned.wait(timeout=60)
            return plan

        monkeypatch.setattr(JournalStore, "processed", rendezvous_processed)
        monkeypatch.setattr(JournalStore, "_plan", rendezvous_plan)

        chunk = {"h:0123456789abcdef": "shared body"}
        errors: list[BaseException] = []

        def replay(name: str, doc_id: str) -> None:
            store = JournalStore(state, PATH_IDENTITY_LEGACY)
            try:
                store.apply_journal(
                    name, chunk, [meta_doc(doc_id, "1-x", children=["h:0123456789abcdef"])]
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                store.close()

        threads = [
            threading.Thread(target=replay, args=(JOURNAL_1, "a.md")),
            threading.Thread(target=replay, args=(JOURNAL_1 if same_journal else JOURNAL_2,
                                                         "a.md" if same_journal else "b.md")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []

        check = JournalStore(state, PATH_IDENTITY_LEGACY)
        try:
            assert len(check.leaves("h:0123456789abcdef")) == 1
            assert check.winner("h:0123456789abcdef")["data"] == "shared body"
            assert check.files_processed_count() == (1 if same_journal else 2)
            assert check.read_note("a.md")[1] == "shared body"
            assert check.read_note("a.md" if same_journal else "b.md")[1] == "shared body"
        finally:
            check.close()

    def test_a_chunk_identifier_outside_the_cache_alphabet_is_held_as_its_document(self, store):
        # A device holds every chunk its decoder admits, whatever its
        # identifier. The document is held; a note referring to it by that
        # identifier is refused as an invalid reference. A compact line whose
        # identifier is not a chunk's is not a journal record at all: the
        # decoder refuses the file.
        with pytest.raises(ValueError):
            parse_journal(b"~f:notachunk\x1fx\n")
        store.apply_journal(
            JOURNAL_1,
            {"h:aa../../../escaped": "evil"},
            [meta_doc("a.md", "1-x", children=["h:aa../../../escaped"])],
        )
        assert store.processed(JOURNAL_1) is True
        assert store.winner("h:aa../../../escaped")["data"] == "evil"
        assert not any(p.is_file() for p in (store.state_dir / "chunks").rglob("*"))
        with pytest.raises(ValueError, match="invalid chunk reference"):
            store.read_note("a.md")

    def test_traversal_chunk_write_never_escapes_state_dir(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        # chunks/aa/../../../escaped resolves outside the state dir.
        store.apply_journal(JOURNAL_1, {"h:aa../../../escaped": "evil"}, [])
        assert not (tmp_path / "escaped").exists()
        assert [
            p for p in tmp_path.rglob("*") if p.is_file() and "escaped" in p.name
        ] == []
        store.close()

    def test_traversal_child_is_isolated_not_an_escape(self, store, tmp_path):
        # Reading must not exfiltrate files outside the chunk store through
        # crafted child references: a "aa" shard directory is exactly the
        # state in which an unguarded path join would stat and read the
        # outside file, so it is planted as a release before chunk documents
        # would have left it.
        secret = tmp_path / "outside"
        secret.write_text("SECRET")
        (store.state_dir / "chunks" / "aa").mkdir(parents=True)
        doomed = meta_doc("doomed.md", "1-x", children=["h:aa../../../outside"])
        good = meta_doc("good.md", "1-y", children=["h:g"])
        store.apply_journal(JOURNAL_1, {"h:g": "fine"}, [doomed, good])
        with pytest.raises(ValueError, match="corrupt"):
            store.read_note("doomed.md")
        pairs = dict((d["_id"], text) for d, text in store.iterate_notes())
        assert pairs == {"doomed.md": None, "good.md": "fine"}

    def test_full_valid_charset_chunk_ids_round_trip(self, store):
        chunk_id = "h:Ab0-_9+zZ"
        doc = meta_doc("charset.md", "1-x", children=[chunk_id])
        store.apply_journal(JOURNAL_1, {chunk_id: "payload"}, [doc])
        assert store.read_note("charset.md")[1] == "payload"

    @pytest.mark.parametrize("chunk_id", ["h:" + "a" * 65, "h:abc\n", "h:a/b"])
    def test_cache_id_limits_do_not_refuse_what_a_device_holds(self, store, chunk_id):
        # The decoder admits these identifiers; the cache cannot name them.
        # The chunk document is held, nothing is written under chunks/, and an
        # inline or stored reference is still refused by the reference check.
        doc = meta_doc(
            "invalid.md", "1-x", children=[chunk_id],
            eden={chunk_id: {"data": "must not read", "epoch": 1}},
        )
        store.apply_journal(JOURNAL_1, {chunk_id: "must not write"}, [doc])
        assert store.winner(chunk_id)["data"] == "must not write"
        assert not any(p.is_file() for p in store._chunks_dir.rglob("*"))
        assert list(store.iterate_notes())[0][1] is None

    def test_a_chunk_identifier_the_decoder_refuses_refuses_the_file(self, store):
        # journalRecords.ts:37: "h:" alone is not a chunk identifier.
        with pytest.raises(ValueError):
            parse_journal(b"~h:\x1fdata\n")
        with pytest.raises(RecordRefused) as refused:
            store.apply_journal(
                JOURNAL_1, {"h:": "must not write"}, [meta_doc("invalid.md", "1-x")])
        assert refused.value.reason == "invalid-chunk-record"
        assert store.winner("invalid.md") is None and store.winner("h:") is None
        assert not any(p.is_file() for p in store._chunks_dir.rglob("*"))

    def test_symlink_shard_cannot_escape_chunks_root(self, store, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "payload").write_bytes(b"private")
        (store._chunks_dir / "aa").symlink_to(outside, target_is_directory=True)
        store.apply_journal(
            JOURNAL_1, {"h:aanew": "bad"},
            [meta_doc("bad.md", "1-x", children=["h:aapayload"])],
        )
        assert not (outside / "new").exists()
        assert list(store.iterate_notes())[0][1] is None


class TestProcessedAndMeta:
    @pytest.mark.parametrize("symlink_root", [False, True], ids=["shard", "root"])
    def test_cache_reset_never_traverses_or_removes_chunk_symlinks(
        self, store, tmp_path, symlink_root
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_shard = outside / "bb"
        outside_shard.mkdir()
        (outside / "payload").write_bytes(b"private")
        (outside_shard / "nested").write_bytes(b"also private")
        if symlink_root:
            store._chunks_dir.rmdir()
            link = store._chunks_dir
        else:
            link = store._chunks_dir / "aa"
        link.symlink_to(outside, target_is_directory=True)
        store.mark_processed(JOURNAL_1)

        store.reset()

        assert (outside / "payload").read_bytes() == b"private"
        assert (outside_shard / "nested").read_bytes() == b"also private"
        assert link.is_symlink()
        assert link.exists()
        assert store.files_processed_count() == 0

    def test_defaults_on_fresh_store(self, store):
        assert store.last_sent_key == ""
        assert store.epoch == ""
        assert store.files_processed_count() == 0
        assert store.note_ids() == []

    def test_mark_processed_lifecycle(self, store):
        assert store.processed("some-file") is False
        store.mark_processed("some-file")
        assert store.processed("some-file") is True
        assert store.files_processed_count() == 1

    def test_last_sent_key_roundtrip(self, store):
        store.last_sent_key = JOURNAL_2
        assert store.last_sent_key == JOURNAL_2

    def test_state_persists_across_reopen(self, tmp_path):
        state = tmp_path / "state"
        first = JournalStore(state, PATH_IDENTITY_LEGACY)
        # Realistic order (Task 5): epoch is known from sync parameters
        # before any journal is replayed; the first epoch set on a fresh
        # store differs from the "" default and resets nothing.
        first.epoch = "1:salt"
        first.apply_journal(
            JOURNAL_1, {"h:a": "body"}, [meta_doc("note.md", "1-a", children=["h:a"])]
        )
        first.last_sent_key = JOURNAL_1
        first.close()

        second = JournalStore(state, PATH_IDENTITY_LEGACY)
        assert second.processed(JOURNAL_1) is True
        assert second.note_ids() == ["note.md"]
        assert second.last_sent_key == JOURNAL_1
        assert second.epoch == "1:salt"
        second.close()

    def test_epoch_records_value_without_destroying_state(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        store.epoch = "1:salt"
        store.apply_journal(
            JOURNAL_1,
            {"h:0123456789abcdef": "body"},
            [meta_doc("note.md", "1-x", children=["h:0123456789abcdef"])],
        )
        store.last_sent_key = JOURNAL_1
        assert store.note_ids() == ["note.md"]
        assert store.winner("h:0123456789abcdef")["data"] == "body"

        store.epoch = "2:othersalt"

        assert store.epoch == "2:othersalt"
        assert store.note_ids() == ["note.md"]
        assert store.files_processed_count() == 1
        assert store.last_sent_key == JOURNAL_1
        assert store.read_note("note.md")[1] == "body"
        store.reset()
        assert store.note_ids() == ["note.md"]
        assert store.files_processed_count() == 0
        assert store.last_sent_key == ""
        assert store.epoch == "2:othersalt"
        assert store.read_note("note.md")[1] == "body"
        store.close()

    def test_setting_same_epoch_does_not_reset(self, store):
        store.epoch = "1:salt"
        store.apply_journal(
            JOURNAL_1, {"h:a": "body"}, [meta_doc("note.md", "1-a", children=["h:a"])]
        )
        store.epoch = "1:salt"
        assert store.note_ids() == ["note.md"]
        assert store.files_processed_count() == 1


class TestPathIdentity:
    def test_unknown_identity_value_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="path identity"):
            JournalStore(tmp_path / "state", "weird")

    def test_identity_mismatch_on_reopen_quarantines(self, tmp_path):
        state = tmp_path / "state"
        JournalStore(state, PATH_IDENTITY_LEGACY).close()
        fresh = JournalStore(state, PATH_IDENTITY_NFC)
        assert fresh.path_identity == PATH_IDENTITY_NFC
        fresh.close()
        archived = next(state.with_name(state.name + ".quarantine").glob("*/state"))
        reopened = JournalStore(archived, PATH_IDENTITY_LEGACY)
        reopened.close()

    def test_unicode_doc_id_untouched_in_nfc_mode(self, tmp_path):
        nfd_id = unicodedata.normalize("NFD", "café.md")
        store = JournalStore(tmp_path / "state", PATH_IDENTITY_NFC)
        store.apply_journal(JOURNAL_1, {}, [meta_doc(nfd_id, "1-x")])
        # The store never derives or rewrites ids: whatever the journal
        # delivered is preserved byte-for-byte.
        assert store.note_ids() == [nfd_id]
        assert store.winner(nfd_id)["_id"] == nfd_id
        store.close()


class TestReadNote:
    def test_unknown_doc_returns_none(self, store):
        assert store.read_note("nope.md") is None
        assert store.winner("nope.md") is None

    def test_plain_children_concatenate_in_order(self, store):
        doc = meta_doc("a.md", "1-x", children=["h:one", "h:two"])
        store.apply_journal(
            JOURNAL_1, {"h:one": "Hello, ", "h:two": "мир"}, [doc]
        )
        winner, text, data, is_text = store.read_note("a.md")
        assert winner["_rev"] == "1-x"
        assert (text, data, is_text) == ("Hello, мир", None, True)

    def test_eden_inline_chunk_resolves_without_store_file(self, store):
        doc = meta_doc(
            "inline.md",
            "1-x",
            children=["h:inline"],
            eden={"h:inline": "inline body"},
        )
        store.apply_journal(JOURNAL_1, {}, [doc])
        _, text, data, is_text = store.read_note("inline.md")
        assert (text, data, is_text) == ("inline body", None, True)

    def test_newnote_binary_decodes_base64_per_chunk(self, store):
        payload = b"\x89PNG\r\n\x1a\nbinary"
        doc = meta_doc("image.png", "1-x", children=["h:bin"], doc_type="newnote")
        store.apply_journal(
            JOURNAL_1,
            {"h:bin": base64.b64encode(payload).decode("ascii")},
            [doc],
        )
        _, text, data, is_text = store.read_note("image.png")
        assert data == payload
        assert text is None
        assert is_text is False

    def test_newnote_with_text_extension_stays_bytes(self, store):
        payload = "кириллица".encode("utf-8")
        doc = meta_doc("x.md", "1-x", children=["h:bin"], doc_type="newnote")
        store.apply_journal(
            JOURNAL_1,
            {"h:bin": base64.b64encode(payload).decode("ascii")},
            [doc],
        )
        _, text, data, is_text = store.read_note("x.md")
        assert (text, data, is_text) == (None, payload, False)

    def test_legacy_notes_body(self, store):
        doc = meta_doc("old.md", "1-x", doc_type="notes", data="legacy body")
        store.apply_journal(JOURNAL_1, {}, [doc])
        winner, text, data, is_text = store.read_note("old.md")
        assert (text, data, is_text) == ("legacy body", None, True)

    def test_encrypted_winner_marker_refused(self, store):
        doc = meta_doc("enc.md", "1-x", children=["h:a"], e_=True)
        store.apply_journal(JOURNAL_1, {"h:a": "x"}, [doc])
        with pytest.raises(ValueError, match="end-to-end encryption"):
            store.read_note("enc.md")

    def test_legacy_percent_prefix_refused(self, store):
        doc = meta_doc("enc.md", "1-x", doc_type="notes", data="%gibberish")
        store.apply_journal(JOURNAL_1, {}, [doc])
        with pytest.raises(ValueError, match="end-to-end encryption"):
            store.read_note("enc.md")

    def test_encrypted_eden_inline_chunk_refused(self, store):
        doc = meta_doc(
            "enc.md",
            "1-x",
            children=["h:e1"],
            eden={"h:e1": {"e_": True, "data": "secret"}},
        )
        store.apply_journal(JOURNAL_1, {}, [doc])
        with pytest.raises(ValueError, match="end-to-end encryption"):
            store.read_note("enc.md")

    def test_missing_chunk_raises_corrupt(self, store):
        doc = meta_doc("broken.md", "1-x", children=["h:gone"])
        store.apply_journal(JOURNAL_1, {}, [doc])
        with pytest.raises(ValueError, match="corrupt"):
            store.read_note("broken.md")

    def test_invalid_children_field_raises_corrupt(self, store):
        doc = meta_doc("bad.md", "1-x")
        doc["children"] = "not-a-list"
        store.apply_journal(JOURNAL_1, {}, [doc])
        with pytest.raises(ValueError, match="corrupt"):
            store.read_note("bad.md")


class TestIterateNotes:
    def test_yields_texts_sorted_and_filters_extension(self, store):
        docs = [
            meta_doc("b.md", "1-x", children=["h:b"]),
            meta_doc("a.md", "1-y", children=["h:a"]),
            meta_doc("c.png", "1-z", children=["h:c"], doc_type="newnote"),
        ]
        store.apply_journal(
            JOURNAL_1, {"h:a": "alpha", "h:b": "beta", "h:c": "aGVsbG8="}, docs
        )
        pairs = [(d["_id"], text) for d, text in store.iterate_notes()]
        assert pairs == [("a.md", "alpha"), ("b.md", "beta")]

    def test_custom_extension_parameter(self, store):
        doc = meta_doc("draw.canvas", "1-x", children=["h:x"])
        store.apply_journal(JOURNAL_1, {"h:x": "shapes"}, [doc])
        pairs = [(d["_id"], text) for d, text in store.iterate_notes(".canvas")]
        assert pairs == [("draw.canvas", "shapes")]

    def test_corrupt_note_isolated_others_read_fine(self, store):
        good = meta_doc("good.md", "1-x", children=["h:good"])
        bad = meta_doc("bad.md", "1-y", children=["h:missing"])
        store.apply_journal(JOURNAL_1, {"h:good": "fine"}, [good, bad])
        pairs = dict((d["_id"], text) for d, text in store.iterate_notes())
        assert pairs == {"good.md": "fine", "bad.md": None}


class TestSqliteLayout:
    def test_wal_journal_mode_is_persistent(self, tmp_path):
        state = tmp_path / "state"
        store = JournalStore(state, PATH_IDENTITY_LEGACY)
        store.apply_journal(JOURNAL_1, {}, [meta_doc("a.md", "1-x")])
        store.close()
        conn = sqlite3.connect(state / DB_PATH)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        conn.close()

    def test_schema_v1_tables_exist(self, tmp_path):
        state = tmp_path / "state"
        JournalStore(state, PATH_IDENTITY_LEGACY).close()
        conn = sqlite3.connect(state / DB_PATH)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        conn.close()
        assert {"docs", "processed", "meta"} <= names

    def test_second_journal_name_increments_count(self, store):
        store.apply_journal(JOURNAL_1, {}, [meta_doc("a.md", "1-x")])
        store.apply_journal(JOURNAL_2, {}, [meta_doc("b.md", "1-x")])
        assert store.files_processed_count() == 2


@pytest.mark.parametrize("target", ["state", "chunks", "sqlite"])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("dangling", [False, True])
def test_open_rejects_symlinked_persistence(tmp_path, target, reopen, dangling):
    state = tmp_path / "nested" / "state"
    if reopen:
        JournalStore(state, PATH_IDENTITY_LEGACY).close()
    outside = tmp_path / "outside"
    if not dangling:
        outside.mkdir()
    if target == "state":
        if reopen:
            state.rename(tmp_path / "original")
        else:
            state.parent.mkdir()
        link = state
        destination = tmp_path / "original" if reopen and not dangling else outside
    else:
        state.mkdir(parents=True, exist_ok=True)
        link = state / ("chunks" if target == "chunks" else DB_PATH)
        if link.exists():
            if target == "chunks":
                link.rmdir()
            else:
                link.unlink()
        destination = outside if target == "chunks" else outside / "database"
        if target == "sqlite" and not dangling:
            destination.write_bytes(b"untouched")
    link.symlink_to(destination, target_is_directory=target != "sqlite")
    before = {str(p.relative_to(outside)): p.read_bytes()
              for p in outside.rglob("*") if p.is_file()}
    with pytest.raises(StorageError, match="symlink.*refusing"):
        JournalStore(state, PATH_IDENTITY_LEGACY)
    assert link.is_symlink()
    assert {str(p.relative_to(outside)): p.read_bytes()
            for p in outside.rglob("*") if p.is_file()} == before
    if dangling:
        assert not outside.exists()


def test_nested_state_normal_operation_and_reopen(tmp_path):
    state = tmp_path / "nested" / "state"
    store = JournalStore(state, PATH_IDENTITY_LEGACY)
    store.apply_journal(JOURNAL_1, {"h:0123456789abcdef": "body"}, [])
    store.close()
    reopened = JournalStore(state, PATH_IDENTITY_LEGACY)
    assert reopened.processed(JOURNAL_1)
    assert reopened.winner("h:0123456789abcdef")["data"] == "body"
    reopened.close()


def test_state_size_bytes_includes_sqlite_sidecars_and_nested_chunks(store):
    root = store.state_dir
    shard = root / "chunks" / "nested"
    shard.mkdir()
    (shard / "fixture").write_bytes(b"abcde")
    expected = sum(p.stat().st_size for p in root.iterdir() if p.is_file()) + 5
    (root / "unrelated.txt").write_bytes(b"ignored")
    assert store.state_size_bytes() == expected


@pytest.mark.parametrize("identity", ["case-sensitive", "nfc-case-sensitive"])
def test_sensitive_identity_migration_quarantines(tmp_path, identity):
    store = JournalStore(tmp_path, identity)
    store.close()
    fresh = JournalStore(tmp_path, "legacy")
    fresh.close()
    archived = next(tmp_path.with_name(tmp_path.name + ".quarantine").glob("*/state"))
    old = JournalStore(archived, identity)
    old.close()
