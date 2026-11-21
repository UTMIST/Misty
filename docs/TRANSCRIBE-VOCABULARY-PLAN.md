# Chunk-derived Transcribe vocabulary: implementation plan

This document proposes future implementation work. It does not enable a vocabulary,
change the live transcription path, or authorize an automatic production rollout.
The goal is to improve recognition of UTMIST-specific names, acronyms and project
terms spoken in meetings by deriving candidate words from the existing RAG chunks.
Maintain **one shared club-wide vocabulary** for all meetings, refreshed weekly;
there is no per-team or per-channel vocabulary selection.

## Intended flow

1. Extract and count words from eligible chunk text in the documentation-system.
2. Remove English dictionary matches, common technical terms, and numbers.
3. Rank the remaining unique terms by frequency and select the top 20%.
4. Run weekly. Compare the selected terms with the last successful candidate
   snapshot and use a low-cost model, such as Luna if available to Misty, only
   for newly selected terms without an already approved spelling.
5. Validate and review the candidate table, create an AWS custom vocabulary,
   and use the one ready club-wide vocabulary in all meeting transcription streams.

The 20% selection is a **candidate generation rule**, not a guarantee that all
candidates are suitable, fit in AWS's limit, or improve transcription. Validate
with real speech before using it broadly. A model's guess is not a verified
pronunciation. An export, model output or accepted spelling is not evidence that
Amazon Transcribe will recognize a term correctly.

## Current system and ownership

`documentation-system` owns `doc_chunks` and its document and grant lifecycle;
`meeting` streams each speaker to Amazon Transcribe and `llm` owns model provider
calls. The current stream is started in
[`transcribe.py`](../services/meeting/src/stt/transcribe.py); it does not pass a
vocabulary. [Meeting recording](MEETING-RECORDING.md) describes the existing audio
path. Do not introduce a new service, cross-service database access, stored audio,
or a second live transcription pass.

| Component | Proposed responsibility |
| --- | --- |
| documentation-system | Generate a bounded, reproducible candidate report from permitted chunk snapshots and own its provenance. |
| llm | Offer a configured inexpensive model through the existing service boundary, if the provider supports it; no assumption that the Codex Luna model is available to this service. |
| operator | Review candidate safety, spelling and spoken forms; run the controlled AWS staging trial. |
| meeting | Use the configured, ready club-wide vocabulary by name at stream creation and preserve the current behavior when disabled. |

## 1. Extract and rank

- Use active, eligible document chunks for the served embedding model, once per
  document and chunk ordinal. Multiple embedding model sets must not multiply
  counts. Pin or record the source snapshot and model used for a run; exclude
  retired documents and stale or unauthorized source content according to the
  catalog's current policy. Define overlap handling and text normalization in the
  implementation, and record them: chunk overlap can inflate frequency.
- Tokenize words and keep the original form alongside a normalized matching key.
  Aggregate counts case-insensitively, preserving source evidence for authorized
  review. Counts represent occurrences in indexed text, not unique documents or
  how often a term is spoken in meetings. Order by descending count, then by a
  deterministic normalized key for ties.
- Apply a versioned, pinned English dictionary with inflections and common
  Canadian, US and UK spellings. Match whole normalized tokens, not arbitrary
  suffixes, so a project name is not removed merely because it contains an
  English word. Dictionary coverage is imperfect: `situational` was missed in
  the exploratory word lists. Test against a broader source, record its version,
  license and checksum, and allow review of removals and an explicit exception list.
- Subtract a pinned public common-technology dictionary, such as the CSpell
  software and computing acronym lists, including terms like `http` and `www`.
  Record source/version/license; its broad entries can also remove relevant
  jargon, so make exclusions reviewable and permit explicit exceptions. Do not
  assume every dictionary entry is already recognized by AWS.
- Remove standalone numeric tokens and numeric ordinals such as `2nd` or `10th`;
  keep alphanumeric names like `Scene3` for review. Document tokenization of
  decimals and punctuation, rather than silently treating every digit-containing
  token as a number.
- After these filters, select `ceil(0.20 * N)` of the **distinct remaining terms**,
  or zero if N is zero. Ties are resolved by the deterministic order above. Keep
  counts and ranks in a restricted review report; do not commit source text or
  production candidate exports. Compare selected and removed terms so important
  omissions and garbage can be found before publishing anything.

The exploratory CSV contained 26,524 distinct tokens. English and technical
filters and numeric-only removal left 11,812; its top 20% is 2,363 terms and
accounts for approximately 80.64% of those remaining tokens' reported hits.
This is an exploratory snapshot, not a production baseline; adding the ordinal
and broader dictionary rules will change it. These counts may include overlap,
repeated source material, names and web fragments.

## Weekly refresh and change detection

Run the pipeline once a week in a single scheduled worker, outside the meeting
and audio path. Pick the day/time and scheduler during implementation; start with
an operator-triggered staging run to establish the first snapshot. Prevent
overlapping runs. Deduplicate triggers for the same scheduled period and use provider idempotency
where supported. After an ambiguous external failure, reconcile existing AWS
resources before retrying; an interrupted model call may still have incurred cost. A failed run does not advance
the successful checkpoint; retry with bounded backoff and preserve completed,
validated work so a retry calls the model only for unresolved terms.

Each run recomputes the full filtered ranking from its recorded source snapshot.
Compare normalized keys in its top 20% to the last successful snapshot, not just
new raw database words: a word may enter or leave the top 20% as counts change.
Record additions and removals separately. If the selected set and its approved
renderings are unchanged, record a no-change outcome and make **no model or AWS
call**. Changed hit counts alone do not require a model call or AWS update.

For additions, reuse a previously approved spelling for the same normalized key
and compatible vocabulary rules where available; otherwise send only new or
unresolved candidates to the model. An existing word is not reprocessed merely
because its count changed. Flag a change in source spelling, dictionary/model
rules or an operator correction for review rather than silently reusing a stale
rendering. Removals need no model call: build a revised table without them after
review. A term leaving the top 20% or failing the new eligibility check must not
be newly published, and any removal from the club-wide vocabulary must follow the
source eligibility and revocation policy below. Dictionary, tokenizer or scope version changes
require recomputing the snapshot and reviewing their effect, not an automatic
full-model rerun.

The successful candidate snapshot, reusable accepted spellings, input/rule
versions, and proposed additions/removals need durable restricted storage owned
by documentation-system, with atomic checkpoint updates. Keep pending generation
and review separate from the approved AWS vocabulary: if review, model calls,
size validation or AWS creation fail, retain the prior approved version and make
the next run retry the unresolved change. Only publish a new AWS version after
review and when the resulting table differs. The weekly schedule detects changes;
it does not bypass privacy review or the manual staging approval gate. Once a
version is approved and ready, new meetings can use it; ongoing meetings retain
their chosen version. Define monitoring for failed, stalled and no-change runs,
with counts and version IDs rather than source terms in logs.

## 2. Generate and validate spoken forms

Use the existing `llm` boundary for bounded batches. Configure and verify a cheap
model supported by the deployed provider and region; Luna is an example, not an
assumed API/model ID. Pin the model/version, prompt, decoding settings and input
snapshot in the run record. Apply a per-run cost and token budget with bounded
retry and concurrency; interrupted runs must be resumable without regenerating
accepted results. Do not send raw chunks to the model: use only approved candidate
tokens and minimal necessary context, subject to the provider's data policy.

Ask for structured output mapping each input key to a proposed `Phrase`,
`DisplayAs`, and uncertainty/reason. Treat text as untrusted input, not instructions.
Validate that exactly the requested input identifiers return, preserve source
provenance, reject hallucinated or duplicated entries, and constrain characters,
lengths and format. Invalid, ambiguous or low-confidence outputs go to review;
they are never silently invented or automatically activated. A word may be said
as letters or a word, and lowercase chunks do not establish capitalization or
pronunciation. Review examples including UTMIST, GNN, EigenAI, Scene3, and
homophones with a speaker or recording. Do not change the canonical display form
solely because the model guesses it.

## 3. Scope, privacy and AWS format

A derived word can expose a private project, a person's name, an identifier or
other protected information even without its surrounding text. The chunk store
has documents with different audiences. Because this vocabulary is shared across
the club, derive it only from an explicitly approved corpus whose terms are safe
for club-wide use and external processing; do not simply use all readable chunks. AWS explicitly says not
to put confidential information, personal information or protected health
information into a custom vocabulary. Restrict the pilot to an explicitly approved,
non-sensitive corpus and review terms before any external model or AWS upload.
Authorization to read a document is not by itself authorization to disclose its
terms to a model provider or to every meeting. Fail closed when source scope is
unknown or source eligibility is withdrawn: stop using the affected club-wide
vocabulary for new meetings until a verified replacement is ready. Define how to
identify affected versions, rebuild and remove AWS resources before rollout. Limit review artifacts, logs and storage accordingly.

Use the [AWS table format](https://docs.aws.amazon.com/transcribe/latest/dg/custom-vocabulary-create-table.html):
`Phrase`, `SoundsLike`, `IPA`, `DisplayAs`. Leave `SoundsLike` and `IPA` empty:
AWS currently ignores their values. `Phrase` has hyphen-separated words,
individually spoken letters separated by periods, and spelled-out numbers;
`DisplayAs` carries the desired spelling. For example, `G.N.N.` displays as
`GNN`, and `Scene-three` can display as `Scene3` if that is how people say it.
Validate the current language-specific character set and every row, including
acronyms, plural forms, digit spelling and the 256-character entry limit.

A [custom vocabulary file](https://docs.aws.amazon.com/transcribe/latest/dg/custom-vocabulary.html)
is limited to 50 KB and must match the stream's language and AWS Region. Measure
actual UTF-8 bytes of the final four-column table; the exploratory `term,hits` CSV
is not an AWS table. Upload through the supported S3/API workflow as a text file,
create a versioned vocabulary and wait for `READY`. Failures must not replace the
last known-good version when it is still authorized. A standard stream selects a single vocabulary name;
splitting candidates across multiple files does not apply all of them to the same
stream. If the top 20% does not fit, keep it as the candidate pool and review an
explicit smaller selection for the pilot; do not silently truncate or claim that
three files are equivalent to one combined vocabulary.

## 4. Meeting integration and rollout

Start with one operator-approved club-wide staging vocabulary and a feature-disabled
default. Use that same vocabulary for every meeting; do not select by team or channel.
Verify the installed streaming SDK supports the vocabulary-name parameter, its
language and region, and actual AWS readiness before wiring it to
`start_stream_transcription`. Freeze the chosen vocabulary for a meeting and use
it for every speaker and any reopened stream. Creation and model calls happen
outside the audio path. Never buffer or replay live audio to prepare a vocabulary.

If no vocabulary is configured or approved, stream as today. A failed preparation
or deployment must leave the previous known-good version available only if it
remains authorized; otherwise use no vocabulary. Define an explicit operator
rollback to an eligible version or to no vocabulary. Do not silently retry an
active audio stream in a way that drops or duplicates audio. Define how AWS
connection errors are surfaced and whether a no-vocabulary retry is safe before
implementation. An update affects new meetings only; no mid-meeting vocabulary
swap. Restrict who can generate, approve and deploy a vocabulary, record version
and status without logging document text, and define its refresh and removal
schedule during implementation.

## 5. Evaluation and acceptance

Before rollout, obtain representative recordings and independently corrected
reference transcripts with appropriate consent. On the **same held-out audio**,
compare no vocabulary, small reviewed lists (for example 100 and 300 terms), and
a larger eligible list where it fits. Keep meeting/topic selection and scoring
rules fixed. Separate development data from the held-out evaluation and avoid
building a list using the test transcript. Measure overall word error rate,
recognition of a fixed set of important terms, false insertions/substitutions,
and operational cost/latency. Include recordings where vocabulary terms are
absent and similar-sounding ordinary words occur. Report counts and uncertainty;
a small sample cannot establish a reliable gain. Approve numeric thresholds and
budget before the trial, and retain the baseline if results do not meet them.

AWS's [2019 article](https://aws.amazon.com/blogs/machine-learning/build-a-custom-vocabulary-to-enhance-speech-to-text-transcription-accuracy-with-amazon-transcribe/)
recommended targeting lists and avoiding more than 300 words. This is historical
guidance, not a present hard limit or a quantified degradation curve. A
[2023 speech-recognition study](https://www.isca-archive.org/interspeech_2023/huang23d_interspeech.pdf)
found that more distractors reduced the gain and increased other-word errors for
its models; it does not measure AWS. Test our own system rather than infer an
accuracy rate from that paper.

## Proposed implementation tickets

1. **Weekly vocabulary pipeline:** define the permitted corpus, snapshot and
   overlap rules; filter, rank and take the top 20%; compare against the last
   successful run; suggest spellings for unresolved additions through `llm`;
   review and validate the one club-wide AWS vocabulary, including its byte size.
   Acceptance: fixtures cover dictionary inflections, common technical terms,
   numbers, ordinals, compounds, repeated embedding sets, ties and empty input.
   No-change and count-only runs call neither the model nor AWS; removals need no
   model call; retries preserve completed work and do not publish incomplete
   changes. Reject unsafe, unapproved, invalid or oversized outputs and retain a
   still-authorized prior version on failure. External providers are faked in tests.
2. **Transcribe integration and evaluation:** configure the single approved
   club-wide vocabulary in meeting, use it for each speaker and reopened stream,
   and compare fixed held-out recordings with and without it. Acceptance: fake
   tests cover disabled and enabled operation, multiple speakers, reconnects,
   failures and rollback; existing audio, timestamp and stop behavior is preserved.
   Staging results include baseline, smaller and larger candidate lists, absent-term
   cases, costs and an explicit decision against agreed thresholds before rollout.

Open decisions: permitted corpus and reviewer; dictionary source/version; provider
availability and data policy; weekly schedule and durable checkpoint location; pronunciation
review process; staging sample, accuracy thresholds and spending budget; and the
policy for revocation and selection when a 20% candidate set exceeds 50 KB.
