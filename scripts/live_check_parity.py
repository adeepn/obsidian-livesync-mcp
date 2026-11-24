#!/usr/bin/env python3
"""Owner-assisted parity acceptance. Defaults target disposable localhost only.

No dotenv/environment credential loading. Only write-large, oversize and
write-phase publish; prepare-* create exclusive files in the chosen vault.
Never rerun a partially applied write phase without inspecting its results.
Raw milestone/connection values and exception text are never printed/saved.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import S3Config
from diagnostic_hygiene import escape_path
from livesync_format import (TWEAK_COMPATIBILITY_RULES, _TWEAK_TEMPLATE_KEYS,
                             _RECOGNIZED_TWEAK_KEYS, ForbiddenPathError, extract_inline_chunks, prepare_file_content,
                             validate_vault_path)
from object_storage import Boto3ObjectStorage
from storage_base import RenameRefused, WriteSkipped
from storage_couchdb import CouchDBBackend
from storage_journal import JournalBackend, MILESTONE_KEY

MIB = 1024**2
PREFIX = 'parity-live'
DECODING = {'invalid.md': b'prefix\xff\xc3(\x80tail\n',
            'text.png': 'text bytes Привет🙂\n'.encode(),
            'upper.MD': 'uppercase text Привет🙂\n'.encode()}
BODY = '# Parity acceptance\n\nСоздано MCP-пиром.\n'
SAFE_ENUMS = {
    'hashAlg': {'xxhash64', 'xxhash32', 'mixed-purejs', 'sha1', ''},
    'chunkSplitterVersion': {'v1', 'v2', 'v2-segmenter', 'v3-rabin-karp', ''},
    'E2EEAlgorithm': {'v1', 'v2'},
}
# Non-secret scalar knobs only; known but non-scalar/pattern/key values withheld.
SAFE_SCALARS = {rule.key for rule in TWEAK_COMPATIBILITY_RULES} - {
    'encrypt', 'usePathObfuscation', 'E2EEAlgorithm', 'hashAlg', 'chunkSplitterVersion'}
SAFE_SCALARS |= {'encrypt', 'usePathObfuscation', 'syncMaxSizeInMB', 'tweakModified'}
KNOWN_KEYS = _TWEAK_TEMPLATE_KEYS | _RECOGNIZED_TWEAK_KEYS
SECRET_KEY = re.compile(r'pass|salt|secret|token|credential|idderivationkey|apikey', re.I)


def check(ok, message):
    print(f"{'PASS' if ok else 'FAIL'} {message}", flush=True)
    return bool(ok)


def note_bytes(note):
    # read_note keeps newnote bytes even when read_text_note could project text.
    return note['text'].encode('utf-8') if note['is_text'] else note['data']


def sanitized_milestone(milestone):
    """Do not copy unknown values, device IDs, keys, salts or passphrases."""
    maps = milestone.get('tweak_values', {})
    if not isinstance(maps, dict) or not maps:
        raise ValueError('no advertised tweak snapshots')
    result, unknown = {}, set()
    for index, (device, settings) in enumerate(maps.items()):
        if not isinstance(settings, dict):
            continue
        label = 'PREFERRED' if device == 'PREFERRED' else f'device-{index + 1}'
        public = {}
        for key, value in settings.items():
            safe_key = key if isinstance(key, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', key) else '<unexpected key>'
            if key not in KNOWN_KEYS:
                unknown.add(safe_key)
            visible = '<withheld>'
            if isinstance(key, str) and not SECRET_KEY.search(key):
                if key in SAFE_ENUMS and type(value) is str and value in SAFE_ENUMS[key]:
                    visible = value
                elif key in SAFE_SCALARS and (type(value) is bool or
                        type(value) in (int, float) and abs(value) < 2**63 and math.isfinite(value)):
                    visible = value
            public[safe_key] = visible
        result[label] = {'keys': sorted(public), 'values': public}
    if not result:
        raise ValueError('no readable tweak snapshots')
    return {'tweak_values': result, 'unknown_keys': sorted(unknown)}


async def raw_doc(backend, path):
    if isinstance(backend, JournalBackend):
        store = backend._require_store('acceptance')
        return store.winner(backend._path2id(store, path))
    return await backend._get_doc(backend._path2id(path))


async def refresh(backend):
    await backend.startup_check()
    await backend.refresh(force=True)


async def verify(backend, vault, paths, *, partitions=True):
    good, records = True, []
    if not paths:
        return check(False, 'no ordinary vault files'), records
    for path in paths:
        validate_vault_path(path)
        local = vault / path
        note = await backend.read_note(path)
        raw = local.read_bytes() if local.is_file() else None
        matches = raw is not None and note is not None and raw == note_bytes(note)
        doc = await raw_doc(backend, path)
        children = doc.get('children', []) if doc else []
        piece_match = None
        if partitions and matches:
            body, _, _ = prepare_file_content(path, raw)
            pieces = backend._splitter.split(body, path=path)
            expected = [backend._splitter.chunk_id_for_piece(piece) for piece in pieces if piece != '']
            piece_match = expected == children
            matches = matches and piece_match
        records.append({'path': path, 'byte_exact': bool(matches),
                        'size': len(raw) if raw is not None else None,
                        'metadata_size': doc.get('size') if doc else None,
                        'type': doc.get('type') if doc else None,
                        'piece_count': len(children), 'piece_ids_match': piece_match,
                        'sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None})
        good = check(matches, f'byte comparison + piece IDs {escape_path(path)} pieces={len(children)}') and good
    return good, records


def local_prepare(vault, payloads):
    paths = [vault / PREFIX / name for name in payloads]
    if any(p.exists() for p in paths):
        return check(False, 'preflight: acceptance files already exist')
    if not vault.is_dir():
        return check(False, 'vault directory missing')
    (vault / PREFIX).mkdir(exist_ok=True)
    for name, data in payloads.items():
        # Exclusive mode prevents overwriting even if Obsidian creates a file
        # after preflight. Integer payloads are streamed, bounded memory.
        with (vault / PREFIX / name).open('xb') as stream:
            if isinstance(data, int):
                block = b'parity-live\x00\xff' * 8192
                remaining = data
                while remaining:
                    piece = block[:min(remaining, len(block))]
                    stream.write(piece)
                    remaining -= len(piece)
            else:
                stream.write(data)
        check(True, f'created local {PREFIX}/{name}')
    return True


async def write_phase(backend):
    names = [f'{PREFIX}/{n}' for n in ('created.md', 'moved.md', 'deleted.md')]
    if any([await backend.read_note(p) is not None for p in names]):
        return check(False, 'preflight: write-phase paths already exist'), {}
    source, moved, deleted = names
    await backend.write_note(source, BODY, create=True)
    check(True, 'create created.md')
    current = await backend.read_note(source)
    await backend.write_note(source, BODY + 'Обновлено.\n', expected_rev=current['rev'])
    check(True, 'update created.md')
    await backend.rename_note(source, moved)
    check(True, 'move created.md -> moved.md')
    await backend.write_note(deleted, BODY, create=True)
    before = await backend.read_note(deleted)
    removed = await backend.delete_note(deleted, expected_rev=before['rev'])
    tombstone = await raw_doc(backend, deleted)
    good = check(removed and bool(tombstone and tombstone.get('deleted')), 'soft-delete deleted.md')
    good = check(await backend.read_note(source) is None and await backend.read_note(deleted) is None,
                 'source and deleted path absent') and good
    note = await backend.read_note(moved)
    good = check(note is not None and note['text'] == BODY + 'Обновлено.\n', 'updated destination read-back') and good
    return good, {'moved': moved, 'deleted': deleted, 'body': BODY + 'Обновлено.\n'}


async def oversize(backend, source):
    limit = backend.sync_max_size_mb
    if not 0 < limit <= 256:
        return check(False, 'requires finite active limit <=256 MiB'), {}
    destination, skipped = f'{PREFIX}/refused-move.bin', f'{PREFIX}/skipped.bin'
    before = await backend.read_note(source)
    if before is None or len(note_bytes(before)) <= limit * MIB:
        return check(False, 'source must already exist above current lowered limit'), {}
    if await backend.read_note(destination) is not None or await backend.read_note(skipped) is not None:
        return check(False, 'preflight: destinations must be absent'), {}
    skipped_ok = False
    try:
        await backend.write_attachment(skipped, b'x' * (int(limit * MIB) + 1), create=True)
    except WriteSkipped:
        skipped_ok = True
    refused = False
    try:
        await backend.rename_note(source, destination)
    except RenameRefused:
        refused = True
    after = await backend.read_note(source)
    good = check(skipped_ok and await backend.read_note(skipped) is None, 'oversized upload skipped without publication')
    good = check(refused and after is not None and before['rev'] == after['rev'] and
                 note_bytes(before) == note_bytes(after) and await backend.read_note(destination) is None,
                 'oversized rename refused; source revision/bytes intact; destination absent') and good
    return good, {'limit_mib': limit, 'skipped': skipped_ok, 'rename_refused': refused}


async def inspect_decoding(backend):
    records, good = {}, True
    for name in DECODING:
        path = f'{PREFIX}/{name}'
        doc = await raw_doc(backend, path)
        if not doc:
            good = check(False, f'missing device decoding document {path}') and good
            continue
        children = doc.get('children', [])
        if isinstance(backend, JournalBackend):
            data = backend._store._chunk_datas(children, extract_inline_chunks(doc), path)
        else:
            inline = extract_inline_chunks(doc)
            data = []
            for child in children:
                leaf = await backend._get_doc(child) if child not in inline else None
                data.append(inline[child] if child in inline else (leaf or {}).get('data'))
        if any(not isinstance(value, str) or
               len(value.encode('utf-8', errors='surrogatepass')) > 4096 for value in data):
            raise ValueError('unexpected experiment chunk')
        records[path] = {'type': doc.get('type'), 'size': doc.get('size'),
                         'children': children, 'chunk_data': data,
                         'disk_input_b64': base64.b64encode(DECODING[name]).decode()}
        check(True, f'captured native decoding {path} type={doc.get("type")} pieces={len(children)}')
    return good, records


async def run(args):
    if args.command.startswith('prepare-'):
        payloads = DECODING if args.command == 'prepare-decoding' else {
            'device-40m.bin': 40 * MIB, 'exact.bin': args.limit_mib * MIB,
            'above.bin': args.limit_mib * MIB + 1}
        return local_prepare(args.vault, payloads), {'prepared': sorted(payloads)}
    if args.backend == 's3':
        cfg = S3Config(endpoint=args.endpoint, access_key=args.access_key, secret_key=args.secret_key,
                       bucket=args.bucket, region=args.region, prefix=args.prefix, uri='',
                       state_dir=str(args.state_dir))
        backend = JournalBackend(Boto3ObjectStorage(cfg), args.state_dir)
    else:
        backend = CouchDBBackend(url=args.url, username=args.user,
                                 password=args.password, database=args.database)
    # Local-only filters must be explicitly supplied, just like the server.
    backend.selection_only = args.only_regex
    backend.selection_ignore = args.ignore_regex
    backend.selection_use_ignore_files = args.use_ignore_files
    backend.selection_ignore_files = tuple(x for x in args.ignore_files.split(',') if x)
    try:
        await refresh(backend)
        check(True, 'startup + forced refresh')
        if args.command == 'reconnect':
            paths = sorted(p.relative_to(args.vault).as_posix() for p in args.vault.rglob('*')
                           if p.is_file() and not any(part.startswith('.') for part in p.relative_to(args.vault).parts))
            good, records = await verify(backend, args.vault, paths)
            milestone = (backend._load_milestone() if isinstance(backend, JournalBackend)
                         else await backend._get_doc('_local/obsydian_livesync_milestone'))
            settings = sanitized_milestone(milestone or {})
            good = check(not settings['unknown_keys'], 'settings table reconciliation; unknown keys=' + ','.join(settings['unknown_keys'])) and good
            hidden = sum(p.is_file() and any(part.startswith('.') for part in p.relative_to(args.vault).parts)
                         for p in args.vault.rglob('*'))
            check(True, f'scope: {len(paths)} ordinary files; {hidden} hidden files outside peer scope')
            return good, {'files': records, 'milestone': settings, 'hidden_files_outside_scope': hidden}
        if args.command == 'verify-size':
            good, records = await verify(backend, args.vault, [f'{PREFIX}/device-40m.bin', f'{PREFIX}/exact.bin'])
            good = check(backend.sync_max_size_mb == args.limit_mib, 'resolved vault size limit matches owner setting') and good
            above = await backend.read_note(f'{PREFIX}/above.bin')
            good = check(above is None, 'device did not publish above-limit file') and good
            return good, {'files': records, 'limit_mib': backend.sync_max_size_mb}
        if args.command == 'write-large':
            path = f'{PREFIX}/peer-40m.bin'
            if await backend.read_note(path) is not None:
                return check(False, 'preflight: peer attachment exists'), {}
            raw = b'parity-live\x00\xff' * ((40 * MIB + 12) // 13)
            raw = raw[:40 * MIB]
            await backend.write_attachment(path, raw, create=True)
            note = await backend.read_note(path)
            doc = await raw_doc(backend, path)
            good = check(note is not None and note_bytes(note) == raw, 'write_attachment 40 MiB byte-exact read-back')
            return good, {'path': path, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                          'piece_count': len(doc['children'])}
        if args.command == 'oversize':
            return await oversize(backend, args.source)
        if args.command == 'write-phase':
            return await write_phase(backend)
        if args.command == 'inspect-decoding':
            return await inspect_decoding(backend)
        return await verify(backend, args.vault, args.path)
    finally:
        await backend.close()


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest='command', required=True)
    for command in ('reconnect', 'prepare-size', 'verify-size', 'write-large',
                    'oversize', 'write-phase', 'verify-visible', 'prepare-decoding', 'inspect-decoding'):
        child = sub.add_parser(command)
        child.add_argument('--backend', choices=('s3', 'couchdb'), default='s3')
        child.add_argument('--state-dir', type=Path, required=True)
        child.add_argument('--vault', type=Path, required=True)
        child.add_argument('--output', type=Path, required=True, help='sanitized JSON; keep working evidence in .superpowers/')
        child.add_argument('--endpoint', default='http://127.0.0.1:19000')
        child.add_argument('--access-key', default='testkey')
        child.add_argument('--secret-key', default='testsecret')
        child.add_argument('--bucket', default='plan-b-it')
        child.add_argument('--region', default='us-east-1')
        child.add_argument('--prefix', default='livetest/')
        child.add_argument('--url', default='http://127.0.0.1:5984')
        child.add_argument('--user', default='admin')
        child.add_argument('--password', default='testsecret')
        child.add_argument('--database', default='parity-live')
        child.add_argument('--only-regex', default='')
        child.add_argument('--ignore-regex', default='')
        child.add_argument('--use-ignore-files', action='store_true')
        child.add_argument('--ignore-files', default='')
        child.add_argument('--limit-mib', type=int, default=100)
        if command == 'oversize':
            child.add_argument('--source', default=f'{PREFIX}/exact.bin')
        if command == 'verify-visible':
            child.add_argument('--path', action='append', required=True)
    return cli


def main():
    cli = parser()
    args = cli.parse_args()
    if not 40 <= args.limit_mib <= 256:
        cli.error('--limit-mib must be between 40 and 256 for bounded acceptance fixtures')
    for path in getattr(args, 'path', []) + ([args.source] if hasattr(args, 'source') else []):
        try:
            validate_vault_path(path)
        except (ValueError, ForbiddenPathError):
            cli.error('paths must be ordinary vault-relative paths')
        if Path(path).is_absolute() or '..' in Path(path).parts:
            cli.error('paths must be relative without parent traversal')
    try:
        good, result = asyncio.run(run(args))
    except Exception as error:
        good, result = False, {'cause': type(error).__name__}
        check(False, f'{args.command}: {type(error).__name__}')
    artifact = {'date': date.today().isoformat(), 'command': args.command,
                'passed': good, 'result': result}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=True, indent=2) + '\n')
    check(good, f'overall {args.command}')
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
