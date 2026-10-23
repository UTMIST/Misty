# EigenAI infrastructure notes

These are working notes for the EigenAI project's shared compute, kept loosely and
updated as things change. They are not a policy document and the arrangements here have
changed more than once during the year, so treat anything undated as current only until
it visibly is not.

EigenAI runs on a mix of departmental credits and a donated allocation that renews
quarterly. The donated allocation covers training runs; the departmental credits cover
everything else, including inference for demos and the nightly evaluation job. When the
donated allocation is exhausted before a quarter ends, training requests fall back to
departmental credits, which is the usual reason a run that worked last month suddenly
needs approval this month.

Compute requests are approved by the EigenAI project lead, currently Priya Raman, who
also maintains the allocation spreadsheet. A request over 200 GPU-hours additionally
needs the departmental infrastructure lead to sign off, and in practice that second
signature is the slow part, so large runs should be raised a week ahead rather than the
day before. Requests under 20 GPU-hours do not need approval at all and are logged
after the fact.

Storage is a separate arrangement. The project holds two terabytes of shared object
storage, and that figure has not changed since the project started. Datasets staged for
a training run should be removed within two weeks of the run finishing; the nightly job
reports anything older and the project lead clears it manually when the report gets
long. There is no automatic deletion, which has caused the allocation to fill twice.

The nightly evaluation job runs at 02:00 and writes its summary to the project channel.
A failed run is retried once automatically. A second failure is left alone so the
morning has something to look at rather than a clean retry that hides the cause.
