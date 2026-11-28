"""Import operator-exported Railway samples into one private collector database."""

import argparse
import csv
from datetime import datetime, timezone
import math
from pathlib import Path
import re

from store import ENVIRONMENTS, RETENTION, SERVICES, connect, iso, utc

FIELDS = [
    "environment",
    "service",
    "observed_at",
    "cpu_cores",
    "memory_bytes",
    "deployed_at",
]
SCHEMA = """CREATE TABLE IF NOT EXISTS railway_samples(
    service TEXT NOT NULL, observed_at TEXT NOT NULL, cpu_cores REAL NOT NULL,
    memory_bytes INTEGER NOT NULL, deployed_at TEXT, PRIMARY KEY(service,observed_at))"""


def import_samples(db, env, stream, now):
    stored_env = db.execute("select value from meta where key='environment'").fetchone()
    if not stored_env or stored_env[0] != env:
        raise ValueError("collector environment differs")
    rows = csv.DictReader(stream)
    if rows.fieldnames != FIELDS:
        raise ValueError("invalid CSV header")
    oldest = now - RETENTION
    with db:
        db.execute(SCHEMA)
        db.execute("delete from railway_samples where observed_at < ?", (iso(oldest),))
        count = 0
        for count, row in enumerate(rows, 1):
            if count > 10000:
                raise ValueError("maximum 10000 samples per import")
            if set(row) != set(FIELDS) or any(v is None for v in row.values()):
                raise ValueError("invalid CSV row")
            service = row["service"]
            stamp = utc(row["observed_at"])
            deploy = utc(row["deployed_at"]) if row["deployed_at"] else None
            cpu = float(row["cpu_cores"])
            if not re.fullmatch(r"[0-9]{1,19}", row["memory_bytes"]):
                raise ValueError("invalid memory byte count")
            memory = int(row["memory_bytes"])
            if (
                row["environment"] != env
                or service not in SERVICES
                or not oldest <= stamp <= now
                or (deploy and deploy > stamp)
                or not math.isfinite(cpu)
                or cpu < 0
                or memory >= 2**63
            ):
                raise ValueError("invalid resource sample")
            values = (service, iso(stamp), cpu, memory, iso(deploy) if deploy else None)
            previous = db.execute(
                "select cpu_cores,memory_bytes,deployed_at from railway_samples where service=? and observed_at=?",
                values[:2],
            ).fetchone()
            if previous and tuple(previous) != values[2:]:
                raise ValueError(
                    "conflicting sample for the same service and observation time"
                )
            db.execute(
                "insert or ignore into railway_samples values(?,?,?,?,?)", values
            )
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path)
    parser.add_argument("environment", choices=ENVIRONMENTS)
    parser.add_argument("csv_file", type=Path)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("an existing collector database is required")
    if args.csv_file.stat().st_size > 2_000_000:
        parser.error("maximum CSV size is 2 MB")
    db = connect(args.db, args.environment)
    try:
        with args.csv_file.open(encoding="utf-8", newline="") as stream:
            count = import_samples(
                db, args.environment, stream, datetime.now(timezone.utc)
            )
    except (ValueError, csv.Error):
        parser.exit(2, "Invalid resource samples. No samples imported.\n")
    finally:
        db.close()
    print(f"Validated {count} samples.")


if __name__ == "__main__":
    main()
