"""One isolated, stateless JSON-lines regex worker per server process."""
import atexit
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import threading
import time
import weakref

# The isolated executable path loads standard-library modules only. Unlike
# multiprocessing spawn, it never imports run.py or the server's application.
if __name__ != '__main__':
    from text_eligibility import DEFAULT_TEXT_SCAN_MAX_BYTES
else:
    DEFAULT_TEXT_SCAN_MAX_BYTES = 0  # unused by the child request dispatcher

DEFAULT_TIMEOUT = 10.0
_process = None
_lock = threading.RLock()
_lifecycle_lock = threading.RLock()


class RegexTimeoutError(ValueError):
    """The worker exceeded its hard deadline."""
    def format(self):
        return ('Error: regex pattern too complex or input too large; '
                'increase LIVESYNC_REGEX_TIMEOUT_SECONDS and retry.')


class RegexOutputExceeded(ValueError):
    def format(self):
        return ('Error: regex replacement output exceeds its byte budget; '
                'increase LIVESYNC_REGEX_MAX_OUTPUT_BYTES and retry.')


def _send(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True) + '\n')
    sys.stdout.flush()


def _execute(pattern, text, op, replacement, flags):
    regex = re.compile(pattern, flags)
    if op == 'findall':
        matches = []
        for match in regex.finditer(text):
            groups = match.groups(default='')
            matches.append(match.group() if not groups else
                           groups[0] if len(groups) == 1 else groups)
            if len(matches) == 10:
                break
        _send({'kind': op, 'matches': matches})
    elif op == 'search':
        match = regex.search(text)
        _send({'kind': op, 'context': None if match is None else
               text[max(0, match.start() - 50):match.end() + 50]})
    elif op == 'subn':
        # Send bounded pieces, so the parent can enforce its output budget
        # without passing configuration or other server state into the worker.
        end, count, pending = 0, 0, ''
        for match in regex.finditer(text):
            for piece in (text[end:match.start()], replacement):
                for offset in range(0, len(piece), 32768):
                    pending += piece[offset:offset + 32768]
                    if len(pending) >= 32768:
                        _send({'piece': pending})
                        pending = ''
            end, count = match.end(), count + 1
        tail = text[end:]
        for offset in range(0, len(tail), 32768):
            pending += tail[offset:offset + 32768]
            if len(pending) >= 32768:
                _send({'piece': pending})
                pending = ''
        if pending:
            _send({'piece': pending})
        _send({'kind': op, 'count': count})
    else:
        _send({'error': 'worker'})


def _worker():
    # Upstream VaultService.ts:73-81 admits by the vault's size policy; regex
    # isolation is a peer transport concern, not another file-size restriction.
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if set(request) != {'pattern', 'text', 'op', 'replacement', 'flags'}:
                raise ValueError
            _execute(**request)
        except re.error:
            _send({'error': 'regex'})
        except Exception:
            _send({'error': 'worker'})


def close():
    """Synchronously kill and reap the child, including without an event loop."""
    global _process
    # Request serialization cannot protect shutdown: apply() holds that lock
    # throughout a potentially very long configured deadline. Lifecycle alone
    # serializes process publication and retirement, allowing kill to interrupt
    # the active request before waiting on any request lock. The request thread
    # sees pipe EOF/closure and returns the closed, non-echoing worker error.
    with _lifecycle_lock:
        process, _process = _process, None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass  # the child exited between poll and kill
        process.wait()
        process.stdin.close()
        process.stdout.close()


def bind_loop(loop):
    """Close the process-owned worker even when its owner loop closes abruptly.

    asyncio has no close callback registry: install one idempotent close hook.
    A weak bound method avoids retaining the loop. Several app lifespans may
    share the singleton worker; closing any owning loop reaps it, and another
    active owner starts a fresh worker on its next request.
    """
    if getattr(loop.close, '_livesync_regex_close', False):
        return
    original = weakref.WeakMethod(loop.close)

    def close_loop():
        close()
        callback = original()
        if callback is not None:
            callback()

    close_loop._livesync_regex_close = True
    loop.close = close_loop


def _start():
    global _process
    with _lifecycle_lock:
        if _process is not None and _process.poll() is not None:
            close()
        if _process is None:
            _process = subprocess.Popen(
                [sys.executable, '-I', str(Path(__file__).resolve()), '--worker'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True, bufsize=0, env={})
            os.set_blocking(_process.stdin.fileno(), False)
            os.set_blocking(_process.stdout.fileno(), False)
        return _process


def apply(pattern: str, text: str, op: str, timeout: float | None = None,
          *, replacement: str = '', flags: int = re.IGNORECASE,
          max_output_bytes: int = DEFAULT_TEXT_SCAN_MAX_BYTES) -> dict:
    """Reuse the child; discard it on timeout, crash, or output refusal.

    The deadline includes lock contention and pipe writes as well as execution.
    The child receives only the five operation fields; diagnostic errors are
    closed typed messages and never contain patterns, content or replacements.
    """
    deadline = time.monotonic() + (DEFAULT_TIMEOUT if timeout is None else timeout)
    if not _lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise RegexTimeoutError('Increase LIVESYNC_REGEX_TIMEOUT_SECONDS')
    try:
        process = _start()
        payload = memoryview((json.dumps({
            'pattern': pattern, 'text': text, 'op': op,
            'replacement': replacement, 'flags': int(flags)},
            ensure_ascii=True) + '\n').encode('ascii'))
        pending = bytearray()
        pieces, size = [], 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RegexTimeoutError('Increase LIVESYNC_REGEX_TIMEOUT_SECONDS')
                events = selector.select(remaining)
                if not events:
                    raise RegexTimeoutError('Increase LIVESYNC_REGEX_TIMEOUT_SECONDS')
                for key, _ in events:
                    if key.fileobj is process.stdin:
                        try:
                            count = os.write(process.stdin.fileno(), payload[:65536])
                        except BlockingIOError:
                            continue
                        payload = payload[count:]
                        if not payload:
                            selector.unregister(process.stdin)
                    else:
                        try:
                            data = os.read(process.stdout.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not data:
                            raise ValueError('regex worker failed')
                        pending.extend(data)
                        while b'\n' in pending:
                            line, _, tail = pending.partition(b'\n')
                            pending = bytearray(tail)
                            result = json.loads(line)
                            if 'piece' in result:
                                piece = result['piece']
                                size += len(piece.encode('utf-8'))
                                if max_output_bytes and size > max_output_bytes:
                                    raise RegexOutputExceeded(
                                        'Output too large; increase LIVESYNC_REGEX_MAX_OUTPUT_BYTES')
                                pieces.append(piece)
                                continue
                            if result.get('error') == 'regex':
                                raise re.error('Invalid regular expression.')
                            if result.get('error') or result.get('kind') != op:
                                raise ValueError('regex worker failed')
                            if op == 'subn':
                                result['result'] = ''.join(pieces)
                            elif op == 'findall':
                                result['matches'] = [tuple(item) if isinstance(item, list) else item
                                                     for item in result['matches']]
                            return result
    except re.error:
        raise
    except (RegexTimeoutError, RegexOutputExceeded):
        close()
        raise
    except Exception:
        close()
        raise ValueError('regex worker failed') from None
    finally:
        _lock.release()


atexit.register(close)

if __name__ == '__main__':
    _worker()
