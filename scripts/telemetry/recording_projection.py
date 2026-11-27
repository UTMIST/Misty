"""Materialize recording attempts and component spans from validated events."""

import json
from pathlib import Path
import subprocess
import tempfile

from store import utc

READER = Path(__file__).resolve().parents[2] / "discord-bot/scripts/recording-report.js"


def materialize(raw, snap, cutoff):
    snap.executescript("""
        create table attempts(trace_id text, traffic text, cohort text, started integer,
          mode text, outcome text, stop text, drain text, save text, link text, pdf text, latency_ms integer,last_seen text,primary key(traffic,trace_id));
        create index cohort on attempts(traffic,cohort);
        create table spans(trace_id text,span_id text,parent text,link_trace text,link_span text,
          service text, operation text,traffic text,start text,finish text,result text,elapsed_ms integer,
          primary key(service,traffic,operation,trace_id,span_id));
        create index component on spans(traffic,service,operation,start);
    """)
    # A newly observed boot makes a start in an older process inconclusive.
    # Legacy recording lines lack boot IDs. Only classify them as open if
    # a bot boot in this environment is known to predate capture.
    boots = dict(
        raw.execute(
            """select service,max(json_extract(payload,'$.process_started_at'))
        from events where ts>=? and json_extract(payload,'$.process_started_at') is not null
        group by service""",
            (cutoff,),
        )
    )
    for (traffic,) in raw.execute(
        "select distinct traffic from events where service='discord-bot' and ts>=?",
        (cutoff,),
    ):
        project_recordings(raw, snap, traffic, cutoff, boots.get("discord-bot"))
    # Timed spans have one stable operation. Starts and ends may arrive in any order.
    # Missing ends keep their start and unknown outcome. Count retries as separate spans.
    rows = raw.execute(
        """select trace_id,span_id,min(parent_span_id),min(link_trace_id),min(link_span_id),
          min(service),min(operation),min(traffic),
          coalesce(min(case when event_name='start' then ts end),min(ts)),
          max(case when event_name='end' then ts end),
          coalesce(min(case when event_name='end' then result end),'unknown'),
          max(case when event_name='end' then elapsed_ms end),
          max(case when event_name='start' then json_extract(payload,'$.process_started_at') end)
          from events where ts>=? and json_extract(payload,'$.schema')='misty.telemetry.v1'
          group by service,traffic,operation,trace_id,span_id""",
        (cutoff,),
    )
    for row in rows:
        span = list(row[:-1])
        if span[9] is None and row[-1] and boots.get(span[5]) == row[-1]:
            span[10] = "open"
        snap.execute("insert into spans values(?,?,?,?,?,?,?,?,?,?,?,?)", span)


def project_recordings(raw, snap, traffic, cutoff, bot_boot):
    with tempfile.TemporaryFile() as events, tempfile.TemporaryFile() as reports:
        # Raw messages already passed the collector allowlist. Replay the bot
        # schema through the same reducer as the single-trace investigation CLI.
        for (payload,) in raw.execute(
            """select payload from events where service='discord-bot'
                and traffic=? and ts>=? and json_extract(payload,'$.schema')='misty.recording.v1'
                order by trace_id""",
            (traffic, cutoff),
        ):
            events.write(payload.encode() + b"\n")
        events.seek(0)
        subprocess.run(
            ["node", str(READER), "--grouped"],
            stdin=events,
            stdout=reports,
            check=True,
        )
        reports.seek(0)
        for line in reports:
            r = json.loads(line)
            row = raw.execute(
                """select
               min(case when event_name like 'record.%requested' then ts end) requested,
               min(case when event_name='capture.started' then ts end) started,
               min(case when event_name in ('capture.stop_requested','capture.empty_channel') then ts end) stopped,
               min(case when event_name='pdf.posted' then ts end) pdf,
               min(case when event_name in ('link.acknowledged','link.posted') then ts end) link,max(ts) last_seen
               from events where trace_id=? and service='discord-bot' and traffic=? and ts>=?""",
                (r["trace_id"], traffic, cutoff),
            ).fetchone()
            cohort = row["started"] or row["requested"]
            if not cohort:  # orphan recovery cannot create a second recording
                continue
            latency = None
            if r["outcome"] == "complete" and row["stopped"] and row["pdf"]:
                ack = (
                    max(row["pdf"], row["link"] or row["pdf"])
                    if r["mode"] == "record.saved_requested"
                    else row["pdf"]
                )
                diff = round((utc(ack) - utc(row["stopped"])).total_seconds() * 1000)
                latency = diff if diff >= 0 else None
            outcome = r["outcome"]
            if (
                outcome == "unconfirmed"
                and row["started"]
                and r["stop"] == "unknown"
                and set(r["events"])
                <= {
                    "record.saved_requested",
                    "record.pdf_requested",
                    "capture.started",
                }
                and bot_boot
                and row["started"] >= bot_boot
            ):
                outcome = "open"
            snap.execute(
                "insert into attempts values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["trace_id"],
                    traffic,
                    cohort,
                    bool(row["started"]),
                    r["mode"],
                    outcome,
                    r["stop"],
                    r["drain"],
                    r["save"],
                    r["link"],
                    r["pdf"],
                    latency,
                    row["last_seen"],
                ),
            )
