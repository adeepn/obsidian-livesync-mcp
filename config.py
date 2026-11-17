"""Strict environment configuration for the Obsidian LiveSync MCP server.

``load_config`` validates the environment in a fixed order — backend selection,
credentials of the chosen backend, then client auth — and fails fast with
human-readable messages that never echo secret values. Placeholder values
copied from ``.env.example`` are rejected at startup (spec section 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping
from urllib.parse import parse_qs

S3_URI_SCHEME = "sls://"

BACKENDS = ("couchdb", "s3")

# Audit A4: the URL default is the standard CouchDB port, matching README
# and .env.example. Credentials and database stay required (no defaults).
COUCHDB_URL_DEFAULT = "http://localhost:5984"

# Values that look like an untouched copy of .env.example; startup must
# refuse them instead of silently running with known credentials.
PLACEHOLDER_SECRET_VALUES = frozenset(
    {
        "your_api_key",
        "your_password",
        "your_oauth_password",
        "your_client_id",
        "your_client_secret",
    }
)


class ConfigError(Exception):
    """Environment configuration is missing, incomplete, or invalid."""


@dataclass(frozen=True)
class CouchDBConfig:
    """Connection settings for the CouchDB storage backend (spec section 6)."""

    url: str
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class S3Config:
    """Settings parsed from a LiveSync setup URI (stub; full backend in Plan B)."""

    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    region: str
    prefix: str
    uri: str


@dataclass(frozen=True)
class Config:
    """Validated server configuration."""

    backend: Literal["couchdb", "s3"]
    couchdb: CouchDBConfig | None
    s3: S3Config | None
    server_url: str
    host: str
    port: int
    api_key: str
    oauth_password: str
    oauth_client_id: str
    oauth_client_secret: str


def parse_s3_uri(uri: str) -> S3Config:
    """Parse ``sls://access_key:secret_key@endpoint?bucket=&region=&prefix=``.

    Credentials sit in the userinfo part, the endpoint (including its scheme)
    between ``@`` and ``?``, and bucket/region/prefix in the query string.
    """
    if not uri.startswith(S3_URI_SCHEME):
        raise ConfigError("LIVESYNC_S3_URI must start with 'sls://'.")

    rest = uri[len(S3_URI_SCHEME) :]

    userinfo, at_sep, tail = rest.partition("@")
    if not at_sep or not userinfo:
        raise ConfigError(
            "LIVESYNC_S3_URI must contain credentials before '@' "
            "(sls://access_key:secret_key@endpoint?...)."
        )
    access_key, colon_sep, secret_key = userinfo.partition(":")
    if not colon_sep or not access_key or not secret_key:
        raise ConfigError(
            "LIVESYNC_S3_URI credentials before '@' must look like "
            "access_key:secret_key."
        )

    endpoint, question_sep, query = tail.partition("?")
    if not endpoint:
        raise ConfigError("LIVESYNC_S3_URI must contain an endpoint after '@'.")

    params = parse_qs(query, keep_blank_values=True)
    bucket = params.get("bucket", [""])[0]
    region = params.get("region", [""])[0]
    prefix = params.get("prefix", [""])[0]
    if not bucket:
        raise ConfigError("LIVESYNC_S3_URI must set a non-empty 'bucket' query parameter.")

    return S3Config(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        region=region,
        prefix=prefix,
        uri=uri,
    )


def _read(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ConfigError(f"{name} is required when LIVESYNC_BACKEND=couchdb.")
    return value


def _reject_placeholder(value: str, name: str) -> None:
    if value in PLACEHOLDER_SECRET_VALUES:
        raise ConfigError(
            f"{name} still holds a placeholder value from .env.example; "
            "set a real secret."
        )


def _load_couchdb(env: Mapping[str, str]) -> CouchDBConfig:
    password = _read(env, "COUCHDB_PASSWORD")
    _reject_placeholder(password, "COUCHDB_PASSWORD")
    return CouchDBConfig(
        url=env.get("COUCHDB_URL", "").strip() or COUCHDB_URL_DEFAULT,
        user=_read(env, "COUCHDB_USER"),
        password=password,
        database=_read(env, "COUCHDB_DATABASE"),
    )


def _load_auth(env: Mapping[str, str]) -> tuple[str, str, str, str]:
    """Validate client auth: bearer (MCP_API_KEY) or the OAUTH_* group."""
    api_key = env.get("MCP_API_KEY", "")
    oauth_password = env.get("OAUTH_PASSWORD", "")
    oauth_client_id = env.get("OAUTH_CLIENT_ID", "")
    oauth_client_secret = env.get("OAUTH_CLIENT_SECRET", "")

    _reject_placeholder(api_key, "MCP_API_KEY")
    _reject_placeholder(oauth_password, "OAUTH_PASSWORD")
    _reject_placeholder(oauth_client_id, "OAUTH_CLIENT_ID")
    _reject_placeholder(oauth_client_secret, "OAUTH_CLIENT_SECRET")

    if api_key:
        # Bearer mode; when both groups are set, MCP_API_KEY wins
        # (same precedence as auth_config.select_auth_mode).
        return api_key, oauth_password, oauth_client_id, oauth_client_secret

    oauth_set = (oauth_password, oauth_client_id, oauth_client_secret)
    if all(oauth_set):
        return api_key, oauth_password, oauth_client_id, oauth_client_secret
    if not any(oauth_set):
        raise ConfigError(
            "Authentication is required: set MCP_API_KEY for bearer auth, "
            "or the OAUTH_PASSWORD / OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET "
            "group for OAuth."
        )
    raise ConfigError(
        "OAUTH_* group is incomplete: OAUTH_PASSWORD, OAUTH_CLIENT_ID and "
        "OAUTH_CLIENT_SECRET must all be set."
    )


def _load_port(env: Mapping[str, str]) -> int:
    raw = env.get("MCP_PORT", "8484")
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"MCP_PORT must be an integer (got {raw!r}).") from None


def load_config(env: Mapping[str, str]) -> Config:
    """Validate the environment once and build a Config.

    Validation order: backend selection, credentials of the chosen backend,
    then client auth. Raises ConfigError on the first problem; messages
    mention variable names but never secret values.
    """
    backend = env.get("LIVESYNC_BACKEND", "")
    if not backend:
        raise ConfigError(
            "LIVESYNC_BACKEND is required: set it to 'couchdb' or 's3'."
        )
    if backend not in BACKENDS:
        raise ConfigError(
            f"LIVESYNC_BACKEND must be 'couchdb' or 's3' (got {backend!r})."
        )

    couchdb: CouchDBConfig | None = None
    s3: S3Config | None = None
    if backend == "couchdb":
        couchdb = _load_couchdb(env)
    else:
        raise ConfigError("S3 backend появится в Плане B")

    api_key, oauth_password, oauth_client_id, oauth_client_secret = _load_auth(env)

    return Config(
        backend=backend,
        couchdb=couchdb,
        s3=s3,
        server_url=env.get("MCP_SERVER_URL", "https://localhost:8484"),
        host=env.get("MCP_HOST", "0.0.0.0"),
        port=_load_port(env),
        api_key=api_key,
        oauth_password=oauth_password,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
    )
