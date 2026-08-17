import pytest

from src.api.ratelimit import key_rate_limit_counter


@pytest.fixture(autouse=True)
def _reset_key_rate_limit():
    """The per-key quota is process-wide, so it would otherwise carry across tests.

    Every test builds its key with the same name, which means without this they
    all share one bucket and the suite starts failing once it grows past
    KEY_LIMIT requests — a confusing failure a long way from its cause.
    """
    key_rate_limit_counter.clear()
    yield
    key_rate_limit_counter.clear()
