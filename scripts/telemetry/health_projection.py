"""Materialize feature cohorts, completed jobs, marks and resource observations."""

from itertools import groupby
import json


def materialize(raw, snap, cutoff):
    snap.executescript("""
        create table health_minutes(traffic text, minute text, service text, operation text, route text, admitted integer,
          result text, latency integer, n integer,
          primary key(traffic,minute,service,operation,route,admitted,result,latency)) without rowid;
        create table health_runs(traffic text, started text, minute text, sealed integer,
          service text,operation text,route text,admitted_at text,ended text,result text,latency integer,conflict_at text);
        create index health_bounds on health_runs(traffic,started);
        create index health_crossings on health_runs(traffic,sealed,started);
        create table health_jobs(traffic text,operation text,ts text,started text,result text,measures text,conflict_at text);
        create index job_end on health_jobs(traffic,operation,ts);
        create table health_marks(traffic text,ts text,name text,n integer, primary key(traffic,ts,name)) without rowid;
        create table health_resources(service text,observed_at text,cpu_cores real,memory_bytes integer,deployed_at text,
          primary key(service,observed_at)) without rowid;
    """)
    # Stream one span at a time. No document, account or trace identifiers enter
    # these aggregate tables. A span with only an end cannot start a cohort.
    events = raw.execute(
        """
        select service,traffic,operation,trace_id,span_id,event_name,ts,result,elapsed_ms,payload
        from events where ts>=? and (operation in ('answer','http.request') or
          operation like 'answer.%' or operation like 'catalog.%' or operation like 'login.%')
        order by service,traffic,operation,trace_id,span_id,ts,case when event_name='start' then 0 else 1 end,event_id
    """,
        (cutoff,),
    )
    for identity, group in groupby(events, key=lambda row: tuple(row[:5])):
        service, traffic, op, _, _ = identity
        start = admitted = ended = conflict = result = None
        route = ""
        latency = 0
        last = None
        job_measures = None
        for row in group:
            name, ts, value, elapsed, payload = row[5:]
            last = ts
            if name == "start" and start is None:
                start = ts
                route = json.loads(payload).get("route", "")
            elif name == "admitted" and admitted is None:
                admitted = ts
            elif name == "end":
                if ended is None:
                    ended, result, latency = ts, value, elapsed
                    if op in (
                        "catalog.discovery",
                        "catalog.refresh",
                        "catalog.freshness",
                    ):
                        job_measures = json.dumps(
                            json.loads(payload).get("measures", {}),
                            separators=(",", ":"),
                        )
                elif value != result and conflict is None:
                    conflict = ts
            elif (op, name) in (
                ("answer.model", "tool_round"),
                ("answer.delivery", "part_sent"),
            ):
                snap.execute(
                    """insert into health_marks values(?,?,?,1)
                  on conflict(traffic,ts,name) do update set n=n+1""",
                    (traffic, ts, name),
                )
        if job_measures is not None:
            snap.execute(
                "insert into health_jobs values(?,?,?,?,?,?,?)",
                (traffic, op, ended, start, result, job_measures, conflict),
            )
        if not start:
            continue
        minute = start[:16] + ":00.000Z"
        sealed = int(last[:16] == start[:16])
        snap.execute(
            "insert into health_runs values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                traffic,
                start,
                minute,
                sealed,
                service,
                op,
                route,
                admitted,
                ended,
                result,
                latency,
                conflict,
            ),
        )
        if sealed:
            known = result if ended and not conflict else "unknown"
            snap.execute(
                """insert into health_minutes values(?,?,?,?,?,?,?,?,1)
                on conflict do update set n=n+1""",
                (
                    traffic,
                    minute,
                    service,
                    op,
                    route,
                    int(admitted is not None),
                    known,
                    latency if known != "unknown" else 0,
                ),
            )
    if raw.execute(
        "select 1 from sqlite_master where type='table' and name='railway_samples'"
    ).fetchone():
        snap.executemany(
            "insert into health_resources values(?,?,?,?,?)",
            raw.execute(
                "select service,observed_at,cpu_cores,memory_bytes,deployed_at from railway_samples where observed_at>=?",
                (cutoff,),
            ),
        )
