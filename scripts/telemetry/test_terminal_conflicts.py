"""Exercise contradictory wire observations through collection, replay and the lead projection."""

import json
import sqlite3
import unittest
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from uuid import uuid4

import collect
from health import _catalog, _cohort
from health_projection import materialize
from store import RETENTION, connect, iso, prune

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


def ingest(db, folder, *events, service=None):
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
            service=service or events[0]["service"],
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

    def test_duration_disagreement_excludes_only_its_sample_before_and_after_sealing(
        self,
    ):
        for late_seconds in (2, 3, 15):
            with (
                self.subTest(late_seconds=late_seconds),
                TemporaryDirectory() as d,
                closing(connect(Path(d) / "raw", "local")) as db,
                closing(sqlite3.connect(":memory:")) as snap,
            ):
                base = span("answer", "discord-bot")
                another = span("answer", "discord-bot")
                start = event(base, "start", "started", 0)
                first = event(base, "end", "full", 2, 500)
                late = event(base, "end", "full", late_seconds, 800)
                zero = event(another, "end", "full", 1, 0)
                wire = [late, zero, event(another, "start", "started", 0), first, start]
                self.assertEqual(ingest(db, d, *wire)["added"], 5)
                self.assertEqual(ingest(db, d, *reversed(wire))["duplicate"], 5)
                self.assertEqual(
                    db.execute(
                        "select result,count,total_ms,max_ms,latency_count from summaries"
                    ).fetchall(),
                    [("full", 2, 0, 0, 1)],
                )
                snap.row_factory = sqlite3.Row
                materialize(db, snap, iso(START))

                def timed(end):
                    rows, median = _cohort(
                        snap, "synthetic", iso(START - timedelta(minutes=1)), end
                    )
                    return [
                        (r["result"], r["n"], r["latency_n"], r["total_ms"])
                        for r in rows
                    ], median

                if late_seconds > 2:
                    self.assertEqual(timed(late["ts"]), ([("full", 2, 2, 500)], 250))
                self.assertEqual(timed(iso(NOW)), ([("full", 2, 1, 0)], 0))

    def test_measure_disagreement_keeps_job_outcome_and_latency(self):
        base = span("catalog.refresh", "documentation-system")
        first = event(base, "end", "drained", 2, 200, measures={"attempted": 7})
        for late_second in (2, 3):
            disputed = event(
                base, "end", "drained", late_second, 200, measures={"attempted": 9}
            )
            with (
                TemporaryDirectory() as d,
                closing(connect(Path(d) / "raw", "local")) as db,
                closing(sqlite3.connect(":memory:")) as snap,
            ):
                self.assertEqual(
                    ingest(db, d, first, disputed, event(base, "start", "started", 0))[
                        "added"
                    ],
                    3,
                )
                self.assertEqual(ingest(db, d, disputed, first)["duplicate"], 2)
                snap.row_factory = sqlite3.Row
                materialize(db, snap, iso(START))
                if late_second > 2:
                    self.assertIn(
                        "<td>7</td>", _catalog(snap, "synthetic", disputed["ts"])
                    )
                html = _catalog(snap, "synthetic", iso(NOW))
                self.assertIn("<td>drained</td>", html)
                self.assertNotIn("<td>7</td>", html)
                self.assertNotIn("<td>9</td>", html)
                self.assertIn("<td>Unknown</td><td>Unknown</td><td>Unknown</td>", html)
                self.assertEqual(
                    db.execute(
                        "select result,count,total_ms,latency_count from summaries"
                    ).fetchall(),
                    [("drained", 1, 200, 1)],
                )

    def test_concurrent_writers_and_upgrade_of_preexisting_numeric_disagreement(self):
        base = span("answer", "discord-bot")
        start = event(base, "start", "started", 0)
        left = event(base, "end", "full", 2, 20)
        right = event(base, "end", "full", 3, 30)
        with TemporaryDirectory() as d:
            path = Path(d) / "raw"
            with closing(connect(path, "local")):
                pass
            ready = Barrier(2)

            def writer(payload):
                with (
                    TemporaryDirectory(dir=d) as folder,
                    closing(connect(path, "local")) as db,
                ):
                    ready.wait(timeout=5)
                    return ingest(db, folder, start, payload)

            with ThreadPoolExecutor(2) as pool:
                result = list(pool.map(writer, [left, right]))
            self.assertEqual(sum(r["added"] for r in result), 3)
            self.assertEqual(sum(r.get("duplicate", 0) for r in result), 1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute(
                        "select result,count,latency_count from summaries"
                    ).fetchall(),
                    [("full", 1, 0)],
                )
                db.executescript("""alter table summaries drop column latency_count;
                   update summaries set total_ms=20,max_ms=20;
                   drop index span_end_observation;
                   create unique index span_end_observation on events(trace_id,span_id,ts,result) where event_name='end';""")
            with closing(connect(path, "local")) as upgraded:
                self.assertEqual(
                    upgraded.execute(
                        "select count,latency_count,total_ms,max_ms from summaries"
                    ).fetchall(),
                    [(1, 0, 0, 0)],
                )
                third = event(base, "end", "full", 2, 40)
                self.assertEqual(ingest(upgraded, d, third)["added"], 1)
                self.assertEqual(
                    ingest(upgraded, d, left, right, third)["duplicate"], 3
                )

    def test_prune_keeps_partial_hour_and_reassigns_first_surviving_end(self):
        base = span("answer", "discord-bot")
        events = [
            event(base, "end", "full", 1, 20),
            event(base, "end", "failed", 20, 30),
            event(span("answer", "discord-bot"), "end", "failed", 8, 7),
            event(span("answer", "discord-bot"), "end", "failed", 7, 999),
        ]
        with (
            TemporaryDirectory() as d,
            closing(connect(Path(d) / "raw", "local")) as db,
        ):
            ingest(db, d, *events)
            now = START + RETENTION + timedelta(seconds=8)
            prune(db, now)
            prune(db, now)
            self.assertEqual(
                db.execute(
                    "select bucket,result,count,total_ms,max_ms,latency_count from summaries order by bucket"
                ).fetchall(),
                [
                    ("2026-11-20T13:00:00.000Z", "failed", 1, 7, 7, 1),
                    ("2026-11-20T14:00:00.000Z", "failed", 1, 30, 30, 1),
                ],
            )
            self.assertEqual(db.execute("select count(*) from events").fetchone(), (2,))

    def test_first_shared_event_binds_traffic_service_and_operation_in_both_orders(
        self,
    ):
        base = span("catalog.search", "documentation-system")
        for change in (
            {"traffic": "member"},
            {"operation": "catalog.refresh"},
            {"operation": "http.client", "service": "meeting"},
        ):
            for swapped in (False, True):
                with (
                    self.subTest(change=change, swapped=swapped),
                    TemporaryDirectory() as d,
                    closing(connect(Path(d) / "raw", "local")) as db,
                ):
                    start = event(base, "start", "started", 0)
                    end = event(dict(base, **change), "end", "success", 1, 10)
                    first, other = (end, start) if swapped else (start, end)
                    self.assertEqual(ingest(db, d, first)["added"], 1)
                    self.assertEqual(ingest(db, d, other)["rejected"], 1)
                    self.assertEqual(ingest(db, d, first)["duplicate"], 1)
                    self.assertEqual(
                        db.execute("select sum(rejected) from sources").fetchone(), (1,)
                    )
                    self.assertEqual(
                        db.execute(
                            "select service,traffic,operation,event_name from events"
                        ).fetchall(),
                        [
                            (
                                first["service"],
                                first["traffic"],
                                first["operation"],
                                first["event_name"],
                            )
                        ],
                    )
                    self.assertEqual(
                        db.execute("select count(*) from summaries").fetchone(),
                        (int(swapped),),
                    )

    def test_legacy_recording_marks_do_not_bind_shared_span_metadata(self):
        shared = event(span("answer", "discord-bot"), "end", "full", 2, 100)
        legacy = dict(
            schema="misty.recording.v1",
            ts=shared["ts"],
            trace_id=shared["trace_id"],
            span_id=shared["span_id"],
            elapsed_ms=0,
            dropped_before=0,
            event_name="capture.started",
        )
        with (
            TemporaryDirectory() as d,
            closing(connect(Path(d) / "raw", "local")) as db,
        ):
            self.assertEqual(ingest(db, d, legacy, service="discord-bot")["added"], 1)
            self.assertEqual(ingest(db, d, shared)["added"], 1)
            self.assertEqual(ingest(db, d, shared, legacy)["duplicate"], 2)
