"""Config-driven chat provider selection and OpenAI embedding construction."""

from collections.abc import Callable

from src.config import Settings
from src.providers.base import EmbeddingProvider, LLMProvider
from src.providers.bedrock import BedrockClaudeProvider
from src.providers.bedrock_converse import BedrockConverseProvider
from src.providers.openai_embed import OpenAIEmbeddingProvider


def _build_bedrock(settings: Settings) -> LLMProvider:
    return BedrockClaudeProvider(
        aws_region=settings.aws_region,
        default_model=settings.llm_model,
        timeout_s=settings.request_timeout_s,
    )


def _build_bedrock_converse(settings: Settings) -> LLMProvider:
    return BedrockConverseProvider(
        aws_region=settings.aws_region,
        default_model=settings.llm_model,
        timeout_s=settings.request_timeout_s,
    )


PROVIDERS: dict[str, Callable[[Settings], LLMProvider]] = {
    "bedrock": _build_bedrock,  # Anthropic Messages/Mantle endpoint (needs global model access)
    "bedrock-converse": _build_bedrock_converse,  # bedrock-runtime Converse (US-regional profiles)
}


def get_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider not in PROVIDERS:
        raise ValueError(f"unknown LLM provider: {settings.llm_provider!r}")
    return PROVIDERS[settings.llm_provider](settings)


def get_embedding_provider(settings: Settings) -> EmbeddingProvider:
    return OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key.get_secret_value(),
        default_model=settings.embed_model,
        timeout_s=settings.request_timeout_s,
    )
