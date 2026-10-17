from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ApiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    name: str
    prefix: str
    scopes: list[str]
    active: bool = True
    created_at: datetime
    updated_at: datetime
    created_by: str
    updated_by: str
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    scopes: list[str] = []


class IssuedApiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plaintext: str
    api_key: ApiKey
