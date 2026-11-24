"""Strict environment configuration for the Obsidian LiveSync MCP server.

``load_config`` validates the environment in a fixed order — backend selection,
credentials of the chosen backend, then client auth — and fails fast with
human-readable messages that never echo secret values. Placeholder values
copied from ``.env.example`` are rejected at startup (spec section 6).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from typing import Literal, Mapping
from urllib.parse import parse_qs

from replay_limits import ReplayLimits

S3_URI_SCHEME = "sls://"

BACKENDS = ("couchdb", "s3")

# S3 journal-peer local state (spec section 6): directory holding the
# cached journal state, and the minimum spacing between automatic
# refreshes of that state.
S3_STATE_DIR_DEFAULT = "./data"
S3_REFRESH_MIN_INTERVAL_DEFAULT = 5.0

# Audit A4: the URL default is the standard CouchDB port, matching README
# and .env.example. Credentials and database stay required (no defaults).
COUCHDB_URL_DEFAULT = "http://localhost:5984"

# Values that look like an untouched copy of .env.example; startup must
# refuse them instead of silently running with known credentials.
PLACEHOLDER_SECRET_VALUES = frozenset(
    {
        "your_api_key",
        "your_access_key",
        "your_secret_key",
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
    """Settings for the S3 journal peer: connection fields plus local state.

    Connection fields come from ``LIVESYNC_S3_URI`` or individual S3 variables.
    ``state_dir`` (env ``S3_STATE_DIR``) is where the peer keeps its cached
    journal state; ``refresh_min_interval_sec`` (env
    ``S3_REFRESH_MIN_INTERVAL_SEC``) throttles refresh calls.
    """

    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    region: str
    prefix: str
    uri: str
    state_dir: str = S3_STATE_DIR_DEFAULT
    refresh_min_interval_sec: float = S3_REFRESH_MIN_INTERVAL_DEFAULT
    state_size_warn_mb: int = 0


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
    replay_limits: ReplayLimits = ReplayLimits()
    sync_max_size_mb: float | None = None
    selection_only: str = ''
    selection_ignore: str = ''
    selection_use_ignore_files: bool = False
    selection_ignore_files: tuple[str, ...] = ()
    text_verdict_cache_entries: int = 4096
    text_scan_max_bytes: int = 8 * 1024 * 1024
    scan_max_notes: int = 10000
    scan_max_bytes: int = 64 * 1024 * 1024


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


def _load_s3_state(env: Mapping[str, str]) -> tuple[str, float]:
    """Read S3_STATE_DIR / S3_REFRESH_MIN_INTERVAL_SEC with their defaults.

    Blank values fall back to the defaults (same convention as
    ``COUCHDB_URL``). The refresh interval must be a positive, finite
    number; the error names the variable and echoes only the offending
    non-secret value.
    """
    state_dir = env.get("S3_STATE_DIR", "").strip() or S3_STATE_DIR_DEFAULT

    raw = env.get("S3_REFRESH_MIN_INTERVAL_SEC", "").strip()
    if not raw:
        return state_dir, S3_REFRESH_MIN_INTERVAL_DEFAULT
    try:
        interval = float(raw)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError
    except ValueError:
        raise ConfigError(
            f"S3_REFRESH_MIN_INTERVAL_SEC must be a positive number "
            f"(got {raw!r})."
        ) from None
    return state_dir, interval


def _load_s3(env: Mapping[str, str]) -> S3Config:
    """Validate exactly one connection form, plus shared local state settings."""
    uri = env.get("LIVESYNC_S3_URI", "")
    fields = (
        "S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
        "S3_BUCKET", "S3_REGION", "S3_PREFIX",
    )
    mixed = [name for name in fields if name in env]
    if uri and mixed:
        raise ConfigError(
            "LIVESYNC_S3_URI conflicts with individual S3 variables: "
            + ", ".join(mixed) + "; choose exactly one connection form."
        )
    if uri:
        connection = parse_s3_uri(uri)
    else:
        if not mixed:
            raise ConfigError(
                "the S3 backend requires LIVESYNC_S3_URI or individual "
                "S3_ENDPOINT_URL / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_BUCKET."
            )
        for name in fields[:4]:
            if not env.get(name, "").strip():
                raise ConfigError(f"{name} is required when LIVESYNC_BACKEND=s3.")
        connection = S3Config(
            endpoint=env["S3_ENDPOINT_URL"],
            access_key=env["S3_ACCESS_KEY_ID"],
            secret_key=env["S3_SECRET_ACCESS_KEY"],
            bucket=env["S3_BUCKET"],
            region=env.get("S3_REGION", "").strip() or "auto",
            prefix=env.get("S3_PREFIX", ""),
            uri="",
        )
    _reject_placeholder(connection.access_key, "S3_ACCESS_KEY_ID")
    _reject_placeholder(connection.secret_key, "S3_SECRET_ACCESS_KEY")
    state_dir, refresh_sec = _load_s3_state(env)
    try:
        warn_mb = int(env.get("S3_STATE_SIZE_WARN_MB", "").strip() or "0")
        if warn_mb < 0:
            raise ValueError
    except ValueError:
        raise ConfigError("S3_STATE_SIZE_WARN_MB must be a non-negative integer") from None
    return replace(
        connection,
        state_dir=state_dir,
        refresh_min_interval_sec=refresh_sec,
        state_size_warn_mb=warn_mb,
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
        port = int(raw)
    except ValueError:
        raise ConfigError(f"MCP_PORT must be an integer (got {raw!r}).") from None

    if not 1 <= port <= 65535:
        raise ConfigError("MCP_PORT must be in 1..65535.")
    return port


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Validate the environment once and build a Config.

    Validation order: backend selection, credentials of the chosen backend,
    then client auth. Raises ConfigError on the first problem; messages
    mention variable names but never secret values.
    """
    if env is None:
        env = os.environ
    try:
        replay_limits = ReplayLimits.from_env(env)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    size_override = env.get('LIVESYNC_SYNC_MAX_SIZE_MB', '').strip()
    try:
        sync_max_size_mb = float(size_override) if size_override else None
        if sync_max_size_mb is not None and not math.isfinite(sync_max_size_mb):
            raise ValueError
    except ValueError:
        raise ConfigError('LIVESYNC_SYNC_MAX_SIZE_MB must be a finite number or empty') from None
    scan_limits = []
    for key, default in (("LIVESYNC_SCAN_MAX_NOTES", 10000),
                         ("LIVESYNC_SCAN_MAX_BYTES", 64 * 1024 * 1024),
                         ("LIVESYNC_TEXT_SCAN_MAX_BYTES", 8 * 1024 * 1024),
                         ("LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES", 4096)):
        try:
            value = int(env.get(key, default))
            if value <= 0:
                raise ValueError
        except (ValueError, TypeError):
            raise ConfigError(f"{key} must be a positive integer") from None
        scan_limits.append(value)
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
        s3 = _load_s3(env)

    api_key, oauth_password, oauth_client_id, oauth_client_secret = _load_auth(env)

    use_ignore = env.get('LIVESYNC_USE_IGNORE_FILES', 'false').lower()
    if use_ignore not in ('true', 'false'):
        raise ConfigError('LIVESYNC_USE_IGNORE_FILES must be true or false.')
    ignore_files = tuple(
        name.strip() for name in env.get('LIVESYNC_IGNORE_FILES', '').split(',')
        if name.strip()
    )
    if any(
        name.startswith('/') or '\\' in name
        or any(part in ('', '.', '..') for part in name.split('/'))
        for name in ignore_files
    ):
        raise ConfigError(
            'LIVESYNC_IGNORE_FILES must contain comma-separated vault-relative '
            'file names without traversal.'
        )

    return Config(
        replay_limits=replay_limits,
        sync_max_size_mb=sync_max_size_mb,
        selection_only=env.get('LIVESYNC_SYNC_ONLY_REGEX', ''),
        selection_ignore=env.get('LIVESYNC_SYNC_IGNORE_REGEX', ''),
        selection_use_ignore_files=use_ignore == 'true',
        selection_ignore_files=ignore_files,
        text_verdict_cache_entries=scan_limits[3],
        text_scan_max_bytes=scan_limits[2],
        scan_max_notes=scan_limits[0],
        scan_max_bytes=scan_limits[1],
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
