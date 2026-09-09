"""Credential redaction at the source of Requests/urllib3 log records."""

import logging
import os
import re
from urllib.parse import quote, quote_plus

# Match query parameters as well as dictionary/debug representations.
_API_KEY = re.compile(
    r"""(?i)(\bapi_key['"]?\s*[:=]\s*)(?:'[^']*'|"[^"]*"|[^&\s'"\)\]}>,]+)"""
)
_URL_LOGGERS = (
    "urllib3",
    "urllib3.connection",
    "urllib3.connectionpool",
    "urllib3.poolmanager",
    "urllib3.response",
    "urllib3.util.retry",
    "urllib3.http2.connection",
    "urllib3.contrib.pyopenssl",
)


def _redact(text: str) -> str:
    # Read at emission time rather than keeping credentials in a filter object.
    secret = os.environ.get("SERPAPI_API_KEY", "").strip()
    if secret:
        variants = {
            secret,
            quote(secret, safe=""),
            quote_plus(secret),
            repr(secret)[1:-1],
        }
        for value in sorted(variants, key=len, reverse=True):
            text = text.replace(value, "REDACTED")
    return _API_KEY.sub(r"\1REDACTED", text)


class _CredentialFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Clear arguments too: handlers may inspect records beyond getMessage().
        record.msg = _redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = _redact(record.exc_text)
        if record.stack_info:
            record.stack_info = _redact(record.stack_info)
        return True


_FILTER = _CredentialFilter()


def protect_transport_logs() -> None:
    """Install a persistent, idempotent filter without changing logging levels.

    Logger filters are not inherited, so protect the emitting modules directly.
    Legacy Requests-vendored urllib3 logger names are covered as well.
    """
    for name in ("requests", *_URL_LOGGERS):
        logging.getLogger(name).addFilter(_FILTER)
    for name in _URL_LOGGERS:
        logging.getLogger(f"requests.packages.{name}").addFilter(_FILTER)
