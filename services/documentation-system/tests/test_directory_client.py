from uuid import uuid4, UUID

import httpx
import pytest

from contracts.directory import DirectoryUnavailable
from src.directory.http_client import HttpDirectoryClient


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_team_label_returns_label():
    def handler(request):
        assert request.headers["X-API-Key"] == "k"
        return httpx.Response(200, json={"id": str(uuid4()), "label": "Partnerships"})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_team_label(uuid4()) == "Partnerships"


def test_get_person_label_uses_display_name():
    def handler(request):
        return httpx.Response(200, json={"display_name": "Priya"})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_person_label(uuid4()) == "Priya"


def test_404_returns_none():
    def handler(request):
        return httpx.Response(404, json={"detail": "not found"})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_team_label(uuid4()) is None


def test_connection_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom")

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_team_label(uuid4())


def test_5xx_raises_unavailable():
    def handler(request):
        return httpx.Response(503)

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_person_label(uuid4())


def test_403_raises_unavailable():
    def handler(request):
        return httpx.Response(403, json={"detail": "forbidden"})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_team_label(uuid4())


def test_get_active_team_ids_parses_team_ids():
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        assert request.url.path == "/memberships"
        assert request.url.params["person_id"] == str(pid)
        assert request.url.params["active_only"] == "true"
        return httpx.Response(
            200,
            json=[
                {"team_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                {"team_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
            ],
        )

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    ids = dc.get_active_team_ids(pid)
    assert ids == frozenset(
        {
            UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        }
    )


def test_get_active_team_ids_empty():
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        return httpx.Response(200, json=[])

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    ids = dc.get_active_team_ids(pid)
    assert ids == frozenset()


def test_get_active_team_ids_5xx_raises_unavailable():
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        return httpx.Response(503)

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_active_team_ids(pid)


def test_get_active_team_ids_malformed_2xx_body_raises_unavailable():
    """A 200 with an unexpected body shape (missing team_id key) must degrade
    to DirectoryUnavailable, not leak a raw KeyError/ValueError/TypeError up
    to the caller — the read path must never 500 on a malformed 2xx."""
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        return httpx.Response(200, json=[{"not_team_id": "x"}])

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_active_team_ids(pid)


def test_get_active_team_ids_non_uuid_team_id_raises_unavailable():
    pid = UUID("11111111-1111-1111-1111-111111111111")

    def handler(request):
        return httpx.Response(200, json=[{"team_id": "not-a-uuid"}])

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    with pytest.raises(DirectoryUnavailable):
        dc.get_active_team_ids(pid)


def test_verified_email_falls_back_from_primary_to_verified_identifier():
    person_id = UUID("11111111-1111-1111-1111-111111111111")
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.startswith("/people/by-email/"):
            return httpx.Response(404)
        return httpx.Response(200, json={"id": str(person_id)})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_person_id_by_verified_email("Member+docs@Example.com") == person_id
    assert seen == [
        "/people/by-email/member+docs@example.com",
        "/people/by-identifier/email/member+docs@example.com",
    ]


def test_synced_google_group_uses_fail_closed_mapping_endpoint():
    team_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def handler(request):
        assert request.url.path == "/teams/by-synced-google-group/team@example.com"
        return httpx.Response(200, json={"id": str(team_id)})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_team_id_by_synced_google_group("TEAM@example.com") == team_id


def test_source_access_team_ids_are_separate_from_ordinary_memberships():
    person_id = UUID("11111111-1111-1111-1111-111111111111")
    team_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def handler(request):
        assert request.url.path == f"/people/{person_id}/synced-google-teams"
        return httpx.Response(200, json=[{"team_id": str(team_id)}])

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_source_access_team_ids(person_id) == frozenset({team_id})


def test_channel_scope_reads_authenticated_configuration_and_synced_groups():
    from tests.retrieval_cases import T1, T2

    calls = []

    def handler(request):
        assert request.headers["X-API-Key"] == "k"
        calls.append(request.url.path)
        if request.url.path.startswith("/channels/"):
            return httpx.Response(
                200,
                json={
                    "guild_id": "100",
                    "channel_id": "200",
                    "team_ids": [str(T1), str(T2)],
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "team_id": str(T1),
                    "group_email": "a@example.test",
                    "group_name": "groups/a",
                    "status": "synced",
                },
                {
                    "team_id": str(T2),
                    "group_email": "b@example.test",
                    "group_name": "groups/b",
                    "status": "failed",
                },
            ],
        )

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_channel_source_team_ids("100", "200") == frozenset({T1})
    assert calls == ["/channels/100/200/teams", "/teams/google-groups"]


def test_unconfigured_channel_does_not_lookup_all_groups():
    def handler(request):
        assert request.url.path == "/channels/100/200/teams"
        return httpx.Response(200, json={"guild_id": "100", "channel_id": "200", "team_ids": []})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_channel_source_team_ids("100", "200") == frozenset()


@pytest.mark.parametrize(
    "status,body",
    [
        (403, {}),
        (404, {}),
        (503, {}),
        (200, {}),
        (200, []),
        (200, {"guild_id": "100", "channel_id": "other", "team_ids": []}),
        (200, {"guild_id": "100", "channel_id": "200", "team_ids": ["bad-id"]}),
    ],
)
def test_channel_scope_never_falls_back_on_dependency_errors(status, body):
    dc = HttpDirectoryClient(
        "http://dir", "k", client=_client(lambda request: httpx.Response(status, json=body))
    )
    with pytest.raises(DirectoryUnavailable):
        dc.get_channel_source_team_ids("100", "200")


@pytest.mark.parametrize("synced", [True, False])
def test_meeting_audience_requires_complete_group_coverage(synced):
    from tests.retrieval_cases import T1, T2

    def handler(request):
        assert request.headers["X-API-Key"] == "k"
        if request.url.path.startswith("/channels/"):
            return httpx.Response(
                200, json={"guild_id": "100", "channel_id": "200", "team_ids": [str(T1), str(T2)]}
            )
        return httpx.Response(
            200,
            json=[
                {
                    "team_id": str(T1),
                    "group_email": "a@example.test",
                    "group_name": "groups/a",
                    "status": "synced",
                },
                {
                    "team_id": str(T2),
                    "group_email": "b@example.test",
                    "group_name": "groups/b",
                    "status": "synced" if synced else "failed",
                },
            ],
        )

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_meeting_audience("100", "200") == (
        {T1: "a@example.test", T2: "b@example.test"} if synced else {}
    )


def test_discord_identity_uses_existing_identifier_endpoint():
    from uuid import uuid4

    person_id = uuid4()

    def handler(request):
        assert request.url.path == "/people/by-identifier/discord/123"
        return httpx.Response(200, json={"id": str(person_id)})

    dc = HttpDirectoryClient("http://dir", "k", client=_client(handler))
    assert dc.get_person_id_by_discord("123") == person_id
