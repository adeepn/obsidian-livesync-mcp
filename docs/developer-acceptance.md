# Developer acceptance

## Automated real-server acceptance

The env-gated tests use disposable S3 prefixes and `mcp_test_<random>` CouchDB
databases. Supply `MINIO_TEST_ENDPOINT`, `MINIO_TEST_KEY`, `MINIO_TEST_SECRET`,
`MINIO_TEST_BUCKET`, `COUCHDB_TEST_URL`, `COUCHDB_TEST_USER` and
`COUCHDB_TEST_PASSWORD` only in the invoking shell. No variables means skips
and no server access. Never point these tests at an owner's vault.

`tests/test_integration_parity_s3.py` and
`tests/test_integration_parity_couchdb.py` reuse the 60-row pairwise settings
selection and committed published-dependency oracle. `MINIO_TEST_MATRIX=full`
also includes the existing supplemental branch rows on both backends. The
matrix lifecycle retains each row's document kind and additionally checks plain
text and binary newnote documents in every matrix row, with exact native-oracle
children and fresh-reader comparisons.

JWT tests additionally require `COUCHDB_TEST_JWT_URL`,
`COUCHDB_TEST_JWT_SUBJECT`, and `COUCHDB_TEST_JWT_HS_KEY` or
`COUCHDB_TEST_JWT_ES_KEY`; ES512 uses `COUCHDB_TEST_JWT_ES512_KEY` and
`COUCHDB_TEST_JWT_KEY_ID_ES512=es512`. EC keys must be PKCS8 PEM. All four
algorithms (HS256, HS512, ES256, ES512), wrong-key and expired-clock refusals
are exercised. Backend construction
uses JWT configuration without Basic credentials. Test environment values are
never copied into `.env` or committed artifacts.

```bash
./venv/bin/python -m pytest tests/test_integration_minio.py tests/test_integration_couchdb.py tests/test_integration_parity_s3.py tests/test_integration_parity_couchdb.py -q --tb=short
```

The final F4 run closes JWT wrong-key HTTP 400 classification and confirms
CouchDB rejects device-shaped lone-surrogate JSON while the peer refuses it
before sending any request. The rotated EC test keys use PKCS8.

Acceptance also exercises 40 MiB and 100 MiB binary uploads under a 200 MiB
vault policy, byte-exact fresh-peer reads, exact native-oracle child IDs and
piece digests, a 201 MiB skip without publication, and an oversized rename
with its source intact. A 12 MiB textual `.json` newnote traverses `create_app`
MCP create, whole/window fetch, near-end search, append and search-replace.
Recovery checks retained local revisions/chunks after wipe and epoch changes,
identity quarantine with remote rebuild, and interrupted replay checkpoints.
S3 before-send events capture custom headers on authenticated outgoing requests;
CouchDB request hooks cover GET, POST and PUT, including local documents,
listing and leaf/metadata publication. Real compression reads include committed
upstream vectors with the peer write setting both false and true.

Large-file oracle regeneration (offline; Node 26.13.1+, exact published archives):

```bash
node scripts/extract_acceptance_vectors.mjs COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TGZ WASM_TGZ tests/fixtures/acceptance_upstream.json
```

See [the final acceptance artifact](test-artifacts/2026-11-25-parity-docker/summary.txt)
for counts, durations, input digests, image digests and reproduction commands.
This establishes the enumerated scenarios on these servers; it does not prove
all possible settings or server combinations.


