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
- `selection_provenance.py` / `scripts/selection_provenance.mjs`: verify the complete installed SHA256 manifest before worker launch or oracle imports.
- `selection_engine.py`: optional Node capability checks, persistent worker and response validation.
- `selection_worker.mjs`: id-correlated JSON-lines selection worker with bounded lifecycle.
- `vendor/selection/installed-sha256.json`: hashes every retained vendor file, `selection_worker.mjs` and the JS verifier; regenerate after changing those files.
- `vendor/selection/`: pinned upstream selection functions and minimatch dependency tree, source URLs and checksums; inspect provenance before updating.
- `hosting_logging.py`: closed SDK/HTTP log records installed at application construction.
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
python -m pip install -r requirements.lock
```

The lock is the exact `./venv/bin/python -m pip freeze` of the branch test and
acceptance environment. Reproduce with `python -m pip install -r requirements.lock`
in a fresh Python 3.13 virtual environment; it includes development dependencies.
Regenerate deliberately after dependency changes and validate the isolated install.

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

The second command must emit nothing. The test compares `docs/configuration.md`
and `.env.example` variable inventories with config/replay consumers (plus the separate unsafe-debug
consumer), and loads documented connection snippets after required field
substitution. Never add new tracked files under ignored paths.
Working evidence and suite logs belong in gitignored `.superpowers/`.
`docs/test-artifacts/` holds final owner acceptance transcripts/outputs only.

Copy `.env.example` to `.env` and set `LIVESYNC_BACKEND`, the backend credentials, and exactly one auth mode before running against real data.

## Coding Style & Naming Conventions

Use Python 3.11+ and PEP 8 conventions: 4-space indentation, clear names, and snake_case for variables, functions, and helpers. Keep MCP tool handlers descriptive and aligned with existing names such as `create_note`, `manage_tags`, and `vault_stats`. Prefer typed function signatures where practical.

No formatter or linter config is committed. If adding one, document the command here and avoid reformatting unrelated files.

## Testing Guidelines

`tests/test_config_public.py` inventories validated and unrestricted environment
variables and checks value-free errors through configuration and both startup entry points.

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

`tests/regex_doubles.py` injects typed tool-level request deadlines without timing process startup. Real worker timeout/reap/restart remains covered separately. Concurrency tests use event rendezvous; performance smoke tests use generous scheduler margins. Normal suites run serially; gate F1 additionally verifies two independent suite processes in parallel with separate logs/temp state.

`scripts/extract_chunk_parameter_vectors.mjs` directly executes the pinned cap expression plus native JSON ToPrimitive/ToNumber/addition probes, retaining lexical overflow inputs. Coercion cell generator modes INCLUDE_MINIMUM, COERCION_EDGES, CROSS_EDGES, INGRESS_EDGES, OVERFLOW_EDGES and ZERO_EDGES cover parameter types, evaluation order and true loops.

`content_splitter.SplitterRefusal` is a codec-owned ValueError with a typed diagnostic; storage backends map it to WriterParameterRefused before publication. Codec modules retain their storage-independent boundary.

`tests/test_reserved_compression_prefix.py` reuses the committed native compression oracle for both write settings, repeat/fresh-reader lifecycle, typed notices, invalid-prefix preflight and shared-leaf protection.

`scripts/extract_minimum_concatenation_vectors.mjs` records native V2 array-minimum repeated-concatenation neighbours through the fourth adjustment using the integrity-verified splitter harness; `tests/test_coercion_cells.py` consumes its exact pieces and IDs.

`scripts/extract_scalar_policy_vectors.mjs` executes the pinned VaultService size method and targetFilter truthiness gate with Node builtins only; `tests/fixtures/scalar_policy_upstream.json.gz` records source hashes and native predicate/equivalence vectors consumed by tweak and backend selection tests.

`scripts/extract_deep_scalar_vectors.mjs` executes pinned size/cap/adjustment expressions with native JS builtins and records source hashes and Node stack flags. `tests/test_deep_scalar.py` covers deep scalar coercion, cyclic/shared arrays, both backend startups and journal refresh.

`scripts/extract_jwt_vectors.mjs` accepts an optional fifth argument naming the original JWT fixture to emit cache-lifecycle expectations with the same synthetic keys. `tests/fixtures/jwt_refresh_upstream.json` covers two successive native cache boundaries, exact refreshed timestamps and HS token bytes.

`tests/test_integration_parity_s3.py` and `tests/test_integration_parity_couchdb.py`
add env-gated real-server pairwise/oracle, surrogate and JWT acceptance.
See `docs/connecting-storage.md` for environment and incomplete acceptance scope.

`scripts/extract_acceptance_vectors.mjs` executes the integrity-verifying native
splitter harness for 40/100 MiB binary inputs; its compact committed
`tests/fixtures/acceptance_upstream.json` retains ordered children, piece
digests, source hashes and published dependency provenance. Real-server F4
coverage extends both parity integration modules with per-row document
classes, large uploads/MCP text access, recovery, compression and request hooks.


## Script catalogue and application construction

[Oracle regeneration and verified acquisition](docs/regenerating-oracle-fixtures.md)
catalogues every `scripts/extract_*.mjs`, `scripts/probe_*.mjs`, and
`scripts/vendor_selection.py`, with exact commands, input provenance, output
fixtures and validating tests. Generate expectations only by executing the pinned
upstream with the verified published dependencies.

| Group | Scripts | Storage / side effects |
|---|---|---|
| Offline oracle generators | `scripts/extract_*.mjs`, `scripts/probe_*.mjs` | Scratch fixture/probe outputs; `extract_selection_runtime.mjs` instead rewrites tracked vendor runtime and provenance |
| Provenance helpers | `scripts/splitter_oracle_dependencies.mjs`, `scripts/vendor_selection.py` | Dependency helper verifies archives/imported members; vendor helper downloads exact lockfile versions and writes tracked vendor files; no vault access |
| Offline mutation campaign | `scripts/check_splitter_mutations.py` | In-memory source mutations and isolated test runs; ignored evidence output; never rewrites production source |
| Offline benchmarks | `scripts/benchmark_splitters.py`, `scripts/benchmark_selection.py`, `scripts/benchmark_diagnostics.py` | Synthetic local inputs; JSON on stdout; redirect evidence to ignored scratch space |
| Upstream watcher | `scripts/check_upstream.sh` | Network GitHub reads; its CI workflow maintains drift issues; not an offline command; needs authenticated `gh` |
| Owner acceptance | `scripts/live_check_parity.py`, `scripts/live_check_s3.py` | Real storage access; parity defaults read-only and requires explicit write/delete gates; legacy S3 helper's write-phase mutates storage without those new gates; use only the documented dedicated test vault |

Run the complete offline splitter campaign from the repository root:

```bash
./venv/bin/python scripts/check_splitter_mutations.py .superpowers/sdd/2026-11-23-settings-parity/d/campaign
```

For application tests and embedding, `server.create_app(cfg)` accepts a validated
injected `config.Config`. That same object controls client authentication, public
URL, host, port and backend startup. `run.py` loads dotenv before application
construction; alternative launchers must supply their environment themselves.
Restart an application after changing its configuration.


F6 property projection: `frontmatter.py` preflights YAML events before
round-trip construction, memoises aliases and binds `FrontmatterLimits` through
the per-tool context. `tests/test_frontmatter_limits.py` covers aliases, bombs
and byte preservation; `tests/test_frontmatter_tools_limits.py` covers both
backend doubles and value-free typed errors.
`scripts/extract_frontmatter_admission_vectors.mjs` executes the pinned native
size predicate (Node builtins, no packages) into
`tests/fixtures/frontmatter_admission_upstream.json`. This is admission evidence,
not an invented plugin YAML parser oracle. See `docs/frontmatter-properties.md`.

F7 capability probes use a test-only 60-second scheduler margin in tests/conftest.py; production selection deadlines remain configurable and unchanged. Stalled-worker deadline tests wait for readiness before applying the short request timeout. tests/test_retry_confirmation.py covers original-key confirmation and pending state after ambiguous conditional publication; real-server cases live in tests/test_integration_parity_s3.py.

F8 documentation contracts read the environment table in `docs/configuration.md`;
README is the practical entry point and `docs/peer-limitations.md` holds the
complete setting/difference inventory. `docs/couchdb-jwt.md` holds JWT provisioning;
`docs/developer-acceptance.md` holds automated real-server acceptance.
