from src.pipeline.llm_client import LlmUnavailable
from src.pipeline.minutes import summarize_minutes


class FakeLlm:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, **kw):
        self.calls.append(kw)
        return self.content


class FailingLlm:
    def chat(self, **kw):
        raise LlmUnavailable("boom")


def test_parses_json():
    llm = FakeLlm('{"summary":"s","decisions":["d"],"action_items":["a"]}')
    m = summarize_minutes("[00:00] x: hi", llm)
    assert m.summary == "s" and m.decisions == ["d"] and m.action_items == ["a"]
    assert "hi" in llm.calls[0]["messages"][-1]["content"]


def test_parses_llm_title():
    llm = FakeLlm('{"title":"Q3 Roadmap Sync","summary":"s","decisions":[],"action_items":[]}')
    assert summarize_minutes("x", llm).title == "Q3 Roadmap Sync"


def test_title_defaults_empty_when_absent_or_non_string():
    assert summarize_minutes("x", FakeLlm('{"summary":"s"}')).title == ""
    assert summarize_minutes("x", FakeLlm('{"title":123,"summary":"s"}')).title == ""


def test_tolerates_fenced_json():
    llm = FakeLlm('```json\n{"summary":"s","decisions":[],"action_items":[]}\n```')
    assert summarize_minutes("x", llm).summary == "s"


def test_fallback_on_bad_json():
    llm = FakeLlm("not json")
    m = summarize_minutes("x", llm)
    assert m.summary == "not json" and m.decisions == [] and m.action_items == []


def test_null_or_scalar_list_fields_fall_back_to_empty():
    llm = FakeLlm('{"summary":"s","decisions":null,"action_items":5}')
    m = summarize_minutes("x", llm)
    assert m.summary == "s"
    assert m.decisions == []
    assert m.action_items == []


def test_degrades_to_placeholder_minutes_on_llm_outage():
    # A transient LLM outage must not lose the whole meeting (transcript/PDF/audio
    # still get delivered) — summarize_minutes degrades instead of propagating.
    m = summarize_minutes("x", FailingLlm())
    assert m.summary == "(minutes unavailable: LLM service error)"
    assert m.decisions == []
    assert m.action_items == []
    assert m.title == ""  # no title on outage -> PDF uses its fallback


# --- truncated LLM responses -------------------------------------------------
#
# The model is capped at max_tokens, so a long meeting can have its JSON cut off
# mid-write. Before this was handled, `_extract_json` returned None and the
# fallback put the RAW TRUNCATED JSON in the summary field -- so the PDF showed
# a wall of JSON ending mid-word, with no decisions or action items at all.

_TRUNCATED_MID_ARRAY = (
    '{"title": "Weekly Sync: Q3 Roadmap", '
    '"summary": "The team reviewed the roadmap and agreed on hiring priorities.", '
    '"decisions": ["Ship the beta on Friday", "Defer the redesign to Q4"], '
    '"action_items": ["Ethan to draft the spec", "Priya to review the bud'
)

_TRUNCATED_MID_STRING = (
    '{"title": "Weekly Sync", '
    '"summary": "The team reviewed the roadmap and agreed on hiring priorities '
    "for the coming quarter, then moved on to discussing the budg"
)


def test_salvages_a_response_truncated_mid_array():
    """Everything the model DID finish must survive: title, summary, all
    decisions, and the action items it completed."""
    m = summarize_minutes("x", FakeLlm(_TRUNCATED_MID_ARRAY))

    assert m.title == "Weekly Sync: Q3 Roadmap"
    assert m.summary == "The team reviewed the roadmap and agreed on hiring priorities."
    assert m.decisions == ["Ship the beta on Friday", "Defer the redesign to Q4"]
    assert m.action_items == ["Ethan to draft the spec"]


def test_salvages_a_response_truncated_mid_string():
    """Cut off inside the summary itself: keep the title, and keep the partial
    summary as readable prose rather than losing it."""
    m = summarize_minutes("x", FakeLlm(_TRUNCATED_MID_STRING))

    assert m.title == "Weekly Sync"
    assert m.summary.startswith("The team reviewed the roadmap")
    assert m.decisions == []


def test_never_surfaces_raw_json_as_the_summary():
    """Whatever happens, the summary is prose for a human -- never a dump of the
    model's raw JSON, which is what a reader used to see in the PDF."""
    for content in (_TRUNCATED_MID_ARRAY, _TRUNCATED_MID_STRING, '{"summary": "ok"'):
        summary = summarize_minutes("x", FakeLlm(content)).summary
        assert not summary.lstrip().startswith("{"), f"raw JSON leaked into summary: {summary[:60]}"
        assert '"action_items"' not in summary
        assert '"decisions"' not in summary


def test_unparseable_prose_still_becomes_the_summary():
    """A model that ignores the JSON instruction and writes prose should still
    produce usable minutes -- existing behaviour, pinned so the salvage path
    doesn't regress it."""
    m = summarize_minutes("x", FakeLlm("We agreed to ship on Friday."))
    assert m.summary == "We agreed to ship on Friday."


def test_asks_for_enough_output_tokens_for_a_long_meeting():
    """1500 was low enough that a busy meeting's minutes were routinely cut off;
    the llm service allows far more."""
    llm = FakeLlm('{"summary":"s"}')
    summarize_minutes("x", llm)
    assert llm.calls[0]["max_tokens"] >= 4000


def test_never_surfaces_raw_json_when_the_response_is_fenced_or_prefixed():
    """The startswith('{') guard misses the two commonest wrappers. A truncated
    response never closes its fence, so the fragment reaches the reader with
    backticks (or a lead-in sentence) in front of it -- the same wall of JSON in
    the PDF the guard exists to prevent."""
    for content in (
        '```json\n{"title":"T","summar',
        '```\n{"title":"T","summar',
        'Here are the minutes:\n{"title":"T","summar',
    ):
        summary = summarize_minutes("x", FakeLlm(content)).summary
        assert '"summar' not in summary, f"raw JSON leaked: {summary[:70]!r}"
        assert "```" not in summary


def test_salvage_is_bounded_on_a_large_unparseable_response():
    """Every checkpoint costs a json.loads over a growing prefix, so a fragment
    with no top-level summary walks all of them. The LLM response is untrusted
    input from a network service and max_tokens is a caller-chosen constant, so
    the cost must not be quadratic in the response size."""
    import time

    payload = '{"a":[' + ",".join('{"k":"v"}' for _ in range(4000)) + ',{"k":"v'
    started = time.perf_counter()
    summarize_minutes("x", FakeLlm(payload))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"salvage took {elapsed:.2f}s on a {len(payload)}-char response"


def test_lossy_salvage_is_logged(caplog):
    """A salvaged response silently drops whatever came after the truncation.
    The PDF then looks authoritative while a decision the model actually made is
    missing, so operators need a signal that it happened."""
    truncated = '{"title":"T","summary":"S","action_items":["a","bb'
    with caplog.at_level("WARNING"):
        m = summarize_minutes("x", FakeLlm(truncated))

    assert m.action_items == ["a"]
    assert any("salvage" in r.message.lower() for r in caplog.records), (
        f"no warning logged; records: {[r.message for r in caplog.records]}"
    )


def test_keeps_salvaged_fields_when_the_summary_never_arrived():
    """Salvage can return a dict with real content but no `summary` -- the model
    got cut off before finishing that value. Discarding the whole thing throws
    away a title and decisions it genuinely produced.

    Before salvage existed this could not happen: `_extract_json` returned
    either None or a fully valid object, so requiring `summary` was safe. It
    isn't any more."""
    raw = '{"title": "Q3 Roadmap Sync", "decisions": ["Ship Friday"], "summar'
    m = summarize_minutes("x", FakeLlm(raw))

    assert m.title == "Q3 Roadmap Sync"
    assert m.decisions == ["Ship Friday"]
    # No summary was recovered, so it must say so -- not carry raw JSON, and not
    # silently pretend the meeting had no decisions.
    assert "unavailable" in m.summary.lower()
    assert "{" not in m.summary


def test_prose_that_merely_mentions_json_is_kept_as_the_summary():
    """The raw-JSON guard must not fire on a meeting that *talks about* JSON.
    This org's meetings are about prompts, schemas and APIs, so a summary
    quoting `{"role":"assistant"}` is ordinary content, not a malformed
    response -- replacing it with a placeholder loses a perfectly good summary."""
    prose = (
        'We compared {"role":"assistant"} and {"role":"user"} examples, then agreed to ship Friday.'
    )
    m = summarize_minutes("x", FakeLlm(prose))

    assert m.summary == prose


def test_cap_never_hides_the_last_recoverable_top_level_value():
    """Attempts are capped to bound cost, but nested junk must not eat the whole
    budget: a top-level `summary` that is plainly recoverable has to survive
    however much malformed nesting follows it."""
    payload = '{"summary":"s","action_items":[{' + ",".join(f'"k{i}"' for i in range(70))

    m = summarize_minutes("x", FakeLlm(payload))

    assert m.summary == "s"


def test_non_string_list_entries_are_dropped_not_stringified():
    """A salvaged nested object used to be rendered with `str()`, putting a
    Python dict repr into the PDF as if it were a real action item."""
    m = summarize_minutes("x", FakeLlm('{"summary":"x","decisions":[{"a":"b","c":"d'))

    assert m.decisions == [], f"got a stringified object: {m.decisions}"


def test_an_enormous_transcript_is_bounded_before_the_llm_call():
    """A multi-hour meeting can overflow the model's context, which fails the
    call outright -- and LlmUnavailable renders "(minutes unavailable)", strictly
    worse than minutes drawn from most of the meeting."""
    from src.pipeline.minutes import MAX_TRANSCRIPT_CHARS

    llm = FakeLlm('{"summary":"s"}')
    huge = "[00:00] alice: hello\n" * 40_000
    assert len(huge) > MAX_TRANSCRIPT_CHARS

    summarize_minutes(huge, llm)

    sent = llm.calls[0]["messages"][-1]["content"]
    assert len(sent) < MAX_TRANSCRIPT_CHARS + 500
    assert "omitted" in sent
