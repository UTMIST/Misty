from pydantic import BaseModel, Field, field_validator

# Allowlist, not a passthrough: an unknown name 422s before any provider call.
ALLOWED_EMBED_MODELS = {
    "openai-embed-3-small",
    "openai-embed-3-large",
}

# MAX_BATCH_INPUTS bounds batch and response size. MAX_INPUT_CHARS is a coarse
# character cap, not a token count: token-dense text can still exceed the model's
# token limit.
MAX_BATCH_INPUTS = 96
MAX_INPUT_CHARS = 32_000


class EmbedRequest(BaseModel):
    inputs: list[str] = Field(min_length=1, max_length=MAX_BATCH_INPUTS)
    model: str | None = None

    @field_validator("inputs")
    @classmethod
    def _validate_inputs(cls, v: list[str]) -> list[str]:
        for i, text in enumerate(v):
            if not text.strip():
                raise ValueError(f"inputs[{i}] is empty or whitespace-only")
            if len(text) > MAX_INPUT_CHARS:
                raise ValueError(
                    f"inputs[{i}] is {len(text)} characters, over the {MAX_INPUT_CHARS} limit"
                )
        return v

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str | None) -> str | None:
        if v is not None and v not in ALLOWED_EMBED_MODELS:
            raise ValueError(f"model must be one of {sorted(ALLOWED_EMBED_MODELS)}")
        return v


class EmbedUsage(BaseModel):
    input_tokens: int


class Embedding(BaseModel):
    # Order is preserved, but the index means a consumer never has to rely on that.
    index: int
    vector: list[float]


class EmbedResponse(BaseModel):
    embeddings: list[Embedding]
    # Resolved provider-side id, not the neutral name asked for, so a stored
    # vector records what produced it.
    model: str
    # A consumer persisting these MUST check this against its column width.
    dimensions: int
    usage: EmbedUsage
