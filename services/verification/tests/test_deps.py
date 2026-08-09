from src.api import deps
from src.config import Settings

# The SecretStr boundary stops at get_email_sender: the senders themselves take
# plain str (ResendSender puts the key straight into an Authorization header,
# GmailSender base64-decodes the credentials). Handing them a SecretStr would
# not raise — it would just send "Bearer **********" / fail to decode at the
# first real send, in production. These tests pin the unwrap.


def _use(monkeypatch, **overrides) -> None:
    settings = Settings(**overrides)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)


def test_resend_sender_receives_plain_str_api_key(monkeypatch):
    _use(
        monkeypatch, email_backend="resend", resend_api_key="re_live_key", email_from="x@utmist.ca"
    )
    sender = deps.get_email_sender()

    assert sender._api_key == "re_live_key"
    assert isinstance(sender._api_key, str)


def test_gmail_sender_receives_plain_str_credentials(monkeypatch):
    _use(
        monkeypatch,
        email_backend="gmail",
        gmail_sender="noreply@utmist.ca",
        gmail_credentials_json="base64creds",
    )
    sender = deps.get_email_sender()

    assert sender._credentials_json_b64 == "base64creds"
    assert isinstance(sender._credentials_json_b64, str)
