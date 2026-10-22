"""Bounded metadata enumeration; items are not authorization evidence."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$", max_length=200)
    page_token: str | None = Field(default=None, min_length=1, max_length=4096)
    page_size: int = Field(default=100, ge=1, le=100)


class DiscoveryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=200)
    kind: Literal["file", "folder", "unsupported", "shortcut", "shared_drive"]
    source_id: Literal["gdocs", "gsheets", "gslides", "gdrive"] | None = None


class DiscoveryPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[DiscoveryItem] = Field(max_length=100)
    next_page_token: str | None = Field(default=None, min_length=1, max_length=4096)
    incomplete: bool = False
