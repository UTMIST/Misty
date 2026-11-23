from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from contracts.github_oauth import GitHubOAuthError
from src.github_linking import GitHubLinking
from src.sources.github_vault import GitHubVault


class Provider:
    github_id = 77
    login = "before-rename"
    failure = False
    calls = 0

    def authorize_url(self, state, challenge):
        return f"https://github.com/login/oauth/authorize?state={state}&code_challenge={challenge}"

    def exchange(self, code, verifier):
        self.calls += 1
        return dict(
            access_token="VERY-SECRET-ACCESS",
            refresh_token="VERY-SECRET-REFRESH",
            expires_at=2000,
            refresh_expires_at=3000,
        )

    def profile(self, token):
        if self.failure:
            raise GitHubOAuthError("reauthorization_required")
        return dict(github_id=self.github_id, login=self.login)

    def refresh(self, token):
        if self.failure:
            raise GitHubOAuthError("reauthorization_required")
        return self.exchange("code", "verifier")

    def revoke(self, token):
        self.calls += 1


class Directory:
    valid = True
    records = {}

    def __init__(self):
        self.records = {}

    def validate(self, person_id, discord_id):
        if not self.valid:
            raise GitHubOAuthError("directory_binding_rejected")

    def verify(self, person_id, discord_id, **profile):
        self.records[person_id] = profile

    def unlink(self, person_id):
        self.records.pop(person_id, None)


@pytest.fixture
def system(tmp_path):
    path = tmp_path / "vault.sqlite"
    key = Fernet.generate_key().decode()
    vault = GitHubVault(str(path), key)
    provider, directory, now = Provider(), Directory(), [1000]
    service = GitHubLinking(vault, provider, directory, clock=lambda: now[0])
    return service, provider, directory, now, path, key


def begin(service, person):
    result = service.start(person, "123")
    return parse_qs(urlsplit(result["authorize_url"]).query)["state"][0]


def link(service, person):
    result = service.callback(begin(service, person), "code")
    return service.confirm(person, "123", result["confirmation_code"])


def test_encrypted_persistent_link_rename_and_single_use(system):
    service, provider, directory, now, path, key = system
    person = str(uuid4())
    state = begin(service, person)
    result = service.callback(state, "code")
    assert result["status"] == "awaiting_confirmation"
    assert not directory.records
    service.confirm(person, "123", result["confirmation_code"])
    with pytest.raises(GitHubOAuthError, match="consumed"):
        service.callback(state, "code")
    assert provider.calls == 1
    provider.login = "after-rename"
    restarted = GitHubLinking(
        GitHubVault(str(path), key), provider, directory, clock=lambda: now[0]
    )
    assert restarted.status(person, "123")["login"] == "after-rename"
    assert directory.records[person]["github_id"] == 77
    raw = path.read_bytes()
    assert b"VERY-SECRET" not in raw and state.encode() not in raw


def test_expiry_duplicate_and_explicit_account_switch(system):
    service, provider, directory, now, path, key = system
    a, b = str(uuid4()), str(uuid4())
    expired = begin(service, a)
    now[0] += 301
    with pytest.raises(GitHubOAuthError, match="expired"):
        service.callback(expired, "code")
    link(service, a)
    with pytest.raises(GitHubOAuthError, match="already_linked"):
        service.callback(begin(service, b), "code")
    provider.github_id = 88
    with pytest.raises(GitHubOAuthError, match="unlink_before"):
        link(service, a)
    service.unlink(a, "123")
    link(service, a)
    assert service.status(a, "123")["github_id"] == 88


def test_binding_change_and_unlink_cancel_pending_callback(system):
    service, provider, directory, now, path, key = system
    person = str(uuid4())
    state = begin(service, person)
    directory.valid = False
    with pytest.raises(GitHubOAuthError, match="binding_rejected"):
        service.callback(state, "code")
    directory.valid = True
    state = begin(service, person)
    service.unlink(person, "123")
    with pytest.raises(GitHubOAuthError, match="binding_changed"):
        service.callback(state, "code")
    assert not directory.records


def test_refresh_failure_and_revocation_remove_verified_identity(system):
    service, provider, directory, now, path, key = system
    person = str(uuid4())
    link(service, person)
    now[0] = 1990
    provider.failure = True
    assert service.status(person, "123")["status"] == "reauthorization_required"
    assert not directory.records
    provider.failure = False
    link(service, person)
    service.revoked(77)
    assert service.status(person, "123")["status"] == "revoked"
    assert not directory.records


def test_confirmation_requires_original_discord_and_browser_code(system):
    service, provider, directory, now, path, key = system
    person = str(uuid4())
    result = service.callback(begin(service, person), "code")
    assert service.status(person, "123")["status"] == "awaiting_confirmation"
    with pytest.raises(GitHubOAuthError, match="confirmation_unavailable"):
        service.confirm(person, "456", result["confirmation_code"])
    for _ in range(5):
        with pytest.raises(GitHubOAuthError, match="confirmation_invalid"):
            service.confirm(person, "123", "wrong")
    with pytest.raises(GitHubOAuthError, match="confirmation_expired"):
        service.confirm(person, "123", result["confirmation_code"])
    assert not directory.records
