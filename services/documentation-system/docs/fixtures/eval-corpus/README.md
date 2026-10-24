# Evaluation corpus (#178)

Every document here is **invented**. It imitates the shape of UTMIST's catalogued
Google Docs -- the acronyms, headings, list and procedure structure, and rough length
that #208 recorded in `docs/ARCHITECTURE.md` -- without reproducing any real document's
text. #208's corpus boundary says to experiment on approved content without committing
private document text, and this fixture is checked in permanently, so nothing here is
copied from Drive.

Facts (dates, dollar figures, names) are placeholders. They exist so a question has a
findable answer, not because they are true.

Replacing a document with a real one is fine when that document is already public.
Otherwise rewrite the stand-in to match the real document's *shape*: that is what the
harness measures. Keep `docs/fixtures/eval-questions.json` in step with any edit --
a question whose answer you deleted silently becomes a permanent miss.
