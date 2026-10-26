from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from contracts.login import LoginChallenge

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
PERSON_ID = uuid4()


def challenge(at=NOW, *, owner="compute", email="member@example.com"):
    return LoginChallenge(
        id=uuid4(), owner=owner, email=email, created_at=at, expires_at=at + timedelta(minutes=10)
    )


class LoginStorageCases:
    def test_delivery_quotas_bound_guesses_across_apps(self, adapter):
        admitted = 0
        for minute in range(5):
            item = challenge(NOW + timedelta(minutes=minute), owner=f"app-{minute}")
            assert adapter.reserve_login(item)
            assert adapter.activate_login(item.id, PERSON_ID, item.created_at)
            for _ in range(3):
                assert adapter.admit_login_attempt(item.id, item.owner, item.created_at)
                admitted += 1
            assert adapter.admit_login_attempt(item.id, item.owner, item.created_at) is None
        assert admitted == 15
        assert not adapter.reserve_login(challenge(NOW + timedelta(minutes=5), owner="other"))

    def test_concurrent_reservations_send_once(self, adapter):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: adapter.reserve_login(challenge()), range(8)))
        assert results.count(True) == 1

    def test_guess_budgets_are_independent_and_bounded_by_delivery_quotas(self, adapter):
        for minute in range(3):
            item = challenge(NOW + timedelta(minutes=minute))
            assert adapter.reserve_login(item)
            assert adapter.activate_login(item.id, PERSON_ID, item.created_at)
            for _ in range(3):
                assert adapter.admit_login_attempt(item.id, item.owner, item.created_at)
            assert adapter.admit_login_attempt(item.id, item.owner, item.created_at) is None
        item = challenge(NOW + timedelta(minutes=3), owner="another-app")
        assert adapter.reserve_login(item)
        assert adapter.activate_login(item.id, PERSON_ID, item.created_at)
        assert adapter.admit_login_attempt(item.id, item.owner, item.created_at) is not None

    def test_send_hour_and_day_budgets(self, adapter):
        for hour in range(4):
            for minute in range(5):
                item = challenge(NOW + timedelta(hours=hour, minutes=minute))
                assert adapter.reserve_login(item)
            assert not adapter.reserve_login(challenge(NOW + timedelta(hours=hour, minutes=6)))
        assert not adapter.reserve_login(challenge(NOW + timedelta(hours=4)))
        assert adapter.reserve_login(challenge(NOW + timedelta(days=1)))

    def test_resend_preserves_other_challenges_until_expiry(self, adapter):
        first = challenge()
        assert adapter.reserve_login(first)
        assert adapter.activate_login(first.id, PERSON_ID, NOW)
        second = challenge(NOW + timedelta(minutes=1))
        assert adapter.reserve_login(second)
        assert adapter.admit_login_attempt(first.id, first.owner, second.created_at)
        assert adapter.activate_login(second.id, PERSON_ID, second.created_at)
        assert adapter.admit_login_attempt(first.id, first.owner, second.created_at) is not None
        assert adapter.redeem_login(first.id, first.owner, second.created_at)
        assert not adapter.activate_login(second.id, PERSON_ID, second.expires_at)
        assert adapter.admit_login_attempt(second.id, second.owner, second.expires_at) is None

    def test_late_delivery_preserves_both_active_codes(self, adapter):
        first = challenge()
        second = challenge(NOW + timedelta(minutes=1))
        assert adapter.reserve_login(first)
        assert adapter.reserve_login(second)
        assert adapter.activate_login(second.id, PERSON_ID, second.created_at)
        assert adapter.activate_login(first.id, PERSON_ID, second.created_at)
        assert adapter.admit_login_attempt(second.id, second.owner, second.created_at)

    def test_concurrent_attempts_and_single_redemption(self, adapter):
        item = challenge()
        assert adapter.reserve_login(item)
        assert adapter.activate_login(item.id, PERSON_ID, NOW)
        with ThreadPoolExecutor(max_workers=10) as pool:
            attempts = list(
                pool.map(lambda _: adapter.admit_login_attempt(item.id, item.owner, NOW), range(10))
            )
        assert sum(x is not None for x in attempts) == 3
        assert not adapter.redeem_login(item.id, "other-app", NOW)
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(
                pool.map(lambda _: adapter.redeem_login(item.id, item.owner, NOW), range(10))
            )
        assert results.count(True) == 1

    def test_app_and_ip_limits_are_separate_from_status(self, adapter):
        for n in range(30):
            assert adapter.allow_login_request("compute", "start", f"192.0.2.{n}", NOW)
        assert not adapter.allow_login_request("compute", "start", "192.0.2.99", NOW)
        assert adapter.allow_login_request("compute", "status", None, NOW)
        assert adapter.allow_login_request("compute", "confirm", "192.0.2.99", NOW)
        assert adapter.allow_login_request(
            "compute", "start", "192.0.2.99", NOW + timedelta(seconds=60)
        )

    def test_ipv6_rotation_and_mapped_ipv4_do_not_reset_quota(self, adapter):
        for n in range(5):
            assert adapter.allow_login_request("compute", "start", f"2001:db8::{n}", NOW)
        assert not adapter.allow_login_request("other", "start", "2001:db8::99", NOW)
        for _ in range(5):
            assert adapter.allow_login_request("compute", "start", "192.0.2.1", NOW)
        assert not adapter.allow_login_request("compute", "start", "::ffff:192.0.2.1", NOW)

    def test_expired_rows_are_pruned_without_resetting_live_email_budget(self, adapter):
        item = challenge()
        assert adapter.reserve_login(item)
        assert adapter.allow_login_request("compute", "status", None, NOW + timedelta(minutes=11))
        assert adapter.admit_login_attempt(item.id, item.owner, NOW + timedelta(minutes=11)) is None
        for minute in range(11, 15):
            assert adapter.reserve_login(challenge(NOW + timedelta(minutes=minute)))
        assert not adapter.reserve_login(challenge(NOW + timedelta(minutes=15)))
