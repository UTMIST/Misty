from src.providers.base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResult,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)


def test_request_defaults():
    req = LLMRequest(messages=[LLMMessage(role="user", content="hi")])
    assert req.system is None
    assert req.model is None
    assert req.max_tokens == 16000
    assert req.thinking is True


def test_result_fields():
    res = LLMResult(
        content="hi",
        model="anthropic.claude-sonnet-5",
        stop_reason="end_turn",
        input_tokens=3,
        output_tokens=2,
    )
    assert res.content == "hi"
    assert res.output_tokens == 2


def test_error_hierarchy():
    for cls in (ProviderRateLimited, ProviderTimeout, ProviderUnavailable):
        assert issubclass(cls, ProviderError)


def test_protocol_is_runtime_usable():
    class _Fake:
        def chat(self, request: LLMRequest) -> LLMResult:
            return LLMResult(
                content="ok", model="m", stop_reason="end_turn", input_tokens=0, output_tokens=0
            )

    provider: LLMProvider = _Fake()
    out = provider.chat(LLMRequest(messages=[LLMMessage(role="user", content="x")]))
    assert out.content == "ok"
