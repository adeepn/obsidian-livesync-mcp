"""Enforce P-19/P-21/P-22 at validation and the actual startup boundary.

These are peer security-policy assertions, not plugin wire-format oracles.
Upstream commonlib replication/httplib.ts:68-101,110-113 constrains JWT
key/identifier encodings; common/utils.ts:459-469 parses custom headers.
No upstream behaviour or fixture is changed by omitting local error echoes.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from config import ConfigError, load_config
from tests.test_config import COUCHDB_ENV, S3_INDIVIDUAL_ENV, S3_ENV
from tests.test_documentation_config import environment_names

MARKER = 'SYNTHETIC_PRIVATE_VALUE_0123456789'
JWT_ENV = {k: v for k, v in COUCHDB_ENV.items()
           if k not in ('COUCHDB_USER', 'COUCHDB_PASSWORD')}
JWT_ENV.update(COUCHDB_JWT_ALGORITHM='HS256', COUCHDB_JWT_KEY='dGVzdC1rZXk=',
               COUCHDB_JWT_SUBJECT='test-user')

# Explicit inventory: adding a consumer requires classifying its validation.
INVALID_CASES = [
    *[(name, S3_ENV if name.startswith('S3_') else COUCHDB_ENV, MARKER,
       'positive integer' if name == 'S3_MAX_ATTEMPTS' else 'positive and finite')
      for name in ('COUCHDB_CONNECT_TIMEOUT_SECONDS', 'COUCHDB_IO_TIMEOUT_SECONDS',
                   'S3_CONNECT_TIMEOUT_SECONDS', 'S3_READ_TIMEOUT_SECONDS',
                   'S3_MAX_ATTEMPTS', 'LIVESYNC_SELECTION_TIMEOUT_SECONDS')],
    *[(name, COUCHDB_ENV, MARKER, 'positive integer') for name in
      ('LIVESYNC_FRONTMATTER_MAX_NODES', 'LIVESYNC_FRONTMATTER_MAX_DEPTH')],
    ('LIVESYNC_BACKEND', COUCHDB_ENV, MARKER, "'couchdb' or 's3'"),
    ('MCP_PORT', COUCHDB_ENV, MARKER, 'integer'),
    *[(name, COUCHDB_ENV, MARKER, 'true or false') for name in (
        'LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH',
        'LIVESYNC_AUTO_ACCEPT_COMPATIBLE_TWEAK', 'LIVESYNC_USE_IGNORE_FILES')],
    ('LIVESYNC_SYNC_MAX_SIZE_MB', COUCHDB_ENV, MARKER, 'finite number or empty'),
    *[(name, COUCHDB_ENV, MARKER, rule) for name, rule in (
        ('LIVESYNC_SCAN_MAX_NOTES', 'nonnegative integer'),
        ('LIVESYNC_SCAN_MAX_BYTES', 'nonnegative integer'),
        ('LIVESYNC_TEXT_SCAN_MAX_BYTES', 'nonnegative integer or empty'),
        ('LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES', 'positive integer'),
        ('LIVESYNC_REGEX_MAX_OUTPUT_BYTES', 'nonnegative integer'),
        ('LIVESYNC_REGEX_TIMEOUT_SECONDS', 'positive and finite'),
        ('LIVESYNC_SCAN_MAX_SECONDS', 'nonnegative and finite'))],
    *[('LIVESYNC_JOURNAL_' + suffix, COUCHDB_ENV, MARKER,
       'positive finite number below 2**63' if suffix == 'MAX_REFRESH_SECONDS'
       else 'positive integer below 2**63') for suffix in (
        'MAX_LINE_BYTES', 'MAX_INFLATED_BYTES', 'MAX_RECORDS', 'MAX_JSON_DEPTH',
        'MAX_OBJECT_BYTES', 'MAX_REFRESH_KEYS', 'MAX_REFRESH_BYTES', 'MAX_REFRESH_SECONDS')],
    ('S3_REFRESH_MIN_INTERVAL_SEC', S3_ENV, MARKER, 'positive finite number below 2**63'),
    ('S3_STATE_SIZE_WARN_MB', S3_ENV, MARKER, 'non-negative integer'),
    ('S3_FORCE_PATH_STYLE', S3_ENV, MARKER, 'true or false'),
    ('LIVESYNC_S3_URI', S3_ENV, MARKER, "start with 'sls://'"),
    *[(name, env, '{"X-Test": "' + MARKER + '\\u000a"}',
       'must not contain control characters') for name, env in (
        ('COUCHDB_CUSTOM_HEADERS', COUCHDB_ENV), ('S3_CUSTOM_HEADERS', S3_ENV))],
    ('COUCHDB_JWT_ALGORITHM', JWT_ENV, MARKER, 'HS256, HS512, ES256 or ES512'),
    ('COUCHDB_JWT_KEY', JWT_ENV, MARKER, 'base64 HMAC bytes'),
    ('COUCHDB_JWT_EXPIRY_MINUTES', JWT_ENV, MARKER, 'nonnegative finite number'),
    *[(name, JWT_ENV, MARKER + '\u0100', 'Latin-1') for name in (
        'COUCHDB_JWT_SUBJECT', 'COUCHDB_JWT_KEY_ID')],
    # Arbitrary strings are valid credentials. Use the real, synthetic template
    # sentinels to exercise their only content validation, without new limits.
    *[(name, env, marker, 'placeholder') for name, env, marker in (
        ('COUCHDB_PASSWORD', COUCHDB_ENV, 'your_password'),
        ('MCP_API_KEY', COUCHDB_ENV, 'your_api_key'),
        ('OAUTH_PASSWORD', COUCHDB_ENV, 'your_oauth_password'),
        ('OAUTH_CLIENT_ID', COUCHDB_ENV, 'your_client_id'),
        ('OAUTH_CLIENT_SECRET', COUCHDB_ENV, 'your_client_secret'),
        ('S3_ACCESS_KEY_ID', S3_INDIVIDUAL_ENV, 'your_access_key'),
        ('S3_SECRET_ACCESS_KEY', S3_INDIVIDUAL_ENV, 'your_secret_key'))],
]

# Presence-only, pass-through, or optional strings have no invalid nonempty
# marker. Keep accepting them; do not invent restrictions for this audit.
PASSTHROUGH_CASES = [
    *[(name, COUCHDB_ENV) for name in (
        'COUCHDB_URL', 'COUCHDB_USER', 'COUCHDB_DATABASE', 'MCP_SERVER_URL',
        'MCP_HOST', 'LIVESYNC_SYNC_ONLY_REGEX', 'LIVESYNC_SYNC_IGNORE_REGEX',
        'LIVESYNC_IGNORE_FILES', 'LIVESYNC_UNSAFE_DEBUG')],
    *[(name, S3_INDIVIDUAL_ENV) for name in (
        'S3_ENDPOINT_URL', 'S3_BUCKET', 'S3_REGION', 'S3_PREFIX', 'S3_STATE_DIR')],
]


def test_configuration_validation_inventory_is_exhaustive():
    assert {row[0] for row in INVALID_CASES + PASSTHROUGH_CASES} == environment_names()


@pytest.mark.parametrize('name,base,value,rule', INVALID_CASES, ids=[r[0] for r in INVALID_CASES])
def test_config_error_names_rule_without_supplied_value(name, base, value, rule):
    with pytest.raises(ConfigError) as caught:
        load_config({**base, name: value})
    message = str(caught.value)
    assert name in message
    assert rule in message
    assert value not in message
    assert MARKER not in message


@pytest.mark.parametrize('name,base', PASSTHROUGH_CASES, ids=[r[0] for r in PASSTHROUGH_CASES])
def test_unrestricted_strings_remain_accepted(name, base):
    load_config({**base, name: MARKER})


@pytest.mark.parametrize('entrypoint', ['run.py', 'server.py'])
@pytest.mark.parametrize('name', ['MCP_PORT', 'LIVESYNC_BACKEND'])
def test_real_startup_never_prints_invalid_value(entrypoint, name):
    # No inherited credentials, Python startup hooks or dotenv file loading.
    env = {'PATH': os.defpath, 'PYTHON_DOTENV_DISABLED': '1',
           **COUCHDB_ENV, name: MARKER}
    result = subprocess.run([sys.executable, entrypoint], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert 'Configuration error:' in result.stderr
    assert name in result.stderr
    assert MARKER not in result.stdout
    assert MARKER not in result.stderr


def test_replay_config_bridge_uses_public_fields_not_exception_text(monkeypatch):
    from replay_limits import ReplayLimits

    with pytest.raises(ValueError) as caught:
        ReplayLimits.from_env({'LIVESYNC_JOURNAL_MAX_RECORDS': MARKER})
    error = caught.value
    assert error.public_message == 'LIVESYNC_JOURNAL_MAX_RECORDS must be a positive integer below 2**63'
    # A formatter must never become the public input to ConfigError.
    monkeypatch.setattr(type(error), '__str__', lambda self: MARKER)

    def fail(env):
        raise error

    monkeypatch.setattr(ReplayLimits, 'from_env', fail)
    with pytest.raises(ConfigError) as public:
        load_config(COUCHDB_ENV)
    assert str(public.value) == error.public_message
    assert MARKER not in str(public.value)
