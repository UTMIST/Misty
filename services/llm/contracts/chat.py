from typing import Literal

from pydantic import BaseModel, Field, field_validator

ALLOWED_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6"}


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    system: str | None = None
    model: str | None = None
    max_tokens: int = Field(default=16000, ge=1, le=64000)
    thinking: bool | None = None

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str | None) -> str | None:
        if v is not None and v not in ALLOWED_MODELS:
            raise ValueError(f"model must be one of {sorted(ALLOWED_MODELS)}")
        return v

    @field_validator("system")
    @classmethod
    def _validate_system(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("system must be valid UTF-8") from None
        return v


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class ChatResponse(BaseModel):
    content: str
    model: str
    stop_reason: str
    usage: Usage
