"""Aggregate feature and service health on the environment-bound dashboard."""

from collections import Counter
from datetime import datetime, timedelta
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
    # Complete one-minute buckets use stored counts and latency histograms.
    # Read projected attempts only at a fractional boundary or when their
    # lifetime crosses a minute. A terminal after the selected end stays unknown.
    left = datetime.fromisoformat(start.replace("Z", "+00:00"))
    rounded = left.replace(second=0, microsecond=0)
    if rounded != left:
        rounded += timedelta(minutes=1)
    first = rounded.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    last = end[:16] + ":00.000Z"
    rows = db.execute(
        """
        with cohort as (
          select service,operation,route,admitted,result,latency,n from health_minutes
            where traffic=:traffic and minute>=:first and minute<:last
          union all
          select service,operation,route,coalesce(admitted_at<:end,0) as admitted,
            case when ended<:end and (conflict_at is null or conflict_at>=:end) then result else 'unknown' end,
            case when ended<:end and (conflict_at is null or conflict_at>=:end) then latency else 0 end,
            1 from health_runs where traffic=:traffic and started>=:start and started<:end and (sealed=0 or minute<:first or minute>=:last)
        )
        select service,operation,route,admitted,result,latency,sum(n) as n,sum(latency*n) as total_ms
        from cohort group by service,operation,route,admitted,result,latency
    """,
        dict(traffic=traffic, first=first, last=last, start=start, end=end),
    ).fetchall()
    # Fold the histogram once into the UI totals while retaining exact full-answer percentiles.
    groups = {}
    durations = Counter()
    for r in rows:
        key = (r["service"], r["operation"], r["route"], r["admitted"], r["result"])
        out = groups.setdefault(
            key,
            dict(
                zip(("service", "operation", "route", "admitted", "result"), key),
                n=0,
                total_ms=0,
            ),
        )
        out["n"] += r["n"]
        out["total_ms"] += r["total_ms"]
        if r["operation"] == "answer" and r["result"] == "full":
            durations[r["latency"]] += r["n"]
    count = sum(durations.values())
    ranks = [(count - 1) // 2, count // 2] if count else []
    seen = 0
    middle = 0
    for elapsed, n in sorted(durations.items()):
        middle += elapsed * sum(seen <= rank < seen + n for rank in ranks)
        seen += n
    return list(groups.values()), middle / 2 if count else None


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
            "select ts,result,measures from health_jobs where traffic=? and operation=? and ts<? order by ts desc limit 1",
            (traffic, operation, end),
        ).fetchone()
        success = db.execute(
            "select max(ts) from health_jobs where traffic=? and operation=? and result=? and ts<?",
            (traffic, operation, good, end),
        ).fetchone()[0]
        finished = db.execute(
            "select max(ts) from health_jobs where traffic=? and operation=? and result in (?, 'gaps') and ts<?",
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
            m = json.loads(latest["measures"])
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
        "select ts,result,measures from health_jobs where traffic=? and operation='catalog.freshness' and ts<? order by ts desc limit 1",
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
    measures = json.loads(last["measures"])
    # A newer *finished* refresh need not imply a missing summary: the API
    # samples at guard release just before the runner emits its terminal event.
    # Compare the next refresh START with the sample, after its run has ended.
    missing_after_refresh = db.execute(
        """
        select 1 from health_jobs where operation='catalog.refresh' and traffic=? and started>? and ts<? limit 1
    """,
        (traffic, last["ts"], end),
    ).fetchone()

    eligible = measures.get("eligible")
    pairs = [
        (measures.get(name + "_current"), measures.get(name + "_unknown"))
        for name in ("content", "index", "access")
    ]
    if (
        type(eligible) is not int
        or eligible < 0
        or any(
            type(current) is not int
            or type(unknown) is not int
            or min(current, unknown) < 0
            or current + unknown > eligible
            for current, unknown in pairs
        )
    ):
        return (
            "".join(parts)
            + "<p>Incomplete or inconsistent catalog counts. Current document state unknown.</p>"
        )
    if missing_after_refresh or age >= 65 or last["result"] != "success":
        parts.append("<p>Current document state unknown.</p>")
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
    if not db.execute("select 1 from health_resources limit 1").fetchone():
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
            from health_resources where observed_at>=? and observed_at<? group by service order by service
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


def render_health(
    db, traffic, start, end, previous_start, now, stale_seconds=300, source_db=None
):
    parts = ["<h2>Workflow health</h2>"]
    source_status = []
    for service in (
        "discord-bot",
        "documentation-system",
        "gateway",
        "team-tracking",
        "verification",
    ):
        state = (
            (source_db or db)
            .execute(
                "select last_success,last_error,gaps,drops,rejected from sources where service=?",
                (service,),
            )
            .fetchone()
        )
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
        source_status.append(
            (
                service,
                stamp or "Unknown",
                status,
                *(state[2:] if state else ("—", "—", "—")),
            )
        )
    parts.append(
        _table(
            (
                "Service",
                "Last successful collection UTC",
                "Collection state",
                "Historical gaps",
                "Dropped events",
                "Rejected events",
            ),
            source_status,
        )
    )
    if any(row[2] != "Current" for row in source_status):
        parts.append(
            "<p>Missing or stale sources leave activity unknown. Counts below cover observed events only.</p>"
        )
    if any(
        any(isinstance(value, int) and value for value in row[3:])
        for row in source_status
    ):
        parts.append(
            "<p>Historical gaps, drops and rejections cover the collector's lifetime. A recent successful read cannot recover missing records.</p>"
        )
    currents, current_median = _cohort(db, traffic, start, end)
    previous, previous_median = _cohort(db, traffic, previous_start, start)
    summary = []
    searches = []
    components = []
    routes = []
    work = []
    for period, rows in (("Current", currents), ("Previous", previous)):
        left, right = (start, end) if period == "Current" else (previous_start, start)
        answers = _outcomes(rows, "answer")
        median = current_median if period == "Current" else previous_median
        marks = dict(
            db.execute(
                "select name,sum(n) from health_marks where traffic=? and ts>=? and ts<? group by name",
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
