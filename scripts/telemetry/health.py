"""Aggregate feature and service health on the environment-bound dashboard."""

from collections import Counter
from datetime import datetime
from html import escape
import json


def _table(headings, rows):
    def cells(values, tag):
        return "".join(f"<{tag}>{escape(str(value))}</{tag}>" for value in values)

    return (
        '<div class="scroll"><table><thead><tr>'
        + cells(headings, "th")
        + "</tr></thead><tbody>"
        + "".join("<tr>" + cells(row, "td") + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def _minutes(stamp, end):
    return max(
        0,
        int(
            (
                datetime.fromisoformat(end.replace("Z", "+00:00"))
                - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            ).total_seconds()
            // 60
        ),
    )


def _cohort(db, traffic, start, end):
    # A start defines the cohort. Terminal-only observations cannot inflate it.
    # Use only evidence observed by the period boundary, including for comparison.
    return db.execute(
        """
        WITH attempts AS (
          SELECT trace_id,span_id,service,operation,
             max(CASE WHEN event_name='start' THEN ts END) AS started,
             max(CASE WHEN event_name='start' THEN json_extract(payload,'$.route') END) AS route,
             max(event_name='admitted') AS admitted,
             count(DISTINCT CASE WHEN event_name='end' THEN result END) AS terminal_states,
             max(CASE WHEN event_name='end' THEN result END) AS result,
             max(CASE WHEN event_name='end' THEN elapsed_ms END) AS latency
          FROM events WHERE traffic=? AND ts>=? AND ts<? AND event_name IN ('start','admitted','end')
          GROUP BY trace_id,span_id,service,operation
        )
        SELECT service,operation,route,admitted,
          CASE WHEN terminal_states=1 THEN result ELSE 'unknown' END AS result,
          count(*) AS n, sum(CASE WHEN terminal_states=1 THEN latency ELSE 0 END) AS total_ms
        FROM attempts WHERE started IS NOT NULL GROUP BY service,operation,route,admitted,
          CASE WHEN terminal_states=1 THEN result ELSE 'unknown' END
    """,
        (traffic, start, end),
    ).fetchall()


def _answer_latency(db, traffic, start, end):
    # Median over fully delivered answer attempts in the receipt cohort.
    return db.execute(
        """
        WITH completed AS (
          SELECT max(CASE WHEN event_name='end' THEN elapsed_ms END) AS ms,
                 max(event_name='start') AS started, count(DISTINCT CASE WHEN event_name='end' THEN result END) AS states,
                 max(CASE WHEN event_name='end' THEN result END) AS result
          FROM events WHERE traffic=? AND operation='answer' AND ts>=? AND ts<?
          GROUP BY trace_id,span_id
        ), ranked AS (
          SELECT ms,row_number() OVER(ORDER BY ms) AS i,count(*) OVER() AS n
          FROM completed WHERE started AND states=1 AND result='full'
        )
        SELECT avg(ms) FROM ranked WHERE i IN ((n+1)/2,(n+2)/2)
    """,
        (traffic, start, end),
    ).fetchone()[0]


def _outcomes(rows, operation):
    counts = Counter()
    for r in rows:
        if r["operation"] == operation:
            counts[r["result"]] += r["n"]
    return counts


def _catalog(db, traffic, end):
    parts = ["<h2>Catalog evidence</h2>"]
    for operation, label, good in (
        ("catalog.discovery", "Discovery", "clean"),
        ("catalog.refresh", "Refresh", "drained"),
    ):
        latest = db.execute(
            "select ts,result,payload from events where traffic=? and operation=? and event_name='end' and ts<? order by ts desc limit 1",
            (traffic, operation, end),
        ).fetchone()
        success = db.execute(
            "select max(ts) from events where traffic=? and operation=? and event_name='end' and result=? and ts<?",
            (traffic, operation, good, end),
        ).fetchone()[0]
        finished = db.execute(
            "select max(ts) from events where traffic=? and operation=? and event_name='end' and result in (?, 'gaps') and ts<?",
            (traffic, operation, good, end),
        ).fetchone()[0]
        row = (
            label,
            latest["ts"] if latest else "Unknown",
            latest["result"] if latest else "Unknown",
            finished or "Unknown",
            success or "Unknown",
        )
        parts.append(
            _table(
                (
                    "Job",
                    "Last observed",
                    "Result",
                    "Last completed",
                    "Last clean completion",
                ),
                [row],
            )
        )
        if latest and operation == "catalog.refresh":
            m = json.loads(latest["payload"]).get("measures", {})
            parts.append(
                _table(
                    ("Attempted", "Item failures", "Index failures"),
                    [
                        [
                            m.get(k, "Unknown")
                            for k in ("attempted", "item_failed", "index_failed")
                        ]
                    ],
                )
            )
    last = db.execute(
        "select ts,result,payload from events where traffic=? and operation='catalog.freshness' and event_name='end' and ts<? order by ts desc limit 1",
        (traffic, end),
    ).fetchone()
    if not last:
        return (
            "".join(parts)
            + "<p>No catalog summary received. Current state unknown.</p>"
        )
    age = _minutes(last["ts"], end)
    parts.append(
        f"<p>Catalog summary at {escape(last['ts'])} · {age} min old at period end.</p>"
    )
    measures = json.loads(last["payload"]).get("measures", {})
    # A newer *finished* refresh need not imply a missing summary: the API
    # samples at guard release just before the runner emits its terminal event.
    # Compare the next refresh START with the sample, after its run has ended.
    missing_after_refresh = db.execute(
        """
        select 1 from events s join events e using(trace_id,span_id)
        where s.operation='catalog.refresh' and s.event_name='start' and e.event_name='end'
        and s.traffic=? and e.traffic=? and s.ts>? and e.ts<? limit 1
    """,
        (traffic, traffic, last["ts"], end),
    ).fetchone()

    if (
        missing_after_refresh
        or age >= 65
        or last["result"] != "success"
        or "eligible" not in measures
    ):
        parts.append("<p>Current document state unknown.</p>")
    if not measures or "eligible" not in measures:
        return "".join(parts)
    rows = []
    for name in ("content", "index", "access"):
        rows.append(
            (
                name,
                measures["eligible"],
                measures.get(name + "_current", "Unknown"),
                measures.get(name + "_unknown", "Unknown"),
            )
        )
    return "".join(parts) + _table(
        ("Last observed measure", "Eligible", "Current at sample", "Unknown at sample"),
        rows,
    )


def _resources(db, start, end, previous_start):
    parts = [
        "<h2>Railway resource samples</h2><p>Environment-wide samples across traffic groups.</p>"
    ]
    if not db.execute(
        "select 1 from sqlite_master where type='table' and name='railway_samples'"
    ).fetchone():
        return (
            "".join(parts)
            + "<p>No resource samples imported. CPU, memory and deployment time unknown.</p>"
        )
    rows = []
    for period, left, right in (
        ("Current", start, end),
        ("Previous", previous_start, start),
    ):
        for service, last, n, cpu, mem, deploy in db.execute(
            """
            select service,max(observed_at),count(*),avg(cpu_cores),max(memory_bytes),max(deployed_at)
            from railway_samples where observed_at>=? and observed_at<? group by service order by service
        """,
            (left, right),
        ):
            rows.append(
                (
                    period,
                    service,
                    n,
                    f"{cpu:.3f}",
                    f"{mem / 1048576:.1f}",
                    last,
                    _minutes(last, right),
                    deploy or "Unknown",
                )
            )
    for period in ("Current", "Previous"):
        if not any(row[0] == period for row in rows):
            parts.append(
                f"<p>{period} period has no resource samples. Resource use unknown.</p>"
            )
    return "".join(parts) + (
        _table(
            (
                "Period",
                "Service",
                "Samples",
                "Mean CPU cores",
                "Peak MiB",
                "Last sample UTC",
                "Age (min)",
                "Latest deployment UTC",
            ),
            rows,
        )
        if rows
        else "<p>No samples in either period. Resource use unknown.</p>"
    )


def render_health(db, traffic, start, end, previous_start, now, stale_seconds=300):
    parts = ["<h2>Workflow health</h2>"]
    source_status = []
    for service in (
        "discord-bot",
        "documentation-system",
        "gateway",
        "team-tracking",
        "verification",
    ):
        state = db.execute(
            "select last_success,last_error from sources where service=?", (service,)
        ).fetchone()
        stamp = state[0] if state else None
        status = (
            "No receipt"
            if not stamp
            else (
                "Read failed"
                if state[1]
                else (
                    "Stale"
                    if not -60
                    <= (
                        datetime.fromisoformat(now.replace("Z", "+00:00"))
                        - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    ).total_seconds()
                    <= stale_seconds
                    else "Current"
                )
            )
        )
        source_status.append((service, stamp or "Unknown", status))
    parts.append(
        _table(
            ("Service", "Last successful collection UTC", "Collection state"),
            source_status,
        )
    )
    if any(row[-1] != "Current" for row in source_status):
        parts.append(
            "<p>Missing or stale sources leave activity unknown. Counts below cover observed events only.</p>"
        )
    currents = _cohort(db, traffic, start, end)
    previous = _cohort(db, traffic, previous_start, start)
    summary = []
    searches = []
    components = []
    routes = []
    work = []
    for period, rows in (("Current", currents), ("Previous", previous)):
        left, right = (start, end) if period == "Current" else (previous_start, start)
        answers = _outcomes(rows, "answer")
        median = _answer_latency(db, traffic, left, right)
        marks = dict(
            db.execute(
                "select event_name,count(*) from events where traffic=? and ts>=? and ts<? and (operation,event_name) in (('answer.model','tool_round'),('answer.delivery','part_sent')) group by event_name",
                (traffic, left, right),
            )
        )
        work.append(
            (
                period,
                marks.get("tool_round", 0),
                marks.get("part_sent", 0),
                answers["full"],
                round(median) if median is not None else "—",
            )
        )
        accepted = sum(
            r["n"] for r in rows if r["operation"] == "answer" and r["admitted"]
        )
        summary.append(
            [period, sum(answers.values()), accepted]
            + [
                answers[k]
                for k in (
                    "full",
                    "partial",
                    "withheld",
                    "denied",
                    "rate_limited",
                    "timeout",
                    "cancelled",
                    "action",
                    "failed",
                    "unavailable",
                    "unknown",
                )
            ]
        )
        search = _outcomes(rows, "catalog.search")
        complete = search["results"] + search["no_hits"]
        searches.append(
            (
                period,
                search["results"],
                search["no_hits"],
                f"{search['no_hits']}/{complete}" if complete else "—",
                search["no_channel_teams"],
                search["denied"],
                search["invalid_request"],
                search["directory_unavailable"]
                + search["embedding_error"]
                + search["storage_unavailable"]
                + search["internal_error"],
                search["unknown"],
            )
        )
        route_counts = {}
        for r in rows:
            op, result, n = r["operation"], r["result"], r["n"]
            if op == "http.request":
                key = (r["service"], r["route"] or "other")
                totals = route_counts.setdefault(key, Counter())
                totals[result] += n
                totals["duration"] += r["total_ms"]
            elif op.startswith(("answer.", "login.", "catalog.")):
                components.append(
                    (
                        period,
                        r["service"],
                        op,
                        result,
                        n,
                        "—" if result == "unknown" else round(r["total_ms"] / n),
                    )
                )
        for (service, route), values in sorted(route_counts.items()):
            elapsed = values.pop("duration")
            all_count = sum(values.values())
            finished = all_count - values["unknown"]
            routes.append(
                (
                    period,
                    service,
                    route,
                    all_count,
                    values["denied"],
                    values["rate_limited"],
                    values["invalid_request"],
                    values["failed"],
                    values["unknown"],
                    round(elapsed / finished) if finished else "—",
                )
            )
    parts.append(
        _table(
            (
                "Period",
                "Received",
                "Admitted",
                "Full",
                "Partial",
                "Withheld",
                "Denied",
                "Rate limited",
                "Timeout",
                "Cancelled",
                "Action",
                "Failed",
                "Unavailable",
                "Unknown",
            ),
            summary,
        )
    )
    parts.append(
        "<h2>Authorized search</h2>"
        + _table(
            (
                "Period",
                "Results",
                "No hits",
                "Empty / complete",
                "No channel teams",
                "Denied",
                "Invalid",
                "Failure",
                "Unknown",
            ),
            searches,
        )
    )
    parts.append(
        _table(
            (
                "Period",
                "Tool rounds requested",
                "Answer parts acknowledged",
                "Full latency samples",
                "Median receipt-to-delivery ms",
            ),
            work,
        )
    )
    parts.append(_catalog(db, traffic, end))
    parts.append(
        "<h2>Workflow components</h2>"
        + (
            _table(
                ("Period", "Service", "Operation", "Result", "Calls", "Mean ms"),
                components,
            )
            if components
            else "<p>No component observations.</p>"
        )
    )
    parts.append(
        "<h2>HTTP route templates</h2>"
        + (
            _table(
                (
                    "Period",
                    "Service",
                    "Template",
                    "Requests",
                    "Denied",
                    "Rate limited",
                    "Invalid",
                    "Server error",
                    "Unknown",
                    "Mean finished ms",
                ),
                routes,
            )
            if routes
            else "<p>No route observations.</p>"
        )
    )
    parts.append(_resources(db, start, end, previous_start))
    return "".join(parts)
