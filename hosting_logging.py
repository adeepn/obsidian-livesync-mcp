"""Closed diagnostics for third-party HTTP hosting code, installed before use."""
import logging
import threading

from diagnostic_hygiene import cause_name

_LOCK = threading.Lock()
_STANDARD_KEYS = frozenset(logging.LogRecord('', 0, '', 0, '', (), None).__dict__) | {'message', 'asctime', '_closed_hosting_record'}
_PREFIXES = ('mcp.', 'sse_starlette.', 'uvicorn.', 'hypercorn.', 'httpx', 'httpcore')


class HostingLogFilter(logging.Filter):
    def filter(self, record):
        # Installed mcp 1.30.0 transport_security.py:71,93,104 interpolates
        # headers; sse.py:261,281 and lowlevel/server.py:682,713 log request
        # data; fastmcp/server.py:410,1112 logs resource/prompt exceptions.
        # Do not render third-party messages, arguments, stack or exceptions.
        if (record.name == 'mcp' or record.name.startswith(_PREFIXES)
                or '/site-packages/mcp/' in record.pathname):
            # SDK LoggerAdapters add extras after factory construction.
            for key in tuple(record.__dict__):
                if key not in _STANDARD_KEYS:
                    del record.__dict__[key]
            if getattr(record, '_closed_hosting_record', False):
                return True
            record._closed_hosting_record = True
            exception = record.exc_info[1] if record.exc_info else None
            cause = cause_name(exception) if exception is not None else 'none'
            level = logging.getLevelName(record.levelno)
            if level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
                level = 'OTHER'
            record.msg = f'sdk_event logger={record.name} level={level} cause={cause}'
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            record.__dict__.pop('message', None)
        return True


_FILTER = HostingLogFilter()


def install_hosting_log_filters():
    """Cover loaded SDK loggers and future lazy loggers before root handlers.

    Logger filters do not propagate from parents. The factory closes the same
    boundary for future imports, independently of handler configuration.
    """
    with _LOCK:
        for name in tuple(logging.Logger.manager.loggerDict):
            if name == 'mcp' or name.startswith(_PREFIXES):
                logger = logging.getLogger(name)
                if _FILTER not in logger.filters:
                    logger.addFilter(_FILTER)
        previous = logging.getLogRecordFactory()
        if getattr(previous, '_closed_hosting_records', False):
            return

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            _FILTER.filter(record)
            if getattr(record, "_closed_hosting_record", False):
                logger = logging.getLogger(record.name)
                if _FILTER not in logger.filters:
                    logger.addFilter(_FILTER)
            return record

        factory._closed_hosting_records = True
        logging.setLogRecordFactory(factory)
