"""Verify public login across three local HTTP services and the offline collector."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
EMAIL = "telemetry-wire@example.org"
CODE = "028493"
KEY = "local-wire-input"


def serve(name, fd, work):
    service = ROOT / "services" / name
    sys.path[:0] = [str(service), str(ROOT / "packages/auth")]
    # Work from the ephemeral directory. A developer .env is never loaded.
    import contracts
    import src
    import platform_auth
    import uvicorn
    from src.api.app import create_app
    from src.api.deps import get_storage

    for module, path in (
        (src, service / "src"),
        (contracts, service / "contracts"),
        (platform_auth, ROOT / "packages/auth/platform_auth"),
    ):
        assert Path(module.__file__).resolve().parent == path
    app = create_app()
    if name == "team-tracking":
        from contracts.types import PersonCreate
        from src.storage.in_memory import InMemoryStorageAdapter

        db = InMemoryStorageAdapter()
        db.create_person(
            PersonCreate(display_name="Fixture member", primary_email=EMAIL),
            actor="fixture",
        )
    elif name == "verification":
        from src.api.routers import verification
        from src.storage.in_memory import InMemoryVerificationStore

        # Known test code stays inside the fake delivery process. Storage and
        # verification still use the production code-hash and redemption paths.
        verification.generate_code = lambda: CODE
        db = InMemoryVerificationStore()
    else:
        from src.api.hashing import generate_key
        from src.storage.in_memory import InMemoryStorageAdapter

        db = InMemoryStorageAdapter()
        token, prefix, digest = generate_key()
        db.create_api_key(
            name="fixture",
            prefix=prefix,
            key_hash=digest,
            scopes=["auth:email"],
            actor="fixture",
        )
        (work / "key").write_text(token)
    app.dependency_overrides[get_storage] = lambda: db
    uvicorn.run(app, fd=fd, log_level="info")


def http(base, path, data=None, key=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(
        base + path,
        body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": key or KEY,
            "traceparent": "00-" + "ac" * 16 + "-" + "de" * 8 + "-01",
        },
    )
    try:
        response = urlopen(req, timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def verify(work):
    from collect import pull
    from store import connect, iso

    logs = {}
    bases = {}
    with ExitStack() as stack:
        for name in ("team-tracking", "verification", "gateway"):
            sock = stack.enter_context(socket.socket())
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            bases[name] = "http://127.0.0.1:" + str(sock.getsockname()[1])
            logs[name] = work / (name + ".log")
            log = stack.enter_context(logs[name].open("w"))
            env = {
                "PATH": os.environ["PATH"],
                "TELEMETRY_ENABLED": "true",
                "TELEMETRY_ENV": "local",
                "TELEMETRY_TRAFFIC": "synthetic",
                "API_KEY": KEY,
                "EMAIL_BACKEND": "fake",
                "EMAIL_LOGIN_ENABLED": "true",
                "DIRECTORY_BASE_URL": bases["team-tracking"],
                "DIRECTORY_API_KEY": KEY,
                "VERIFICATION_BASE_URL": bases.get(
                    "verification", "http://127.0.0.1:1"
                ),
                "VERIFICATION_API_KEY": KEY,
            }
            child = subprocess.Popen(
                [sys.executable, __file__, "--serve", name, "--fd", str(sock.fileno())],
                cwd=work,
                pass_fds=(sock.fileno(),),
                stdout=log,
                stderr=log,
                env=env,
            )

            def stop(p=child):
                p.terminate()
                try:
                    p.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=4)

            stack.callback(stop)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(name + " fixture exited before readiness")
                try:
                    if http(bases[name], "/health")[0] == 200:
                        break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.04)
            else:
                raise RuntimeError(name + " fixture not ready")
        base = bases["gateway"]
        token = (work / "key").read_text()
        start, payload = http(
            base,
            "/v1/auth/email/start",
            {"email": EMAIL, "client_ip": "192.0.2.7"},
            token,
        )
        challenge = payload["challenge_id"]
        request = {"challenge_id": challenge, "client_ip": "192.0.2.7", "code": CODE}
        # The 202 precedes independent delivery. Wait only for local evidence
        # that activation finished, so polling cannot consume any guesses.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any(
                '"operation":"login.delivery"' in line and '"event_name":"end"' in line
                for line in logs["gateway"].read_text().splitlines()
            ):
                break
            time.sleep(0.02)
        wrong = http(
            base, "/v1/auth/email/confirm", {**request, "code": "741920"}, token
        )[0]
        good, person = http(base, "/v1/auth/email/confirm", request, token)
        replay = http(base, "/v1/auth/email/confirm", request, token)[0]
        assert (start, wrong, good, replay) == (202, 400, 200, 400)
    db = connect(work / "evidence.sqlite", "local")
    try:
        now = datetime.now(timezone.utc)
        raw_locations = []
        for name, path in logs.items():
            rows = work / (name + ".jsonl")
            with rows.open("w") as target:
                for line in path.read_text().splitlines():
                    if EMAIL.split("@")[0] in line:
                        raw_locations.append(name)
                    target.write(
                        json.dumps({"timestamp": iso(now), "message": line}) + "\n"
                    )
            args = SimpleNamespace(
                command="import",
                service=name,
                environment="local",
                until=iso(now),
                available_since=None,
                traffic="synthetic",
                input=rows,
                overlap_seconds=60,
                window_seconds=300,
            )
            pull(db, args, now)
        events = [json.loads(r[0]) for r in db.execute("select payload from events")]
        persisted = " ".join(db.iterdump())
        assert all(
            c not in persisted
            for c in (
                EMAIL.split("@")[0],
                challenge,
                person["person_id"],
                token,
                KEY,
                "192.0.2.7",
                "ac" * 16,
            )
        )
        assert all(f'"{code}"' not in persisted for code in (CODE, "741920"))
        ended = [e for e in events if e["event_name"] == "end"]
        edges = []
        for e in ended:
            if (
                e["service"] in ("team-tracking", "verification")
                and e["operation"] == "http.request"
                and "parent_span_id" in e
            ):
                [parent] = [
                    p
                    for p in ended
                    if p["trace_id"] == e["trace_id"]
                    and p["span_id"] == e["parent_span_id"]
                ]
                if e["service"] == "team-tracking":
                    assert e["route"] in ("/people/by-email", "/people/{person_id}")
                edges.append((parent["operation"], e["service"], e["result"]))
        assert {a for a, _, _ in edges} == {
            "login.check",
            "login.directory",
            "login.send",
        }
        assert not raw_locations, raw_locations
        return {
            "HTTP": {
                "start": start,
                "wrong_code": wrong,
                "confirm": good,
                "replay": replay,
            },
            "linked_components": edges,
            "events_collected": len(events),
            "legacy_synthetic_email_locations": sorted(raw_locations),
            "member_or_login_values_in_collector": False,
        }
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--serve",
        choices=["team-tracking", "verification", "gateway"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--fd", type=int, help=argparse.SUPPRESS)
    opts = parser.parse_args()
    if opts.serve:
        serve(opts.serve, opts.fd, Path.cwd())
    else:
        with TemporaryDirectory() as folder:
            print(json.dumps(verify(Path(folder)), indent=2))
