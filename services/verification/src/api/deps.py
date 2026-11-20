from functools import lru_cache

from platform_auth import InMemoryKeyStore, key_store_from_config

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from contracts.storage import VerificationStore
from src.config import get_settings
from src.email.base import EmailSender
from src.email.fake import FakeSender


@lru_cache(maxsize=1)
def _default_engine() -> Engine:
    return create_engine(get_settings().database_url, future=True, pool_pre_ping=True)


def get_storage() -> VerificationStore:
    # Lazy import: postgres adapter lands in Task 8. Tests override this dep.
    from src.storage.postgres import PostgresVerificationStore

    return PostgresVerificationStore(_default_engine())


@lru_cache(maxsize=1)
def _key_store() -> InMemoryKeyStore:
    return key_store_from_config(get_settings().consumer_keys.get_secret_value())


def get_key_store() -> InMemoryKeyStore:
    return _key_store()


def get_email_sender() -> EmailSender:
    settings = get_settings()
    if settings.email_backend == "resend":
        from src.email.resend import ResendSender

        # Unwrap at the boundary — senders take plain str; SecretStr stops here.
        return ResendSender(
            api_key=settings.resend_api_key.get_secret_value(), sender=settings.email_from
        )
    if settings.email_backend == "gmail":
        from src.email.gmail import GmailSender

        return GmailSender(
            sender=settings.gmail_sender,
            credentials_json_b64=settings.gmail_credentials_json.get_secret_value(),
        )
    return FakeSender()
