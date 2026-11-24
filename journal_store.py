"""Local peer state for journal replication: SQLite revision trees plus a
content-addressed on-disk chunk store (Plan B, Task 3).

This is the journal analogue of an Obsidian client's local database: every
journal file that :mod:`journal_codec` parses is replayed into

- one SQLite database (``journal.sqlite3``) holding every metadata document
  revision ever seen, keyed ``(doc_id, gen, rev_hash)`` so re-applying a file
  is a no-op, plus the ``processed`` journal-name ledger and a ``meta`` table
  (epoch, last-sent key, path identity); and
- ``chunks/<hh>/<rest>`` files under the state directory holding chunk data
  verbatim (UTF-8), deduplicated by existence because chunk ids are
  content-addressed (``h:`` + hash prefix, see
  :func:`livesync_format.chunk_id_for`).

Design notes:

- *Leaves*: journal records carry a CouchDB-style ``_rev`` but no
  ``_revisions`` parentage, so — exactly like upstream's
  ``new_edits=false`` inserts into PouchDB — every stored revision is kept
  as an independent leaf; :func:`pick_winner` (max generation, then
  lexicographically largest hash) decides the winner. Sibling leaves from
  concurrent devices coexist and the loser stays queryable via
  :meth:`JournalStore.leaves`.
- *Epoch resets*: the epoch is ``protocolVersion:pbkdf2salt`` from the
  journal sync parameters. The backend confirms wipes against send history;
  resets clear only processed-journal and last-sent tracking. Local revision
  trees and chunks survive, including notes absent from rebuilt remote history.
- *Path identity*: the mode resolved at startup
  (:func:`livesync_format.resolve_path_identity`) is stored on first open;
  reopening with a different mode quarantines the whole old state directory
  beside it before starting fresh. IDs are never mixed or rewritten.

The store uses only the standard library; identity quarantine is a sibling
of ``state_dir`` so old documents and chunks remain recoverable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

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
from text_eligibility import TextProjection, DEFAULT_TEXT_VERDICT_CACHE_ENTRIES

logger = logging.getLogger(__name__)

_DB_FILENAME = "journal.sqlite3"
_CHUNKS_DIRNAME = "chunks"
_SHORT_SHARD = "_short"

_META_PATH_IDENTITY = "path_identity"
_META_EPOCH = "epoch"
_META_LAST_SENT_KEY = "last_sent_key"

# CouchDB/LiveSync revision strings: "<generation>-<hash>".
_REV_RE = re.compile(r"^(\d+)-(.+)$")

# Accept bounded historical IDs and standard SHA-1 base64 IDs. Upstream:
# commonlib@484c164450d61deb2e301582b60cb9b5f9fa977f
# managers/HashManager/PureJSHashManager.ts:77-87.
_CHUNK_ID_RE = re.compile(r"^h:(?:[A-Za-z0-9+_-]{1,64}|[A-Za-z0-9+/]{27}=)$")

_ENCRYPTED_MESSAGE = (
    "note {path!r} uses end-to-end encryption, "
    "which this server does not support"
)


class EncryptedNoteError(ValueError):
    """The note body requires unsupported end-to-end decryption.

    A distinct class (still a :class:`ValueError` for existing callers) so
    backends can classify E2EE refusals structurally — never by scanning
    the human-readable message, which embeds the user-controlled note path.
    """

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS docs (
    doc_id TEXT NOT NULL,
    gen INTEGER NOT NULL,
    rev_hash TEXT NOT NULL,
    doc_json TEXT NOT NULL,
    PRIMARY KEY (doc_id, gen, rev_hash)
);
CREATE TABLE IF NOT EXISTS processed (
    name TEXT NOT NULL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def parse_rev(rev: object) -> tuple[int, str]:
    """Split a revision ``"<gen>-<hash>"`` into ``(gen, hash)``.

    Raises ``ValueError`` for anything else (the journal codec guarantees a
    non-empty string, but not the CouchDB shape).
    """
    match = _REV_RE.match(rev) if isinstance(rev, str) else None
    if match is None:
        raise ValueError(f"invalid revision {rev!r}; expected '<gen>-<hash>'")
    generation = int(match.group(1))
    if not 0 <= generation <= 2**63 - 1:
        raise ValueError("revision generation exceeds SQLite INTEGER range")
    return generation, match.group(2)


def pick_winner(leaves: list[dict]) -> dict | None:
    """Deterministic winner among leaf revisions.

    Highest generation wins; equal generations break on the
    lexicographically *largest* hash. An empty list returns ``None``; a
    malformed ``_rev`` raises ``ValueError``.
    """
    if not leaves:
        return None

    def rank(doc: dict) -> tuple[int, str]:
        gen, rev_hash = parse_rev(doc.get("_rev"))
        return gen, rev_hash

    return max(leaves, key=rank)


def _private_directory(path: Path) -> None:
    """Create private directories and tighten previously permissive caches."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
        logger.warning("tightened journal state directory permissions: %s", path)


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
            quarantine_root = self.state_dir.with_name(self.state_dir.name + ".quarantine")
            if quarantine_root.is_symlink():
                raise StorageError("quarantine directory is a symlink; refusing to move state")
            _private_directory(quarantine_root)
            # Reserve a unique sibling destination; never overwrite an archive.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-")
            destination = Path(tempfile.mkdtemp(prefix=stamp, dir=quarantine_root))
            # If the move fails, retain both the source and reservation.
            self.state_dir.rename(destination / "state")
            archived = destination / "state"
            logger.warning(
                "journal path identity changed %r -> %r; old state quarantined at %s. "
                "To restore: stop all peers using this state directory, move the "
                "current state aside, move %s back to %s and configure identity %r",
                stored_identity, path_identity, archived, archived,
                self.state_dir, stored_identity,
            )
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

        stored_identity = self._meta_get(_META_PATH_IDENTITY)
        if stored_identity is None:
            self._meta_set(_META_PATH_IDENTITY, path_identity)

    # -- Journal replay ------------------------------------------------------

    def apply_journal(
        self, name: str, chunks: dict[str, str], docs: list[dict]
    ) -> None:
        """Replay one parsed journal file; idempotent per ``name``.

        Stage and install content-addressed chunks before committing metadata.
        A failed install cannot expose a revision whose children are missing.
        Failed transactions may leave harmless orphan chunks, reused on retry.
        Mark processed only after both chunks and metadata are installed.
        """
        if self.processed(name):
            return
        staged: list[tuple[Path, Path]] = []
        try:
            for chunk_id, data in (chunks or {}).items():
                try:
                    chunk_file = self._chunk_path(chunk_id)
                except ValueError:
                    logger.warning("skipping invalid journal chunk identifier %r", chunk_id)
                    continue
                if chunk_file.exists():
                    continue
                _private_directory(chunk_file.parent)
                with tempfile.NamedTemporaryFile(
                    dir=chunk_file.parent, prefix=".staged-", delete=False
                ) as stream:
                    tmp_file = Path(stream.name)
                    staged.append((tmp_file, chunk_file))
                    stream.write(data.encode("utf-8"))
            for tmp_file, chunk_file in staged:
                os.replace(tmp_file, chunk_file)
            with self._conn:
                for doc in docs or []:
                    self._insert_doc(name, doc)
            self.finish_bookkeeping(lambda: self.mark_processed(name))
        finally:
            for tmp_file, _ in staged:
                tmp_file.unlink(missing_ok=True)

    def _insert_doc(self, name: str, doc: object) -> None:
        gen, rev_hash = self._validate_doc(name, doc)
        doc_id = doc["_id"]
        self._conn.execute("DELETE FROM text_verdicts WHERE doc_id=?", (doc_id,))
        self._conn.execute(
            "INSERT OR IGNORE INTO docs(doc_id, gen, rev_hash, doc_json) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, gen, rev_hash, _dump_doc(doc)),
        )

    def validate_journal(self, chunks: dict[str, str], docs: list[dict]) -> None:
        """Check deterministic store constraints without changing local state."""
        for chunk_id, data in chunks.items():
            self._chunk_path(chunk_id)
            data.encode("utf-8")
        for doc in docs:
            self._validate_doc("publication", doc)
            _dump_doc(doc)

    @staticmethod
    def _validate_doc(name: str, doc: object) -> tuple[int, str]:
        if not isinstance(doc, dict):
            raise ValueError(f"{name}: journal document must be a JSON object")
        doc_id = doc.get("_id")
        rev = doc.get("_rev")
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError(
                f"{name}: journal document identifier must be a non-empty string"
            )
        if not isinstance(rev, str) or not rev:
            raise ValueError(
                f"{name}: journal document {doc_id!r} needs a non-empty _rev"
            )
        try:
            gen, rev_hash = parse_rev(rev)
        except ValueError:
            raise ValueError(
                f"{name}: journal document {doc_id!r} has invalid revision {rev!r}"
            ) from None
        return gen, rev_hash

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
                    logger.warning("completed journal bookkeeping deferred to replay")

    def mark_processed(self, name: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed(name) VALUES (?)", (name,)
            )

    def files_processed_count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
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
            self._conn.execute("DELETE FROM meta WHERE key = ?", (_META_LAST_SENT_KEY,))
        logger.warning(
            "journal store dedupe caches reset; local documents and chunks retained"
        )

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
        """The winning revision document for ``doc_id``, or ``None``."""
        docs = self._stored_docs(doc_id)
        if not docs:
            return None
        return pick_winner(docs)

    def leaves(self, doc_id: str) -> list[dict]:
        """All stored leaves for ``doc_id``, ascending ``(gen, hash)``.

        Journal records carry no revision parentage, so every stored
        revision is an independent leaf (see the module docstring); losing
        siblings from concurrent devices remain queryable here.
        """
        return self._stored_docs(doc_id)

    def _stored_docs(self, doc_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT doc_json FROM docs WHERE doc_id = ? ORDER BY gen, rev_hash",
            (doc_id,),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # -- Note reads ----------------------------------------------------------

    def note_ids(self) -> list[str]:
        """Sorted ids of live notes: note-classified winners, no tombstones."""
        result: list[str] = []
        for (doc_id,) in self._conn.execute(
            "SELECT DISTINCT doc_id FROM docs ORDER BY doc_id"
        ):
            doc = self.winner(doc_id)
            if doc is not None and is_note_document(doc):
                result.append(doc_id)
        return result

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
        if (winner is None or winner.get('deleted') or winner.get('_deleted')
                or winner.get('type') not in ('plain', 'newnote', 'notes')):
            return None
        if not doc_id.startswith('i:') and not is_note_document(winner):
            return None
        return self._read_note_content(winner)

    def _read_note_content(self, winner: dict):
        doc_id = winner['_id']
        path = _display_path(winner, doc_id)

        if winner.get("e_"):
            raise EncryptedNoteError(_ENCRYPTED_MESSAGE.format(path=path))

        doc_type = winner.get("type")
        if doc_type == "notes":
            raw = winner.get("data")
            if isinstance(raw, str) and raw.startswith("%"):
                raise EncryptedNoteError(_ENCRYPTED_MESSAGE.format(path=path))
            try:
                text = legacy_body(winner)
            except ValueError:
                raise ValueError(
                    f"note {path!r} is corrupt: missing legacy data field"
                ) from None
            return winner, text, None, True

        # Both entry points restrict non-legacy content to plain/newnote.
        children = winner.get("children")
        if children is None:
            children = []
        if not isinstance(children, list) or not all(
            isinstance(child, str) for child in children
        ):
            raise ValueError(f"note {path!r} is corrupt: invalid children field")
        try:
            inline = extract_inline_chunks(winner)
        except EncryptedInlineChunk:
            raise EncryptedNoteError(
                _ENCRYPTED_MESSAGE.format(path=path)
            ) from None
        except ValueError:
            raise ValueError(
                f"note {path!r} is corrupt: invalid eden field"
            ) from None

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
            raise ValueError(
                f"note {path!r} is corrupt: invalid chunk encoding"
            ) from None
        return winner, None, body, False

    def scan_newnote(self, doc: dict, max_bytes: int) -> str | None:
        key = (doc['_id'], doc['_rev'], max_bytes)
        verdict = self._conn.execute(
            'SELECT rev, budget FROM text_verdicts WHERE doc_id=?',
            (doc['_id'],)).fetchone()
        if verdict == key[1:]:
            self._remember_nontext(key)
            return None
        if verdict is not None:
            with self._conn:
                self._conn.execute('DELETE FROM text_verdicts WHERE doc_id=?', (doc['_id'],))
        if doc.get('e_'):
            raise EncryptedNoteError(_ENCRYPTED_MESSAGE.format(path=doc['path']))
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
                break
        if not eligible:
            self._remember_nontext(key)
        return projection.text() if eligible else None

    def _remember_nontext(self, key: tuple[str, str, int]) -> None:
        # Reinsert on a hit so rowid orders least-to-most recently used.
        with self._conn:
            self._conn.execute('DELETE FROM text_verdicts WHERE doc_id=?', (key[0],))
            self._conn.execute('INSERT INTO text_verdicts VALUES (?, ?, ?, 0)', key)
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
        chunk store, then full-JSON chunk records in the docs table."""
        datas: list[str] = []
        for child in children:
            try:
                chunk_file = self._chunk_path(child)
            except ValueError:
                raise ValueError(
                    f"note {path!r} is corrupt: invalid chunk reference"
                ) from None
            if child in inline:
                datas.append(inline[child])
                continue
            if chunk_file.is_file():
                try:
                    datas.append(chunk_file.read_bytes().decode("utf-8"))
                except (OSError, UnicodeDecodeError):
                    raise ValueError(
                        f"note {path!r} is corrupt: unreadable chunk file"
                    ) from None
                continue
            chunk_doc = self.winner(child)
            if chunk_doc is not None:
                if chunk_doc.get("e_"):
                    raise EncryptedNoteError(
                        _ENCRYPTED_MESSAGE.format(path=path)
                    )
                data = chunk_doc.get("data")
                if not chunk_doc.get("deleted") and isinstance(data, str):
                    datas.append(data)
                    continue
            raise ValueError(
                f"note {path!r} is corrupt: a content chunk is missing"
            )
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
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def _display_path(doc: dict, doc_id: str) -> str:
    path = doc.get("path")
    return path if isinstance(path, str) and path else doc_id
