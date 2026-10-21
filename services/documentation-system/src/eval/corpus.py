"""Load the #178 evaluation corpus and seed it into a storage adapter.

Seeding goes through the real indexing path (`src/indexing.py:index_doc`), not a
private copy of it. That matters more than it looks: `index_doc` prefixes each chunk
with its document title and heading path before embedding (`embedding_input`), and a
harness that skipped that step would score a pipeline the service does not run.

Chunk size is the variable under test, so it is passed in. Everything else about a run
-- the documents, their text, their grants -- is fixed by the corpus file, so two runs
differ only where you intended.

A document whose text lives inline in JSON is unreadable past a paragraph or two, and
the chunker's boundary logic only engages on real headings and lists. So a corpus entry
may instead name a `text_file` beside the corpus, and real documents should use one.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID

from contracts.types import Doc, SourceGrant
from contracts.visibility import Actor, Channel, RetrievalContext
from src.chunking import ChunkSettings
from src.content import content_hash
from src.indexing import index_doc

# Source-derived grants expire, and an expired grant stops authorizing. Seed them far
# enough out that a long sweep cannot start passing and end failing on the clock.
GRANT_LIFETIME = timedelta(days=365)

ACTOR = "eval-harness"


@dataclass(frozen=True)
class CorpusDoc:
    key: str
    title: str
    url: str
    source_id: str
    text: str
    # (grantee_type, grantee_id, origin); origin "manual" is a hand-added grant,
    # anything else is treated as source-derived and reconciled from the source.
    grants: tuple[tuple[str, UUID | None, str], ...]
    active: bool = True


@dataclass(frozen=True)
class Corpus:
    documents: tuple[CorpusDoc, ...]
    # Named retrieval contexts the question set refers to, e.g. "alice", "members".
    contexts: dict[str, RetrievalContext]


@dataclass(frozen=True)
class Seeded:
    docs: dict[str, Doc]
    chunk_counts: dict[str, int]
    settings: ChunkSettings

    @property
    def total_chunks(self) -> int:
        return sum(self.chunk_counts.values())


def load_corpus(path: Path) -> Corpus:
    """Read a corpus file, resolving any `text_file` against its own directory."""
    path = Path(path)
    raw = json.loads(path.read_text())
    documents = []
    for item in raw["documents"]:
        if ("text" in item) == ("text_file" in item):
            raise ValueError(f"{item['key']}: give exactly one of text or text_file")
        text = item["text"] if "text" in item else (path.parent / item["text_file"]).read_text()
        documents.append(
            CorpusDoc(
                key=item["key"],
                title=item["title"],
                url=item["url"],
                source_id=item.get("source_id", "gdocs"),
                text=text,
                grants=tuple(
                    (kind, UUID(grantee) if grantee else None, origin)
                    for kind, grantee, origin in item.get("grants", [])
                ),
                active=item.get("active", True),
            )
        )
    return Corpus(tuple(documents), _contexts(raw.get("contexts", {})))


def _contexts(raw: dict) -> dict[str, RetrievalContext]:
    contexts: dict[str, RetrievalContext] = {}
    for name, spec in raw.items():
        team_ids = frozenset(UUID(team) for team in spec.get("team_ids", []))
        # Most people's manual and source teams are the same set; say so once.
        source_team_ids = (
            frozenset(UUID(team) for team in spec["source_team_ids"])
            if "source_team_ids" in spec
            else team_ids
        )
        if spec["kind"] == "person":
            contexts[name] = Actor(UUID(spec["person_id"]), team_ids, source_team_ids)
        elif spec["kind"] == "channel":
            contexts[name] = Channel(source_team_ids)
        else:
            raise ValueError(f"context {name}: kind must be 'person' or 'channel'")
    return contexts


def seed(adapter, corpus: Corpus, *, embedder, settings: ChunkSettings) -> Seeded:
    """Create every document, apply its grants, and index it at `settings`.

    Raises on any document that does not end up indexed. An unindexed document
    returns no results, which the harness would otherwise score as a retrieval miss
    and you would read as evidence about chunking.
    """
    now = datetime.now(timezone.utc)
    docs: dict[str, Doc] = {}
    chunk_counts: dict[str, int] = {}
    for item in corpus.documents:
        doc = adapter.create_doc(
            url=item.url,
            url_normalized=item.url,
            source_id=item.source_id,
            title=item.title,
            description=None,
            owning_team_id=None,
            owning_team_label=None,
            owning_person_id=None,
            owning_person_label=None,
            content_snapshot=None,
            fetched_at=None,
            tags=[],
            actor=ACTOR,
        )
        docs[item.key] = doc
        _apply_grants(adapter, doc.id, item.grants, now=now)
        adapter.upsert_doc_content(
            doc.id,
            content_text=item.text,
            content_hash=content_hash(item.text),
            fetched_at=now,
        )
        # Retire after indexing, so a retired document exercises the same clearing
        # path the service uses rather than simply never having been indexed.
        outcome = index_doc(
            doc.id, storage=adapter, embedder=embedder, force=True, settings=settings
        )
        if outcome.status != "indexed":
            raise RuntimeError(
                f"{item.key} did not index ({outcome.status}: {outcome.detail}); "
                f"its absence from results would look like a retrieval miss"
            )
        chunk_counts[item.key] = outcome.chunk_count
        if not item.active:
            adapter.update_doc(doc.id, {"active": False}, actor=ACTOR)
    return Seeded(docs, chunk_counts, settings)


def _apply_grants(adapter, doc_id, grants, *, now: datetime) -> None:
    source: list[SourceGrant] = []
    for grantee_type, grantee_id, origin in grants:
        if origin == "manual":
            adapter.add_grant(doc_id, grantee_type=grantee_type, grantee_id=grantee_id, actor=ACTOR)
        else:
            source.append(
                SourceGrant(
                    grantee_type=grantee_type,
                    grantee_id=grantee_id,
                    source_permission_id=f"{grantee_type}-{grantee_id}",
                    source_principal=f"{grantee_type}@example.test",
                    source_role="reader",
                )
            )
    # Always written, even when empty: replacing an empty set is what records that
    # the source was consulted, and a Channel only ever sees source-derived grants.
    adapter.replace_source_grants(
        doc_id,
        origin="google_drive",
        grants=source,
        synced_at=now,
        expires_at=now + GRANT_LIFETIME,
        actor=ACTOR,
    )
