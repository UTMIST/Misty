"""Serve the stored Misty operations views on loopback."""

import argparse
import base64
import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from project import ENVIRONMENTS, TRAFFIC, iso, parsed, percentiles, readonly

RUNBOOK = "https://github.com/UTMIST/Misty/blob/staging/docs/RECORDING-TELEMETRY.md"
RUNBOOKS = {
    "meeting": "services/meeting/docs/DEPLOYMENT.md#troubleshooting",
    "documentation-system": "services/documentation-system/docs/DEPLOYMENT.md#meeting-record-persistence",
    "connectors": "services/connectors/docs/DEPLOYMENT.md",
    "discord-bot": "discord-bot/docs/meeting-delivery.md#recovery",
}
CLIENT = b"""const loading = document.querySelector('#loading');
function pending() {if(loading) loading.hidden=false;}
document.addEventListener('submit',pending);
document.addEventListener('click',e=>{if(e.target.closest('a')?.origin===location.origin) pending();});
if(!new URLSearchParams(location.search).has('end')) setTimeout(()=>location.reload(),60000);
"""
DETAIL_ROLES = {"developer", "admin"}
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


def esc(value):
    return escape(str(value), quote=True)


def table(columns, rows):
    return (
        '<div class="scroll"><table><thead><tr>'
        + "".join("<th>" + esc(x) + "</th>" for x in columns)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join("<td>" + str(c) + "</td>" for c in row) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def link(path, params, title):
    return (
        '<a href="' + esc(path + "?" + urlencode(params)) + '">' + esc(title) + "</a>"
    )


def sources(db, end, max_age):
    rows = db.execute(
        "select service,last_success,last_error,gaps,drops,rejected from sources order by service"
    ).fetchall()

    def condition(r):
        if r["last_error"]:
            return "Read failed"
        if not r["last_success"]:
            return "No successful read"
        age = (end - parsed(r["last_success"])).total_seconds()
        return "Current" if -60 <= age <= max_age else "Stale"

    return [(r, condition(r)) for r in rows]


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
        or end < now - timedelta(days=30) + timedelta(hours=hours * 2)
    ):
        raise ValueError("invalid period or traffic")
    start = end - timedelta(hours=hours)
    previous = start - timedelta(hours=hours)
    q = {"traffic": traffic, "hours": hours, "end": iso(end)}
    base = "/" + env + "/"
    flags = sources(raw, now, stale)
    generated = db.execute("select value from meta where key='generated'").fetchone()[0]
    aged = not -60 <= (now - parsed(generated)).total_seconds() <= stale
    healthy = (
        any(
            r["service"] == "discord-bot" and status == "Current" for r, status in flags
        )
        and not aged
    )

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
        + link(base + "overview", q, "Recordings")
        + link(base + "health", q, "Answer, catalog and login health")
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
    if aged:
        body += '<p class="note">Snapshot stale. Values below are the last observations.</p>'
    if view == "overview":
        counts = []
        for name, left, right in [
            ("Selected", start, end),
            ("Previous", previous, start),
        ]:
            for mode, title in [
                ("record.saved_requested", "Saved Doc, link and PDF"),
                ("record.pdf_requested", "PDF only"),
            ]:
                rows = db.execute(
                    "select outcome,started,latency_ms,pdf,link from attempts where traffic=? and mode=? and cohort>=? and cohort<?",
                    (traffic, mode, iso(left), iso(right)),
                ).fetchall()
                started = sum(r["started"] for r in rows)
                done = sum(r["outcome"] == "complete" for r in rows)
                delivered = sum(
                    r["started"]
                    and r["pdf"] == "pdf.posted"
                    and (mode == "record.pdf_requested" or r["link"] == "link.posted")
                    for r in rows
                )
                n, median, p95 = percentiles(r["latency_ms"] for r in rows)
                counts.append(
                    [
                        name,
                        title,
                        started if rows or healthy else "Unknown",
                        delivered if rows or healthy else "Unknown",
                        done if rows or healthy else "Unknown",
                        f"{done / started:.1%}" if started else "—",
                        ms(median),
                        ms(p95),
                        n,
                    ]
                )
        body += "<h2>Recording outcomes</h2>"
        if not flags:
            body += '<p class="note">No measurements configured.</p>'
        elif not healthy:
            body += '<p class="note">Recording source unavailable or stale. Current activity is unknown.</p>'
        elif not db.execute(
            "select 1 from attempts where traffic=? and cohort>=? and cohort<? limit 1",
            (traffic, iso(start), iso(end)),
        ).fetchone():
            body += "<p>No activity in this period.</p>"
        body += table(
            [
                "Period",
                "Output",
                "Started",
                "Discord delivery confirmed",
                "Complete",
                "Completion rate",
                "Median delivery",
                "p95 delivery",
                "Timed samples",
            ],
            counts,
        )
        states = db.execute(
            "select case when outcome='open' and ?=0 then 'unconfirmed' else outcome end as state,count(*) n from attempts where traffic=? and cohort>=? and cohort<? group by state order by state",
            (healthy, traffic, iso(start), iso(end)),
        )
        body += table(["Outcome", "Attempts"], [(esc(r[0]), r[1]) for r in states])
        components = db.execute(
            "select service,operation,count(*) n from spans where traffic=? and start>=? and start<? group by service,operation order by service,operation",
            (traffic, iso(start), iso(end)),
        )
        body += "<h2>Component calls</h2>" + table(
            ["Service", "Operation", "Observed calls"],
            [
                (
                    esc(r["service"]),
                    link(
                        base + "component",
                        {**q, "service": r["service"], "operation": r["operation"]},
                        r["operation"],
                    ),
                    r["n"],
                )
                for r in components
            ],
        )
        missing = sorted(set(RUNBOOKS) - {r["service"] for r, _ in flags})
        if missing:
            body += (
                '<p class="note">No measurements configured for '
                + esc(", ".join(missing))
                + ".</p>"
            )
        if any(r["gaps"] or r["drops"] or r["rejected"] for r, _ in flags):
            body += '<p class="note">Partial history. Collection recorded gaps, dropped observations or rejected records. Check the source counters below.</p>'
        if role in DETAIL_ROLES:
            rows = db.execute(
                "select trace_id,cohort,case when outcome='open' and ?=0 then 'unconfirmed' else outcome end,stop from attempts where traffic=? and cohort>=? and cohort<? order by cohort desc limit 100",
                (healthy, traffic, iso(start), iso(end)),
            )
            body += "<h2>Recent attempts</h2>" + table(
                ["Recording trace", "Started (UTC)", "Outcome", "Stop"],
                [
                    (
                        link(base + "attempt/" + r[0], q, r[0]),
                        esc(r[1]),
                        esc(r[2]),
                        esc(r[3]),
                    )
                    for r in rows
                ],
            )
        body += "<h2>Collection</h2>" + table(
            [
                "Service",
                "Status",
                "Last successful coverage (UTC)",
                "Historical gaps",
                "Dropped",
                "Rejected",
            ],
            [
                (
                    esc(r["service"]),
                    s,
                    esc(r["last_success"] or "Unknown"),
                    r["gaps"],
                    r["drops"],
                    r["rejected"],
                )
                for r, s in flags
            ],
        )
    elif view.startswith("attempt/"):
        if role not in DETAIL_ROLES:
            raise PermissionError
        trace = view[8:]
        if not re.fullmatch("[a-f0-9]{32}", trace):
            raise LookupError
        row = db.execute(
            "select * from attempts where trace_id=? and traffic=? and cohort>=? and cohort<?",
            (trace, traffic, iso(max(start, now - timedelta(days=30))), iso(end)),
        ).fetchone()
        if not row:
            raise LookupError
        outcome = (
            "unconfirmed"
            if row["outcome"] == "open" and not healthy
            else row["outcome"]
        )
        body += (
            "<h2>Recording investigation</h2><p><code>"
            + esc(trace)
            + "</code></p>"
            + table(
                ["Outcome", "Capture stop", "Server drain", "Save", "Link", "PDF"],
                [
                    [
                        esc(outcome),
                        *(
                            esc(row[k])
                            for k in ("stop", "drain", "save", "link", "pdf")
                        ),
                    ]
                ],
            )
        )
        body += "<p>Last recording milestone at " + esc(row["last_seen"]) + ".</p>"
        # Follow async links from the recording in this environment. UNION also
        # terminates cycles. The collector has already validated the payload.
        rows = raw.execute(
            """with recursive traces(id) as (
            values(?) union select e.trace_id from events e join traces on e.link_trace_id=traces.id
              where e.ts>=? and e.traffic in (?,'background') limit 101)
            select ts,service,operation,event_name,result,trace_id,span_id,parent_span_id,link_span_id,link_trace_id,elapsed_ms
              from events where trace_id in (select id from traces) and ts>=? and traffic in (?,'background')
              order by ts,event_id limit 501""",
            (
                trace,
                iso(now - timedelta(days=30)),
                traffic,
                iso(now - timedelta(days=30)),
                traffic,
            ),
        ).fetchall()
        if len(rows) > 500:
            body += (
                '<p class="note">Timeline limited to the first 500 observations.</p>'
            )
        rows = rows[:500]
        body += "<h2>Milestones</h2>" + table(
            ["Time (UTC)", "Service", "Event", "Result", "Elapsed"],
            [
                [
                    esc(r["ts"]),
                    esc(r["service"]),
                    esc(r["event_name"]),
                    esc(r["result"] or "—"),
                    ms(r["elapsed_ms"]),
                ]
                for r in rows
            ],
        )
        body += "<h2>Call tree</h2>" + span_tree(rows, base, q)
    elif view == "component":
        service = params.get("service", "")
        op = params.get("operation", "")
        if not service or not op or max(len(op), len(service)) > 128:
            raise ValueError
        body += "<h2>" + esc(service) + " / " + esc(op) + "</h2>"
        current = (
            any(r["service"] == service and status == "Current" for r, status in flags)
            and not aged
        )
        if not current:
            body += '<p class="note">Component source unavailable or stale. Current activity is unknown.</p>'
        for name, left, right in [
            ("Selected", start, end),
            ("Previous", previous, start),
        ]:
            args = (service, op, traffic, iso(left), iso(right))
            rows = db.execute(
                "select result,elapsed_ms from spans where service=? and operation=? and traffic=? and start>=? and start<?",
                args,
            )
            outcomes = {}
            times = []
            for r in rows:
                result = (
                    "unknown" if r["result"] == "open" and not current else r["result"]
                )
                outcomes[result] = outcomes.get(result, 0) + 1
                if r["elapsed_ms"] is not None:
                    times.append(r["elapsed_ms"])
            n, p50, p95 = percentiles(times)
            body += (
                f"<h3>{name} period</h3><p>{sum(outcomes.values())} observed calls · Median {ms(p50)} · p95 {ms(p95)} · {n} timed samples.</p>"
                + table(
                    ["Result", "Calls"],
                    [(esc(k), v) for k, v in sorted(outcomes.items())],
                )
            )
        if service in RUNBOOKS:
            body += (
                '<p><a href="https://github.com/UTMIST/Misty/blob/staging/'
                + RUNBOOKS[service]
                + '">'
                + esc(service)
                + " operator runbook</a></p>"
            )
    elif view == "health":
        try:
            from health import render_health
        except ModuleNotFoundError as exc:
            if exc.name != "health":
                raise
            body += "<p>No health measurements configured.</p>"
        else:
            body += render_health(
                raw, traffic, iso(start), iso(end), iso(previous), iso(now), stale
            )
    else:
        raise LookupError
    return (
        body
        + '<script defer src="/'
        + env
        + '/ui.js"></script><p><a href="'
        + RUNBOOK
        + '">Recording operator runbook</a></p></main>'
    )


def span_tree(rows, base, q):
    spans = {}
    children = {}
    for r in rows:
        key = (r["trace_id"], r["span_id"])
        if key not in spans or r["event_name"] == "end":
            spans[key] = r
    roots = []
    for key, r in spans.items():
        parent = (
            (r["trace_id"], r["parent_span_id"])
            if r["parent_span_id"]
            else (r["link_trace_id"], r["link_span_id"])
        )
        if parent in spans:
            children.setdefault(parent, []).append(key)
        else:
            roots.append(
                (key, "Missing parent or linked span" if parent[1] else "Root")
            )
    visible = []
    visited = set()
    # Iterative walk remains bounded even if an imported history contains a cycle.
    for root, label in roots + [(k, "Cyclic or unresolved link") for k in spans]:
        stack = [(root, 0, label)]
        while stack:
            key, depth, note = stack.pop()
            if key in visited:
                continue
            visited.add(key)
            r = spans[key]
            op = link(
                base + "component",
                {**q, "service": r["service"], "operation": r["operation"]},
                r["operation"],
            )
            visible.append(
                [
                    '<span style="padding-left:'
                    + str(min(12, depth))
                    + 'em">'
                    + op
                    + "</span>",
                    esc(r["service"]),
                    esc(r["span_id"]),
                    esc(note),
                    ms(r["elapsed_ms"]),
                ]
            )
            for child in reversed(children.get(key, [])):
                stack.append(
                    (
                        child,
                        depth + 1,
                        "Async link" if spans[child]["link_span_id"] else "Child",
                    )
                )
    return table(["Operation", "Service", "Span", "Relationship", "Elapsed"], visible)


def ms(value):
    return "—" if value is None else f"{value:g} ms"


def page(body):
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Misty operations</title><style>'
        + STYLE
        + "</style>"
        + body
        + "</html>"
    ).encode()


def load_access(path):
    rows = json.loads(Path(path).read_text())
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
                    view = "overview"
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
                    + '/overview">Overview</a></main>',
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
    ThreadingHTTPServer(("127.0.0.1", a.port), handler).serve_forever()
