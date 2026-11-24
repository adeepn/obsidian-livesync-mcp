"""Fail-closed boundary around the pinned ECMAScript selection engine."""
import json
import logging
import queue
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)
RUNNER = Path(__file__).parent / 'vendor/selection/runner.mjs'
TIMEOUT = 5


class EngineFailure(Exception):
    def __init__(self, cause):
        self.cause = cause
        super().__init__(f'Device selection engine: {cause}; install Node.js 22+ in the service PATH or disable selection filters')


def validate_response(value):
    from livesync_format import TargetVerdict
    if (type(value) is not dict or set(value) != {'allowed', 'rule', 'writer_supported', 'reason'}
            or type(value['allowed']) is not bool
            or type(value['writer_supported']) is not bool
            or (value['rule'] is not None and type(value['rule']) is not str)
            or (value['reason'] is not None and type(value['reason']) is not str)
            or value['rule'] not in (None, 'ignoreFiles', 'syncOnlyRegEx', 'syncIgnoreRegEx')
            or (value['allowed'] and (not value['writer_supported'] or value['rule'] is not None))
            or (value['writer_supported'] and (value['reason'] is not None
                or value['allowed'] != (value['rule'] is None)))
            or (not value['writer_supported'] and (value['allowed'] or not value['reason']))):
        raise EngineFailure('malformed-output')
    # Never reflect runner exception text into a client-facing error.
    if not value['writer_supported']:
        logger.warning('Selection runner rejected pattern: %s', value['reason'])
        value = {**value, 'reason': 'Invalid device selection pattern'}
    return TargetVerdict(**value)


def run(args, **kwargs):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, **kwargs)
    except FileNotFoundError as exc:
        raise EngineFailure('node-missing') from exc
    except subprocess.TimeoutExpired as exc:
        logger.warning('Selection engine timeout', exc_info=True)
        raise EngineFailure('timeout') from exc
    except OSError as exc:
        logger.warning('Selection engine launch failed', exc_info=True)
        raise EngineFailure('node-missing') from exc
    if result.returncode:
        logger.warning('Selection engine exit %s', result.returncode)
        raise EngineFailure(f'nonzero-exit (status {result.returncode})')
    return result.stdout


def evaluate(payload):
    try:
        return validate_response(json.loads(run(['node', str(RUNNER)], input=json.dumps(payload))))
    except (ValueError, UnicodeError) as exc:
        raise EngineFailure('malformed-output') from exc


class SelectionWorker:
    """One serialized process, bounded pipe readers, and bounded request time.

    The async owner admits only one evaluation before entering a worker thread.
    A killed or broken process is discarded; the next request starts afresh.
    No failed request is retried (write callers receive the original cause).
    """
    def __init__(self):
        import threading
        self.process = None
        self._lock = threading.Lock()
        self._sequence = 0
        self.evaluations = 0

    def _start(self):
        import threading
        from collections import deque
        capability_version_check()
        try:
            self.process = subprocess.Popen(
                ['node', str(Path(__file__).parent / 'selection_worker.mjs')],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            raise EngineFailure('node-missing') from exc
        self._responses = queue.Queue(maxsize=1)
        responses = self._responses
        self._reader_stop = threading.Event()
        stop = self._reader_stop
        self._stderr = deque(maxlen=8)
        stderr = self.process.stderr
        chunks = self._stderr
        def read_stderr():
            try:
                while data := stderr.read(1024):
                    chunks.append(data)
            except (OSError, ValueError):
                pass
        self._stderr_reader = threading.Thread(target=read_stderr, daemon=True, name='selection-stderr')
        self._stderr_reader.start()
        stream = self.process.stdout
        def read():
            try:
                while True:
                    line = stream.readline(1024 * 1024 + 1)
                    while not stop.is_set():
                        try:
                            responses.put(line, timeout=.05)
                            break
                        except queue.Full:
                            continue
                    if stop.is_set():
                        break
                    if not line or len(line) > 1024 * 1024:
                        break
            except (OSError, ValueError):
                pass
        self._requests = queue.Queue(maxsize=1)
        requests = self._requests
        stdin = self.process.stdin
        def write():
            try:
                while not stop.is_set():
                    try:
                        data = requests.get(timeout=.05)
                    except queue.Empty:
                        continue
                    remaining = memoryview(data)
                    while remaining and not stop.is_set():
                        written = stdin.write(remaining)
                        if not written:
                            return
                        remaining = remaining[written:]
                    stdin.flush()
            except (OSError, ValueError):
                pass  # stdout EOF / deadline reports the sanitized cause.
        self._writer = threading.Thread(target=write, daemon=True, name='selection-stdin')
        self._writer.start()
        self._reader = threading.Thread(target=read, daemon=True, name='selection-stdout')
        self._reader.start()
        result = self._exchange('selftest')
        if type(result) is not dict or set(result) != {'ok'} or type(result['ok']) is not bool:
            raise EngineFailure('malformed-output')
        if not result['ok']:
            raise EngineFailure('runner-selftest-failed')

    def _exchange(self, op, payload=None):
        self._sequence += 1
        request_id = self._sequence
        try:
            self._requests.put_nowait((json.dumps(dict(id=request_id, op=op, payload=payload)) + '\n').encode())
            line = self._responses.get(timeout=TIMEOUT)
        except queue.Empty as exc:
            raise EngineFailure('timeout') from exc
        except (BrokenPipeError, OSError) as exc:
            status = self.process.poll()
            raise EngineFailure(f'nonzero-exit (status {status})') from exc
        if not line:
            try:
                status = self.process.wait(timeout=1)
            except subprocess.TimeoutExpired as exc:
                raise EngineFailure('timeout') from exc
            raise EngineFailure(f'nonzero-exit (status {status})')
        try:
            response = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise EngineFailure('malformed-output') from exc
        if (len(line) > 1024 * 1024 or type(response) is not dict
                or set(response) != {'id', 'result'} or type(response['id']) is not int
                or response['id'] != request_id):
            raise EngineFailure('malformed-output')
        return response['result']

    def evaluate(self, payload):
        with self._lock:
            try:
                if self.process is None or self.process.poll() is not None:
                    self._stop()
                    self._start()
                self.evaluations += 1
                return validate_response(self._exchange('evaluate', payload))
            except EngineFailure:
                logger.warning('Selection worker failed; stderr=%s',
                               b''.join(getattr(self, '_stderr', ())).decode('utf-8', 'replace'),
                               exc_info=True)
                self._stop()
                raise

    def _stop(self):
        process = self.process
        if process is None:
            return
        if hasattr(self, '_reader_stop'):
            self._reader_stop.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # Drain the single-slot queue to let the reader publish EOF and finish.
        if hasattr(self, '_responses'):
            try:
                self._responses.get_nowait()
            except queue.Empty:
                pass
        if hasattr(self, '_reader'):
            self._reader.join(timeout=1)
        if hasattr(self, '_writer'):
            self._writer.join(timeout=1)
        if hasattr(self, '_stderr_reader'):
            self._stderr_reader.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
        self.process = None

    def close(self):
        with self._lock:
            self._stop()


def capability_version_check():
    version = run(['node', '--version']).strip()
    match = re.fullmatch(r'v(\d+)\.\d+\.\d+(?:[-+][\w.-]+)?', version)
    if match is None:
        raise EngineFailure('malformed-output')
    if int(match[1]) < 22:
        raise EngineFailure('node-version-too-old')
