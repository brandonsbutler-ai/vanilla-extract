#!/usr/bin/env python3
"""Render VALIDATION.md to a customer-facing PDF.

    python3 generate_validation_pdf.py [out.pdf]

Deliberately a thin renderer rather than a general Markdown engine: it handles
the constructs VALIDATION.md actually uses (headings, paragraphs, fenced code,
pipe tables, bullets, bold spans) and nothing else. A general engine would be
more code and more ways to render the document wrong.

fpdf2 is a BUILD-time dependency for this one script. The library itself still
has none.
"""

import os
import re
import sys

try:
    from fpdf import FPDF
except ImportError:
    sys.exit("this renderer needs fpdf2:  python3 -m pip install fpdf2")

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "VALIDATION.md")

INK = (26, 26, 24)
MUTED = (110, 110, 104)
RULE = (200, 200, 192)
CODE_BG = (244, 244, 241)
ACCENT = (31, 77, 143)


def to_latin1(text):
    """Helvetica is latin-1. Replace what it cannot draw rather than crash."""
    return (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"')
                .replace("—", "--").replace("–", "-")
                .replace("…", "...").replace(" ", " ")
                .replace("•", "-").replace("→", "->")
                .encode("latin-1", "replace").decode("latin-1"))


class Report(FPDF):
    def header(self):
        if self.page_no() == 1:
            return
        self.set_y(8)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 4, "vanilla-extract 0.2.0 -- Usage, Methods and Validation Results",
                  align="L")
        self.set_y(8)
        self.cell(0, 4, f"page {self.page_no()}", align="R", new_x="LMARGIN",
                  new_y="NEXT")
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.line(self.l_margin, 14, self.w - self.r_margin, 14)
        self.set_y(19)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*MUTED)
        self.cell(0, 4, "Every figure in this document is produced by a script in the "
                        "repository. Section 9 lists the commands.", align="C")

    # -- block renderers --------------------------------------------------
    def rich(self, text, size=9.2, leading=4.3):
        """A paragraph with **bold** and `code` spans honoured."""
        self.set_text_color(*INK)
        width = self.w - self.l_margin - self.r_margin
        for token in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
            if not token:
                continue
            if token.startswith("**") and token.endswith("**"):
                self.set_font("Helvetica", "B", size)
                body = token[2:-2]
            elif token.startswith("`") and token.endswith("`"):
                self.set_font("Courier", "", size - 0.6)
                body = token[1:-1]
            else:
                self.set_font("Helvetica", "", size)
                body = token
            for i, word in enumerate(body.split(" ")):
                if not word and i:
                    continue
                chunk = word + " "
                w = self.get_string_width(chunk)
                if self.get_x() + w > self.w - self.r_margin:
                    self.ln(leading)
                    self.set_x(self.l_margin)
                self.cell(w, leading, chunk)
        self.ln(leading)

    def heading(self, level, text):
        self.ln(3.2 if level == 2 else 2.2)
        if self.get_y() > self.h - 40:
            self.add_page()
        size = {1: 15, 2: 11.5, 3: 9.8}.get(level, 9.8)
        self.set_font("Helvetica", "B", size)
        self.set_text_color(*(ACCENT if level <= 2 else INK))
        self.multi_cell(0, size * 0.42, to_latin1(text), new_x="LMARGIN", new_y="NEXT")
        if level <= 2:
            self.set_draw_color(*RULE)
            self.set_line_width(0.25)
            y = self.get_y() + 0.6
            self.line(self.l_margin, y, self.w - self.r_margin, y)
            self.ln(2.4)
        else:
            self.ln(1.2)

    def code(self, lines):
        self.ln(1)
        self.set_font("Courier", "", 7.9)
        width = self.w - self.l_margin - self.r_margin
        height = len(lines) * 3.7 + 3
        if self.get_y() + height > self.h - 20:
            self.add_page()
        self.set_fill_color(*CODE_BG)
        self.rect(self.l_margin, self.get_y(), width, height, style="F")
        self.set_xy(self.l_margin + 2, self.get_y() + 1.5)
        self.set_text_color(*INK)
        for line in lines:
            self.set_x(self.l_margin + 2)
            self.cell(width - 4, 3.7, to_latin1(line)[:112], new_x="LMARGIN",
                      new_y="NEXT")
        self.ln(2.6)

    def table(self, rows):
        if not rows:
            return
        cols = len(rows[0])
        width = self.w - self.l_margin - self.r_margin
        # first column narrower when there are three or more
        widths = ([width * 0.30] + [width * 0.70 / (cols - 1)] * (cols - 1)
                  if cols > 1 else [width])
        for r, row in enumerate(rows):
            # Pad or trim so a ragged row cannot index past `widths`.
            row = (list(row) + [""] * cols)[:cols]
            self.set_font("Helvetica", "B" if r == 0 else "", 8.3)
            heights = []
            for i, cell in enumerate(row):
                txt = to_latin1(re.sub(r"\*\*|`", "", cell))
                lines = max(1, len(self.multi_cell(widths[i], 3.6, txt,
                                                   dry_run=True, output="LINES")))
                heights.append(lines * 3.6)
            h = max(heights) + 1.6
            if self.get_y() + h > self.h - 18:
                self.add_page()
                self.set_font("Helvetica", "B" if r == 0 else "", 8.3)
            y0 = self.get_y()
            if r == 0:
                self.set_fill_color(*CODE_BG)
                self.rect(self.l_margin, y0, width, h, style="F")
            x = self.l_margin
            for i, cell in enumerate(row):
                self.set_xy(x, y0 + 0.8)
                self.set_text_color(*INK)
                self.multi_cell(widths[i], 3.6,
                                to_latin1(re.sub(r"\*\*|`", "", cell)),
                                new_x="LMARGIN", new_y="TOP")
                x += widths[i]
            self.set_y(y0 + h)
            self.set_draw_color(*RULE)
            self.set_line_width(0.15)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin,
                      self.get_y())
        self.ln(2.4)

    def bullet(self, text):
        width = self.w - self.l_margin - self.r_margin
        if self.get_y() > self.h - 26:
            self.add_page()
        self.set_font("Helvetica", "", 9.2)
        self.set_text_color(*INK)
        self.set_x(self.l_margin)
        self.cell(3.4, 4.3, "-")
        start = self.get_x()
        self.set_x(start)
        saved_l = self.l_margin
        self.l_margin = start
        self.rich(text)
        self.l_margin = saved_l


_PIPE_SENTINEL = "\x00PIPE\x00"


def _split_row(line):
    r"""Split a Markdown table row, honouring the `\|` escape.

    A naive split on "|" also splits an escaped pipe inside a cell, which both
    shifts every following cell and produces more cells than the header has --
    the shell example in section 6 contains one.
    """
    return [c.strip().replace(_PIPE_SENTINEL, "|")
            for c in line.replace("\\|", _PIPE_SENTINEL).strip("|").split("|")]


def parse_and_render(pdf, md):
    lines = md.splitlines()
    i = 0
    para = []

    def flush():
        nonlocal para
        if para:
            pdf.rich(" ".join(para))
            pdf.ln(1.4)
            para = []

    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            flush()
            i += 1
            block = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            pdf.code(block)
            i += 1
            continue

        if line.startswith("|") and i + 1 < len(lines) and set(
                lines[i + 1].replace("|", "").replace(" ", "")) <= {"-", ":"}:
            flush()
            rows = [_split_row(line)]
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            pdf.table(rows)
            continue

        if line.startswith("### "):
            flush(); pdf.heading(3, line[4:]); i += 1; continue
        if line.startswith("## "):
            flush(); pdf.heading(2, line[3:]); i += 1; continue
        if line.startswith("# "):
            flush(); pdf.heading(1, line[2:]); i += 1; continue
        if line.strip() == "---":
            flush(); i += 1; continue
        if line.startswith("- "):
            flush(); pdf.bullet(line[2:]); i += 1; continue
        if not line.strip():
            flush(); i += 1; continue

        para.append(line.strip())
        i += 1
    flush()


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "vanilla_extract_Validation_Report.pdf")
    md = open(SRC, encoding="utf-8").read()

    pdf = Report(format="letter", unit="mm")
    pdf.set_margins(18, 19, 18)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 21)
    pdf.set_text_color(*INK)
    pdf.cell(0, 9, "vanilla-extract", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11.5)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 6, "Usage, Methods and Validation Results",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8.6)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 5, "Version 0.2.0  |  verification run 2026-09-16  |  MIT licensed  |  "
                   "github.com/brandonsbutler-ai/vanilla-extract",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)

    # skip the markdown's own title block; the cover above replaces it
    body = md.split("\n", 3)[3] if md.startswith("# ") else md
    parse_and_render(pdf, body)

    pdf.output(out)
    print(f"wrote {out}  ({pdf.page_no()} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
