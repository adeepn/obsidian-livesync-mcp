# Regenerating upstream oracle fixtures

Run from the repository root with the Python development environment and **Node
26.13.1**. Committed expectations come from executing the pinned upstream with
published dependencies, never from the Python peer. Ordinary oracle tests read
fixtures offline; active-selection integration tests separately invoke Node 22+.
Regeneration does not contact vault storage. Keep outputs in ignored scratch space
until reviewing a deliberate fixture update. Do not edit either upstream checkout.

## Inputs and verified acquisition

The source repositories are
[commonlib](https://github.com/vrtmrz/livesync-commonlib) at
`484c164450d61deb2e301582b60cb9b5f9fa977f` and
[the plugin](https://github.com/vrtmrz/obsidian-livesync) at
`5070b67beae3b60e3a79132386a08da3f1ef213a`. The plugin is required by the tweak
extractor; the other extractors consume commonlib. Generators compare consumed
source bytes with `git show PIN:path`; fixture provenance records source SHA256
under `source_sha256`, `sources`, `hashes`, or nested `provenance` fields. Preserve
those fields. Check both commits before starting:

```bash
PARITY_ORACLE=.superpowers/sdd/2026-11-23-settings-parity/d/oracle-reproduction
mkdir -p "$PARITY_ORACLE"
git clone --no-checkout https://github.com/vrtmrz/livesync-commonlib.git "$PARITY_ORACLE/commonlib"
git -C "$PARITY_ORACLE/commonlib" checkout --detach 484c164450d61deb2e301582b60cb9b5f9fa977f
git clone --no-checkout https://github.com/vrtmrz/obsidian-livesync.git "$PARITY_ORACLE/plugin"
git -C "$PARITY_ORACLE/plugin" checkout --detach 5070b67beae3b60e3a79132386a08da3f1ef213a
PARITY_COMMON="$PARITY_ORACLE/commonlib"
PARITY_PLUGIN="$PARITY_ORACLE/plugin"
git -C "$PARITY_COMMON" rev-parse HEAD
git -C "$PARITY_PLUGIN" rev-parse HEAD
node --version
```

Existing read-only checkouts can be used instead by setting `PARITY_COMMON` and
`PARITY_PLUGIN` to their paths. Do not run package install scripts. The acquisition
below reads the pinned lockfile's exact URLs and verifies SHA512 **before** writing
or extracting any package. It retains a URL/version/SHA256/integrity manifest.
The `xxhash-wasm-102` alias resolves to **xxhash-wasm 1.1.0**, not version 1.0.2.

| Lockfile package | Published version | Download and verification |
|---|---|---|
| `xxhash-wasm-102` | xxhash-wasm 1.1.0 | `https://registry.npmjs.org/xxhash-wasm/-/xxhash-wasm-1.1.0.tgz`; lockfile SHA512; extracted `package/esm/xxhash-wasm.js` SHA256 `e3be9143c10d60a015448de1ceef383b8f2ea1fd2773a5a5c69973ed52d8325a` |
| `octagonal-wheels` | 0.1.57 | `https://registry.npmjs.org/octagonal-wheels/-/octagonal-wheels-0.1.57.tgz`; lockfile SHA512 and archive SHA256 `a7b3f0e70ee2092c3b5367e35c31ee99f0b3bcfd4f19098da8b038520bbf2ea7` |
| `fflate` | 0.8.3 | `https://registry.npmjs.org/fflate/-/fflate-0.8.3.tgz`; lockfile SHA512 |
| `pouchdb-adapter-http` | 9.0.0 | `https://registry.npmjs.org/pouchdb-adapter-http/-/pouchdb-adapter-http-9.0.0.tgz`; lockfile SHA512; only its verified serializer source is evaluated |
| `minimatch` | 11.0.3 | Exact lockfile `resolved` URL and SHA512; additionally recorded in `vendor/selection/sources.json` |
| `brace-expansion` | 5.0.12 | Same |
| `balanced-match` | 4.0.4 | Same |
| `@isaacs/nonrecursive-glob-match` | 1.0.3 | Same |

```bash
export PARITY_COMMON PARITY_ORACLE
./venv/bin/python - <<'PY'
import base64, hashlib, io, json, os, subprocess, tarfile, urllib.request
from pathlib import Path, PurePosixPath
root = Path(os.environ['PARITY_COMMON'])
out = Path(os.environ['PARITY_ORACLE'])
pin = '484c164450d61deb2e301582b60cb9b5f9fa977f'
raw = subprocess.check_output(['git', '-C', str(root), 'show', pin + ':package-lock.json'])
assert raw == (root / 'package-lock.json').read_bytes()
lock = json.loads(raw)['packages']
manifest = {}
for name in ('xxhash-wasm-102', 'octagonal-wheels', 'fflate', 'pouchdb-adapter-http',
             'minimatch', 'brace-expansion', 'balanced-match', '@isaacs/nonrecursive-glob-match'):
    entry = lock['node_modules/' + name]
    assert entry['resolved'].startswith('https://registry.npmjs.org/')
    filename = ('task3b-wheels' if name == 'octagonal-wheels' else name.replace('/', '_')) + '.tgz'
    archive = out / filename
    data = archive.read_bytes() if archive.exists() else urllib.request.urlopen(entry['resolved'], timeout=30).read()
    assert 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode() == entry['integrity']
    archive.write_bytes(data)
    manifest[name] = dict(version=entry['version'], url=entry['resolved'],
                          integrity=entry['integrity'], sha256=hashlib.sha256(data).hexdigest())
    if name == 'xxhash-wasm-102':
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            module = tar.extractfile('package/esm/xxhash-wasm.js').read()
        assert hashlib.sha256(module).hexdigest() == 'e3be9143c10d60a015448de1ceef383b8f2ea1fd2773a5a5c69973ed52d8325a'
        (out / 'xxhash.mjs').write_bytes(module)
    if name == 'octagonal-wheels':
        assert manifest[name]['sha256'] == 'a7b3f0e70ee2092c3b5367e35c31ee99f0b3bcfd4f19098da8b038520bbf2ea7'
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            for member in tar.getmembers():
                rel = PurePosixPath(member.name)
                assert not rel.is_absolute() and '..' not in rel.parts
                if member.isfile():
                    target = out / 'wheels' / str(rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())
(out / 'dependencies.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY
PARITY_WASM="$PARITY_ORACLE/xxhash.mjs"
PARITY_WASM_TGZ="$PARITY_ORACLE/xxhash-wasm-102.tgz"
PARITY_WHEELS="$PARITY_ORACLE/wheels"
PARITY_WHEELS_TGZ="$PARITY_ORACLE/task3b-wheels.tgz"
```

`scripts/splitter_oracle_dependencies.mjs:8-52` rechecks the wheels archive,
relative imported modules and WASM archive/module against the lockfile and tar
members. The compression, surrogate and ignore-name generators additionally
verify their own fflate/PouchDB/minimatch archives. Older standalone hash/legacy
fixtures record module hashes rather than a full dependency manifest: retain
`dependencies.json` alongside a regeneration review so archive URL, version and
integrity are not lost. No helper fetches an unpinned newer package.

## Main fixture: exact offline reproduction

Input recipes are `tests/settings_oracle_inputs.py`; expected partitions, IDs,
coercion and helper outputs are produced by executing commonlib, not by those
recipes. The loader `tests/settings_oracle.py` enforces the recipe file digest.
The **last WASM archive argument is required** by the current generator.

```bash
PYTHON=./venv/bin/python node scripts/extract_settings_oracle.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_ORACLE/settings_oracle_upstream.json.gz" "$PARITY_WASM_TGZ"
./venv/bin/python - "$PARITY_ORACLE/settings_oracle_upstream.json.gz" <<'PY'
import hashlib, sys
from pathlib import Path
actual = Path(sys.argv[1]).read_bytes()
committed = Path('tests/fixtures/settings_oracle_upstream.json.gz').read_bytes()
assert actual == committed, 'Review provenance and semantic differences before replacing a fixture'
print('byte-identical sha256=' + hashlib.sha256(actual).hexdigest())
PY
./venv/bin/python -m pytest tests/test_settings_differential.py tests/test_settings_matrix.py tests/test_settings_oracle_guard.py tests/test_splitter_helpers_oracle.py -q
```

A different Node version can alter recorded provenance even if outcomes agree.
Keep exact version and acquisition manifest with review evidence. Never replace
expected values from peer results to make a failing test pass.

## Complete generator command catalogue

All commands below run from repository root after the variables above are set.
Outputs are scratch files; fixture names below are relative to `tests/fixtures/`.
Inputs beyond the verified dependencies are fixed recipes embedded in each
script unless stated otherwise. No command in this table uses real storage.

| Script and exact command | Produced fixture / artifact | Validation |
|---|---|---|
| `node scripts/extract_settings_oracle.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_ORACLE/settings_oracle_upstream.json.gz" "$PARITY_WASM_TGZ"` | `settings_oracle_upstream.json.gz`; Python input recipes | `test_settings_differential.py`, `test_settings_matrix.py`, `test_settings_oracle_guard.py`, `test_splitter_helpers_oracle.py` |
| `node scripts/extract_splitter_vectors.mjs "$PARITY_COMMON" "$PARITY_ORACLE/content_splitter_upstream.json" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ"` | `content_splitter_upstream.json`; pinned upstream unit-spec assertions and deterministic differential inputs | `test_content_splitter.py` |
| `node scripts/extract_legacy_splitter_vectors.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_ORACLE/legacy_splitter_upstream.json"` | `legacy_splitter_upstream.json`; deterministic seed `0x3c202611`, direct and worker paths | `test_legacy_splitter.py` |
| `node scripts/extract_splitter_migration_vectors.mjs "$PARITY_COMMON" "$PARITY_ORACLE/splitter_migration_upstream.json"` | `splitter_migration_upstream.json`; native migration branch, no package dependencies | `test_splitter_migration.py` |
| `node scripts/extract_hash_vectors.mjs "$PARITY_COMMON" "$PARITY_WHEELS" "$PARITY_WASM" "$PARITY_ORACLE/hash_algorithms_upstream.json"` | `hash_algorithms_upstream.json`; requires wheels tarball named `task3b-wheels.tgz` beside the wheels directory | `test_hash_algorithms.py`, `test_settings_matrix.py` |
| `node scripts/extract_decoder_vectors.mjs "$PARITY_COMMON" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_ORACLE/splitter_decoding_upstream.json" "$PARITY_WASM" "$PARITY_WASM_TGZ"` | `splitter_decoding_upstream.json` | `test_splitter_decoding.py` |
| `node --experimental-vm-modules scripts/extract_tweak_vectors.mjs "$PARITY_COMMON" "$PARITY_PLUGIN" "$PARITY_ORACLE/tweak_compatibility_upstream.json.gz"` | `tweak_compatibility_upstream.json.gz`; real source imports, both source pins | `test_tweak_differential.py`, `test_tweak_compatibility.py`, `test_selection_settings.py` |
| `node scripts/extract_chunk_parameter_vectors.mjs "$PARITY_COMMON" "$PARITY_ORACLE/chunk_parameters_upstream.json"` | `chunk_parameters_upstream.json`; native cap/coercion expressions, no packages | `test_coercion_cells.py` |
| `INCLUDE_MINIMUM=1 node scripts/extract_coercion_cells.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/coercion_upstream.json"` | `coercion_upstream.json`; mode commands below | `test_coercion_cells.py`, `test_writer_support.py` |
| `node scripts/extract_minimum_concatenation_vectors.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/minimum_concatenation_upstream.json.gz"` | `minimum_concatenation_upstream.json.gz`; native repeated array addition | `test_coercion_cells.py` |
| `node scripts/extract_legacy_cells.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/legacy_cells_upstream.json"` | `legacy_cells_upstream.json`; isolated parameter cells with yield/deadline guards | `test_legacy_cells.py` |
| `node scripts/extract_legacy_bom_cells.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/legacy_bom_cells_upstream.json"` | `legacy_bom_cells_upstream.json`; BOM and final-delimiter cells | `test_legacy_cells.py` |
| `node scripts/extract_legacy_wire_cells.mjs "$PARITY_COMMON" "$PARITY_ORACLE/settings_oracle_upstream.json.gz" "$PARITY_ORACLE/legacy_wire_cells_upstream.json"` | `legacy_wire_cells_upstream.json`; consumes regenerated main oracle and native journal transport | `test_legacy_cells.py` |
| `node scripts/extract_compression_vectors.mjs "$PARITY_COMMON" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_ORACLE/fflate.tgz" "$PARITY_ORACLE/compression_upstream.json"` | `compression_upstream.json`; real fflate and wheels, native zlib interoperability | `test_compression.py`, `test_reserved_compression_prefix.py` |
| `node scripts/extract_header_vectors.mjs "$PARITY_COMMON" "$PARITY_ORACLE/headers_upstream.json"` | `headers_upstream.json`; native parser/merge, no packages | `test_custom_headers.py` |
| `node scripts/extract_surrogate_vectors.mjs "$PARITY_COMMON" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_ORACLE/surrogate_wire_upstream.json" "$PARITY_ORACLE/pouchdb-adapter-http.tgz"` | `surrogate_wire_upstream.json`; native journal and verified PouchDB serializer source | `test_surrogate_wire.py`, `test_couchdb_surrogate_refusal.py`, `test_settings_matrix.py` |
| `node scripts/extract_jwt_vectors.mjs "$PARITY_COMMON" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_ORACLE/jwt_upstream.json"` | `jwt_upstream.json`; fresh **synthetic** keys from WebCrypto, so bytes intentionally differ | `test_couchdb_jwt.py` |
| `node scripts/extract_jwt_vectors.mjs "$PARITY_COMMON" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_ORACLE/jwt_refresh_upstream.json" tests/fixtures/jwt_upstream.json` | `jwt_refresh_upstream.json`; reuse committed synthetic keys for refresh boundaries | `test_couchdb_jwt.py` |
| `node scripts/extract_text_budget_vectors.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/text_budget_upstream.json"` | `text_budget_upstream.json`; native size predicate + 12 MiB splitter input | `test_text_access_budget.py` |
| `node scripts/extract_collision_vector.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/collision_upstream.json"` | `collision_upstream.json`; native splitting/hash/journal projection | `test_publication_collision.py` |
| `node scripts/extract_scalar_policy_vectors.mjs "$PARITY_COMMON" "$PARITY_ORACLE/scalar_policy_upstream.json.gz"` | `scalar_policy_upstream.json.gz`; native size and truthiness, no packages | `test_tweak_differential.py`, `test_selection_settings.py` |
| `node --stack-size=16384 scripts/extract_deep_scalar_vectors.mjs "$PARITY_COMMON" "$PARITY_ORACLE/deep_scalar_upstream.json"` | `deep_scalar_upstream.json`; native deep-array coercion, no packages | `test_deep_scalar.py` |
| `node scripts/extract_acceptance_vectors.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/acceptance_upstream.json"` | `acceptance_upstream.json`; 40/100 MiB native binary inputs | `test_settings_oracle_guard.py`, env-gated `test_integration_parity_s3.py` and `test_integration_parity_couchdb.py` |
| `node scripts/extract_selection_vectors.mjs "$PARITY_COMMON" "$PARITY_ORACLE/selection.json"` | `selection.json`; verified vendored minimatch tree required | `test_target_filter.py`, `test_selection_worker.py` |
| `node scripts/extract_ignore_name_vectors.mjs "$PARITY_COMMON" "$PARITY_ORACLE" "$PARITY_ORACLE/ignore_names_upstream.json"` | `ignore_names_upstream.json`; verified selection tarballs named `NAME.tgz` with `/` replaced by `_` | `test_selection_settings.py` |
| `node scripts/probe_ingested_blob_size.mjs "$PARITY_COMMON" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_ORACLE/ingested_blob_size_upstream.json"` | `ingested_blob_size_upstream.json`; native ingestion/reflection with in-memory host seams | `test_ingested_blob_size.py`, `test_settings_matrix.py` |
| `node scripts/probe_journal_checkpoints.mjs "$PARITY_COMMON" > "$PARITY_ORACLE/journal-checkpoints.json"` | Scratch-only probe, no committed fixture; source hashes, native checkpoint assertions | Built-in assertions; `test_storage_journal.py` covers peer lifecycle |
| `node scripts/probe_size_admission.mjs "$PARITY_COMMON" > "$PARITY_ORACLE/size-admission.json"` | Scratch-only probe, no committed fixture; native rename/Blob-size assertions | Built-in assertions; `test_size_policy.py` covers peer admission |

Coercion fixtures have distinct embedded recipe modes. Set only the named mode(s)
for each invocation; do not inherit other `*_EDGES` variables:

```bash
INCLUDE_MINIMUM=1 node scripts/extract_coercion_cells.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/coercion_upstream.json"
COERCION_EDGES=1 node scripts/extract_coercion_cells.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/coercion_edges_upstream.json"
CROSS_EDGES=1 node scripts/extract_coercion_cells.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/coercion_cross_upstream.json"
INGRESS_EDGES=1 node scripts/extract_coercion_cells.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/coercion_ingress_upstream.json"
OVERFLOW_EDGES=1 node scripts/extract_coercion_cells.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/coercion_overflow_upstream.json"
ZERO_EDGES=1 node scripts/extract_coercion_cells.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" "$PARITY_ORACLE/coercion_zero_upstream.json"
```

Time/yield guards record experimental nontermination evidence, not a device
setting limit. Check the fixture's cited source proof before treating a deadline
as a proven loop. The real input is retained for every cell.

## Selection vendoring and generated runtime

These two helpers write tracked vendor files, so use them only for an intentional
vendor update. They do not touch vault storage. Verify the dependency archives
with the acquisition block first. `vendor_selection.py` downloads the same exact
lockfile versions, verifies SHA512, retains selected ES modules/licenses only,
and writes URL, version, SHA256 and integrity to `vendor/selection/sources.json`.
`extract_selection_runtime.mjs` writes `vendor/selection/upstream.mjs` and
`vendor/selection/upstream-sources.json` from pinned source, with source hashes.

```bash
./venv/bin/python scripts/vendor_selection.py "$PARITY_COMMON"
node scripts/extract_selection_runtime.mjs "$PARITY_COMMON"
./venv/bin/python -m pytest tests/test_target_filter.py tests/test_selection_worker.py tests/test_selection_settings.py -q
```

The fixture-only `extract_selection_vectors.mjs` imports the vendored tree.
Verify its archive manifest and selected JavaScript modules before generating:

```bash
./venv/bin/python - "$PARITY_ORACLE" <<'PYTHON'
import json, sys, tarfile
from pathlib import Path
archives = Path(sys.argv[1])
manifest = json.loads(Path('vendor/selection/sources.json').read_text())
acquired = json.loads((archives / 'dependencies.json').read_text())
for name, provenance in manifest.items():
    assert acquired[name] == provenance
    with tarfile.open(archives / (name.replace('/', '_') + '.tgz'), 'r:gz') as tar:
        for member in tar.getmembers():
            if member.isfile() and '/dist/esm/' in member.name and member.name.endswith('.js'):
                local = Path('vendor/selection/node_modules') / name / member.name.removeprefix('package/')
                assert local.read_bytes() == tar.extractfile(member).read()
print('Verified selection dependency manifest and JavaScript members')
PYTHON
```

`extract_ignore_name_vectors.mjs` performs this archive comparison itself.
Do not confuse runtime generation with fixture generation.

## Reviewing a fixture update

Run the validation files listed above as `./venv/bin/python -m pytest tests/FILE -q`,
then the complete suite. Inspect source/dependency/input digest changes separately
from result changes; never hand-edit arrays or expected IDs. Record the new
fixture SHA256 and preserve acquisition provenance in review evidence. JWT's
random synthetic key fixture is not expected to regenerate byte-identically;
regenerate its dependent refresh fixture against whichever baseline is selected.


### Frontmatter admission

`node scripts/extract_frontmatter_admission_vectors.mjs /tmp/settings-parity-commonlib /tmp/frontmatter_admission_upstream.json`
executes `VaultService.ts:73-81` at the pin using Node 26.13.1 builtins. No
published packages are required. The committed
`tests/fixtures/frontmatter_admission_upstream.json` records source/generator
hashes and note digests; `tests/test_frontmatter_limits.py` consumes it.
LiveSync stores Blob bytes without YAML parsing (`EntryManagerImpls.ts:253-280`);
this oracle establishes admission only, while property projection tests define
the additional peer JSON tool behaviour.

## Installed selection provenance

`scripts/vendor_selection.py` writes `vendor/selection/installed-sha256.json`
for every installed vendor file, the worker and `scripts/selection_provenance.mjs`.
Runtime extraction refreshes the manifest after replacing the pinned runtime.
Production verifies the full manifest before launching Node; workers and oracle
generators verify again before importing vendor modules. Missing, modified or
unlisted files refuse with provenance-mismatch. Restore the checkout or regenerate
from the pinned source and integrity-verified published archives, then restart.
The manifest is repository integrity evidence, not a signature against a checkout
attacker who can replace both the verifier and manifest.

### F13 lossless text boundary oracle

Run `node scripts/extract_lossless_vectors.mjs "$PARITY_COMMON" "$PARITY_WASM" "$PARITY_WHEELS" "$PARITY_WHEELS_TGZ" "$PARITY_WASM_TGZ" tests/fixtures/lossless_upstream.json.gz`.
The generator uses the same verified native harness and dependencies as the
settings oracle. Raw upstream partitions stay separate from the independent JS
lossless projection; pinned native managers hash every projected piece.
Validation: `./venv/bin/python -m pytest tests/test_lossless_legacy.py -q`.
