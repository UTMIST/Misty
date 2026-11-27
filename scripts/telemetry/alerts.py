"""Evaluate recording and collector alerts against stored observations."""

import argparse
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dashboard_ui import sources
from recording_view import RUNBOOK
from project import ENVIRONMENTS, TRAFFIC, iso, parsed, readonly


def evaluate(raw, snap, traffic, now, window, minimum, failures, max_age):
    health = sources(raw, now, max_age)
    generated = snap.execute("select value from meta where key='generated'").fetchone()[
        0
    ]
    required = {"discord-bot", "meeting", "documentation-system", "connectors"}
    healthy = required.issubset({r["service"] for r, s in health if s == "Current"})
    recording_source = any(
        r["service"] == "discord-bot" and s == "Current" for r, s in health
    )
    fresh_snapshot = -60 <= (now - parsed(generated)).total_seconds() <= max_age
    count, bad, unsettled = snap.execute(
        """select count(*),coalesce(sum(outcome in ('interrupted','observed_error')),0),
            coalesce(sum(outcome in ('open','unconfirmed')),0)
         from attempts where started=1 and traffic=? and cohort>=? and cohort<?""",
        (traffic, iso(now - timedelta(minutes=window)), iso(now)),
    ).fetchone()
    recording = "unknown"
    if fresh_snapshot and recording_source and count >= minimum:
        if bad >= failures:
            recording = "breach"
        elif bad + unsettled < failures:
            recording = "healthy"
    return {
        "collection": (
            "healthy" if healthy and fresh_snapshot else "breach",
            "Collector coverage and recording snapshot",
        ),
        "recording": (
            recording,
            f"{bad} interrupted or failed of {count} started, {unsettled} unresolved in {window} minutes",
        ),
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def discord_send(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "discord.com"
        or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9_-]+", parts.path)
        or parts.query
        or parts.fragment
    ):
        raise ValueError("invalid Discord webhook")

    def deliver(content):
        data = json.dumps(
            {"content": content, "allowed_mentions": {"parse": []}}
        ).encode()
        req = Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Misty-Telemetry",
            },
        )
        try:
            with build_opener(NoRedirect()).open(req, timeout=5) as response:
                return 200 <= response.status < 300
        except (OSError, HTTPError):
            return False

    return deliver


def notify(
    state,
    env,
    traffic,
    conditions,
    *,
    now,
    owner,
    dashboard,
    window,
    send,
    silenced=False,
):
    state.execute("""create table if not exists incidents(environment text,traffic text,signal text,
                open integer,changed text,primary key(environment,traffic,signal))""")
    results = []
    for signal, (condition, detail) in conditions.items():
        # Source coverage is environment-wide; recording outcomes have a traffic cohort.
        group = "all" if signal == "collection" else traffic
        state.execute("begin immediate")
        try:
            row = state.execute(
                "select open from incidents where environment=? and traffic=? and signal=?",
                (env, group, signal),
            ).fetchone()
            active = bool(row and row[0])
            if silenced or condition == "unknown" or active == (condition == "breach"):
                state.rollback()
                continue
            kind = "breach" if condition == "breach" else "recovery"
            query = urlencode(
                {
                    "traffic": traffic,
                    "hours": max(
                        1, next((h for h in (1, 6, 24, 168) if h * 60 >= window), 168)
                    ),
                    "end": iso(now),
                }
            )
            content = f"Misty {env} · {signal} {kind} · {group}\n{detail}. Owner: {owner}.\n{dashboard.rstrip('/')}/{env}/overview?{query}\n{RUNBOOK}"
            ok = send(content)
            results.append((signal, kind, ok))
            if not ok:
                state.rollback()
                continue
            state.execute(
                "insert or replace into incidents values(?,?,?,?,?)",
                (env, group, signal, condition == "breach", iso(now)),
            )
            state.commit()
        except BaseException:
            state.rollback()
            raise
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("db", "snapshot", "state", "owner", "dashboard-url"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--environment", choices=ENVIRONMENTS, required=True)
    p.add_argument("--traffic", choices=TRAFFIC, required=True)
    for k in ("window-minutes", "min-started", "failure-count", "max-age-seconds"):
        p.add_argument("--" + k, type=int, required=True)
    p.add_argument("--silence-until", help="UTC instant ending planned maintenance")
    p.add_argument(
        "--send",
        action="store_true",
        help="Deliver notifications and persist acknowledged transitions",
    )
    a = p.parse_args()
    if (
        min(a.window_minutes, a.min_started, a.failure_count, a.max_age_seconds) <= 0
        or a.window_minutes > 7 * 24 * 60
    ):
        p.error("positive thresholds and a window up to seven days required")
    url = urlsplit(a.dashboard_url)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        p.error("invalid dashboard URL")
    now = datetime.now(timezone.utc)
    try:
        with (
            closing(readonly(a.db, a.environment)) as raw,
            closing(readonly(a.snapshot, a.environment)) as snap,
        ):
            conditions = evaluate(
                raw,
                snap,
                a.traffic,
                now,
                a.window_minutes,
                a.min_started,
                a.failure_count,
                a.max_age_seconds,
            )
    except (OSError, sqlite3.Error):
        conditions = {
            "collection": ("breach", "Collector or snapshot unreadable"),
            "recording": ("unknown", "Coverage unavailable"),
        }
    if not a.send:
        print(json.dumps(conditions))
        return
    state_path = Path(a.state)
    if not state_path.parent.is_dir() or state_path.parent.stat().st_mode & 0o077:
        p.error("incident state directory must be owner-only")
    if any(
        state_path.resolve() == Path(f).resolve()
        or (state_path.exists() and Path(f).exists() and state_path.samefile(f))
        for f in (a.db, a.snapshot)
    ):
        p.error("incident state requires a separate database")
    send = discord_send(os.environ.get("RECORDING_ALERT_WEBHOOK", ""))
    with closing(sqlite3.connect(a.state, timeout=15)) as state:
        result = notify(
            state,
            a.environment,
            a.traffic,
            conditions,
            now=now,
            owner=a.owner,
            dashboard=a.dashboard_url,
            window=a.window_minutes,
            send=send,
            silenced=bool(a.silence_until and now < parsed(a.silence_until)),
        )
    print(json.dumps(result))
    if any(not ok for _, _, ok in result):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
