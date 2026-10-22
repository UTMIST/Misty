"""Measure retrieval quality against a fixed question set.

Run it:

    uv run python -m src.eval.harness
    uv run python -m src.eval.harness --config 500:50 --config 250:25
    uv run python -m src.eval.harness calibrate

What the number means, and what it does not:

hit@k is the share of questions whose correct document appeared anywhere in the top k.
It is measured with a deliberately dumb lexical embedder (src/eval/embedding.py), so it
says nothing about how well real embeddings would do. Its one job is comparison: every
run holds the corpus, the questions, the embedder and the relevance floor fixed, so a
difference between two runs is attributable to the one setting that changed.

Two runs scoring the same is a result: equal scores are a valid outcome, not a reason to
go looking for a difference.

Every result carries the configuration that produced it -- chunk size, overlap, chunker
version, token counting, embedder, k and the floor. A score without its configuration
is not comparable to anything, so they are never printed apart.
"""

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sys

from src.chunking import CHUNKER_VERSION, ChunkSettings
from src.eval.corpus import Corpus, Seeded, load_corpus, seed
from src.eval.embedding import LexicalEmbedder
from src.storage.in_memory import InMemoryStorageAdapter

FIXTURES = Path(__file__).resolve().parents[2] / "docs/fixtures"
DEFAULT_CORPUS = FIXTURES / "eval-corpus.json"
DEFAULT_QUESTIONS = FIXTURES / "eval-questions.json"

DEFAULT_K = 5
# Production's search_min_cosine (src/config.py). The harness measures the system as
# configured rather than a floor chosen to flatter it: see `calibrate`, which reports
# that on this corpus no floor cleanly separates correct documents from incorrect ones.
# Held constant across a sweep, so a difference between configurations is the chunking.
DEFAULT_MIN_COSINE = 0.3


@dataclass(frozen=True)
class Question:
    id: str
    query: str
    context: str
    expected_doc_keys: tuple[str, ...]
    forbidden_doc_keys: tuple[str, ...] = ()
    expected_section: str | None = None
    rationale: str = ""


@dataclass(frozen=True)
class QuestionResult:
    id: str
    status: str
    passed: bool
    returned: tuple[str, ...]
    # 1-based position of the first expected document, or None when it never appeared.
    rank: int | None = None


@dataclass(frozen=True)
class Run:
    config: dict
    results: tuple[QuestionResult, ...] = field(default_factory=tuple)

    @property
    def passes(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def hit_rate(self) -> float:
        return self.passes / len(self.results) if self.results else 0.0

    @property
    def leaks(self) -> tuple[QuestionResult, ...]:
        return tuple(r for r in self.results if r.status == "leak")


def load_questions(path: Path) -> tuple[Question, ...]:
    raw = json.loads(Path(path).read_text())["questions"]
    return tuple(
        Question(
            id=item["id"],
            query=item["query"],
            context=item["context"],
            expected_doc_keys=tuple(item["expected_doc_keys"]),
            forbidden_doc_keys=tuple(item.get("forbidden_doc_keys", ())),
            expected_section=item.get("expected_section"),
            rationale=item.get("rationale", ""),
        )
        for item in raw
    )


def check(corpus: Corpus, questions: tuple[Question, ...]) -> None:
    """Fail before measuring if the answer key points at anything that is not there.

    A question naming a document the corpus dropped would otherwise score a permanent
    miss and read as a retrieval problem.
    """
    keys = {doc.key for doc in corpus.documents}
    for question in questions:
        if question.context not in corpus.contexts:
            raise ValueError(f"{question.id}: no context named {question.context!r}")
        for key in question.expected_doc_keys + question.forbidden_doc_keys:
            if key not in keys:
                raise ValueError(f"{question.id}: no document keyed {key!r}")


def score(question: Question, returned: list[str]) -> QuestionResult:
    """Grade one question's returned document keys, nearest hit first.

    A forbidden document outranks every other outcome: retrieval returning something
    the asker may not see is a different kind of failure from ranking badly, and
    averaging the two into one number would hide it.
    """
    leaked = [key for key in question.forbidden_doc_keys if key in returned]
    if leaked:
        return QuestionResult(question.id, "leak", False, tuple(returned))
    if question.expected_doc_keys:
        found = [key for key in question.expected_doc_keys if key in returned]
        if found:
            rank = min(returned.index(key) for key in found) + 1
            return QuestionResult(question.id, "hit", True, tuple(returned), rank)
        return QuestionResult(question.id, "miss", False, tuple(returned))
    # Nothing expected. With a forbidden list the rule is "never this document", which
    # the leak check above already settled; without one, the rule is "nothing at all".
    if question.forbidden_doc_keys:
        return QuestionResult(question.id, "no-leak", True, tuple(returned))
    if returned:
        return QuestionResult(question.id, "false-positive", False, tuple(returned))
    return QuestionResult(question.id, "empty-ok", True, ())


def run(
    corpus: Corpus,
    questions: tuple[Question, ...],
    *,
    settings: ChunkSettings,
    k: int = DEFAULT_K,
    min_cosine: float = DEFAULT_MIN_COSINE,
) -> Run:
    """Seed the corpus at `settings`, ask every question, and grade the answers."""
    embedder = LexicalEmbedder()
    adapter = InMemoryStorageAdapter()
    seeded = seed(adapter, corpus, embedder=embedder, settings=settings)
    by_id = {doc.id: key for key, doc in seeded.docs.items()}
    results = []
    for question in questions:
        hits = adapter.search_chunks(
            query=question.query,
            embedding=embedder.embed_query(question.query),
            visibility=corpus.contexts[question.context],
            k=k,
            min_cosine=min_cosine,
        )
        # Several chunks of one document may rank; the answer key is per document, so
        # collapse to first appearance and keep ranking order.
        returned: list[str] = []
        for hit in hits:
            key = by_id[hit.doc_id]
            if key not in returned:
                returned.append(key)
        results.append(score(question, returned))
    return Run(_config(seeded, embedder, k, min_cosine, len(questions)), tuple(results))


def _config(seeded: Seeded, embedder, k: int, min_cosine: float, questions: int) -> dict:
    return {
        "target_tokens": seeded.settings.target_tokens,
        "overlap_tokens": seeded.settings.overlap_tokens,
        "chunker_version": CHUNKER_VERSION,
        "counting": "chars/4",
        "embedder": embedder.model,
        "k": k,
        "min_cosine": min_cosine,
        "documents": len(seeded.docs),
        "chunks": seeded.total_chunks,
        "questions": questions,
    }


def format_run(result: Run, *, verbose: bool = True) -> str:
    lines = [_config_line(result.config)]
    if verbose:
        width = max(len(r.id) for r in result.results)
        for item in result.results:
            rank = f"@{item.rank}" if item.rank else "  "
            returned = ", ".join(item.returned) or "(nothing)"
            mark = "ok  " if item.passed else "FAIL"
            lines.append(f"  {mark} {item.status:<14} {item.id:<{width}} {rank:>3}  {returned}")
    k = result.config["k"]
    lines.append(f"  hit@{k} = {result.passes}/{len(result.results)} = {result.hit_rate:.2f}")
    if result.leaks:
        lines.append(
            f"  LEAKS: {len(result.leaks)} question(s) returned a forbidden document: "
            + ", ".join(item.id for item in result.leaks)
        )
    return "\n".join(lines)


def _config_line(config: dict) -> str:
    return (
        f"target_tokens={config['target_tokens']} overlap_tokens={config['overlap_tokens']} "
        f"chunker_v{config['chunker_version']} counting={config['counting']} "
        f"embedder={config['embedder']} k={config['k']} min_cosine={config['min_cosine']} "
        f"-> {config['chunks']} chunks over {config['documents']} documents"
    )


def format_comparison(runs: list[Run]) -> str:
    """Several configurations side by side. Equal scores are a valid outcome: the rows
    are printed as measured, and nothing here looks for a difference."""
    lines = [
        "",
        "comparison",
        f"  {'target':>6} {'overlap':>7} {'chunks':>6} {'hit rate':>9}  leaks",
    ]
    for item in runs:
        lines.append(
            f"  {item.config['target_tokens']:>6} {item.config['overlap_tokens']:>7} "
            f"{item.config['chunks']:>6} {item.hit_rate:>9.2f}  {len(item.leaks)}"
        )
    rates = {item.hit_rate for item in runs}
    if len(runs) > 1 and len(rates) == 1:
        lines.append("  every configuration scored the same; that is a result, not an error")
    return "\n".join(lines)


def calibrate(corpus: Corpus, questions: tuple[Question, ...], settings: ChunkSettings) -> str:
    """Report how well cosine separates correct documents from incorrect ones.

    docs/DEPLOYMENT.md points here for calibrating `search_min_cosine`. This is the
    measurement behind that: the best cosine against the expected document, against the
    best wrong document, and what each candidate floor would admit.
    """
    embedder = LexicalEmbedder()
    adapter = InMemoryStorageAdapter()
    seeded = seed(adapter, corpus, embedder=embedder, settings=settings)
    cosine = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731

    rows = []
    for question in questions:
        if not question.expected_doc_keys:
            continue
        query = embedder.embed_query(question.query).vector
        wanted = {seeded.docs[key].id for key in question.expected_doc_keys}
        best = lambda ids: max(  # noqa: E731
            cosine(query, chunk.embedding)
            for doc_id in ids
            for chunk in adapter.list_doc_chunks(doc_id)
        )
        rows.append(
            (question.id, best(wanted), best({d.id for d in seeded.docs.values()} - wanted))
        )

    width = max(len(row[0]) for row in rows)
    lines = [_config_line(_config(seeded, embedder, DEFAULT_K, DEFAULT_MIN_COSINE, len(rows))), ""]
    lines.append(f"  {'question':<{width}} {'correct':>8} {'best wrong':>11}  margin")
    for name, right, wrong in rows:
        lines.append(f"  {name:<{width}} {right:>8.4f} {wrong:>11.4f}  {right - wrong:+.4f}")
    lines.append("")
    lines.append(f"  {'floor':>5} {'correct kept':>13} {'wrong admitted':>15}")
    for floor in (0.30, 0.25, 0.20, 0.15, 0.10, 0.05):
        kept = sum(1 for _, right, _ in rows if right >= floor)
        noise = sum(1 for _, _, wrong in rows if wrong >= floor)
        lines.append(f"  {floor:>5.2f} {kept:>9}/{len(rows)} {noise:>11}/{len(rows)}")
    lines.append("")
    lines.append(
        "  A floor is only useful where these separate. Read the margin column before\n"
        "  moving search_min_cosine: overlapping distributions mean no floor helps."
    )
    return "\n".join(lines)


def _settings(value: str) -> ChunkSettings:
    try:
        target, overlap = (int(part) for part in value.split(":", 1))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected TARGET:OVERLAP, got {value!r}") from None
    return ChunkSettings(target_tokens=target, overlap_tokens=overlap)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.eval.harness",
        description="Measure retrieval quality against the fixed question set.",
    )
    parser.add_argument("mode", nargs="?", choices=("run", "calibrate"), default="run")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument(
        "--config",
        type=_settings,
        action="append",
        metavar="TARGET:OVERLAP",
        help="chunk configuration to measure; repeat to compare several (default 500:50)",
    )
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--min-cosine", type=float, default=DEFAULT_MIN_COSINE)
    parser.add_argument("--json", action="store_true", help="emit results as JSON")
    parser.add_argument("--quiet", action="store_true", help="scores only, no per-question rows")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    corpus = load_corpus(args.corpus)
    questions = load_questions(args.questions)
    check(corpus, questions)
    configs = args.config or [ChunkSettings()]

    if args.mode == "calibrate":
        print(calibrate(corpus, questions, configs[0]))
        return 0

    runs = [
        run(corpus, questions, settings=settings, k=args.k, min_cosine=args.min_cosine)
        for settings in configs
    ]
    if args.json:
        print(json.dumps([asdict(item) | {"hit_rate": item.hit_rate} for item in runs], indent=2))
        return 0
    for item in runs:
        print(format_run(item, verbose=not args.quiet))
    if len(runs) > 1:
        print(format_comparison(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
