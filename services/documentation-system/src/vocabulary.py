"""Rank a complete catalog snapshot and record private weekly review candidates."""

import argparse
import json
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from functools import partial
from hashlib import sha256
from itertools import groupby
from pathlib import Path

import regex
from sqlalchemy.exc import SQLAlchemyError

from contracts.embedder import RESOLVED_EMBED_MODELS
from contracts.vocabulary import (
    VocabularyCandidate,
    VocabularyChunk,
    VocabularyRunBusy,
    VocabularySnapshot,
)
from src.api.deps import get_storage
from src.config import get_settings

_ENGLISH = [
    Path(f"/usr/share/dict/{locale}-english-large")
    for locale in ("american", "british", "canadian")
]
_TECHNICAL = [
    Path("/usr/share/misty-vocabulary") / name
    for name in ("softwareTerms.txt", "computing-acronyms.txt")
]
# Letters/digits with combining marks and internal hyphens or apostrophes;
# decimal/group separators are internal only when surrounded by digits.
_TOKEN = regex.compile(
    r"[\p{L}\p{N}][\p{L}\p{N}\p{M}]*(?:(?:[-'’]|(?<=\d)[.,](?=\d))[\p{L}\p{N}][\p{L}\p{N}\p{M}]*)*"
)
_NUMBER = regex.compile(r"\p{N}+(?:[.,-]\p{N}+)*(?:st|nd|rd|th)?")
# Prevent an unbroken ID or encoded blob spanning chunks from becoming a term.
_MAX_TERM_CHARS = 256
_RULE_VERSION = "1"


def _key(word: str) -> str:
    return unicodedata.normalize("NFKC", word).replace("’", "'").casefold()


def _load_list(paths: list[Path]) -> tuple[set[str], str]:
    words = set()
    for path in paths:
        data = path.read_text(encoding="utf-8")
        entries = {
            _key(line)
            for line in map(str.strip, data.splitlines())
            if line and not line.startswith("#")
        }
        if not entries:
            raise ValueError("empty word list")
        words.update(entries)
    # Pin the effective list independent of file names, order or duplicate lines.
    return words, sha256("\n".join(sorted(words)).encode()).hexdigest()


def _covered_text(chunks: Iterable[VocabularyChunk]) -> Iterator[str]:
    parts: list[str] = []
    end = 0
    for chunk in chunks:
        if chunk.start > end and parts:
            yield "".join(parts)
            parts.clear()
        if chunk.end > end:
            parts.append(chunk.text[max(0, end - chunk.start) :])
            end = chunk.end
    if parts:
        yield "".join(parts)


def rank_snapshot(
    chunks: Iterable[VocabularyChunk],
    previous: set[str],
    *,
    excluded: set[str],
    rule_versions: dict[str, str],
) -> VocabularySnapshot:
    digest = sha256()
    forms: dict[str, Counter[str]] = defaultdict(Counter)

    def fingerprint() -> Iterator[VocabularyChunk]:
        for chunk in chunks:
            digest.update(json.dumps((str(chunk.doc_id), *chunk[1:])).encode())
            digest.update(b"\n")
            yield chunk

    for _, document in groupby(fingerprint(), key=lambda chunk: chunk.doc_id):
        for text in _covered_text(document):
            for match in _TOKEN.finditer(text):
                spelling = match[0]
                if len(spelling) > _MAX_TERM_CHARS:
                    continue
                key = _key(spelling)
                # NFKC can turn numeric characters such as Ⅷ into letters.
                if (
                    len(key) > _MAX_TERM_CHARS
                    or key in excluded
                    or _NUMBER.fullmatch(key)
                    or _NUMBER.fullmatch(spelling.casefold())
                ):
                    continue
                forms[key][spelling] += 1

    counts = {key: sum(spellings.values()) for key, spellings in forms.items()}
    top = sorted(counts, key=lambda key: (-counts[key], key))[: (len(counts) + 4) // 5]
    selected = set(top)
    return VocabularySnapshot(
        snapshot_id=digest.hexdigest(),
        rule_versions=rule_versions,
        candidates=[
            VocabularyCandidate(
                key=key,
                spelling=min(forms[key], key=lambda form: (-forms[key][form], form)),
                count=counts[key],
                rank=rank,
            )
            for rank, key in enumerate(top, 1)
        ],
        additions=sorted(selected - previous),
        removals=sorted(previous - selected),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--english", type=Path, action="append", help="Override English lists")
    parser.add_argument("--technical", type=Path, action="append", help="Override technical lists")
    parser.add_argument("--exceptions", type=Path, help="Words exempt from dictionary filtering")
    args = parser.parse_args(argv)
    try:
        english, english_hash = _load_list(args.english or _ENGLISH)
        technical, technical_hash = _load_list(args.technical or _TECHNICAL)
        exceptions, exceptions_hash = _load_list([args.exceptions] if args.exceptions else [])
    except (OSError, ValueError):
        print("vocabulary status=failed reason=word_lists")
        return 1

    today = datetime.now(timezone.utc).date()
    week = today - timedelta(days=today.weekday())
    build = partial(
        rank_snapshot,
        excluded=(english | technical) - exceptions,
        rule_versions={
            "ranking": _RULE_VERSION,
            "unicode": unicodedata.unidata_version,
            "regex": regex.__version__,
            "english_sha256": english_hash,
            "technical_sha256": technical_hash,
            "exceptions_sha256": exceptions_hash,
        },
    )
    try:
        snapshot = get_storage().record_vocabulary_week(
            week, RESOLVED_EMBED_MODELS[get_settings().search_embed_model], build
        )
    except VocabularyRunBusy:
        print(f"vocabulary week={week} status=busy")
        return 1
    except Exception as exc:
        # Driver errors can contain bound candidate JSON; never print their messages.
        reason = "database" if isinstance(exc, SQLAlchemyError) else "job"
        print(f"vocabulary week={week} status=failed reason={reason}")
        return 1
    if snapshot is None:
        print(f"vocabulary week={week} status=already_completed")
    else:
        print(
            f"vocabulary week={week} status=completed candidates={len(snapshot.candidates)} "
            f"additions={len(snapshot.additions)} removals={len(snapshot.removals)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
