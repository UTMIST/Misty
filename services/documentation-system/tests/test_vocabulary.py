from functools import partial
from uuid import UUID

from contracts.vocabulary import VocabularyChunk
from src import vocabulary


def _rank(rows, previous=frozenset()):
    return vocabulary.rank_snapshot(
        iter(rows),
        set(previous),
        excluded={"situational", "colour", "http", "www"},
        rule_versions={},
    )


def test_ranks_distinct_filtered_terms_with_overlap_casefold_and_ties():
    doc = UUID(int=1)
    text = (
        "GNN gnn GNN EigenAI eigenai Scene3 scene3 Foo-Bar O’Quux Zürich Zeta "
        "situational colour HTTP WWW 42 2nd 1,234th 10.25 ¹²"
    )
    split = text.index("gnn") + 2
    rows = [
        VocabularyChunk(doc, 0, 0, split, text[:split]),
        VocabularyChunk(doc, 1, 4, split, text[4:split]),
        VocabularyChunk(doc, 2, split - 1, len(text), text[split - 1 :]),
    ]
    snapshot = _rank(rows, {"obsolete", "gnn"})
    assert [(c.key, c.spelling, c.count, c.rank) for c in snapshot.candidates] == [
        ("gnn", "GNN", 3, 1),
        ("eigenai", "EigenAI", 2, 2),
    ]
    assert snapshot.additions == ["eigenai"]
    assert snapshot.removals == ["obsolete"]
    assert _rank(rows, {"gnn", "eigenai"}).snapshot_id == snapshot.snapshot_id


def test_numeric_only_unicode_and_ordinals_are_filtered():
    text = "Ⅷ Ⅷ ½ ½ １２ 1,234th 2ND ² 12.45 20-30 1️⃣ 1️⃣ 1’000 1’000 Scene3"
    snapshot = _rank([VocabularyChunk(UUID(int=1), 0, 0, len(text), text)])
    assert [(c.key, c.count) for c in snapshot.candidates] == [("scene3", 1)]


def test_long_unbroken_source_is_not_saved_as_a_candidate():
    blob = "X" * 260
    text = f"{blob} {blob} Scene3"
    doc = UUID(int=1)
    rows = [
        VocabularyChunk(doc, 0, 0, 130, text[:130]),
        VocabularyChunk(doc, 1, 130, len(text), text[130:]),
    ]
    snapshot = _rank(rows)
    assert [(c.key, c.count) for c in snapshot.candidates] == [("scene3", 1)]


def test_offsets_join_adjacent_chunks_but_not_across_gaps_or_documents():
    one, two = UUID(int=1), UUID(int=2)
    rows = [
        VocabularyChunk(one, 0, 0, 3, "Eig"),
        VocabularyChunk(one, 1, 3, 7, "enAI"),
        VocabularyChunk(one, 2, 9, 16, "EigenAI"),
        VocabularyChunk(two, 0, 0, 7, "EigenAI"),
        VocabularyChunk(two, 1, 8, 13, "QuuxX"),
    ]
    assert [(c.key, c.count) for c in _rank(rows).candidates] == [("eigenai", 3)]
    assert _rank([]).candidates == []


def test_cli_uses_served_model_and_logs_counts_only(tmp_path, monkeypatch, capsys):
    english = tmp_path / "english.txt"
    tech = tmp_path / "technical.txt"
    allow = tmp_path / "allow.txt"
    english.write_text("# comment\nSituational\n", encoding="utf-8")
    tech.write_text("HTTP\n", encoding="utf-8")
    allow.write_text("http\n", encoding="utf-8")

    class Store:
        def record_vocabulary_week(self, week, model, build):
            assert week.weekday() == 0
            assert model == "text-embedding-3-small"
            text = "situational HTTP http VeryPrivateTerm"
            row = VocabularyChunk(UUID(int=1), 0, 0, len(text), text)
            result = build([row], set())
            assert [c.key for c in result.candidates] == ["http"]
            assert result.rule_versions["english_sha256"]
            return result

    monkeypatch.setattr(vocabulary, "get_storage", Store)
    run = partial(
        vocabulary.main,
        ["--english", str(english), "--technical", str(tech), "--exceptions", str(allow)],
    )
    assert run() == 0
    output = capsys.readouterr().out
    assert "status=completed candidates=1 additions=1 removals=0" in output
    assert "http" not in output.lower()
    assert "VeryPrivateTerm" not in output

    def fail():
        raise RuntimeError("VeryPrivateTerm")

    monkeypatch.setattr(vocabulary, "get_storage", fail)
    assert run() == 1
    output = capsys.readouterr().out
    assert "status=failed" in output
    assert "VeryPrivateTerm" not in output
