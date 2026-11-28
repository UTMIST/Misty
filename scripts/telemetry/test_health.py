"""Wireframe denominators, source age, route grouping and private resource import."""

import csv
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from contextlib import closing
from collect import pull
from dashboard import report_page
from health_projection import materialize
from resource_metrics import FIELDS, import_samples
from store import RETENTION, connect, insert, iso, prune, validate

END = datetime(2026, 11, 20, 14, tzinfo=timezone.utc)


class HealthTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = connect(Path(folder.name) / "collector.sqlite", "local")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.start = END - timedelta(hours=1)
        self.previous = self.start - timedelta(hours=1)

    def attempt(
        self,
        operation,
        result=None,
        *,
        service="discord-bot",
        traffic="synthetic",
        at=None,
        admitted=False,
        ms=4600,
        measures=None,
        route=None,
    ):
        trace = uuid4().hex
        sid = uuid4().hex[:16]
        at = at or self.start + timedelta(minutes=12)
        base = dict(
            schema="misty.telemetry.v1",
            trace_id=trace,
            span_id=sid,
            service=service,
            process_id="1" * 16,
            environment="local",
            traffic=traffic,
            deployment="unknown",
            operation=operation,
            dropped_before=0,
        )
        if route:
            base["route"] = route
        names = [("start", "started", 0)]
        if admitted:
            names.append(("admitted", "success", 0))
        if result:
            names.append(("end", result, ms))
        with self.db:
            for name, value, elapsed in names:
                event = dict(
                    base,
                    event_id=uuid4().hex,
                    event_name=name,
                    result=value,
                    elapsed_ms=elapsed,
                    ts=iso(at + timedelta(milliseconds=elapsed)),
                )
                if name == "end" and measures:
                    event["measures"] = measures
                assert validate(event, service, "local")
                insert(self.db, event, service, traffic, event["ts"])
        return trace

    def page(self, end=END):
        with closing(sqlite3.connect(":memory:")) as snapshot:
            snapshot.row_factory = sqlite3.Row
            materialize(self.db, snapshot, iso(END - timedelta(days=29)))
            snapshot.execute("create table meta(key text,value text)")
            snapshot.execute("insert into meta values('generated',?)", (iso(END),))

            # The online lead route may inspect the latest source coverage.
            # Deny every read of the raw events, resource samples or summaries.
            def authorize(action, table, column, db, trigger):
                if action == sqlite3.SQLITE_READ and table not in ("meta", "sources"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            self.db.set_authorizer(authorize)
            try:
                return report_page(
                    snapshot,
                    self.db,
                    "local",
                    "health",
                    {"end": iso(end), "hours": "1", "traffic": "synthetic"},
                    "lead",
                    END,
                    300,
                )
            finally:
                self.db.set_authorizer(lambda *_: sqlite3.SQLITE_OK)

    def test_fractional_boundary_and_spans_crossing_minutes(self):
        stop = END - timedelta(seconds=15)
        start = stop - timedelta(hours=1)
        self.attempt(
            "answer",
            "full",
            at=start - timedelta(milliseconds=1),
            admitted=True,
            ms=100,
        )
        self.attempt("answer", "full", at=start, admitted=True, ms=70000)
        self.attempt(
            "answer",
            "partial",
            at=stop - timedelta(milliseconds=1),
            admitted=True,
            ms=500,
        )
        page = self.page(stop)
        self.assertIn("<td>Current</td><td>2</td><td>2</td><td>1</td><td>0</td>", page)
        self.assertIn("<td>1</td><td>70000</td>", page)
        self.assertIn("<td>Previous</td><td>1</td><td>1</td><td>0</td>", page)

    def test_wireframe_reconciles_and_excludes_other_traffic_and_late_results(self):
        for _ in range(22):
            self.attempt("answer", "full", admitted=True)
        for _ in range(2):
            self.attempt("answer", "denied")
        self.attempt("answer", "cancelled", admitted=True)
        lost = self.attempt(
            "answer", "full", admitted=True, at=END - timedelta(milliseconds=5)
        )
        self.attempt("answer", "full", admitted=True, traffic="member")
        self.attempt(
            "answer", "full", admitted=True, at=self.previous + timedelta(minutes=1)
        )
        for outcome in (
            "results",
            "results",
            "no_hits",
            "no_channel_teams",
            "denied",
            "embedding_error",
            "failed",
            "cancelled",
            None,
        ):
            self.attempt("catalog.search", outcome, service="documentation-system")
        for result in (
            "success",
            "denied",
            "rate_limited",
            "failed",
            "cancelled",
            None,
        ):
            self.attempt(
                "http.request", result, service="gateway", route="/v1/auth/email/start"
            )
        page = self.page()
        self.assertIn("<td>Current</td><td>26</td><td>24</td><td>22</td>", page)
        self.assertIn(
            "<td>Current</td><td>0</td><td>0</td><td>22</td><td>4600</td>", page
        )
        self.assertIn("<td>Previous</td><td>1</td><td>1</td><td>1</td>", page)
        self.assertIn(
            "<td>Current</td><td>9</td><td>2</td><td>1</td><td>1/3</td>"
            "<td>1</td><td>1</td><td>0</td><td>2</td><td>1</td><td>1</td>",
            page,
        )
        for result in ("failed", "cancelled", "unknown"):
            self.assertIn(f"<td>catalog.search</td><td>{result}</td><td>1</td>", page)
        self.assertIn(
            "<td>/v1/auth/email/start</td><td>6</td><td>1</td><td>1</td><td>1</td><td>0</td><td>1</td><td>1</td><td>1</td><td>5</td><td>4600</td>",
            page,
        )
        self.assertNotIn(lost, page)
        self.assertIn("No resource samples imported", page)
        self.assertIn("Missing or stale sources", page)

    def test_unclassified_outcomes_and_cross_minute_route_latency(self):
        self.attempt("answer", "success", admitted=True)
        for result, ms in (("success", 100), ("unknown", 70000), ("full", 150)):
            self.attempt(
                "http.request",
                result,
                ms=ms,
                service="gateway",
                route="/v1/auth/email/start",
            )
        page = self.page()
        self.assertIn(
            "<td>Current</td><td>1</td><td>1</td>"
            + "<td>0</td>" * 10
            + "<td>1</td></tr>",
            page,
        )
        self.assertIn(
            "<td>/v1/auth/email/start</td><td>3</td><td>1</td>"
            + "<td>0</td>" * 5
            + "<td>2</td><td>1</td><td>100</td></tr>",
            page,
        )

    def test_successful_poll_after_a_gap_reports_freshness_and_missing_history(self):
        with TemporaryDirectory() as folder:
            events = Path(folder) / "empty.jsonl"
            events.touch()
            args = Namespace(
                command="import",
                input=events,
                environment="local",
                service="gateway",
                traffic="synthetic",
                until=iso(END - timedelta(hours=2)),
                available_since=None,
            )
            pull(self.db, args, END)
            args.until = iso(END)
            args.available_since = iso(END - timedelta(hours=1))
            self.assertEqual(pull(self.db, args, END)["gaps"], 1)
        self.assertIn(
            f"<td>gateway</td><td>{iso(END)}</td><td>Current</td><td>1</td>",
            self.page(),
        )

    def test_freshness_uses_real_summaries_and_detects_a_missed_run(self):
        values = {
            "eligible": 40,
            "content_current": 38,
            "content_unknown": 2,
            "index_current": 37,
            "index_unknown": 1,
            "access_current": 39,
            "access_unknown": 0,
        }

        def summary(age):
            self.attempt(
                "catalog.freshness",
                "success",
                service="documentation-system",
                measures=values,
                at=END - timedelta(minutes=age),
                ms=0,
            )

        summary(115)
        self.assertIn("115 min old", self.page())
        self.assertIn("Current document state unknown", self.page())
        summary(35)
        self.assertNotIn("Current document state unknown", self.page())
        self.assertIn("<td>index</td><td>40</td><td>37</td><td>1</td>", self.page())
        self.attempt(
            "catalog.refresh",
            "gaps",
            service="documentation-system",
            at=END - timedelta(minutes=20),
            measures={"attempted": 40, "item_failed": 2, "index_failed": 0},
        )
        self.assertIn("Current document state unknown", self.page())
        self.assertIn("<td>40</td><td>2</td><td>0</td>", self.page())

    def test_invalid_coverage_and_rejected_inputs_stay_visible(self):
        # The wire contract bounds integers separately. An upstream producer can
        # still send counts that cannot describe a single snapshot.
        measures = {
            "eligible": 1,
            "content_current": 1,
            "content_unknown": 1,
            "index_current": 1,
            "index_unknown": 0,
            "access_current": 1,
            "access_unknown": 0,
        }
        self.attempt(
            "catalog.freshness",
            "success",
            service="documentation-system",
            measures=measures,
        )
        with self.db:
            self.db.execute(
                "insert into sources(service,last_success,gaps,drops,rejected) values(?,?,?,?,?)",
                ("documentation-system", iso(END), 2, 3, 4),
            )
        page = self.page()
        self.assertIn("Current document state unknown", page)
        self.assertNotIn("<td>content</td><td>1</td><td>1</td><td>1</td>", page)
        self.assertIn("<td>Current</td><td>2</td><td>3</td><td>4</td>", page)

    def test_initial_read_failure_and_recovery(self):
        self.assertIn(
            "<td>gateway</td><td>Unknown</td><td>No receipt</td>", self.page()
        )
        with self.db:
            self.db.execute(
                "insert into sources(service,last_error) values('gateway','read_failed')"
            )
        self.assertIn(
            "<td>gateway</td><td>Unknown</td><td>Read failed</td>", self.page()
        )
        with self.db:
            self.db.execute(
                "update sources set last_success=?,last_error=NULL where service='gateway'",
                (iso(END),),
            )
        self.assertIn(
            f"<td>gateway</td><td>{iso(END)}</td><td>Current</td>", self.page()
        )

    def test_resource_import_is_atomic_replay_safe_and_bound_to_environment(self):
        def data(rows):
            stream = StringIO()
            w = csv.writer(stream)
            w.writerow(FIELDS)
            w.writerows(rows)
            stream.seek(0)
            return stream

        row = [
            "local",
            "gateway",
            iso(END - timedelta(minutes=4)),
            0.25,
            104857600,
            iso(END - timedelta(hours=2)),
        ]
        import_samples(self.db, "local", data([row, row]), END)
        page = self.page()
        self.assertIn("<td>0.250</td><td>100.0</td>", page)
        self.assertIn("<td>4</td>", page)
        for index, value in (
            (0, "staging"),
            (1, "private-member"),
            (2, iso(END + timedelta(seconds=2))),
            (3, "NaN"),
            (4, -1),
            (5, iso(END + timedelta(seconds=2))),
        ):
            bad = row.copy()
            bad[index] = value
            extra = row.copy()
            extra[2] = iso(END - timedelta(minutes=9))
            with self.assertRaises(ValueError):
                import_samples(self.db, "local", data([extra, bad]), END)
        with self.assertRaises(ValueError):
            import_samples(self.db, "staging", data([row]), END)
        conflict = row.copy()
        conflict[3] = 0.5
        with self.assertRaises(ValueError):
            import_samples(self.db, "local", data([conflict]), END)
        self.assertEqual(
            self.db.execute("select count(*) from railway_samples").fetchone()[0], 1
        )
        boundary = row.copy()
        boundary[2] = iso(END - RETENTION)
        boundary[5] = ""
        expired = boundary.copy()
        expired[2] = iso(END - RETENTION - timedelta(milliseconds=1))
        with self.assertRaises(ValueError):
            import_samples(self.db, "local", data([boundary, expired]), END)
        self.assertEqual(
            self.db.execute("select count(*) from railway_samples").fetchone()[0], 1
        )
        import_samples(self.db, "local", data([boundary]), END)
        prune(self.db, END)
        self.assertEqual(
            self.db.execute("select count(*) from railway_samples").fetchone()[0], 2
        )
        prune(self.db, END + timedelta(milliseconds=1))
        self.assertEqual(
            self.db.execute("select observed_at from railway_samples").fetchone()[0],
            row[2],
        )


if __name__ == "__main__":
    unittest.main()
