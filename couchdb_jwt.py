"""CouchDB JWT wire format from pinned commonlib replication/httplib.ts:67-212."""
from __future__ import annotations

import base64
import math
import re
import time
from decimal import Decimal
from typing import Callable, TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import get_default_algorithms

from wire_encoding import json_stringify

if TYPE_CHECKING:
    from config import CouchDBConfig

ALGORITHMS = ('HS256', 'HS512', 'ES256', 'ES512')
# ECMAScript String.trim (same set as the connection-header parser).
JS_SPACE = '\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'


class JWTConfigError(ValueError):
    """Closed, variable-only actionable JWT configuration error."""

    def __init__(self, message: str):
        self.public_message = message
        super().__init__(message)


def _seconds(now: float) -> int:
    # httplib.ts:166,190 uses ~~: signed int32 seconds, including after 2038.
    return (math.trunc(now) + 2**31) % 2**32 - 2**31


def needs_refresh(iat: int, exp: float, now: float) -> bool:
    # httplib.ts:181-196: this expression always yields ten, strict inequality.
    margin = min(10, max((exp - iat) * 0.1, 60))
    return exp < _seconds(now) + margin


def _decode_key(text: str) -> bytes:
    # Web atob accepts ASCII whitespace and omitted padding, not URL base64.
    text = re.sub(r'[\t\n\f\r ]', '', text)
    if len(text) % 4 == 1 or ('=' in text and len(text) % 4):
        raise ValueError
    return base64.b64decode(text + '=' * (-len(text) % 4), validate=True)


def _segment(value: dict) -> str:
    # httplib.ts:110-113: JSON.stringify followed by btoa (Latin-1 bytes).
    if 'exp' in value:
        exp = value['exp']
        # JSON.stringify Number spelling: decimal below 1e21, unpadded exponents.
        if not math.isfinite(exp):
            number = 'null'
        elif exp == 0:
            number = '0'
        elif 1e-6 <= abs(exp) < 1e21:
            number = format(Decimal(repr(float(exp))), 'f')
            if '.' in number:
                number = number.rstrip('0').rstrip('.')
        else:
            mantissa, exponent = repr(exp).lower().split('e')
            number = mantissa.removesuffix('.0') + 'e' + ('+' if int(exponent) >= 0 else '-') + str(abs(int(exponent)))
        text = '{"exp":' + number + ',' + json_stringify({k:v for k,v in value.items() if k != 'exp'})[1:]
    else:
        text = json_stringify(value)
    return base64.urlsafe_b64encode(text.encode('latin1')).rstrip(b'=').decode('ascii')


class JWTTokenGenerator:
    """Immutable credentials and one per-backend cached token; clock in seconds."""

    def __init__(self, cfg: CouchDBConfig, *, clock: Callable[[], float] = time.time):
        if cfg.jwt_algorithm not in ALGORITHMS:
            raise JWTConfigError('COUCHDB_JWT_ALGORITHM must be HS256, HS512, ES256 or ES512; choose a supported algorithm.')
        if not cfg.jwt_key.strip(JS_SPACE):
            raise JWTConfigError('COUCHDB_JWT_KEY is required; copy JWT Key from the plugin.')
        if not cfg.jwt_subject.strip(JS_SPACE):
            raise JWTConfigError('COUCHDB_JWT_SUBJECT is required; copy JWT Subject (sub) from the plugin.')
        self.algorithm = cfg.jwt_algorithm
        self._signer = get_default_algorithms()[self.algorithm]
        try:
            if self.algorithm.startswith('HS'):
                self._key = _decode_key(cfg.jwt_key.strip(JS_SPACE))
                if not self._key:
                    raise ValueError
            else:
                # httplib.ts:84-97: strip PEM delimiters/JS whitespace; PKCS8 only.
                text = re.sub(r'-----BEGIN [^-]+-----', '', cfg.jwt_key, count=1)
                text = re.sub(r'-----END [^-]+-----', '', text, count=1)
                der = _decode_key(''.join(c for c in text if c not in JS_SPACE))
                # PKCS8 starts with SEQUENCE { INTEGER 0, SEQUENCE algorithm }.
                offset = 2 + (der[1] & 127) if der[1] & 128 else 2
                if der[offset:offset + 4] != b'\x02\x01\x00\x30':
                    raise ValueError
                self._key = serialization.load_der_private_key(der, password=None)
                curve = ec.SECP256R1 if self.algorithm == 'ES256' else ec.SECP521R1
                if not isinstance(self._key, ec.EllipticCurvePrivateKey) or not isinstance(self._key.curve, curve):
                    raise ValueError
        except Exception:
            raise JWTConfigError('COUCHDB_JWT_KEY is invalid; use base64 HMAC bytes or a matching PKCS8 EC PEM private key.') from None
        self._header = {'alg': self.algorithm, 'typ': 'JWT'}
        if cfg.jwt_key_id:
            self._header['kid'] = cfg.jwt_key_id
        # httplib.ts:150 validates trimmed sub, but :171 serializes ORIGINAL sub.
        self._subject = cfg.jwt_subject
        try:
            _segment(self._header)
            _segment({'sub': self._subject})
        except UnicodeEncodeError:
            raise JWTConfigError('COUCHDB_JWT_SUBJECT and COUCHDB_JWT_KEY_ID must fit the plugin Latin-1 encoder; configure matching Latin-1 identifiers in the plugin and server.') from None
        # httplib.ts:167: (jwtExpDuration || 5) * 60; zero is five minutes.
        self._duration = cfg.jwt_expiry_minutes or 5
        self._clock = clock
        self._cached = None
        self._iat = 0
        self._exp = 0.0

    def token(self) -> str:
        now = self._clock()
        if self._cached is None or needs_refresh(self._iat, self._exp, now):
            self._iat = _seconds(now)
            self._exp = self._iat + self._duration * 60
            # JSON.stringify renders integer-valued Numbers without '.0'.
            payload = {'exp': self._exp, 'iat': self._iat, 'sub': self._subject, '_couchdb.roles': ['_admin']}
            data = (_segment(self._header) + '.' + _segment(payload)).encode('ascii')
            # PyJWT ECAlgorithm.sign emits JOSE raw r||s, like WebCrypto :118-136.
            signature = self._signer.sign(data, self._key)
            self._cached = data.decode('ascii') + '.' + base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')
        return self._cached
