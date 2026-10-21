"""Chat model catalog shared by wire validation, configuration, and providers."""

OPENAI_MODELS = {"gpt-6.1-sol"}
BEDROCK_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6"}
MODELS_BY_PROVIDER = {
    "openai": OPENAI_MODELS,
    "bedrock": BEDROCK_MODELS,
    "bedrock-converse": BEDROCK_MODELS,
}
ALLOWED_MODELS = OPENAI_MODELS | BEDROCK_MODELS
