"""Tests for strict environment configuration (config.load_config)."""

import pytest

from config import ConfigError, load_config, parse_s3_uri

COUCHDB_ENV = {
    "LIVESYNC_BACKEND": "couchdb",
    "COUCHDB_URL": "http://localhost:5984",
    "COUCHDB_USER": "admin",
    "COUCHDB_PASSWORD": "secret-password",
    "COUCHDB_DATABASE": "obsidian",
    "MCP_API_KEY": "real-api-key",
}


def test_backend_required():
    with pytest.raises(ConfigError, match="LIVESYNC_BACKEND"):
        load_config({})


def test_backend_unknown():
    env = {**COUCHDB_ENV, "LIVESYNC_BACKEND": "foo"}
    with pytest.raises(ConfigError, match="LIVESYNC_BACKEND"):
        load_config(env)


def test_couchdb_complete():
    config = load_config(COUCHDB_ENV)

    assert config.backend == "couchdb"
    assert config.couchdb is not None
    assert config.couchdb.url == "http://localhost:5984"
    assert config.couchdb.user == "admin"
    assert config.couchdb.password == "secret-password"
    assert config.couchdb.database == "obsidian"
    assert config.api_key == "real-api-key"


def test_couchdb_missing_creds():
    env = {k: v for k, v in COUCHDB_ENV.items() if k != "COUCHDB_PASSWORD"}
    with pytest.raises(ConfigError, match="COUCHDB_PASSWORD"):
        load_config(env)


def test_couchdb_url_defaults_to_standard_port():
    # Audit A4: the in-code default must be the standard CouchDB port
    # 5984, matching README and .env.example (never 5443 again).
    env = {k: v for k, v in COUCHDB_ENV.items() if k != "COUCHDB_URL"}
    config = load_config(env)
    assert config.couchdb.url == "http://localhost:5984"

    env_blank = {**COUCHDB_ENV, "COUCHDB_URL": ""}
    assert load_config(env_blank).couchdb.url == "http://localhost:5984"


def test_couchdb_user_password_database_have_no_default():
    for name in ("COUCHDB_USER", "COUCHDB_PASSWORD", "COUCHDB_DATABASE"):
        env = {k: v for k, v in COUCHDB_ENV.items() if k != name}
        with pytest.raises(ConfigError, match=name):
            load_config(env)


def test_placeholder_api_key_rejected():
    env = {**COUCHDB_ENV, "MCP_API_KEY": "your_api_key"}
    with pytest.raises(ConfigError, match="MCP_API_KEY"):
        load_config(env)


def test_no_auth_rejected():
    env = {
        "LIVESYNC_BACKEND": "couchdb",
        "COUCHDB_URL": "http://localhost:5984",
        "COUCHDB_USER": "admin",
        "COUCHDB_PASSWORD": "secret-password",
        "COUCHDB_DATABASE": "obsidian",
    }
    with pytest.raises(ConfigError, match="MCP_API_KEY|OAUTH_PASSWORD"):
        load_config(env)


def test_s3_uri_parsed():
    s3 = parse_s3_uri("sls://AK:SK@https://minio.local?bucket=vault&region=auto&prefix=")

    assert s3.endpoint == "https://minio.local"
    assert s3.access_key == "AK"
    assert s3.secret_key == "SK"
    assert s3.bucket == "vault"
    assert s3.region == "auto"
    assert s3.prefix == ""


def test_s3_backend_rejected():
    env = {**COUCHDB_ENV, "LIVESYNC_BACKEND": "s3"}
    with pytest.raises(ConfigError, match="S3 backend"):
        load_config(env)


OAUTH_ENV = {**COUCHDB_ENV, 'MCP_API_KEY': '', 'OAUTH_PASSWORD': 'password',
             'OAUTH_CLIENT_ID': 'client', 'OAUTH_CLIENT_SECRET': 'secret'}


def test_oauth_complete_group():
    cfg = load_config(OAUTH_ENV)
    assert (cfg.api_key, cfg.oauth_password, cfg.oauth_client_id,
            cfg.oauth_client_secret) == ('', 'password', 'client', 'secret')


@pytest.mark.parametrize('missing', ['OAUTH_PASSWORD', 'OAUTH_CLIENT_ID',
                                     'OAUTH_CLIENT_SECRET'])
def test_oauth_partial_group_names_missing_variable(missing):
    with pytest.raises(ConfigError, match=missing):
        load_config({k: v for k, v in OAUTH_ENV.items() if k != missing})


@pytest.mark.parametrize('port', ['0', '-1', '65536', '100000', 'bad'])
def test_port_rejects_invalid_values(port):
    with pytest.raises(ConfigError, match='MCP_PORT'):
        load_config({**COUCHDB_ENV, 'MCP_PORT': port})


@pytest.mark.parametrize('port', [1, 65535])
def test_port_accepts_boundaries(port):
    assert load_config({**COUCHDB_ENV, 'MCP_PORT': str(port)}).port == port


@pytest.mark.parametrize('key', ['LIVESYNC_SCAN_MAX_NOTES', 'LIVESYNC_SCAN_MAX_BYTES', 'LIVESYNC_TEXT_SCAN_MAX_BYTES'])
@pytest.mark.parametrize('value', ['invalid', '0', '-1'])
def test_invalid_scan_limits_fail_config_validation(key, value):
    if key == 'LIVESYNC_TEXT_SCAN_MAX_BYTES' and value == '0':
        assert load_config({**COUCHDB_ENV, key: value}).text_scan_max_bytes == 0
        return
    with pytest.raises(ConfigError, match=key):
        load_config({**COUCHDB_ENV, key: value})


S3_ENV = {
    "LIVESYNC_BACKEND": "s3",
    "LIVESYNC_S3_URI": "sls://AK:SK@https://minio.local?bucket=vault&region=auto&prefix=",
    "MCP_API_KEY": "real-api-key",
}


def test_s3_state_dir_default_and_env():
    config = load_config(S3_ENV)

    assert config.backend == "s3"
    assert config.s3 is not None
    assert config.s3.state_dir == "./data"
    assert config.s3.refresh_min_interval_sec == 5.0

    env = {
        **S3_ENV,
        "S3_STATE_DIR": "/var/lib/mcp/livesync-state",
        "S3_REFRESH_MIN_INTERVAL_SEC": "2.5",
    }
    s3 = load_config(env).s3
    assert s3.state_dir == "/var/lib/mcp/livesync-state"
    assert s3.refresh_min_interval_sec == 2.5

    # Blank values fall back to the defaults, like COUCHDB_URL does.
    blank = load_config({
        **S3_ENV, "S3_STATE_DIR": "", "S3_REFRESH_MIN_INTERVAL_SEC": "",
    }).s3
    assert blank.state_dir == "./data"
    assert blank.refresh_min_interval_sec == 5.0

    # parse_s3_uri alone carries the documented defaults too.
    bare = parse_s3_uri("sls://AK:SK@https://minio.local?bucket=vault")
    assert bare.state_dir == "./data"
    assert bare.refresh_min_interval_sec == 5.0


@pytest.mark.parametrize('value', ['0', '-1', '-0.5', 'bad', 'nan', 'inf'])
def test_s3_refresh_interval_validation(value):
    # The throttle interval must be a positive, finite number; the error
    # names the variable and never echoes credential values.
    env = {**S3_ENV, 'S3_REFRESH_MIN_INTERVAL_SEC': value}
    with pytest.raises(ConfigError, match='S3_REFRESH_MIN_INTERVAL_SEC'):
        load_config(env)


S3_INDIVIDUAL_ENV = {
    "LIVESYNC_BACKEND": "s3",
    "S3_ENDPOINT_URL": "https://minio.local",
    "S3_ACCESS_KEY_ID": "AK",
    "S3_SECRET_ACCESS_KEY": "SK",
    "S3_BUCKET": "vault",
    "MCP_API_KEY": "test-token",
}


def test_s3_individual_form():
    s3 = load_config(S3_INDIVIDUAL_ENV).s3
    assert (s3.endpoint, s3.access_key, s3.secret_key, s3.bucket) == (
        "https://minio.local", "AK", "SK", "vault")
    assert (s3.region, s3.prefix, s3.uri) == ("auto", "", "")
    s3 = load_config({**S3_INDIVIDUAL_ENV, "S3_REGION": "eu-west-1",
                      "S3_PREFIX": "notes/"}).s3
    assert (s3.region, s3.prefix) == ("eu-west-1", "notes/")


@pytest.mark.parametrize("name", [
    "S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET", "S3_REGION", "S3_PREFIX",
])
def test_s3_uri_rejects_mixed_form(name):
    with pytest.raises(ConfigError, match="LIVESYNC_S3_URI.*" + name):
        load_config({**S3_ENV, name: ""})


@pytest.mark.parametrize("name", [
    "S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_BUCKET",
])
def test_s3_individual_requires_fields(name):
    env = dict(S3_INDIVIDUAL_ENV)
    del env[name]
    with pytest.raises(ConfigError, match=name):
        load_config(env)


def test_s3_rejects_both_complete_forms():
    with pytest.raises(ConfigError, match="LIVESYNC_S3_URI"):
        load_config({**S3_INDIVIDUAL_ENV, **S3_ENV})


@pytest.mark.parametrize("form", ["uri", "individual"])
def test_s3_env_example(form):
    from io import StringIO
    from pathlib import Path
    from dotenv import dotenv_values

    lines = Path(".env.example").read_text().splitlines()
    names = {"S3_STATE_DIR", "S3_REFRESH_MIN_INTERVAL_SEC"}
    if form == "uri":
        names.add("LIVESYNC_S3_URI")
    else:
        names.update(S3_INDIVIDUAL_ENV.keys() - {"MCP_API_KEY", "LIVESYNC_BACKEND"})
    rendered = []
    for line in lines:
        if line.startswith("# ") and line[2:].split("=")[0] in names:
            line = line[2:]
        rendered.append(line)
    env = dict(dotenv_values(stream=StringIO("\n".join(rendered))))
    env.update(LIVESYNC_BACKEND="s3", MCP_API_KEY="test-token")
    env = {key: value.replace("your_access_key", "AK").replace("your_secret_key", "SK")
           for key, value in env.items() if value is not None}
    assert load_config(env).s3.bucket == "vault"


@pytest.mark.parametrize("form", ["uri", "individual"])
def test_s3_rejects_example_secret_placeholders(form):
    env = ({**S3_ENV, "LIVESYNC_S3_URI":
            "sls://your_access_key:your_secret_key@https://minio.local?bucket=vault"}
           if form == "uri" else
           {**S3_INDIVIDUAL_ENV, "S3_SECRET_ACCESS_KEY": "your_secret_key"})
    with pytest.raises(ConfigError, match="placeholder"):
        load_config(env)


@pytest.mark.parametrize("raw, expected", [("0", 0), ("12", 12), ("", 0)])
def test_state_size_warning_threshold(raw, expected):
    assert load_config({**S3_ENV, "S3_STATE_SIZE_WARN_MB": raw}).s3.state_size_warn_mb == expected


@pytest.mark.parametrize("raw", ["-1", "1.5", "invalid"])
def test_state_size_warning_threshold_invalid(raw):
    with pytest.raises(ConfigError, match="S3_STATE_SIZE_WARN_MB"):
        load_config({**S3_ENV, "S3_STATE_SIZE_WARN_MB": raw})


@pytest.mark.parametrize('variable,field,default', [
    ('LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH', 'disable_checking_config_mismatch', False),
    ('LIVESYNC_AUTO_ACCEPT_COMPATIBLE_TWEAK', 'auto_accept_compatible_tweak', True),
])
def test_local_tweak_policy_flags(variable, field, default):
    assert getattr(load_config(COUCHDB_ENV), field) is default
    for value, expected in [('true', True), ('false', False)]:
        config = load_config({**COUCHDB_ENV, variable: value})
        assert getattr(config, field) is expected
    for invalid in ('1', 'yes', '', 'private-invalid-value'):
        with pytest.raises(ConfigError) as caught:
            load_config({**COUCHDB_ENV, variable: invalid})
        assert variable in str(caught.value)
        assert invalid != 'private-invalid-value' or invalid not in str(caught.value)


@pytest.mark.parametrize('backend_kind', ['couchdb', 's3'])
async def test_factory_passes_local_tweak_policy(backend_kind):
    import server
    env = {**COUCHDB_ENV, 'LIVESYNC_BACKEND': backend_kind,
        'LIVESYNC_S3_URI': 'sls://access-key:secret-key@https://localhost?bucket=vault&region=auto&prefix=notes',
        'LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH': 'true',
        'LIVESYNC_AUTO_ACCEPT_COMPATIBLE_TWEAK': 'false'}
    backend = server.get_backend(load_config(env))
    try:
        assert backend.disable_checking_config_mismatch is True
        assert backend.auto_accept_compatible_tweak is False
    finally:
        await backend.close()
