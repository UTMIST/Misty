"""Exercise the built gateway and current directory over HTTP with disposable databases.

Run after building gateway:ci, team-tracking:ci, and verification:ci. Requires
Docker and Python's standard library; never reads .env or contacts a live service.
"""

import json
import os
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def docker(*args, env=None):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, env=os.environ | (env or {}), timeout=120
    )
    if result.returncode:
        # Arguments and command output can contain issued keys. Report only the operation.
        raise RuntimeError(f"docker {args[0]} failed with exit {result.returncode}")
    return result.stdout.strip()


def http(base, path, *, key=None, payload=None, method=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(base + path, data=data, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        body = response.read()
        return response.status, json.loads(body) if body else None


def wait_for(check):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (URLError, OSError, RuntimeError):
            pass
        time.sleep(0.25)
    raise RuntimeError("local smoke service did not become ready")


def main():
    network = f"gateway-smoke-{uuid4().hex[:12]}"
    containers = []
    docker("network", "create", network)

    def start(name, image, env, *, public=False, command=()):
        containers.append(name)
        args = ["run", "-d", "--name", name, "--network", network]
        if public:
            args += ["-p", "127.0.0.1::8000"]
        for field in env:
            args += ["-e", field]
        docker(*args, image, *command, env=env)
        if public:
            port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["8000/tcp"][
                0
            ]["HostPort"]
            return f"http://127.0.0.1:{port}"

    try:
        pg, directory, gateway = (network + suffix for suffix in ("-pg", "-directory", "-api"))
        password = secrets.token_urlsafe(24)
        start(
            pg,
            "postgres:16",
            {"POSTGRES_USER": "smoke", "POSTGRES_PASSWORD": password, "POSTGRES_DB": "directory"},
        )
        # initdb briefly starts a socket-only server; wait for the final TCP listener.
        wait_for(
            lambda: docker(
                "exec", pg, "pg_isready", "-h", "127.0.0.1", "-U", "smoke", "-d", "directory"
            )
        )
        docker("exec", pg, "createdb", "-U", "smoke", "gateway")
        docker("exec", pg, "createdb", "-U", "smoke", "verification")
        directory_db = f"postgresql+psycopg://smoke:{password}@{pg}:5432/directory"
        gateway_db = f"postgresql+psycopg://smoke:{password}@{pg}:5432/gateway"
        verification_db = f"postgresql+psycopg://smoke:{password}@{pg}:5432/verification"
        verification = network + "-verification"
        verification_bootstrap = secrets.token_urlsafe(32)
        credential = json.loads(
            docker(
                "run",
                "--rm",
                "verification:ci",
                "python",
                "-c",
                "import json; from platform_auth import generate_key; "
                "key, prefix, digest = generate_key('vf_'); "
                "print(json.dumps({'key':key,'entry':{'name':'gateway','prefix':prefix,"
                "'key_hash':digest,'scopes':['verification:write']}}))",
            )
        )
        docker(
            "run",
            "--rm",
            "--network",
            network,
            "-e",
            "DATABASE_URL",
            "verification:ci",
            "alembic",
            "upgrade",
            "head",
            env={"DATABASE_URL": verification_db},
        )
        verification_url = start(
            verification,
            "verification:ci",
            {
                "DATABASE_URL": verification_db,
                "API_KEY": verification_bootstrap,
                "VF_ENV": "local",
                "EMAIL_BACKEND": "fake",
                "CONSUMER_KEYS": json.dumps([credential["entry"]]),
                "PORT": "8000",
            },
            public=True,
            # Test-container-only deterministic generator, with an in-memory fake
            # sender. No production test hook, code inbox, or real mail call.
            command=(
                "python",
                "-c",
                "import src.codes; src.codes.generate_code=lambda:'123456'; "
                "import uvicorn; uvicorn.run('src.api.app:app',host='0.0.0.0',port=8000)",
            ),
        )
        wait_for(lambda: http(verification_url, "/health")[0] == 200)
        bootstrap = secrets.token_urlsafe(32)
        docker(
            "run",
            "--rm",
            "--network",
            network,
            "-e",
            "DATABASE_URL",
            "team-tracking:ci",
            "alembic",
            "upgrade",
            "head",
            env={"DATABASE_URL": directory_db},
        )
        directory_url = start(
            directory,
            "team-tracking:ci",
            {"DATABASE_URL": directory_db, "API_KEY": bootstrap, "TT_ENV": "local"},
            public=True,
        )
        wait_for(lambda: http(directory_url, "/health/ready")[0] == 200)
        internal_key = docker(
            "exec",
            directory,
            "team-tracking-keys",
            "issue",
            "--name",
            "gateway",
            "--scopes",
            "identifiers:read",
            "people:read",
        )
        assert internal_key.startswith("tt_")
        gateway_url = start(
            gateway,
            "gateway:ci",
            {
                "DATABASE_URL": gateway_db,
                "DIRECTORY_BASE_URL": f"http://{directory}:8000",
                "DIRECTORY_API_KEY": internal_key,
                "GATEWAY_ENV": "staging",
                "EMAIL_LOGIN_ENABLED": "true",
                "VERIFICATION_BASE_URL": f"http://{verification}:8000",
                "VERIFICATION_API_KEY": credential["key"],
            },
            public=True,
        )
        wait_for(lambda: http(gateway_url, "/health")[0] == 200)
        assert docker("exec", gateway, "id", "-u") != "0", "gateway must run as non-root"
        assert http(gateway_url, "/health/ready")[0] == 503
        docker("exec", gateway, "alembic", "upgrade", "head")
        # Failed readiness checks are cached briefly; recovery appears on refresh.
        wait_for(lambda: http(gateway_url, "/health/ready")[0] == 200)
        print("PASS: non-root runtime and migrations; readiness requires the key table", flush=True)

        status, person = http(
            directory_url,
            "/people",
            key=bootstrap,
            payload={"display_name": "Gateway smoke person", "primary_email": "smoke@example.test"},
        )
        assert status == 201
        for provider, external_id in (
            ("github", "gateway-smoke"),
            ("discord", "123456789012345678"),
        ):
            assert (
                http(
                    directory_url,
                    f"/people/{person['id']}/identifiers",
                    key=bootstrap,
                    payload={"provider": provider, "external_id": external_id},
                )[0]
                == 201
            )
        external_key = docker(
            "exec",
            gateway,
            "gateway-keys",
            "issue",
            "--name",
            "smoke-consumer",
            "--scopes",
            "resolve:discord",
        )
        assert external_key.startswith("gw_")
        path = "/v1/resolve/discord/gateway-smoke"
        assert http(gateway_url, path, key=external_key) == (
            200,
            {"discord_id": "123456789012345678"},
        )
        assert http(gateway_url, path)[0] == 401
        assert http(gateway_url, path, key=internal_key)[0] == 401
        assert http(directory_url, "/people", key=external_key)[0] == 401
        assert http(gateway_url, "/people", key=external_key)[0] == 404
        print(
            "PASS: current directory lookup, curated response, and separate credentials", flush=True
        )

        missing = http(gateway_url, "/v1/resolve/discord/unknown", key=external_key)
        assert (
            http(
                directory_url,
                f"/people/{person['id']}/identifiers/discord",
                key=bootstrap,
                method="DELETE",
            )[0]
            == 204
        )
        unlinked = http(gateway_url, path, key=external_key)
        assert missing == unlinked
        assert missing[0] == 404

        compute_key = docker(
            "exec",
            gateway,
            "gateway-keys",
            "issue",
            "--name",
            "compute",
            "--scopes",
            "auth:email",
            "auth:status",
        )
        # A separate private consumer has just sent a Discord-link code. Age the
        # fixture's timestamp to avoid spending a minute in CI, retaining the real
        # verification cooldown and HTTP response that previously lost this login.
        assert (
            http(
                verification_url,
                "/verification/request-code",
                key=verification_bootstrap,
                payload={"subject": "discord:smoke", "email": "smoke@example.test"},
            )[0]
            == 202
        )
        docker(
            "exec",
            pg,
            "psql",
            "-U",
            "smoke",
            "-d",
            "verification",
            "-c",
            "UPDATE verification_codes SET created_at=now()-interval '55 seconds' WHERE subject='discord:smoke'",
        )
        status, challenge = http(
            gateway_url,
            "/v1/auth/email/start",
            key=compute_key,
            payload={"email": "smoke@example.test", "client_ip": "192.0.2.1"},
        )
        assert status == 202
        challenge_id = challenge["challenge_id"]
        wait_for(
            lambda: (
                docker(
                    "exec",
                    pg,
                    "psql",
                    "-U",
                    "smoke",
                    "-d",
                    "gateway",
                    "-Atc",
                    f"SELECT count(*) FROM login_challenges WHERE id='{challenge_id}' AND person_id IS NOT NULL",
                )
                == "1"
            )
        )
        # Simulate the gateway minute expiring before verification's minute. The
        # resend must eventually activate without superseding the first code.
        docker(
            "exec",
            pg,
            "psql",
            "-U",
            "smoke",
            "-d",
            "gateway",
            "-c",
            "UPDATE login_limits SET expires_at=now()-interval '1 second' WHERE bucket LIKE 'email:%:send:60'",
        )
        docker(
            "exec",
            pg,
            "psql",
            "-U",
            "smoke",
            "-d",
            "verification",
            "-c",
            f"UPDATE verification_codes SET created_at=now()-interval '55 seconds' WHERE subject='gateway-login:{challenge_id}'",
        )
        resend_status, resend = http(
            gateway_url,
            "/v1/auth/email/start",
            key=compute_key,
            payload={"email": "smoke@example.test", "client_ip": "192.0.2.2"},
        )
        assert resend_status == 202
        resend_id = resend["challenge_id"]
        wait_for(
            lambda: (
                docker(
                    "exec",
                    pg,
                    "psql",
                    "-U",
                    "smoke",
                    "-d",
                    "gateway",
                    "-Atc",
                    f"SELECT count(*) FROM login_challenges WHERE id='{resend_id}' AND person_id IS NOT NULL",
                )
                == "1"
            )
        )
        assert (
            http(
                gateway_url,
                "/v1/auth/email/confirm",
                key=compute_key,
                payload={"challenge_id": resend_id, "code": "123456", "client_ip": "192.0.2.2"},
            )[0]
            == 200
        )
        assert (
            docker(
                "exec",
                pg,
                "psql",
                "-U",
                "smoke",
                "-d",
                "gateway",
                "-Atc",
                "SELECT count FROM login_limits WHERE bucket LIKE 'email:%:send:3600'",
            )
            == "2"
        )
        print(
            "PASS: shared-mailbox cooldown and resend timing recover over real private HTTP",
            flush=True,
        )
        payload = {"challenge_id": challenge_id, "code": "123456", "client_ip": "192.0.2.1"}
        assert http(gateway_url, "/v1/auth/email/confirm", key=compute_key, payload=payload) == (
            200,
            {"person_id": person["id"], "email": "smoke@example.test", "issuer": "misty:staging"},
        )
        assert (
            http(gateway_url, "/v1/auth/email/confirm", key=compute_key, payload=payload)[0] == 400
        )
        status_path = f"/v1/auth/people/{person['id']}/status"
        assert http(gateway_url, status_path, key=compute_key) == (
            200,
            {"active": True, "issuer": "misty:staging"},
        )
        assert (
            http(
                directory_url,
                f"/people/{person['id']}",
                key=bootstrap,
                payload={"active": False},
                method="PATCH",
            )[0]
            == 200
        )
        assert http(gateway_url, status_path, key=compute_key) == (
            200,
            {"active": False, "issuer": "misty:staging"},
        )
        print(
            "PASS: scoped email login over private HTTP, single redemption, and retirement",
            flush=True,
        )

        docker("stop", directory)
        assert http(gateway_url, path, key=external_key) == (
            503,
            {"detail": "directory temporarily unavailable"},
        )
        key_id = next(
            line.split()[0]
            for line in docker(
                "exec", gateway, "gateway-keys", "list", "--active-only"
            ).splitlines()
            if "smoke-consumer" in line
        )
        docker("exec", gateway, "gateway-keys", "revoke", key_id)
        assert http(gateway_url, path, key=external_key)[0] == 401
        print("PASS: identical misses, upstream outage, and immediate key revocation", flush=True)
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-fv", container], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
