"""Durable fixed windows start at the first admitted action, not clock boundaries."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from ipaddress import ip_address, ip_network

TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
# (bucket, maximum admissions, window seconds)
Limit = tuple[str, int, int]


def now() -> datetime:
    return datetime.now(timezone.utc)


def email_key(email: str) -> str:
    return sha256(email.encode()).hexdigest()


def request_limits(owner: str, operation: str, client_ip: str | None) -> list[Limit]:
    counts = {"start": 30, "confirm": 60, "status": 60}
    limits = [(f"app:{owner}:{operation}", counts[operation], 60)]
    if operation == "start":
        limits.append((f"app:{owner}:send-hour", 300, 3600))
    if client_ip is not None:
        ip = ip_address(client_ip)
        ip = getattr(ip, "ipv4_mapped", None) or ip
        # Group IPv6 privacy addresses so rotating the host part does not reset limits.
        network = str(ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)
        digest = sha256(network.encode()).hexdigest()
        limits.append((f"ip:{digest}:{operation}", 5 if operation == "start" else 30, 60))
    return limits


def send_limits(email: str) -> list[Limit]:
    digest = email_key(email)
    return [
        (f"email:{digest}:send:{seconds}", count, seconds)
        for count, seconds in [(1, 60), (5, 3600), (20, 86400)]
    ]


def guess_limits(email: str) -> list[Limit]:
    return [(f"email:{email_key(email)}:guess", 15, 3600)]
