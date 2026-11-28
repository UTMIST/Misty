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
CREATE UNIQUE INDEX IF NOT EXISTS terminal ON events(trace_id,span_id,event_name)
 WHERE event_name IN ('start','end');
CREATE TABLE IF NOT EXISTS sources(
 service TEXT PRIMARY KEY, checkpoint TEXT,observed_at TEXT,last_success TEXT,last_error TEXT,
 gaps INTEGER NOT NULL DEFAULT 0,rejected INTEGER NOT NULL DEFAULT 0,drops INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS summaries(
 service TEXT NOT NULL,traffic TEXT NOT NULL,operation TEXT NOT NULL,result TEXT NOT NULL,bucket TEXT NOT NULL,
 count INTEGER NOT NULL,total_ms INTEGER NOT NULL,max_ms INTEGER NOT NULL,
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
    # Drop the partially expired summary hour as its contributing spans age out.
    with db:
        db.execute("delete from events where ts < ?", (cutoff,))
        if db.execute(
            "select 1 from sqlite_master where type='table' and name='railway_samples'"
        ).fetchone():
            db.execute("delete from railway_samples where observed_at < ?", (cutoff,))
        db.execute(
            "delete from summaries where bucket <= ?", (cutoff[:13] + ":00:00.000Z",)
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
    added = db.execute(
        "insert or ignore into events values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
    ).rowcount
    if not added:
        existing = db.execute(
            "select payload from events where event_id=?", (event_id,)
        ).fetchone()
        return ("duplicate" if existing and existing[0] == payload else "rejected"), 0
    if p["schema"] == SHARED and p["event_name"] == "end":
        db.execute(
            """insert into summaries values(?,?,?,?,?,1,?,?)
                     on conflict(service,traffic,operation,result,bucket) do update set
                     count=count+1,total_ms=total_ms+excluded.total_ms,max_ms=max(max_ms,excluded.max_ms)""",
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
