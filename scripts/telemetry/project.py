"""Build an environment-scoped, read-only UI snapshot from the local collector."""

import argparse
import fcntl
import json
import math
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ENVIRONMENTS = ("local", "staging", "production")
TRAFFIC = ("synthetic", "member", "background")
READER = Path(__file__).resolve().parents[2] / "discord-bot/scripts/recording-report.js"


def iso(dt):
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parsed(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("UTC offset required")
    return dt


def readonly(path, environment):
    db = sqlite3.connect(
        "file:" + quote(str(Path(path).resolve())) + "?mode=ro", uri=True
    )
    db.row_factory = sqlite3.Row
    try:
        db.execute("pragma query_only=on")
        db.execute("begin")
        row = db.execute("select value from meta where key='environment'").fetchone()
        if not row or row[0] != environment:
            raise ValueError("environment mismatch")
        return db
    except BaseException:
        db.close()
        raise


def percentiles(values):
    values = sorted(v for v in values if v is not None)
    n = len(values)
    if not n:
        return n, None, None
    median = (values[(n - 1) // 2] + values[n // 2]) / 2
    return n, median, values[math.ceil(n * 0.95) - 1] if n >= 20 else None


def build(source, output, env, now=None):
    output = Path(output)
    directory = output.parent.stat()
    if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise ValueError("snapshot directory must be owner-only")
    fd = os.open(str(output) + ".lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        # Serialize rebuilds before opening the read snapshot. An older run
        # may never replace a fresher projection.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if output.exists():
            if Path(source).samefile(output):
                raise ValueError("source and snapshot must be different files")
            with closing(readonly(output, env)) as previous:
                if not previous.execute(
                    "select value from meta where key='generated'"
                ).fetchone():
                    raise ValueError("output must be a recording snapshot")
        rebuild(source, output, env, now)


def rebuild(source, output, env, now=None):
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError("source and snapshot must be different files")
    now = now or datetime.now(timezone.utc)
    cutoff = iso(now - timedelta(days=30))
    raw = sqlite3.connect("")
    raw.row_factory = sqlite3.Row
    try:
        with closing(readonly(source, env)) as source_db:
            source_db.backup(raw)
    except BaseException:
        raw.close()
        raise
    fd, temporary = tempfile.mkstemp(
        dir=Path(output).parent, prefix=".recording-snapshot-"
    )
    os.close(fd)
    snap = sqlite3.connect(temporary)
    try:
        snap.executescript("""
            create table meta(key text primary key,value text);
            create table attempts(trace_id text primary key, traffic text, cohort text, started integer,
              mode text, outcome text, stop text, drain text, save text, link text, pdf text, latency_ms integer,last_seen text);
            create index cohort on attempts(traffic,cohort);
            create table spans(trace_id text,span_id text,parent text,link_trace text,link_span text,
              service text, operation text,traffic text,start text,finish text,result text,elapsed_ms integer,
              primary key(trace_id,span_id));
            create index component on spans(traffic,service,operation,start);
        """)
        snap.executemany(
            "insert into meta values (?,?)",
            [("environment", env), ("generated", iso(now))],
        )
        # A newly observed boot makes a start in an older process inconclusive.
        # Legacy recording lines lack boot IDs. Only classify them as open if
        # a bot boot in this environment is known to predate capture.
        boots = dict(
            raw.execute(
                """select service,max(json_extract(payload,'$.process_started_at'))
            from events where ts>=? and json_extract(payload,'$.process_started_at') is not null
            group by service""",
                (cutoff,),
            )
        )
        with tempfile.TemporaryFile() as events, tempfile.TemporaryFile() as reports:
            # Raw messages already passed the collector allowlist. Replay the bot
            # schema through the same reducer as the single-trace investigation CLI.
            for (payload,) in raw.execute(
                """select payload from events where service='discord-bot'
                    and ts>=? and json_extract(payload,'$.schema')='misty.recording.v1'
                    order by trace_id""",
                (cutoff,),
            ):
                events.write(payload.encode() + b"\n")
            events.seek(0)
            subprocess.run(
                ["node", str(READER), "--grouped"],
                stdin=events,
                stdout=reports,
                check=True,
            )
            reports.seek(0)
            for line in reports:
                r = json.loads(line)
                row = raw.execute(
                    """select
                   min(case when event_name like 'record.%requested' then ts end) requested,
                   min(case when event_name='capture.started' then ts end) started,
                   min(case when event_name in ('capture.stop_requested','capture.empty_channel') then ts end) stopped,
                   min(case when event_name='pdf.posted' then ts end) pdf,
                   min(case when event_name='link.posted' then ts end) link,max(ts) last_seen
                   from events where trace_id=? and service='discord-bot' and ts>=?""",
                    (r["trace_id"], cutoff),
                ).fetchone()
                cohort = row["started"] or row["requested"]
                if not cohort:  # orphan recovery cannot create a second recording
                    continue
                traffic = raw.execute(
                    """select traffic from events where trace_id=? and service='discord-bot'
                       and ts=? and event_name in ('capture.started','record.saved_requested','record.pdf_requested')
                       order by event_id limit 1""",
                    (r["trace_id"], cohort),
                ).fetchone()[0]
                latency = None
                if r["outcome"] == "complete" and row["stopped"] and row["pdf"]:
                    ack = (
                        max(row["pdf"], row["link"] or row["pdf"])
                        if r["mode"] == "record.saved_requested"
                        else row["pdf"]
                    )
                    diff = round(
                        (parsed(ack) - parsed(row["stopped"])).total_seconds() * 1000
                    )
                    latency = diff if diff >= 0 else None
                outcome = r["outcome"]
                if (
                    outcome == "unconfirmed"
                    and row["started"]
                    and r["stop"] == "unknown"
                    and set(r["events"])
                    <= {
                        "record.saved_requested",
                        "record.pdf_requested",
                        "capture.started",
                    }
                    and boots.get("discord-bot")
                    and row["started"] >= boots["discord-bot"]
                ):
                    outcome = "open"
                snap.execute(
                    "insert into attempts values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r["trace_id"],
                        traffic,
                        cohort,
                        bool(row["started"]),
                        r["mode"],
                        outcome,
                        r["stop"],
                        r["drain"],
                        r["save"],
                        r["link"],
                        r["pdf"],
                        latency,
                        row["last_seen"],
                    ),
                )
        # Timed spans have one stable operation. Starts and ends may arrive in any order.
        # Missing ends keep their start and unknown outcome. Count retries as separate spans.
        rows = raw.execute(
            """select trace_id,span_id,min(parent_span_id),min(link_trace_id),min(link_span_id),
              min(service),min(operation),min(traffic),
              coalesce(min(case when event_name='start' then ts end),min(ts)),
              max(case when event_name='end' then ts end),
              coalesce(min(case when event_name='end' then result end),'unknown'),
              max(case when event_name='end' then elapsed_ms end),
              max(case when event_name='start' then json_extract(payload,'$.process_started_at') end)
              from events where ts>=? and json_extract(payload,'$.schema')='misty.telemetry.v1'
              group by trace_id,span_id""",
            (cutoff,),
        )
        for row in rows:
            span = list(row[:-1])
            if span[9] is None and row[-1] and boots.get(span[5]) == row[-1]:
                span[10] = "open"
            snap.execute("insert into spans values(?,?,?,?,?,?,?,?,?,?,?,?)", span)
        snap.commit()
        snap.close()
        os.replace(temporary, output)
    finally:
        raw.close()
        snap.close()
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--environment", choices=ENVIRONMENTS, required=True)
    a = p.parse_args()
    build(a.db, a.snapshot, a.environment)
