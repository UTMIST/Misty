"""Private vocabulary job types. No HTTP resource exposes these records."""

from collections.abc import Callable, Iterable
from typing import NamedTuple
from uuid import UUID

from pydantic import BaseModel, Field


class VocabularyChunk(NamedTuple):
    doc_id: UUID
    ordinal: int
    start: int
    end: int
    text: str


class VocabularyCandidate(BaseModel):
    key: str
    spelling: str
    count: int
    rank: int


class VocabularySnapshot(BaseModel):
    snapshot_id: str
    rule_versions: dict[str, str]
    candidates: list[VocabularyCandidate] = Field(repr=False)
    additions: list[str] = Field(repr=False)
    removals: list[str] = Field(repr=False)


BuildVocabularySnapshot = Callable[[Iterable[VocabularyChunk], set[str]], VocabularySnapshot]


class VocabularyRunBusy(Exception):
    """A vocabulary run already holds the job lock."""
