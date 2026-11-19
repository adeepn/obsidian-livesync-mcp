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
  journal sync parameters. When it changes, the remote history is no longer
  compatible with local state, so setting a different epoch wipes every
  table (except the path-identity configuration) and every chunk file, and
  the reset is logged.
- *Path identity*: the mode resolved at startup
  (:func:`livesync_format.resolve_path_identity`) is stored on first open;
  reopening the same state directory with a different mode is a state-dir
  configuration error. The store never derives or rewrites ids — NFC
  normalization already happened when the journal's doc ids were produced.

The store performs no I/O outside ``state_dir`` and only uses the standard
library.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from livesync_format import (
    PATH_IDENTITY_LEGACY,
    PATH_IDENTITY_NFC,
    EncryptedInlineChunk,
    assemble_binary,
    assemble_text,
    extract_inline_chunks,
    is_note_document,
    is_text_doc,
    legacy_body,
)

logger = logging.getLogger(__name__)

_DB_FILENAME = "journal.sqlite3"
_CHUNKS_DIRNAME = "chunks"
_SHORT_SHARD = "_short"

_META_PATH_IDENTITY = "path_identity"
_META_EPOCH = "epoch"
_META_LAST_SENT_KEY = "last_sent_key"

# CouchDB/LiveSync revision strings: "<generation>-<hash>".
_REV_RE = re.compile(r"^(\d+)-(.+)$")

# Accept only bounded chunk identifiers without path separators or dots.
_CHUNK_ID_RE = re.compile(r"^h:[A-Za-z0-9+_-]{1,64}$")

_ENCRYPTED_MESSAGE = (
    "note {path!r} uses end-to-end encryption, "
    "which this server does not support"
)

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
    return int(match.group(1)), match.group(2)


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


class JournalStore:
    """Local journal-peer state rooted at ``state_dir``.

    All persistence lives under ``state_dir``: the SQLite database and the
    ``chunks/`` tree. See the module docstring for the format and the
    revision-tree semantics.
    """

    def __init__(self, state_dir: Path, path_identity: str):
        if path_identity not in (PATH_IDENTITY_LEGACY, PATH_IDENTITY_NFC):
            raise ValueError(
                f"unknown path identity {path_identity!r}; expected "
                f"{PATH_IDENTITY_LEGACY!r} or {PATH_IDENTITY_NFC!r}"
            )
        self.state_dir = Path(state_dir)
        self.path_identity = path_identity
        self._chunks_dir = self.state_dir / _CHUNKS_DIRNAME
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._chunks_dir.mkdir(parents=True, exist_ok=True)
        # Pin the chunk root for resolved-path containment checks.
        self._chunks_root = self._chunks_dir.resolve()

        self._conn = sqlite3.connect(self.state_dir / _DB_FILENAME)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA_V1)

        stored_identity = self._meta_get(_META_PATH_IDENTITY)
        if stored_identity is None:
            self._meta_set(_META_PATH_IDENTITY, path_identity)
        elif stored_identity != path_identity:
            self._conn.close()
            raise ValueError(
                f"journal state at {str(self.state_dir)!r} uses path "
                f"identity {stored_identity!r} but {path_identity!r} was "
                "configured; the state directory belongs to a different "
                "vault configuration"
            )

    # -- Journal replay ------------------------------------------------------

    def apply_journal(
        self, name: str, chunks: dict[str, str], docs: list[dict]
    ) -> None:
        """Replay one parsed journal file; idempotent per ``name``.

        Chunks are staged in unique temporary files. Docs commit before chunks
        are atomically published; only then is the journal marked processed in
        a separate transaction. Failed transactions remove their staging
        without publishing any new chunks. Publication failures leave the
        journal unprocessed so replay can complete it idempotently.
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
                chunk_file.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=chunk_file.parent, prefix=".staged-", delete=False
                ) as stream:
                    tmp_file = Path(stream.name)
                    staged.append((tmp_file, chunk_file))
                    stream.write(data.encode("utf-8"))
            with self._conn:
                for doc in docs or []:
                    self._insert_doc(name, doc)
            for tmp_file, chunk_file in staged:
                os.replace(tmp_file, chunk_file)
            self.mark_processed(name)
        finally:
            for tmp_file, _ in staged:
                tmp_file.unlink(missing_ok=True)

    def _insert_doc(self, name: str, doc: object) -> None:
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
        self._conn.execute(
            "INSERT OR IGNORE INTO docs(doc_id, gen, rev_hash, doc_json) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, gen, rev_hash, _dump_doc(doc)),
        )

    def _chunk_path(self, chunk_id: str) -> Path:
        if not isinstance(chunk_id, str) or _CHUNK_ID_RE.fullmatch(chunk_id) is None:
            raise ValueError("invalid journal chunk identifier")
        digest = chunk_id[len("h:") :]
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
        current = self.epoch
        if value != current:
            logger.info(
                "journal store: epoch changed %r -> %r; resetting local state",
                current,
                value,
            )
            self.reset()
        self._meta_set(_META_EPOCH, value)

    def reset(self) -> None:
        """Wipe replicated state: docs/processed/meta rows and chunk files.

        The path-identity configuration row is preserved (it describes the
        state directory, not the remote history). The reset is logged.
        """
        with self._conn:
            self._conn.execute("DELETE FROM docs")
            self._conn.execute("DELETE FROM processed")
            self._conn.execute("DELETE FROM meta")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (_META_PATH_IDENTITY, self.path_identity),
            )
        removed = 0
        if self._chunks_dir.is_dir():
            for shard in self._chunks_dir.iterdir():
                if shard.is_dir():
                    for chunk_file in shard.iterdir():
                        if chunk_file.is_file():
                            chunk_file.unlink()
                            removed += 1
                    try:
                        shard.rmdir()
                    except OSError:
                        pass
                elif shard.is_file():
                    shard.unlink()
                    removed += 1
        logger.warning(
            "journal store state reset: cleared docs/processed/meta tables "
            "and removed %d chunk file(s)",
            removed,
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
        Raises ``ValueError`` for E2EE refusals and for corrupt bodies
        (missing chunk, invalid fields); use :meth:`iterate_notes` for
        per-note isolation.
        """
        winner = self.winner(doc_id)
        if winner is None or not is_note_document(winner):
            return None
        path = _display_path(winner, doc_id)

        if winner.get("e_"):
            raise ValueError(_ENCRYPTED_MESSAGE.format(path=path))

        doc_type = winner.get("type")
        if doc_type == "notes":
            raw = winner.get("data")
            if isinstance(raw, str) and raw.startswith("%"):
                raise ValueError(_ENCRYPTED_MESSAGE.format(path=path))
            try:
                text = legacy_body(winner)
            except ValueError:
                raise ValueError(
                    f"note {path!r} is corrupt: missing legacy data field"
                ) from None
            return winner, text, None, True

        # is_note_document guarantees doc_type in ("plain", "newnote") here.
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
            raise ValueError(_ENCRYPTED_MESSAGE.format(path=path)) from None
        except ValueError:
            raise ValueError(
                f"note {path!r} is corrupt: invalid eden field"
            ) from None

        if not children:
            if doc_type == "plain" or (
                not winner.get("eden") and is_text_doc(doc_type, path)
            ):
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
        is_text = is_text_doc(doc_type, path)
        text: str | None = None
        if is_text:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                is_text = False
        return winner, text, body, is_text

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
                    raise ValueError(_ENCRYPTED_MESSAGE.format(path=path))
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
