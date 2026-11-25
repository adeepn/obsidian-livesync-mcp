# Repository Guidelines

## Project Structure & Module Organization

This repository is a small Python MCP server for accessing an Obsidian LiveSync vault through a configurable storage backend (CouchDB or S3 journal). Core files live at the root:

- `server.py`: FastMCP server setup, all MCP tool definitions, strict UTF-8 text representation of newnotes, auth wiring, and the `get_backend(cfg)` factory that builds the storage backend from configuration.
- `config.py`: strict environment validation (`load_config`) — backend selection, backend credentials, client auth; rejects placeholder secrets.
- `storage_base.py`: storage-agnostic `StorageBackend` protocol plus the storage exception hierarchy.
- `couchdb_compression.py`: unconditional CouchDB document decompression, lossless shorter-only raw-deflate writes, and typed corruption errors.
- `couchdb_jwt.py`: pinned plugin JWT key import, compact JWS signing and per-backend token refresh.
- `storage_couchdb.py`: `CouchDBBackend`, the LiveSync-aware CouchDB implementation of the protocol.
- `wire_encoding.py`: shared well-formed JSON serialization and journal TextEncoder surrogate projection.
- `journal_codec.py`: LiveSync journal record parsing and serialization.
- `journal_store.py`: local SQLite revision state and on-disk chunk cache; `state_size_bytes()` measures SQLite/sidecar and chunk bytes for backend/tool observability.
- `object_storage.py`: S3 object storage adapter using boto3.
- `storage_journal.py`: append-only LiveSync S3 journal storage backend.
- `livesync_format.py`: LiveSync document/chunk codecs, device disk-ingestion document typing, path rules, and vault validation.
- `content_splitter.py`: pinned V1/V2/V2-segmenter/V3 and legacy migration, UTF-16/UTF-8 boundaries and all supported unencrypted chunk hashes.
- `device_selection.py`: shared backend admission, ignore-file loading and bounded verdict caching.
- `selection_engine.py`: optional Node capability checks, persistent worker and response validation.
- `selection_worker.mjs`: id-correlated JSON-lines selection worker with bounded lifecycle.
- `vendor/selection/`: pinned upstream selection functions and minimatch dependency tree, source URLs and checksums; inspect provenance before updating.
- `diagnostic_hygiene.py` / `diagnostic_codes.py`: typed public fields, closed reasons, control escaping and opt-in bounded debug sink.
- `replay_limits.py`: configurable operational journal/object/refresh budgets, separate from device file-size admission.
- `regex_runner.py`: lazy persistent isolated JSON-lines regex worker, request deadline/output cap, shutdown and loop-close cleanup.
- `scan_budget.py`: aggregate note/byte/time scan admission, injectable clock, active deadline context and typed truncation metadata.
- `text_eligibility.py`: shared whole-note text budget resolution and incremental UTF-8/control-character projection, shared by both backends.
- `frontmatter.py`: round-trip YAML frontmatter editing (ruamel.yaml).
- `links.py`: Obsidian-compatible link extraction, resolution, and graph building.
- `auth_config.py`: auth-mode selection and the static bearer token verifier.
- `oauth_provider.py`: OAuth 2.1 authorization provider used when OAuth is enabled.
- `run.py`: local entry point; loads `.env` before importing the server.
- `requirements.txt` / `requirements-dev.txt` / `requirements.lock`: runtime, dev, and frozen dependency sets.
- `.env.example`: required configuration template.
- `obsidian-mcp.service`: optional systemd deployment unit.

`tests/test_selection_settings.py` covers independent vault-derived ignore settings,
backend admission, refresh, cache invalidation and typed source diagnostics.
`scripts/extract_tweak_vectors.mjs` records real upstream template keys, ignore
defaults, name splitting and unreadable-file behaviour as well as tweak
compatibility vectors.

`scripts/extract_settings_oracle.mjs` also records direct published encoder and
raw-hash conversion vectors in `tests/fixtures/settings_oracle_upstream.json.gz`.
The shared loader enforces the input digest before exporting any fixture view.
`tests/splitter_comparisons.json` inventories every comparison operator, including
chained endpoints, with a measured oracle witness or cited proof.
`tests/splitter_observability.py` executes isolated in-memory mutations;
`tests/splitter_mutations.json` and `scripts/check_splitter_mutations.py` replay
the known and additional grid/CouchDB/journal witnesses. Nothing rewrites the
production module on disk. Run the fast observability checks with:

```bash
./venv/bin/python -m pytest tests/test_splitter_observability.py tests/test_splitter_helpers_oracle.py tests/test_settings_oracle_guard.py -q
./venv/bin/python scripts/check_splitter_mutations.py .superpowers/sdd/2026-11-23-settings-parity/s1/campaign
```

`scripts/extract_legacy_cells.mjs` runs isolated pinned-upstream parameter cells
with a hard child deadline and yield limit;
`scripts/extract_legacy_bom_cells.mjs` adds BOM-only and delimiter-tail probes;
`scripts/extract_legacy_wire_cells.mjs` executes journal leaf transport for
zero-cap surrogate pieces; `tests/test_legacy_cells.py` checks
partitions, IDs, backend lifecycles and pre-mutation loop refusals.

`scripts/extract_compression_vectors.mjs` executes pinned compression functions with integrity-verified published wheels and fflate; `tests/fixtures/compression_upstream.json` records upstream and zlib interoperability vectors.

`scripts/extract_header_vectors.mjs` executes the pinned native-JavaScript header parser
(no package dependencies); `tests/fixtures/headers_upstream.json` records its source
hash and outputs. `tests/test_custom_headers.py` covers config, wire and secrets.

`scripts/extract_surrogate_vectors.mjs` executes pinned journal records, legacy splitting and verified published dependencies to produce `tests/fixtures/surrogate_wire_upstream.json`.

Tests live in `tests/` as `tests/test_<module>.py` (pytest + pytest-asyncio, auto mode via `pytest.ini`); shared doubles are `tests/fake_backend.py` (in-memory backend) and `tests/couchdb_fake.py` (HTTP-level CouchDB double). `tests/settings_oracle_inputs.py` contains deterministic input recipes only; `tests/settings_oracle.py` loads interned compressed upstream expectations from `tests/fixtures/`. `tests/fake_object_storage.py` supplies an S3 double. Keep new modules focused and colocated unless the project grows enough to justify a package layout.

## Build, Test, and Development Commands

Create a virtual environment before local work:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
```

On Windows PowerShell, activate with `.\venv\Scripts\Activate.ps1`.

Run the server locally:

```bash
python run.py
```

Run the test suite:

```bash
./venv/bin/python -m pytest -q
```

The full suite normally takes about 3–4 minutes (equality sweep: about 4m10s;
allow up to roughly 5 minutes). Run serially to avoid load-dependent timing.

Oracle-backed tests consume committed fixtures offline and never invoke Node.
Regenerating fixtures requires Node 26.13.1+; see the commands at the top of
`scripts/extract_*`. **The full suite also includes active-selection integration
tests that invoke Node 22+** (`test_target_filter.py`, `test_selection_worker.py`, `test_selection_settings.py`,
and the active-ignore lifecycle in `test_settings_matrix.py`).
The claim that Node is needed only for regeneration is false for the current
suite; removing that integration dependency is separate implementation work.
The server itself runs without Node when selection filters are off.

Documentation/configuration contract:

```bash
./venv/bin/python -m pytest tests/test_documentation_config.py -q
git ls-files -i -c --exclude-standard
```

The second command must emit nothing. The test compares README and `.env.example`
variable inventories with config/replay consumers (plus the separate unsafe-debug
consumer), and loads documented connection snippets after required field
substitution. Never add new tracked files under ignored paths.
Working evidence and suite logs belong in gitignored `.superpowers/`.
`docs/test-artifacts/` holds final owner acceptance transcripts/outputs only.

Copy `.env.example` to `.env` and set `LIVESYNC_BACKEND`, the backend credentials, and exactly one auth mode before running against real data.

## Coding Style & Naming Conventions

Use Python 3.11+ and PEP 8 conventions: 4-space indentation, clear names, and snake_case for variables, functions, and helpers. Keep MCP tool handlers descriptive and aligned with existing names such as `create_note`, `manage_tags`, and `vault_stats`. Prefer typed function signatures where practical.

No formatter or linter config is committed. If adding one, document the command here and avoid reformatting unrelated files.

## Testing Guidelines

The automated suite covers configuration, codecs, frontmatter, links, the CouchDB backend (against an HTTP-level fake), all tool groups, and auth. Keep it green; add focused tests in `tests/test_<module>.py` before broad refactors, especially for note parsing, CouchDB document handling, OAuth, or MCP tools.

Tool tests run against `tests/fake_backend.py`; backend tests against `tests/couchdb_fake.py`. Manual verification with a configured CouchDB instance and `python run.py` remains useful for end-to-end checks: exercise the changed MCP tool and check note content, frontmatter, and CouchDB revisions.

## Commit & Pull Request Guidelines

Recent commits use short, imperative subjects with Conventional Commit prefixes, for example `docs: add CHANGELOG` and `fix: prevent silent data loss in search-replace on chunked documents`. Prefer `docs:`, `fix:`, `feat:`, or `chore:`.

Pull requests should include purpose, affected files or tool paths, verification performed, and configuration or migration notes. Link related issues when available.

## Security & Configuration Tips

Never commit `.env`, CouchDB credentials, OAuth client secrets, or public deployment tokens. When changing OAuth behavior, document client registration assumptions and redirect URI implications in `README.md`.

`scripts/extract_jwt_vectors.mjs` executes pinned `JWTTokenGenerator` with
integrity-verified published octagonal-wheels and Node WebCrypto; synthetic
private keys in `tests/fixtures/jwt_upstream.json` are test-only, not secrets.
`tests/test_couchdb_jwt.py` covers exact wire bytes, raw EC verification,
refresh boundaries, exclusive configuration, transport hygiene and typed stats.
Its one live startup smoke is skipped unless `COUCHDB_TEST_JWT_URL` is exported
with JWT credentials and the intended `COUCHDB_DATABASE`.

`scripts/extract_text_budget_vectors.mjs` executes the pinned device size
predicate and the verified splitter harness for a 12 MiB textual newnote;
`tests/fixtures/text_budget_upstream.json` records source hashes and published
dependency integrity. `tests/test_text_access_budget.py` covers both backends,
exact byte boundaries, unlimited overrides, typed refusals and real-size notes.


`tests/test_scan_pagination.py` covers unlimited scan defaults, cursor composition,
page limits, deterministic time truncation, busy-slot refusals and fetch windows
on both storage doubles. `tests/test_scan_time_pages.py` checks page composition
under injected processing and body-read clocks. `tests/test_resumable_index.py` covers update-sequence
keyed CouchDB index progress. Regex worker lifecycle coverage lives in
`tests/test_regex_runner.py`. Scan documentation/default contracts are pinned in
`tests/test_documentation_config.py`; see `docs/scan-policy.md` for response
shapes and the incomplete-index pagination caveat.

`scripts/extract_collision_vector.mjs` executes pinned splitting, hashing and journal transport with integrity-verified dependencies; `tests/test_publication_collision.py` checks projected equality and lossless preflight.

`scripts/extract_coercion_cells.mjs` executes isolated pinned dispatcher cells for JSON-typed writer parameters; `tests/test_coercion_cells.py` crosses exact upstream partitions and IDs with both backend doubles.

`publication_notices.py` owns closed, typed successful-write transformations, context-local collection and external result suffixes. `tests/test_publication_notices.py` exercises both backends and all writing tool families.

`scripts/extract_ignore_name_vectors.mjs` executes pinned targetFilter/path functions with integrity-verified published minimatch dependencies; the committed ignore-name fixture covers inactive, missing, unreadable and readable names.
