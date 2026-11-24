"""Last-line redaction for public diagnostics, never for vault content."""
import re
from urllib.parse import unquote

# Accept punctuation/case variations used by headers, JSON and configuration.
_SEPARATOR = r'[\W_]*'
_NAMES = ('aws_access_key_id', 'access_key', 'secret_key', 'secret', 'password',
          'passwd', 'passphrase', 'token', 'api_key', 'apikey', 'client_secret',
          'private_key', 'id_derivation_key', 'idDerivationKey', 'authorization',
          'rev', 'revision')
_LABEL = '(?:' + '|'.join(_SEPARATOR.join(re.escape(part) for part in
    re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])', name.replace('_', ' ')))
    for name in _NAMES) + ')'
_CREDENTIAL = re.compile(r"(?i)(?<![\w])_?" + _LABEL + r"[\"']?\s*[:=]\s*")


def _credential_end(text: str, start: int) -> int:
    """Consume a quoted/container value without reflecting any of its leaves."""
    if start >= len(text):
        return start
    opening = text[start]
    if opening not in "\"'{[":
        end = re.search(r"[,;}\]\n]", text[start:])
        return start + end.start() if end else len(text)
    quote = opening if opening in "\"'" else None
    stack = [] if quote else [opening]
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
                if not stack:
                    return index + 1
        elif char in "\"'":
            quote = char
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if stack:
                stack.pop()
            if not stack:
                return index + 1
    return len(text)


def _redact_credentials(text: str) -> str:
    while match := _CREDENTIAL.search(text):
        end = _credential_end(text, match.end())
        text = text[:match.start()] + '[redacted]' + text[end:]
    return text


def sanitize_diagnostic(text: str) -> str:
    """Redact credential-shaped text, including encoded/nested diagnostics.

    Producers must use safe structured fields. This deliberately errs toward
    redaction; it is not a licence to reflect remote bodies or setting values.
    """
    # Decode before matching, including repeatedly percent-encoded userinfo.
    while True:
        decoded = unquote(text)
        decoded = decoded.replace('\\\"', '\"').replace("\\'", "'")
        decoded = re.sub(r'\\u([0-9a-fA-F]{4})',
                         lambda m: chr(int(m[1], 16)), decoded)
        if decoded == text:
            break
        text = decoded
    # Diagnostic URLs have no public purpose; avoid retaining host/path secrets.
    text = re.sub(r'[A-Za-z][A-Za-z0-9+.-]*://(?:[\s\S]*@)?\S+', '[redacted URL]', text)
    text = re.sub(r'(?is)\bauthorization[\'"\s]*[:=].*', '[redacted]', text)
    text = re.sub(r'(?i)\b(?:bearer|basic)\s+[^\s,;\'"}\]]+', '[redacted]', text)
    # Keep the existing safe key-only explanation, never an actual key value.
    sentinel = 'SAFE_INVALID_ID_CONFIGURATION'
    text = text.replace('idDerivationKey=<invalid configuration>', sentinel)
    text = _redact_credentials(text)
    text = text.replace(sentinel, 'idDerivationKey=<invalid configuration>')
    text = re.sub(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b', '[redacted]', text)
    text = re.sub(r'(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])', '[redacted]', text)
    def entropy(match):
        value = match[0]
        if ('+' in value or '/' in value or (any(c.islower() for c in value)
                and any(c.isupper() for c in value) and any(c.isdigit() for c in value))):
            return '[redacted]'
        return value
    return re.sub(r'[A-Za-z0-9+/=_-]{40,}', entropy, text)


def sanitize_diagnostic_fields(value):
    """Preserve structured diagnostic types while redacting every string leaf."""
    if isinstance(value, str):
        return sanitize_diagnostic(value)
    if isinstance(value, dict):
        return {key: sanitize_diagnostic_fields(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_diagnostic_fields(item) for item in value]
    return value


def invalid_setting(key: str, value: object) -> str:
    """Describe a known setting key and shape without inspecting its value."""
    shape = {str: 'string', int: 'number', float: 'number', bool: 'boolean',
             type(None): 'null', list: 'array', tuple: 'array'}.get(type(value), 'object')
    return f'{key}=<invalid: {shape}>'
