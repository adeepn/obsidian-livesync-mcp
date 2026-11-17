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
