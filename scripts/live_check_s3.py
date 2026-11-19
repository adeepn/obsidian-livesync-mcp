#!/usr/bin/env python3
"""Manual acceptance helper; run with venv/bin/python from any directory.

Defaults are throwaway LOCAL SeaweedFS credentials, never production secrets.
No environment/config loading. Only write-phase publishes remote changes.
Use an empty, separate state directory for the initial sweep. Keep Obsidian
idle during server writes; do not repeat write-phase after a partial failure
without inspecting its PASS lines and remote state.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import S3Config
from journal_codec import inflate_journal, parse_journal
from object_storage import Boto3ObjectStorage
from storage_journal import JournalBackend, is_journal_key, journal_sort_key

BLOCK = "\n\n<!-- live-s3-acceptance -->\nДописано сервером (S3)\n"
CREATED = "Серверная S3.md"
MOVED = "Папка S3/Переехавшая.md"
DELETED = "На удаление s3.md"
EDITED = "Тестовая заметка.md"
BODY = "# Серверная S3\n\nСоздано сервером (S3).\n"


def check(ok: bool, message: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {message}", flush=True)
    return ok


def note_bytes(note: dict) -> bytes:
    return note['text'].encode('utf-8') if note['is_text'] else note['data']


async def verify(backend: JournalBackend, vault: Path, paths: list[str]) -> bool:
    if not paths:
        return check(False, "no vault files selected")
    good = True
    for path in paths:
        local = vault / path
        note = await backend.read_note(path)
        matches = local.is_file() and note is not None and local.read_bytes() == note_bytes(note)
        good = check(matches, f"byte match {path}") and good
    return good


async def write_phase(backend: JournalBackend, storage: Boto3ObjectStorage) -> bool:
    # Preflight before publishing anything, so reruns do not silently overwrite.
    original = await backend.read_note(EDITED)
    if original is None or not original['is_text']:
        return check(False, f"missing text note {EDITED}")
    for path in (CREATED, MOVED, DELETED):
        if await backend.read_note(path) is not None:
            return check(False, f"preflight: already exists {path}")
    if BLOCK in original['text']:
        return check(False, "preflight: marked block already present")
    for path, body in ((CREATED, BODY), (DELETED, "Удалить сервером (S3).\n")):
        meta = await backend.write_note(path, body, create=True)
        check(True, f"create {path} rev={meta['rev']}")
    expected = original['text'] + BLOCK
    meta = await backend.write_note(EDITED, expected, expected_rev=original['rev'])
    check(True, f"append {EDITED} rev={meta['rev']}")
    meta = await backend.rename_note(CREATED, MOVED)
    check(True, f"move {CREATED} -> {MOVED} rev={meta['rev']}")
    before = await backend.read_note(DELETED)
    previous_keys = set(storage.list())
    if not await backend.delete_note(DELETED, expected_rev=before['rev']):
        return check(False, f"delete {DELETED}")
    tombstones = []
    for key in set(storage.list()) - previous_keys:
        if is_journal_key(key):
            journal = parse_journal(inflate_journal(storage.get(key)))
            tombstones.extend(doc for doc in journal.docs
                              if doc.get('path') == DELETED and doc.get('deleted'))
    if len(tombstones) != 1:
        return check(False, "cannot identify published deletion revision")
    check(True, f"soft-delete {DELETED} rev={tombstones[0]['_rev']}")
    await backend.refresh(force=True)
    good = True
    for path, body in ((MOVED, BODY), (EDITED, expected), (CREATED, None), (DELETED, None)):
        note = await backend.read_note(path)
        matches = note is None if body is None else note is not None and note['text'] == body
        rev = note['rev'] if note else 'absent'
        good = check(matches, f"read-back {path} rev={rev}") and good
    return good


async def run(args: argparse.Namespace) -> bool:
    storage = Boto3ObjectStorage(S3Config(
        endpoint=args.endpoint, access_key='testkey', secret_key='testsecret',
        bucket=args.bucket, region='us-east-1', prefix=args.prefix,
        uri='', state_dir=str(args.state_dir),
    ))
    backend = JournalBackend(storage, args.state_dir)
    try:
        await backend.startup_check()
        await backend.refresh(force=True)
        check(True, "startup + forced refresh")
        if args.command == 'sweep-notes':
            notes = await backend.list_notes()
            for note in notes:
                check(True, f"note {note['path']} size={note['size']}")
            keys = sorted((key for key in storage.list() if is_journal_key(key)), key=journal_sort_key)
            if not keys:
                return check(False, "no client journals found")
            raw = storage.get(keys[0])
            if raw is None:
                return check(False, "first journal disappeared")
            parsed = parse_journal(inflate_journal(raw))
            fixture = ROOT / 'tests/fixtures/live_client_journal_sample.bin'
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_bytes(raw)
            check(True, f"captured {keys[0]} -> {fixture} ({len(raw)} bytes)")
            if not parsed.docs:
                return check(False, "captured first journal has no docs; golden test will fail")
            return check(bool(notes), f"sweep {len(notes)} notes")
        if args.command == 'write-phase':
            return await write_phase(backend, storage)
        if args.command == 'verify-visible':
            paths = args.path
        else:
            paths = sorted(p.relative_to(args.vault).as_posix() for p in args.vault.rglob('*')
                           if p.is_file() and p.suffix.lower() in {'.md', '.png'}
                           and not any(part.startswith('.') for part in p.relative_to(args.vault).parts))
        return await verify(backend, args.vault, paths)
    finally:
        await backend.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('sweep-notes', 'verify-vault', 'write-phase', 'verify-visible'):
        child = sub.add_parser(command)
        child.add_argument('--state-dir', type=Path, required=True)
        child.add_argument('--prefix', default='livetest/')
        child.add_argument('--endpoint', default='http://127.0.0.1:19000')
        child.add_argument('--bucket', default='plan-b-it')
        child.add_argument('--vault', type=Path, default=Path('/Users/adeep/Documents/TESTVault'))
        if command == 'verify-visible':
            child.add_argument('--path', action='append', required=True, help='vault-relative path; repeatable')
    args = parser.parse_args()
    if args.command == 'verify-visible' and any(Path(p).is_absolute() or '..' in Path(p).parts for p in args.path):
        parser.error('--path must be relative and cannot contain ..')
    try:
        good = asyncio.run(run(args))
    except Exception as exc:
        # Exception messages from SDKs can include connection details. Keep
        # diagnostics safe; controller can inspect backend logs separately.
        check(False, f"{args.command}: {type(exc).__name__}")
        good = False
    check(good, f"overall {args.command}")
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
