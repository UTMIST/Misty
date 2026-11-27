"""Import or poll one trusted service. Never print provider bodies or credentials."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from store import (
    ENVIRONMENTS,
    LEGACY,
    RETENTION,
    SERVICES,
    SHARED,
    TRAFFIC,
    UUID,
    bind,
    connect,
    insert,
    iso,
    prune,
    utc,
    validate,
)

LIMIT = 200
MAX_BYTES = 8 * 1024 * 1024
QUERY = """query PilotLogs($env:String!,$filter:String!,$start:String!,$end:String!,$limit:Int!){
 environmentLogs(environmentId:$env,filter:$filter,anchorDate:$end,beforeDate:$start,
                 beforeLimit:$limit,afterLimit:0){
 timestamp message attributes {key value} tags {serviceId environmentId deploymentId}
 }}"""


class ReadFailure(Exception):
    """Only fixed error codes may reach the store or stderr."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def railway_rows(args, start, end):
    token = os.getenv("RAILWAY_VIEWER_TOKEN")
    if not token:
        raise ReadFailure("read_failed")
    body = json.dumps(
        {
            "query": QUERY,
            "variables": {
                "env": args.environment_id,
                "filter": "@serviceId:" + args.service_id,
                "start": iso(start),
                "end": iso(end),
                "limit": LIMIT,
            },
        }
    ).encode()
    request = Request(
        "https://backboard.railway.com/graphql/v2",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ReadFailure("saturated")
        document = json.loads(data)
        if document.get("errors"):
            raise ReadFailure("read_failed")
        rows = document["data"]["environmentLogs"]
        if not isinstance(rows, list):
            raise ReadFailure("read_failed")
        if len(rows) >= LIMIT:
            raise ReadFailure("saturated")
        for row in rows:
            tags = row["tags"]
            if (
                tags["serviceId"] != args.service_id
                or tags["environmentId"] != args.environment_id
            ):
                raise ReadFailure("read_failed")
        return rows
    except (
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        TimeoutError,
        OSError,
        URLError,
    ) as exc:
        raise ReadFailure("read_failed") from exc


def imported(path):
    total = 0
    with path.open("rb") as source:
        while line := source.readline(64 * 1024 + 1):
            total += len(line)
            if len(line) > 64 * 1024 or total > MAX_BYTES:
                raise ReadFailure("saturated")
            try:
                yield json.loads(line)
            except (ValueError, RecursionError) as exc:
                # Broken transport window could be a truncated response. Retry it intact.
                raise ReadFailure("read_failed") from exc


def unpack(row):
    """Railway can return JSON as a message or parse it into log attributes."""
    message = row.get("message")
    if isinstance(message, str) and message.lstrip().startswith("{"):
        try:
            record = json.loads(message)
        except (ValueError, RecursionError):
            return None
        if isinstance(record, dict) and "schema" in record:
            return record
    attributes = row.get("attributes", [])
    attrs = {}
    if not isinstance(attributes, list):
        return None
    for a in attributes:
        if (
            not isinstance(a, dict)
            or not isinstance(a.get("key"), str)
            or a["key"] in attrs
        ):
            return None
        k, v = a["key"], a.get("value")
        if k in ("elapsed_ms", "dropped_before", "measures") and isinstance(v, str):
            try:
                v = json.loads(v)
            except (ValueError, RecursionError):
                return None
        attrs[k] = v
    return attrs


def pull(db, args, now):
    prune(db, now)
    observed = iso(now)
    counts = Counter()
    drops = 0
    try:
        db.execute("begin immediate")
        if args.command == "railway":
            bind(db, "railway_environment", args.environment_id)
            bind(db, "railway_service/" + args.service, args.service_id)
        db.execute("insert or ignore into sources(service) values(?)", (args.service,))
        last = db.execute(
            "select checkpoint from sources where service=?", (args.service,)
        ).fetchone()[0]
        end = utc(args.until) if args.until else now - timedelta(seconds=5)
        if end > now or end < now - RETENTION:
            raise ValueError("poll end must be within retention and in the past")
        prior = utc(last) if last else None
        floor = (
            max(utc(args.available_since), now - RETENTION)
            if args.available_since
            else now - RETENTION
        )
        if floor > end:
            raise ValueError("available-since exceeds window")
        gap = prior is not None and prior < floor
        if args.command == "railway":
            if prior:
                end = min(
                    end, max(prior, floor) + timedelta(seconds=args.window_seconds)
                )
            start = (prior or end - timedelta(seconds=args.window_seconds)) - timedelta(
                seconds=args.overlap_seconds
            )
        else:
            start = now - RETENTION
        start = max(start, floor)
        if args.command == "railway" and prior and end < prior:
            raise ValueError("a live poll cannot rewind its checkpoint")
        records = (
            railway_rows(args, start, end)
            if args.command == "railway"
            else imported(args.input)
        )
        for row in records:
            if not isinstance(row, dict):
                raise ReadFailure("read_failed")
            try:
                received = utc(row.get("timestamp"))
            except (ValueError, OverflowError) as exc:
                raise ReadFailure("read_failed") from exc
            if not start <= received <= end:
                if args.command == "railway":
                    raise ReadFailure("read_failed")
                continue
            p = unpack(row)
            if p and "schema" in p and p["schema"] not in (SHARED, LEGACY):
                counts["rejected"] += 1
                continue
            if not p or "schema" not in p:
                counts["ignored"] += 1
                continue
            try:
                stamp = utc(p.get("ts"))
                valid = validate(p, args.service, args.environment)
            except (ValueError, TypeError, OverflowError):
                valid = False
            if not valid or not now - RETENTION <= stamp <= now:
                counts["rejected"] += 1
                continue
            status, lost = insert(db, p, args.service, args.traffic, iso(received))
            counts[status] += 1
            drops += lost
        checkpoint = iso(max(prior, end) if prior else end)
        db.execute(
            """update sources set checkpoint=?,observed_at=?,last_success=?,last_error=?,
                   gaps=gaps+?,rejected=rejected+?,drops=drops+? where service=?""",
            (
                checkpoint,
                observed,
                checkpoint,
                "window_expired" if gap else None,
                int(gap),
                counts["rejected"],
                drops,
                args.service,
            ),
        )
        db.commit()
    except (OSError, ReadFailure, RecursionError) as exc:
        db.rollback()
        error = str(exc) if isinstance(exc, ReadFailure) else "read_failed"
        with db:
            db.execute(
                """insert into sources(service,observed_at,last_error) values(?,?,?)
                       on conflict(service) do update set observed_at=excluded.observed_at,
                       last_error=excluded.last_error where sources.observed_at is null or sources.observed_at<=excluded.observed_at""",
                (args.service, observed, error),
            )
        raise ReadFailure(error) from None
    finally:
        if db.in_transaction:
            db.rollback()
    return dict(counts, checkpoint=checkpoint, drops=drops, gaps=int(gap))


def status(db, now):
    db.row_factory = sqlite3.Row
    for service in SERVICES:
        row = db.execute("select * from sources where service=?", (service,)).fetchone()
        if not row:
            yield {"service": service, "state": "unknown", "age_seconds": None}
            continue
        age = (
            max(0, int((now - utc(row["last_success"])).total_seconds()))
            if row["last_success"]
            else None
        )
        if row["last_error"]:
            state = row["last_error"]
        elif age is None or age > 180:
            state = "stale"
        else:
            state = "fresh"
        yield dict(row, age_seconds=age, state=state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("import", "railway", "prune", "summary", "status"):
        p = commands.add_parser(name)
        p.add_argument("--db", type=Path, required=True)
        p.add_argument("--environment", choices=ENVIRONMENTS, required=True)
        if name in ("prune", "summary", "status"):
            continue
        p.add_argument("--service", choices=SERVICES, required=True)
        p.add_argument(
            "--traffic",
            choices=TRAFFIC,
            required=True,
            help="Trusted legacy recording source traffic class",
        )
        p.add_argument("--until")
        p.add_argument(
            "--available-since", help="Earliest Railway log time retained by the source"
        )
        if name == "import":
            p.add_argument("--input", type=Path, required=True)
        else:
            p.add_argument("--environment-id", required=True)
            p.add_argument("--service-id", required=True)
            p.add_argument("--window-seconds", type=int, default=60)
            p.add_argument("--overlap-seconds", type=int, default=5)
    args = parser.parse_args()
    if args.command == "railway" and (
        not re.fullmatch(UUID, args.environment_id)
        or not re.fullmatch(UUID, args.service_id)
        or not 1 <= args.window_seconds <= 3600
        or not 0 <= args.overlap_seconds <= 300
    ):
        parser.error("invalid source identifiers or window size")
    db = connect(args.db, args.environment)
    try:
        now = datetime.now(timezone.utc)
        if args.command in ("railway", "import"):
            print(json.dumps(pull(db, args, now), sort_keys=True))
        else:
            prune(db, now)
            if args.command == "status":
                for item in status(db, now):
                    print(json.dumps(item, sort_keys=True))
            if args.command == "summary":
                db.row_factory = sqlite3.Row
                for row in db.execute(
                    "select * from summaries order by bucket,service,operation,result,traffic"
                ):
                    print(json.dumps(dict(row), sort_keys=True))
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except ReadFailure as exc:
        sys.exit(str(exc))
    except (ValueError, OSError, sqlite3.Error) as exc:
        # Even parser exceptions can contain a raw payload or transport URL.
        sys.exit(
            "invalid collector configuration or storage unavailable ("
            + type(exc).__name__
            + ")"
        )
