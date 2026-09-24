"""Outgoing mailbox of the web server (verification codes).

Registration, password reset and code login all depend on this service, so the
tests pin the contract the administrator relies on: settings are validated
against the real server before anything is stored, an unconfigured or corrupted
configuration never sends, and the delivered message carries the code.
"""

import json
import os
import smtplib
import stat

import pytest

from PyFlow.transfer_web.web_backend.mail_service import (
    ENCRYPTION_MODES,
    MailService,
    normalize_config,
)


class FakeSmtp:
    """Stand-in for an authenticated SMTP session."""

    def __init__(self, fail_send=False):
        self.messages = []
        self.fail_send = fail_send
        self.quit_called = False

    def send_message(self, message):
        if self.fail_send:
            raise smtplib.SMTPException("mailbox unavailable")
        self.messages.append(message)

    def quit(self):
        self.quit_called = True

    def close(self):
        self.quit_called = True


def valid_config(**overrides):
    config = {
        "host": "smtp.example.com",
        "port": 465,
        "username": "bot@example.com",
        "password": "authorization-code",
        "from": "PyFlow <bot@example.com>",
        "encryption": "ssl",
    }
    config.update(overrides)
    return config


@pytest.fixture
def service(tmp_path):
    return MailService(str(tmp_path / "email_config.json"))


def test_normalize_fills_defaults_and_checks_every_field():
    minimal = {"host": "h", "port": "587", "username": "u@x.com", "password": "p"}
    assert normalize_config(minimal) == {
        "host": "h",
        "port": 587,
        "username": "u@x.com",
        "password": "p",
        "from": "u@x.com",  # the sender defaults to the mailbox account
        "encryption": "ssl",
    }
    missing_sender = {"host": "h", "port": 465, "username": "x", "password": "p", "from": ""}
    for bad in [
        valid_config(host=""),
        valid_config(username=""),
        valid_config(password=""),
        missing_sender,  # the sender falls back to the username, which is not an address
        valid_config(port="not-a-port"),
        valid_config(port=0),
        valid_config(port=70000),
        valid_config(encryption="plain"),
    ]:
        with pytest.raises(ValueError):
            normalize_config(bad)


def test_encryption_modes_are_the_three_supported_ones():
    assert ENCRYPTION_MODES == ("ssl", "starttls", "none")


def test_service_is_disabled_without_a_configuration(service):
    assert service.is_enabled() is False
    assert service.get_config() is None
    with pytest.raises(ValueError, match="not configured"):
        service.send_code("alice@example.com", "123456", "register", 300)


def test_configure_validates_stores_and_enables_sending(service, tmp_path, monkeypatch):
    session = FakeSmtp()
    monkeypatch.setattr(service, "_connect", lambda config: session)

    stored = service.configure(valid_config())
    assert stored["port"] == 465
    assert service.is_enabled() is True
    assert service.get_config()["host"] == "smtp.example.com"
    assert session.quit_called is True  # the probe connection is closed again

    written = json.loads((tmp_path / "email_config.json").read_text(encoding="utf-8"))
    assert written == stored
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "email_config.json").stat().st_mode) == 0o600


def test_configure_rejects_settings_the_server_refuses(service, tmp_path, monkeypatch):
    def refuse(config):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(service, "_connect", refuse)
    with pytest.raises(ValueError, match="rejected"):
        service.configure(valid_config())
    assert service.is_enabled() is False
    assert not (tmp_path / "email_config.json").exists()  # nothing is stored


def test_a_stored_configuration_is_loaded_again(tmp_path, monkeypatch):
    path = str(tmp_path / "email_config.json")
    first = MailService(path)
    monkeypatch.setattr(first, "_connect", lambda config: FakeSmtp())
    first.configure(valid_config())

    assert MailService(path).is_enabled() is True


def test_a_corrupted_configuration_leaves_the_service_disabled(tmp_path):
    path = tmp_path / "email_config.json"
    path.write_text("{ not json", encoding="utf-8")
    assert MailService(str(path)).is_enabled() is False

    path.write_text(json.dumps(valid_config(host="")), encoding="utf-8")
    assert MailService(str(path)).is_enabled() is False


def test_send_code_delivers_the_code_with_its_purpose(service, monkeypatch):
    session = FakeSmtp()
    monkeypatch.setattr(service, "_connect", lambda config: session)
    service.configure(valid_config())

    service.send_code("alice@example.com", "654321", "reset_password", 300)
    message = session.messages[0]
    assert message["To"] == "alice@example.com"
    assert message["From"] == "PyFlow <bot@example.com>"
    assert "654321" in message.get_content()
    assert "password change" in message.get_content()
    assert "5 minutes" in message.get_content()
    assert session.quit_called is True


def test_send_code_reports_delivery_failures(service, monkeypatch):
    monkeypatch.setattr(service, "_connect", lambda config: FakeSmtp())
    service.configure(valid_config())
    monkeypatch.setattr(service, "_connect", lambda config: FakeSmtp(fail_send=True))

    with pytest.raises(ValueError, match="sending the verification code"):
        service.send_code("alice@example.com", "654321", "login", 300)
