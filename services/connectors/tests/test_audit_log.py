import json

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.deps import get_source_registry
from src.sources.base import SourceResult

AUTH = {"X-API-Key": "dev-api-key-change-me"}


class _FakeSource:
    def __init__(self, result):
        self._result = result

    def fetch(self, url):
        return self._result


def _client(source):
    app = create_app()
    app.dependency_overrides[get_source_registry] = lambda: {"gdocs": source}
    return TestClient(app)


def _last_line(capsys) -> dict:
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("{")]
    assert lines, "no audit log line emitted"
    return json.loads(lines[-1])


def test_audit_line_has_source_id_and_warning_count(capsys):
    source = _FakeSource(
        result=SourceResult(title="Deck", content="body text", warnings=["heads up", "and this"])
    )
    with _client(source) as c:
        resp = c.post("/fetch", json={"url": "https://x", "source_id": "gdocs"}, headers=AUTH)
    assert resp.status_code == 200
    entry = _last_line(capsys)
    assert entry["path"] == "/fetch"
    assert entry["status"] == 200
    assert entry["source_id"] == "gdocs"
    assert entry["warnings"] == 2


def test_audit_line_omits_api_key_and_content(capsys):
    secret_content = "SUPER-SECRET-DOCUMENT-BODY-TEXT"
    source = _FakeSource(result=SourceResult(title="Deck", content=secret_content, warnings=[]))
    with _client(source) as c:
        c.post("/fetch", json={"url": "https://x", "source_id": "gdocs"}, headers=AUTH)
    entry = _last_line(capsys)
    line = json.dumps(entry)
    assert secret_content not in line
    assert AUTH["X-API-Key"] not in line  # the raw API key never appears
