"""Exercise contradictory wire observations through collection, replay and the lead projection."""

import json
import sqlite3
import unittest
from argparse import Namespace
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import collect
from health import _catalog, _cohort
from health_projection import materialize
from store import connect, iso

NOW = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)
START = NOW - timedelta(hours=1, seconds=10)


def event(base, name, result, seconds, elapsed=0, **fields):
    return dict(
        base,
        event_id=uuid4().hex,
        event_name=name,
        result=result,
        ts=iso(START + timedelta(seconds=seconds)),
        elapsed_ms=elapsed,
        **fields,
    )


def span(operation, service):
    return dict(
        schema="misty.telemetry.v1",
        trace_id=uuid4().hex,
        span_id=uuid4().hex[:16],
        process_id="a" * 16,
        service=service,
        operation=operation,
        deployment="unknown",
        traffic="synthetic",
        environment="local",
        dropped_before=0,
    )


def ingest(db, folder, *events):
    path = Path(folder) / "wire.jsonl"
    path.write_text(
        "".join(
            json.dumps({"timestamp": p["ts"], "message": json.dumps(p)}) + "\n"
            for p in events
        )
    )
    return collect.pull(
        db,
        Namespace(
            command="import",
            service=events[0]["service"],
            environment="local",
            traffic="synthetic",
            until=iso(NOW),
            available_since=None,
            input=path,
        ),
        NOW,
    )


class TerminalConflictsTest(unittest.TestCase):
    def test_replay_order_legacy_upgrade_and_hourly_totals(self):
        answer = span("answer", "discord-bot")
        start = event(answer, "start", "started", 0)
        admitted = event(answer, "admitted", "success", 1)
        first = event(answer, "end", "full", 2, 2000)
        second = event(answer, "end", "failed", 15, 15000)
        independent = event(span("answer", "discord-bot"), "end", "full", 3, 110)
        for old in (False, True):
            for early, late in ((first, second), (second, first)):
                with (
                    self.subTest(old=old, first=early["result"]),
                    TemporaryDirectory() as d,
                ):
                    path = Path(d) / "collector.sqlite"
                    with closing(connect(path, "local")) as db:
                        if old:
                            db.executescript("""
                                drop index span_start;
                                drop index span_end_observation;
                                create unique index terminal on events(trace_id,span_id,event_name)
                                 where event_name in ('start','end');
                            """)
                        self.assertEqual(ingest(db, d, independent, early)["added"], 2)
                    with (
                        closing(connect(path, "local")) as db,
                        closing(sqlite3.connect(":memory:")) as snap,
                    ):
                        receipts = ingest(db, d, late, admitted, start, early)
                        self.assertEqual(
                            (receipts["added"], receipts["duplicate"]), (3, 1)
                        )
                        self.assertEqual(
                            ingest(db, d, early, late, start, admitted, independent)[
                                "duplicate"
                            ],
                            5,
                        )
                        self.assertEqual(
                            db.execute(
                                "select bucket,result,count,total_ms,max_ms from summaries order by result"
                            ).fetchall(),
                            [
                                ("2026-11-20T13:00:00.000Z", "full", 1, 110, 110),
                                ("2026-11-20T13:00:00.000Z", "unknown", 1, 0, 0),
                            ],
                        )
                        snap.row_factory = sqlite3.Row
                        materialize(db, snap, iso(START))
                        before, median = _cohort(
                            snap, "synthetic", iso(START), second["ts"]
                        )
                        self.assertEqual((before[0]["result"], median), ("full", 2000))
                        after, median = _cohort(snap, "synthetic", iso(START), iso(NOW))
                        self.assertEqual(
                            [
                                (r["result"], r["n"], r["admitted"], r["total_ms"])
                                for r in after
                            ],
                            [("unknown", 1, 1, 0)],
                        )
                        self.assertIsNone(median)

    def test_catalog_conflict_invalidates_success_and_measures_at_disagreement(self):
        base = span("catalog.refresh", "documentation-system")
        start, good, bad = (
            event(base, "start", "started", 0),
            event(base, "end", "drained", 2, 2000, measures={"attempted": 73}),
            event(base, "end", "failed", 3, 3000),
        )
        with (
            TemporaryDirectory() as d,
            closing(connect(Path(d) / "raw", "local")) as db,
            closing(sqlite3.connect(":memory:")) as snap,
        ):
            self.assertEqual(ingest(db, d, bad, good, start)["added"], 3)
            snap.row_factory = sqlite3.Row
            materialize(db, snap, iso(START))
            self.assertEqual(
                snap.execute("select count(*) from health_jobs").fetchone()[0], 1
            )
            before = _catalog(snap, "synthetic", bad["ts"])
            self.assertIn("<td>drained</td>", before)
            self.assertIn("<td>73</td>", before)
            after = _catalog(snap, "synthetic", iso(NOW))
            self.assertIn(
                f"<td>{bad['ts']}</td><td>unknown</td><td>Unknown</td><td>Unknown</td>",
                after,
            )
            self.assertNotIn("<td>73</td>", after)
            rows, _ = _cohort(
                snap, "synthetic", iso(START - timedelta(minutes=1)), iso(NOW)
            )
            self.assertEqual([(r["result"], r["n"]) for r in rows], [("unknown", 1)])

    def test_old_snapshot_keeps_serving_undisputed_catalog_jobs(self):
        with closing(sqlite3.connect(":memory:")) as old:
            old.row_factory = sqlite3.Row
            old.executescript("""
                create table health_jobs(traffic text,operation text,ts text,started text,result text,measures text);
                insert into health_jobs values('synthetic','catalog.refresh','2026-11-20T13:59:52.000Z',
                  '2026-11-20T13:59:50.000Z','drained','{"attempted":12}');
            """)
            page = _catalog(old, "synthetic", iso(NOW))
            self.assertIn("<td>drained</td>", page)
            self.assertIn("<td>12</td>", page)
