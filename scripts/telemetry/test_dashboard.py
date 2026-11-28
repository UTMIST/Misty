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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, build_opener
from pathlib import Path

from alerts import discord_send, evaluate, notify
from dashboard import load_access, make_handler, report_page
from pilot import fixture, seed
from project import build, iso, prune_snapshot, readonly
from recording_view import percentiles


class PilotAcceptance(unittest.TestCase):
    def test_replay_through_actual_collector(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "raw.sqlite"
            snapshot = Path(temp) / "snapshot.sqlite"
            now = datetime.now(timezone.utc)
            seed(raw, snapshot, now)
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
                finish = db.execute(
                    "select min(finish) from spans where service='connectors'"
                ).fetchone()[0]
                with closing(readonly(raw, "local")) as source:
                    page = report_page(
                        db,
                        source,
                        "local",
                        "component",
                        {
                            "service": "connectors",
                            "operation": "connectors.google_create",
                            "traffic": "synthetic",
                            "end": finish,
                        },
                        "lead",
                        now,
                        300,
                    )
                self.assertIn(
                    "1 observed calls · Median — · p95 — · 0 timed samples", page
                )
                self.assertIn("<td>unknown</td><td>1</td>", page)
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
        access_file.touch(mode=0o600, exist_ok=False)
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

    def test_disputed_component_counts_once_without_a_latency_sample(self):
        with closing(sqlite3.connect(self.raw)) as db:
            db.row_factory = sqlite3.Row
            receipt = dict(
                db.execute(
                    """select * from events where service='connectors' and event_name='end'
                   order by ts limit 1"""
                ).fetchone()
            )
            first_end = datetime.fromisoformat(receipt["ts"].replace("Z", "+00:00"))
            disputed_time = iso(first_end + timedelta(seconds=3))
            disputed_result = iso(first_end + timedelta(seconds=5))
            for event_id, ts, result in (
                ("f" * 32, disputed_result, "failed"),
                ("e" * 32, disputed_time, "success"),
            ):
                conflict = dict(
                    event_id=event_id, ts=ts, result=result, elapsed_ms=9000
                )
                row = dict(receipt, **conflict)
                row["payload"] = json.dumps(
                    dict(json.loads(receipt["payload"]), **conflict)
                )
                db.execute(
                    "insert into events values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(row.values()),
                )
            db.commit()
        build(self.raw, self.snapshot, "local", self.now)
        with closing(readonly(self.snapshot, "local")) as snapshot:
            disputed = snapshot.execute(
                "select result,elapsed_ms from spans where trace_id=? and span_id=?",
                (receipt["trace_id"], receipt["span_id"]),
            ).fetchall()
            self.assertEqual([tuple(row) for row in disputed], [("unknown", None)])
        url = "/local/component?traffic=synthetic&service=connectors&operation=connectors.google_create"
        for end, outcome, samples in (
            (iso(first_end), "unknown", 0),
            (disputed_time, "success", 1),
            (disputed_result, "success", 0),
            (iso(first_end + timedelta(seconds=6)), "unknown", 0),
        ):
            with self.subTest(end=end):
                status, component = self.request(url + "&end=" + end, "lead")
                self.assertEqual(status, 200)
                self.assertIn(f"<td>{outcome}</td><td>1</td>", component)
                self.assertIn(
                    f"{samples} timed samples", component.split("Previous period")[0]
                )
        status, component = self.request(url, "dev")
        self.assertEqual(status, 200)
        self.assertIn(
            "2 observed calls · Median 320 ms · p95 — · 1 timed samples", component
        )
        self.assertIn("<td>unknown</td><td>1</td>", component)
        self.assertIn("<td>success</td><td>1</td>", component)
        _, detail = self.request("/local/attempt/" + receipt["trace_id"], "dev")
        timeline, tree = detail.split("<h2>Call tree</h2>")
        self.assertIn("9000 ms", timeline)
        self.assertNotIn("9000 ms", tree)

    def test_call_tree_with_missing_end_has_no_elapsed_time(self):
        with closing(sqlite3.connect(self.raw)) as db:
            span = db.execute(
                """select span_id from events where trace_id=? and service='connectors' and event_name='start'""",
                (self.trace,),
            ).fetchone()[0]
            db.execute(
                "delete from events where trace_id=? and span_id=? and event_name='end'",
                (self.trace, span),
            )
            db.commit()
        build(self.raw, self.snapshot, "local", self.now)
        status, detail = self.request("/local/attempt/" + self.trace, "dev")
        self.assertEqual(status, 200)
        tree = detail.split("<h2>Call tree</h2>")[1]
        self.assertIn(f"<td>{span}</td><td>Child</td><td>—</td>", tree)

    def test_existing_snapshot_hides_end_at_period_boundary(self):
        with closing(sqlite3.connect(self.snapshot)) as db:
            db.execute("alter table spans rename to new_spans")
            db.execute("""create table spans as select trace_id,span_id,parent,link_trace,link_span,
                service,operation,traffic,start,finish,result,elapsed_ms from new_spans""")
            finish = db.execute(
                "select min(finish) from spans where service='connectors'"
            ).fetchone()[0]
            db.commit()
        status, page = self.request(
            "/local/component?traffic=synthetic&service=connectors&operation=connectors.google_create&end="
            + finish,
            "lead",
        )
        self.assertEqual(status, 200)
        self.assertIn("<td>unknown</td><td>1</td>", page)
        self.assertIn("1 observed calls · Median — · p95 — · 0 timed samples", page)

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

    def test_another_traffic_class_cannot_complete_or_resolve_an_incident(self):
        trace = f"{1:032x}"
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute(
                "update events set traffic='member' where trace_id=? and event_name='pdf.posted'",
                (trace,),
            )
            db.commit()
        build(self.raw, self.snapshot, "local", self.now)
        self.assertIn(
            "<td>unconfirmed</td>", self.request("/local/attempt/" + trace, "dev")[1]
        )
        self.assertEqual(
            self.request("/local/attempt/" + trace + "?traffic=member", "dev")[0], 404
        )
        with (
            closing(readonly(self.raw, "local")) as raw,
            closing(readonly(self.snapshot, "local")) as snap,
        ):
            self.assertEqual(
                evaluate(raw, snap, "synthetic", self.now, 60, 3, 2, 300)["recording"][
                    0
                ],
                "unknown",
            )

    def test_discord_wire_payload_and_failed_delivery(self):
        received = []
        responses = iter((429, 204, 302, 204))

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                status = next(responses)
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/other")
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        class LocalTransport:
            def __init__(self, redirect_policy):
                self.receiver = build_opener(redirect_policy)

            def open(self, req, timeout):
                # Replace only the final network destination with a local listener.
                if req.full_url != "https://discord.com/api/webhooks/1/fake":
                    raise AssertionError("unexpected destination")
                local = Request(
                    f"http://127.0.0.1:{server.server_port}/messages",
                    data=req.data,
                    headers=dict(req.header_items()),
                )
                return self.receiver.open(local, timeout=timeout)

        with patch("alerts.build_opener", LocalTransport):
            deliver = discord_send("https://discord.com/api/webhooks/1/fake")
            with closing(sqlite3.connect(self.root / "incidents.sqlite")) as state:

                def transition(value):
                    return notify(
                        state,
                        "local",
                        "synthetic",
                        {"recording": (value, "fixture")},
                        now=self.now,
                        owner="William",
                        dashboard="http://127.0.0.1:8090",
                        window=60,
                        send=deliver,
                    )

                self.assertEqual(transition("breach"), [("recording", "breach", False)])
                self.assertEqual(transition("breach"), [("recording", "breach", True)])
                self.assertEqual(transition("breach"), [])
                self.assertEqual(
                    transition("healthy"), [("recording", "recovery", False)]
                )
                self.assertEqual(
                    transition("healthy"), [("recording", "recovery", True)]
                )
        self.assertEqual(len(received), 4)
        self.assertTrue(
            all(
                r["allowed_mentions"] == {"parse": []}
                and self.trace not in r["content"]
                for r in received
            )
        )

    def test_discord_ack_completes_on_time_despite_unconfirmed_catalog_receipt(self):
        trace = f"{3:032x}"
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute(
                "update events set event_name='link.acknowledged',payload=json_set(payload,'$.event_name','link.acknowledged') where trace_id=? and event_name='link.posted'",
                (trace,),
            )
            db.commit()
        build(self.raw, self.snapshot, "local", self.now)
        with closing(readonly(self.snapshot, "local")) as snap:
            row = snap.execute(
                "select outcome,link,latency_ms from attempts where trace_id=?",
                (trace,),
            ).fetchone()
            self.assertEqual(tuple(row), ("complete", "link.acknowledged", 60010))
        self.assertIn(
            "link.acknowledged", self.request("/local/attempt/" + trace, "dev")[1]
        )

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

    def test_offline_snapshot_expiration_and_retained_partial_window(self):
        original = self.now
        trace = self.trace
        ref = "/local/attempt/" + trace
        self.now = original + timedelta(days=28, hours=22)
        retained = ref + "?hours=168&end=" + iso(original)
        status, detail = self.request(retained, "dev")
        self.assertEqual(status, 200)
        self.assertIn("retention cutoff", detail)
        self.assertIn("Snapshot stale", detail)
        self.now = original + timedelta(days=30)
        self.assertEqual(self.request(retained, "dev")[0], 400)
        self.assertEqual(self.request(ref, "dev")[0], 404)
        self.assertNotIn(trace, self.request("/local/overview", "dev")[1])
        self.raw.rename(self.root / "offline-collector.sqlite")
        self.assertEqual(self.request(retained, "lead")[0], 403)
        status, body = self.request(ref, "dev")
        self.assertEqual(status, 503)
        self.assertNotIn(trace, body)
        with self.assertRaisesRegex(ValueError, "environment mismatch"):
            prune_snapshot(self.snapshot, "staging", self.now)
        self.assertIn(trace.encode(), self.snapshot.read_bytes())
        prune_snapshot(self.snapshot, "local", self.now)
        with closing(readonly(self.snapshot, "local")) as db:
            self.assertEqual(
                db.execute("select count(*) from attempts").fetchone()[0], 0
            )
            self.assertEqual(db.execute("select count(*) from spans").fetchone()[0], 0)
            self.assertEqual(
                db.execute("select value from meta where key='generated'").fetchone()[
                    0
                ],
                iso(original),
            )
        self.assertNotIn(trace.encode(), self.snapshot.read_bytes())

    def test_failed_before_capture_is_visible_and_excluded_from_started(self):
        trace = f"{1:032x}"
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute(
                "delete from events where trace_id=? and event_name not in ('record.pdf_requested','capture.started')",
                (trace,),
            )
            db.execute(
                "update events set event_name='capture.start_failed',payload=json_set(payload,'$.event_name','capture.start_failed') where trace_id=? and event_name='capture.started'",
                (trace,),
            )
            db.commit()
        build(self.raw, self.snapshot, "local", self.now)
        with (
            closing(readonly(self.snapshot, "local")) as snap,
            closing(readonly(self.raw, "local")) as raw,
        ):
            row = snap.execute(
                "select started,outcome from attempts where trace_id=?", (trace,)
            ).fetchone()
            self.assertEqual(tuple(row), (0, "observed_error"))
            self.assertEqual(
                evaluate(raw, snap, "synthetic", self.now, 60, 3, 1, 300)["recording"][
                    0
                ],
                "unknown",
            )
        self.assertIn(
            "observed_error", self.request("/local/attempt/" + trace, "dev")[1]
        )

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
