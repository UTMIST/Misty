# Retrieval quality evaluation

A local harness that asks retrieval a fixed set of questions and reports how often the
right document came back. Its purpose is **comparison**: the corpus, the questions, the
embedder and the relevance floor are all held fixed, so a difference between two runs is
attributable to the one setting that changed.

It is not a benchmark, it does not run in CI as a gate, and nothing in the service
imports it. It exists so that a chunking or model change can be judged on its own,
before retrieval reaches conversational answers and every problem starts arriving as
"the bot gave a bad answer".

## Quick start

```bash
cd services/documentation-system
uv run python -m src.eval.harness                                  # default 500:50
uv run python -m src.eval.harness --config 500:50 --config 250:25  # compare two
uv run python -m src.eval.harness calibrate                        # relevance floor
```

No network, no API key, no database: the corpus is seeded into the in-memory adapter and
embedded locally. A run takes about a second.

Useful flags: `--k` (default 5), `--min-cosine` (default 0.3, production's
`SEARCH_MIN_COSINE`), `--quiet` for scores only, `--json` for machine-readable output,
and `--corpus` / `--questions` to point at other fixtures.

## What the number means

`hit@k` is the share of questions that passed. A question passes when the document that
should answer it appeared anywhere in the top `k` results, under the authorization
context that asked it.

Every result prints the configuration that produced it:

```
target_tokens=500 overlap_tokens=50 chunker_v1 counting=chars/4
embedder=fake-lexical-v1 k=5 min_cosine=0.3 -> 16 chunks over 8 documents
```

A score without its configuration compares to nothing, so the two are never printed
apart. `chunker_v1` matters in particular: scores either side of a `CHUNKER_VERSION`
bump were produced by different splitters and are not comparable.

**Equal scores are a valid outcome.** If two chunk sizes score the same, the setting did
not matter for this corpus and these questions. Report that. The harness never looks for
a difference, and neither should anyone reading it.

## Seeding the corpus

`docs/fixtures/eval-corpus.json` lists the documents; their text lives in
`docs/fixtures/eval-corpus/` as Markdown, one file each. Every document is invented —
see that directory's README for why none of it is copied from Drive.

Seeding goes through the real indexing path, `src/indexing.py:index_doc`, rather than a
private reimplementation. That matters: `index_doc` prefixes each chunk with its
document title and heading path before embedding (`embedding_input`), and a harness that
skipped that step would be scoring a pipeline the service does not run.

Grants are seeded with each document, and `contexts` names the askers the question set
refers to — people with their team entitlements, and channels with only source-derived
team entitlement. Retrieval filters by context *before* ranking, so a question's context
is part of its answer.

## The question set

`docs/fixtures/eval-questions.json`. One entry per question:

| field | meaning |
| --- | --- |
| `id` | stable name, used in output |
| `query` | what is asked |
| `context` | which asker from the corpus `contexts` |
| `expected_doc_keys` | documents that would answer it; empty means "nothing should come back" |
| `forbidden_doc_keys` | documents that must **never** come back |
| `expected_section` | not scored; tells you *which* chunk should have matched when one does not |
| `rationale` | why the question is in the set |

`rationale` is not decoration. Six months on, nobody can tell a boundary probe from a
baseline question, and a set whose purpose has been forgotten stops being maintained.

The set deliberately spans: baseline lookups, answers buried mid-document, procedures
and tables that straddle chunk boundaries, bare acronyms and exact terms, near-miss
competitors where several documents mention the query but only one answers it,
authorization pairs, and questions whose correct answer is no results at all.

## Reading a result

| status | meaning |
| --- | --- |
| `hit` | an expected document came back, at the printed rank |
| `miss` | none did |
| `leak` | a **forbidden** document came back |
| `no-leak` | nothing forbidden came back (an authorization question passing) |
| `empty-ok` | nothing was expected and nothing came back |
| `false-positive` | nothing was expected but something came back |

A `leak` is reported separately from the hit rate and should never be tolerated. It
means retrieval returned a document the asker is not entitled to see, which is an
authorization failure, not a ranking one — a different kind of problem that a single
averaged score would bury. `tests/test_eval_harness.py` asserts the checked-in fixture
leaks nothing.

An authorization question asserts `forbidden_doc_keys` rather than expecting no results.
The asker may legitimately see other documents that match loosely, so "never returned
this one" is the real rule and "returned nothing" is the wrong bar.

## Calibrating the relevance floor

`calibrate` reports, for every question, the best cosine against the correct document
and against the best wrong one, then what each candidate floor would admit.

Run against the current fixture it shows the two distributions **overlapping across
their whole range** — some correct documents score below some incorrect ones. No floor
separates them cleanly, so moving `SEARCH_MIN_COSINE` trades recall against precision
with no sweet spot to find. Read the margin column before changing it.

That result is specific to the lexical stand-in described below and should not be
carried over to a decision about production's floor without re-measuring against real
embeddings.

## Limitations — read before quoting a number

**The embedder is a stand-in, not a model.** `src/eval/embedding.py` scores vocabulary
overlap by feature hashing: a query and a chunk are similar to the extent they use the
same words. Real embeddings place "reimbursement" near "get paid back"; this does not,
and scores them zero. #178 requires automated tests to avoid paid provider calls, and a
score that depended on a network round trip would not be reproducible anyway.

Consequences worth holding onto:

- **No number here is a claim about production retrieval quality.** It is a claim about
  how a setting behaves relative to another setting.
- **Questions must share vocabulary with their documents.** A well-chunked corpus still
  misses a question phrased entirely in synonyms, and that is the embedder, not the
  chunking.
- **The hybrid fusion gets weak exercise.** `search_chunks` fuses a vector branch and a
  keyword branch, and under a lexical embedder both rank on nearly the same signal.
- **The keyword branch is strict.** `keyword_density` scores zero unless *every* query
  term appears in the chunk, so a natural-language question contributes nothing there
  and rests entirely on cosine.
- **The relevance floor can dominate.** At `min_cosine=0.3`, smaller chunks score higher
  largely because they concentrate query terms above the threshold; at a floor that
  stops filtering, chunk size barely moves the score on this corpus. Sweep the floor
  before concluding anything about chunk size.

The corpus is also eight invented documents. It imitates the shape of the Drive content
#208 sampled, which is what the harness measures, but it is not a sample of the real
corpus and results do not transfer to it without re-measuring.

## Editing the fixture

Change the corpus and the question set together. A question naming a document you
deleted becomes a permanent miss that reads as a retrieval problem, which is why
`check()` refuses to measure when the answer key points at anything absent, and why
`tests/test_eval_harness.py` validates the pairing on every test run.

When a change alters what the embedder produces for the same text, bump `MODEL` in
`src/eval/embedding.py`. Scores either side of that bump are not comparable, and the
label recorded in each result is what says so.
