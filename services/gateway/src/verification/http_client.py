import httpx

from contracts.login import VerificationRateLimited, VerificationUnavailable


class HttpVerificationClient:
    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None):
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=httpx.Timeout(10.0))
        self._client.headers["X-API-Key"] = api_key

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, payload: dict) -> httpx.Response:
        try:
            return self._client.post(f"{self._base_url}/verification/{path}", json=payload)
        except httpx.HTTPError:
            raise VerificationUnavailable("verification unreachable") from None

    def request_code(self, subject: str, email: str) -> None:
        response = self._post("request-code", {"subject": subject, "email": email})
        if response.status_code == 429:
            delay = response.headers.get("Retry-After", "")
            try:
                known_cooldown = response.json() == {"detail": "rate_limited"}
            except ValueError:
                known_cooldown = False
            if known_cooldown and len(delay) <= 2 and delay.isascii() and delay.isdecimal():
                seconds = int(delay)
                if 1 <= seconds <= 60:
                    raise VerificationRateLimited(seconds)
        if response.status_code != 202:
            raise VerificationUnavailable("code delivery unavailable")

    def confirm_code(self, subject: str, code: str) -> str | None:
        response = self._post("confirm-code", {"subject": subject, "code": code})
        if response.status_code in {400, 404, 410, 429}:
            return None
        if response.status_code != 200:
            raise VerificationUnavailable("verification unavailable")
        try:
            data = response.json()
        except ValueError:
            raise VerificationUnavailable("invalid verification response") from None
        if (
            not isinstance(data, dict)
            or data.get("verified") is not True
            or data.get("subject") != subject
            or not isinstance(data.get("email"), str)
        ):
            raise VerificationUnavailable("invalid verification response")
        return data["email"]
