"""Serve the stored Misty operations views on loopback."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from store import RETENTION
from project import ENVIRONMENTS, TRAFFIC, FEATURES, iso, parsed, readonly
from dashboard_ui import DETAIL_ROLES, Period, esc, link, sources

CLIENT = b"""const loading = document.querySelector('#loading');
function pending() {if(loading) loading.hidden=false;}
document.addEventListener('submit',pending);
document.addEventListener('click',e=>{if(e.target.closest('a')?.origin===location.origin) pending();});
if(!new URLSearchParams(location.search).has('end')) setTimeout(()=>location.reload(),60000);
"""
NAV = (
    ("recording", "overview", "Recordings"),
    ("health", "health", "Answer, catalog and login health"),
)
HOME = next((route for feature, route, _ in NAV if feature in FEATURES), None)
STYLE = """
:root{font:16px/1.5 Inter,system-ui,sans-serif;color:#192d36;background:#f2f5f3}
body{margin:0}main{max-width:1180px;margin:auto;padding:2rem}header{background:#16333b;color:white}
header main{padding-block:1.2rem}h1{font-size:2rem;line-height:1.15;margin:.25rem 0}
h2{margin-top:1.8rem;font-size:1.35rem}nav{display:flex;gap:1.7rem;margin:1rem 0}
a{color:#0e675c;text-underline-offset:.18em}header a{color:#c7e4db}
form{display:flex;gap:.9rem;align-items:end;flex-wrap:wrap}label{display:grid;gap:.15rem;font-size:.85rem}
button,select,input{border:1px solid #92a7a5;border-radius:4px;padding:.4rem;font:inherit}
button{background:#2d605c;color:white;padding-inline:1rem}table{border-collapse:collapse;width:100%;background:white}
th,td{text-align:left;padding:.55rem .75rem;border-bottom:1px solid #d4ddda;overflow-wrap:anywhere}
th{background:#e1ebe6}p{max-width:95ch}.note{border-left:4px solid #ba7622;padding:.4rem .8rem;background:#fff4df}
.scroll{overflow-x:auto}code{font-size:.92em}button:focus,a:focus{outline:3px solid #c76b18}
"""


def report_page(db, raw, env, view, params, role, now, stale):
    traffic = params.get("traffic", "synthetic")
    hours = int(params.get("hours", "1"))
    end = (
        parsed(params["end"])
        if params.get("end")
        else now.replace(second=0, microsecond=0)
    )
    if (
        traffic not in TRAFFIC
        or hours not in (1, 6, 24, 168)
        or end > now + timedelta(minutes=1)
        or end <= now - RETENTION
    ):
        raise ValueError("invalid period or traffic")
    retention_floor = now - RETENTION
    previous_full = end - timedelta(hours=2 * hours)
    start = max(end - timedelta(hours=hours), retention_floor)
    previous = max(previous_full, retention_floor)
    period = Period(traffic, hours, start, end, previous, retention_floor)
    q = period.query
    base = "/" + env + "/"
    generated = db.execute("select value from meta where key='generated'").fetchone()[0]
    aged = not -60 <= (now - parsed(generated)).total_seconds() <= stale

    def options(choices, selected):
        return "".join(
            "<option"
            + (" selected" if v == selected else "")
            + ">"
            + esc(v)
            + "</option>"
            for v in choices
        )

    # A single environment is bound to the server; all links preserve the selected cohort.
    header = (
        "<header><main><h1>Misty operations</h1><p>"
        + esc(env)
        + " · Times in UTC</p><nav>"
        + "".join(
            link(base + route, q, title)
            for feature, route, title in NAV
            if feature in FEATURES
        )
        + "</nav></main></header>"
    )
    filters = (
        '<form method="get"><label>Traffic<select name="traffic">'
        + options(TRAFFIC, traffic)
        + '</select></label><label>Period (hours)<select name="hours">'
        + options((1, 6, 24, 168), hours)
        + '</select></label><label>End (UTC)<input name="end" value="'
        + esc(iso(end))
        + '"></label>'
    )
    for k in ("service", "operation"):
        if k in params:
            filters += (
                '<input type="hidden" name="' + k + '" value="' + esc(params[k]) + '">'
            )
    filters += "<button>Apply</button></form>"
    info = f"<p>Compared with {esc(iso(previous))} to {esc(iso(start))}. Snapshot built at {esc(generated)}.</p>"
    body = (
        header
        + '<main><p role="status" id="loading" hidden>Loading observations…</p>'
        + filters
        + info
    )
    if previous_full < retention_floor:
        body += '<p class="note">The retention cutoff limits the selected or comparison window. Earlier activity is unknown.</p>'
    if aged:
        body += '<p class="note">Snapshot stale. Values below are the last observations.</p>'
    if view == "health" and "health" in FEATURES:
        from health import render_health

        body += render_health(
            db,
            traffic,
            iso(start),
            iso(end),
            iso(previous),
            iso(now),
            stale,
            source_db=raw,
        )
    elif "recording" in FEATURES and (
        view in ("overview", "component") or view.startswith("attempt/")
    ):
        from recording_view import render_recording

        body += render_recording(
            db, raw, view, params, role, base, period, sources(raw, now, stale), aged
        )
    else:
        raise LookupError
    return body + '<script defer src="/' + env + '/ui.js"></script></main>'


def page(body):
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Misty operations</title><style>'
        + STYLE
        + "</style>"
        + body
        + "</html>"
    ).encode()


def load_access(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError(
                "access file must be owned by the dashboard user and have mode 0600"
            )
        with open(fd, encoding="utf-8", closefd=False) as stream:
            rows = json.load(stream)
    finally:
        os.close(fd)
    if not isinstance(rows, list) or not rows:
        raise ValueError("access file must contain users")
    access = {}
    for r in rows:
        if (
            r["role"] not in DETAIL_ROLES | {"lead"}
            or r["environment"] not in ENVIRONMENTS
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", r["user"])
            or not re.fullmatch(r"[0-9a-f]{64}", r["sha256"])
            or (r["environment"], r["user"]) in access
        ):
            raise ValueError("invalid access file")
        access[(r["environment"], r["user"])] = (r["role"], r["sha256"])
    return access


def make_handler(
    db_path,
    snapshot_path,
    environment,
    access,
    stale=300,
    clock=lambda: datetime.now(timezone.utc),
):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # request paths can contain restricted trace IDs

        def reply(self, status, content, challenge=False):
            content = page(content)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            if challenge:
                self.send_header(
                    "WWW-Authenticate",
                    'Basic realm="Misty operations", charset="UTF-8"',
                )
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            try:
                scheme, encoded = self.headers.get("Authorization", "").split(" ", 1)
                if scheme.lower() != "basic" or len(encoded) > 512:
                    raise ValueError
                user, key = (
                    base64.b64decode(encoded, validate=True).decode().split(":", 1)
                )
            except (ValueError, UnicodeError):
                return self.reply(
                    401, "<main>Sign in to Misty operations.</main>", True
                )
            found = access.get((environment, user))
            digest = hashlib.sha256(key.encode()).hexdigest()
            if (
                not hmac.compare_digest(digest, found[1] if found else "0" * 64)
                or not found
            ):
                return self.reply(
                    401, "<main>Sign in to Misty operations.</main>", True
                )
            try:
                url = urlsplit(self.path)
                if url.path == "/":
                    view = HOME
                    if view is None:
                        raise LookupError
                elif url.path.startswith("/" + environment + "/"):
                    view = url.path[len(environment) + 2 :]
                else:
                    raise PermissionError
                if view == "ui.js":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(CLIENT)))
                    self.end_headers()
                    self.wfile.write(CLIENT)
                    return
                if view.startswith("attempt/") and found[0] not in DETAIL_ROLES:
                    raise PermissionError
                query = parse_qs(url.query, max_num_fields=8)
                if any(
                    len(v) != 1
                    or k not in {"traffic", "hours", "end", "service", "operation"}
                    for k, v in query.items()
                ):
                    raise ValueError
                with (
                    closing(readonly(db_path, environment)) as raw,
                    closing(readonly(snapshot_path, environment)) as snapshot,
                ):
                    content = report_page(
                        snapshot,
                        raw,
                        environment,
                        view,
                        {k: v[0] for k, v in query.items()},
                        found[0],
                        clock(),
                        stale,
                    )
            except PermissionError:
                return self.reply(
                    403,
                    '<main>Access restricted. <a href="/'
                    + environment
                    + "/"
                    + (HOME or "")
                    + '">Overview</a></main>',
                )
            except LookupError:
                return self.reply(404, "<main>No matching observation.</main>")
            except ValueError as exc:
                if str(exc) == "environment mismatch":
                    return self.reply(
                        503, "<main>Environment storage unavailable.</main>"
                    )
                return self.reply(400, "<main>Invalid filter.</main>")
            except (sqlite3.Error, OSError):
                return self.reply(
                    503,
                    "<main>Measurements unavailable. Check collection and snapshot generation.</main>",
                )
            self.reply(200, content)

    return Handler


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("db", "snapshot", "access"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--environment", choices=ENVIRONMENTS, required=True)
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--stale-seconds", type=int, default=300)
    a = p.parse_args()
    handler = make_handler(
        a.db, a.snapshot, a.environment, load_access(a.access), a.stale_seconds
    )
    with ThreadingHTTPServer(("127.0.0.1", a.port), handler) as server:
        print(f"Listening on http://127.0.0.1:{server.server_port}/", flush=True)
        server.serve_forever()
