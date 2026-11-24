"""Bounded UTF-8 projection of independently base64-encoded newnote pieces."""
import base64
import codecs
import math

DEFAULT_TEXT_SCAN_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_TEXT_VERDICT_CACHE_ENTRIES = 65536


def is_text_characters(text: str) -> bool:
    """Allow Unicode text and tab/newline/CR, reject NUL and controls."""
    return all(c in '\t\n\r' or (ord(c) >= 32 and not 127 <= ord(c) <= 159)
               for c in text)


class TextProjection:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.bytes = 0
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.parts = []

    def feed(self, piece: str, *, final: bool) -> bool:
        raw = base64.b64decode(piece, validate=True)
        self.bytes += len(raw)
        if self.max_bytes and self.bytes > self.max_bytes:
            return False
        try:
            text = self.decoder.decode(raw, final=final)
        except UnicodeDecodeError:
            return False
        if not is_text_characters(text):
            return False
        self.parts.append(text)
        return True

    def text(self) -> str:
        return ''.join(self.parts)


class TextAccessPolicy:
    """Resolve against the current size policy, including every re-resolution.

    C:services/base/VaultService.ts:73-81 multiplies the binary64 MiB value
    by 1024 twice before comparing integer bytes. Floor that same product:
    an integer size is admitted iff it is <= the product. The peer's default
    floor is 256 MiB even for unlimited device policies (G4a/P-29).
    """
    _text_budget_override = None
    regex_max_output_bytes = 0
    regex_timeout_seconds = 10.0

    @property
    def sync_max_size_mb(self):
        return self._sync_max_size_mb

    @sync_max_size_mb.setter
    def sync_max_size_mb(self, value):
        # Both backends assign here at the device size-policy resolution site.
        self._sync_max_size_mb = value
        self._resolve_text_budget()

    def _resolve_text_budget(self):
        if self._text_budget_override is not None:
            self._effective_text_budget = self._text_budget_override
            return
        policy = getattr(self, '_sync_max_size_mb', 50)
        product = policy * 1024 * 1024 if policy > 0 else 0
        # An overflowing binary64 product makes the device predicate unlimited.
        self._effective_text_budget = (0 if math.isinf(product) else
                                      max(DEFAULT_TEXT_SCAN_MAX_BYTES, int(product)))

    @property
    def text_scan_max_bytes(self):
        return self._effective_text_budget

    @text_scan_max_bytes.setter
    def text_scan_max_bytes(self, value):
        self._text_budget_override = value
        self._resolve_text_budget()

    @property
    def effective_regex_max_output_bytes(self):
        return self.regex_max_output_bytes or self.text_scan_max_bytes

    def text_limits(self):
        return dict(text_budget_bytes=self.text_scan_max_bytes,
                    regex_max_output_bytes=self.effective_regex_max_output_bytes,
                    regex_timeout_seconds=self.regex_timeout_seconds)


def text_size(note):
    return (len(note['text'].encode('utf-8')) if note['is_text']
            else len(note['data']))


def enforce_text_budget(note, budget):
    from storage_base import TextBudgetExceeded
    if note is not None:
        size = text_size(note)
        if budget and size > budget:
            raise TextBudgetExceeded(size, budget)
    return note


def scan_text(text, budget):
    if text is None or (budget and len(text.encode('utf-8')) > budget):
        return None
    return text


def project_note(note, budget):
    """Project strict UTF-8 newnotes without changing the stored type."""
    enforce_text_budget(note, budget)
    if note is None or note['is_text']:
        return note
    try:
        text = note['data'].decode('utf-8')
    except UnicodeDecodeError:
        return note
    return {**note, 'text': text, 'is_text': True} if is_text_characters(text) else note
