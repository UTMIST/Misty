"""OAuth adapter for Misty's dedicated Gmail forwarding mailbox."""

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import email
from email.message import EmailMessage, Message
import email.policy
from html.parser import HTMLParser
import re

from contracts.gmail import (
    AuthMechanism,
    ClaimedHeaders,
    GmailError,
    GmailInspectResult,
    GmailListPage,
    GmailListRequest,
    GmailMessageRef,
    GmailReceiptCheckRequest,
    GmailReceiptCheckResult,
    GmailReplyRequest,
    GmailReplyResult,
    OmittedAttachment,
)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

_EXCLUDED_LABELS = frozenset({"SPAM", "TRASH", "SENT", "DRAFT"})
_LOOP_PRECEDENCE = frozenset({"bulk", "list", "junk", "auto_reply"})
_METADATA_HEADERS = [
    "From",
    "Sender",
    "Return-Path",
    "Reply-To",
    "Subject",
    "Message-ID",
    "Date",
    "Auto-Submitted",
    "Precedence",
    "X-Autoreply",
    "X-Autorespond",
    "List-Id",
    "List-Unsubscribe",
    "Authentication-Results",
]

# Multi-label ICANN public suffixes and shared multi-tenant hosting suffixes that
# must never count as a single shared organizational domain during relaxed
# DMARC / DKIM alignment checks.
_MULTI_LABEL_PUBLIC_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "me.uk",
        "ltd.uk",
        "plc.uk",
        "sch.uk",
        "nhs.uk",
        "on.ca",
        "qc.ca",
        "bc.ca",
        "ab.ca",
        "mb.ca",
        "nb.ca",
        "nf.ca",
        "nl.ca",
        "ns.ca",
        "nt.ca",
        "nu.ca",
        "pe.ca",
        "sk.ca",
        "yk.ca",
        "gc.ca",
        "gov.ca",
        "com.au",
        "edu.au",
        "org.au",
        "net.au",
        "gov.au",
        "co.nz",
        "ac.nz",
        "org.nz",
        "govt.nz",
        "co.jp",
        "ac.jp",
        "or.jp",
        "ne.jp",
        "go.jp",
        "co.kr",
        "ac.kr",
        "or.kr",
        "go.kr",
        "com.br",
        "org.br",
        "net.br",
        "gov.br",
        "edu.br",
        "com.mx",
        "org.mx",
        "edu.mx",
        "gob.mx",
        "com.ar",
        "org.ar",
        "edu.ar",
        "gob.ar",
        "com.co",
        "edu.co",
        "org.co",
        "com.pe",
        "edu.pe",
        "edu.cn",
        "com.cn",
        "org.cn",
        "net.cn",
        "gov.cn",
        "com.tw",
        "edu.tw",
        "org.tw",
        "gov.tw",
        "com.hk",
        "edu.hk",
        "org.hk",
        "gov.hk",
        "com.sg",
        "edu.sg",
        "org.sg",
        "gov.sg",
        "com.my",
        "edu.my",
        "org.my",
        "ac.in",
        "co.in",
        "org.in",
        "net.in",
        "gov.in",
        "edu.in",
        "co.za",
        "ac.za",
        "org.za",
        "gov.za",
        "co.il",
        "ac.il",
        "org.il",
        "gov.il",
        "com.tr",
        "edu.tr",
        "org.tr",
        "gov.tr",
        "com.pl",
        "net.pl",
        "org.pl",
        "edu.pl",
        "com.es",
        "nom.es",
        "org.es",
        "gob.es",
        "edu.es",
        "co.id",
        "ac.id",
        "or.id",
        "co.th",
        "ac.th",
        "or.th",
        "in.th",
        "com.vn",
        "edu.vn",
        "com.ph",
        "edu.ph",
        "com.ng",
        "edu.ng",
        "org.ng",
        "co.ke",
        "ac.ke",
        "or.ke",
        "com.eg",
        "edu.eg",
        "com.sa",
        "edu.sa",
        "com.pk",
        "edu.pk",
        "com.bd",
        "edu.bd",
        "com.ua",
        "org.ua",
        # Common multi-tenant hosting suffixes (PSL private section)
        "github.io",
        "gitlab.io",
        "pages.dev",
        "workers.dev",
        "appspot.com",
        "web.app",
        "firebaseapp.com",
        "herokuapp.com",
        "vercel.app",
        "netlify.app",
        "railway.app",
        "fly.dev",
        "onrender.com",
        "cloudfront.net",
        "s3.amazonaws.com",
        "azurewebsites.net",
        "blogspot.com",
        "wordpress.com",
    }
)
_RECEIPT_FALLBACK_MAX_PAGES = 5

_MSG_ID_RE = re.compile(r"^<[^\s\r\n\x00<>]{1,996}>$")
_GMAIL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_FWD_MARKERS = (
    re.compile(r"^-{5,}\s*forwarded message\s*-{5,}\s*$", re.IGNORECASE),
    re.compile(r"^begin forwarded message:\s*$", re.IGNORECASE),
    re.compile(r"^-{5,}\s*original message\s*-{5,}\s*$", re.IGNORECASE),
)
_HR_LINE_RE = re.compile(r"^_{16,}\s*$")
_CLAIMED_HEADER_RE = re.compile(
    r"^(?:>\s*)*(?:\*+)?(from|date|sent|subject|to|cc)(?:\*+)?:\s*(.*)$",
    re.IGNORECASE,
)


def _execute(request):
    from googleapiclient.errors import HttpError

    try:
        return request.execute(num_retries=0)
    except HttpError as exc:
        status = exc.resp.status
        if status == 404:
            raise GmailError("gmail_not_found", uncertain=False) from None
        raise GmailError(
            f"gmail_{status}", uncertain=status >= 500 or status in {408, 429}
        ) from None
    except Exception:
        raise GmailError("gmail_transport", uncertain=True) from None


def _strip_cfws_comments(value: str) -> str:
    """Remove nested RFC 5322 parenthesized comments outside quoted strings."""
    out: list[str] = []
    depth = 0
    in_quotes = False
    escaped = False
    for ch in value:
        if escaped:
            if depth == 0:
                out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            if depth == 0:
                out.append(ch)
            continue
        if depth == 0 and ch == '"':
            in_quotes = not in_quotes
            out.append(ch)
            continue
        if not in_quotes:
            if ch == "(":
                depth += 1
                continue
            if ch == ")" and depth > 0:
                depth -= 1
                continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _normalize_domain(domain: str) -> str | None:
    cleaned = domain.strip().strip(".").lower()
    if not cleaned or any(c.isspace() or ord(c) < 33 for c in cleaned):
        return None
    try:
        ascii_dom = cleaned.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = ascii_dom.split(".")
    if len(labels) < 2 or any(not lbl for lbl in labels):
        return None
    return ascii_dom


def _organizational_domain(domain: str) -> str | None:
    """Derive the registrable organizational domain using offline public-suffix boundaries."""
    labels = domain.split(".")
    if len(labels) < 2 or domain in _MULTI_LABEL_PUBLIC_SUFFIXES:
        return None
    for idx in range(1, len(labels) - 1):
        suffix = ".".join(labels[idx:])
        if suffix in _MULTI_LABEL_PUBLIC_SUFFIXES:
            return ".".join(labels[idx - 1 :])
    return ".".join(labels[-2:])


def _domains_align(authenticated_domain: str | None, from_domain: str) -> bool:
    """Relaxed DMARC/DKIM organizational-domain alignment (parent, child, or sibling)."""
    if not authenticated_domain:
        return False
    auth_dom = _normalize_domain(authenticated_domain)
    target_dom = _normalize_domain(from_domain)
    if not auth_dom or not target_dom:
        return False
    org_auth = _organizational_domain(auth_dom)
    org_target = _organizational_domain(target_dom)
    return bool(org_auth and org_target and org_auth == org_target)


def _parse_single_mailbox(raw_value: str) -> tuple[str, str] | None:
    """Return (email_lower, domain_lower) iff raw_value is one defect-free mailbox."""
    if not raw_value or "\x00" in raw_value:
        return None
    header = email.policy.default.header_factory("From", raw_value)
    if header.defects or len(header.addresses) != 1:
        return None
    addr = header.addresses[0]
    if not addr.username or not addr.domain:
        return None
    if any(c.isspace() or ord(c) < 33 for c in addr.username):
        return None
    norm_dom = _normalize_domain(addr.domain)
    if not norm_dom:
        return None
    return f"{addr.username.lower()}@{norm_dom}", norm_dom


def _verify_google_auth(auth_headers: list[str], from_domain: str) -> AuthMechanism | None:
    """Evaluate ONLY the topmost Authentication-Results header from mx.google.com.

    Inspecting only index 0 prevents trusting attacker-supplied headers that
    existed in the message prior to Google Workspace SMTP delivery.
    """
    if not auth_headers:
        return None
    top = _strip_cfws_comments(auth_headers[0].replace("\r", " ").replace("\n", " "))
    clauses = [c.strip() for c in top.split(";") if c.strip()]
    if not clauses:
        return None
    authserv_tokens = clauses[0].split()
    if not authserv_tokens or authserv_tokens[0].strip().lower() != "mx.google.com":
        return None

    dmarc_result: str | None = None
    dmarc_from: str | None = None
    spf_result: str | None = None
    dkim_entries: list[tuple[str, str | None]] = []

    for clause in clauses[1:]:
        tokens = clause.split()
        if not tokens or "=" not in tokens[0]:
            continue
        method, _, verdict = tokens[0].partition("=")
        method = method.strip().lower()
        verdict = verdict.strip().lower()
        props: dict[str, str] = {}
        for tok in tokens[1:]:
            if "=" in tok:
                k, _, v = tok.partition("=")
                props[k.strip().lower()] = v.strip().strip('"').lower()

        if method == "dmarc" and dmarc_result is None:
            dmarc_result = verdict
            dmarc_from = props.get("header.from")
        elif method == "spf" and spf_result is None:
            spf_result = verdict
        elif method == "dkim":
            dkim_dom = props.get("header.d")
            if not dkim_dom and "header.i" in props and "@" in props["header.i"]:
                dkim_dom = props["header.i"].rsplit("@", 1)[1]
            dkim_entries.append((verdict, dkim_dom))

    if dmarc_result == "pass":
        if dmarc_from and not _domains_align(dmarc_from, from_domain):
            return None
        return "dmarc_pass"

    if dmarc_result in {"none", None}:
        if spf_result == "fail":
            return None
        if any(
            verdict == "fail" and _domains_align(dom, from_domain) for verdict, dom in dkim_entries
        ):
            return None
        if any(
            verdict == "pass" and _domains_align(dom, from_domain) for verdict, dom in dkim_entries
        ):
            return "dkim_aligned_no_dmarc"

    return None


def _clean_single_line(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"[\r\n\x00]+", " ", value).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def _clean_message_id(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not _MSG_ID_RE.match(candidate):
        return None
    return candidate


class _SafeHTMLTextExtractor(HTMLParser):
    """Convert HTML email bodies to plain text locally; never fetches URLs."""

    _SKIP_TAGS = frozenset({"script", "style", "head", "noscript"})
    _BLOCK_TAGS = frozenset(
        {"br", "p", "div", "li", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and t in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and t in self._BLOCK_TAGS and t != "br":
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks).replace("\r\n", "\n").replace("\r", "\n")
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _html_to_text(html: str) -> str:
    parser = _SafeHTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.get_text()


def _split_forwarded_message(text: str) -> tuple[str, str, bool, ClaimedHeaders]:
    """Separate forwarder note from quoted forward text and untrusted quoted headers."""
    lines = text.split("\n")
    marker_idx: int | None = None

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if any(pattern.match(stripped) for pattern in _FWD_MARKERS):
            marker_idx = idx
            break
        if _HR_LINE_RE.match(stripped):
            lookahead = [ln.strip() for ln in lines[idx + 1 : idx + 6] if ln.strip()]
            if (
                len(lookahead) >= 2
                and any(ln.lower().startswith("from:") for ln in lookahead[:2])
                and any(
                    ln.lower().startswith(("sent:", "date:", "subject:")) for ln in lookahead[:4]
                )
            ):
                marker_idx = idx
                break

    if marker_idx is None:
        return "", text.strip(), False, ClaimedHeaders()

    note = "\n".join(lines[:marker_idx]).strip()
    forwarded = "\n".join(lines[marker_idx:]).strip()

    claimed: dict[str, str] = {}
    scan_idx = marker_idx + 1
    while scan_idx < len(lines) and not lines[scan_idx].strip():
        scan_idx += 1
    for line in lines[scan_idx : scan_idx + 12]:
        stripped = line.strip()
        if not stripped:
            if claimed:
                break
            continue
        match = _CLAIMED_HEADER_RE.match(stripped)
        if not match:
            if claimed:
                break
            continue
        key = match.group(1).lower()
        val = _clean_single_line(match.group(2), limit=2000)
        if not val:
            continue
        if key == "from" and "from_raw" not in claimed:
            claimed["from_raw"] = val[:1000]
        elif key in {"date", "sent"} and "date_raw" not in claimed:
            claimed["date_raw"] = val[:500]
        elif key == "subject" and "subject_raw" not in claimed:
            claimed["subject_raw"] = val[:1000]
        elif key == "to" and "to_raw" not in claimed:
            claimed["to_raw"] = val[:2000]
        elif key == "cc" and "cc_raw" not in claimed:
            claimed["cc_raw"] = val[:2000]

    return note, forwarded, True, ClaimedHeaders(**claimed)


def _part_size_bytes(part: EmailMessage) -> int:
    if not part.is_multipart():
        raw_payload = part.get_payload(decode=True)
        return len(raw_payload) if isinstance(raw_payload, (bytes, bytearray)) else 0
    try:
        return len(part.as_bytes())
    except Exception:
        return 0


def _decode_text_part(part: EmailMessage) -> tuple[str, bool]:
    """Decode a text/* part safely; fall back to UTF-8 on unknown MIME charsets."""
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content, False
    except (KeyError, LookupError, UnicodeDecodeError, ValueError):
        pass
    raw_payload = part.get_payload(decode=True)
    if not isinstance(raw_payload, (bytes, bytearray)):
        return "", False
    charset = (part.get_content_charset() or "").strip()
    if charset:
        try:
            return raw_payload.decode(charset, errors="replace"), False
        except LookupError:
            return raw_payload.decode("utf-8", errors="replace"), True
    return raw_payload.decode("utf-8", errors="replace"), False


def _extract_body_and_attachments(
    msg: EmailMessage, *, max_text_chars: int
) -> tuple[str, bool, list[OmittedAttachment], list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[OmittedAttachment] = []
    warnings: list[str] = []

    def _record_omitted(part: EmailMessage, ctype: str) -> None:
        if len(attachments) < 200:
            attachments.append(
                OmittedAttachment(content_type=ctype[:255], size_bytes=_part_size_bytes(part))
            )
        if "attachments omitted" not in warnings:
            warnings.append("attachments omitted")

    def _visit(part: EmailMessage, *, is_root: bool) -> None:
        ctype = (part.get_content_type() or "application/octet-stream").lower()
        disp = (part.get_content_disposition() or "").lower()
        has_filename = bool(part.get_filename())

        # Prune attachment and embedded-message subtrees (including message/rfc822
        # .eml attachments, which are multipart in Python's email package) BEFORE
        # recursing so attached correspondence can never leak into forwarder_note.
        if not is_root and (disp == "attachment" or has_filename or ctype.startswith("message/")):
            _record_omitted(part, ctype)
            return

        if part.is_multipart():
            children = part.get_payload()
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, Message):
                        _visit(child, is_root=False)
            return

        if ctype not in {"text/plain", "text/html"}:
            _record_omitted(part, ctype)
            return

        content, charset_fallback = _decode_text_part(part)
        if charset_fallback and "unknown charset fallback" not in warnings:
            warnings.append("unknown charset fallback")
        if ctype == "text/plain":
            plain_parts.append(content)
        else:
            html_parts.append(content)

    _visit(msg, is_root=True)

    if plain_parts:
        combined = "\n\n".join(p.strip() for p in plain_parts if p.strip())
    elif html_parts:
        combined = "\n\n".join(_html_to_text(h) for h in html_parts if h.strip())
        if combined:
            warnings.append("converted from html")
    else:
        combined = ""

    combined = combined.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    truncated = False
    if len(combined) > max_text_chars:
        combined = combined[:max_text_chars]
        truncated = True
        warnings.append("content truncated")

    return combined, truncated, attachments, warnings


def _header_map(headers: list[dict]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for item in headers:
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "")
        if name:
            grouped.setdefault(name, []).append(value)
    return grouped


def _is_loop_or_auto_reply(headers: dict[str, list[str]], own_address: str) -> bool:
    for rp in headers.get("return-path", []):
        stripped = rp.strip()
        if stripped == "<>" or not stripped.strip("<>").strip():
            return True
        parsed_rp = _parse_single_mailbox(stripped)
        if parsed_rp and parsed_rp[0] == own_address:
            return True

    for auto_sub in headers.get("auto-submitted", []):
        val = _strip_cfws_comments(auto_sub).strip().lower()
        if val and val != "no":
            return True

    for prec in headers.get("precedence", []):
        if _strip_cfws_comments(prec).strip().lower() in _LOOP_PRECEDENCE:
            return True

    for loop_hdr in ("x-autoreply", "x-autorespond", "list-id", "list-unsubscribe"):
        if headers.get(loop_hdr):
            return True

    return False


class GoogleGmailMailbox:
    def __init__(
        self,
        *,
        address: str,
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
        max_email_bytes: int = 5 * 1024 * 1024,
        max_email_text_chars: int = 500_000,
        timeout: float = 30.0,
        gmail=None,
    ) -> None:
        self.address = address.strip().lower()
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self.max_email_bytes = max_email_bytes
        self.max_email_text_chars = max_email_text_chars
        self._timeout = timeout
        self._gmail = gmail

    @contextmanager
    def _client(self):
        parsed = _parse_single_mailbox(self.address) if self.address else None
        if not parsed:
            raise GmailError("gmail_not_configured")
        if self._gmail is not None:
            yield self._gmail
            return
        if not all((self._client_id, self._client_secret, self._refresh_token)):
            raise GmailError("gmail_not_configured")
        import google_auth_httplib2
        import httplib2
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials(
            None,
            refresh_token=self._refresh_token,
            client_id=self._client_id,
            client_secret=self._client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=GMAIL_SCOPES,
        )
        # Fresh transport per request: httplib2 is not thread safe.
        gmail = build(
            "gmail",
            "v1",
            cache_discovery=False,
            http=google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http(timeout=self._timeout)
            ),
        )
        try:
            yield gmail
        finally:
            gmail.close()

    def list_messages(self, body: GmailListRequest) -> GmailListPage:
        query_parts = ["-in:chats", "-in:drafts", "-in:sent"]
        if body.after_epoch_s is not None:
            query_parts.append(f"after:{body.after_epoch_s}")
        kwargs: dict[str, object] = {
            "userId": "me",
            "labelIds": ["INBOX"],
            "q": " ".join(query_parts),
            "maxResults": body.page_size,
            "includeSpamTrash": False,
        }
        if body.page_token is not None:
            kwargs["pageToken"] = body.page_token

        with self._client() as gmail:
            resp = _execute(gmail.users().messages().list(**kwargs))

        if not isinstance(resp, dict):
            raise GmailError("gmail_invalid_response")
        raw_items = resp.get("messages") or []
        if not isinstance(raw_items, list):
            raise GmailError("gmail_invalid_response")

        refs: list[GmailMessageRef] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise GmailError("gmail_invalid_response")
            mid = str(item.get("id") or "")
            tid = str(item.get("threadId") or "")
            if not (_GMAIL_ID_RE.match(mid) and _GMAIL_ID_RE.match(tid)):
                raise GmailError("gmail_invalid_response")
            refs.append(GmailMessageRef(message_id=mid, thread_id=tid))

        next_token = resp.get("nextPageToken")
        if next_token is not None and (
            not isinstance(next_token, str) or not (1 <= len(next_token) <= 4096)
        ):
            raise GmailError("gmail_invalid_response")

        return GmailListPage(
            mailbox_address=self.address,
            messages=refs,
            next_page_token=next_token,
        )

    def inspect_message(self, message_id: str) -> GmailInspectResult:
        with self._client() as gmail:
            meta = _execute(
                gmail.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=_METADATA_HEADERS,
                )
            )
            if not isinstance(meta, dict):
                raise GmailError("gmail_invalid_response")

            tid = str(meta.get("threadId") or "")
            if not _GMAIL_ID_RE.match(tid):
                raise GmailError("gmail_invalid_response")

            try:
                internal_ms = int(meta["internalDate"])
            except (KeyError, TypeError, ValueError):
                raise GmailError("gmail_invalid_response") from None
            received_at = datetime.fromtimestamp(internal_ms / 1000.0, tz=timezone.utc)

            labels = set(meta.get("labelIds") or [])
            if "INBOX" not in labels or (labels & _EXCLUDED_LABELS):
                return GmailInspectResult(
                    classification="ignored_non_inbox",
                    mailbox_address=self.address,
                    message_id=message_id,
                    thread_id=tid,
                    received_at=received_at,
                )

            payload_meta = meta.get("payload") or {}
            raw_headers = payload_meta.get("headers") if isinstance(payload_meta, dict) else []
            if not isinstance(raw_headers, list):
                raise GmailError("gmail_invalid_response")
            headers = _header_map(raw_headers)

            if _is_loop_or_auto_reply(headers, self.address):
                return GmailInspectResult(
                    classification="ignored_loop",
                    mailbox_address=self.address,
                    message_id=message_id,
                    thread_id=tid,
                    received_at=received_at,
                )

            from_values = headers.get("from", [])
            if len(from_values) != 1:
                return GmailInspectResult(
                    classification="rejected_unauthenticated",
                    mailbox_address=self.address,
                    message_id=message_id,
                    thread_id=tid,
                    received_at=received_at,
                )

            parsed_from = _parse_single_mailbox(from_values[0])
            if parsed_from is None:
                return GmailInspectResult(
                    classification="rejected_unauthenticated",
                    mailbox_address=self.address,
                    message_id=message_id,
                    thread_id=tid,
                    received_at=received_at,
                )
            sender_email, sender_domain = parsed_from
            if sender_email == self.address:
                return GmailInspectResult(
                    classification="ignored_loop",
                    mailbox_address=self.address,
                    message_id=message_id,
                    thread_id=tid,
                    received_at=received_at,
                )

            auth_mech = _verify_google_auth(
                headers.get("authentication-results", []), sender_domain
            )
            if auth_mech is None:
                return GmailInspectResult(
                    classification="rejected_unauthenticated",
                    mailbox_address=self.address,
                    message_id=message_id,
                    thread_id=tid,
                    received_at=received_at,
                )

            subject = _clean_single_line(
                headers["subject"][0] if headers.get("subject") else None, limit=1000
            )
            outer_msg_id = _clean_message_id(
                headers["message-id"][0] if len(headers.get("message-id", [])) == 1 else None
            )

            try:
                size_estimate = int(meta.get("sizeEstimate") or 0)
            except (TypeError, ValueError):
                raise GmailError("gmail_invalid_response") from None
            if size_estimate > self.max_email_bytes:
                return GmailInspectResult(
                    classification="rejected_oversize",
                    mailbox_address=self.address,
                    message_id=message_id,
                    thread_id=tid,
                    received_at=received_at,
                    verified_sender_email=sender_email,
                    auth_mechanism=auth_mech,
                    outer_message_id=outer_msg_id,
                    subject=subject,
                )

            raw_resp = _execute(
                gmail.users().messages().get(userId="me", id=message_id, format="raw")
            )

        if not isinstance(raw_resp, dict) or not isinstance(raw_resp.get("raw"), str):
            raise GmailError("gmail_invalid_response")

        raw_b64 = raw_resp["raw"]
        max_b64_len = ((self.max_email_bytes + 2) // 3) * 4 + 64
        if len(raw_b64) > max_b64_len:
            return GmailInspectResult(
                classification="rejected_oversize",
                mailbox_address=self.address,
                message_id=message_id,
                thread_id=tid,
                received_at=received_at,
                verified_sender_email=sender_email,
                auth_mechanism=auth_mech,
                outer_message_id=outer_msg_id,
                subject=subject,
            )

        try:
            padded = raw_b64 + "=" * (-len(raw_b64) % 4)
            raw_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception:
            raise GmailError("gmail_invalid_response") from None

        if len(raw_bytes) > self.max_email_bytes:
            return GmailInspectResult(
                classification="rejected_oversize",
                mailbox_address=self.address,
                message_id=message_id,
                thread_id=tid,
                received_at=received_at,
                verified_sender_email=sender_email,
                auth_mechanism=auth_mech,
                outer_message_id=outer_msg_id,
                subject=subject,
            )

        mime_msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        raw_from_all = mime_msg.get_all("From", [])
        if len(raw_from_all) != 1:
            return GmailInspectResult(
                classification="rejected_unauthenticated",
                mailbox_address=self.address,
                message_id=message_id,
                thread_id=tid,
                received_at=received_at,
            )
        mime_from = _parse_single_mailbox(str(raw_from_all[0]))
        if mime_from is None or mime_from[0] != sender_email:
            return GmailInspectResult(
                classification="rejected_unauthenticated",
                mailbox_address=self.address,
                message_id=message_id,
                thread_id=tid,
                received_at=received_at,
            )

        extracted, truncated, attachments, warnings = _extract_body_and_attachments(
            mime_msg, max_text_chars=self.max_email_text_chars
        )
        note, forwarded, has_delim, claimed = _split_forwarded_message(extracted)

        return GmailInspectResult(
            classification="accepted_candidate",
            mailbox_address=self.address,
            message_id=message_id,
            thread_id=tid,
            received_at=received_at,
            verified_sender_email=sender_email,
            auth_mechanism=auth_mech,
            outer_message_id=outer_msg_id,
            subject=subject,
            forwarder_note=note,
            forwarded_body=forwarded,
            delimiter_detected=has_delim,
            truncated=truncated,
            omitted_attachment_count=len(attachments),
            omitted_attachments=attachments,
            untrusted_claimed_headers=claimed,
            warnings=warnings,
        )

    def send_reply(self, body: GmailReplyRequest) -> GmailReplyResult:
        if any(
            bad in value
            for value in (body.to_email, body.subject, body.in_reply_to or "")
            for bad in ("\r", "\n", "\x00")
        ):
            raise GmailError("invalid_reply_header")
        if "\x00" in body.body_text:
            raise GmailError("invalid_reply_body")

        parsed_to = _parse_single_mailbox(body.to_email)
        if parsed_to is None:
            raise GmailError("invalid_recipient")
        recipient_email, _ = parsed_to

        if body.in_reply_to is not None and not _MSG_ID_RE.match(body.in_reply_to):
            raise GmailError("invalid_reply_header")

        with self._client() as gmail:
            if recipient_email == self.address:
                raise GmailError("self_reply_forbidden")

            message = EmailMessage()
            message["From"] = self.address
            message["To"] = recipient_email
            message["Subject"] = body.subject
            message["Auto-Submitted"] = "auto-replied"
            message["X-Auto-Response-Suppress"] = "All"
            message["X-Misty-Receipt-ID"] = str(body.receipt_id)
            if body.in_reply_to is not None:
                message["In-Reply-To"] = body.in_reply_to
                message["References"] = body.in_reply_to
            message.set_content(body.body_text)

            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
            sent = _execute(
                gmail.users()
                .messages()
                .send(userId="me", body={"raw": raw, "threadId": body.thread_id})
            )

        if not isinstance(sent, dict):
            raise GmailError("gmail_invalid_response", uncertain=True)
        sent_id = str(sent.get("id") or "")
        sent_tid = str(sent.get("threadId") or body.thread_id)
        if not (_GMAIL_ID_RE.match(sent_id) and _GMAIL_ID_RE.match(sent_tid)):
            raise GmailError("gmail_invalid_response", uncertain=True)
        return GmailReplyResult(sent_message_id=sent_id, thread_id=sent_tid)

    @staticmethod
    def _match_receipt_header(msg: object, target: str) -> str | None:
        if not isinstance(msg, dict):
            return None
        mid = str(msg.get("id") or "")
        payload = msg.get("payload") or {}
        headers = payload.get("headers") if isinstance(payload, dict) else []
        if not isinstance(headers, list):
            return None
        for hdr in headers:
            if (
                isinstance(hdr, dict)
                and str(hdr.get("name") or "").strip().lower() == "x-misty-receipt-id"
                and str(hdr.get("value") or "").strip() == target
                and _GMAIL_ID_RE.match(mid)
            ):
                return mid
        return None

    def check_receipt(self, body: GmailReceiptCheckRequest) -> GmailReceiptCheckResult:
        target = str(body.receipt_id)
        checked_ids: set[str] = set()
        with self._client() as gmail:
            try:
                thread = _execute(
                    gmail.users()
                    .threads()
                    .get(
                        userId="me",
                        id=body.thread_id,
                        format="metadata",
                        metadataHeaders=["X-Misty-Receipt-ID"],
                    )
                )
            except GmailError as exc:
                if exc.code != "gmail_not_found":
                    raise
                thread = {"messages": []}

            if not isinstance(thread, dict):
                raise GmailError("gmail_invalid_response")
            messages = thread.get("messages") or []
            if not isinstance(messages, list):
                raise GmailError("gmail_invalid_response")

            for msg in messages:
                if isinstance(msg, dict) and isinstance(msg.get("id"), str):
                    checked_ids.add(msg["id"])
                matched_id = self._match_receipt_header(msg, target)
                if matched_id is not None:
                    return GmailReceiptCheckResult(sent=True, sent_message_id=matched_id)

            # Fallback independent of thread_id: if Gmail placed the reply into a
            # different thread (e.g., missing In-Reply-To, subject drift, or thread
            # splitting) and the send timed out, scan SENT metadata across threads.
            page_token: str | None = None
            seen_tokens: set[str] = set()
            for _ in range(_RECEIPT_FALLBACK_MAX_PAGES):
                list_kwargs: dict[str, object] = {
                    "userId": "me",
                    "labelIds": ["SENT"],
                    "maxResults": 100,
                    "includeSpamTrash": False,
                }
                if body.sent_after_epoch_s is not None:
                    list_kwargs["q"] = f"after:{body.sent_after_epoch_s}"
                if page_token is not None:
                    list_kwargs["pageToken"] = page_token

                resp = _execute(gmail.users().messages().list(**list_kwargs))
                if not isinstance(resp, dict):
                    raise GmailError("gmail_invalid_response")
                raw_items = resp.get("messages") or []
                if not isinstance(raw_items, list):
                    raise GmailError("gmail_invalid_response")

                for item in raw_items:
                    if not isinstance(item, dict):
                        raise GmailError("gmail_invalid_response")
                    mid = str(item.get("id") or "")
                    if not _GMAIL_ID_RE.match(mid):
                        raise GmailError("gmail_invalid_response")
                    if mid in checked_ids:
                        continue
                    checked_ids.add(mid)
                    try:
                        meta = _execute(
                            gmail.users()
                            .messages()
                            .get(
                                userId="me",
                                id=mid,
                                format="metadata",
                                metadataHeaders=["X-Misty-Receipt-ID"],
                            )
                        )
                    except GmailError as exc:
                        if exc.code == "gmail_not_found":
                            continue
                        raise
                    matched_id = self._match_receipt_header(meta, target)
                    if matched_id is not None:
                        return GmailReceiptCheckResult(sent=True, sent_message_id=matched_id)

                next_token = resp.get("nextPageToken")
                if next_token is None:
                    return GmailReceiptCheckResult(sent=False)
                if not isinstance(next_token, str) or not (1 <= len(next_token) <= 4096):
                    raise GmailError("gmail_invalid_response")
                if next_token in seen_tokens:
                    raise GmailError("gmail_invalid_response")
                seen_tokens.add(next_token)
                page_token = next_token

            # Never authorize a retry when SENT scanning stopped at the page cap.
            raise GmailError("receipt_check_inconclusive", uncertain=True)
