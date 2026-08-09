"""Render meeting minutes + transcript into a sleek, branded UTMIST PDF.

Design: an editorial, restrained layout — mostly ink-on-white with generous
whitespace, the UTMIST cobalt used only as a sparing accent (a hairline under
the header, small tracked section labels, speaker names). The logo sits on white
(top-left) with a small tracked "MEETING MINUTES" eyebrow; the LLM-generated
meeting title is the dominant element.

Uses bundled **DejaVu** (Unicode) TTFs, so accented names and non-latin scripts
(Cyrillic, Greek, …) render instead of ``?`` (DejaVu does not cover CJK or
emoji — those degrade to a missing-glyph box, never a ``?``). The only image is
the logo; all text stays real text, so the PDF is fully text-parsable.
"""

from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from src.contracts import Minutes

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_FONT_DIR = _ASSETS / "fonts"
_LOGO = _ASSETS / "brand" / "utmist-logo.png"

# Restrained palette (cobalt #002FCB is the accent, used sparingly).
_INK = (24, 26, 31)  # title + body text
_COBALT = (0, 47, 203)  # accent: header rule, section labels, speaker names
_MUTED = (108, 114, 126)  # meta, table labels, footer
_FAINT = (150, 156, 168)  # eyebrow
_RULE = (228, 230, 234)  # hairlines
_ZEBRA = (247, 248, 250)  # very light transcript row tint
_WHITE = (255, 255, 255)

_MARGIN = 18.0


class _MeetingPDF(FPDF):
    """FPDF with the bundled DejaVu families registered and a restrained footer."""

    def __init__(self) -> None:
        super().__init__()
        self.set_margins(_MARGIN, 16, _MARGIN)
        self.set_auto_page_break(auto=True, margin=20)
        self.add_font("DejaVu", "", str(_FONT_DIR / "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", str(_FONT_DIR / "DejaVuSans-Bold.ttf"))
        self.add_font("DejaVuMono", "", str(_FONT_DIR / "DejaVuSansMono.ttf"))

    def footer(self) -> None:
        self.set_y(-13)
        self.set_draw_color(*_RULE)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)
        self.set_x(self.l_margin)
        self.set_font("DejaVu", "B", 7.5)
        self.set_text_color(*_MUTED)
        _tracked(self, 0.8)
        self.cell(0, 5, "UTMIST", align="L", new_x=XPos.LMARGIN, new_y=YPos.TOP)
        _tracked(self, 0)
        self.set_font("DejaVu", "", 7.5)
        self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")
        self.set_text_color(*_INK)


def _tracked(pdf: FPDF, spacing: float) -> None:
    """Set letter-spacing (0 to disable) for uppercase labels/eyebrows."""
    pdf.set_char_spacing(spacing)


def _line(pdf: FPDF, h: float, text: str) -> None:
    # Left-aligned (ragged right) reads more editorial than justified, and avoids
    # the huge word-gaps justification produces on a short title.
    pdf.multi_cell(0, h, text, align="LEFT", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _label(pdf: FPDF, text: str) -> None:
    """A small, tracked, uppercase cobalt section label."""
    pdf.ln(4.5)
    pdf.set_font("DejaVu", "B", 9)
    pdf.set_text_color(*_COBALT)
    _tracked(pdf, 1.4)
    _line(pdf, 5, text.upper())
    _tracked(pdf, 0)
    pdf.set_text_color(*_INK)
    pdf.ln(1.2)


def _muted_note(pdf: FPDF, text: str) -> None:
    pdf.set_font("DejaVu", "", 10.5)
    pdf.set_text_color(*_MUTED)
    _line(pdf, 5.6, text)
    pdf.set_text_color(*_INK)


def _masthead(pdf: FPDF, title: str, meta_strip: str, participants_line: str) -> None:
    top = pdf.get_y()
    # Logo (natural blue on white), top-left.
    logo_w = 14.0
    logo_h = logo_w * (672.0 / 600.0)
    pdf.image(str(_LOGO), x=pdf.l_margin, y=top, w=logo_w, h=logo_h)

    # Right-aligned tracked eyebrow, vertically centered against the logo.
    pdf.set_font("DejaVu", "B", 8)
    pdf.set_text_color(*_FAINT)
    _tracked(pdf, 2.0)
    pdf.set_xy(pdf.l_margin, top + (logo_h - 5) / 2)
    pdf.cell(0, 5, "MEETING MINUTES", align="R", new_x=XPos.LMARGIN, new_y=YPos.TOP)
    _tracked(pdf, 0)
    pdf.set_text_color(*_INK)

    # Title + meta below the logo row.
    pdf.set_y(top + logo_h + 6)
    pdf.set_font("DejaVu", "B", 22)
    pdf.set_text_color(*_INK)
    _line(pdf, 10, title)

    pdf.ln(0.5)
    pdf.set_font("DejaVu", "", 9.5)
    pdf.set_text_color(*_MUTED)
    if meta_strip:
        _line(pdf, 5, meta_strip)
    _line(pdf, 5, participants_line)
    pdf.set_text_color(*_INK)

    # Signature hairline in cobalt separating the header from the body.
    pdf.ln(2.5)
    pdf.set_draw_color(*_COBALT)
    pdf.set_line_width(0.5)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.set_line_width(0.2)


def _marked_list(pdf: FPDF, items: list[str], marker: str, marker_color) -> None:
    if not items:
        _muted_note(pdf, "None recorded.")
        return
    pdf.set_font("DejaVu", "", 10.5)
    with pdf.table(
        borders_layout="NONE",
        first_row_as_headings=False,
        col_widths=(6, 146),
        text_align=("LEFT", "LEFT"),
        v_align="TOP",
        line_height=5.8,
        padding=(0.6, 1, 0.6, 0),
        width=pdf.epw,
    ) as table:
        for item in items:
            row = table.row()
            row.cell(marker, style=FontFace(color=marker_color))
            row.cell(item)


def _parse_turn(line: str) -> tuple[str, str, str]:
    if line.startswith("[") and "] " in line:
        ts, after = line.split("] ", 1)
        speaker, sep, text = after.partition(": ")
        if sep:
            return ts.lstrip("[").strip(), speaker.strip(), text.strip()
    return "", "", line.strip()


def _transcript(pdf: FPDF, transcript: str) -> None:
    lines = [ln for ln in (transcript or "").split("\n") if ln.strip()]
    if not lines:
        _muted_note(pdf, "None recorded.")
        return
    pdf.set_font("DejaVu", "", 9)
    with pdf.table(
        borders_layout="NONE",
        headings_style=FontFace(emphasis="BOLD", color=_MUTED),
        col_widths=(19, 26, 107),
        text_align=("LEFT", "LEFT", "LEFT"),
        v_align="TOP",
        cell_fill_mode="ROWS",
        cell_fill_color=_ZEBRA,
        line_height=5.2,
        padding=(1.6, 1.5, 1.6, 1.5),
        width=pdf.epw,
    ) as table:
        table.row(("TIME", "SPEAKER", "TEXT"))
        for line in lines:
            ts, speaker, text = _parse_turn(line)
            row = table.row()
            # 8.5pt mono keeps "00:00:01" on one line in the Time column.
            row.cell(ts, style=FontFace(family="DejaVuMono", size_pt=8.5, color=_MUTED))
            row.cell(speaker, style=FontFace(emphasis="BOLD", color=_COBALT))
            row.cell(text)


def _meta_strip(meta: dict, minutes: Minutes) -> tuple[str, str]:
    participants = meta.get("participants", []) or []
    parts = []
    if meta.get("started_at"):
        parts.append(str(meta["started_at"]))
    if meta.get("duration_label"):
        parts.append(str(meta["duration_label"]))
    parts.append(f"{len(participants)} participant" + ("" if len(participants) == 1 else "s"))
    n_actions = len(minutes.action_items)
    if n_actions:
        parts.append(f"{n_actions} action item" + ("" if n_actions == 1 else "s"))
    strip = "    ·    ".join(parts)
    participants_line = "Participants: " + (
        ", ".join(participants) if participants else "None recorded."
    )
    return strip, participants_line


def _document_title(minutes: Minutes, meta: dict) -> str:
    """The big meeting title: the LLM-generated title, falling back to a
    caller-supplied meta title, then a static default."""
    return (
        (getattr(minutes, "title", "") or "").strip()
        or (meta.get("title") or "").strip()
        or "Meeting Minutes"
    )


def render_meeting_pdf(minutes: Minutes, transcript: str, meta: dict) -> bytes:
    pdf = _MeetingPDF()
    pdf.add_page()

    strip, participants_line = _meta_strip(meta, minutes)
    _masthead(pdf, _document_title(minutes, meta), strip, participants_line)

    _label(pdf, "Summary")
    if minutes.summary:
        pdf.set_font("DejaVu", "", 10.5)
        pdf.set_text_color(*_INK)
        _line(pdf, 5.6, minutes.summary)
    else:
        _muted_note(pdf, "None recorded.")

    _label(pdf, "Decisions")
    _marked_list(pdf, minutes.decisions, "•", _COBALT)

    _label(pdf, "Action Items")
    _marked_list(pdf, minutes.action_items, "☐", _MUTED)

    _label(pdf, "Full Transcript")
    _transcript(pdf, transcript)

    return bytes(pdf.output())
