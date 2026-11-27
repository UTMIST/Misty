"""Replay synthetic success, interruption and late recovery into a NEW local store."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from project import build, iso


def fixture(now):
    events = []
    base = now.replace(second=0, microsecond=0) - timedelta(minutes=40)

    def add(service, row):
        events.append(
            (
                service,
                {
                    "timestamp": row["ts"],
                    "message": json.dumps(row, separators=(",", ":")),
                },
            )
        )

    for n in range(1, 4):
        trace = f"{n:032x}"
        root = f"{16 * n:016x}"
        stop = f"{16 * n + 1:016x}"
        start = base + timedelta(minutes=5 * n)
        marks = [
            ("record.pdf_requested" if n == 1 else "record.saved_requested", 0),
            ("capture.started", 1),
            ("capture.voice_lost" if n == 2 else "capture.stop_requested", 180000),
            ("capture.drained", 180010),
            ("stop.request", 180020),
            ("stop.drain_confirmed", 181000),
            ("stop.response", 181001),
            ("pdf.posting", 181100),
            ("pdf.posted", 181200),
        ]
        if n >= 2:
            marks.extend(
                [
                    ("save.pending", 181020),
                    ("link.pending", 181021),
                    ("save.confirmed", 240000),
                    ("link.posted", 240010),
                ]
            )
        for name, ms in marks:
            child = name.startswith("stop.")
            row = dict(
                schema="misty.recording.v1",
                trace_id=trace,
                span_id=stop if child else root,
                event_name=name,
                elapsed_ms=ms if not child else ms - 180020,
                dropped_before=0,
                ts=iso(start + timedelta(milliseconds=ms)),
            )
            if child:
                row["parent_span_id"] = root
            if ms >= 240000:
                row["span_id"] = f"{16 * n + 2:016x}"
            add("discord-bot", row)
        for j, (service, operation, offset, length) in enumerate(
            [
                ("meeting", "meeting.finalize", 180030, 960),
                ("documentation-system", "catalog.writer_create", 180200, 570),
                ("connectors", "connectors.google_create", 180300, 320),
            ]
        ):
            if n == 1 and j > 0:
                continue
            sid = f"{16 * n + j + 3:016x}"
            for name, ms, result in [
                ("start", 0, "started"),
                ("end", length, "success"),
            ]:
                row = dict(
                    schema="misty.telemetry.v1",
                    event_id=hashlib.sha256((sid + name).encode()).hexdigest()[:32],
                    trace_id=trace,
                    span_id=sid,
                    parent_span_id=stop if j == 0 else f"{16 * n + j + 2:016x}",
                    event_name=name,
                    result=result,
                    elapsed_ms=ms,
                    ts=iso(start + timedelta(milliseconds=offset + ms)),
                    service=service,
                    operation=operation,
                    environment="local",
                    traffic="synthetic",
                    deployment="unknown",
                    process_id="0000000000000001",
                    dropped_before=0,
                )
                add(service, row)
    return events


def seed(db, snapshot, now):
    if Path(db).exists() or Path(snapshot).exists():
        raise ValueError("pilot requires new local database and snapshot paths")
    rows = fixture(now)
    with tempfile.TemporaryDirectory() as temp:
        for service in sorted({s for s, _ in rows}):
            batch = [r for s, r in rows if s == service]
            path = Path(temp) / "events.jsonl"
            path.write_text(
                "".join(json.dumps(r) + "\n" for r in list(reversed(batch)) + batch[:2])
            )
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("collect.py")),
                    "import",
                    "--db",
                    str(db),
                    "--environment",
                    "local",
                    "--service",
                    service,
                    "--traffic",
                    "synthetic",
                    "--input",
                    str(path),
                    "--until",
                    iso(now),
                ],
                check=True,
            )
    build(db, snapshot, "local", now)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--snapshot", required=True)
    a = p.parse_args()
    seed(a.db, a.snapshot, datetime.now(timezone.utc))
