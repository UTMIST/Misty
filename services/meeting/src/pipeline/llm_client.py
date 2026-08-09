import httpx


class LlmUnavailable(Exception): ...


class LlmClient:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 60.0):
        self._base = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        self._timeout = timeout_s

    def chat(self, *, messages, system=None, model=None, max_tokens=1500) -> str:
        body = {"messages": messages, "max_tokens": max_tokens}
        if system:
            body["system"] = system
        if model:
            body["model"] = model
        try:
            r = httpx.post(
                f"{self._base}/chat",
                json=body,
                headers=self._headers,
                timeout=self._timeout,
            )
            r.raise_for_status()
            return r.json().get("content", "")
        except (httpx.HTTPError, ValueError) as e:
            raise LlmUnavailable(str(e)) from e
