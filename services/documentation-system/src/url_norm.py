"""Pure helpers for URL normalization (dedup key) and source derivation.

No I/O — deterministic and heavily unit-tested. Consumed by the ingest flow.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from contracts.types import Source

# Query params that never identify a distinct document — stripped for dedup.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_url(url: str) -> str:
    """Return a canonical form of `url` for dedup.

    - lowercase scheme and host
    - drop the default port for the scheme
    - drop the fragment
    - remove tracking query params; keep the rest, sorted for stability
    - strip a trailing slash from the path
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) != str(port):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS]
    kept.sort()
    query = urlencode(kept)

    return urlunsplit((scheme, netloc, path, query, ""))


def _host_and_path(url: str) -> tuple[str, str]:
    parts = urlsplit(url.strip())
    return (parts.hostname or "").lower(), parts.path


def derive_source(url: str, sources: list[Source]) -> str:
    """Return the id of the most-specific matching source, else 'web'.

    A pattern is either a bare host (`github.com`) or host + path prefix
    (`docs.google.com/document`). A pattern matches when the URL host equals
    or ends with the pattern host AND the URL path starts with the pattern
    path prefix. The longest matching pattern wins; the empty-pattern source
    ('web') is the fallback.
    """
    host, path = _host_and_path(url)
    best_id = "web"
    best_len = -1
    for source in sources:
        for pattern in source.url_patterns:
            phost, _, ppath = pattern.partition("/")
            ppath = f"/{ppath}" if ppath else ""
            host_ok = host == phost or host.endswith(f".{phost}")
            path_ok = (path == ppath or path.startswith(ppath + "/")) if ppath else True
            if host_ok and path_ok and len(pattern) > best_len:
                best_id = source.id
                best_len = len(pattern)
    return best_id
