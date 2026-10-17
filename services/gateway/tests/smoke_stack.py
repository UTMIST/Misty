"""Exercise the built gateway and current directory over HTTP with disposable databases.

Run from the repo root after building gateway:ci and team-tracking:ci. Requires
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

    def start(name, image, env, *, public=False):
        containers.append(name)
        args = ["run", "-d", "--name", name, "--network", network]
        if public:
            args += ["-p", "127.0.0.1::8000"]
        for field in env:
            args += ["-e", field]
        docker(*args, image, env=env)
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
        directory_db = f"postgresql+psycopg://smoke:{password}@{pg}:5432/directory"
        gateway_db = f"postgresql+psycopg://smoke:{password}@{pg}:5432/gateway"
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
            },
            public=True,
        )
        wait_for(lambda: http(gateway_url, "/health")[0] == 200)
        assert docker("exec", gateway, "id", "-u") != "0", "gateway must run as non-root"
        assert http(gateway_url, "/health/ready")[0] == 503
        docker("exec", gateway, "alembic", "upgrade", "head")
        assert http(gateway_url, "/health/ready")[0] == 200
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

        docker("stop", directory)
        assert http(gateway_url, path, key=external_key) == (
            503,
            {"detail": "directory temporarily unavailable"},
        )
        key_id = docker("exec", gateway, "gateway-keys", "list", "--active-only").split()[0]
        docker("exec", gateway, "gateway-keys", "revoke", key_id)
        assert http(gateway_url, path, key=external_key)[0] == 401
        print("PASS: identical misses, upstream outage, and immediate key revocation", flush=True)
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-fv", container], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
