"""Typed diagnostic construction. Free text is never a public message source."""
from dataclasses import dataclass
import math
from decimal import Decimal
import hashlib
import logging
import os
import re
import unicodedata

from diagnostic_codes import DiagnosticCode


_CONTROLS = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]')


def escape_path(value: str, *, remote: bool = False) -> str:
    """Escape C0/C1, line separators and bidi embeddings/overrides/isolates.

    Zero-width U+200B/C/D and BOM U+FEFF pass through: they can belong to
    a note identity and do not reorder the displayed outcome. Structured
    path fields retain exact identity; only free-text lines use escaping.
    Remote diagnostic metadata uses the protocol's NFC normalization.
    """
    if remote:
        value = unicodedata.normalize('NFC', value)
    def visible(match):
        c = match[0]
        return {'\n': r'\n', '\r': r'\r', '\t': r'\t'}.get(c, f'\\u{ord(c):04x}')
    return _CONTROLS.sub(visible, value)


def numeric_field(value, *, kind='integer'):
    """Validate only built-in numbers against the declared field contract."""
    if kind == 'exit':
        valid = type(value) is int and -256 <= value <= 256
    elif kind == 'measured':
        valid = (type(value) in (int, float) and 0 <= value < 2**63
                 and (type(value) is int or math.isfinite(value)))
    else:
        valid = type(value) is int and 0 <= value < 2**63
    return value if valid else '<invalid>'


def numeric_text(value, *, kind='integer'):
    """Plain decimal text; floats round to 12 significant digits, ints stay exact.

    All valid magnitudes use fixed notation, including below 1e-6 and at/above
    1e16. Validation precedes rounding; out-of-range values stay invalid.
    """
    value = numeric_field(value, kind=kind)
    if type(value) is int:
        return str(value)
    if type(value) is float:
        text = format(Decimal(format(value, '.12g')), 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    return '<invalid>'


def identifier(value, fallback='<unexpected identifier>'):
    return value if type(value) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', value) else fallback


def cause_name(value):
    name = value.__name__ if isinstance(value, type) else None
    return identifier(name, '<unexpected cause>')


def operation_field(value):
    return value if type(value) is str and value in {'GET','PUT','POST','DELETE','HEAD','LIST'} else '<unexpected operation>'


@dataclass(frozen=True)
class DiagnosticPath:
    value: str
    remote: bool

    def format(self):
        return escape_path(self.value, remote=self.remote)


def path_field(value) -> str:
    return value.format() if isinstance(value, DiagnosticPath) else '<path unavailable>'


def setting_key(key) -> str:
    # Lazy import avoids the codec -> diagnostic helper cycle.
    from livesync_format import TWEAK_COMPATIBILITY_RULES, _TWEAK_TEMPLATE_KEYS
    known = {r.key for r in TWEAK_COMPATIBILITY_RULES} | _TWEAK_TEMPLATE_KEYS | {
        'idDerivationKey', 'syncOnlyRegEx', 'syncIgnoreRegEx', 'useIgnoreFiles',
        'max_line_bytes', 'max_inflated_bytes', 'max_records', 'max_json_depth',
        'max_object_bytes', 'max_refresh_keys', 'max_refresh_bytes', 'max_refresh_seconds',
    }
    return key if type(key) is str and key in known else '<unexpected key>'


def setting_value(value):
    if type(value) in (int, float, bool):
        return value
    return value if type(value) is str and value in {
        'v1', 'v2', 'v2-segmenter', 'v3-rabin-karp', 'xxhash32', 'xxhash64',
        'mixed-purejs', 'sha1', 'plain', 'legacy', 'nfc', 'case-sensitive',
        'case-insensitive', 'nfc-case-sensitive', 'nfc-case-insensitive',
    } else '<value hidden>'


def journal_name(value: str) -> str:
    if re.fullmatch(r'[0-9]+-(?:[0-9a-fA-F]{32}-)?docs\.jsonl\.gz', value):
        return value
    return f'<unexpected journal name> (length {len(value)})'


def fingerprint(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


@dataclass(frozen=True)
class SettingIssue:
    key: str
    shape: str

    @classmethod
    def from_value(cls, key, value):
        return cls(key, {str:'string', int:'number', float:'number', bool:'boolean',
             type(None):'null', list:'array', tuple:'array'}.get(type(value), 'object'))

    def format(self):
        shape = self.shape if self.shape in {'string', 'number', 'boolean', 'null', 'array', 'object', 'configuration'} else 'object'
        return f'{setting_key(self.key)}=<invalid: {shape}>' if shape != 'configuration' else f'{setting_key(self.key)}=<invalid configuration>'


_STATUS_GUIDANCE = {
    400: 'the server rejected the request as invalid; do not retry unchanged',
    405: 'method not allowed (server/configuration mismatch)',
    408: 'the request timed out; try again later',
    413: "the request is too large for the server; do not retry unchanged (split the content or raise the server's limit)",
    429: 'rate limited; wait before retrying',
}


@dataclass(frozen=True)
class PublicDiagnostic:
    code: DiagnosticCode = DiagnosticCode.FAILED
    keys: tuple[str, ...] = ()
    issues: tuple[SettingIssue, ...] = ()
    paths: tuple[DiagnosticPath, ...] = ()
    journal: str | None = None
    cause: type[BaseException] | None = None
    selection_cause: str | None = None
    status: int | None = None

    operation: str | None = None
    transport: str | None = None
    cause_class: str | None = None

    def __post_init__(self):
        if self.status is not None:
            object.__setattr__(self, 'status', numeric_field(self.status, kind='exit' if self.code is DiagnosticCode.SELECTION_ENGINE else 'integer'))
        if self.operation is not None:
            object.__setattr__(self, 'operation', operation_field(self.operation))
        if self.cause_class is not None:
            object.__setattr__(self, 'cause_class', identifier(self.cause_class, '<unexpected cause>'))

    def status_guidance(self):
        status = numeric_field(self.status)
        if status == 408 and operation_field(self.operation) in {'PUT', 'POST', 'DELETE'}:
            return 'the request timed out; the outcome of a write is unknown — re-read before retrying'
        return _STATUS_GUIDANCE.get(status)

    def format(self):
        if self.operation is not None:
            transport = self.transport if type(self.transport) is str and self.transport in {'CouchDB', 'S3'} else 'storage'
            operation = operation_field(self.operation)
            text = (f'{transport} returned HTTP {numeric_field(self.status)} for {operation}'
                    if self.status is not None else f'{transport} {operation} failed')
            guidance = self.status_guidance()
            if guidance is not None:
                text += ': ' + guidance
            cause = identifier(self.cause_class, '<unexpected cause>') if self.cause_class is not None else cause_name(self.cause) if self.cause is not None else None
            if isinstance(self.code, DiagnosticCode) and self.code is not DiagnosticCode.FAILED:
                text = self.code.value + ': ' + text
            return text + ('; cause: ' + cause if cause is not None else '')
        if self.code is DiagnosticCode.WRITER:
            return ('vault uses ' + ', '.join(issue.format() for issue in self.issues) +
                    '; peer writes are not implemented for this mode yet — reads, search and replay work')
        if self.code is DiagnosticCode.TWEAK_ALIGNMENT:
            return ('vault tweak settings differ: ' + ', '.join(setting_key(k) for k in self.keys) +
                '; devices require alignment with PREFERRED before synchronizing' +
                ('; cause: ' + cause_name(self.cause) if self.cause else ''))
        result = self.code.value
        if self.code is DiagnosticCode.SELECTION_ENGINE:
            cause = self.selection_cause if self.selection_cause in {
                'node-missing', 'node-version-too-old', 'timeout', 'nonzero-exit',
                'malformed-output', 'runner-selftest-failed', 'worker-unavailable', 'provenance-mismatch',
            } else '<value hidden>'
            result += ': ' + cause
            if self.status is not None:
                result += f" (status {numeric_text(self.status, kind='exit')})"
            if cause == 'provenance-mismatch':
                result += '; restore or regenerate the verified selection installation, then restart'
            elif cause == 'timeout':
                result += '; raise LIVESYNC_SELECTION_TIMEOUT_SECONDS and restart, or simplify selection rules'
            else:
                result += '; install Node.js 22+ in the service PATH or disable selection filters'
        if self.paths:
            result += "; note: " + " -> ".join(path_field(p) for p in self.paths)
        if self.keys:
            result += '; settings: ' + ', '.join(setting_key(k) for k in self.keys)
        if self.journal is not None:
            result += '; journal: ' + journal_name(self.journal)
        if self.cause is not None:
            result += '; cause: ' + cause_name(self.cause)
        return result


def diagnostic(value=None, *, cause=None) -> PublicDiagnostic:
    if isinstance(value, PublicDiagnostic):
        return value
    if isinstance(value, DiagnosticCode):
        return PublicDiagnostic(value, cause=type(cause) if cause else None)
    # Compatibility with internal literal reasons only: this finite inventory
    # cannot admit a new string, a setting value, or an interpolated template.
    try:
        code = DiagnosticCode(value) if type(value) is str else DiagnosticCode.FAILED
    except ValueError:
        code = DiagnosticCode.FAILED
    return PublicDiagnostic(code, cause=type(cause) if cause else None)


def settings_exception_diagnostic(error):
    """Consume codec-owned fields, never its exception message."""
    if hasattr(error, 'tweak_assessment'):
        return PublicDiagnostic(DiagnosticCode.TWEAK_ALIGNMENT,
            keys=tuple(error.tweak_assessment['differing_keys']), cause=type(error))
    if getattr(error, 'setting_keys', ()):
        return PublicDiagnostic(DiagnosticCode.INVALID_SETTING,
            keys=tuple(error.setting_keys), cause=type(error))
    return getattr(error, 'diagnostic', PublicDiagnostic(cause=type(error)))


def writer_diagnostic(value):
    if isinstance(value, PublicDiagnostic):
        return value.format()
    return ('Invalid device selection pattern' if value == 'Invalid device selection pattern' else
            'writer settings unsupported') if value is not None else None


def assessment_fields(value):
    status = value.get('status')
    return {'status': status if type(status) is str and status in {
        'OK', 'ID_KEY_MISMATCH', 'PATH_IDENTITY_MISMATCH', 'LOSSY_AUTO_ACCEPTABLE', 'MISMATCHED',
    } else '<value hidden>',
        'differing_keys': [setting_key(k) for k in value.get('differing_keys', [])]}


def sanitize_diagnostic(text: str) -> str:
    """Last-line fail-closed redactor for opt-in DEBUG text only.

    Inspect at most 2048 characters. No regex, decoding loop or scanning of the
    original input: O(min(n, 2048)) time and space, independent of secret syntax.
    Free text has no tool-visible use. Even short encodings are withheld.
    """
    if not isinstance(text, str):
        return '[redacted]'
    sample = str.__getitem__(text, slice(0, 2048))
    return '[redacted]' if sample else ''


def unsafe_debug(logger, text):
    if os.environ.get('LIVESYNC_UNSAFE_DEBUG') == '1' and logger.isEnabledFor(logging.DEBUG):
        logger.debug('unsafe diagnostic: %s', sanitize_diagnostic(text))


def invalid_setting(key: str, value: object) -> str:
    shape = {str:'string', int:'number', float:'number', bool:'boolean',
             type(None):'null', list:'array', tuple:'array'}.get(type(value), 'object')
    return f'{setting_key(key)}=<invalid: {shape}>'


class _NoTransportDiagnostics(logging.Filter):
    """Transport debug records can contain credentials or raw exception text."""

    def filter(self, record):
        return False


def silence_transport_logging():
    """Keep SDK/HTTP diagnostics closed when proxy credentials are configured.

    SDK debug tracebacks and response bodies cannot be safely projected from
    their free-form records. Application diagnostics retain typed fields only.
    These modules are imported by the transports before this function is used.
    """
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.split('.')[0] in {'botocore', 'boto3', 'urllib3', 'httpx', 'httpcore'}:
            target = logging.getLogger(name)
            if not any(isinstance(f, _NoTransportDiagnostics) for f in target.filters):
                target.addFilter(_NoTransportDiagnostics())


def recommendation_fields(value):
    """Closed P-48 projection; no remote strings, names, IDs or extra fields."""
    if type(value) is not dict:
        return {}
    result = {}
    for key in ('syncMaxSizeInMB', 'useIgnoreFiles', 'ignoreFiles'):
        entry = value.get(key)
        if type(entry) is not dict:
            continue
        source = entry.get('source')
        projected = dict(source=source if type(source) is str and source in {
            'override', 'device', 'preferred'} else None)
        count = entry.get('device_count')
        projected['device_count'] = count if type(count) is int and 0 <= count < 2**63 else 0
        disagree = entry.get('disagree')
        projected['disagree'] = disagree if type(disagree) is bool else False
        if key == 'ignoreFiles':
            count = entry.get('name_count')
            projected['name_count'] = count if type(count) is int and 0 <= count < 2**63 else 0
        else:
            policy = entry.get('value')
            valid = (type(policy) is bool if key == 'useIgnoreFiles' else
                     type(policy) in (int, float))
            # Native +Infinity represents an unlimited device predicate. Keep
            # strict JSON and mark this representation explicitly with null.
            projected['value'] = policy if valid and (type(policy) is bool or math.isfinite(policy)) else None
        result[key] = projected
    return result
