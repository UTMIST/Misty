import ipaddress
import re
import socket
from urllib.parse import SplitResult, urlsplit

import httpx

from contracts.fetcher import FetchError, FetchResult
from src.content import MAX_CONTENT_CHARS

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TIMEOUT = httpx.Timeout(5.0)

# The snapshot stays at the historical preview length, sliced from the full text.
SNAPSHOT_CHARS = 2000

# Coarse guard against pathological pages. It sits well ABOVE MAX_CONTENT_CHARS
# on purpose: clamp_content() at ingest/refetch is the single truncation point,
# and capping here at the storage limit would hand it text of exactly
# MAX_CONTENT_CHARS, which it reads as "not truncated" — silently swallowing the
# oversized-content warning for every real web page.
FETCH_HARD_CAP = MAX_CONTENT_CHARS * 4

# SSRF egress protection.
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MAX_REDIRECTS = 5
_METADATA_IP = ipaddress.ip_address("169.254.169.254")
# Carrier-grade NAT / shared address space (RFC 6598). ipaddress.is_private does
# NOT flag this, yet Alibaba Cloud's metadata endpoint (100.100.100.200) and
# other internal services live here, so block it explicitly.
_CGN_V4 = ipaddress.ip_network("100.64.0.0/10")
_DEFAULT_PORTS = {"http": 80, "https": 443}

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class BlockedURLError(FetchError):
    """A URL was rejected by SSRF egress protection before any connection was
    attempted. Subclasses FetchError so ingest treats it as a normal fetch
    failure (warning) and the router surfaces it as a fetch error, not a 500."""


def parse_title(html: str) -> str | None:
    m = _TITLE_RE.search(html)
    if not m:
        return None
    title = _WS_RE.sub(" ", m.group(1)).strip()
    return title or None


def extract_text(html: str, limit: int = FETCH_HARD_CAP) -> str:
    stripped = _TAG_RE.sub(" ", html)
    text = _WS_RE.sub(" ", stripped).strip()
    return text[:limit]


def _is_blocked_ip(ip: _IpAddress) -> bool:
    """True if `ip` is a destination we must never connect to: private, loopback,
    link-local, reserved, unspecified, multicast, carrier-grade-NAT, or the cloud
    metadata address. IPv4-mapped IPv6 addresses are unwrapped first so an
    attacker cannot smuggle an internal IPv4 through the IPv6 form."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip == _METADATA_IP:
        return True
    if ip.version == 4 and ip in _CGN_V4:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def _resolve_ips(host: str) -> list[_IpAddress]:
    """Resolve `host` to the IPs a socket would connect to. A literal IP is
    returned as-is (no DNS); otherwise every getaddrinfo answer is returned so
    that ALL candidate addresses can be validated, not just the first."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips: list[_IpAddress] = []
    for info in infos:
        addr = info[4][0]
        # getaddrinfo can suffix a scope id on link-local IPv6 (e.g. "fe80::1%eth0").
        addr = addr.split("%", 1)[0]
        ips.append(ipaddress.ip_address(addr))
    return ips


def _validate_and_resolve(url: str) -> tuple[str, str, list[_IpAddress]]:
    """Reject `url` unless it uses http(s) and EVERY address it resolves to is a
    public destination; otherwise raise BlockedURLError. Returns the hostname, the
    reconstructed Host-header authority, and the validated IPs so the caller can
    pin the connection to a validated IP. Called for the initial URL and again for
    each redirect hop.

    All URL parsing happens inside the guarded region: a malformed URL (invalid
    IPv6 literal → urlsplit raises; out-of-range port → parts.port raises) becomes
    a handled BlockedURLError, never a raw ValueError/500."""
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = parts.hostname
        host_header = _host_header(parts)  # accesses parts.port, may raise ValueError
    except ValueError as e:
        raise BlockedURLError(f"malformed URL {url!r}: {e}") from e
    if scheme not in _ALLOWED_SCHEMES:
        raise BlockedURLError(f"blocked URL scheme: {scheme or '(none)'!r}")
    if not host:
        raise BlockedURLError(f"blocked URL with no host: {url!r}")
    try:
        ips = _resolve_ips(host)
    except (socket.gaierror, UnicodeError, ValueError) as e:
        # gaierror: DNS failure. UnicodeError/ValueError: malformed host (e.g.
        # IDNA encoding failure). All become a handled fetch failure, never a 500.
        raise BlockedURLError(f"could not resolve host {host!r}: {e}") from e
    if not ips:
        raise BlockedURLError(f"could not resolve host {host!r}")
    for ip in ips:
        if _is_blocked_ip(ip):
            raise BlockedURLError(f"blocked internal address for host {host!r}: {ip}")
    return host, host_header, ips


def _host_header(parts: SplitResult) -> str:
    """The original authority for the Host header: hostname (bracketed if IPv6)
    plus a non-default port. Userinfo is intentionally excluded."""
    host = parts.hostname or ""
    rendered = f"[{host}]" if ":" in host else host
    port = parts.port
    if port is None or port == _DEFAULT_PORTS.get((parts.scheme or "").lower()):
        return rendered
    return f"{rendered}:{port}"


class WebFetcher:
    """Fetch title, full text, and a bounded snapshot from a public web page.

    Every hop's host is resolved and validated, then the request is PINNED to a
    validated IP (URL authority replaced by the IP, original hostname preserved
    in the Host header and TLS SNI). Because the socket connects to the literal
    validated IP, httpx never re-resolves the hostname — closing the DNS-rebinding
    (TOCTOU) window where a hostile resolver returns a public IP for the check and
    a private IP for the connection. Auto-redirects are disabled and each hop is
    revalidated and re-pinned, so a blocked host is never connected to."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def fetch(self, url: str) -> FetchResult:
        # follow_redirects stays False: we validate, pin, and follow each hop.
        client = self._client or httpx.Client(timeout=_TIMEOUT)
        try:
            current = url
            for _ in range(_MAX_REDIRECTS + 1):
                # Parses + validates + resolves; raises BlockedURLError before connecting.
                host, host_header, ips = _validate_and_resolve(current)
                pinned = httpx.URL(current).copy_with(host=str(ips[0]))
                resp = client.get(
                    pinned,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": host},
                )
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchError("web fetch failed: redirect without location header")
                    # Join against the logical (hostname) URL, not the pinned IP URL.
                    current = str(httpx.URL(current).join(location))
                    continue
                resp.raise_for_status()
                text = extract_text(resp.text)
                return FetchResult(
                    title=parse_title(resp.text),
                    # Empty page → both fields None, never "" (FetchResult's
                    # invariant): ingest skips the doc_content upsert and
                    # refetch keeps the stored snapshot instead of blanking it.
                    content=text or None,
                    content_snapshot=text[:SNAPSHOT_CHARS] or None,
                )
            raise FetchError(f"web fetch failed: too many redirects (>{_MAX_REDIRECTS})")
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
            # httpx.InvalidURL/ValueError: a malformed URL or redirect Location that
            # httpx.URL(...)/join(...) rejects — surface as a handled fetch failure,
            # never a raw 500. (BlockedURLError is a FetchError and propagates as-is.)
            raise FetchError(f"web fetch failed: {e}") from e
        finally:
            if self._client is None:
                client.close()
