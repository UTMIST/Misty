import base64
import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from alerts import evaluate, notify
from dashboard import load_access, make_handler
from pilot import fixture, seed
from project import build, iso, percentiles, readonly


class PilotAcceptance(unittest.TestCase):
    def test_replay_through_actual_collector(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "raw.sqlite"
            snapshot = Path(temp) / "snapshot.sqlite"
            seed(raw, snapshot, datetime.now(timezone.utc))
            with closing(readonly(raw, "local")) as db:
                self.assertEqual(
                    db.execute(
                        "select sum(drops+rejected+gaps) from sources"
                    ).fetchone()[0],
                    0,
                )
            with closing(readonly(snapshot, "local")) as db:
                counts = dict(
                    db.execute("select outcome,count(*) from attempts group by outcome")
                )
                self.assertEqual(counts, {"complete": 2, "interrupted": 1})
                self.assertEqual(
                    db.execute("select count(*) from spans").fetchone()[0], 7
                )
            with self.assertRaises(ValueError):
                seed(raw, snapshot, datetime.now(timezone.utc))

    def test_sample_sizes(self):
        self.assertEqual(percentiles([None]), (0, None, None))
        self.assertEqual(percentiles([1, 8, None]), (2, 4.5, None))
        self.assertEqual(percentiles(range(1, 21)), (20, 10.5, 19))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        self.raw = self.root / "collector.sqlite"
        self.snapshot = self.root / "snapshot.sqlite"
        # The acceptance fixture below tests the reader and UI independently of
        # collection credentials. The pilot command runs these same lines through the real importer.
        with closing(sqlite3.connect(self.raw)) as db:
            db.executescript("""
                create table meta(key text primary key,value text);
                insert into meta values('environment','local');
                create table events(event_id text primary key,trace_id text,span_id text,parent_span_id text,
                   link_trace_id text,link_span_id text,ts text,received_at text,service text,operation text,
                   event_name text,result text,elapsed_ms int,traffic text,deployment text,payload text);
                create table sources(service text primary key,last_success text,last_error text,gaps int,drops int,rejected int);
            """)
            self.rows = fixture(self.now)
            for service, msg in self.rows[::-1] + self.rows:
                r = json.loads(msg["message"])
                db.execute(
                    "insert or ignore into events values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r.get(
                            "event_id",
                            hashlib.sha256(msg["message"].encode()).hexdigest()[:32],
                        ),
                        r["trace_id"],
                        r["span_id"],
                        r.get("parent_span_id"),
                        r.get("link_trace_id"),
                        r.get("link_span_id"),
                        r["ts"],
                        iso(self.now),
                        service,
                        r.get("operation", "recording"),
                        r["event_name"],
                        r.get("result", "unknown"),
                        r["elapsed_ms"],
                        "synthetic",
                        "unknown",
                        msg["message"],
                    ),
                )
                db.execute(
                    "insert or ignore into sources values(?,?,null,0,0,0)",
                    (service, iso(self.now)),
                )
            db.commit()
        build(self.raw, self.snapshot, "local", self.now)
        access_file = self.root / "access.json"
        self.ids = {
            "lead": "lead-demo-key",
            "dev": "developer-demo-key",
            "admin": "admin-demo-key",
            "prod": "prod-demo-key",
        }
        access_file.write_text(
            json.dumps(
                [
                    {
                        "user": user,
                        "environment": "production" if user == "prod" else "local",
                        "role": {
                            "lead": "lead",
                            "dev": "developer",
                            "admin": "admin",
                            "prod": "admin",
                        }[user],
                        "sha256": hashlib.sha256(key.encode()).hexdigest(),
                    }
                    for user, key in self.ids.items()
                ]
            )
        )
        handler = make_handler(
            self.raw,
            self.snapshot,
            "local",
            load_access(access_file),
            clock=lambda: self.now,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.trace = f"{2:032x}"

    def request(self, path, user=None):
        headers = {}
        if user:
            headers["Authorization"] = (
                "Basic "
                + base64.b64encode((user + ":" + self.ids[user]).encode()).decode()
            )
        with closing(
            http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        ) as conn:
            conn.request("GET", path, headers=headers)
            res = conn.getresponse()
            return res.status, res.read().decode()

    def test_http_permissions_and_drilldown(self):
        url = "/local/attempt/" + self.trace + "?traffic=synthetic&hours=1"
        self.assertEqual(self.request(url)[0], 401)
        self.assertEqual(self.request(url, "lead")[0], 403)
        self.assertEqual(self.request(url, "prod")[0], 401)
        self.assertEqual(
            self.request(url.replace("/local/", "/production/"), "dev")[0], 403
        )
        self.assertEqual(
            self.request(url.replace("synthetic", "member"), "dev")[0], 404
        )
        status, lead = self.request("/local/overview", "lead")
        self.assertEqual(status, 200)
        self.assertNotIn(self.trace, lead)
        self.assertIn("50.0%", lead)
        for user in ("dev", "admin"):
            status, detail = self.request(url, user)
            self.assertEqual(status, 200)
            for expected in (
                "interrupted",
                "capture.voice_lost",
                "save.confirmed",
                "link.posted",
                "pdf.posted",
                "Call tree",
                "component?",
                "connectors.google_create",
                "Child",
                "runbook",
            ):
                self.assertIn(expected, detail)
        status, component = self.request(
            "/local/component?traffic=synthetic&service=connectors&operation=connectors.google_create",
            "dev",
        )
        self.assertEqual(status, 200)
        self.assertIn("2 observed calls", component)
        self.assertIn("connectors operator runbook", component)
        self.assertIn("320 ms", component)
        self.assertIn("2 timed samples", component)

    def test_open_then_restart_or_collection_loss(self):
        trace = f"{3:032x}"
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute(
                "delete from events where trace_id=? and service='discord-bot' and event_name not in ('capture.started','record.saved_requested')",
                (trace,),
            )
            # New boot metadata comes from the authenticated bot's shared spans.
            db.execute(
                "update events set service='discord-bot',payload=json_set(payload,'$.service','discord-bot','$.process_started_at',?) where service='meeting' and trace_id=?",
                (iso(self.now - timedelta(days=1)), trace),
            )
            db.commit()
        build(self.raw, self.snapshot, "local", self.now)
        url = "/local/attempt/" + trace
        self.assertIn("<td>open</td>", self.request(url, "dev")[1])
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute(
                "update sources set last_error='read_failed' where service='discord-bot'"
            )
            db.commit()
        self.assertIn("<td>unconfirmed</td>", self.request(url, "dev")[1])
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute("update sources set last_error=null")
            db.execute(
                "update events set payload=json_set(payload,'$.process_started_at',?) where json_extract(payload,'$.schema')='misty.telemetry.v1' and service='discord-bot'",
                (iso(self.now - timedelta(minutes=2)),),
            )
            db.commit()
        build(self.raw, self.snapshot, "local", self.now)
        self.assertIn("<td>unconfirmed</td>", self.request(url, "dev")[1])

    def test_display_loading_partial_and_retention(self):
        self.assertIn('id="loading" hidden', self.request("/local/overview", "lead")[1])
        self.assertIn("loading.hidden=false", self.request("/local/ui.js", "lead")[1])
        self.assertIn("60000", self.request("/local/ui.js", "lead")[1])
        old = iso(self.now - timedelta(days=31))
        self.assertEqual(self.request("/local/overview?end=" + old, "dev")[0], 400)
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute("update sources set drops=1")
            db.commit()
        self.assertIn("Partial history", self.request("/local/overview", "lead")[1])
        with closing(sqlite3.connect(self.snapshot)) as db:
            db.execute("update meta set value=? where key='generated'", (old,))
            db.commit()
        self.assertIn("Snapshot stale", self.request("/local/overview", "lead")[1])

    def test_cohort_retains_late_recovery_and_revoked_access(self):
        with closing(sqlite3.connect(self.raw)) as db:
            # Move only the later save and link confirmation into the next hour.
            db.execute(
                "update events set ts=? where trace_id=? and event_name in ('save.confirmed','link.posted')",
                (iso(self.now + timedelta(hours=1)), f"{3:032x}"),
            )
            db.commit()
        build(self.raw, self.snapshot, "local", self.now + timedelta(hours=1))
        with closing(readonly(self.snapshot, "local")) as snap:
            row = snap.execute(
                "select * from attempts where trace_id=?", (f"{3:032x}",)
            ).fetchone()
            self.assertEqual(row["outcome"], "complete")
            self.assertLess(row["cohort"], iso(self.now))
            self.assertGreater(row["latency_ms"], 3600000)
        with closing(sqlite3.connect(self.snapshot)) as db:
            db.execute("update meta set value='production' where key='environment'")
            db.commit()
        status, body = self.request("/local/overview", "dev")
        self.assertEqual(status, 503)
        self.assertNotIn(self.trace, body)

    def test_linked_recovery_and_parent_gap(self):
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute(
                """update events set trace_id=?,link_trace_id=?, link_span_id=?,
                       parent_span_id=null where trace_id=? and service='connectors'""",
                (f"{300:032x}", self.trace, f"{2 * 16 + 4:016x}", self.trace),
            )
            db.commit()
        ref = "/local/attempt/" + self.trace
        self.assertIn("Async link", self.request(ref, "dev")[1])
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute(
                "update events set link_span_id=? where trace_id=?",
                (f"{999:016x}", f"{300:032x}"),
            )
            db.commit()
        self.assertIn("Missing parent or linked span", self.request(ref, "dev")[1])

    def test_refuse_to_replace_wrong_environment_or_input(self):
        self.assertEqual(
            self.request("/local/overview?end=0001-01-01T00%3A00%3A00Z", "dev")[0], 400
        )
        with self.assertRaises(ValueError):
            build(self.raw, self.raw, "local", self.now)
        with closing(sqlite3.connect(self.snapshot)) as db:
            db.execute("update meta set value='staging' where key='environment'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "environment mismatch"):
            build(self.raw, self.snapshot, "local", self.now)
        with closing(readonly(self.snapshot, "staging")) as db:
            self.assertEqual(
                db.execute("select count(*) from attempts").fetchone()[0], 3
            )

    def test_no_activity_and_unknown_states(self):
        self.assertIn(
            "No activity", self.request("/local/overview?traffic=member", "dev")[1]
        )
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute("update sources set last_error='read_failed'")
            db.commit()
        self.assertIn(
            "Current activity is unknown",
            self.request("/local/overview?traffic=member", "lead")[1],
        )
        self.assertIn("Read failed", self.request("/local/overview", "lead")[1])
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute("delete from sources")
            db.commit()
        self.assertIn(
            "No measurements configured", self.request("/local/overview", "lead")[1]
        )

    def test_alert_retry_silence_and_recovery(self):
        with (
            closing(readonly(self.raw, "local")) as raw,
            closing(readonly(self.snapshot, "local")) as snap,
        ):
            conditions = evaluate(raw, snap, "synthetic", self.now, 60, 3, 1, 300)
            quiet = evaluate(raw, snap, "member", self.now, 60, 3, 1, 300)
        self.assertEqual(conditions["recording"][0], "breach")
        self.assertEqual(quiet["recording"][0], "unknown")
        state = sqlite3.connect(self.root / "alerts.sqlite")
        self.addCleanup(state.close)
        calls = []
        config = dict(
            now=self.now, owner="William", dashboard="http://127.0.0.1:8090", window=60
        )

        def fire(c, ok=True, silenced=False):
            return notify(
                state,
                "local",
                "synthetic",
                c,
                **config,
                silenced=silenced,
                send=lambda t: calls.append(t) or ok,
            )

        self.assertEqual(fire(conditions, silenced=True), [])
        self.assertEqual(fire(conditions, False), [("recording", "breach", False)])
        self.assertEqual(fire(conditions), [("recording", "breach", True)])
        self.assertEqual(fire(conditions), [])
        self.assertEqual(fire(quiet), [])
        self.assertEqual(
            fire({"recording": ("healthy", "good")}), [("recording", "recovery", True)]
        )
        self.assertEqual(fire({"recording": ("healthy", "good")}), [])
        self.assertTrue(
            all("William" in t and "RECORDING-TELEMETRY.md" in t for t in calls)
        )
        self.assertTrue(all(self.trace not in t for t in calls))
        with (
            closing(readonly(self.raw, "local")) as raw,
            closing(readonly(self.snapshot, "local")) as snap,
        ):
            late = evaluate(
                raw, snap, "synthetic", self.now + timedelta(minutes=7), 60, 3, 1, 300
            )
        self.assertEqual(late["collection"][0], "breach")
        self.assertEqual(late["recording"][0], "unknown")
        self.assertEqual(fire(late), [("collection", "breach", True)])
        self.assertEqual(fire(quiet), [("collection", "recovery", True)])


if __name__ == "__main__":
    unittest.main()
