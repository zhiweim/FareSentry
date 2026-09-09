import pytest
import requests


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed if a test forgets to mock a Requests call."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Real HTTP requests are forbidden in unit tests")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key-not-a-real-credential")
