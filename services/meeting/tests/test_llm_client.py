import httpx
import pytest

from src.pipeline.llm_client import LlmClient, LlmTimeout


def test_thinking_is_only_sent_when_explicit(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json={"content": "ok"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    client = LlmClient("http://fake", "key", 12)
    assert client.chat(messages=[]) == "ok"
    assert "thinking" not in calls[0]["json"]
    client.chat(messages=[], thinking=False, max_tokens=1024)
    assert calls[1]["json"]["thinking"] is False
    assert calls[1]["timeout"] == 12


def test_timeout_is_normalized_without_provider_details(monkeypatch):
    def post(*args, **kwargs):
        raise httpx.ReadTimeout("PRIVATE")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(LlmTimeout, match="timed out") as error:
        LlmClient("http://fake", "key").chat(messages=[])
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("envelope", [[], "PRIVATE", None])
def test_malformed_success_envelope_is_normalized(monkeypatch, envelope):
    from src.pipeline.llm_client import LlmUnavailable

    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200, json=envelope, request=httpx.Request("POST", url)
        ),
    )
    with pytest.raises(LlmUnavailable, match="malformed LLM response"):
        LlmClient("http://fake", "key").chat(messages=[])
