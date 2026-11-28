"""Private SQLite store for the pilot collector. Dependencies are Python stdlib only."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "packages/auth/platform_auth/telemetry_contract.json"
    ).read_text()
)
SERVICES = sorted(set(CONTRACT["operations"]) - {"common"})
ENVIRONMENTS = ("local", "staging", "production")
TRAFFIC = ("synthetic", "member", "background")
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
RETENTION = timedelta(days=29)
LEGACY = "misty.recording.v1"
SHARED = "misty.telemetry.v1"
BASE = {
    "schema",
    "trace_id",
    "span_id",
    "event_name",
    "ts",
    "elapsed_ms",
    "dropped_before",
}
SHARED_REQUIRED = BASE | {
    "event_id",
    "process_id",
    "environment",
    "traffic",
    "service",
    "operation",
    "result",
    "deployment",
}
SHARED_OPTIONAL = {
    "sequence",
    "process_started_at",
    "parent_span_id",
    "link_trace_id",
    "link_span_id",
    "route",
    "measures",
}


def utc(value):
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires offset")
    return parsed.astimezone(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def natural(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def identity(value, size):
    return (
        isinstance(value, str)
        and len(value) == size
        and value != "0" * size
        and re.fullmatch("[a-f0-9]+", value)
    )


def validate(p, service, env):
    if not isinstance(p, dict) or not BASE <= p.keys():
        return False
    if not identity(p["trace_id"], 32) or not identity(p["span_id"], 16):
        return False
    if "parent_span_id" in p and not identity(p["parent_span_id"], 16):
        return False
    if not natural(p["elapsed_ms"]) or not natural(p["dropped_before"]):
        return False
    if p["schema"] == LEGACY:
        return (
            service == "discord-bot"
            and p.keys() <= BASE | {"parent_span_id"}
            and p["event_name"] in CONTRACT["recording_events"]
        )
    if (
        p["schema"] != SHARED
        or not SHARED_REQUIRED <= p.keys() <= SHARED_REQUIRED | SHARED_OPTIONAL
    ):
        return False
    if "sequence" in p and (not natural(p["sequence"]) or not p["sequence"]):
        return False
    if (
        "process_started_at" in p
        and not iso(utc(p["process_started_at"])) == p["process_started_at"]
    ):
        return False
    op = p["operation"]
    if (
        service != p["service"]
        or env != p["environment"]
        or p["traffic"] not in TRAFFIC
        or op not in CONTRACT["operations"][service] + CONTRACT["operations"]["common"]
        or p["result"] not in CONTRACT["result"]
        or not identity(p["event_id"], 32)
        or not identity(p["process_id"], 16)
        or not isinstance(p["deployment"], str)
        or not re.fullmatch("unknown|" + UUID + "|[0-9a-f]{40}", p["deployment"])
    ):
        return False
    if p["event_name"] not in ["start", "end"] + CONTRACT["marks"].get(op, []):
        return False
    if (p["event_name"] == "start") != (p["result"] == "started"):
        return False
    if "route" in p and (op != "http.request" or p["route"] not in CONTRACT["routes"]):
        return False
    if ("link_span_id" in p) != ("link_trace_id" in p):
        return False
    if "link_span_id" in p and not (
        identity(p["link_span_id"], 16) and identity(p["link_trace_id"], 32)
    ):
        return False
    measures = p.get("measures", {})
    return (
        isinstance(measures, dict)
        and (not measures or p["event_name"] == "end")
        and all(
            k in CONTRACT["measures"].get(op, []) and natural(v)
            for k, v in measures.items()
        )
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 event_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,span_id TEXT NOT NULL,parent_span_id TEXT,
 link_trace_id TEXT,link_span_id TEXT,ts TEXT NOT NULL,received_at TEXT NOT NULL,
 service TEXT NOT NULL,operation TEXT NOT NULL,event_name TEXT NOT NULL,result TEXT NOT NULL,
 elapsed_ms INTEGER NOT NULL,traffic TEXT NOT NULL,deployment TEXT NOT NULL,payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_time ON events(ts);
CREATE INDEX IF NOT EXISTS trace ON events(trace_id,span_id);
CREATE UNIQUE INDEX IF NOT EXISTS span_start ON events(trace_id,span_id) WHERE event_name='start';
CREATE TABLE IF NOT EXISTS sources(
 service TEXT PRIMARY KEY, checkpoint TEXT,observed_at TEXT,last_success TEXT,last_error TEXT,
 gaps INTEGER NOT NULL DEFAULT 0,rejected INTEGER NOT NULL DEFAULT 0,drops INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS summaries(
 service TEXT NOT NULL,traffic TEXT NOT NULL,operation TEXT NOT NULL,result TEXT NOT NULL,bucket TEXT NOT NULL,
 count INTEGER NOT NULL,total_ms INTEGER NOT NULL,max_ms INTEGER NOT NULL,latency_count INTEGER NOT NULL,
 PRIMARY KEY(service,traffic,operation,result,bucket)
);
"""


def connect(path, env):
    # Put DB, rollback journals, and private snapshots on an operator-owned volume.
    path = Path(path)
    directory = path.parent.stat()
    if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise ValueError("database directory must be owner-only")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("database must be owner-only")
    finally:
        os.close(fd)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute("pragma secure_delete=ON")
        db.execute("pragma journal_mode=DELETE")
        db.executescript(SCHEMA)
        with db:
            bind(db, "environment", env)
            old = db.execute(
                "select sql from sqlite_master where name='span_end_observation'"
            ).fetchone()
            if old and "elapsed_ms" not in old[0]:
                db.execute("drop index span_end_observation")
            db.execute("drop index if exists terminal")
            # Canonical payload JSON sorts the measure names; missing and empty
            # measures carry identical evidence.
            db.execute("""create unique index if not exists span_end_observation on
                events(trace_id,span_id,ts,result,elapsed_ms,coalesce(json_extract(payload,'$.measures'),'{}'))
                where event_name='end'""")
            if "latency_count" not in {
                r[1] for r in db.execute("pragma table_info(summaries)")
            }:
                db.execute(
                    "alter table summaries add column latency_count integer not null default 0"
                )
                db.execute("update summaries set latency_count=count")
                # Reconcile preexisting disagreements. Older collectors also
                # discarded the entire first partial hour although some rows survived.
                affected = db.execute("""select distinct service,traffic,operation,hour from (
                    select service,traffic,operation,substr(min(ts),1,13) hour from events
                    where event_name='end' group by trace_id,span_id having count(*)>1)
                    union select service,traffic,operation,substr(ts,1,13) from events
                    where event_name='end' and substr(ts,1,13)=(select substr(min(ts),1,13) from events)""")
                for key in affected:
                    rebuild_summary_hour(db, *key)
        return db
    except BaseException:
        db.close()
        raise


def bind(db, key, expected):
    db.execute("insert or ignore into meta values(?,?)", (key, expected))
    if (
        db.execute("select value from meta where key=?", (key,)).fetchone()[0]
        != expected
    ):
        raise ValueError("source binding mismatch")


def prune(db, now):
    cutoff = iso(now - RETENTION)
    hour = cutoff[:13]
    with db:
        # An expiring end can change the first surviving hour or resolve a
        # disagreement. Recompute only the affected groups that retain evidence.
        if not db.in_transaction:
            db.execute("begin immediate")
        groups = db.execute(
            """select distinct service,traffic,operation,substr(first_kept,1,13) from (
                 select old.service,old.traffic,old.operation,
                   (select min(peer.ts) from events peer where peer.trace_id=old.trace_id
                      and peer.span_id=old.span_id and peer.event_name='end' and peer.ts>=:cutoff) first_kept
                 from events old indexed by event_time where old.ts<:cutoff and old.event_name='end'
               ) where first_kept is not null
               union select distinct service,traffic,operation,substr(ts,1,13) from events indexed by event_time
                 where event_name='end' and ts>=:hour and ts<:cutoff""",
            {"cutoff": cutoff, "hour": hour + ":00:00.000Z"},
        ).fetchall()
        db.execute("delete from events where ts < ?", (cutoff,))
        if db.execute(
            "select 1 from sqlite_master where type='table' and name='railway_samples'"
        ).fetchone():
            db.execute("delete from railway_samples where observed_at < ?", (cutoff,))
        db.execute("delete from summaries where bucket < ?", (hour + ":00:00.000Z",))
        for key in groups:
            rebuild_summary_hour(db, *key)


def rebuild_summary_hour(db, service, traffic, operation, hour):
    """Rebuild one group from its retained terminal evidence."""
    bucket = hour + ":00:00.000Z"
    stop = iso(utc(bucket) + timedelta(hours=1))
    key = dict(
        service=service, traffic=traffic, operation=operation, bucket=bucket, stop=stop
    )
    db.execute(
        """delete from summaries where service=:service and traffic=:traffic
           and operation=:operation and bucket=:bucket""",
        key,
    )
    # Group all terminal observations of each span under its earliest end.
    # A consistent result survives conflicting timings. Only a consistent
    # result and elapsed value supply a latency sample.
    db.execute(
        """with observations as (
             select min(peer.result) lo,max(peer.result) hi,
                    min(peer.elapsed_ms) shortest,max(peer.elapsed_ms) longest
             from events e indexed by event_time join events peer on peer.trace_id=e.trace_id and peer.span_id=e.span_id
               and peer.event_name='end'
             where e.service=:service and e.traffic=:traffic and e.operation=:operation
               and e.event_name='end' and e.ts>=:bucket and e.ts<:stop and not exists (
                 select 1 from events prior where prior.trace_id=e.trace_id and prior.span_id=e.span_id
                   and prior.event_name='end' and (prior.ts,prior.event_id)<(e.ts,e.event_id))
             group by e.trace_id,e.span_id
           ), outcomes as (
             select case when lo=hi then lo else 'unknown' end outcome,
                    case when lo=hi and shortest=longest then shortest end elapsed from observations
           )
           insert into summaries(service,traffic,operation,result,bucket,count,total_ms,max_ms,latency_count)
           select :service,:traffic,:operation,outcome,:bucket,count(*),coalesce(sum(elapsed),0),
                  coalesce(max(elapsed),0),count(elapsed) from outcomes group by outcome""",
        key,
    )


def insert(db, p, service, traffic, received):
    payload = json.dumps(p, sort_keys=True, separators=(",", ":"), allow_nan=False)
    event_id = p.get("event_id") or hashlib.sha256(payload.encode()).hexdigest()
    result = p.get("result", "unknown")
    ts = iso(utc(p["ts"]))
    values = (
        event_id,
        p["trace_id"],
        p["span_id"],
        p.get("parent_span_id"),
        p.get("link_trace_id"),
        p.get("link_span_id"),
        ts,
        received,
        service,
        p.get("operation", "recording"),
        p["event_name"],
        result,
        p["elapsed_ms"],
        p.get("traffic", traffic),
        p.get("deployment", "unknown"),
        payload,
    )
    replay = db.execute(
        "select payload from events where event_id=?", (event_id,)
    ).fetchone()
    if replay:
        return ("duplicate" if replay[0] == payload else "rejected"), 0
    shared = p["schema"] == SHARED
    span_ids = (p["trace_id"], p["span_id"])
    if (
        shared
        and db.execute(
            """select 1 from events where trace_id=? and span_id=?
           and (service,traffic,operation)!=(?,?,?) and json_extract(payload,'$.schema')=?
           limit 1""",
            (*span_ids, service, p["traffic"], p["operation"], SHARED),
        ).fetchone()
    ):
        return "rejected", 0
    terminal = shared and p["event_name"] == "end"
    previous = None
    if terminal:
        previous = db.execute(
            """select min(ts),min(result),max(result),min(elapsed_ms),max(elapsed_ms)
               from events where trace_id=? and span_id=? and event_name='end' group by trace_id,span_id""",
            span_ids,
        ).fetchone()
    if not db.execute(
        "insert or ignore into events values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
    ).rowcount:
        return "rejected", 0
    if previous:
        first_ts, lo, hi, shortest, longest = previous
        moves_hour = ts[:13] < first_ts[:13]
        new_dispute = lo == hi and (
            result != lo or (shortest == longest and shortest != p["elapsed_ms"])
        )
        if moves_hour or new_dispute:
            # An out-of-order earlier end can move the span to a different hour.
            for hour in {first_ts[:13], min(ts, first_ts)[:13]}:
                rebuild_summary_hour(db, service, p["traffic"], p["operation"], hour)
    elif terminal:
        db.execute(
            """insert into summaries(service,traffic,operation,result,bucket,count,total_ms,max_ms,latency_count) values(?,?,?,?,?,1,?,?,1)
                     on conflict(service,traffic,operation,result,bucket) do update set
                     count=count+1,latency_count=latency_count+1,total_ms=total_ms+excluded.total_ms,max_ms=max(max_ms,excluded.max_ms)""",
            (
                service,
                p["traffic"],
                p["operation"],
                result,
                ts[:13] + ":00:00.000Z",
                p["elapsed_ms"],
                p["elapsed_ms"],
            ),
        )
    return "added", p["dropped_before"]
