"""Fail-closed boundary around the pinned ECMAScript selection engine."""
import json
import logging
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
            or (value['writer_supported'] and value['reason'] is not None)
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


def capability_check():
    version = run(['node', '--version']).strip()
    try:
        major = int(version.removeprefix('v').split('.')[0])
    except ValueError as exc:
        raise EngineFailure('malformed-output') from exc
    if major < 22:
        raise EngineFailure('node-version-too-old')
    verdict = evaluate(dict(path='selftest.md', only_patterns='md$', ignore_patterns='',
                            case_sensitive=True, ignore_files=None, ignore_names=[]))
    if not verdict.allowed or not verdict.writer_supported:
        raise EngineFailure('runner-selftest-failed')
