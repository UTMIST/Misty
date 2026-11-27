"""Render stored recording outcomes, restricted timelines and component views."""

import math
import re

from dashboard_ui import DETAIL_ROLES, esc, link, table
from project import iso

RUNBOOK = "https://github.com/UTMIST/Misty/blob/staging/docs/RECORDING-TELEMETRY.md"
RUNBOOKS = {
    "meeting": "services/meeting/docs/DEPLOYMENT.md#troubleshooting",
    "documentation-system": "services/documentation-system/docs/DEPLOYMENT.md#meeting-record-persistence",
    "connectors": "services/connectors/docs/DEPLOYMENT.md",
    "discord-bot": "discord-bot/docs/meeting-delivery.md#recovery",
}


def percentiles(values):
    values = sorted(v for v in values if v is not None)
    n = len(values)
    if not n:
        return n, None, None
    median = (values[(n - 1) // 2] + values[n // 2]) / 2
    return n, median, values[math.ceil(n * 0.95) - 1] if n >= 20 else None


def render_recording(db, raw, view, params, role, base, period, flags, aged):
    traffic = period.traffic
    start, end, previous = period.start, period.end, period.previous
    retention_floor = period.retention_floor
    q = period.query
    body = ""
    healthy = (
        any(
            r["service"] == "discord-bot" and status == "Current" for r, status in flags
        )
        and not aged
    )

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
                    and (
                        mode == "record.pdf_requested"
                        or r["link"] in ("link.posted", "link.acknowledged")
                    )
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
            (trace, traffic, iso(max(start, retention_floor)), iso(end)),
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
                iso(retention_floor),
                traffic,
                iso(retention_floor),
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
    else:
        raise LookupError
    return body + '<p><a href="' + RUNBOOK + '">Recording operator runbook</a></p>'


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
