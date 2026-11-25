import asyncio
import pytest
from tests.test_target_filter import backend


async def test_worker_reused_restarted_and_closed(backend):
    b, _ = backend
    b.selection_only = 'md$'
    assert (await b.selection_for('a.md')).allowed
    worker = b._selection_worker
    pid = worker.process.pid
    results = await asyncio.gather(*(b.selection_for(f'{i}.txt' if i % 2 else f'{i}.md') for i in range(30)))
    assert [v.allowed for v in results] == [not i % 2 for i in range(30)]
    assert worker.process.pid == pid
    assert (await b.backend_info())['selection_engine_state'] == 'ready'
    worker.process.kill()
    worker.process.wait()
    assert (await b.selection_for('restart.md')).allowed
    assert worker.process.pid != pid
    process = worker.process
    await b.close()
    assert process.poll() is not None


async def test_cache_hit_rules_invalidate_and_attachment_once(backend, monkeypatch):
    b, _ = backend
    b.selection_only = 'md$'
    await b.selection_for('a.md')
    worker = b._selection_worker
    count = worker.evaluations
    await b.selection_for('a.md')
    assert worker.evaluations == count
    b.selection_only = 'txt$'
    assert not (await b.selection_for('a.md')).allowed
    # Changed configuration is capability-checked before evaluating the path.
    assert worker.evaluations == count + 2
    calls = []
    original = b.selection_for
    async def track(path):
        calls.append(path)
        return await original(path)
    monkeypatch.setattr(b, 'selection_for', track)
    await b.write_attachment('a.txt', b'yes', create=True)
    assert calls == ['a.txt']


async def test_cache_bounded_and_retained_by_journal_dedupe_reset(backend):
    b, _ = backend
    b.selection_only = 'md$'
    assert hasattr(b, 'selection_cache_size')
    b.selection_cache_entries = 16
    for i in range(10000):
        await b.selection_for(f'{i}.md')
    assert b.selection_cache_size() == 16
    if getattr(b, '_store', None):
        b._store.reset()
        assert b.selection_cache_size() == 16


async def test_rename_checks_each_path_once(backend, monkeypatch):
    b, _ = backend
    await b.write_note('old.md', 'x', create=True)
    b.selection_only = 'md$'
    calls = []
    original = b.selection_for
    async def track(path):
        calls.append(path)
        return await original(path)
    monkeypatch.setattr(b, 'selection_for', track)
    await b.rename_note('old.md', 'new.md')
    assert sorted(calls) == ['new.md', 'old.md']


async def test_concurrent_evaluations_have_dedicated_limit(backend, monkeypatch):
    import threading
    import time
    from selection_engine import SelectionWorker
    b, _ = backend
    b.selection_only = 'md$'
    original = SelectionWorker.evaluate
    active = maximum = 0
    lock = threading.Lock()
    def evaluate(self, payload):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0)
            return original(self, payload)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(SelectionWorker, 'evaluate', evaluate)
    await asyncio.gather(*(b.selection_for(f'{i}.md') for i in range(80)))
    assert maximum == 1


def test_worker_matches_unchanged_pinned_runner():
    import json
    import subprocess
    from pathlib import Path
    from selection_engine import SelectionWorker
    root = Path(__file__).resolve().parents[1]
    fixture = json.loads((root / 'tests/fixtures/selection.json').read_text())
    inputs = [dict(row) for row in fixture['regex']]
    inputs += [dict(path=row['path'], only_patterns='', ignore_patterns='',
                    ignore_files=row['files'], ignore_names=row['names']) for row in fixture['ignores']]
    expected = json.loads(subprocess.run(['node', str(root / 'vendor/selection/runner.mjs')],
        input=json.dumps(inputs), text=True, capture_output=True, check=True).stdout)
    worker = SelectionWorker()
    try:
        for request, oracle in zip(inputs, expected, strict=True):
            actual = worker.evaluate(request)
            assert actual.allowed == oracle['allowed']
            assert actual.rule == oracle['rule']
            assert actual.writer_supported == oracle['writer_supported']
    finally:
        worker.close()


@pytest.mark.parametrize('failure', ['timeout', 'nonzero-exit (status 17)',
    'malformed-output', 'runner-selftest-failed', 'wrong-id', 'wrong-type'])
async def test_worker_transport_failures_are_clean_and_deny(backend, monkeypatch, tmp_path, failure):
    import subprocess
    import sys
    import selection_engine
    from storage_base import StorageError
    b, mutations = backend
    b.selection_only = 'md$'
    script = tmp_path / 'broken_worker.py'
    script.write_text('''import sys, json, time
for line in sys.stdin:
 r = json.loads(line)
 FAILURE = ''' + repr(failure) + '''
 if FAILURE == 'runner-selftest-failed':
  print(json.dumps({'id':r['id'], 'result':{'ok':False}}), flush=True)
 elif r['op'] == 'selftest':
  print(json.dumps({'id':r['id'], 'result':{'ok':True}}), flush=True)
 elif FAILURE == 'timeout': time.sleep(30)
 elif FAILURE.startswith('nonzero'): sys.exit(17)
 elif FAILURE == 'malformed-output': print('private payload', flush=True)
 elif FAILURE == 'wrong-id': print(json.dumps({'id':False, 'result':{}}), flush=True)
 else: print(json.dumps({'id':r['id'], 'result':{'allowed':'false'}}), flush=True)
''')
    original = subprocess.Popen
    def popen(args, *a, **k):
        if str(args[-1]).endswith('selection_worker.mjs'):
            args = [sys.executable, '-u', str(script)]
        return original(args, *a, **k)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(selection_engine, 'TIMEOUT', 5)
    before = mutations()
    with pytest.raises(StorageError) as exc:
        await b.write_note('denied.md', 'denied', create=True)
    cause = 'malformed-output' if failure in ('wrong-id', 'wrong-type') else failure
    assert cause in str(exc.value)
    assert 'private payload' not in str(exc.value)
    assert mutations() == before
    assert (await b.backend_info())['selection_engine_state'] == 'unsupported'


async def test_journal_cache_persists(backend):
    from journal_store import JournalStore
    b, _ = backend
    if getattr(b, '_store', None) is None:
        return
    b.selection_only = 'md$'
    await b.selection_for('persist.md')
    old = b._store
    directory, identity = old.state_dir, old.path_identity
    old.close()
    b._store = JournalStore(directory, identity)
    count = b._selection_worker.evaluations
    assert (await b.selection_for('persist.md')).allowed
    assert b._selection_worker.evaluations == count


def test_stalled_stdin_write_obeys_timeout(monkeypatch, tmp_path):
    import json
    import subprocess
    import sys
    import threading
    import selection_engine
    script = tmp_path / 'stalled.py'
    script.write_text("import sys,json,time\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'id':r['id'],'result':{'ok':True}}),flush=True)\ntime.sleep(30)\n")
    original = subprocess.Popen
    def popen(args, *a, **k):
        if str(args[-1]).endswith('selection_worker.mjs'):
            args = [sys.executable, '-u', str(script)]
        return original(args, *a, **k)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    worker = selection_engine.SelectionWorker()
    worker._start()
    monkeypatch.setattr(selection_engine, 'TIMEOUT', 1)
    errors = []
    def evaluate():
        try:
            worker.evaluate({'path':'a' * 20000000})
        except selection_engine.EngineFailure as exc:
            errors.append(exc.cause)
    thread = threading.Thread(target=evaluate)
    thread.start()
    try:
        thread.join(15)
        assert not thread.is_alive(), 'stdin write escaped engine deadline'
        assert errors == ['timeout']
    finally:
        if worker.process:
            worker.process.kill()
        thread.join(15)
        worker.close()


async def test_repeated_cancellation_keeps_admission_until_thread_exits(backend, monkeypatch):
    import threading
    from selection_engine import SelectionWorker
    from livesync_format import TargetVerdict
    b, _ = backend
    b.selection_only = 'md$'
    await b.selection_startup()
    started, release = threading.Event(), threading.Event()
    acknowledged = [asyncio.Event(), asyncio.Event()]
    second_attempt = asyncio.Event()
    second_saw_held = []
    first = second = None
    active = maximum = shield_calls = 0
    real_shield = asyncio.shield
    semaphore = b._selection_limit

    class ObservedPermit:
        async def __aenter__(self):
            if asyncio.current_task() is second:
                # Observe admission before releasing the worker, independently
                # of when the executor dispatches the second thread.
                second_saw_held.append(semaphore.locked())
                second_attempt.set()
            await semaphore.acquire()
            return self

        async def __aexit__(self, *args):
            semaphore.release()

        def release(self):
            semaphore.release()

    def shield(task):
        nonlocal shield_calls
        if asyncio.current_task() is first:
            shield_calls += 1
            if 2 <= shield_calls <= 3:
                # Re-entry acknowledges that each cancellation was handled.
                acknowledged[shield_calls - 2].set()
        return real_shield(task)

    def slow(self, payload):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.set()
        try:
            assert release.wait(30), 'test failed to release evaluator'
            return TargetVerdict(True)
        finally:
            active -= 1

    monkeypatch.setattr(b, '_selection_limit', ObservedPermit())
    monkeypatch.setattr(SelectionWorker, 'evaluate', slow)
    monkeypatch.setattr(asyncio, 'shield', shield)
    try:
        first = asyncio.create_task(b.selection_for('first.md'))
        assert await asyncio.to_thread(started.wait, 30)
        for ack in acknowledged:
            first.cancel()
            await asyncio.wait_for(ack.wait(), 30)
        second = asyncio.create_task(b.selection_for('second.md'))
        await asyncio.wait_for(second_attempt.wait(), 30)
        assert second_saw_held == [True], 'admission released while first worker still runs'
    finally:
        release.set()
        tasks = [task for task in (first, second) if task is not None]
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 30)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1].allowed
    assert maximum == 1
