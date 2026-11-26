"""Keep executable documentation aligned with actual environment consumers."""
import ast
from dataclasses import fields
from io import StringIO
from pathlib import Path
import re
import secrets

from dotenv import dotenv_values
import pytest

from config import load_config
from replay_limits import ReplayLimits

ROOT = Path(__file__).resolve().parents[1]
def documentation_text(name):
    # Semantic contracts follow their named focused pages after F8 relocation.
    locations = {
        'README.md': ('README.md', 'docs/configuration.md', 'docs/peer-limitations.md'),
        'docs/connecting-storage.md': ('docs/connecting-storage.md', 'docs/couchdb-jwt.md', 'docs/developer-acceptance.md'),
    }
    return '\n'.join((ROOT / path).read_text() for path in locations.get(name, (name,)))


ENV_NAME = r'(?:LIVESYNC|COUCHDB|S3|MCP|OAUTH)_[A-Z0-9_]+'


def environment_names():
    # Include the separate render-time debug consumer, deriving its spelling
    # from actual source instead of hardcoding a second inventory.
    trees = [ast.parse(documentation_text(name)) for name in ('config.py', 'diagnostic_hygiene.py')]
    names = {node.value for tree in trees for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and re.fullmatch(ENV_NAME, node.value) and not node.value.endswith('_')}
    names |= {'LIVESYNC_JOURNAL_' + field.name.upper() for field in fields(ReplayLimits)}
    return names


def test_environment_inventory_matches_readme_and_template():
    actual = environment_names()
    readme = set(re.findall(r'^\| `(' + ENV_NAME + r')` \|',
                            (ROOT / 'docs/configuration.md').read_text(), re.M))
    template = set(re.findall(r'^(?:# )?(' + ENV_NAME + r')=',
                              (ROOT / '.env.example').read_text(), re.M))
    assert readme == actual
    inline_names = set(re.findall(r'\b(' + ENV_NAME + r')\b', documentation_text('README.md')))
    assert inline_names <= actual
    assert template == actual


def blocks():
    for path in [ROOT / 'README.md', ROOT / 'docs/connecting-storage.md', ROOT / 'docs/couchdb-jwt.md']:
        for index, block in enumerate(re.findall(r'```dotenv\n(.*?)```', path.read_text(), re.S)):
            yield f'{path.name}-{index}', block


@pytest.mark.parametrize('name,block', list(blocks()))
def test_documented_connection_block_loads(name, block):
    # The docs explicitly require replacing <...>. These replacements are
    # generated only in process, never credentials copied from a real vault.
    values = {
        '<paste-generated-token>': secrets.token_urlsafe(32),
        '<CouchDB-server-URI>': 'http://127.0.0.1:5984',
        '<plugin-jwt-key>': 'c3ludGhldGljLWRvY3MtdGVzdC1rZXk=',
        '<plugin-username>': 'local-test-user',
        '<plugin-password>': secrets.token_urlsafe(32),
        '<plugin-database-name>': 'local-test-vault',
        '<paste-sls://-setup-URI>': 'sls://testkey:testsecret@http://127.0.0.1:19000?bucket=plan-b-it&region=us-east-1&prefix=livetest/',
        '<plugin-endpoint-URL>': 'http://127.0.0.1:19000',
        '<plugin-access-key>': 'testkey',
        '<plugin-secret-key>': 'testsecret',
        '<plugin-bucket>': 'plan-b-it',
        '<plugin-custom-header-value>': 'synthetic-docs-header',
    }
    for marker, value in values.items():
        block = block.replace(marker, value)
    assert '<' not in block, name
    env = dict(dotenv_values(stream=StringIO(block)))
    if 'LIVESYNC_BACKEND' not in env:
        # Shared auth block is composed with a backend, as instructed.
        env.update(LIVESYNC_BACKEND='couchdb', COUCHDB_USER='local-test-user',
                   COUCHDB_PASSWORD=secrets.token_urlsafe(32), COUCHDB_DATABASE='local-test-vault')
    env.setdefault('MCP_API_KEY', secrets.token_urlsafe(32))
    cfg = load_config(env)
    assert cfg.backend == env['LIVESYNC_BACKEND']


def test_no_tracked_files_under_ignored_paths():
    import subprocess
    result = subprocess.run(['git', 'ls-files', '-i', '-c', '--exclude-standard'],
                            cwd=ROOT, check=True, capture_output=True, text=True)
    assert not result.stdout


@pytest.mark.parametrize('connection', ('couchdb', 's3-fields', 's3-uri'))
@pytest.mark.parametrize('auth', ('bearer', 'oauth'))
def test_env_template_loads_after_required_configuration(connection, auth):
    env = dict(dotenv_values(ROOT / '.env.example'))
    # Follow the template's instructions: choose one backend/auth group and
    # replace its required secrets. Never write a configured .env to disk.
    for name in tuple(env):
        if name.startswith(('COUCHDB_', 'S3_', 'OAUTH_')) or name in ('MCP_API_KEY', 'LIVESYNC_S3_URI'):
            del env[name]
    env['LIVESYNC_BACKEND'] = 'couchdb' if connection == 'couchdb' else 's3'
    if connection == 'couchdb':
        env.update(COUCHDB_USER='local-test-user', COUCHDB_PASSWORD=secrets.token_urlsafe(32),
                   COUCHDB_DATABASE='local-test-vault')
    elif connection == 's3-fields':
        env.update(S3_ENDPOINT_URL='http://127.0.0.1:19000', S3_ACCESS_KEY_ID='testkey',
                   S3_SECRET_ACCESS_KEY='testsecret', S3_BUCKET='plan-b-it')
    else:
        env['LIVESYNC_S3_URI'] = 'sls://testkey:testsecret@http://127.0.0.1:19000?bucket=plan-b-it'
    if auth == 'bearer':
        env['MCP_API_KEY'] = secrets.token_urlsafe(32)
    else:
        env.update({name: secrets.token_urlsafe(32) for name in
                    ('OAUTH_PASSWORD', 'OAUTH_CLIENT_ID', 'OAUTH_CLIENT_SECRET')})
    cfg = load_config(env)
    assert bool(cfg.api_key) == (auth == 'bearer')
    assert cfg.backend == env['LIVESYNC_BACKEND']


def test_surrogate_wire_contract_is_documented_without_a_switch():
    for name in ('README.md', '.env.example', 'docs/s3-journal.md',
                 'docs/vault-format.md', 'CHANGELOG.md'):
        text = documentation_text(name)
        assert 'transport_replacements' in text, name
    journal = (ROOT / 'docs/s3-journal.md').read_text()
    assert 'U+FFFD' in journal and 'TextEncoder' in journal
    assert 'JSON.stringify' in (ROOT / 'docs/vault-format.md').read_text()
    assert 'wire_encoding.py' in (ROOT / 'AGENTS.md').read_text()
    assert 'extract_surrogate_vectors.mjs' in (ROOT / 'AGENTS.md').read_text()
    spec = (ROOT / 'docs/superpowers/specs/2026-11-23-settings-parity.md').read_text()
    assert '2026-11-24 — G7' in spec and '[GOTCHA]' in spec


@pytest.mark.parametrize('name', [
    'tests/splitter_comparisons.json', 'tests/splitter_observability.py',
    'tests/splitter_mutations.json', 'scripts/check_splitter_mutations.py',
    'tests/fixtures/settings_oracle_upstream.json.gz',
    'tests/test_splitter_observability.py', 'tests/test_splitter_helpers_oracle.py',
    'tests/test_settings_oracle_guard.py',
])
def test_splitter_observability_artifacts_are_documented(name):
    assert (ROOT / name).is_file()
    assert name in (ROOT / 'AGENTS.md').read_text()


def test_part_zero_status_records_executable_inventory():
    status = (ROOT / 'docs/superpowers/specs/2026-11-23-settings-parity.md').read_text()
    assert 'Part 0 round 2 — 2026-11-24 (S1, P-27/P-32)' in status
    assert 'tests/splitter_comparisons.json' in status
    assert 'helper-level oracle vectors' in status


@pytest.mark.parametrize('value,expected', [(None, None), ('', None), ('true', True), ('false', False)])
def test_ignore_selection_tristate_defaults(value, expected):
    env = dict(LIVESYNC_BACKEND='couchdb', COUCHDB_USER='test-user',
               COUCHDB_PASSWORD='test-password', COUCHDB_DATABASE='vault', MCP_API_KEY='test-key')
    if value is not None:
        env['LIVESYNC_USE_IGNORE_FILES'] = value
        env['LIVESYNC_IGNORE_FILES'] = ''
    cfg = load_config(env)
    assert cfg.selection_use_ignore_files is expected
    assert cfg.selection_ignore_files is None
    template = dict(dotenv_values(ROOT / '.env.example'))
    assert template['LIVESYNC_USE_IGNORE_FILES'] == ''
    assert template['LIVESYNC_IGNORE_FILES'] == ''


@pytest.mark.parametrize('use', [None, '', 'true', 'false'])
@pytest.mark.parametrize('names', [None, '', '.gitignore, rules.txt'])
def test_ignore_environment_choices_are_independent(use, names):
    env = dict(LIVESYNC_BACKEND='couchdb', COUCHDB_USER='test-user',
               COUCHDB_PASSWORD='test-password', COUCHDB_DATABASE='vault', MCP_API_KEY='test-key')
    if use is not None:
        env['LIVESYNC_USE_IGNORE_FILES'] = use
    if names is not None:
        env['LIVESYNC_IGNORE_FILES'] = names
    cfg = load_config(env)
    assert cfg.selection_use_ignore_files is (None if not use else use == 'true')
    assert cfg.selection_ignore_files == (None if not names else ('.gitignore', 'rules.txt'))


def test_vault_ignore_derivation_documentation_contract():
    readme = documentation_text('README.md')
    for setting in ('useIgnoreFiles', 'ignoreFiles'):
        row = next(line for line in readme.splitlines() if line.startswith('| `' + setting + '` |'))
        assert '| Identical |' in row
        assert 'Derived from vault settings unless overridden' in row
    focused = (ROOT / 'docs/selection-filters.md').read_text()
    assert 'Reads bypass all selection filters, including explicitly configured filters.' in focused
    assert 'Missing or unreadable ignore files are treated as absent, matching upstream.' in focused
    assert 'Regex lists stay env-only' in focused
    assert 'including vault useIgnoreFiles=true' in focused


def test_couchdb_compression_documentation_contract():
    readme = documentation_text('README.md')
    row = next(line for line in readme.splitlines() if line.startswith('| `enableCompression` |'))
    assert '| Identical |' in row
    assert 'CR/LF' in row and 'surrogate' in row and 'BOM' in row
    template = (ROOT / '.env.example').read_text()
    assert 'enableCompression follows the vault; no environment override' in template
    focused = (ROOT / 'docs/vault-format.md').read_text()
    assert 'zlib raw' in focused and 'level 8' in focused
    assert 'U+FFFD replacement' in focused and 'CompressedDataError' in focused
    tweaks = (ROOT / 'docs/tweak-compatibility.md').read_text()
    assert 'no CouchDB compression adapter' not in tweaks
    assert 'typed boolean `enableCompression`' in tweaks


def test_legacy_parameter_recovery_documented():
    readme = documentation_text('README.md')
    focused = (ROOT / 'docs/vault-format.md').read_text()
    assert 'Nonpositive legacy piece caps and V2 plain-text minimums are unsupported' not in readme
    assert 'plugin itself does not terminate' in readme
    for name in ('customChunkSize', 'minimumChunkSize'):
        assert f'| `{name}` |' in readme
        assert name in focused and name in (ROOT / '.env.example').read_text()
    for term in ('864', 'UTF-16', 'single code point', 'null', 'NaN', 'Recovery'):
        assert term in focused


def test_custom_header_connection_contract():
    for variable in ('COUCHDB_CUSTOM_HEADERS', 'S3_CUSTOM_HEADERS', 'S3_FORCE_PATH_STYLE'):
        assert variable in environment_names()
        assert variable in documentation_text('docs/connecting-storage.md')
    readme = documentation_text('README.md')
    for setting in ('couchDB_CustomHeaders', 'bucketCustomHeaders', 'forcePathStyle'):
        assert f'| `{setting}` |' in readme
    assert 'set S3_FORCE_PATH_STYLE=false' in readme
    connection = documentation_text('docs/connecting-storage.md')
    for term in ('Custom Headers', 'JSON object', 'python-dotenv', 'entry', 'control', 'https://', '127.0.0.1'):
        assert term in connection
    for name in ('docs/s3-journal.md', 'docs/vault-format.md', 'CHANGELOG.md'):
        assert 'custom headers' in documentation_text(name)
    assert '2026-11-25 — G5' in (ROOT / 'docs/superpowers/specs/2026-11-23-settings-parity.md').read_text()


def test_jwt_connection_contract_is_documented():
    names = ('COUCHDB_JWT_ALGORITHM', 'COUCHDB_JWT_KEY', 'COUCHDB_JWT_SUBJECT',
             'COUCHDB_JWT_KEY_ID', 'COUCHDB_JWT_EXPIRY_MINUTES')
    for file in ('README.md', '.env.example', 'docs/connecting-storage.md'):
        text = documentation_text(file)
        assert all(name in text for name in names), file
    text = documentation_text('docs/connecting-storage.md')
    for label in ('Use JWT Authentication', 'JWT Algorithm', 'JWT Key',
                  'JWT Key ID (kid)', 'JWT Subject (sub)',
                  'JWT Expiration Duration (minutes)', 'PKCS8', 'hmac:_default',
                  'ec:_default', 'authentication_handlers', 'auth_mode', 'jwt_algorithm'):
        assert label in text
    assert 'CouchDB JWT auth are not supported' not in documentation_text('README.md')
    assert '2026-11-25 — G6' in (ROOT / 'docs/superpowers/specs/2026-11-23-settings-parity.md').read_text()
    for artifact in ('couchdb_jwt.py', 'scripts/extract_jwt_vectors.mjs'):
        assert artifact in (ROOT / 'AGENTS.md').read_text()


def test_text_access_budget_defaults_and_documented_controls():
    from tests.test_config import COUCHDB_ENV
    cfg = load_config(COUCHDB_ENV)
    assert cfg.text_scan_max_bytes is None
    assert cfg.regex_max_output_bytes == 0
    assert cfg.regex_timeout_seconds == 10
    for name in ('README.md', '.env.example', 'docs/scan-policy.md', 'docs/tools.md'):
        text = documentation_text(name)
        for variable in ('LIVESYNC_TEXT_SCAN_MAX_BYTES', 'LIVESYNC_REGEX_MAX_OUTPUT_BYTES',
                         'LIVESYNC_REGEX_TIMEOUT_SECONDS'):
            assert variable in text
        assert '256 MiB' in text

    template = dotenv_values(ROOT / '.env.example')
    assert template['LIVESYNC_TEXT_SCAN_MAX_BYTES'] == ''
    assert template['LIVESYNC_REGEX_MAX_OUTPUT_BYTES'] == '0'
    assert template['LIVESYNC_REGEX_TIMEOUT_SECONDS'] == '10'
    readme = documentation_text('README.md')
    for key, default in [('LIVESYNC_TEXT_SCAN_MAX_BYTES', 'empty'),
                         ('LIVESYNC_REGEX_MAX_OUTPUT_BYTES', '0'),
                         ('LIVESYNC_REGEX_TIMEOUT_SECONDS', '10')]:
        assert f'| `{key}` | `{default}` |' in readme


def test_complete_scan_defaults_and_documented_pagination():
    """Pin peer controls, not upstream wire oracle values.

    Commonlib serviceModules/ServiceFileAccessBase.ts:287-290 enumerates all
    files; pagination and optional operational budgets are peer transport.
    """
    from tests.test_config import COUCHDB_ENV
    cfg = load_config(COUCHDB_ENV)
    defaults = {
        'LIVESYNC_SCAN_MAX_NOTES': '0',
        'LIVESYNC_SCAN_MAX_BYTES': '0',
        'LIVESYNC_SCAN_MAX_SECONDS': '20',
        'LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES': '65536',
    }
    assert (cfg.scan_max_notes, cfg.scan_max_bytes, cfg.scan_max_seconds,
            cfg.text_verdict_cache_entries) == (0, 0, 20, 65536)
    template = dotenv_values(ROOT / '.env.example')
    readme = documentation_text('README.md')
    for key, value in defaults.items():
        assert template[key] == value
        assert f'| `{key}` | `{value}` |' in readme
    for name in ('README.md', 'docs/scan-policy.md', 'docs/tools.md'):
        text = documentation_text(name)
        for token in ('cursor', 'next_cursor', 'elapsed_ms', 'stopped_because',
                      'start_line', 'end_line', 'scans_busy'):
            assert token in text
    tools = (ROOT / 'docs/tools.md').read_text()
    for tool in ('obsidian_search', 'obsidian_list_notes', 'obsidian_get_all_tags',
                 'obsidian_get_links', 'obsidian_get_link_graph',
                 'obsidian_find_orphaned_notes', 'obsidian_find_hub_notes',
                 'obsidian_list_templates', 'obsidian_vault_stats'):
        assert f'`{tool}`' in tools
    policy = (ROOT / 'docs/scan-policy.md').read_text()
    for token in ('index_complete', 'index_ids_collected', 'index_docs_indexed',
                  'index_next_skip', 'update_seq', 'scanned pages only',
                  'persistent', 'total_lines', 'returned_lines'):
        assert token in policy
    assert '2026-11-25 — G4b' in (ROOT / 'docs/superpowers/specs/2026-11-23-settings-parity.md').read_text()


def test_collision_recovery_documented():
    text = documentation_text('README.md')
    assert 'Distinct colliding CouchDB pieces refuse before publication' in text
    assert 'Journal within-publication collisions compare TextEncoder-projected data' in (ROOT / 'docs/vault-format.md').read_text()


def test_custom_coercion_documented():
    assert 'JavaScript truthiness/ToNumber coercion' in documentation_text('README.md')
    assert 'V2 binary data loss' in (ROOT / 'docs/vault-format.md').read_text()


def test_minimum_addition_documented():
    assert 'JavaScript ToPrimitive/addition' in documentation_text('README.md')
    assert 'integer shortcut and its invalid array-equivalence proof have been removed' in (ROOT / 'docs/vault-format.md').read_text()


def test_typed_publication_notices_documented():
    text = (ROOT / 'docs/outcome-reporting.md').read_text()
    for code in ('legacy_bom_removed','invalid_utf8_replaced','input_surrogates_replaced','journal_surrogates_replaced'):
        assert code in text
    assert 'original tool result remains verbatim' in documentation_text('README.md')
    assert 'typed notices' in (ROOT / '.env.example').read_text()


def test_ignore_name_warning_documented():
    for name in ('README.md','docs/selection-filters.md','docs/tools.md','.env.example'):
        assert 'selection_unresolved_ignore_names' in documentation_text(name)


def test_runtime_journal_size_policy_documented():
    assert 'Journal refresh re-resolves' in documentation_text('README.md')
    assert 'every unthrottled refresh' in (ROOT / 'docs/size-policy.md').read_text()


def test_jwt_zero_expiry_documented():
    assert '0 uses the plugin five-minute fallback' in documentation_text('README.md')
    assert 'zero uses the plugin five-minute fallback' in (ROOT / '.env.example').read_text()
    assert 'already-expired tokens' in documentation_text('docs/connecting-storage.md')


def test_json_coercion_recovery_documented():
    text = (ROOT / 'docs/vault-format.md').read_text()
    for fragment in ('noncallable toString','lexical overflow','zero-preserving addition','closed typed diagnostics'):
        assert fragment in text


def test_reserved_compression_prefix_notice_documented():
    for name in ('docs/outcome-reporting.md','docs/vault-format.md','docs/tools.md'):
        assert 'couchdb_marker_interpreted' in documentation_text(name)
    assert 'binary attachment path to preserve literal prefix bytes' in ' '.join(documentation_text('README.md').split())


def test_jwt_empty_and_string_zero_documented():
    for name in ('README.md', '.env.example', 'docs/connecting-storage.md', 'docs/vault-format.md'):
        text = documentation_text(name)
        assert 'empty' in text and 'refused' in text
        assert 'zero-second' in text or 'zero seconds' in text
        assert 'cannot' in text and 'numeric' in text


def test_atomic_journal_policy_documented():
    for name in ('README.md', 'docs/size-policy.md'):
        text = documentation_text(name)
        assert 'before committing any effective setting' in text
        assert 'previous' in text and 'tuple' in text


def test_shared_leaf_recovery_documented():
    for name in ('README.md', 'docs/vault-format.md'):
        text = ' '.join(documentation_text(name).split())
        assert 'resolve the conflicting leaf in Obsidian' in text
        assert 'alternate hashAlg or a positive customChunkSize' in text


def test_vault_scalar_coercion_and_strict_overrides_documented():
    from tests.test_config import COUCHDB_ENV
    from config import ConfigError
    for value in ('true', 'null', '[]', '{}', 'Infinity', 'PRIVATE_INVALID'):
        with pytest.raises(ConfigError, match='LIVESYNC_SYNC_MAX_SIZE_MB'):
            load_config({**COUCHDB_ENV, 'LIVESYNC_SYNC_MAX_SIZE_MB': value})
    for name in ('README.md', '.env.example', 'docs/size-policy.md'):
        text = ' '.join(documentation_text(name).split())
        assert '1 MiB' in text and 'coercion' in text
    for name in ('README.md', '.env.example', 'docs/selection-filters.md'):
        text = documentation_text(name)
        assert 'truthiness' in text


def test_candidate_selection_capability_documented():
    for name in ('README.md', 'docs/size-policy.md'):
        text = documentation_text(name)
        assert 'While that check awaits' in text
        assert 'without committing the candidate' in text
        assert 'revalidates a restored previously active policy' in text


def test_iterative_scalar_depth_documented():
    for name in ('README.md', '.env.example', 'docs/size-policy.md'):
        text = documentation_text(name)
        assert 'iterative' in text and 'JSON admission' in text
        assert 'no coercion depth limit' in text
    text = (ROOT / 'docs/size-policy.md').read_text()
    assert 'cycles' in text and '1000/5000' in text
    assert 'Node --stack-size=16384' in text


def test_real_server_acceptance_scope_documented():
    for name in ('docs/upstream-pin.md', 'docs/developer-acceptance.md'):
        text = documentation_text(name)
        assert '60-row pairwise' in text
        assert 'JWT wrong-key HTTP 400' in text
        assert '2026-11-25-parity-docker/summary.txt' in text
    assert 'Acceptance credentials belong only in the invoking shell' in (ROOT / '.env.example').read_text()


def test_couchdb_surrogate_constraint_documented():
    # chunks.ts:243-252; live CouchDB 3.5.3, 2026-11-25 invalid UTF-8 JSON.
    for name in ('README.md', 'docs/vault-format.md', 'docs/outcome-reporting.md'):
        text = documentation_text(name)
        assert 'CouchDB 3.5.3' in text
        if name != 'docs/outcome-reporting.md':
            assert 'v3' in text
        assert 'surrogate' in text
        assert 'lossless CouchDB retains those units' not in text


def test_jwt_session_auth_documented():
    for name in ('README.md', 'docs/connecting-storage.md', '.env.example'):
        text = documentation_text(name)
        assert '/_session' in text
        assert 'subject' in text


def test_jwt_pkcs8_generation_documented():
    text = documentation_text('docs/connecting-storage.md')
    assert 'openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt' in text
    for name in ('README.md', 'docs/connecting-storage.md', '.env.example'):
        text = documentation_text(name)
        assert 'PKCS8' in text and 'SEC1' in text
    assert 'secp521r1' in (ROOT / '.env.example').read_text()


def test_f4_real_acceptance_contract_documented():
    # Live evidence complements the pinned native splitter/compression oracles.
    for name in ('docs/upstream-pin.md', 'docs/developer-acceptance.md'):
        text = documentation_text(name)
        for phrase in ('40 MiB', '100 MiB', '201 MiB', '12 MiB', 'every matrix row'):
            assert phrase in text
    guide = documentation_text('docs/connecting-storage.md')
    for algorithm in ('HS256', 'HS512', 'ES256', 'ES512'):
        assert algorithm in guide
    assert 'COUCHDB_TEST_JWT_ES512_KEY' in guide
    assert '2026-11-25 — F4 final automated acceptance' in (ROOT / 'docs/superpowers/specs/2026-11-23-settings-parity.md').read_text()
    assert 'extract_acceptance_vectors.mjs' in (ROOT / 'AGENTS.md').read_text()


def test_startup_configuration_error_policy_is_documented():
    readme = documentation_text('README.md')
    template = (ROOT / '.env.example').read_text()
    policy = (ROOT / 'docs/diagnostics-and-secrets.md').read_text()
    assert 'without echoing supplied values' in readme
    assert 'without echoing supplied values' in template
    assert 'never the supplied value' in policy
    assert '`MCP_PORT`' in policy and '`LIVESYNC_BACKEND`' in policy


# The paths below map consumers to their returned fields; defaults and validation
# expectations come from the published configuration table, not a second value table.
CONFIG_PATHS = {
    'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES': 'couchdb_max_inflated_bytes',
    'LIVESYNC_MAX_REQUEST_BYTES': 'max_request_bytes',
    'LIVESYNC_FRONTMATTER_MAX_BYTES': 'frontmatter_max_bytes',
    'LIVESYNC_FRONTMATTER_MAX_NODES': 'frontmatter_max_nodes',
    'LIVESYNC_FRONTMATTER_MAX_DEPTH': 'frontmatter_max_depth',
    'COUCHDB_CONNECT_TIMEOUT_SECONDS': 'couchdb.connect_timeout_seconds',
    'COUCHDB_IO_TIMEOUT_SECONDS': 'couchdb.io_timeout_seconds',
    'S3_CONNECT_TIMEOUT_SECONDS': 's3.connect_timeout_seconds',
    'S3_READ_TIMEOUT_SECONDS': 's3.read_timeout_seconds',
    'S3_MAX_ATTEMPTS': 's3.max_attempts',
    'LIVESYNC_SELECTION_TIMEOUT_SECONDS': 'selection_timeout_seconds',
    'LIVESYNC_BACKEND': 'backend', 'COUCHDB_URL': 'couchdb.url',
    'COUCHDB_USER': 'couchdb.user', 'COUCHDB_PASSWORD': 'couchdb.password',
    'COUCHDB_DATABASE': 'couchdb.database', 'COUCHDB_CUSTOM_HEADERS': 'couchdb.custom_headers',
    'LIVESYNC_S3_URI': 's3.uri', 'S3_ENDPOINT_URL': 's3.endpoint',
    'S3_ACCESS_KEY_ID': 's3.access_key', 'S3_SECRET_ACCESS_KEY': 's3.secret_key',
    'S3_BUCKET': 's3.bucket', 'S3_REGION': 's3.region', 'S3_PREFIX': 's3.prefix',
    'S3_STATE_DIR': 's3.state_dir', 'S3_REFRESH_MIN_INTERVAL_SEC': 's3.refresh_min_interval_sec',
    'S3_STATE_SIZE_WARN_MB': 's3.state_size_warn_mb', 'S3_CUSTOM_HEADERS': 's3.custom_headers',
    'S3_FORCE_PATH_STYLE': 's3.force_path_style',
    'MCP_API_KEY': 'api_key', 'MCP_SERVER_URL': 'server_url', 'MCP_HOST': 'host',
    'MCP_PORT': 'port', 'OAUTH_PASSWORD': 'oauth_password',
    'OAUTH_CLIENT_ID': 'oauth_client_id', 'OAUTH_CLIENT_SECRET': 'oauth_client_secret',
    'LIVESYNC_SYNC_ONLY_REGEX': 'selection_only', 'LIVESYNC_SYNC_IGNORE_REGEX': 'selection_ignore',
    'LIVESYNC_USE_IGNORE_FILES': 'selection_use_ignore_files',
    'LIVESYNC_IGNORE_FILES': 'selection_ignore_files',
}
CONFIG_PATHS.update({'COUCHDB_JWT_' + suffix: 'couchdb.jwt_' + attr for suffix, attr in (
    ('ALGORITHM', 'algorithm'), ('KEY', 'key'), ('SUBJECT', 'subject'),
    ('KEY_ID', 'key_id'), ('EXPIRY_MINUTES', 'expiry_minutes'))})
CONFIG_PATHS.update({'LIVESYNC_' + suffix: suffix.lower() for suffix in (
    'SYNC_MAX_SIZE_MB', 'DISABLE_CHECKING_CONFIG_MISMATCH', 'AUTO_ACCEPT_COMPATIBLE_TWEAK',
    'TEXT_VERDICT_CACHE_ENTRIES', 'TEXT_SCAN_MAX_BYTES', 'REGEX_MAX_OUTPUT_BYTES',
    'REGEX_TIMEOUT_SECONDS', 'SCAN_MAX_NOTES', 'SCAN_MAX_BYTES', 'SCAN_MAX_SECONDS')})
CONFIG_PATHS.update({'LIVESYNC_JOURNAL_' + item.name.upper(): 'replay_limits.' + item.name
                     for item in fields(ReplayLimits)})


def environment_rows(text=None):
    """Parse the actual four-column table, retaining requirement and purpose."""
    text = (ROOT / 'docs/configuration.md').read_text() if text is None else text
    pattern = r'^\| `(' + ENV_NAME + r')` \| (.*?) \| (.*?) \| (.*) \|$'
    rows = {name: (default, requirement, purpose)
            for name, default, requirement, purpose in re.findall(pattern, text, re.M)}
    assert set(rows) == environment_names()
    assert set(CONFIG_PATHS) | {'LIVESYNC_UNSAFE_DEBUG'} == set(rows)
    return rows


def controlled_environment(name):
    """Select a valid context which actually consumes this variable."""
    env = dict(LIVESYNC_BACKEND='couchdb', COUCHDB_USER='docs-user',
               COUCHDB_PASSWORD='docs-password', COUCHDB_DATABASE='docs-vault',
               MCP_API_KEY='docs-api-token')
    if name.startswith('S3_') or name == 'LIVESYNC_S3_URI':
        env = dict(LIVESYNC_BACKEND='s3', S3_ENDPOINT_URL='https://storage.example.com',
                   S3_ACCESS_KEY_ID='docs-access', S3_SECRET_ACCESS_KEY='docs-secret',
                   S3_BUCKET='docs-vault', MCP_API_KEY='docs-api-token')
    if name.startswith('COUCHDB_JWT_') and name != 'COUCHDB_JWT_ALGORITHM':
        env.pop('COUCHDB_USER')
        env.pop('COUCHDB_PASSWORD')
        env.update(COUCHDB_JWT_ALGORITHM='HS256',
                   COUCHDB_JWT_KEY='ZG9jcy10ZXN0LWtleQ==', COUCHDB_JWT_SUBJECT='docs-user')
    if name == 'MCP_API_KEY':
        env.update(OAUTH_PASSWORD='docs-password', OAUTH_CLIENT_ID='docs-client',
                   OAUTH_CLIENT_SECRET='docs-client-secret')
    return env


def configured_value(cfg, name):
    value = cfg
    for attr in CONFIG_PATHS[name].split('.'):
        value = getattr(value, attr)
    return value


def assert_documented_default(name, rows=None, loader=load_config):
    from config import ConfigError
    rows = environment_rows() if rows is None else rows
    default, requirement, _ = rows[name]
    env = controlled_environment(name)
    env.pop(name, None)
    if default == 'none':
        assert 'Required' in requirement, (name, requirement)
        with pytest.raises(ConfigError):
            loader(env)
        return
    actual = configured_value(loader(env), name)
    token = default.replace('`', '').split(' (', 1)[0]
    if token == 'empty':
        assert actual in ('', None, ()), name
    elif token in ('true', 'false'):
        assert actual is (token == 'true'), name
    elif re.fullmatch(r'\d+(?:\.\d+)?', token):
        assert type(actual) in (int, float) and actual == float(token), (name, default, actual)
    else:
        assert actual == token, (name, default, actual)


@pytest.mark.parametrize('name', sorted(CONFIG_PATHS))
def test_readme_default_matches_actual_consumer(name):
    assert_documented_default(name)


def test_documentation_contract_rejects_changed_default_and_table_mutations():
    """Exercise the checker in memory; never edit an imported production file."""
    import config
    text = documentation_text('README.md')
    rows = environment_rows(text)
    namespace = dict(config.__dict__)
    source = (ROOT / 'config.py').read_text()
    assert source.count("env.get('LIVESYNC_SCAN_MAX_SECONDS', 20)") == 1
    exec(compile(source.replace("env.get('LIVESYNC_SCAN_MAX_SECONDS', 20)",
                                "env.get('LIVESYNC_SCAN_MAX_SECONDS', 21)"),
                 '<in-memory config default mutation>', 'exec'), namespace)
    with pytest.raises(AssertionError):
        assert_documented_default('LIVESYNC_SCAN_MAX_SECONDS', rows, namespace['load_config'])
    broken = dict(rows)
    broken['MCP_PORT'] = ('`8485`', *rows['MCP_PORT'][1:])
    with pytest.raises(AssertionError):
        assert_documented_default('MCP_PORT', broken)
    broken = dict(rows)
    broken['COUCHDB_DATABASE'] = ('none', 'Optional', rows['COUCHDB_DATABASE'][2])
    with pytest.raises(AssertionError):
        assert_documented_default('COUCHDB_DATABASE', broken)
    removed = re.sub(r'^\| `MCP_PORT` .*\n', '', text, flags=re.M)
    with pytest.raises(AssertionError):
        environment_rows(removed)


def connection_contexts():
    basic = controlled_environment('MCP_PORT')
    jwt = controlled_environment('COUCHDB_JWT_KEY')
    s3 = controlled_environment('S3_BUCKET')
    uri = {key: value for key, value in s3.items() if not key.startswith('S3_')}
    uri['LIVESYNC_S3_URI'] = 'sls://docs-access:docs-secret@https://storage.example.com?bucket=docs-vault'
    oauth = {key: value for key, value in basic.items() if key != 'MCP_API_KEY'}
    oauth.update(OAUTH_PASSWORD='docs-password', OAUTH_CLIENT_ID='docs-client',
                 OAUTH_CLIENT_SECRET='docs-client-secret')
    return {'CouchDB Basic auth': basic, 'JWT': jwt, 'S3 fields': s3, 'S3 URI': uri,
            'OAuth': oauth}


def assert_documented_requirement(name, rows=None, loader=load_config):
    from config import ConfigError
    rows = environment_rows() if rows is None else rows
    _, requirement, _ = rows[name]
    failed_contexts = set()
    for context, original in connection_contexts().items():
        env = dict(original)
        env.pop(name, None)
        try:
            loader(env)
        except ConfigError:
            failed_contexts.add(context)
    if failed_contexts:
        assert requirement.startswith('Required') or requirement == 'Select JWT mode', (name, requirement)
    elif requirement.startswith('Required'):
        pytest.fail(f'{name}: README claims a requirement not enforced in any documented connection context')
    if requirement == 'Required':
        assert failed_contexts == set(connection_contexts()), name
    elif requirement.startswith('Required for CouchDB Basic auth'):
        assert failed_contexts == {'CouchDB Basic auth', 'OAuth'}, name
        assert 'absent for JWT' in requirement
    elif requirement == 'Required for CouchDB':
        assert failed_contexts == {'CouchDB Basic auth', 'JWT', 'OAuth'}, name
    elif requirement == 'Required for JWT' or requirement == 'Select JWT mode':
        assert failed_contexts == {'JWT'}, name
    elif requirement == 'Required for S3 fields':
        assert failed_contexts == {'S3 fields'}, name
    elif requirement == 'Required for OAuth':
        assert failed_contexts == {'OAuth'}, name
    elif requirement == 'Required unless full OAuth group set':
        assert failed_contexts == {'CouchDB Basic auth', 'JWT', 'S3 fields', 'S3 URI'}, name
    elif requirement == 'Required unless S3 fields set':
        assert failed_contexts == {'S3 URI'}, name
    else:
        assert not failed_contexts, (name, requirement, failed_contexts)


@pytest.mark.parametrize('name', sorted(CONFIG_PATHS))
def test_readme_requirement_matches_actual_connection_contexts(name):
    assert_documented_requirement(name)


@pytest.mark.parametrize('name', sorted(CONFIG_PATHS))
def test_readme_empty_value_contract(name):
    from config import ConfigError
    default, requirement, purpose = environment_rows()[name]
    env = controlled_environment(name)
    env[name] = ''
    description = (requirement + ' ' + purpose.split(';', 1)[0]).lower().replace('non-negative', 'nonnegative')
    numeric = 'integer' in description or 'finite' in description
    empty_allowed = ('or empty' in description or 'empty/unset' in description or
                     name in ('S3_REFRESH_MIN_INTERVAL_SEC', 'S3_STATE_SIZE_WARN_MB'))
    rejected = default == 'none' or ('boolean' in description and 'optional' not in description)
    rejected |= numeric and not empty_allowed
    rejected |= name == 'S3_FORCE_PATH_STYLE'
    if rejected:
        with pytest.raises(ConfigError):
            load_config(env)
    else:
        cfg = load_config(env)
        assert cfg is not None


def numeric_rows():
    for name, row in environment_rows().items():
        description = ' '.join(row[1:]).lower().replace('non-negative', 'nonnegative')
        if ('integer' in description or 'finite' in description) and name != 'MCP_PORT':
            yield name


@pytest.mark.parametrize('name', list(numeric_rows()))
@pytest.mark.parametrize('raw', ['-1', '0', '1', '0.5', 'NaN', 'Infinity', str(2**63 - 1), str(2**63)])
def test_readme_numeric_rule_matches_runtime_boundaries(name, raw):
    from config import ConfigError
    _, requirement, purpose = environment_rows()[name]
    description = (requirement + ' ' + purpose.split(';', 1)[0]).lower().replace('non-negative', 'nonnegative')
    import math
    number = float(raw)
    accepted = math.isfinite(number)
    if 'integer' in description:
        accepted &= bool(re.fullmatch(r'-?\d+', raw))
    if 'nonnegative' in description:
        accepted &= number >= 0
    elif 'positive' in description:
        accepted &= number > 0
    if '2**63' in description:
        # Integer parsing must retain exact boundaries, unlike binary floats.
        exact = int(raw) if re.fullmatch(r'-?\d+', raw) else number
        accepted &= exact < 2**63
    env = {**controlled_environment(name), name: raw}
    if accepted:
        load_config(env)
    else:
        with pytest.raises(ConfigError):
            load_config(env)


@pytest.mark.parametrize('raw', ['0', '1', '65535', '65536', '-1', '0.5', 'NaN'])
def test_readme_port_range_matches_runtime(raw):
    from config import ConfigError
    text = ' '.join(environment_rows()['MCP_PORT'][1:])
    match = re.search(r'integer (\d+)[–-](\d+)', text)
    assert match, 'README must state both inclusive integer port boundaries'
    low, high = map(int, match.groups())
    accepted = raw.isdigit() and low <= int(raw) <= high
    if accepted:
        load_config({**controlled_environment('MCP_PORT'), 'MCP_PORT': raw})
    else:
        with pytest.raises(ConfigError):
            load_config({**controlled_environment('MCP_PORT'), 'MCP_PORT': raw})


def test_readme_connection_exclusivity_is_executable():
    from config import ConfigError
    text = documentation_text('README.md')
    assert 'URI and individual fields are mutually exclusive' in text
    contexts = connection_contexts()
    for name in ('S3_ENDPOINT_URL', 'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY',
                 'S3_BUCKET', 'S3_REGION', 'S3_PREFIX'):
        for value in ('', contexts['S3 fields'].get(name, 'test')):
            with pytest.raises(ConfigError, match='conflicts'):
                load_config({**contexts['S3 URI'], name: value})
    for name in ('COUCHDB_USER', 'COUCHDB_PASSWORD'):
        with pytest.raises(ConfigError, match='conflicts'):
            load_config({**contexts['JWT'], name: ''})
    oauth = contexts['OAuth']
    cfg = load_config({**oauth, 'MCP_API_KEY': 'docs-api-token'})
    assert cfg.api_key == 'docs-api-token'
    assert 'takes precedence over OAuth' in environment_rows()['MCP_API_KEY'][2]


@pytest.mark.parametrize('value', [None, '', '0', 'false', 'true', '1'])
@pytest.mark.parametrize('enabled', [False, True])
def test_readme_separate_debug_switch_matches_actual_sink(monkeypatch, value, enabled):
    import diagnostic_hygiene
    import logging
    row = environment_rows()['LIVESYNC_UNSAFE_DEBUG']
    assert row[0] == '`unset`'
    assert row[1] == 'Set to 1 plus DEBUG logging'
    if value is None:
        monkeypatch.delenv('LIVESYNC_UNSAFE_DEBUG', raising=False)
    else:
        monkeypatch.setenv('LIVESYNC_UNSAFE_DEBUG', value)
    class Sink:
        def __init__(self):
            self.records = []
        def isEnabledFor(self, level):
            assert level == logging.DEBUG
            return enabled
        def debug(self, *args):
            self.records.append(args)
    sink = Sink()
    diagnostic_hygiene.unsafe_debug(sink, 'synthetic private text')
    assert bool(sink.records) == (value == '1' and enabled)
    assert 'synthetic private text' not in str(sink.records)


@pytest.mark.parametrize('name', sorted(CONFIG_PATHS))
def test_readme_variable_is_actually_consumed(name):
    """A documented variable must change the returned config, not just parse."""
    env = controlled_environment(name)
    if name == 'LIVESYNC_BACKEND':
        env = connection_contexts()['S3 fields']
        raw, expected = 's3', 's3'
    elif name == 'LIVESYNC_S3_URI':
        env = connection_contexts()['S3 URI']
        raw = 'sls://docs-access:docs-secret@https://storage.example.com?bucket=another-vault&prefix=exact/'
        expected = raw
    elif name == 'COUCHDB_JWT_ALGORITHM':
        env = connection_contexts()['JWT']
        raw, expected = 'HS512', 'HS512'
    elif name.endswith('CUSTOM_HEADERS'):
        raw, expected = '{"X-Doc-Probe":"synthetic-value"}', (('X-Doc-Probe', 'synthetic-value'),)
    elif name == 'COUCHDB_JWT_KEY':
        raw = expected = 'YW5vdGhlci1zeW50aGV0aWMta2V5'
    elif name == 'LIVESYNC_IGNORE_FILES':
        raw, expected = ' .gitignore , rules.txt ', ('.gitignore', 'rules.txt')
    else:
        description = ' '.join(environment_rows()[name][1:]).lower()
        current = configured_value(load_config(env), name)
        if type(current) is bool or name == 'LIVESYNC_USE_IGNORE_FILES':
            expected = current is not True
            raw = str(expected).lower()
        elif 'integer' in description or 'finite' in description:
            raw, expected = '13', 13
        else:
            raw = expected = 'docs-probe-value'
    cfg = load_config({**env, name: raw})
    assert configured_value(cfg, name) == expected, name


def test_documentation_contract_rejects_removed_variable_consumer(monkeypatch):
    import config
    source = (ROOT / 'config.py').read_text()
    old = 'raw = env.get(key, default)'
    assert source.count(old) == 1
    namespace = dict(config.__dict__)
    exec(compile(source.replace(old,
        'raw = default if key == "LIVESYNC_SCAN_MAX_NOTES" else env.get(key, default)'),
        '<in-memory removed environment consumer>', 'exec'), namespace)
    monkeypatch.setitem(globals(), 'load_config', namespace['load_config'])
    with pytest.raises(AssertionError):
        test_readme_variable_is_actually_consumed('LIVESYNC_SCAN_MAX_NOTES')


def test_readme_region_default_distinguishes_connection_forms():
    cell = environment_rows()['S3_REGION'][0]
    assert '(fields)' in cell and '; empty if omitted in URI' in cell
    env = connection_contexts()['S3 URI']
    assert load_config(env).s3.region == ''


@pytest.mark.parametrize('name', [name for name in CONFIG_PATHS
    if type(configured_value(load_config(controlled_environment(name)), name)) is bool
    or name == 'LIVESYNC_USE_IGNORE_FILES'])
def test_readme_boolean_variables_refuse_other_strings(name):
    from config import ConfigError
    # A boolean control must not accidentally accept arbitrary truthy strings.
    with pytest.raises(ConfigError, match=name):
        load_config({**controlled_environment(name), name: 'synthetic-invalid-boolean'})


def test_complete_generator_catalogue_and_acceptance_package():
    guide = (ROOT / 'docs/regenerating-oracle-fixtures.md').read_text()
    scripts = sorted((ROOT / 'scripts').glob('extract_*.mjs'))
    scripts += sorted((ROOT / 'scripts').glob('probe_*.mjs'))
    scripts += [ROOT / 'scripts/vendor_selection.py']
    for script in scripts:
        assert script.name in guide, script.name
    for path in ('AGENTS.md', 'docs/upstream-pin.md'):
        assert 'regenerating-oracle-fixtures.md' in (ROOT / path).read_text()
    pin = (ROOT / 'docs/upstream-pin.md').read_text()
    assert 'real-server\nautomated acceptance done' in pin
    assert 'Live Obsidian acceptance: **IN PROGRESS**' in pin
    assert 'final live acceptance pending' in pin
    assert '2026-11-25-parity-docker/summary.txt' in pin
    readme = documentation_text('README.md')
    assert 'create_app(cfg)' not in readme
    for setting in ('deleteMetadataOfDeletedFiles', 'automaticallyDeleteMetadataOfDeletedFiles',
                    'disableMarkdownAutoMerge', 'resolveConflictsByNewerFile',
                    'writeDocumentsIfConflicted', 'processSizeMismatchedFiles',
                    'doNotDeleteFolder', 'trashInsteadDelete', 'doNotUseFixedRevisionForChunks'):
        assert setting in readme
        assert setting in (ROOT / 'docs/peer-limitations.md').read_text()
    changelog = (ROOT / 'CHANGELOG.md').read_text().split('## [1.0.1]', 1)[0]
    assert 'JWT auth/compression adapters are absent' not in changelog
    assert 'not inferred from milestone recommendations' not in changelog
    assert 'Reassemble CouchDB split pairs losslessly' not in changelog
    checklist = (ROOT / 'docs/live-check-parity-checklist.md').read_text()
    assert 'Sync with a saved connection' in checklist  # C:remoteConfig.ts:458-470.
    assert 'Select a remote to replicate with' in checklist  # C:remoteConfig.ts:397.
    assert 'Maximum file size' in checklist  # C:common/settingConstants.ts:207-211.
    for line in checklist.splitlines():
        if 'scripts/live_check_parity.py' not in line:
            continue
        if any(f' {command} ' in line for command in ('write-large', 'write-phase', 'oversize')):
            assert '--allow-remote-write' in line and '--confirm-target' in line
        if any(f' {command} ' in line for command in ('write-phase', 'oversize')):
            assert '--allow-remote-delete' in line


def test_documentation_enum_checker_catches_wrong_jwt_algorithm():
    rows = environment_rows()
    broken = dict(rows)
    default, requirement, purpose = broken['COUCHDB_JWT_ALGORITHM']
    assert 'HS512' in purpose
    broken['COUCHDB_JWT_ALGORITHM'] = (default, requirement, purpose.replace('HS512', 'RS256'))
    with pytest.raises(AssertionError):
        assert_documented_enum('COUCHDB_JWT_ALGORITHM', broken)


def assert_documented_enum(name, rows=None):
    from config import BACKENDS, ConfigError
    from couchdb_jwt import ALGORITHMS
    rows = environment_rows() if rows is None else rows
    purpose = rows[name][2]
    if name == 'LIVESYNC_BACKEND':
        documented = set(re.findall(r'`([a-z0-9_-]+)`', purpose))
        inventory = set(BACKENDS)
    else:
        documented = set(re.findall(r'\b[A-Z]{2}\d{3}\b', purpose))
        inventory = set(ALGORITHMS)
    assert documented == inventory, (name, documented, inventory)
    for value in documented | {'synthetic-invalid-choice'}:
        env = controlled_environment('S3_BUCKET' if value == 's3' else 'MCP_PORT')
        if name == 'COUCHDB_JWT_ALGORITHM':
            env.pop('COUCHDB_USER'); env.pop('COUCHDB_PASSWORD')
            key = 'ZG9jcy10ZXN0LWtleQ=='
            if value.startswith('ES'):
                from cryptography.hazmat.primitives.asymmetric import ec
                from cryptography.hazmat.primitives import serialization
                curve = ec.SECP256R1() if value == 'ES256' else ec.SECP521R1()
                key = ec.generate_private_key(curve).private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption()).decode()
            env.update(COUCHDB_JWT_KEY=key, COUCHDB_JWT_SUBJECT='docs-user')
        env[name] = value
        if value in documented:
            assert configured_value(load_config(env), name) == value
        else:
            with pytest.raises(ConfigError):
                load_config(env)


@pytest.mark.parametrize('name', ['LIVESYNC_BACKEND', 'COUCHDB_JWT_ALGORITHM'])
def test_readme_enum_choices_match_actual_configuration(name):
    assert_documented_enum(name)


def test_readme_names_terminating_binary_data_loss_refusal():
    # C:string_and_binary/chunks.ts:418-432 native slice(i,NaN) yields no bytes.
    text = documentation_text('README.md')
    row = next(line for line in text.splitlines() if line.startswith('| `customChunkSize` |'))
    assert 'data loss' in row
    assert 'NaN-cap V2 binary' in text
    assert 'Refuse only inputs on which the plugin itself does not terminate' not in text


def test_f6_transport_policy_documentation():
    names = ('COUCHDB_CONNECT_TIMEOUT_SECONDS', 'COUCHDB_IO_TIMEOUT_SECONDS',
             'S3_CONNECT_TIMEOUT_SECONDS', 'S3_READ_TIMEOUT_SECONDS',
             'S3_MAX_ATTEMPTS', 'LIVESYNC_SELECTION_TIMEOUT_SECONDS')
    for name in names:
        assert name in environment_names()
    s3 = (ROOT / 'docs/s3-journal.md').read_text()
    assert 'Always one' in s3 and '412' in s3 and 'Outcome unknown' in s3
    assert 'LIVESYNC_SELECTION_TIMEOUT_SECONDS' in (ROOT / 'docs/selection-filters.md').read_text()
    assert 'COUCHDB_IO_TIMEOUT_SECONDS' in documentation_text('docs/connecting-storage.md')


def test_f6_frontmatter_contract_documented():
    assert 'ruamel.yaml==0.18.17' in (ROOT / 'requirements.txt').read_text().splitlines()
    guide = (ROOT / 'docs/frontmatter-properties.md').read_text()
    for name in ('LIVESYNC_FRONTMATTER_MAX_NODES', 'LIVESYNC_FRONTMATTER_MAX_DEPTH', 'LIVESYNC_FRONTMATTER_MAX_BYTES'):
        assert name in guide and name in environment_names()
    assert 'alias expansion' in guide and 'raw note text' in guide
    assert '4 GiB' in guide and '1723285504' in guide
    assert '2026-11-25 — F6 operational limits' in (ROOT / 'docs/superpowers/specs/2026-11-23-settings-parity.md').read_text()
    assert 'frontmatter_admission_upstream.json' in (ROOT / 'AGENTS.md').read_text()
    assert 'extract_frontmatter_admission_vectors.mjs' in (ROOT / 'docs/regenerating-oracle-fixtures.md').read_text()


def test_empty_bucket_initialisation_precedes_owner_acceptance():
    # C:remoteConfig.ts:388-423, JournalSyncCore.ts:347-352;
    # P:PaneMaintenance.ts:110-139; combinedMessages.prod.ts:7538-7541,
    # 7912-7917,9394-9399,9773-9778; redFlag.ts:518.
    text = (ROOT / 'docs/live-check-parity-checklist.md').read_text()
    assert text.index('**0. Однократная инициализация') < text.index('## 2. Повторное подключение (A)')
    for phrase in ('No Synchronisation Settings Found', "Use this device's settings",
                   'Overwrite Server Data with This Device\'s Files', 'остаются английскими',
                   'Rebuild everything operation completed.', 'не отправляет файлы повторно',
                   'Запланировать и перезапустить'):
        assert phrase in text


def test_practical_docs_have_single_inventory_and_recipe_order():
    readme = (ROOT / 'README.md').read_text()
    assert 180 <= len(readme.splitlines()) <= 230
    assert 'docs/configuration.md' in readme
    assert '## Environment reference' in (ROOT / 'docs/configuration.md').read_text()
    connection = (ROOT / 'docs/connecting-storage.md').read_text()
    assert connection.index('## CouchDB') < connection.index('**S3 individual fields:**') < connection.index('## CouchDB JWT authentication')
    assert 'authentication_handlers' not in connection
    assert '## Automated real-server acceptance' not in connection
    assert 'couchdb-jwt.md' in connection and 'developer-acceptance.md' in connection


def test_selection_deadline_override_and_historical_scan_links():
    text = (ROOT / 'docs/selection-filters.md').read_text()
    assert 'configurable with `LIVESYNC_SELECTION_TIMEOUT_SECONDS`' in text
    for name in ('2026-11-17-universal-mcp-foundation-couchdb.md', '2026-11-19-s3-journal-peer.md'):
        text = (ROOT / 'docs/superpowers/plans' / name).read_text()
        assert '../../scan-policy.md#text-scan-eligibility' in text
        assert 'README.md#text-scan-eligibility' not in text


def test_http_body_policy_documentation():
    for name in ("README.md", "docs/configuration.md", "docs/peer-limitations.md", ".env.example"):
        assert "LIVESYNC_MAX_REQUEST_BYTES" in (ROOT / name).read_text()
    text = (ROOT / "docs/configuration.md").read_text()
    for term in ("1 GiB", "68 MiB", "held in memory", "HTTP 413", "OAuth", "0 (unlimited"):
        assert term in text


def test_hosting_log_policy_documented():
    for name in ("README.md", "docs/diagnostics-and-secrets.md"):
        text = (ROOT / name).read_text()
        assert "hosting boundary" in text and "Content-Type, Host and Origin" in text
    assert "hosting_logging.py" in (ROOT / "AGENTS.md").read_text()


def test_couchdb_inflate_policy_documented():
    for name in ("README.md", "docs/configuration.md", "docs/peer-limitations.md", "docs/vault-format.md", ".env.example"):
        assert "LIVESYNC_COUCHDB_MAX_INFLATED_BYTES" in (ROOT / name).read_text()
    text = (ROOT / "docs/vault-format.md").read_text()
    assert "32 MiB" in text and "safety factor" in text and "P-4" in text


def test_transport_owned_headers_documented():
    for name in ("README.md", "docs/connecting-storage.md", "docs/peer-limitations.md", ".env.example"):
        text = (ROOT / name).read_text()
        for field in ("Content-Length", "Transfer-Encoding", "Host", "Proxy-*"):
            assert field in text


def test_selection_installed_provenance_documented():
    for name in ("AGENTS.md", "docs/regenerating-oracle-fixtures.md", "docs/selection-filters.md"):
        assert "installed-sha256.json" in (ROOT / name).read_text()


def test_reproducible_install_and_fixed_runner_are_documented():
    lock = (ROOT / 'requirements.lock').read_text()
    assert '# Produced by: ./venv/bin/python -m pip freeze' in lock
    assert '# Reproduce: python -m pip install -r requirements.lock' in lock
    assert 'python -m pip install -r requirements.lock' in (ROOT / 'AGENTS.md').read_text()
    workflow = (ROOT / '.github/workflows/upstream-watch.yml').read_text()
    assert 'runs-on: ubuntu-24.04' in workflow
    assert 'ubuntu-latest' not in workflow
    assert not re.search(r'uses:.*@(main|master|latest|v\d+)\s*$', workflow, re.M)


def test_settings_changelog_is_operator_facing():
    text = (ROOT / 'CHANGELOG.md').read_text().split('## [1.0.1]', 1)[0]
    assert text.count('### Settings parity') == 1
    assert 'scheduler' not in text.lower()
    assert not re.search(r'\bgates?\b', text.lower())
    assert 'test margins' not in text.lower()
    for term in ('LIVESYNC_MAX_REQUEST_BYTES', 'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES',
                 'custom headers', 'transport_replacements', 'Node', 'E2EE'):
        assert term in text


def test_current_live_acceptance_status_is_explicit():
    for name in ('docs/upstream-pin.md', 'docs/live-check-parity-checklist.md'):
        text = (ROOT / name).read_text()
        for term in ('DONE', 'IN PROGRESS', '1.0.50', '2026-11-25',
                     '2026-11-25-parity-docker/summary.txt'):
            assert term in text
        assert 'final live acceptance pending' in text
        assert '1.0.52 has not been exercised live' in text


def test_couchdb_http_decoding_policy_documented():
    for name in ('docs/peer-limitations.md', 'docs/vault-format.md'):
        text = (ROOT / name).read_text()
        for required in ('Accept-Encoding: identity', 'wire', 'decoded',
                         'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES', '_bulk_docs'):
            assert required in text
    assert 'HTTP' in (ROOT / '.env.example').read_text().split('LIVESYNC_COUCHDB_MAX_INFLATED_BYTES=')[0][-250:]


def test_changelog_distinguishes_vault_ignore_files_from_local_regex():
    # C:common/models/tweak.definition.ts:116-151 milestone tweak fields omit
    # syncOnlyRegEx/syncIgnoreRegEx; useIgnoreFiles/ignoreFiles are published.
    text = (ROOT / 'CHANGELOG.md').read_text().split('### Settings parity', 1)[1]
    text = text.split('\n## ', 1)[0]
    assert 'syncOnlyRegEx' in text and 'syncIgnoreRegEx' in text
    assert 'operator environment settings' in text
    assert 'Derive allow/ignore regex' not in text
