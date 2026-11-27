"""Shared presentation helpers for the local operator views."""

from dataclasses import dataclass
from datetime import datetime
from html import escape
from urllib.parse import urlencode

from project import iso, parsed

DETAIL_ROLES = {"developer", "admin"}


@dataclass(frozen=True)
class Period:
    traffic: str
    hours: int
    start: datetime
    end: datetime
    previous: datetime
    retention_floor: datetime

    @property
    def query(self):
        return {"traffic": self.traffic, "hours": self.hours, "end": iso(self.end)}


def esc(value):
    return escape(str(value), quote=True)


def table(columns, rows):
    return (
        '<div class="scroll"><table><thead><tr>'
        + "".join("<th>" + esc(x) + "</th>" for x in columns)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join("<td>" + str(c) + "</td>" for c in row) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def link(path, params, title):
    return (
        '<a href="' + esc(path + "?" + urlencode(params)) + '">' + esc(title) + "</a>"
    )


def sources(db, end, max_age):
    rows = db.execute(
        "select service,last_success,last_error,gaps,drops,rejected from sources order by service"
    ).fetchall()

    def condition(r):
        if r["last_error"]:
            return "Read failed"
        if not r["last_success"]:
            return "No successful read"
        age = (end - parsed(r["last_success"])).total_seconds()
        return "Current" if -60 <= age <= max_age else "Stale"

    return [(r, condition(r)) for r in rows]
