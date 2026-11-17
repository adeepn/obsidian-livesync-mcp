"""Disposable spawn workers for untrusted regular expressions; no server imports."""
import multiprocessing
import re

DEFAULT_TIMEOUT = 2.0
MAX_OUTPUT_BYTES = 10 * 1024 * 1024


class RegexTimeoutError(ValueError):
    """The worker exceeded its hard deadline."""


def _worker(connection, pattern, text, op, replacement, flags):
    try:
        regex = re.compile(pattern, flags)
        if op == 'findall':
            matches = []
            for match in regex.finditer(text):
                groups = match.groups(default="")
                matches.append(match.group() if not groups else
                               groups[0] if len(groups) == 1 else groups)
                if len(matches) == 10:
                    break
            result = {'kind': op, 'matches': matches}
        elif op == 'search':
            match = regex.search(text)
            result = {'kind': op, 'context': None if match is None else
                      text[max(0, match.start() - 50):match.end() + 50]}
        elif op == 'subn':
            pieces, end, size, count = [], 0, 0, 0
            for match in regex.finditer(text):
                part = text[end:match.start()]
                size += len(part.encode('utf-8')) + len(replacement.encode('utf-8'))
                if size > MAX_OUTPUT_BYTES:
                    raise ValueError('regex replacement output too large')
                pieces.extend((part, replacement))
                end, count = match.end(), count + 1
            tail = text[end:]
            if size + len(tail.encode('utf-8')) > MAX_OUTPUT_BYTES:
                raise ValueError('regex replacement output too large')
            pieces.append(tail)
            result = {'kind': op, 'result': ''.join(pieces), 'count': count}
        else:
            raise ValueError('unknown regex operation')
        connection.send(('ok', result))
    except re.error:
        connection.send(('regex', 'Invalid regular expression.'))
    except Exception as error:
        connection.send(('error', str(error)))
    finally:
        connection.close()


def apply(pattern: str, text: str, op: str, timeout: float | None = None,
          *, replacement: str = '', flags: int = re.IGNORECASE) -> dict:
    """Run an operation in a child, terminating and reaping it on every exit."""
    context = multiprocessing.get_context('spawn')
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_worker,
                              args=(writer, pattern, text, op, replacement, flags))
    try:
        process.start()
        writer.close()
        if not reader.poll(DEFAULT_TIMEOUT if timeout is None else timeout):
            raise RegexTimeoutError('regex pattern too complex or input too large')
        try:
            kind, result = reader.recv()
        except EOFError:
            raise ValueError('regex worker failed') from None
        if kind == 'regex':
            raise re.error(result)
        if kind != 'ok':
            raise ValueError(result)
        return result
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join()
            process.close()
        reader.close()
        writer.close()
