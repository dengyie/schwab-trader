"""The legacy ?token= must not survive into a log record.

uvicorn logs WebSocket handshakes from its protocol layer, so the only place
the credential can be removed is the LogRecord itself.
"""
import logging

from app.main import _RedactWsQueryFilter, _configure_logging


def make_record(message, args):
    return logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, message, args, None
    )


def test_ws_access_record_loses_its_query_string():
    record = make_record(
        '%s - "WebSocket %s" [accepted]', ("127.0.0.1:5000", "/ws?token=secret")
    )

    _RedactWsQueryFilter().filter(record)

    assert record.getMessage() == '127.0.0.1:5000 - "WebSocket /ws" [accepted]'


def test_ws_close_record_is_scrubbed_too():
    record = make_record(
        '%s - "WebSocket %s" 4401', ("127.0.0.1:5000", "/ws?token=secret")
    )

    _RedactWsQueryFilter().filter(record)

    assert "secret" not in record.getMessage()
    assert record.getMessage().endswith('"WebSocket /ws" 4401')


def test_ws_record_without_query_is_unchanged():
    record = make_record('%s - "WebSocket %s" [accepted]', ("127.0.0.1:5000", "/ws"))

    _RedactWsQueryFilter().filter(record)

    assert record.getMessage() == '127.0.0.1:5000 - "WebSocket /ws" [accepted]'


def test_non_ws_records_are_untouched():
    record = make_record("Application startup complete.", None)

    assert _RedactWsQueryFilter().filter(record) is True
    assert record.getMessage() == "Application startup complete."


def test_configure_logging_installs_the_filter_once():
    _configure_logging()
    _configure_logging()

    filters = logging.getLogger("uvicorn.error").filters
    installed = [f for f in filters if isinstance(f, _RedactWsQueryFilter)]
    assert len(installed) == 1
