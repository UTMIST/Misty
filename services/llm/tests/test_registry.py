from unittest.mock import Mock

import pytest

from contracts.embed import ALLOWED_EMBED_MODELS
from src.config import Settings
from src.providers import openai_embed, registry
from src.providers.bedrock import BedrockClaudeProvider
from src.providers.bedrock_converse import BedrockConverseProvider
from src.providers.openai_embed import OpenAIEmbeddingProvider
from src.providers.registry import get_embedding_provider, get_provider


@pytest.fixture(autouse=True)
def _dummy_aws_creds(monkeypatch):
    # The Bedrock client resolves AWS creds lazily (at request time), but set
    # dummy values so construction never touches a real credential chain in CI.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def test_get_provider_returns_bedrock():
    settings = Settings(llm_provider="bedrock", aws_region="us-east-1", llm_model="claude-sonnet-5")
    provider = get_provider(settings)
    assert isinstance(provider, BedrockClaudeProvider)


def test_get_provider_returns_bedrock_converse():
    settings = Settings(
        llm_provider="bedrock-converse", aws_region="us-east-1", llm_model="claude-sonnet-4-6"
    )
    provider = get_provider(settings)
    assert isinstance(provider, BedrockConverseProvider)


def test_unknown_provider_raises():
    settings = Settings(llm_provider="nope", aws_region="us-east-1")
    with pytest.raises(ValueError, match="unknown LLM provider"):
        get_provider(settings)


@pytest.mark.parametrize("model", ["openai-embed-3-small", "openai-embed-3-large"])
def test_get_embedding_provider_passes_openai_settings(monkeypatch, model):
    constructor = Mock(wraps=OpenAIEmbeddingProvider)
    monkeypatch.setattr(registry, "OpenAIEmbeddingProvider", constructor)
    settings = Settings(
        embed_model=model,
        openai_api_key="synthetic-openai-key",
        request_timeout_s=17.5,
    )

    provider = get_embedding_provider(settings)

    assert isinstance(provider, OpenAIEmbeddingProvider)
    constructor.assert_called_once_with(
        api_key="synthetic-openai-key", default_model=model, timeout_s=17.5
    )


def test_embedding_model_tables_match_the_wire_allowlist():
    expected_ids = {
        "openai-embed-3-small": "text-embedding-3-small",
        "openai-embed-3-large": "text-embedding-3-large",
    }
    assert ALLOWED_EMBED_MODELS == set(expected_ids)
    assert openai_embed._MODEL_IDS == expected_ids
    assert openai_embed._MODEL_DIMENSIONS == dict.fromkeys(expected_ids, 1536)
