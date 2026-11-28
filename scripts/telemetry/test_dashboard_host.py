"""Boot each installed workflow with its sibling physically absent."""

import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

from dashboard import load_access
from project import FEATURES
from store import connect, insert, iso

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path("scripts/telemetry")


TRACE = "a" * 32
KEY = "test-secret"


def install_workflow(root, feature):
    files = [
        SCRIPTS / f
        for f in ("dashboard.py", "dashboard_ui.py", "project.py", "store.py")
    ]
    files.append(Path("packages/auth/platform_auth/telemetry_contract.json"))
    if feature == "recording":
        files.extend(
            SCRIPTS / f for f in ("recording_projection.py", "recording_view.py")
        )
        files.extend(
            Path("discord-bot") / f
            for f in (
                "package.json",
                "scripts/recording-report.js",
                "src/meeting/recordingProjection.js",
                "src/meeting/stopTrace.js",
            )
        )
    else:
        files.extend(SCRIPTS / f for f in ("health_projection.py", "health.py"))
    for file in files:
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / file, root / file)


def seed_collector(raw):
    # The raw store contains both workflows, even when the host includes one.
    now = iso(datetime.now(timezone.utc) - timedelta(minutes=2))
    base = dict(
        trace_id=TRACE, span_id="b" * 16, ts=now, elapsed_ms=0, dropped_before=0
    )
    with closing(connect(raw, "local")) as db:
        insert(
            db,
            dict(
                base,
                schema="misty.recording.v1",
                event_name="record.pdf_requested",
            ),
            "discord-bot",
            "synthetic",
            now,
        )
        insert(
            db,
            dict(
                base,
                schema="misty.telemetry.v1",
                span_id="c" * 16,
                event_id="d" * 32,
                service="discord-bot",
                operation="answer",
                environment="local",
                traffic="synthetic",
                deployment="unknown",
                process_id="e" * 16,
                event_name="start",
                result="started",
            ),
            "discord-bot",
            "synthetic",
            now,
        )
        db.commit()


def get(url, path, auth=True):
    headers = (
        {"Authorization": "Basic " + base64.b64encode(f"test:{KEY}".encode()).decode()}
        if auth
        else {}
    )
    with closing(
        http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    ) as client:
        client.request("GET", path, headers=headers)
        res = client.getresponse()
        return res.status, res.read().decode()


class DashboardHostTests(unittest.TestCase):
    def test_access_rejects_public_file_link_and_fifo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            access = root / "access.json"
            access.write_text(
                json.dumps(
                    [
                        dict(
                            user="ops",
                            environment="local",
                            role="lead",
                            sha256="f" * 64,
                        )
                    ]
                )
            )
            access.chmod(0o640)
            with self.assertRaises(ValueError):
                load_access(access)
            access.chmod(0o600)
            self.assertIn(("local", "ops"), load_access(access))
            link = root / "link"
            link.symlink_to(access)
            with self.assertRaises(OSError):
                load_access(link)
            fifo = root / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):
                load_access(fifo)

    def exercise(self, feature):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            install_workflow(root, feature)
            raw = root / "raw.sqlite"
            snap = root / "snap.sqlite"
            seed_collector(raw)
            env = {**os.environ, "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"}
            cli = [
                sys.executable,
                str(root / SCRIPTS / "project.py"),
                "--environment",
                "local",
                "--snapshot",
                str(snap),
            ]
            subprocess.run([*cli, "--db", str(raw)], env=env, check=True, timeout=10)
            subprocess.run([*cli, "--prune"], env=env, check=True, timeout=10)
            table = "attempts" if feature == "recording" else "health_runs"
            with closing(sqlite3.connect(snap)) as db:
                tables = {
                    r[0]
                    for r in db.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
                self.assertNotIn(
                    "health_runs" if feature == "recording" else "attempts", tables
                )
                self.assertEqual(
                    db.execute(f"select count(*) from {table}").fetchone()[0], 1
                )
            access = root / "grants.json"
            access.touch(mode=0o600)
            access.write_text(
                json.dumps(
                    [
                        dict(
                            user="test",
                            environment="local",
                            role="lead",
                            sha256=hashlib.sha256(KEY.encode()).hexdigest(),
                        )
                    ]
                )
            )
            server = subprocess.Popen(
                [
                    sys.executable,
                    str(root / SCRIPTS / "dashboard.py"),
                    "--environment",
                    "local",
                    "--db",
                    str(raw),
                    "--snapshot",
                    str(snap),
                    "--access",
                    str(access),
                    "--port",
                    "0",
                ],
                env=env,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertTrue(
                    select.select([server.stdout], [], [], 10)[0],
                    "dashboard startup timeout",
                )
                url = urlsplit(
                    server.stdout.readline().strip().removeprefix("Listening on ")
                )
                self.assertEqual(url.hostname, "127.0.0.1")

                self.assertEqual(get(url, "/", False)[0], 401)
                status, body = get(url, "/")
                self.assertEqual(status, 200)
                heading = (
                    "Recording outcomes"
                    if feature == "recording"
                    else "Workflow health"
                )
                self.assertIn(heading, body)
                absent_route = (
                    "/local/health" if feature == "recording" else "/local/overview"
                )
                self.assertNotIn(absent_route, body)
                self.assertEqual(get(url, absent_route)[0], 404)
                self.assertEqual(get(url, "/production/health")[0], 403)
                self.assertEqual(get(url, "/local/attempt/" + TRACE)[0], 403)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                finally:
                    server.kill()
                    server.wait()
                    server.stdout.close()

    @unittest.skipUnless("recording" in FEATURES, "recording view is not installed")
    def test_recording_only(self):
        self.exercise("recording")

    @unittest.skipUnless("health" in FEATURES, "health view is not installed")
    def test_health_only(self):
        self.exercise("health")
