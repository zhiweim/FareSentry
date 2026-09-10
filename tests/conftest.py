import pytest
import requests
from botocore.client import BaseClient
from botocore.httpsession import URLLib3Session


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed if a test forgets to mock a Requests call."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Real HTTP requests are forbidden in unit tests")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key-not-a-real-credential")


@pytest.fixture(autouse=True)
def block_real_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block AWS API calls and credential-service HTTP, including metadata."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Real AWS calls are forbidden in unit tests")

    monkeypatch.setattr(BaseClient, "_make_api_call", blocked)
    monkeypatch.setattr(URLLib3Session, "send", blocked)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
