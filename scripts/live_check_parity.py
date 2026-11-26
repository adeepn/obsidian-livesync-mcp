#!/usr/bin/env python3
"""Owner-assisted parity acceptance. Defaults target disposable localhost only.

No dotenv/environment credential loading. Remote mutations require explicit
write/delete flags and exact target confirmation. Only write-large, oversize and
write-phase publish; prepare-* create exclusive files in the chosen vault.
Never rerun a partially applied write phase without inspecting its results.
Raw milestone/connection values and exception text are never printed/saved.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from content_splitter import _wheels_read_string
from config import S3Config
from diagnostic_hygiene import escape_path, recommendation_fields
from livesync_format import (TWEAK_COMPATIBILITY_RULES, _TWEAK_TEMPLATE_KEYS,
                             _RECOGNIZED_TWEAK_KEYS, ForbiddenPathError, extract_inline_chunks, prepare_file_content,
                             validate_vault_path, split_ignore_names)
from object_storage import Boto3ObjectStorage
from storage_base import RenameRefused, WriteSkipped, WriterUnsupported, check_file_size
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
            if key not in KNOWN_KEYS:
                unknown.add(key)
                continue
            safe_key = key
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
    names = json.dumps(sorted(unknown), ensure_ascii=True, separators=(',', ':')).encode()
    return {'tweak_values': result, 'unknown_keys': {
        'marker': '<unknown keys withheld>', 'count': len(unknown),
        'sha256': hashlib.sha256(names).hexdigest()}}


async def raw_doc(backend, path):
    if isinstance(backend, JournalBackend):
        store = backend._require_store('acceptance')
        return store.winner(backend._path2id(store, path))
    return await backend._get_doc(backend._path2id(path))


async def refresh(backend):
    await backend.startup_check()
    await backend.refresh(force=True)


def safe_metadata(doc):
    """Expose closed type names and bounded integer sizes only."""
    doc = doc if isinstance(doc, dict) else {}
    kind, size = doc.get('type'), doc.get('size')
    kind = kind if type(kind) is str and kind in ('plain', 'newnote') else '<unexpected>'
    size = size if type(size) is int and 0 <= size < 2**63 else '<invalid>'
    return kind, size


async def verify(backend, vault, paths, *, partitions=True):
    good, records = True, []
    if not paths:
        return check(False, 'no ordinary vault files'), records
    for path in paths:
        try:
            validate_vault_path(path)
            local = vault / path
            note = await backend.read_note(path)
            raw = local.read_bytes() if local.is_file() else None
            body, expected_type, blob_size = (prepare_file_content(path, raw, lossy_text=True) if raw is not None
                                             else (None, None, None))
            # C:serviceModules/FileAccessBase.ts:201-206 selects host text reads;
            # C:managers/EntryManager/EntryManagerImpls.ts:253-280 stores Blob size.
            # Host invalid-byte replacement: owner plugin 1.0.50 observation,
            # 2026-11-26 (tests/fixtures/device_decoding_observed.json), not an oracle.
            # C:string_and_binary/chunks.ts:252,357,445 decodes legacy text Blobs;
            # compare the splitter projection (including BOM removal), not disk bytes.
            pieces = (backend._splitter.split(body, path=path)
                      if raw is not None and (partitions or expected_type == 'plain') else [])
            expected_bytes = (''.join(pieces).encode('utf-8') if expected_type == 'plain' else raw)
            transformed = raw is not None and expected_bytes != raw
            invalid = False
            if expected_type == 'plain' and raw is not None:
                try:
                    raw.decode('utf-8')
                except UnicodeDecodeError:
                    invalid = True
            byte_match = raw is not None and note is not None and expected_bytes == note_bytes(note)
            doc = await raw_doc(backend, path)
            kind, metadata_size = safe_metadata(doc)
            metadata_match = type(metadata_size) is int and metadata_size == blob_size
            type_match = kind == expected_type
            children = doc.get('children', []) if doc else []
            piece_match = None
            if partitions and byte_match:
                expected = [backend._splitter.chunk_id_for_piece(piece) for piece in pieces if piece != '']
                piece_match = expected == children
            matches = byte_match and metadata_match and type_match and (piece_match is not False)
            records.append({'path': path, 'byte_exact': bool(byte_match),
                            'ingestion_transformed': transformed,
                            'ingestion_reason': ('invalid_bytes_replaced' if invalid else
                                                 'splitter_projection' if transformed else None),
                            'remote_present': note is not None,
                            'remote_size': len(note_bytes(note)) if note is not None else None,
                            'remote_sha256': hashlib.sha256(note_bytes(note)).hexdigest() if note is not None else None,
                            'size': len(raw) if raw is not None else None,
                            'ingested_blob_size': blob_size, 'metadata_size': metadata_size,
                            'metadata_size_matches': metadata_match, 'type': kind, 'type_matches': type_match,
                            'piece_count': len(children), 'piece_ids_match': piece_match,
                            'sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None})
            good = check(matches, f'bytes + Blob metadata + piece IDs {escape_path(path)} pieces={len(children)}') and good
        except Exception as cause:
            records.append({'path': path, 'cause': type(cause).__name__,
                            'byte_exact': False, 'remote_present': None,
                            'metadata_size_matches': False, 'type_matches': False})
            good = check(False, f'file {escape_path(path)} cause={type(cause).__name__}') and good
    return good, records


async def reconnect_files(backend, vault):
    """Use writer admission, not read visibility, for expected device files.

    C:serviceFeatures/targetFilter.ts:132-146 and
    C:services/base/VaultService.ts:73-81: selection and strict size predicate.
    Production startup resolves vault settings and ignore-file lookup.
    """
    if not vault.is_dir():
        return check(False, 'vault directory missing'), {'cause': 'vault_directory_missing'}
    expected, excluded, failures = [], {}, []
    for local in sorted(vault.rglob('*')):
        path = local.relative_to(vault).as_posix()
        try:
            if not local.is_file() or any(p.startswith('.') for p in Path(path).parts):
                continue
            verdict = await backend.selection_for(path)
            if not verdict.writer_supported:
                raise WriterUnsupported(backend._selection_diagnostic or verdict.reason)
            rule = verdict.rule
            if verdict.allowed:
                try:
                    check_file_size(path, local.stat().st_size, backend.sync_max_size_mb)
                except WriteSkipped:
                    rule = 'syncMaxSizeInMB'
            if rule:
                excluded[rule] = excluded.get(rule, 0) + 1
            else:
                expected.append(path)
        except Exception as cause:
            failures.append({'path': path, 'cause': type(cause).__name__,
                             'byte_exact': False, 'remote_present': None,
                             'metadata_size_matches': False, 'type_matches': False})
            check(False, f'file {escape_path(path)} cause={type(cause).__name__}')
    good, records = await verify(backend, vault, expected) if expected else (True, [])
    records.extend(failures)
    good = good and not failures
    missing, mismatch = 0, 0
    for row in records:
        if row['remote_present'] is False:
            missing += 1
        elif not row['byte_exact'] or not row['metadata_size_matches'] or not row['type_matches']:
            mismatch += 1
    check(True, f'policy: expected={len(expected) + len(failures)} excluded={sum(excluded.values())} '
                f'missing={missing} size_or_hash_mismatch={mismatch}')
    return good, {'files': records, 'expected': len(expected) + len(failures),
                  'excluded_by_policy': excluded, 'missing': missing,
                  'size_or_hash_mismatch': mismatch}


def ignore_names(raw):
    # C:serviceFeatures/targetFilter.ts:95-98; share production JS trim/split.
    return split_ignore_names(raw) if raw else None


def mutation_authorized(args):
    """Validate both gates before constructing clients or opening local state."""
    write = args.command in ('write-large', 'write-phase', 'oversize')
    delete = args.command in ('write-phase', 'oversize')
    target = args.bucket + '/' + args.prefix if args.backend == 's3' else args.database
    if write and not args.allow_remote_write:
        return check(False, 'requires --allow-remote-write on a dedicated test vault')
    if delete and not args.allow_remote_delete:
        return check(False, 'requires --allow-remote-delete for rename and soft delete')
    if write and args.confirm_target != target:
        return check(False, '--confirm-target must equal bucket/prefix or database name')
    return True


async def verify_size(backend, vault, limit_mib):
    if backend.sync_max_size_mb != limit_mib:
        return check(False, 'resolved vault size limit differs from expected owner setting'), {}
    sizes = {'device-40m.bin': 40 * MIB, 'exact.bin': limit_mib * MIB,
             'above.bin': limit_mib * MIB + 1}
    physical, good = {}, True
    for name, expected in sizes.items():
        file = vault / PREFIX / name
        size = file.stat().st_size if file.is_file() else None
        physical[name] = {'size': size, 'expected_size': expected}
        good = check(size == expected, f'physical size {PREFIX}/{name}') and good
    if not good:
        return False, {'physical_files': physical}
    good, records = await verify(backend, vault, [f'{PREFIX}/device-40m.bin', f'{PREFIX}/exact.bin'])
    above = await backend.read_note(f'{PREFIX}/above.bin')
    good = check(above is None, 'device did not publish above-limit file') and good
    return good, {'files': records, 'physical_files': physical, 'limit_mib': limit_mib,
                  'recommendation_policy': recommendation_fields(
                      (await backend.backend_info()).get('recommendation_policy', {}))}


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


async def oversize(backend, source, *, expected_limit=50):
    limit = backend.sync_max_size_mb
    if type(limit) not in (int, float) or not 0 < limit <= 256 or limit != expected_limit:
        return check(False, 'resolved size limit differs from expected lowered owner setting'), {}
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


MAX_DECODING_BYTES = 4096
MAX_DECODING_CHILDREN = 256
LEAF_ID = re.compile(r'h:[A-Za-z0-9+/=_-]{1,128}\Z')


def public_decoder_variants(name):
    """Sharing allowlist, NOT an assertion of native-host decoder semantics.

    Unknown observations stay unknown: share only size/digest and ask for local
    owner inspection instead of leaking stale content or inventing an answer.
    """
    raw = DECODING[name]
    if name != 'invalid.md':
        return {raw}
    return {raw, b'', _wheels_read_string(raw).encode('utf-8', errors='surrogatepass'),
            *(raw.decode(codec, errors=errors).encode('utf-8') for codec, errors in
              [('utf-8', 'replace'), ('utf-8', 'ignore'), ('latin-1', 'strict'), ('cp1252', 'replace')])}


async def inspect_decoding(backend, vault=None):
    if vault is None or any(not (vault / PREFIX / name).is_file() or
                            (vault / PREFIX / name).stat().st_size != len(data) or
                            (vault / PREFIX / name).read_bytes() != data
                            for name, data in DECODING.items()):
        return check(False, 'prepared fixed local decoding inputs are required'), {}
    records, good = {}, True
    for name in DECODING:
        path = f'{PREFIX}/{name}'
        doc = await raw_doc(backend, path)
        kind, size = safe_metadata(doc)
        children = doc.get('children') if isinstance(doc, dict) else None
        valid = (kind in ('plain', 'newnote') and type(size) is int and size <= MAX_DECODING_BYTES
                 and isinstance(children, list) and len(children) <= MAX_DECODING_CHILDREN
                 and all(type(child) is str and LEAF_ID.fullmatch(child) for child in children))
        if not valid:
            records[path] = {'type': kind, 'size': size, 'capture': 'invalid metadata; no chunk data shared'}
            good = check(False, f'invalid bounded decoding metadata {path}') and good
            continue
        if isinstance(backend, JournalBackend):
            data = backend._store._chunk_datas(children, extract_inline_chunks(doc), path)
        else:
            inline = extract_inline_chunks(doc)
            data = []
            for child in children:
                leaf = await backend._get_doc(child) if child not in inline else None
                data.append(inline[child] if child in inline else (leaf or {}).get('data'))
        if any(type(value) is not str or len(value) > MAX_DECODING_BYTES for value in data):
            good = check(False, f'oversized or invalid decoding chunks {path}') and good
            records[path] = {'type': kind, 'size': size, 'capture': 'invalid chunks; no data shared'}
            continue
        encoded = [value.encode('utf-8', errors='surrogatepass') for value in data]
        if sum(map(len, encoded)) > MAX_DECODING_BYTES:
            good = check(False, f'aggregate decoding byte bound {path}') and good
            records[path] = {'type': kind, 'size': size, 'capture': 'aggregate bound; no data shared'}
            continue
        try:
            observed = (b''.join(encoded) if kind == 'plain' else
                        b''.join(base64.b64decode(value, validate=True) for value in data))
        except (ValueError, binascii.Error):
            observed = b''.join(encoded)
            shared = False
        else:
            shared = (observed in public_decoder_variants(name) and
                      [backend._splitter.chunk_id_for_piece(value) for value in data] == children)
        row = {'type': kind, 'size': size, 'piece_count': len(children),
               'observed_size': len(observed), 'observed_sha256': hashlib.sha256(observed).hexdigest(),
               'disk_input_b64': base64.b64encode(DECODING[name]).decode()}
        if shared:
            row.update(children=children, chunk_data=data, capture='recognized public fixture observation')
        else:
            row['capture'] = 'unexpected observation; raw data withheld; inspect locally with owner'
        records[path] = row
        good = check(shared, f'native decoding {path} type={kind} pieces={len(children)}') and good
    return good, records


async def run(args):
    if not mutation_authorized(args):
        return False, {'authorization': 'remote mutation refused'}
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
    # Regex lists are local; absent ignore-file choices derive from the vault.
    backend.selection_only = args.only_regex
    backend.selection_ignore = args.ignore_regex
    backend.selection_use_ignore_files = args.use_ignore_files
    backend.selection_ignore_files = ignore_names(args.ignore_files)
    try:
        await refresh(backend)
        check(True, 'startup + forced refresh')
        if args.command == 'reconnect':
            good, file_report = await reconnect_files(backend, args.vault)
            milestone = (backend._load_milestone() if isinstance(backend, JournalBackend)
                         else await backend._get_doc('_local/obsydian_livesync_milestone'))
            settings = sanitized_milestone(milestone or {})
            good = check(not settings['unknown_keys']['count'],
                         'settings table reconciliation; unknown key count=' +
                         str(settings['unknown_keys']['count'])) and good
            hidden = sum(p.is_file() and any(part.startswith('.') for part in p.relative_to(args.vault).parts)
                         for p in args.vault.rglob('*'))
            check(True, f'scope: {hidden} hidden files outside peer scope')
            return good, {**file_report, 'milestone': settings, 'hidden_files_outside_scope': hidden}
        if args.command == 'verify-size':
            return await verify_size(backend, args.vault, args.limit_mib)
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
            return await oversize(backend, args.source, expected_limit=args.limit_mib)
        if args.command == 'write-phase':
            return await write_phase(backend)
        if args.command == 'inspect-decoding':
            return await inspect_decoding(backend, args.vault)
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
        child.add_argument('--use-ignore-files', action=argparse.BooleanOptionalAction, default=None)
        child.add_argument('--ignore-files', default=None)
        child.add_argument('--allow-remote-write', action='store_true')
        child.add_argument('--allow-remote-delete', action='store_true')
        child.add_argument('--confirm-target')
        child.add_argument('--limit-mib', type=int, default=50 if command == 'oversize' else 100)
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
