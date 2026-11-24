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
ENV_NAME = r'(?:LIVESYNC|COUCHDB|S3|MCP|OAUTH)_[A-Z0-9_]+'


def environment_names():
    # Include the separate render-time debug consumer, deriving its spelling
    # from actual source instead of hardcoding a second inventory.
    trees = [ast.parse((ROOT / name).read_text()) for name in ('config.py', 'diagnostic_hygiene.py')]
    names = {node.value for tree in trees for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and re.fullmatch(ENV_NAME, node.value) and not node.value.endswith('_')}
    names |= {'LIVESYNC_JOURNAL_' + field.name.upper() for field in fields(ReplayLimits)}
    return names


def test_environment_inventory_matches_readme_and_template():
    actual = environment_names()
    readme = set(re.findall(r'^\| `(' + ENV_NAME + r')` \|',
                            (ROOT / 'README.md').read_text(), re.M))
    template = set(re.findall(r'^(?:# )?(' + ENV_NAME + r')=',
                              (ROOT / '.env.example').read_text(), re.M))
    assert readme == actual
    inline_names = set(re.findall(r'\b(' + ENV_NAME + r')\b', (ROOT / 'README.md').read_text()))
    assert inline_names <= actual
    assert template == actual


def blocks():
    for path in [ROOT / 'README.md', ROOT / 'docs/connecting-storage.md']:
        for index, block in enumerate(re.findall(r'```dotenv\n(.*?)```', path.read_text(), re.S)):
            yield f'{path.name}-{index}', block


@pytest.mark.parametrize('name,block', list(blocks()))
def test_documented_connection_block_loads(name, block):
    # The docs explicitly require replacing <...>. These replacements are
    # generated only in process, never credentials copied from a real vault.
    values = {
        '<paste-generated-token>': secrets.token_urlsafe(32),
        '<CouchDB-server-URI>': 'http://127.0.0.1:5984',
        '<plugin-username>': 'local-test-user',
        '<plugin-password>': secrets.token_urlsafe(32),
        '<plugin-database-name>': 'local-test-vault',
        '<paste-sls://-setup-URI>': 'sls://testkey:testsecret@http://127.0.0.1:19000?bucket=plan-b-it&region=us-east-1&prefix=livetest/',
        '<plugin-endpoint-URL>': 'http://127.0.0.1:19000',
        '<plugin-access-key>': 'testkey',
        '<plugin-secret-key>': 'testsecret',
        '<plugin-bucket>': 'plan-b-it',
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
        text = (ROOT / name).read_text()
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
    readme = (ROOT / 'README.md').read_text()
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
    readme = (ROOT / 'README.md').read_text()
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
    readme = (ROOT / 'README.md').read_text()
    focused = (ROOT / 'docs/vault-format.md').read_text()
    assert 'Nonpositive legacy piece caps and V2 plain-text minimums are unsupported' not in readme
    assert 'plugin itself does not terminate' in readme
    for name in ('customChunkSize', 'minimumChunkSize'):
        assert f'| `{name}` |' in readme
        assert name in focused and name in (ROOT / '.env.example').read_text()
    for term in ('864', 'UTF-16', 'single code point', 'null', 'NaN', 'Recovery'):
        assert term in focused
