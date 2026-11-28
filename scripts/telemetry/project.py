"""Build an environment-scoped, read-only UI snapshot from the local collector."""

import argparse
import fcntl
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from urllib.parse import quote

from store import RETENTION

ENVIRONMENTS = ("local", "staging", "production")
TRAFFIC = ("synthetic", "member", "background")
# An independently installed workflow supplies its projection and HTTP view.
FEATURES = tuple(
    name
    for name in ("recording", "health")
    if Path(__file__).with_name(name + "_projection.py").is_file()
)


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


@contextmanager
def locked_snapshot(output, env):
    directory = output.parent.stat()
    if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise ValueError("snapshot directory must be owner-only")
    fd = os.open(str(output) + ".lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if output.is_symlink():
            raise ValueError("snapshot path must be a regular file")
        if output.exists():
            with closing(readonly(output, env)) as previous:
                if not previous.execute(
                    "select value from meta where key='generated'"
                ).fetchone():
                    raise ValueError("output must be a telemetry snapshot")
        yield


def build(source, output, env, now=None):
    output = Path(output)
    if output.exists() and Path(source).samefile(output):
        raise ValueError("source and snapshot must be different files")
    with locked_snapshot(output, env):
        rebuild(source, output, env, now)


def prune_snapshot(output, env, now=None):
    output = Path(output)
    cutoff = iso((now or datetime.now(timezone.utc)) - RETENTION)
    # Independently scheduled during collection and projection outages. Leave
    # the generation time untouched: expiration does not restore freshness.
    with (
        locked_snapshot(output, env),
        closing(
            sqlite3.connect(
                "file:" + quote(str(output.resolve())) + "?mode=rw",
                uri=True,
                timeout=10,
            )
        ) as db,
    ):
        db.execute("pragma secure_delete=on")
        tables = {
            r[0]
            for r in db.execute("select name from sqlite_master where type='table'")
        }
        # An older snapshot can contain data for a workflow no longer installed.
        # Expire its rows even when its projection and the raw store are absent.
        for table, stamp in (
            ("attempts", "cohort"),
            ("spans", "start"),
            ("health_runs", "started"),
            ("health_minutes", "minute"),
            ("health_jobs", "ts"),
            ("health_marks", "ts"),
            ("health_resources", "observed_at"),
        ):
            if table in tables:
                db.execute(f"delete from {table} where {stamp} < ?", (cutoff,))
        db.commit()


def rebuild(source, output, env, now=None):
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError("source and snapshot must be different files")
    now = now or datetime.now(timezone.utc)
    cutoff = iso(now - RETENTION)
    raw = sqlite3.connect("")
    raw.row_factory = sqlite3.Row
    try:
        with closing(readonly(source, env)) as source_db:
            source_db.backup(raw)
    except BaseException:
        raw.close()
        raise
    fd, temporary = tempfile.mkstemp(
        dir=Path(output).parent, prefix=".telemetry-snapshot-"
    )
    os.close(fd)
    snap = sqlite3.connect(temporary)
    try:
        snap.execute("create table meta(key text primary key,value text)")
        snap.executemany(
            "insert into meta values (?,?)",
            [("environment", env), ("generated", iso(now))],
        )
        for feature in FEATURES:
            import_module(feature + "_projection").materialize(raw, snap, cutoff)
        snap.commit()
        snap.close()
        os.replace(temporary, output)
    finally:
        raw.close()
        snap.close()
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument("--db", help="build from the private collector store")
    choice.add_argument(
        "--prune",
        action="store_true",
        help="expire old snapshot rows even when collection is offline",
    )
    p.add_argument("--snapshot", required=True)
    p.add_argument("--environment", choices=ENVIRONMENTS, required=True)
    a = p.parse_args()
    if a.prune:
        prune_snapshot(a.snapshot, a.environment)
    else:
        build(a.db, a.snapshot, a.environment)
