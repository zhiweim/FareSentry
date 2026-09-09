"""Credential redaction at the source of Requests/urllib3 log records."""

import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote, quote_plus

# Match query parameters as well as dictionary/debug representations.
_CREDENTIAL_PARAMETER = re.compile(
    r"""(?i)(\b(?:api_key|departure_token)['"]?\s*[:=]\s*)"""
    r"""(?:'[^']*'|"[^"]*"|[^&\s'"\)\]}>,]+)"""
)
_ACTIVE_SECRETS: ContextVar[tuple[str, ...]] = ContextVar(
    "transport_secrets", default=()
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
    secrets = (*_ACTIVE_SECRETS.get(), os.environ.get("SERPAPI_API_KEY", "").strip())
    variants: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        variants.update(
            {
                secret,
                quote(secret, safe=""),
                quote_plus(secret),
                repr(secret)[1:-1],
            }
        )
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, "REDACTED")
    return _CREDENTIAL_PARAMETER.sub(r"\1REDACTED", text)


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


@contextmanager
def redact_transport_secrets(*secrets: str | None) -> Iterator[None]:
    """Redact bare/encoded secrets during a request without retaining tokens.

    Context-local state isolates concurrent requests; parameter-based redaction
    remains active after the scope ends, including for delayed URL log messages.
    """
    protect_transport_logs()
    state = _ACTIVE_SECRETS.set(
        (*_ACTIVE_SECRETS.get(), *(secret for secret in secrets if secret))
    )
    try:
        yield
    finally:
        _ACTIVE_SECRETS.reset(state)
