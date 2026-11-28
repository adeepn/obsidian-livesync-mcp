"""Local peer state for journal replication: SQLite revision trees plus a
content-addressed on-disk chunk store (Plan B, Task 3).

This is the journal analogue of an Obsidian client's local database: every
journal file that :mod:`journal_codec` parses is replayed into

- one SQLite database (``journal.sqlite3``) holding every stored revision's
  body in the slot the IndexedDB adapter stores it in (``id::rev``, read back
  as that adapter reads it), each document's revision tree, the ``processed`` journal-name ledger
  (re-applying a file is a no-op) and a ``meta`` table (epoch, last-sent key,
  path identity); and
- ``chunks/<hh>/<rest>`` files under the state directory, written by
  releases that held a compact chunk as a file alone. They are read only
  for a chunk no document names; a chunk received since is held as a
  document in the database, as a device holds it, and no file is written
  for it.

Design notes:

- *Revision trees*: each journal file is replayed the way a device receives
  it (:mod:`revision_tree` ports the published PouchDB code): chunk documents,
  compact records and full-JSON ones alike, are created once, metadata is
  filtered, sorted and merged into the
  document's revision tree, which is stemmed to the device's limit of 100
  revisions per path. The ``rev_trees`` table holds one tree per document and
  the ``docs`` table every stored revision's body, keyed by the revision the
  device keeps. The winning revision is the leaf that is not
  revision-deleted (``_deleted``), then the one at the highest position, then
  the greatest hash. LiveSync's ``deleted`` flag is the note's own state and
  takes no part in the choice. Sibling leaves from concurrent devices coexist
  and stay queryable through :meth:`JournalStore.leaves`;
  :meth:`JournalStore.revisions` lists every stored revision.
- *Epoch resets*: the epoch is ``protocolVersion:pbkdf2salt`` from the
  journal sync parameters. The backend confirms wipes against send history;
  resets clear only processed-journal and last-sent tracking. Local revision
  trees and chunks survive, including notes absent from rebuilt remote history.
- *Path identity*: the mode resolved at startup
  (:func:`livesync_format.resolve_path_identity`) is stored on first open;
  reopening with a different mode quarantines the whole old state directory
  beside it before starting fresh. IDs are never mixed or rewritten.

The store and :mod:`revision_tree` use only the standard library; identity
quarantine is a sibling of ``state_dir`` so old documents and chunks remain
recoverable.
"""

from __future__ import annotations

from wire_encoding import escape_json_surrogates

from diagnostic_hygiene import PublicDiagnostic, DiagnosticPath, DiagnosticCode

import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from content_splitter import _js_truthy
from revision_tree import (
    RecordRefused,
    collect_conflicts,
    collect_leaves,
    is_deleted,
    plan_file,
    _DATA_WORDS,
    _RESERVED_WORDS,
    revision_chain,
    valid_rev,
    winning_rev,
    write_batch,
)
from livesync_format import (
    PATH_IDENTITY_CASE_SENSITIVE,
    PATH_IDENTITY_NFC_CASE_SENSITIVE,
    PATH_IDENTITY_LEGACY,
    PATH_IDENTITY_NFC,
    EncryptedInlineChunk,
    assemble_binary,
    assemble_text,
    extract_inline_chunks,
    is_note_document,
    legacy_body,
)

from storage_base import StorageError
from storage_base import TextBudgetExceeded
from text_eligibility import TextProjection, DEFAULT_TEXT_VERDICT_CACHE_ENTRIES

logger = logging.getLogger(__name__)

_DB_FILENAME = "journal.sqlite3"
_CHUNKS_DIRNAME = "chunks"
_SHORT_SHARD = "_short"

_META_PATH_IDENTITY = "path_identity"
_META_EPOCH = "epoch"
_META_LAST_SENT_KEY = "last_sent_key"

# Accept bounded historical IDs and standard SHA-1 base64 IDs. Upstream:
# commonlib@484c164450d61deb2e301582b60cb9b5f9fa977f
# managers/HashManager/PureJSHashManager.ts:77-87.
_CHUNK_ID_RE = re.compile(r"^h:(?:[A-Za-z0-9+_-]{1,64}|[A-Za-z0-9+/]{27}=)$")

_ENCRYPTED_MESSAGE = (
    "note {path!r} uses end-to-end encryption, "
    "which this server does not support"
)

# A revision name may hold a lone UTF-16 unit: a hash that is one, or a string
# used as `ids`, whose characters become the hashes. SQLite TEXT is UTF-8 and
# cannot hold it. Such a key is stored with its units spelled out behind a
# marker; every other key is stored as it is, so state written before is read
# unchanged. The name itself is kept as the device spells it.
_KEY_ESCAPED = "\x00units:"


def _db_key(text: str) -> str:
    """A document, revision or digest name as SQLite can hold it."""
    if text.startswith(_KEY_ESCAPED) or any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        body = text.replace("\\", "\\\\")
        body = "".join(f"\\u{ord(char):04x}" if 0xD800 <= ord(char) <= 0xDFFF else char
                       for char in body)
        return _KEY_ESCAPED + body
    return text


def _db_unkey(text: str) -> str:
    """The name :func:`_db_key` stored."""
    if not text.startswith(_KEY_ESCAPED):
        return text
    body = text[len(_KEY_ESCAPED):]
    units, index = [], 0
    while index < len(body):
        if body[index] == "\\":
            if body[index + 1] == "\\":
                units.append("\\")
                index += 2
            elif body[index + 1] == "u":
                units.append(chr(int(body[index + 2:index + 6], 16)))
                index += 6
            else:
                raise ValueError("corrupt journal state key")
        else:
            units.append(body[index])
            index += 1
    return "".join(units)


# A chunk this state holds as a file only, written by releases that did not
# hold chunk documents: held for admission, as the device holds it.
_CACHE_ONLY = object()


def _replay_changed(record: dict, body: dict) -> bool:
    """Whether replaying a legacy record stored something else than the record
    itself. What the new state keeps elsewhere is not a change: the identity
    of a document lives in its slot's key, its chain in its tree, and a
    revision's deletion in its tree, from where the body takes its stamp.
    Every other key counts on both sides, a stub as it was written no less
    than attachment data made a stub of."""
    def kept(value, dropped):
        return {key: item for key, item in value.items() if key not in dropped}

    elsewhere = _RESERVED_WORDS - _DATA_WORDS
    return kept(record, elsewhere) != kept(body, {"_id", "_rev", "_deleted"})


class UnsupportedStateLayout(Exception):
    """A state directory no release wrote; it is kept beside a fresh one."""


class _Slots:
    """The stores the IndexedDB adapter's transaction writes to, for
    :func:`revision_tree.write_batch`: the by-sequence bodies, the attachment
    mappings and the attachment data. Records which bodies were written and
    which slots were emptied."""

    def __init__(self, conn):
        self._conn = conn
        self.bodies: dict = {}
        self.removed: set = set()

    @property
    def written(self) -> set:
        return set(self.bodies)

    def merge(self, other: "_Slots") -> None:
        self.bodies.update(other.bodies)
        self.removed |= other.removed

    def slot(self, doc_id, rev) -> str:
        return _db_key(_seq_key(doc_id, rev))

    def stored(self, doc_id, rev):
        key = self.slot(doc_id, rev)
        found = self._conn.execute("SELECT 1 FROM docs WHERE seq_key = ?", (key,)).fetchone()
        return key if found else None

    def store_body(self, doc_id, rev, body: dict) -> None:
        key = self.slot(doc_id, rev)
        self._conn.execute(
            "INSERT OR REPLACE INTO docs(seq_key, doc_id, rev, doc_json) VALUES (?, ?, ?, ?)",
            (key, _db_key(doc_id), _db_key(rev), _dump_doc(body)),
        )
        self._conn.execute("DELETE FROM text_verdicts WHERE doc_id=?", (_db_key(doc_id),))
        self.bodies[(doc_id, rev)] = body

    def drop_body(self, key: str) -> None:
        self._conn.execute("DELETE FROM docs WHERE seq_key = ?", (key,))
        self.removed.add(key)

    def following(self, key: str, after):
        """The mapping of the slot ``key`` stored after position ``after``, as
        ``(position, digest)``; the store holds mappings in the order they were
        put in, as the adapter's does (an auto-increment key)."""
        row = self._conn.execute(
            "SELECT rowid, digest FROM attachment_digests WHERE seq_key = ? AND rowid > ?"
            " ORDER BY rowid LIMIT 1", (key, after if after is not None else 0)).fetchone()
        return None if row is None else (row[0], _db_unkey(row[1]))

    def map(self, digest: str, key: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO attachment_digests(digest, seq_key) VALUES (?, ?)",
            (_db_key(digest), key))

    def unmap(self, digest: str, key: str) -> None:
        self._conn.execute("DELETE FROM attachment_digests WHERE digest = ? AND seq_key = ?",
                           (_db_key(digest), key))

    def mapped(self, digest: str) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM attachment_digests WHERE digest = ?",
                                  (_db_key(digest),)).fetchone()[0]

    def hold(self, digest: str) -> None:
        self._conn.execute("INSERT OR IGNORE INTO attachment_data(digest) VALUES (?)", (_db_key(digest),))

    def held(self, digest: str) -> bool:
        return self._conn.execute("SELECT 1 FROM attachment_data WHERE digest = ?",
                                  (_db_key(digest),)).fetchone() is not None

    def forget(self, digest: str) -> None:
        self._conn.execute("DELETE FROM attachment_data WHERE digest = ?", (_db_key(digest),))


def _seq_key(doc_id: str, rev: str) -> str:
    """The by-sequence slot the IndexedDB adapter stores a body in
    (pouchdb-adapter-idb 9.0.0 `lib/index.js:472`). Two documents whose id
    and revision spell one slot share it, as they do on a device."""
    return doc_id + "::" + rev


def _decoded(body: dict, seq_key: str) -> dict:
    """The body as that adapter hands it back (``decodeDoc``, `lib/index.js:75-83`):
    it stores no ``_id`` and ``_rev`` and reads both off the slot's key, cut at
    the key's last colon. A revision that holds a colon does not survive that."""
    cut = seq_key.rfind(":")
    body["_id"] = seq_key[:max(cut - 1, 0)]
    body["_rev"] = seq_key[cut + 1:]
    return body


class NoteDecodeError(ValueError):
    """Typed corruption context for backend/tool projection."""

    def __init__(self, public):
        self.diagnostic = public
        super().__init__(public.format())


class EncryptedNoteError(NoteDecodeError):
    """Structural encryption classification; no exception-text parsing."""

    def __init__(self, public=None, *, path=None):
        super().__init__(public if isinstance(public, PublicDiagnostic) else
            PublicDiagnostic(DiagnosticCode.ENCRYPTED_NOTE,
                paths=(DiagnosticPath(path, remote=True),) if path is not None else ()))


_CREATE_REVISIONS = """
CREATE TABLE IF NOT EXISTS docs (
    seq_key TEXT NOT NULL PRIMARY KEY,
    doc_id TEXT NOT NULL,
    rev TEXT NOT NULL,
    doc_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rev_trees (
    doc_id TEXT PRIMARY KEY,
    tree TEXT NOT NULL
);
"""

_SCHEMA_V1 = _CREATE_REVISIONS + """
CREATE TABLE IF NOT EXISTS processed (
    name TEXT NOT NULL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS outbound (
    name TEXT NOT NULL PRIMARY KEY,
    digest TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachment_digests (
    digest TEXT NOT NULL,
    seq_key TEXT NOT NULL,
    PRIMARY KEY (digest, seq_key)
);
CREATE TABLE IF NOT EXISTS attachment_data (
    digest TEXT NOT NULL PRIMARY KEY
);
"""


def _private_directory(path: Path) -> None:
    """Create private directories and tighten previously permissive caches."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
        logger.warning('tightened journal state directory permissions: <omitted>')


class JournalStore:
    """Local journal-peer state rooted at ``state_dir``.

    Active persistence lives under ``state_dir``: the SQLite database and the
    ``chunks/`` tree. Identity changes archive it in a sibling quarantine.
    See the module docstring for the format and revision-tree semantics.
    """

    def state_size_bytes(self) -> int:
        """Logical bytes in SQLite (including WAL/SHM) and the chunk tree.

        Best-effort measurement: external state maintenance may move files
        during the walk. Do not follow symlinks outside this peer's state.
        """
        paths = [self.state_dir / name for name in
                 (_DB_FILENAME, _DB_FILENAME + "-wal", _DB_FILENAME + "-shm")]
        def file_size(path: Path) -> int:
            try:
                info = path.lstat()
                return info.st_size if stat.S_ISREG(info.st_mode) else 0
            except FileNotFoundError:
                return 0

        total = sum(file_size(path) for path in paths)
        for root, dirs, files in os.walk(self._chunks_dir, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            total += sum(file_size(Path(root) / name) for name in files)
        return total

    def __init__(self, state_dir: Path, path_identity: str, *,
                 text_verdict_cache_entries: int = DEFAULT_TEXT_VERDICT_CACHE_ENTRIES):
        if text_verdict_cache_entries <= 0:
            raise ValueError("text verdict cache entries must be positive")
        self.text_verdict_cache_entries = text_verdict_cache_entries
        if path_identity not in (
            PATH_IDENTITY_LEGACY, PATH_IDENTITY_NFC,
            PATH_IDENTITY_CASE_SENSITIVE, PATH_IDENTITY_NFC_CASE_SENSITIVE,
        ):
            raise ValueError(
                f"unknown path identity {path_identity!r}; expected "
                f"{PATH_IDENTITY_LEGACY!r}, {PATH_IDENTITY_NFC!r}, "
                f"{PATH_IDENTITY_CASE_SENSITIVE!r} or {PATH_IDENTITY_NFC_CASE_SENSITIVE!r}"
            )
        self.state_dir = Path(state_dir)
        self.path_identity = path_identity
        self._chunks_dir = self.state_dir / _CHUNKS_DIRNAME
        db_path = self.state_dir / _DB_FILENAME
        # Check every persistence entry before chmod, traversal, or SQLite open.
        # is_symlink also detects dangling links.
        for path, label in (
            (self.state_dir, "state directory"),
            (self._chunks_dir, "chunks directory"),
            (db_path, "sqlite file"),
        ):
            if path.is_symlink():
                raise StorageError(
                    f"{label} is a symlink; refusing to operate through it — "
                    "move the real directory or file and remove the link"
                )
        _private_directory(self.state_dir)
        _private_directory(self._chunks_dir)
        for directory in self._chunks_dir.rglob("*"):
            if directory.is_dir() and not directory.is_symlink():
                _private_directory(directory)
        # Pin the chunk root for resolved-path containment checks.
        self._chunks_root = self._chunks_dir.resolve()

        descriptor = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        db_path.chmod(0o600)
        self._conn = sqlite3.connect(db_path)
        # Check identity before migrations can change the old database.
        has_meta = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        stored_identity = self._meta_get(_META_PATH_IDENTITY) if has_meta else None
        if stored_identity is not None and stored_identity != path_identity:
            self._conn.close()
            self._quarantine(
                'journal path identity changed <omitted> -> <omitted>; old state quarantined at '
                '<omitted>. To restore: stop all peers using this state directory, move the '
                'current state aside, move <omitted> back to <omitted> and configure identity <omitted>')
            self.__init__(self.state_dir, path_identity,
                          text_verdict_cache_entries=text_verdict_cache_entries)
            return
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA_V1)
        self._conn.execute("""CREATE TABLE IF NOT EXISTS text_verdicts (
            doc_id TEXT, rev TEXT, budget INTEGER, eligible INTEGER,
            PRIMARY KEY(doc_id, rev, budget))""")

        # Disposable cache migration: remove old positives and obsolete
        # revisions/budgets before enforcing one row per document.
        with self._conn:
            self._conn.execute('DELETE FROM text_verdicts WHERE eligible != 0')
            self._conn.execute('DELETE FROM text_verdicts WHERE rowid NOT IN '
                               '(SELECT MAX(rowid) FROM text_verdicts GROUP BY doc_id)')
            self._conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS '
                               'text_verdict_document ON text_verdicts(doc_id)')
            self._trim_text_verdicts()

        self._conn.execute("""CREATE TABLE IF NOT EXISTS selection_verdicts (
            cache_key TEXT PRIMARY KEY, rules TEXT, verdict TEXT, used INTEGER)""")
        self._conn.commit()
        try:
            self._rebuild_legacy_state()
        except (RecordRefused, UnsupportedStateLayout) as exc:
            # A device refuses a record this state was built from, or no release wrote
            # this layout: keep it beside a fresh one, which the next replay builds
            # exactly as the device does.
            self._conn.close()
            self._quarantine('journal state holds a record a device refuses; it was quarantined at '
                             '<omitted> and will be rebuilt from the journal'
                             if isinstance(exc, RecordRefused) else
                             'journal state was written by an unreleased build; it was quarantined at '
                             '<omitted> and will be rebuilt from the journal')
            self.__init__(self.state_dir, path_identity,
                          text_verdict_cache_entries=text_verdict_cache_entries)
            return

        stored_identity = self._meta_get(_META_PATH_IDENTITY)
        if stored_identity is None:
            self._meta_set(_META_PATH_IDENTITY, path_identity)

    def _quarantine(self, message: str) -> None:
        """Move the whole state directory aside, never over an earlier archive."""
        quarantine_root = self.state_dir.with_name(self.state_dir.name + ".quarantine")
        if quarantine_root.is_symlink():
            raise StorageError("quarantine directory is a symlink; refusing to move state")
        _private_directory(quarantine_root)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-")
        destination = Path(tempfile.mkdtemp(prefix=stamp, dir=quarantine_root))
        # If the move fails, retain both the source and reservation.
        self.state_dir.rename(destination / "state")
        logger.warning(message)

    def _rebuild_legacy_state(self) -> None:
        """Rebuild trees for state written before files were replayed as a device does.

        Earlier releases stored every record under its ``_rev`` in arrival
        order. Each one is replayed again, on its own, through the same
        planning a journal file gets; a record that is written is stored under
        the revision the device keeps. Everything else keeps its body as
        history. No body the old state held is dropped: a row whose body no
        stored revision holds any more, because a later row's replay took its
        place or stemmed its revision out of the tree, is kept as it was in
        ``legacy_bodies``; so is a row whose replay stored something else
        than the record, attachment data made a stub of. The result is exact
        for well-formed histories; an arrival order that only stemming
        distinguishes is rebuilt by a full replay after a wipe. A layout no
        release wrote raises :class:`UnsupportedStateLayout`.
        """
        # One explicit transaction: the driver's implicit one excludes DDL, a
        # crash between the rename and the copy must leave the old table in
        # place, and a second process opening the same directory meanwhile
        # waits here and then finds the migrated schema.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(docs)")}
            if "seq_key" in columns:
                self._conn.execute("COMMIT")
                return
            if "rev" in columns:
                # A development build between the releases stored bodies by revision.
                raise UnsupportedStateLayout
            self._conn.execute("ALTER TABLE docs RENAME TO docs_legacy")
            for statement in _CREATE_REVISIONS.split(";"):
                if statement.strip():
                    self._conn.execute(statement)
            trees: dict = {}
            rows = self._conn.execute(
                "SELECT rowid, doc_id, gen, rev_hash, doc_json FROM docs_legacy ORDER BY rowid"
            ).fetchall()
            # Which by-sequence slot, as it is spelled in the table, holds whose body, so
            # that a legacy row whose body is overwritten, compacted or never kept is kept
            # aside before the old table goes.
            holder: dict = {}
            held: dict = {}
            archived: set = set()

            def keep(key, index):
                previous = holder.get(key)
                if previous is not None and previous != index:
                    held[previous].discard(key)
                    if not held[previous] and previous not in archived:
                        archive(previous)
                holder[key] = index
                held.setdefault(index, set()).add(key)

            def drop(key):
                # A merge stemmed the revision of that slot away: the body it
                # held is gone, as it is on a device.
                previous = holder.pop(key, None)
                if previous is not None:
                    held[previous].discard(key)
                    if not held[previous] and previous not in archived:
                        archive(previous)

            def archive(index):
                if not archived:
                    self._conn.execute("""CREATE TABLE legacy_bodies (
    doc_id TEXT NOT NULL,
    gen INTEGER NOT NULL,
    rev_hash TEXT NOT NULL,
    doc_json TEXT NOT NULL,
    PRIMARY KEY (doc_id, gen, rev_hash)
)""")
                archived.add(index)
                _, old_id, old_gen, old_hash, old_json = rows[index]
                self._conn.execute(
                    "INSERT OR REPLACE INTO legacy_bodies(doc_id, gen, rev_hash, doc_json)"
                    " VALUES (?, ?, ?, ?)", (old_id, old_gen, old_hash, old_json))

            for index, (_, doc_id, gen, rev_hash, doc_json) in enumerate(rows):
                doc = json.loads(doc_json)
                # A stub may name an attachment a row replayed just before stored.
                plan = plan_file([doc], trees.get, has_attachment=self._has_attachment)
                if plan.failed:
                    raise plan.refusal
                kept = self._write_plan(plan.chunk_rows, plan.chunk_trees)
                kept.merge(self._write_plan(plan.rows, plan.trees))
                trees.update(plan.chunk_trees)
                trees.update(plan.trees)
                for doc_id_written, rev_written in kept.written:
                    keep(_db_key(_seq_key(doc_id_written, rev_written)), index)
                for key in kept.removed:
                    drop(key)
                name = f"{gen}-{rev_hash}"
                stored = kept.bodies.get((doc_id, name))
                original = _db_key(_seq_key(doc_id, name))
                if stored is None:
                    if original not in holder:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO docs(seq_key, doc_id, rev, doc_json) VALUES (?, ?, ?, ?)",
                            (original, _db_key(doc_id), _db_key(name), doc_json),
                        )
                        keep(original, index)
                elif _replay_changed(doc, stored):
                    archive(index)
                if not held.get(index) and index not in archived:
                    archive(index)
            self._conn.execute("DROP TABLE docs_legacy")
            for doc_id, tree in trees.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO rev_trees(doc_id, tree) VALUES (?, ?)",
                    (_db_key(doc_id), json.dumps(tree)),
                )
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _tree(self, doc_id: str):
        row = self._conn.execute(
            "SELECT tree FROM rev_trees WHERE doc_id = ?", (_db_key(doc_id),)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def _body(self, doc_id: str, rev: str) -> dict | None:
        """The body stored in the by-sequence slot ``doc_id::rev``, read as the
        IndexedDB adapter reads it, or ``None`` when nothing is stored there."""
        row = self._conn.execute(
            "SELECT seq_key, doc_json FROM docs WHERE seq_key = ?",
            (_db_key(_seq_key(doc_id, rev)),),
        ).fetchone()
        if row is None:
            return None
        return _decoded(json.loads(row[1]), _db_unkey(row[0]))

    def _write_plan(self, rows, trees) -> _Slots:
        """Store one planned batch in the order a device's transaction stores
        it (:func:`revision_tree.write_batch`: the records of every document
        start together, and a shared slot holds the body stored last in the
        adapter's request order), then the trees. Returns what was written:
        the bodies by ``(doc_id, rev)`` and the by-sequence slots emptied.
        """
        slots = _Slots(self._conn)
        write_batch(rows, slots)
        for doc_id, tree in trees.items():
            self._conn.execute(
                "INSERT OR REPLACE INTO rev_trees(doc_id, tree) VALUES (?, ?)",
                (_db_key(doc_id), json.dumps(tree)),
            )
        return slots

    # -- Journal replay ------------------------------------------------------

    def apply_journal(
        self, name: str, chunks: dict[str, str], docs: list[dict],
        records: list[dict] | None = None,
    ) -> None:
        """Replay one parsed journal file the way a device receives it; idempotent per ``name``.

        ``records`` is the file's documents in order, the documents the decoder
        makes of compact chunk records included: the chunk batch is taken from
        it, so a chunk the state holds is kept as a device keeps it and two
        records for one chunk fail the file. Without ``records``, ``docs`` are
        the records and ``chunks`` the compact ones ahead of them. The records
        are planned (:func:`revision_tree.plan_file`) and stored in two
        commits, the chunk batch and then the metadata batch, as a device
        commits them before it judges the file: a refused metadata batch
        leaves the chunk batch in place, a batch with one refused record keeps
        its other records, and the file stays unprocessed for the retry.
        Chunk documents live in the database as every document does; no file
        is written for them. Raises :class:`~revision_tree.RecordRefused`
        where a device fails the file.
        """
        if self.processed(name):
            return
        if records is None:
            records = [
                {"_id": chunk_id, "data": data, "type": "leaf", "_rev": ""}
                for chunk_id, data in (chunks or {}).items()
            ] + list(docs or [])
        plan = self._plan(list(records))
        # Each batch is stored as a device commits it, before the file is
        # judged: a refused metadata batch holds nothing here, a batch with
        # one refused record holds the other records.
        with self._conn:
            self._write_plan(plan.chunk_rows, plan.chunk_trees)
        with self._conn:
            self._write_plan(plan.rows, plan.trees)
        if plan.failed:
            raise plan.refusal
        self.finish_bookkeeping(lambda: self.mark_processed(name))

    def _cache_only(self, doc_id: str) -> bool:
        """A chunk this state holds as a file and no document."""
        try:
            return self._chunk_path(doc_id).is_file()
        except ValueError:
            return False

    def _plan(self, records: list[dict]):
        """Plan the file's records against the stored state, reading each tree once."""
        trees: dict = {}

        def tree_of(doc_id):
            if doc_id not in trees:
                trees[doc_id] = self._tree(doc_id)
                if trees[doc_id] is None and self._cache_only(doc_id):
                    trees[doc_id] = _CACHE_ONLY
            return trees[doc_id]

        try:
            return plan_file(records, tree_of, has_attachment=self._has_attachment)
        except RecordRefused as refused:
            raise RecordRefused(refused.reason, refused.stage) from None

    def _has_attachment(self, digest) -> bool:
        # verifyAttachment reads the attachment store: a stub may only name an
        # attachment whose data an earlier batch stored and no compaction dropped.
        return isinstance(digest, str) and self._conn.execute(
            "SELECT 1 FROM attachment_data WHERE digest = ?", (_db_key(digest),)
        ).fetchone() is not None

    def validate_journal(
        self, chunks: dict[str, str], docs: list[dict], records: list[dict] | None = None
    ) -> None:
        """Check that a publication replays as a device receives it; change nothing."""
        for chunk_id, data in (chunks or {}).items():
            self._chunk_path(chunk_id)
            data.encode("utf-8")
        for doc in docs:
            _dump_doc(doc)
        plan = self._plan(list(docs) if records is None else list(records))
        if plan.failed:
            raise plan.refusal

    def _chunk_path(self, chunk_id: str) -> Path:
        if not isinstance(chunk_id, str) or _CHUNK_ID_RE.fullmatch(chunk_id) is None:
            raise ValueError("invalid journal chunk identifier")
        # Escape only the cache filename, never the journal ID. SHA-1's '='
        # suffix makes these paths disjoint from historical IDs; '/' -> '_'
        # is injective over the standard base64 alphabet (which excludes '_').
        digest = chunk_id[len("h:") :].replace("/", "_")
        if len(digest) < 3:
            chunk_file = self._chunks_dir / _SHORT_SHARD / digest
        else:
            chunk_file = self._chunks_dir / digest[:2] / digest[2:]
        if not chunk_file.resolve().is_relative_to(self._chunks_root):
            raise ValueError("journal chunk path escapes chunk store")
        return chunk_file

    # -- Processed ledger and meta ------------------------------------------

    def processed(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed WHERE name = ?", (name,)
        ).fetchone()
        return row is not None

    def finish_bookkeeping(self, action) -> None:
        """Retry tracking after metadata commit; replay repairs exhausted attempts."""
        for attempt in range(3):
            try:
                action()
                return
            except (OSError, sqlite3.Error):
                if attempt == 2:
                    logger.warning('completed journal bookkeeping deferred to replay')

    def mark_processed(self, name: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed(name) VALUES (?)", (name,)
            )

    def files_processed_count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
        )

    def record_outbound(self, name: str, digest: str) -> None:
        """Commit intent before sending; a crash must not lose send ownership."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO outbound(name, digest) VALUES (?, ?)", (name, digest),
            )

    def confirm_outbound(self, name: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE outbound SET confirmed = 1 WHERE name = ?", (name,))

    def discard_outbound(self, name: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM outbound WHERE name = ?", (name,))

    def outbound_journals(self) -> list[tuple[str, str, int]]:
        return self._conn.execute("SELECT name, digest, confirmed FROM outbound").fetchall()

    def compact_outbound(self, newest: str) -> None:
        """Retain the newest confirmed wipe sentinel and unresolved intents."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM outbound WHERE confirmed = 1 AND name != ?", (newest,),
            )

    @property
    def last_sent_key(self) -> str:
        return self._meta_get(_META_LAST_SENT_KEY) or ""

    @last_sent_key.setter
    def last_sent_key(self, value: str) -> None:
        self._meta_set(_META_LAST_SENT_KEY, value)

    @property
    def epoch(self) -> str:
        return self._meta_get(_META_EPOCH) or ""

    @epoch.setter
    def epoch(self, value: str) -> None:
        # Epoch alone does not prove a wipe (upstream JournalSyncCore:354-388).
        # The backend reconciles send history before clearing dedupe caches.
        self._meta_set(_META_EPOCH, value)

    def selection_cache_size(self):
        return self._conn.execute('SELECT COUNT(*) FROM selection_verdicts').fetchone()[0]

    def _selection_cache_trim(self, limit):
        self._conn.execute('DELETE FROM selection_verdicts WHERE cache_key NOT IN '
                           '(SELECT cache_key FROM selection_verdicts ORDER BY used DESC LIMIT ?)', (limit,))

    def selection_cache_get(self, key, rules, limit):
        with self._conn:
            self._conn.execute('DELETE FROM selection_verdicts WHERE rules != ?', (rules,))
            self._selection_cache_trim(limit)
            row = self._conn.execute('SELECT verdict FROM selection_verdicts WHERE cache_key = ?', (key,)).fetchone()
            if row is not None:
                self._conn.execute('UPDATE selection_verdicts SET used = '
                                   '(SELECT COALESCE(MAX(used), 0) + 1 FROM selection_verdicts) '
                                   'WHERE cache_key = ?', (key,))
        return json.loads(row[0]) if row else None

    def selection_cache_put(self, key, rules, value, limit):
        with self._conn:
            self._conn.execute('INSERT OR REPLACE INTO selection_verdicts VALUES (?, ?, ?, '
                               '(SELECT COALESCE(MAX(used), 0) + 1 FROM selection_verdicts))',
                               (key, rules, json.dumps(value)))
            self._selection_cache_trim(limit)

    def reset(self) -> None:
        """Clear replication deduplication caches, retaining every local copy.

        Like device checkpoint resets (JournalSyncCore.ts:332-341,392-400),
        this never removes revision trees or content-addressed chunk files.
        Identity, epoch, and read/selection verdicts remain valid locally.
        """
        with self._conn:
            self._conn.execute("DELETE FROM processed")
            self._conn.execute("DELETE FROM outbound")
            self._conn.execute("DELETE FROM meta WHERE key = ?", (_META_LAST_SENT_KEY,))
        logger.warning('journal store dedupe caches reset; local documents and chunks retained')

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row[0]

    def _meta_set(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, value),
            )

    # -- Revision trees ------------------------------------------------------

    def winner(self, doc_id: str) -> dict | None:
        """The body of the winning revision for ``doc_id``, or ``None``.

        The body carries ``_deleted`` exactly when the tree marks the winner
        revision-deleted, and its attachments as stubs, as a device serves a
        document; LiveSync's ``deleted`` flag stays as stored. ``None`` when
        no body is stored under the winning revision's name, as a device's
        ``get`` reports it missing. A revision-deleted winner carries
        ``_deleted``; a live winner's body keeps the flag it was stored with,
        which over a shared slot is another document's. Raises
        :class:`NoteDecodeError` when a body is stored but the tree does not
        name the revision it spells, the one the IndexedDB adapter read off
        the slot's key: a device's ``get`` with ``revs: true`` fails on such
        a revision, so it is not served here under any name.
        """
        tree = self._tree(doc_id)
        if tree is None:
            return None
        rev = winning_rev(tree)
        body = self._body(doc_id, rev)
        if body is None:
            # A device's get looks the winning revision up by name and reports
            # it missing before it reads the tree.
            logger.warning('journal state lacks the body of a winning revision')
            return None
        if is_deleted(tree, rev):
            # A device's get answers "deleted" from the tree before it reads
            # the body's revision.
            return _served(body, True)
        # The body's revision is the one the IndexedDB adapter read off the
        # slot's key; the chain is looked up for it, as a device's get does.
        _require_named_revision(tree, body["_rev"], _display_path(body, doc_id))
        return _served(body, False)

    def raw_get(self, doc_id: str) -> dict | None:
        """The body a raw ``get`` returns, which the plugin reads the base of
        a file or a deletion with (`EntryManagerImpls.ts:295, 746`), or
        ``None`` where that ``get`` reports the document missing: no document,
        a winner the tree marks revision-deleted, or no body in the winner's
        slot.

        The body is the one the slot holds, whichever document stored it,
        with the identity the IndexedDB adapter read off the slot's key and
        the flags it was stored with; the tree of the document asked for is
        not read into it.
        """
        tree = self._tree(doc_id)
        if tree is None:
            return None
        rev = winning_rev(tree)
        if is_deleted(tree, rev):
            return None
        body = self._body(doc_id, rev)
        return None if body is None else _with_stubs(body)

    def delete_base(self, doc_id: str, path: str) -> dict | None:
        """The object a device's deletion is put back with: the body of a
        raw ``get``. Raises :class:`NoteDecodeError` for a revision core
        refuses: the plugin hands that body to ``put``, which fails before
        anything is written.
        """
        base = self.raw_get(doc_id)
        if base is not None and not valid_rev(base["_rev"]):
            raise NoteDecodeError(PublicDiagnostic(
                DiagnosticCode.IS_CORRUPT_REVISION_NOT_NAMED_BY_THE_TREE,
                paths=(DiagnosticPath(path, remote=True),)))
        return base

    def put_base(self, doc_id: str, path: str) -> dict | None:
        """The base a file is put over on a device, or ``None`` where no
        document is and the write starts a tree.

        That is the body a raw ``get`` hands to the ``put``
        (`EntryManagerImpls.ts:295`), with the revision core refuses refused
        as a deletion's is. Where that ``get`` reports the document missing
        and one is, the ``put`` goes without a revision and PouchDB attaches
        it below a revision-deleted winner the tree names
        (pouchdb-adapter-utils ``updateDoc``): the winner is returned for
        that, its stored body if one is. Any other such document makes
        PouchDB answer the write with a conflict, and raises
        :class:`NoteDecodeError` here, so that nothing is published over it.
        """
        tree = self._tree(doc_id)
        if tree is None:
            return None
        base = self.delete_base(doc_id, path)
        if base is not None:
            return base
        rev = winning_rev(tree)
        if _named(tree, rev):
            if is_deleted(tree, rev):
                # A raw get reports this winner missing, so the put names no
                # revision of the body's; PouchDB takes the winner's.
                base = _served(self._body(doc_id, rev) or {"_id": doc_id}, True)
                base["_id"], base["_rev"] = doc_id, rev
                return base
            raise NoteDecodeError(PublicDiagnostic(
                DiagnosticCode.IS_CORRUPT_THE_WINNING_REVISION_HAS_NO_STORED_BODY,
                paths=(DiagnosticPath(path, remote=True),)))
        raise NoteDecodeError(PublicDiagnostic(
            DiagnosticCode.IS_CORRUPT_REVISION_NOT_NAMED_BY_THE_TREE,
            paths=(DiagnosticPath(path, remote=True),)))

    def leaves(self, doc_id: str) -> list[dict]:
        """Leaf revision bodies for ``doc_id``, highest position first.

        Every branch a device still holds, in the order of PouchDB's
        ``collectLeaves``: losing siblings of concurrent devices and revision
        tombstones included.
        """
        tree = self._tree(doc_id)
        if tree is None:
            return []
        return [self._leaf(doc_id, rev, deleted) for rev, deleted in collect_leaves(tree)]

    def conflicts(self, doc_id: str) -> list[dict]:
        """Live leaves other than the winner, in leaf order."""
        tree = self._tree(doc_id)
        if tree is None:
            return []
        return [self._leaf(doc_id, rev, False) for rev in collect_conflicts(tree)]

    def revisions(self, doc_id: str) -> list[dict]:
        """Every stored revision for ``doc_id`` in arrival order."""
        rows = self._conn.execute(
            "SELECT seq_key, doc_json FROM docs WHERE doc_id = ? ORDER BY rowid", (_db_key(doc_id),)
        )
        return [_decoded(json.loads(stored), _db_unkey(seq_key)) for seq_key, stored in rows]

    def revision_chain(self, doc_id: str, rev: str) -> dict:
        """The ``_revisions`` a device reports for ``rev``; see
        :func:`revision_tree.revision_chain`."""
        tree = self._tree(doc_id)
        if tree is None:
            raise ValueError("revision is not in the tree")
        return revision_chain(tree, rev)

    def _leaf(self, doc_id: str, rev: str, deleted: bool) -> dict:
        body = self._body(doc_id, rev) or {"_id": doc_id, "_rev": rev}
        return _served(body, deleted)

    # -- Note reads ----------------------------------------------------------

    def note_ids(self) -> list[str]:
        """Sorted ids of live notes: documents whose winning revision is a live note."""
        result: list[str] = []
        for stored_id, stored in self._conn.execute(
            "SELECT doc_id, tree FROM rev_trees ORDER BY doc_id"
        ):
            doc_id = _db_unkey(stored_id)
            if doc_id.startswith("h:"):
                # A chunk document is never a note (journalReceive.ts:8-15).
                continue
            tree = json.loads(stored)
            rev = winning_rev(tree)
            if is_deleted(tree, rev):
                continue
            body = self._body(doc_id, rev)
            if body is not None and is_note_document(body):
                result.append(doc_id)
        # By name: a spelled-out key would otherwise sort by its marker.
        return sorted(result)

    def read_note(
        self, doc_id: str
    ) -> tuple[dict, str | None, bytes | None, bool] | None:
        """Read a note: ``(winner_doc, text, data, is_text)``.

        ``None`` when the document is unknown, not a note, or tombstoned.
        Raises :class:`EncryptedNoteError` (a ``ValueError``) for E2EE
        refusals and plain ``ValueError`` for corrupt bodies (missing
        chunk, invalid fields); use :meth:`iterate_notes` for per-note
        isolation.
        """
        winner = self.winner(doc_id)
        if winner is None or not is_note_document(winner):
            return None
        return self._read_note_content(winner)

    def read_ignore_file(self, doc_id: str):
        """Decode only a configured ignore document, including hidden i: metadata.

        Public note reads retain their service-namespace gate. This private
        backend seam never lists or exposes internal documents to MCP callers.
        """
        winner = self.winner(doc_id)
        if (winner is None or _js_truthy(winner.get('deleted')) or _js_truthy(winner.get('_deleted'))
                or winner.get('type') not in ('plain', 'newnote', 'notes')):
            return None
        if not doc_id.startswith('i:') and not is_note_document(winner):
            return None
        return self._read_note_content(winner)

    def _read_note_content(self, winner: dict):
        doc_id = winner['_id']
        path = _display_path(winner, doc_id)

        if winner.get("e_"):
            raise EncryptedNoteError(PublicDiagnostic(DiagnosticCode.ENCRYPTED_NOTE, paths=(DiagnosticPath(path, remote=True),)))

        doc_type = winner.get("type")
        if doc_type == "notes":
            raw = winner.get("data")
            if isinstance(raw, str) and raw.startswith("%"):
                raise EncryptedNoteError(PublicDiagnostic(DiagnosticCode.ENCRYPTED_NOTE, paths=(DiagnosticPath(path, remote=True),)))
            try:
                text = legacy_body(winner)
            except ValueError:
                raise NoteDecodeError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_MISSING_LEGACY_DATA_FIELD, paths=(DiagnosticPath(path, remote=True),))) from None
            return winner, text, None, True

        # Both entry points restrict non-legacy content to plain/newnote.
        children = winner.get("children")
        if children is None:
            children = []
        if not isinstance(children, list) or not all(
            isinstance(child, str) for child in children
        ):
            raise NoteDecodeError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_INVALID_CHILDREN_FIELD, paths=(DiagnosticPath(path, remote=True),)))
        try:
            inline = extract_inline_chunks(winner)
        except EncryptedInlineChunk:
            raise EncryptedNoteError(
                PublicDiagnostic(DiagnosticCode.ENCRYPTED_NOTE, paths=(DiagnosticPath(path, remote=True),))
            ) from None
        except ValueError:
            raise NoteDecodeError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_INVALID_EDEN_FIELD, paths=(DiagnosticPath(path, remote=True),))) from None

        if not children:
            if doc_type == "plain":
                return winner, "", None, True
            return winner, None, b"", False

        datas = self._chunk_datas(children, inline, path)
        if doc_type == "plain":
            return winner, assemble_text(datas), None, True
        try:
            body = assemble_binary(datas)
        except ValueError:
            raise NoteDecodeError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_INVALID_CHUNK_ENCODING, paths=(DiagnosticPath(path, remote=True),))) from None
        return winner, None, body, False

    def scan_newnote(self, doc: dict, max_bytes: int, *, strict_budget=False) -> str | None:
        # SQLite INTEGER is signed 64-bit; a BLOB keeps larger budgets exact.
        cache_budget = max_bytes if max_bytes < 2 ** 63 else str(max_bytes).encode('ascii')
        key = (doc['_id'], doc['_rev'], cache_budget)
        verdict = self._conn.execute(
            'SELECT rev, budget FROM text_verdicts WHERE doc_id=?',
            (_db_key(doc['_id']),)).fetchone()
        if verdict is not None and (_db_unkey(verdict[0]), verdict[1]) == key[1:] and not strict_budget:
            self._remember_nontext(key)
            return None
        if verdict is not None:
            with self._conn:
                self._conn.execute('DELETE FROM text_verdicts WHERE doc_id=?', (_db_key(doc['_id']),))
        if doc.get('e_'):
            raise EncryptedNoteError(PublicDiagnostic(DiagnosticCode.ENCRYPTED_NOTE, paths=(DiagnosticPath(doc['path'], remote=True),)))
        children = doc.get('children') or []
        if not isinstance(children, list) or not all(isinstance(c, str) for c in children):
            raise ValueError('invalid children field')
        inline = extract_inline_chunks(doc)
        projection = TextProjection(max_bytes)
        eligible = True
        for index, child in enumerate(children):
            piece = self._chunk_datas([child], inline, doc['path'])[0]
            if not projection.feed(piece, final=index == len(children) - 1):
                eligible = False
                projection.parts.clear()
                if not strict_budget:
                    break
        if strict_budget and max_bytes and projection.bytes > max_bytes:
            raise TextBudgetExceeded(projection.bytes, max_bytes)
        if not eligible:
            self._remember_nontext(key)
        return projection.text() if eligible else None

    def _remember_nontext(self, key: tuple[str, str, int]) -> None:
        # Reinsert on a hit so rowid orders least-to-most recently used.
        with self._conn:
            self._conn.execute('DELETE FROM text_verdicts WHERE doc_id=?', (_db_key(key[0]),))
            self._conn.execute('INSERT INTO text_verdicts VALUES (?, ?, ?, 0)',
                               (_db_key(key[0]), _db_key(key[1]), key[2]))
            self._trim_text_verdicts()

    def _trim_text_verdicts(self) -> None:
        self._conn.execute(
            'DELETE FROM text_verdicts WHERE rowid IN '
            '(SELECT rowid FROM text_verdicts ORDER BY rowid DESC LIMIT -1 OFFSET ?)',
            (self.text_verdict_cache_entries,))

    def _chunk_datas(
        self, children: list[str], inline: dict[str, str], path: str
    ) -> list[str]:
        """Resolve chunk data in children order: eden inline first, then the
        chunk document, then a file a release before chunk documents wrote
        for a chunk no document names."""
        datas: list[str] = []
        for child in children:
            try:
                chunk_file = self._chunk_path(child)
            except ValueError:
                raise NoteDecodeError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_INVALID_CHUNK_REFERENCE, paths=(DiagnosticPath(path, remote=True),))) from None
            if child in inline:
                datas.append(inline[child])
                continue
            if self._tree(child) is not None:
                # A device reads the chunk document: what its winner holds if the
                # tree names one, and nothing once it is deleted or gone.
                chunk_doc = self.winner(child)
                if chunk_doc is not None:
                    if chunk_doc.get("e_"):
                        raise EncryptedNoteError(
                            PublicDiagnostic(DiagnosticCode.ENCRYPTED_NOTE, paths=(DiagnosticPath(path, remote=True),)
                            )
                        )
                    # A revision-deleted chunk is deleted on every device too.
                    data = chunk_doc.get("data")
                    if (not _js_truthy(chunk_doc.get("_deleted"))
                            and not _js_truthy(chunk_doc.get("deleted")) and isinstance(data, str)):
                        datas.append(data)
                        continue
            elif chunk_file.is_file():
                # A chunk a release before chunk documents held as a file only.
                try:
                    datas.append(chunk_file.read_bytes().decode("utf-8"))
                except (OSError, UnicodeDecodeError):
                    raise NoteDecodeError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_UNREADABLE_CHUNK_FILE, paths=(DiagnosticPath(path, remote=True),))) from None
                continue
            raise NoteDecodeError(PublicDiagnostic(DiagnosticCode.IS_CORRUPT_A_CONTENT_CHUNK_IS_MISSING, paths=(DiagnosticPath(path, remote=True),)))
        return datas

    def iterate_notes(self, extension: str = ".md"):
        """Yield ``(winner_doc, text_or_None)`` for notes ending in ``extension``.

        Corrupt or encrypted notes yield ``text=None`` instead of raising,
        so one bad note never stops an iteration (Ruling 1 isolation). The
        extension match is case-sensitive, like upstream ``isPlainText``.
        """
        for doc_id in self.note_ids():
            doc = self.winner(doc_id)
            if doc is None:
                continue
            if not _display_path(doc, doc_id).endswith(extension):
                continue
            try:
                content = self.read_note(doc_id)
            except ValueError:
                yield doc, None
                continue
            if content is None:
                continue
            yield doc, content[1]

    def close(self) -> None:
        self._conn.close()


def _dump_doc(doc: dict) -> str:
    # journalRecords.ts:76 preserves surrogate units in JSON metadata. SQLite
    # accepts UTF-8 text, so retain those units as escapes in the local copy too.
    # Retain local handling of replayed numeric overflows for note isolation;
    # strict wire publication validation belongs in serialize_journal.
    return escape_json_surrogates(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))


def _named(tree, rev: object) -> bool:
    """Whether a device reports a chain for ``rev`` (pouchdb-core ``get``)."""
    try:
        revision_chain(tree, rev)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _require_named_revision(tree, rev: object, path: str) -> None:
    """A device reports no chain for a revision its tree does not name by string
    (pouchdb-core ``get`` fails with ``invalid rev tree``); such a note is
    corrupt for it and is not served here either."""
    if not _named(tree, rev):
        raise NoteDecodeError(PublicDiagnostic(
            DiagnosticCode.IS_CORRUPT_REVISION_NOT_NAMED_BY_THE_TREE,
            paths=(DiagnosticPath(path, remote=True),)))


def _served(body: dict, deleted: bool) -> dict:
    """A stored body as a device serves a winner: deletion stamped from the
    tree, attachments stubs."""
    return _with_stubs(_mark_deletion(body, deleted))


def _with_stubs(body: dict) -> dict:
    """A stored body as core ``get`` hands it back: every attachment a stub."""
    attachments = body.get("_attachments")
    if isinstance(attachments, list):
        body["_attachments"] = [
            {**attachment, "stub": True} if isinstance(attachment, dict) else attachment
            for attachment in attachments]
    elif isinstance(attachments, dict):
        body["_attachments"] = {
            name: {**attachment, "stub": True} if isinstance(attachment, dict) else attachment
            for name, attachment in attachments.items()}
    return body


def _mark_deletion(body: dict, deleted: bool) -> dict:
    """Stamp a winner's deletion as a device answers it: from the tree for a
    revision-deleted winner, whose raw ``get`` reports it missing before any
    body is read. A live winner's body keeps the flag it was stored with: the
    plugin reads ``_deleted`` off the body (`EntryManagerImpls.ts:435-436`),
    and over a shared slot that is another document's."""
    body = dict(body)
    if deleted:
        body["_deleted"] = True
    return body


def _display_path(doc: dict, doc_id: str) -> str:
    path = doc.get("path")
    return path if isinstance(path, str) and path else doc_id
