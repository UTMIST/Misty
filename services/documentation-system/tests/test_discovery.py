import json

import httpx
import pytest
from pydantic import SecretStr

from src.discovery import (
    DiscoveryClient,
    DiscoveryError,
    ImportResult,
    Item,
    Listing,
    Page,
    Settings,
    State,
    checkpoint,
    main,
    run_discovery,
    save_state,
)


def file(file_id):
    return Item(file_id=file_id, kind="file", source_id="gdocs")


def folder(file_id):
    return Item(file_id=file_id, kind="folder")


class Fake:
    def __init__(self):
        self.pages = {
            (None, None): Page(items=[file("direct"), folder("parent")], next_page_token="p2"),
            (None, "p2"): Page(items=[file("overlap"), folder("nested")]),
            ("parent", None): Page(items=[file("overlap"), folder("nested")]),
            ("nested", None): Page(items=[file("child"), folder("parent")]),
        }
        self.listed = []
        self.imported = []
        self.catalog = set()
        self.fail = None

    def check_scope(self, timeout):
        pass

    def list_page(self, listing, timeout):
        key = (listing.folder_id, listing.page_token)
        self.listed.append(key)
        return self.pages[key]

    def import_file(self, item, timeout):
        self.imported.append(item.file_id)
        if self.fail:
            raise self.fail
        existing = item.file_id in self.catalog
        self.catalog.add(item.file_id)
        return ImportResult(
            status="existing" if existing else "indexed",
            doc_id="11111111-1111-1111-1111-111111111111",
        )


def test_direct_nested_overlapping_paginated_and_recurring_new_shares(tmp_path):
    client, settings, state = Fake(), Settings(), State(identity="default")
    path = tmp_path / "state.json"

    def save():
        save_state(path, state)

    assert run_discovery(client, settings, state, save) == 0
    assert state.counts["indexed"] == 3
    assert state.counts["existing"] == 1
    assert client.listed.count(("nested", None)) == 1
    assert len(client.catalog) == 3
    assert run_discovery(client, settings, state, save) == 0
    assert state.counts.get("indexed", 0) == 0
    assert state.counts["existing"] == 4
    client.pages[(None, None)] = Page(
        items=[file("new-share"), folder("parent"), folder("new-folder")]
    )
    client.pages[("parent", None)] = Page(items=[file("new-child")])
    client.pages[("new-folder", None)] = Page(items=[file("nested-new")])
    assert run_discovery(client, settings, state, save) == 0
    assert {"new-share", "new-child", "nested-new"} <= client.catalog


def test_budget_resumes_saved_page_and_does_not_relist(tmp_path):
    client = Fake()
    settings = Settings(discovery_max_items=1)
    path = tmp_path / "state.json"
    state = State(identity="default")
    for _ in range(20):
        code = run_discovery(client, settings, state, lambda: save_state(path, state))
        state = State.model_validate_json(path.read_text())
        if code == 0:
            break
    assert code == 0
    assert len(client.listed) == 4
    assert state.counts["indexed"] == 3


def test_ambiguous_import_keeps_current_item_for_next_invocation(tmp_path):
    client = Fake()
    state = State(identity="default")
    path = tmp_path / "state.json"
    client.fail = DiscoveryError("timeout")
    with pytest.raises(DiscoveryError):
        run_discovery(client, Settings(), state, lambda: save_state(path, state))
    persisted = State.model_validate_json(path.read_text())
    assert persisted.pending[0].file_id == "direct"
    assert client.imported == ["direct"]
    # The timed-out POST may have committed remotely.
    client.catalog.add("direct")
    client.fail = None
    assert run_discovery(client, Settings(), persisted, lambda: None) == 0
    assert persisted.counts["existing"] == 2


def test_partial_listings_unsupported_exclusions_and_errors_continue():
    client = Fake()
    client.pages = {
        (None, None): Page(
            items=[
                folder("denied"),
                folder("ok"),
                Item(file_id="image", kind="unsupported"),
                Item(file_id="short", kind="shortcut"),
                Item(file_id="shared", kind="shared_drive"),
                file("excluded"),
            ],
            incomplete=True,
        ),
        ("denied", None): None,
        ("ok", None): Page(items=[file("good")]),
    }
    state = State(identity="default")
    assert (
        run_discovery(client, Settings(discovery_excluded_ids={"excluded"}), state, lambda: None)
        == 1
    )
    assert client.catalog == {"good"}
    for key in (
        "listing_failed",
        "incomplete_pages",
        "unsupported",
        "shortcut",
        "shared_drive",
        "excluded",
    ):
        assert state.counts[key] == 1
    assert state.completed_at is not None


def test_import_failure_does_not_block_later_files_and_retries_next_scan():
    client = Fake()
    client.pages = {(None, None): Page(items=[file("bad"), file("good")])}
    original = client.import_file
    client.import_file = lambda item, timeout: (
        None if item.file_id == "bad" else original(item, timeout)
    )
    state = State(identity="default")
    assert run_discovery(client, Settings(), state, lambda: None) == 1
    assert client.catalog == {"good"}
    client.import_file = original
    assert run_discovery(client, Settings(), state, lambda: None) == 0
    assert client.catalog == {"good", "bad"}


def test_checkpoint_lock_and_identity(tmp_path):
    settings = Settings(discovery_state_path=tmp_path / "state.json")
    with checkpoint(settings) as (state, save):
        save()
        with pytest.raises(DiscoveryError, match="lock"):
            with checkpoint(settings):
                pass
    settings.discovery_identity = "other"
    with pytest.raises(DiscoveryError, match="identity"):
        with checkpoint(settings):
            pass


def test_folder_bound_is_not_silently_complete():
    with pytest.raises(DiscoveryError, match="folder bound"):
        run_discovery(
            Fake(), Settings(discovery_max_folders=1), State(identity="default"), lambda: None
        )


def test_disabled_and_status_never_make_http_requests(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    monkeypatch.setenv("DISCOVERY_STATE_PATH", str(tmp_path / "state.json"))
    assert main([]) == 0
    assert main(["--status"]) == 0
    assert '"enabled": false' in capsys.readouterr().out


def api(handler):
    return DiscoveryClient(
        Settings(
            docs_api_key=SecretStr("secret-doc"), connectors_api_key=SecretStr("secret-connector")
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_http_boundaries_keys_and_safe_payloads():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api-keys/self":
            return httpx.Response(200, json={"scopes": ["docs:write"]})
        if request.url.path == "/discover/google":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(
            200, json={"status": "indexed", "doc_id": "11111111-1111-1111-1111-111111111111"}
        )

    client = api(handler)
    client.check_scope(1)
    client.list_page(Listing(), 1)
    client.import_file(file("id"), 1)
    assert [r.headers["X-API-Key"] for r in requests] == [
        "secret-doc",
        "secret-connector",
        "secret-doc",
    ]
    assert json.loads(requests[-1].content) == {"file_id": "id", "source_id": "gdocs"}


@pytest.mark.parametrize("body", [{}, {"items": [{}]}, {"items": [], "incomplete": "false"}])
def test_bad_pages_stop_safely(body):
    with pytest.raises(DiscoveryError, match="invalid discovery page"):
        api(lambda request: httpx.Response(200, json=body)).list_page(Listing(), 1)


def test_missing_scope_fails_before_listing():
    with pytest.raises(DiscoveryError, match="requires docs:write"):
        api(lambda request: httpx.Response(200, json={"scopes": ["docs:read"]})).check_scope(1)


@pytest.mark.parametrize("code", [401, 403, 500, 503])
def test_http_failures_do_not_expose_bodies(code):
    with pytest.raises(DiscoveryError) as error:
        api(lambda request: httpx.Response(code, text="secret content")).import_file(file("id"), 1)
    assert "secret" not in str(error.value)


def test_timeout_no_immediate_retry():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret url")

    with pytest.raises(DiscoveryError, match="no immediate retry"):
        api(handler).import_file(file("id"), 1)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("discovery_max_pages", 0),
        ("discovery_page_size", 101),
        ("discovery_max_seconds", float("inf")),
        ("discovery_timeout_seconds", float("nan")),
    ],
)
def test_invalid_bounds(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_null_success_response_does_not_advance_listing():
    with pytest.raises(DiscoveryError, match="not an object"):
        api(lambda request: httpx.Response(200, content="null")).list_page(Listing(), 1)


def test_malformed_exclusions_have_sanitized_cli_error(monkeypatch, capsys):
    monkeypatch.setenv("DISCOVERY_EXCLUDED_IDS", "private-invalid-json")
    assert main([]) == 2
    assert "private-invalid-json" not in capsys.readouterr().err


def test_time_budget_stops_before_provider_work(monkeypatch):
    times = iter([0, 0, 901])
    monkeypatch.setattr("src.discovery.time.monotonic", lambda: next(times))
    client = Fake()
    with pytest.raises(DiscoveryError, match="time budget"):
        run_discovery(client, Settings(), State(identity="default"), lambda: None)
    assert client.listed == []


def test_page_budget_continues_from_cursor_next_invocation():
    client = Fake()
    client.pages = {
        (None, None): Page(items=[], next_page_token="next"),
        (None, "next"): Page(items=[file("after-empty-page")]),
    }
    state = State(identity="default")
    settings = Settings(discovery_max_pages=1)
    assert run_discovery(client, settings, state, lambda: None) == 2
    assert state.frontier[0].page_token == "next"
    assert run_discovery(client, settings, state, lambda: None) == 0
    assert client.catalog == {"after-empty-page"}


@pytest.mark.parametrize(
    "body",
    [
        {"status": "indexed"},
        {"status": "existing", "doc_id": "bad"},
        {"status": "disabled", "doc_id": "11111111-1111-1111-1111-111111111111"},
    ],
)
def test_malformed_import_result_cannot_count_as_indexed(body):
    with pytest.raises(DiscoveryError, match="invalid discovery result"):
        api(lambda request: httpx.Response(200, json=body)).import_file(file("id"), 1)
