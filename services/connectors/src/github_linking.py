"""OAuth lifecycle. Vendor and durable-store implementations are injected at wiring."""

import base64
import hashlib
import hmac
import secrets
import time

from contracts.github_oauth import (
    GitHubCredentialVault,
    GitHubDirectory,
    GitHubOAuthError,
    GitHubOAuthProvider,
)


def challenge(verifier):
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def state_key(state):
    return "state:" + hashlib.sha256(state.encode()).hexdigest()


class GitHubLinking:
    def __init__(
        self,
        vault: GitHubCredentialVault,
        provider: GitHubOAuthProvider,
        directory: GitHubDirectory,
        *,
        clock=time.time,
    ):
        self.vault, self.provider, self.directory, self.clock = vault, provider, directory, clock

    def start(self, person_id, discord_id):
        self.directory.validate(person_id, discord_id)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with self.vault.transaction() as tx:
            # Remove expired states and replace this person's prior pending link.
            for key in tx.keys("state:"):
                row = tx.get(key)
                if row["expires_at"] <= self.clock() or row["person_id"] == person_id:
                    tx.delete(key)
            if len(tx.keys("state:")) >= 1000:
                raise GitHubOAuthError("link_capacity_reached")
            current = tx.get("person:" + person_id) or {"generation": 0}
            tx.set(
                state_key(state),
                dict(
                    person_id=person_id,
                    discord_id=discord_id,
                    generation=current["generation"],
                    verifier=verifier,
                    expires_at=self.clock() + 300,
                ),
            )
        return dict(
            authorize_url=self.provider.authorize_url(state, challenge(verifier)), expires_in=300
        )

    def callback(self, state, code):
        # Consume in its own committed transaction, even on expired/error callbacks.
        with self.vault.transaction() as tx:
            pending = tx.get(state_key(state))
            tx.delete(state_key(state))
        if not pending or pending["expires_at"] <= self.clock() or not code:
            raise GitHubOAuthError("link_expired_or_consumed")
        person_id, discord_id = pending["person_id"], pending["discord_id"]
        tokens = self.provider.exchange(code, pending["verifier"])
        profile = self.provider.profile(tokens["access_token"])
        with self.vault.transaction() as tx:
            old = tx.get("person:" + person_id) or {"generation": 0}
            if old["generation"] != pending["generation"]:
                raise GitHubOAuthError("link_binding_changed")
            if old.get("github_id") and old["github_id"] != profile["github_id"]:
                raise GitHubOAuthError("unlink_before_switching_accounts")
            for key in tx.keys("person:"):
                other = tx.get(key)
                if (
                    other.get("status") == "awaiting_confirmation"
                    and other["confirmation_expires_at"] <= self.clock()
                ):
                    tx.set(key, dict(generation=other["generation"] + 1, status="unlinked"))
                    continue
                if key != "person:" + person_id and other.get("github_id") == profile["github_id"]:
                    raise GitHubOAuthError("github_account_already_linked")
            self.directory.validate(person_id, discord_id)
            confirmation = secrets.token_urlsafe(24)
            tx.set(
                "person:" + person_id,
                dict(
                    **tokens,
                    **profile,
                    discord_id=discord_id,
                    generation=old["generation"] + 1,
                    status="awaiting_confirmation",
                    confirmation_digest=hashlib.sha256(confirmation.encode()).hexdigest(),
                    confirmation_expires_at=self.clock() + 300,
                    confirmation_attempts=0,
                ),
            )
        return {"status": "awaiting_confirmation", "confirmation_code": confirmation}

    def confirm(self, person_id, discord_id, code):
        self.directory.validate(person_id, discord_id)
        failed = False
        with self.vault.transaction() as tx:
            row = tx.get("person:" + person_id)
            if (
                not row
                or row.get("status") != "awaiting_confirmation"
                or row["discord_id"] != discord_id
            ):
                raise GitHubOAuthError("confirmation_unavailable")
            if row["confirmation_expires_at"] <= self.clock() or row["confirmation_attempts"] >= 5:
                raise GitHubOAuthError("confirmation_expired")
            row["confirmation_attempts"] += 1
            if not hmac.compare_digest(
                row["confirmation_digest"], hashlib.sha256(code.encode()).hexdigest()
            ):
                failed = True
                tx.set("person:" + person_id, row)
            else:
                profile = self.provider.profile(row["access_token"])
                if profile["github_id"] != row["github_id"]:
                    raise GitHubOAuthError("github_identity_changed")
                self.directory.verify(person_id, discord_id, **profile)
                row.update(profile, status="linked")
                for key in (
                    "confirmation_digest",
                    "confirmation_expires_at",
                    "confirmation_attempts",
                ):
                    row.pop(key)
                tx.set("person:" + person_id, row)
        if failed:
            raise GitHubOAuthError("confirmation_invalid")
        return {"status": "linked", "github_id": row["github_id"], "login": row["login"]}

    def status(self, person_id, discord_id):
        self.directory.validate(person_id, discord_id)
        failure = None
        with self.vault.transaction() as tx:
            row = tx.get("person:" + person_id)
            if not row or row.get("status") != "linked":
                return {"status": "unlinked" if not row else row.get("status", "unlinked")}
            if row["discord_id"] != discord_id:
                raise GitHubOAuthError("directory_binding_rejected")
            try:
                if row["expires_at"] <= self.clock() + 60:
                    if row["refresh_expires_at"] <= self.clock():
                        raise GitHubOAuthError("reauthorization_required")
                    row.update(self.provider.refresh(row["refresh_token"]))
                profile = self.provider.profile(row["access_token"])
                if profile["github_id"] != row["github_id"]:
                    raise GitHubOAuthError("github_identity_changed")
                self.directory.verify(person_id, discord_id, **profile)
                row.update(profile)
            except GitHubOAuthError:
                # Includes ambiguous refresh: never retry a potentially rotated token.
                row = dict(
                    github_id=row["github_id"],
                    discord_id=discord_id,
                    generation=row["generation"] + 1,
                    status="reauthorization_required",
                )
                failure = True
            tx.set("person:" + person_id, row)
        if failure:
            self.directory.unlink(person_id)
            return {"status": "reauthorization_required"}
        return {"status": "linked", "github_id": row["github_id"], "login": row["login"]}

    def unlink(self, person_id, discord_id):
        self.directory.validate(person_id, discord_id)
        # Clear locally before remote cleanup, including all pending callbacks.
        with self.vault.transaction() as tx:
            old = tx.get("person:" + person_id) or {"generation": 0}
            tx.set("person:" + person_id, dict(generation=old["generation"] + 1, status="unlinked"))
        self.directory.unlink(person_id)
        if old.get("access_token"):
            self.provider.revoke(old["access_token"])
        return {"status": "unlinked"}

    def revoked(self, github_id):
        affected = []
        with self.vault.transaction() as tx:
            for key in tx.keys("person:"):
                row = tx.get(key)
                if row.get("github_id") == github_id:
                    tx.set(
                        key,
                        dict(
                            github_id=github_id, generation=row["generation"] + 1, status="revoked"
                        ),
                    )
                    affected.append(key.split(":", 1)[1])
        for person_id in affected:
            self.directory.unlink(person_id)
