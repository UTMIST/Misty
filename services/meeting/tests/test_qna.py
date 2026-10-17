"""Offline Q&A window, grounding and failure tests."""

import json

import pytest

from src.contracts import Segment
from src.pipeline.llm_client import LlmUnavailable
from src.pipeline.qna import (
    MAX_QNA_TRANSCRIPT_CHARS,
    QnaInvalidResponse,
    QnaWindowTooLarge,
    answer_question,
    select_window,
)


def seg(start_ms, text="deadline Friday", speaker="Alice"):
    return Segment(start_ms=start_ms, speaker=speaker, text=text)


class FakeLlm:
    def __init__(self, result=None, error=None):
        self.result = result or json.dumps(
            {"supported": True, "answer": "The deadline is Friday.", "source_ids": ["s1"]}
        )
        self.error = error
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def test_window_uses_elapsed_time_and_inclusive_boundaries_in_order():
    window = select_window([seg(1_200_001), seg(300_000), seg(299_999), seg(1_200_000)], 1_200_000)
    assert [s.start_ms for s in window.segments] == [300_000, 1_200_000]
    assert (window.start_ms, window.end_ms, window.shortened) == (300_000, 1_200_000, False)


def test_short_meeting_and_silence():
    assert select_window([seg(0), seg(123_000)], 123_000).start_ms == 0
    assert select_window([seg(0)], 1_200_000).segments == []


def test_window_size_counts_ids_and_labels_and_keeps_complete_newest_suffix():
    segments = [seg(i, "x" * 22_000) for i in range(3)]
    window = select_window(segments, 3)
    assert window.shortened
    assert [s.start_ms for s in window.segments] == [1, 2]
    assert window.start_ms == 1
    assert len(window.transcript) <= MAX_QNA_TRANSCRIPT_CHARS
    assert "[s1]" in window.transcript and "[s2]" in window.transcript
    base = select_window([seg(0, "x")], 1)
    overhead = len(base.transcript) - 1
    assert not select_window([seg(0, "x" * (MAX_QNA_TRANSCRIPT_CHARS - overhead))], 1).shortened
    with pytest.raises(QnaWindowTooLarge):
        select_window([seg(0, "x" * (MAX_QNA_TRANSCRIPT_CHARS - overhead + 1))], 1)


def test_answer_uses_only_window_false_thinking_and_snapshot_references():
    llm = FakeLlm()
    result = answer_question([seg(0, "OLD SECRET"), seg(400_000)], 1_200_000, "When?", llm)
    assert result.answer == "The deadline is Friday."
    assert result.references[0].model_dump() == {"speaker": "Alice", "start_ms": 400_000}
    call = llm.calls[0]
    assert call["thinking"] is False and call["max_tokens"] == 1024
    assert "OLD SECRET" not in call["messages"][0]["content"]
    assert "When?" in call["messages"][0]["content"]


def test_empty_window_and_unsupported_answers():
    llm = FakeLlm()
    empty = answer_question([seg(0)], 1_200_000, "When?", llm)
    assert "No finalized speech" in empty.answer
    assert not llm.calls and not empty.references
    llm.result = json.dumps({"supported": False, "answer": "made up", "source_ids": []})
    unsupported = answer_question([seg(300_000)], 1_200_000, "When?", llm)
    assert unsupported.answer == "Not discussed in the transcript available for 05:00–20:00."
    assert unsupported.references == []


@pytest.mark.parametrize(
    "completion",
    [
        "not json",
        "[]",
        "{}",
        {"supported": True, "answer": "Friday", "source_ids": ["invented"]},
        {"supported": True, "answer": "Friday", "source_ids": []},
        {"supported": True, "answer": "Friday", "source_ids": ["s1", "s1"]},
        {"supported": True, "answer": "Friday", "source_ids": ["s1"] * 4},
        {"supported": "false", "answer": "Friday", "source_ids": []},
        {"supported": True, "answer": " ", "source_ids": ["s1"]},
        {"supported": False, "answer": "", "source_ids": ["invented"]},
    ],
)
def test_invalid_answers_and_citations_are_rejected(completion):
    raw = json.dumps(completion) if isinstance(completion, (dict, list)) else completion
    with pytest.raises(QnaInvalidResponse):
        answer_question([seg(0)], 1, "When?", FakeLlm(raw))


def test_provider_failures_are_not_converted_to_unsupported_answers():
    with pytest.raises(LlmUnavailable):
        answer_question([seg(0)], 1, "When?", FakeLlm(error=LlmUnavailable("private detail")))
