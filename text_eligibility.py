"""Bounded UTF-8 projection of independently base64-encoded newnote pieces."""
import base64
import codecs

DEFAULT_TEXT_SCAN_MAX_BYTES = 8 * 1024 * 1024


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
        if self.bytes > self.max_bytes:
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
