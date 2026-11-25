"""Strict environment configuration for the Obsidian LiveSync MCP server.

``load_config`` validates the environment in a fixed order — backend selection,
credentials of the chosen backend, then client auth — and fails fast with
human-readable messages that never echo secret values. Placeholder values
copied from ``.env.example`` are rejected at startup (spec section 6).
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field, replace
from typing import Literal, Mapping
from urllib.parse import parse_qs

from replay_limits import ReplayConfigError, ReplayLimits

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
    # common/models/setting.const.defaults.ts:196-197: empty connection headers.
    custom_headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    jwt_algorithm: str = ""
    jwt_key: str = field(default="", repr=False)
    jwt_subject: str = field(default="", repr=False)
    jwt_key_id: str = field(default="", repr=False)
    jwt_expiry_minutes: float = 5.0
    connect_timeout_seconds: float = 5.0
    io_timeout_seconds: float = 30.0


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
    # common/models/setting.const.defaults.ts:196-197: empty connection headers.
    custom_headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    # common/models/setting.const.defaults.ts:210.
    force_path_style: bool = True
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    max_attempts: int = 1


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
    disable_checking_config_mismatch: bool = False
    auto_accept_compatible_tweak: bool = True
    selection_only: str = ''
    selection_ignore: str = ''
    selection_use_ignore_files: bool | None = None
    selection_ignore_files: tuple[str, ...] | None = None
    text_verdict_cache_entries: int = 65536
    text_scan_max_bytes: int | None = None
    regex_max_output_bytes: int = 0
    regex_timeout_seconds: float = 10.0
    selection_timeout_seconds: float = 5.0
    scan_max_notes: int = 0
    scan_max_bytes: int = 0
    scan_max_seconds: float = 20.0


def _positive_transport(env, name, default, *, integer=False):
    """Validate operational overrides without reflecting supplied values."""
    raw = env.get(name, default)
    try:
        if type(raw) not in (str, int, float) or (integer and type(raw) is float):
            raise ValueError
        value = int(raw) if integer else float(raw)
        if value <= 0 or not math.isfinite(value):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        rule = "a positive integer" if integer else "positive and finite seconds"
        raise ConfigError(f"{name} must be {rule}") from None
    return value


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


# Commonlib common/utils.ts:459-469 (484c164): split(":", 2) discards
# additional colon segments; trim is ECMAScript String.trim, not Python strip.
_JS_SPACE = " \t\n\r\v\f\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_FIELD_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", re.ASCII)


def _load_custom_headers(env: Mapping[str, str], name: str) -> tuple[tuple[str, str], ...]:
    raw = env.get(name, "")

    def fail(ordinal: int, rule: str):
        raise ConfigError(f"{name} entry {ordinal}: {rule}; correct the header configuration.") from None

    if raw.lstrip(_JS_SPACE).startswith(("{", "[")):
        try:
            # Preserve pairs so duplicate JSON keys cannot disappear in json.loads.
            pairs = json.loads(raw, object_pairs_hook=lambda items: items)
        except json.JSONDecodeError as exc:
            # Count only top-level commas preceding the syntax error; never
            # include the decoder's message or document in a public exception.
            ordinal, depth, quoted, escaped = 1, 0, False, False
            for char in raw[:exc.pos]:
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "{[":
                    depth += 1
                elif char in "}]":
                    depth -= 1
                elif char == "," and depth == 1:
                    ordinal += 1
            fail(ordinal, "invalid JSON object")
        except (ValueError, TypeError, RecursionError):
            fail(1, "invalid JSON object")
        if not raw.lstrip(_JS_SPACE).startswith("{") or not isinstance(pairs, list):
            fail(1, "expected JSON object of strings")
        entries = []
        seen = set()
        for ordinal, pair in enumerate(pairs, 1):
            key, value = pair
            if key in seen:
                fail(ordinal, "duplicate JSON name")
            seen.add(key)
            if type(value) is not str:
                fail(ordinal, "JSON values must be strings")
            entries.append((ordinal, key, value))
    else:
        entries = []
        for ordinal, line in enumerate(raw.split("\n"), 1):
            parts = line.split(":", 2)
            if len(parts) < 2:
                continue
            key, value = (part.strip(_JS_SPACE) for part in parts[:2])
            if key and value:
                entries.append((ordinal, key, value))
    headers = {}
    for ordinal, key, value in entries:
        if not _FIELD_NAME.fullmatch(key):
            fail(ordinal, "HTTP field name must be an RFC 9110 token")
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
            fail(ordinal, "HTTP value must not contain control characters")
        # RemoteService.ts:360 creates native Fetch Headers (ByteString).
        # The S3 Node HTTP transport likewise requires one-byte field octets.
        if any(ord(c) > 255 for c in value):
            fail(ordinal, "HTTP transport requires Latin-1 values; encode proxy credentials as ASCII")
        # JS {} has a prototype setter; assigning a string to __proto__ does
        # not create an own header (utils.ts:460,465). JSON has no such setter.
        if key == "__proto__" and not raw.lstrip(_JS_SPACE).startswith("{"):
            continue
        headers[key] = value
    return tuple(headers.items())


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
    from couchdb_jwt import ALGORITHMS, JWTConfigError, JWTTokenGenerator
    algorithm = env.get("COUCHDB_JWT_ALGORITHM", "")
    jwt_names = ("COUCHDB_JWT_KEY", "COUCHDB_JWT_SUBJECT",
                 "COUCHDB_JWT_KEY_ID", "COUCHDB_JWT_EXPIRY_MINUTES")
    if not algorithm and any(env.get(n, "") for n in jwt_names):
        raise ConfigError("COUCHDB_JWT_ALGORITHM is required with COUCHDB_JWT_*; choose exactly one CouchDB auth mode.")
    if algorithm:
        if algorithm not in ALGORITHMS:
            raise ConfigError("COUCHDB_JWT_ALGORITHM must be HS256, HS512, ES256 or ES512.")
        conflicts = [n for n in ("COUCHDB_USER", "COUCHDB_PASSWORD") if n in env]
        if conflicts:
            raise ConfigError("COUCHDB_JWT_ALGORITHM conflicts with " + ", ".join(conflicts) + "; remove Basic auth variables.")
        try:
            expiry = float(env.get("COUCHDB_JWT_EXPIRY_MINUTES", "5"))
            if not math.isfinite(expiry) or expiry < 0:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ConfigError("COUCHDB_JWT_EXPIRY_MINUTES must be a nonnegative finite number; zero uses five minutes; negative durations are refused because upstream would mint already-expired tokens.") from None
        cfg = CouchDBConfig(
            url=env.get("COUCHDB_URL", "").strip() or COUCHDB_URL_DEFAULT,
            user="", password="", database=_read(env, "COUCHDB_DATABASE"),
            custom_headers=_load_custom_headers(env, "COUCHDB_CUSTOM_HEADERS"),
            jwt_algorithm=algorithm, jwt_key=env.get("COUCHDB_JWT_KEY", ""),
            jwt_subject=env.get("COUCHDB_JWT_SUBJECT", ""),
            jwt_key_id=env.get("COUCHDB_JWT_KEY_ID", ""), jwt_expiry_minutes=expiry,
        )
        try:
            JWTTokenGenerator(cfg)
        except JWTConfigError as exc:
            raise ConfigError(exc.public_message) from None
        return cfg
    password = _read(env, "COUCHDB_PASSWORD")
    _reject_placeholder(password, "COUCHDB_PASSWORD")
    return CouchDBConfig(
        url=env.get("COUCHDB_URL", "").strip() or COUCHDB_URL_DEFAULT,
        user=_read(env, "COUCHDB_USER"), password=password,
        database=_read(env, "COUCHDB_DATABASE"),
        custom_headers=_load_custom_headers(env, "COUCHDB_CUSTOM_HEADERS"),
    )


def _load_s3_state(env: Mapping[str, str]) -> tuple[str, float]:
    """Read S3_STATE_DIR / S3_REFRESH_MIN_INTERVAL_SEC with their defaults.

    Blank values fall back to the defaults (same convention as
    ``COUCHDB_URL``). The refresh interval must be a positive, finite
    number below 2**63; the error names the variable without echoing input.
    """
    state_dir = env.get("S3_STATE_DIR", "").strip() or S3_STATE_DIR_DEFAULT

    raw = env.get("S3_REFRESH_MIN_INTERVAL_SEC", "")
    if type(raw) is str:
        raw = raw.strip()
        if not raw:
            return state_dir, S3_REFRESH_MIN_INTERVAL_DEFAULT
    try:
        if type(raw) not in (str, int, float):
            raise ValueError
        if type(raw) is int:
            interval = raw
        elif type(raw) is str:
            try:
                interval = int(raw)
            except ValueError:
                interval = float(raw)
        else:
            interval = raw
        if not 0 < interval < 2**63 or (type(interval) is float and not math.isfinite(interval)):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ConfigError(
            "S3_REFRESH_MIN_INTERVAL_SEC must be a positive finite number below 2**63"
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
    style = env.get("S3_FORCE_PATH_STYLE", "true")
    if style not in ("true", "false"):
        raise ConfigError("S3_FORCE_PATH_STYLE must be true or false")
    return replace(
        connection,
        custom_headers=_load_custom_headers(env, "S3_CUSTOM_HEADERS"),
        force_path_style=style == "true",
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
        # Peer-only listener policy (P-19/P-21/P-22), not a vault setting.
        raise ConfigError("MCP_PORT must be an integer.") from None

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
    except ReplayConfigError as exc:
        raise ConfigError(exc.public_message) from None
    tweak_policy = {}
    for key, default in (('LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH', 'false'),
                         ('LIVESYNC_AUTO_ACCEPT_COMPATIBLE_TWEAK', 'true')):
        value = env.get(key, default).strip().lower()
        if value not in ('true', 'false'):
            raise ConfigError(f'{key} must be true or false')
        tweak_policy[key] = value == 'true'
    size_override = env.get('LIVESYNC_SYNC_MAX_SIZE_MB', '').strip()
    try:
        sync_max_size_mb = float(size_override) if size_override else None
        if sync_max_size_mb is not None and not math.isfinite(sync_max_size_mb):
            raise ValueError
    except ValueError:
        raise ConfigError('LIVESYNC_SYNC_MAX_SIZE_MB must be a finite number or empty') from None
    scan_limits = []
    for key, default in (("LIVESYNC_SCAN_MAX_NOTES", 0),
                         ("LIVESYNC_SCAN_MAX_BYTES", 0),
                         ("LIVESYNC_TEXT_SCAN_MAX_BYTES", None),
                         ("LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES", 65536)):
        try:
            raw = env.get(key, default)
            value = None if key == "LIVESYNC_TEXT_SCAN_MAX_BYTES" and raw in (None, '') else int(raw)
            if value is not None and (value < 0 or (value == 0 and key == "LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES")):
                raise ValueError
        except (ValueError, TypeError):
            if key == "LIVESYNC_TEXT_SCAN_MAX_BYTES":
                rule = "a nonnegative integer or empty"
            elif key == "LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES":
                rule = "a positive integer"
            else:
                rule = "a nonnegative integer"
            raise ConfigError(f"{key} must be {rule}") from None
        scan_limits.append(value)
    try:
        regex_max_output_bytes = int(env.get('LIVESYNC_REGEX_MAX_OUTPUT_BYTES', 0))
        if regex_max_output_bytes < 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigError('LIVESYNC_REGEX_MAX_OUTPUT_BYTES must be a nonnegative integer') from None
    try:
        regex_timeout_seconds = float(env.get('LIVESYNC_REGEX_TIMEOUT_SECONDS', 10))
        if not math.isfinite(regex_timeout_seconds) or regex_timeout_seconds <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigError('LIVESYNC_REGEX_TIMEOUT_SECONDS must be positive and finite') from None
    try:
        scan_max_seconds = float(env.get('LIVESYNC_SCAN_MAX_SECONDS', 20))
        if not math.isfinite(scan_max_seconds) or scan_max_seconds < 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigError('LIVESYNC_SCAN_MAX_SECONDS must be nonnegative and finite') from None
    backend = env.get("LIVESYNC_BACKEND", "")
    if not backend:
        raise ConfigError(
            "LIVESYNC_BACKEND is required: set it to 'couchdb' or 's3'."
        )
    if backend not in BACKENDS:
        raise ConfigError(
            "LIVESYNC_BACKEND must be 'couchdb' or 's3'."
        )

    couchdb: CouchDBConfig | None = None
    s3: S3Config | None = None
    if backend == "couchdb":
        couchdb = _load_couchdb(env)
    else:
        s3 = _load_s3(env)

    if couchdb is not None:
        couchdb = replace(couchdb,
            connect_timeout_seconds=_positive_transport(env, "COUCHDB_CONNECT_TIMEOUT_SECONDS", 5),
            io_timeout_seconds=_positive_transport(env, "COUCHDB_IO_TIMEOUT_SECONDS", 30))
    if s3 is not None:
        s3 = replace(s3,
            connect_timeout_seconds=_positive_transport(env, "S3_CONNECT_TIMEOUT_SECONDS", 5),
            read_timeout_seconds=_positive_transport(env, "S3_READ_TIMEOUT_SECONDS", 30),
            max_attempts=_positive_transport(env, "S3_MAX_ATTEMPTS", 1, integer=True))
    selection_timeout_seconds = _positive_transport(env, "LIVESYNC_SELECTION_TIMEOUT_SECONDS", 5)
    api_key, oauth_password, oauth_client_id, oauth_client_secret = _load_auth(env)

    use_ignore = env.get('LIVESYNC_USE_IGNORE_FILES', '').lower()
    if use_ignore not in ('', 'true', 'false'):
        raise ConfigError('LIVESYNC_USE_IGNORE_FILES must be true or false.')
    from livesync_format import split_ignore_names
    ignore_files = split_ignore_names(env.get('LIVESYNC_IGNORE_FILES', ''))

    return Config(
        replay_limits=replay_limits,
        sync_max_size_mb=sync_max_size_mb,
        disable_checking_config_mismatch=tweak_policy['LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH'],
        auto_accept_compatible_tweak=tweak_policy['LIVESYNC_AUTO_ACCEPT_COMPATIBLE_TWEAK'],
        selection_only=env.get('LIVESYNC_SYNC_ONLY_REGEX', ''),
        selection_ignore=env.get('LIVESYNC_SYNC_IGNORE_REGEX', ''),
        selection_use_ignore_files=None if not use_ignore else use_ignore == 'true',
        selection_ignore_files=ignore_files if env.get('LIVESYNC_IGNORE_FILES', '') else None,
        text_verdict_cache_entries=scan_limits[3],
        text_scan_max_bytes=scan_limits[2],
        regex_max_output_bytes=regex_max_output_bytes,
        regex_timeout_seconds=regex_timeout_seconds,
        selection_timeout_seconds=selection_timeout_seconds,
        scan_max_notes=scan_limits[0],
        scan_max_bytes=scan_limits[1],
        scan_max_seconds=scan_max_seconds,
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
