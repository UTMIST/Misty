"""Offline reader and writer acceptance. No Railway network traffic."""

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import collect
from store import SHARED, connect, iso

NOW = datetime(2026, 1, 12, 12, tzinfo=timezone.utc)
TS = iso(NOW - timedelta(seconds=30))
SOURCE = "12345678-abcd-abcd-abcd-000000000001"
ENV = "87654321-abcd-abcd-abcd-000000000002"


def event(**changes):
    return dict(
        dict(
            schema=SHARED,
            trace_id="1" * 32,
            span_id="2" * 16,
            event_id="3" * 32,
            process_id="4" * 16,
            operation="meeting.finalize",
            service="meeting",
            traffic="synthetic",
            environment="local",
            deployment="unknown",
            ts=TS,
            elapsed_ms=10,
            event_name="end",
            result="success",
            dropped_before=2,
        ),
        **changes,
    )


def outer(p):
    return dict(timestamp=TS, message=json.dumps(p))


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.file = self.path / "input"
        self.db = connect(self.path / "data.sqlite", "local")
        self.addCleanup(self.db.close)
        self.args = collect.argparse.Namespace(
            command="import",
            environment="local",
            service="meeting",
            traffic="synthetic",
            until=iso(NOW),
            available_since=None,
            input=self.file,
            environment_id=ENV,
            service_id=SOURCE,
            window_seconds=60,
            overlap_seconds=5,
        )

    def write(self, *rows):
        self.file.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def read(self, when=NOW):
        return collect.pull(self.db, self.args, when)

    def test_replay_collision_privacy_and_bad_event(self):
        good = event()
        self.write(
            outer(good),
            outer(good),
            outer(event(result="failed")),
            outer(event(event_id="c" * 32)),
            outer(event(key_name="private-canary")),
            outer(event(service="connectors")),
            outer(event(environment="production")),
            outer({"path": "/private-canary"}),
            outer(event(ts="private-canary")),
            outer(event(operation=[])),
            outer(event(measures={"secret": 1})),
            outer(event(schema="misty.v2")),
        )
        counts = self.read()
        self.assertEqual(
            (
                counts["added"],
                counts["duplicate"],
                counts["rejected"],
                counts["ignored"],
            ),
            (1, 1, 9, 1),
        )
        self.db.close()
        self.db = connect(self.path / "data.sqlite", "local")
        self.addCleanup(self.db.close)
        self.assertEqual(self.read()["drops"], 0)
        self.assertEqual(
            self.db.execute("select count,total_ms from summaries").fetchall(),
            [(1, 10)],
        )
        self.assertEqual(self.db.execute("select drops from sources").fetchone(), (2,))
        self.assertNotIn("private-canary", " ".join(self.db.iterdump()))
        with self.assertRaises(ValueError):
            connect(self.path / "data.sqlite", "production")

    def test_failed_window_rolls_back_receipts_and_last_good_summary(self):
        self.write(outer(event()))
        self.read()
        initial = self.db.execute(
            "select checkpoint, last_success from sources"
        ).fetchone()
        self.write(outer(event(trace_id="f" * 32, event_id="d" * 32)), {})
        with self.assertRaises(collect.ReadFailure):
            self.read(NOW + timedelta(minutes=2))
        self.assertEqual(
            self.db.execute(
                "select checkpoint,last_success,last_error from sources"
            ).fetchone(),
            initial + ("read_failed",),
        )
        self.assertEqual(
            self.db.execute("select count from summaries").fetchone(), (1,)
        )

    def test_empty_fresh_source_and_expired_gap(self):
        self.write()
        self.read()
        self.assertEqual(
            self.db.execute(
                "select last_success,last_error,drops from sources"
            ).fetchone(),
            (iso(NOW), None, 0),
        )
        self.args.command = "railway"
        self.args.until = None
        self.args.available_since = iso(NOW + timedelta(minutes=2))
        with patch.object(collect, "railway_rows", return_value=[]) as query:
            result = self.read(NOW + timedelta(minutes=5))
        self.assertEqual(result["gaps"], 1)
        self.assertEqual(iso(query.call_args.args[1]), self.args.available_since)
        self.assertEqual(
            self.db.execute("select gaps,last_error from sources").fetchone(),
            (1, "window_expired"),
        )
        self.assertFalse(self.db.execute("select * from summaries").fetchall())

    def test_railway_tags_attributes_saturation_and_credentials(self):
        self.args.command = "railway"
        p = event()
        row = dict(
            timestamp=TS,
            message="",
            tags=dict(environmentId=ENV, serviceId=SOURCE),
            attributes=[
                dict(key=k, value=v if isinstance(v, str) else json.dumps(v))
                for k, v in p.items()
            ],
        )

        def run(rows):
            with (
                patch.dict("os.environ", RAILWAY_VIEWER_TOKEN="canary-viewer"),
                patch.object(collect, "build_opener") as op,
            ):
                op.return_value.open.return_value = io.BytesIO(
                    json.dumps({"data": {"environmentLogs": rows}}).encode()
                )
                value = self.read()
                req = op.return_value.open.call_args.args[0]
                self.assertEqual(
                    req.get_header("Authorization"), "Bearer canary-viewer"
                )
                self.assertNotIn(b"canary", req.data)
                self.assertEqual(
                    json.loads(req.data)["variables"]["filter"], "@serviceId:" + SOURCE
                )
                return value

        self.assertEqual(run([row])["added"], 1)
        with self.assertRaisesRegex(collect.ReadFailure, "saturated"):
            run([row] * collect.LIMIT)
        with self.assertRaisesRegex(collect.ReadFailure, "read_failed"):
            run([{**row, "tags": {"serviceId": "wrong", "environmentId": ENV}}])
        self.args.environment_id = SOURCE
        with self.assertRaises(ValueError):
            run([])
        self.assertEqual(
            self.db.execute("select count from summaries").fetchone(), (1,)
        )

    def test_retention_and_private_mode(self):
        self.write(outer(event()))
        self.read()
        self.assertEqual((self.path / "data.sqlite").stat().st_mode & 0o777, 0o600)
        collect.prune(self.db, NOW + timedelta(days=31))
        self.assertFalse(self.db.execute("select * from events").fetchall())
        self.assertFalse(self.db.execute("select * from summaries").fetchall())
        (self.path / "public").touch(mode=0o644)
        with self.assertRaises(ValueError):
            connect(self.path / "public", "local")

    def test_stdout_to_cli_to_summary(self):
        # This invocation uses the real asynchronous Node emitter and two fresh reader processes.
        repo = Path(__file__).resolve().parents[2]
        from os import environ

        env = {
            **environ,
            "TELEMETRY_ENABLED": "true",
            "TELEMETRY_ENV": "local",
            "TELEMETRY_TRAFFIC": "synthetic",
        }
        producer = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "import {withSpan} from './src/telemetry.js';await withSpan('command',()=>withSpan('http.client',()=>7));",
            ],
            cwd=repo / "discord-bot",
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        rows = [json.loads(line) for line in producer.stdout.splitlines()]
        self.assertEqual(len(rows), 4)
        self.write(
            *[dict(timestamp=p["ts"], message=json.dumps(p)) for p in reversed(rows)]
        )
        cli = [sys.executable, str(repo / "scripts/telemetry/collect.py")]
        flags = ["--db", str(self.path / "cli.sqlite"), "--environment", "local"]
        argv = cli + [
            "import",
            *flags,
            "--service",
            "discord-bot",
            "--traffic",
            "synthetic",
            "--until",
            iso(datetime.now(timezone.utc)),
            "--input",
            str(self.file),
        ]
        for expected in ("added", "duplicate"):
            self.assertEqual(json.loads(subprocess.check_output(argv))[expected], 4)
        summaries = subprocess.check_output(cli + ["summary", *flags]).decode()
        self.assertEqual(
            [json.loads(s)["count"] for s in summaries.splitlines()], [1, 1]
        )
        self.assertNotIn(rows[0]["trace_id"], summaries)
        with sqlite3.connect(self.path / "cli.sqlite") as db:
            self.assertEqual(
                db.execute("select count(distinct trace_id) from events").fetchone(),
                (1,),
            )
            self.assertEqual(
                db.execute(
                    "select parent_span_id from events where operation='http.client' limit 1"
                ).fetchone(),
                (rows[0]["span_id"],),
            )


if __name__ == "__main__":
    unittest.main()
