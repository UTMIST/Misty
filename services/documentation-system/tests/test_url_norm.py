from datetime import datetime, timezone

from contracts.types import Source
from src.url_norm import derive_source, normalize_url


def _src(sid, patterns):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Source(
        id=sid,
        label=sid,
        url_patterns=patterns,
        requires_auth=False,
        has_api=False,
        content_fetch_enabled=False,
        created_at=now,
        updated_at=now,
        created_by="system",
        updated_by="system",
    )


SOURCES = [
    _src("web", []),
    _src("github", ["github.com"]),
    _src("gdrive", ["drive.google.com"]),
    _src("gdocs", ["docs.google.com/document"]),
    _src("gsheets", ["docs.google.com/spreadsheets"]),
    _src("gslides", ["docs.google.com/presentation"]),
    _src("notion", ["notion.so", "notion.site"]),
    _src("youtube", ["youtube.com", "youtu.be"]),
]


def test_normalize_lowercases_host_and_scheme():
    assert normalize_url("HTTPS://GitHub.com/Foo") == "https://github.com/Foo"


def test_normalize_strips_trailing_slash_and_fragment():
    assert normalize_url("https://x.com/a/#section") == "https://x.com/a"


def test_normalize_strips_tracking_params_keeps_others_sorted():
    out = normalize_url("https://x.com/a?utm_source=news&b=2&a=1&gclid=xyz")
    assert out == "https://x.com/a?a=1&b=2"


def test_normalize_drops_default_port():
    assert normalize_url("https://x.com:443/a") == "https://x.com/a"


def test_derive_github():
    assert derive_source("https://github.com/utmist/site", SOURCES) == "github"


def test_derive_google_split_by_path_most_specific_wins():
    assert derive_source("https://docs.google.com/document/d/abc/edit", SOURCES) == "gdocs"
    assert derive_source("https://docs.google.com/spreadsheets/d/abc", SOURCES) == "gsheets"
    assert derive_source("https://drive.google.com/file/d/abc", SOURCES) == "gdrive"


def test_derive_source_path_prefix_respects_segment_boundary():
    # /documentXYZ is NOT under /document — must not resolve to gdocs
    assert derive_source("https://docs.google.com/documentXYZ/foo", SOURCES) == "web"
    # exact prefix (no trailing path) still matches
    assert derive_source("https://docs.google.com/document", SOURCES) == "gdocs"


def test_derive_falls_back_to_web():
    assert derive_source("https://example.org/whatever", SOURCES) == "web"
