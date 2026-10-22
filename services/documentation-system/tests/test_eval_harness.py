"""Offline tests for the retrieval-quality harness.

Everything here runs on fixtures and the deterministic lexical embedder: automated tests
must avoid paid provider calls, and a score that depended on a network round trip would
not be reproducible anyway.
"""

import json
from pathlib import Path

import pytest

from contracts.embedder import EmbedderRejected
from contracts.retrieval import QueryEmbedding
from contracts.visibility import Actor, Channel
from src.chunking import ChunkSettings
from src.eval import harness
from src.eval.corpus import doc_id_for, load_corpus, seed
from src.eval.embedding import DIMENSIONS, MODEL, LexicalEmbedder
from src.eval.harness import (
    Question,
    Run,
    calibration_rows,
    check,
    load_questions,
    run,
    score,
)
from src.storage.in_memory import InMemoryStorageAdapter

PERSON = "11111111-1111-1111-1111-111111111111"
TEAM = "22222222-2222-2222-2222-222222222222"

LONG = "\n\n".join(
    ["# CPSIF funding guide"]
    + [f"## Section {i}\n\nCPSIF expense claim guidance for members. " * 20 for i in range(4)]
)


def write_corpus(tmp_path: Path, documents=None) -> Path:
    (tmp_path / "cpsif.md").write_text(LONG)
    (tmp_path / "orchid.md").write_text("# Orchids\n\nOrchid growing guidance. " * 20)
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps(
            {
                "contexts": {
                    "alice": {"kind": "person", "person_id": PERSON, "team_ids": [TEAM]},
                    "members": {"kind": "channel", "source_team_ids": [TEAM]},
                },
                "documents": documents
                if documents is not None
                else [
                    {
                        "key": "cpsif",
                        "title": "CPSIF funding guide",
                        "url": "https://x.test/cpsif",
                        "text_file": "cpsif.md",
                        "grants": [["team", TEAM, "google_drive"]],
                    },
                    {
                        "key": "orchid",
                        "title": "Orchids",
                        "url": "https://x.test/orchid",
                        "text_file": "orchid.md",
                        "grants": [["person", PERSON, "google_drive"]],
                    },
                ],
            }
        )
    )
    return path


def questions(*items) -> tuple[Question, ...]:
    return tuple(items)


# --- the embedder -----------------------------------------------------------------


def test_embeds_to_the_width_the_storage_contract_fixes():
    embedding = LexicalEmbedder().embed_query("CPSIF expense claim")
    assert isinstance(embedding, QueryEmbedding)
    assert len(embedding.vector) == DIMENSIONS
    assert embedding.model == MODEL


def test_same_text_embeds_identically():
    # Not an implementation detail: a score that moved between runs would make every
    # comparison the harness exists to support meaningless.
    first, second = LexicalEmbedder(), LexicalEmbedder()
    assert first.embed_text("CPSIF claim") == second.embed_text("CPSIF claim")


def test_cosine_tracks_vocabulary_overlap():
    embedder = LexicalEmbedder()
    query = embedder.embed_query("CPSIF expense claim").vector
    cosine = lambda text: sum(  # noqa: E731
        a * b for a, b in zip(query, embedder.embed_text(text))
    )
    assert cosine("Submitting a CPSIF expense claim") > cosine("CPSIF funding overview")
    assert cosine("Orchid growing guide") == pytest.approx(0.0)


def test_text_with_no_terms_is_rejected_rather_than_embedded_as_zero():
    # An all-zero vector fails QueryEmbedding's validation downstream; failing here
    # names the broken question instead of surfacing as a mysterious validation error.
    with pytest.raises(EmbedderRejected):
        LexicalEmbedder().embed_text("!!! ???")


def test_batch_keeps_input_order_and_reports_its_model():
    embedder = LexicalEmbedder()
    batch = embedder.embed(["CPSIF claim", "Orchid growing"])
    assert batch.model == MODEL and batch.dimensions == DIMENSIONS
    assert batch.vectors[0] == embedder.embed_text("CPSIF claim")
    assert batch.vectors[1] == embedder.embed_text("Orchid growing")


# --- corpus loading and seeding ----------------------------------------------------


def test_loads_text_from_a_file_beside_the_corpus(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path))
    assert {doc.key for doc in corpus.documents} == {"cpsif", "orchid"}
    assert corpus.documents[0].text == LONG


def test_contexts_load_as_people_and_channels(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path))
    assert isinstance(corpus.contexts["alice"], Actor)
    assert isinstance(corpus.contexts["members"], Channel)


def test_a_document_must_give_exactly_one_of_text_and_text_file(tmp_path):
    path = write_corpus(
        tmp_path,
        documents=[
            {
                "key": "both",
                "title": "Both",
                "url": "https://x.test/both",
                "text": "inline",
                "text_file": "cpsif.md",
                "grants": [],
            }
        ],
    )
    with pytest.raises(ValueError, match="exactly one of text or text_file"):
        load_corpus(path)


def test_chunk_settings_change_how_many_chunks_the_corpus_seeds(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path))
    embedder = LexicalEmbedder()
    counts = []
    for target in (500, 125):
        adapter = InMemoryStorageAdapter()
        seeded = seed(
            adapter,
            corpus,
            embedder=embedder,
            settings=ChunkSettings(target_tokens=target, overlap_tokens=target // 10),
        )
        counts.append(seeded.total_chunks)
    assert counts[1] > counts[0]


def test_seeding_fails_loudly_when_a_document_cannot_be_indexed(tmp_path):
    # Whitespace-only text indexes to nothing. Left unreported it would return no
    # results and be scored as a retrieval miss.
    (tmp_path / "blank.md").write_text("   \n\n  ")
    path = write_corpus(
        tmp_path,
        documents=[
            {
                "key": "blank",
                "title": "Blank",
                "url": "https://x.test/blank",
                "text_file": "blank.md",
                "grants": [["team", TEAM, "google_drive"]],
            }
        ],
    )
    with pytest.raises(RuntimeError, match="did not index"):
        seed(
            InMemoryStorageAdapter(),
            load_corpus(path),
            embedder=LexicalEmbedder(),
            settings=ChunkSettings(),
        )


# --- scoring ------------------------------------------------------------------------


def question(**values) -> Question:
    return Question(
        **{
            "id": "q",
            "query": "CPSIF",
            "context": "alice",
            "expected_doc_keys": (),
            **values,
        }
    )


def test_an_expected_document_anywhere_in_the_results_is_a_hit():
    result = score(question(expected_doc_keys=("cpsif",)), ["orchid", "cpsif"])
    assert (result.status, result.passed, result.rank) == ("hit", True, 2)


def test_an_expected_document_that_never_appears_is_a_miss():
    result = score(question(expected_doc_keys=("cpsif",)), ["orchid"])
    assert (result.status, result.passed, result.rank) == ("miss", False, None)


def test_a_forbidden_document_is_a_leak_not_a_miss():
    # Scored separately on purpose: returning a document the asker may not see is an
    # authorization failure, and averaging it into a hit rate would bury it.
    result = score(
        question(expected_doc_keys=(), forbidden_doc_keys=("transition",)),
        ["orchid", "transition"],
    )
    assert (result.status, result.passed) == ("leak", False)


def test_a_forbidden_document_outranks_an_otherwise_correct_answer():
    result = score(
        question(expected_doc_keys=("cpsif",), forbidden_doc_keys=("transition",)),
        ["cpsif", "transition"],
    )
    assert result.status == "leak"


def test_other_documents_may_come_back_while_a_forbidden_one_does_not():
    # The authorization question's rule is "never this document", not "nothing at all":
    # the asker can legitimately see other documents that match loosely.
    result = score(question(expected_doc_keys=(), forbidden_doc_keys=("transition",)), ["orchid"])
    assert (result.status, result.passed) == ("no-leak", True)


def test_expecting_nothing_passes_only_when_nothing_comes_back():
    assert score(question(), []).status == "empty-ok"
    assert score(question(), ["orchid"]).status == "false-positive"
    assert score(question(), ["orchid"]).passed is False


# --- running --------------------------------------------------------------------------


def test_a_run_answers_every_question_and_records_its_configuration(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path))
    asked = questions(
        question(id="finds-it", query="CPSIF expense claim", expected_doc_keys=("cpsif",)),
        question(id="absent", query="parking policy Bahen", expected_doc_keys=()),
    )
    result = run(corpus, asked, settings=ChunkSettings(), k=5, min_cosine=0.05)
    assert [item.id for item in result.results] == ["finds-it", "absent"]
    assert result.config["target_tokens"] == 500
    assert result.config["overlap_tokens"] == 50
    assert result.config["embedder"] == MODEL
    assert result.config["k"] == 5
    assert result.config["min_cosine"] == 0.05
    assert result.config["chunks"] > 0


def test_retrieval_stays_inside_the_asking_context(tmp_path):
    # The orchid document is granted to one person. A channel holds only team
    # entitlement, so it must never reach a personal grant.
    corpus = load_corpus(write_corpus(tmp_path))
    asked = questions(
        question(id="personal", query="orchid growing", context="alice"),
        question(id="channel", query="orchid growing", context="members"),
    )
    result = run(corpus, asked, settings=ChunkSettings(), k=5, min_cosine=0.05)
    by_id = {item.id: item for item in result.results}
    assert "orchid" in by_id["personal"].returned
    assert "orchid" not in by_id["channel"].returned


def test_two_chunk_sizes_produce_comparable_results(tmp_path):
    # The harness must measure the difference honestly, and equal scores are a valid
    # outcome. So this asserts both runs are well formed and
    # labelled with the configuration that produced them -- never that they differ.
    corpus = load_corpus(write_corpus(tmp_path))
    asked = questions(
        question(id="finds-it", query="CPSIF expense claim", expected_doc_keys=("cpsif",))
    )
    runs = [
        run(
            corpus,
            asked,
            settings=ChunkSettings(target_tokens=t, overlap_tokens=t // 10),
            k=5,
            min_cosine=0.05,
        )
        for t in (500, 125)
    ]
    assert {item.config["target_tokens"] for item in runs} == {500, 125}
    for item in runs:
        assert len(item.results) == len(asked)
        assert 0.0 <= item.hit_rate <= 1.0


def test_hit_rate_counts_passes_over_questions():
    results = (
        harness.QuestionResult("a", "hit", True, ("x",), 1),
        harness.QuestionResult("b", "miss", False, ()),
    )
    assert Run({}, results).hit_rate == pytest.approx(0.5)
    assert Run({}, ()).hit_rate == 0.0


def test_leaks_are_listed_separately_from_the_hit_rate():
    results = (harness.QuestionResult("a", "leak", False, ("secret",)),)
    assert [item.id for item in Run({}, results).leaks] == ["a"]


# --- the answer key must point at documents that exist --------------------------------


def test_a_question_naming_an_unknown_document_fails_before_measuring(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path))
    with pytest.raises(ValueError, match="no document keyed"):
        check(corpus, questions(question(expected_doc_keys=("gone",))))


def test_a_question_naming_an_unknown_context_fails_before_measuring(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path))
    with pytest.raises(ValueError, match="no context named"):
        check(corpus, questions(question(context="nobody")))


# --- the checked-in fixture ------------------------------------------------------------


def test_the_checked_in_question_set_matches_the_checked_in_corpus():
    # Guards the pairing: editing one file and not the other turns a question into a
    # permanent miss that reads as a retrieval problem.
    check(load_corpus(harness.DEFAULT_CORPUS), load_questions(harness.DEFAULT_QUESTIONS))


def test_every_corpus_document_has_its_text_on_disk():
    for document in load_corpus(harness.DEFAULT_CORPUS).documents:
        assert document.text.strip(), f"{document.key} has no text"


def test_the_checked_in_fixture_runs_and_leaks_nothing():
    corpus = load_corpus(harness.DEFAULT_CORPUS)
    asked = load_questions(harness.DEFAULT_QUESTIONS)
    result = run(corpus, asked, settings=ChunkSettings())
    assert len(result.results) == len(asked)
    # A leak is an authorization failure, not a low score. It must never be acceptable.
    assert result.leaks == ()


# --- the CLI ----------------------------------------------------------------------------


def test_config_argument_parses_target_and_overlap():
    args = harness.build_parser().parse_args(["--config", "250:25"])
    assert args.config == [ChunkSettings(target_tokens=250, overlap_tokens=25)]


@pytest.mark.parametrize("value", ["250", "250:", "a:b", "250:25:5"])
def test_a_malformed_config_argument_is_rejected(value):
    with pytest.raises(SystemExit):
        harness.build_parser().parse_args(["--config", value])


def test_main_runs_the_checked_in_fixture_and_prints_the_configuration(capsys):
    assert harness.main(["--quiet"]) == 0
    out = capsys.readouterr().out
    assert "target_tokens=500" in out and "min_cosine=0.3" in out
    assert "hit@5" in out


def test_main_compares_several_configurations(capsys):
    assert harness.main(["--config", "500:50", "--config", "250:25", "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "comparison" in out
    assert "target_tokens=500" in out and "target_tokens=250" in out


def test_main_emits_json_carrying_the_configuration(capsys):
    assert harness.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["config"]["chunker_version"] >= 1
    assert 0.0 <= payload[0]["hit_rate"] <= 1.0


def test_calibrate_reports_the_separation_between_right_and_wrong_documents(capsys):
    assert harness.main(["calibrate"]) == 0
    out = capsys.readouterr().out
    assert "margin" in out and "correct kept" in out


# --- stable identity ------------------------------------------------------------------


def test_a_document_seeds_under_the_same_id_every_run(tmp_path):
    # Retrieval breaks ranking ties with the doc id, so a generated id would reorder
    # equally scored documents between runs -- at small k, a hit one run and a miss the
    # next, which reads as a chunking difference and is not one.
    corpus = load_corpus(write_corpus(tmp_path))
    seen = []
    for _ in range(2):
        seeded = seed(
            InMemoryStorageAdapter(),
            corpus,
            embedder=LexicalEmbedder(),
            settings=ChunkSettings(),
        )
        seen.append({key: doc.id for key, doc in seeded.docs.items()})
    assert seen[0] == seen[1]
    assert seen[0]["cpsif"] == doc_id_for("cpsif")


def test_the_adapter_honours_a_supplied_document_id():
    adapter = InMemoryStorageAdapter()
    wanted = doc_id_for("fixed")
    doc = adapter.create_doc(
        url="https://x.test/fixed",
        url_normalized="https://x.test/fixed",
        source_id="gdocs",
        title="Fixed",
        description=None,
        owning_team_id=None,
        owning_team_label=None,
        owning_person_id=None,
        owning_person_label=None,
        content_snapshot=None,
        fetched_at=None,
        tags=[],
        actor="test",
        doc_id=wanted,
    )
    assert doc.id == wanted


def test_identical_runs_produce_identical_results(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path))
    asked = questions(question(id="tie", query="CPSIF expense claim", expected_doc_keys=("cpsif",)))
    first, second = (
        run(corpus, asked, settings=ChunkSettings(), k=1, min_cosine=0.05) for _ in range(2)
    )
    assert [item.returned for item in first.results] == [item.returned for item in second.results]


# --- calibration ----------------------------------------------------------------------


def test_calibration_ignores_documents_the_asker_cannot_retrieve(tmp_path):
    # The orchid document is granted to one person, and its text is the question's own
    # vocabulary, so it would be the strongest competitor if visibility were skipped.
    # A channel cannot reach a personal grant, so for that asker it is not competition.
    corpus = load_corpus(write_corpus(tmp_path))
    asked = questions(
        question(
            id="as-person", query="orchid growing", context="alice", expected_doc_keys=("cpsif",)
        ),
        question(
            id="as-channel", query="orchid growing", context="members", expected_doc_keys=("cpsif",)
        ),
    )
    _, rows = calibration_rows(corpus, asked, ChunkSettings())
    wrong = {name: competitor for name, _, competitor in rows}
    assert wrong["as-person"] > wrong["as-channel"]


def test_calibration_reports_a_row_per_answerable_question(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path))
    asked = questions(
        question(id="answerable", query="CPSIF", expected_doc_keys=("cpsif",)),
        question(id="expects-nothing", query="CPSIF", expected_doc_keys=()),
    )
    _, rows = calibration_rows(corpus, asked, ChunkSettings())
    assert [name for name, _, _ in rows] == ["answerable"]
