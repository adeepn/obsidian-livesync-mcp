"""Typed diagnostic construction. Free text is never a public message source."""
from dataclasses import dataclass
import hashlib
import logging
import os
import re
import unicodedata

from diagnostic_codes import DiagnosticCode


_CONTROLS = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029]')


def escape_path(value: str, *, remote: bool = False) -> str:
    """Preserve identity; remote metadata has the protocol's NFC normalization."""
    if remote:
        value = unicodedata.normalize('NFC', value)
    def visible(match):
        c = match[0]
        return {'\n': r'\n', '\r': r'\r', '\t': r'\t'}.get(c, f'\\u{ord(c):04x}')
    return _CONTROLS.sub(visible, value)


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

    def format(self):
        if self.code is DiagnosticCode.WRITER:
            return ('vault uses ' + ', '.join(issue.format() for issue in self.issues) +
                    '; peer writes are not implemented for this mode yet — reads, search and replay work')
        if self.code is DiagnosticCode.TWEAK_ALIGNMENT:
            return ('vault tweak settings differ: ' + ', '.join(setting_key(k) for k in self.keys) +
                '; devices require alignment with PREFERRED before synchronizing' +
                ('; cause: ' + self.cause.__name__ if self.cause else ''))
        result = self.code.value
        if self.code is DiagnosticCode.SELECTION_ENGINE:
            cause = self.selection_cause if self.selection_cause in {
                'node-missing', 'node-version-too-old', 'timeout', 'nonzero-exit',
                'malformed-output', 'runner-selftest-failed', 'worker-unavailable',
            } else '<value hidden>'
            result += ': ' + cause
            if type(self.status) is int:
                result += f' (status {self.status})'
            result += '; install Node.js 22+ in the service PATH or disable selection filters'
        if self.paths:
            result += "; note: " + " -> ".join(path_field(p) for p in self.paths)
        if self.keys:
            result += '; settings: ' + ', '.join(setting_key(k) for k in self.keys)
        if self.journal is not None:
            result += '; journal: ' + journal_name(self.journal)
        if self.cause is not None:
            result += '; cause: ' + self.cause.__name__
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
    sample = text[:2048]
    return '[redacted]' if sample else ''


def unsafe_debug(logger, text):
    if os.environ.get('LIVESYNC_UNSAFE_DEBUG') == '1' and logger.isEnabledFor(logging.DEBUG):
        logger.debug('unsafe diagnostic: %s', sanitize_diagnostic(text))


def invalid_setting(key: str, value: object) -> str:
    shape = {str:'string', int:'number', float:'number', bool:'boolean',
             type(None):'null', list:'array', tuple:'array'}.get(type(value), 'object')
    return f'{setting_key(key)}=<invalid: {shape}>'
