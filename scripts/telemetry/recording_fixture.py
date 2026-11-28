"""Generate versioned synthetic Railway windows, including replay and reordering."""

import argparse
from datetime import timedelta
import json
from pathlib import Path

from store import LEGACY, SHARED, iso, utc

FIXTURE = json.loads(
    (Path(__file__).with_name("fixtures") / "recording-v1.json").read_text()
)


def examples(anchor):
    for n, case in enumerate(FIXTURE["cases"], 1):
        trace = f"{n:032x}"

        def row(data, offset):
            ts = anchor + timedelta(milliseconds=offset + n * 1000)
            record = dict(trace_id=trace, ts=iso(ts), dropped_before=0, **data)
            return {
                "timestamp": iso(ts + timedelta(milliseconds=37)),
                "message": json.dumps(record, separators=(",", ":")),
            }

        marks = FIXTURE.get(case.get("base"), []) + case["extra"]
        later = case.get("later", [])
        for i, name in enumerate(marks + later):
            stop = name.startswith("stop.")
            recovery = i >= len(marks)
            yield (
                "discord-bot",
                row(
                    dict(
                        schema=LEGACY,
                        span_id=f"{(3 if recovery else 2 if stop else 1):016x}",
                        **({"parent_span_id": f"{1:016x}"} if stop else {}),
                        event_name=name,
                        elapsed_ms=i,
                    ),
                    i + (30000 if recovery else 0),
                ),
            )
        for span in case["spans"]:
            service = (
                "documentation-system"
                if span["op"].startswith("catalog.")
                else "meeting"
            )
            base = dict(
                schema=SHARED,
                span_id=f"{span['id']:016x}",
                service=service,
                environment="local",
                operation=span["op"],
                traffic="synthetic",
                deployment="unknown",
                process_id=f"{n:016x}",
            )
            if "parent" in span:
                base["parent_span_id"] = f"{span['parent']:016x}"
            if "link" in span:
                base.update(link_span_id=f"{span['link']:016x}", link_trace_id=trace)
            events = [("start", "started", 0)]
            if span["end"]:
                events.append(("end", span["end"], 11))
            # Return the end before its start and replay it at least once.
            for name, result, elapsed in reversed(events):
                event_id = f"{(n * 10000 + span['id'] * 10 + int(name == 'end')):032x}"
                r = row(
                    dict(
                        base,
                        event_name=name,
                        result=result,
                        elapsed_ms=elapsed,
                        event_id=event_id,
                    ),
                    (30000 if "link" in span else 0) + span["id"] + elapsed,
                )
                yield service, r
                if name == "end":
                    yield service, r


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--anchor",
        required=True,
        help="UTC timestamp at least one minute before import",
    )
    p.add_argument(
        "--service",
        choices=["discord-bot", "meeting", "documentation-system"],
        required=True,
    )
    args = p.parse_args()
    for service, item in examples(utc(args.anchor)):
        if service == args.service:
            print(json.dumps(item))
