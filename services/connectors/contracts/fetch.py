from typing import Literal

from pydantic import BaseModel, Field


class SourcePermission(BaseModel):
    """One effective Google Drive permission returned with fetched content."""

    permission_id: str = Field(min_length=1)
    principal_type: Literal["user", "group", "domain", "anyone"]
    principal: str | None = None
    role: str = Field(min_length=1)
    inherited: bool = False
    inherited_from: list[str] = []
    allow_file_discovery: bool | None = None
    expiration_time: str | None = None


class FetchRequest(BaseModel):
    url: str = Field(min_length=1)
    # Supplied by the caller rather than re-derived here: documentation-system
    # already resolves it during ingest, and deriving it independently in two
    # services is how the two drift apart.
    source_id: str = Field(min_length=1)


class FetchResponse(BaseModel):
    title: str | None = None
    content: str | None = None
    warnings: list[str] = []
    # None means this source has no ACL contract. Google sources always return
    # a complete list (possibly empty); callers must not treat None as public.
    permissions: list[SourcePermission] | None = None
