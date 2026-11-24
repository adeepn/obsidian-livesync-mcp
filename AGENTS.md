# Repository Guidelines

## Project Structure & Module Organization

This repository is a small Python MCP server for accessing an Obsidian LiveSync vault through a configurable storage backend (CouchDB or S3 journal). Core files live at the root:

- `server.py`: FastMCP server setup, all MCP tool definitions, strict UTF-8 text representation of newnotes, auth wiring, and the `get_backend(cfg)` factory that builds the storage backend from configuration.
- `config.py`: strict environment validation (`load_config`) — backend selection, backend credentials, client auth; rejects placeholder secrets.
- `storage_base.py`: storage-agnostic `StorageBackend` protocol plus the storage exception hierarchy.
- `storage_couchdb.py`: `CouchDBBackend`, the LiveSync-aware CouchDB implementation of the protocol.
- `journal_codec.py`: LiveSync journal record parsing and serialization.
- `journal_store.py`: local SQLite revision state and on-disk chunk cache; `state_size_bytes()` measures SQLite/sidecar and chunk bytes for backend/tool observability.
- `object_storage.py`: S3 object storage adapter using boto3.
- `storage_journal.py`: append-only LiveSync S3 journal storage backend.
- `livesync_format.py`: LiveSync document/chunk codecs, device disk-ingestion document typing, path rules, and vault validation.
- `text_eligibility.py`: bounded incremental UTF-8/control-character projection of newnote pieces, shared by both backends.
- `frontmatter.py`: round-trip YAML frontmatter editing (ruamel.yaml).
- `links.py`: Obsidian-compatible link extraction, resolution, and graph building.
- `auth_config.py`: auth-mode selection and the static bearer token verifier.
- `oauth_provider.py`: OAuth 2.1 authorization provider used when OAuth is enabled.
- `run.py`: local entry point; loads `.env` before importing the server.
- `requirements.txt` / `requirements-dev.txt` / `requirements.lock`: runtime, dev, and frozen dependency sets.
- `.env.example`: required configuration template.
- `obsidian-mcp.service`: optional systemd deployment unit.

Tests live in `tests/` as `tests/test_<module>.py` (pytest + pytest-asyncio, auto mode via `pytest.ini`); shared doubles are `tests/fake_backend.py` (in-memory backend) and `tests/couchdb_fake.py` (HTTP-level CouchDB double). Keep new modules focused and colocated unless the project grows enough to justify a package layout.

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
./venv/bin/python -m pytest
```

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
