import httpx


class LlmUnavailable(Exception): ...


class LlmTimeout(LlmUnavailable): ...


class LlmClient:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 60.0):
        self._base = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        self._timeout = timeout_s

    def chat(self, *, messages, system=None, model=None, max_tokens=1500, thinking=None) -> str:
        body = {"messages": messages, "max_tokens": max_tokens}
        if system:
            body["system"] = system
        if model:
            body["model"] = model
        if thinking is not None:
            body["thinking"] = thinking
        try:
            r = httpx.post(
                f"{self._base}/chat",
                json=body,
                headers=self._headers,
                timeout=self._timeout,
            )
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError:
                raise LlmUnavailable("malformed LLM response") from None
            if not isinstance(data, dict) or not isinstance(data.get("content", ""), str):
                raise LlmUnavailable("malformed LLM response")
            return data.get("content", "")
        except httpx.TimeoutException:
            raise LlmTimeout("LLM request timed out") from None
        except (httpx.HTTPError, ValueError) as e:
            raise LlmUnavailable(str(e)) from e
