from unittest.mock import Mock

import pytest

from faresentry.notification_models import DeliveryReceipt, NotificationMessage
from faresentry.notifications import NotificationProviderError
from faresentry.providers import ses
from scripts import send_test_email


def test_ses_mapping_and_receipt() -> None:
    client = Mock(spec=ses.SESClient)
    client.send_email.return_value = {"MessageId": "ses-message-1"}
    provider = ses.SESEmailProvider(
        sender="sender@example.com", recipient="traveler@example.com", client=client
    )
    receipt = provider.send(
        NotificationMessage(subject="Fare opportunity", body="USD 800")
    )
    assert receipt == DeliveryReceipt(
        provider="ses", provider_message_id="ses-message-1"
    )
    client.send_email.assert_called_once_with(
        Source="sender@example.com",
        Destination={"ToAddresses": ["traveler@example.com"]},
        Message={
            "Subject": {"Data": "Fare opportunity", "Charset": "UTF-8"},
            "Body": {"Text": {"Data": "USD 800", "Charset": "UTF-8"}},
        },
    )


@pytest.mark.parametrize(
    "response",
    [{}, {"MessageId": None}, {"MessageId": ""}, RuntimeError("private AWS details")],
)
def test_ses_failures_are_safe_and_never_retried(response: object) -> None:
    client = Mock(spec=ses.SESClient)
    if isinstance(response, Exception):
        client.send_email.side_effect = response
    else:
        client.send_email.return_value = response
    provider = ses.SESEmailProvider(
        sender="sender@example.com", recipient="traveler@example.com", client=client
    )
    with pytest.raises(NotificationProviderError, match="^SES email request failed$"):
        provider.send(NotificationMessage(subject="Test", body="Test"))
    client.send_email.assert_called_once()


def test_environment_configuration_and_lazy_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FARESENTRY_EMAIL_FROM", "sender@example.com")
    monkeypatch.setenv("FARESENTRY_EMAIL_TO", "traveler@example.com")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    factory = Mock()
    factory.return_value.send_email.return_value = {"MessageId": "message-1"}
    monkeypatch.setattr(ses.boto3, "client", factory)
    provider = ses.SESEmailProvider.from_environment()
    factory.assert_not_called()
    for _ in range(2):
        provider.send(NotificationMessage(subject="Test", body="Test"))
    factory.assert_called_once()
    assert factory.call_args.args == ("ses",)
    assert factory.call_args.kwargs["region_name"] == "us-west-2"
    config = factory.call_args.kwargs["config"]
    assert config.retries["total_max_attempts"] == 1
    assert config.connect_timeout == 10 and config.read_timeout == 30
    assert factory.return_value.send_email.call_count == 2


@pytest.mark.parametrize(
    "address",
    [
        "",
        "missing-at",
        "a@example.com,b@example.com",
        "a@example.com\r\nBcc: b@example.com",
    ],
)
def test_invalid_addresses_fail_before_aws(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = Mock()
    monkeypatch.setattr(ses.boto3, "client", factory)
    with pytest.raises(ValueError, match="plain ASCII"):
        ses.SESEmailProvider(sender=address, recipient="traveler@example.com")
    factory.assert_not_called()


def test_smoke_script_requires_explicit_send(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = Mock()
    monkeypatch.setattr(send_test_email.SESEmailProvider, "from_environment", factory)
    with pytest.raises(SystemExit) as error:
        send_test_email.main([])
    assert error.value.code == 2
    factory.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
def test_smoke_script_uses_fake_provider(
    failed: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = Mock()
    if failed:
        factory.return_value.send.side_effect = NotificationProviderError(
            "SES email request failed"
        )
    else:
        factory.return_value.send.return_value = DeliveryReceipt(
            provider="ses", provider_message_id="test-id"
        )
    monkeypatch.setattr(send_test_email.SESEmailProvider, "from_environment", factory)
    assert send_test_email.main(["--send"]) == (1 if failed else 0)
    factory.return_value.send.assert_called_once()
    output = capsys.readouterr()
    assert (
        ("SES email request failed" in output.err)
        if failed
        else ("test-id" in output.out)
    )
