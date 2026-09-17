"""The desktop window.

Qt rather than tkinter, for two reasons that are not taste. A drop carries real
filesystem paths -- QMimeData gives local files directly -- so the file-state
datasheet keeps meaning something, which a browser drop cannot do. And the
widgets take a stylesheet, so the window can be made to look like the reports
it produces instead of like a 1990s form.

Everything this window DECIDES lives in session.py, which imports nothing
outside the standard library. This file is chrome.
"""

import os
import sys
import webbrowser

from .session import Cancelled, DropError, Session, folders_from_drop

# GOLD ON BLACK, with the particulars in a brighter accent.
#
# Three surfaces, not one, because everything sat on a single flat ground and
# there was no telling where one field ended and the next began:
#
#   BG      the page              deepest, warm black rather than neutral
#   CARD    a panel on the page    a step up, still dark
#   SUNK    a row inside a panel   a half-step back from the panel
#
# Gold carries the STRUCTURE -- headings, rules, borders, the primary action.
# Orange carries the PARTICULARS -- the values the tool extracted, the counts,
# the discovered field names. The split is functional, not decorative: on a
# gold-on-black screen a reader is looking for the numbers, and giving them
# their own hue means they are found without reading anything around them.
#
# Magenta is the exception colour. On this palette the usual amber warning is
# a shade of gold and disappears into the chrome, so anything that could not
# be read is magenta -- the one hue on screen that means only that.
INK     = "#f2ece0"      # warm off-white; pure white glares on black
MUTED   = "#a89d86"
FAINT   = "#7a7160"
BG      = "#0d0c0a"      # the page
CARD    = "#17150f"      # a panel
SUNK    = "#1f1c14"      # a row inside a panel
LINE    = "#3a3425"
LINE2   = "#2a2519"
# The piping is VANILLA, not gold. Gold reads as a finance product; the colour
# this is named after is the cream of the ice cream, and it is warmer and much
# less saturated. It carries the structure -- headings, rules, borders, the
# primary action -- and never the values.
GOLD    = "#e3d2a8"      # vanilla cream: the piping
GOLD_DK = "#8d8059"      # the same hue with the light taken out, for rules
ACCENT  = GOLD
ACCENT2 = "#f0e3c4"
PARTIC  = "#ff9e3d"      # particulars: values, counts, field names
MAGENTA = "#ff4fa3"      # could not be read
MAGBG   = "#2a1220"
OKC     = "#7bd88f"

_TICK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<path d="M3.5 8.4 L6.4 11.3 L12.5 4.8" stroke="#1a170f" stroke-width="2.4"'
    ' fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _tick_path():
    """The checkbox tick, written once to a temp file.

    A data: URI inside a Qt stylesheet is read by Qt's own CSS parser, which
    rejects the commas and angle brackets an inline SVG is made of -- and it
    rejects the WHOLE SHEET from that rule onward, so the symptom was ticked
    boxes drawing empty rather than an error anybody would notice.

    The stroke is near-black because the box it sits in is gold.
    """
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "vanilla_gui_tick.svg")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_TICK_SVG)
    return path.replace("\\", "/")


TICK = _tick_path()

# Two faces, used for two different jobs, so a glance tells you which kind of
# thing you are reading.
#
#   SANS  everything a person wrote: headings, labels, explanations
#   MONO  everything a MACHINE produced: paths, filenames, counts, reasons
#
# A path in the same face as a sentence reads as prose and gets skimmed; in a
# monospaced face it reads as a value and gets checked.
SANS = ('"Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", '
        '"Noto Sans", "DejaVu Sans", sans-serif')
MONO = ('"JetBrains Mono", "Cascadia Mono", "SF Mono", "Ubuntu Mono", '
        '"DejaVu Sans Mono", "Liberation Mono", monospace')

STYLE = f"""
* {{
    font-family: {SANS};
    color: {INK};
}}
QWidget#root      {{ background: {BG}; }}
QLabel#title      {{ font-size: 26px; font-weight: 600; letter-spacing: -0.4px;
                     color: {GOLD}; }}
QLabel#blurb      {{ font-size: 13px; color: {MUTED}; }}
QLabel#sectionCap {{ font-size: 11px; color: {GOLD_DK}; font-weight: 600;
                     letter-spacing: 0.9px; text-transform: uppercase; }}
QLabel#folderPath {{ font-size: 12px; color: {MUTED}; font-family: {MONO}; }}
QLabel#indexLine  {{ font-size: 16px; font-weight: 600; font-family: {MONO};
                     letter-spacing: -0.2px; color: {PARTIC}; }}

/* Step numbering. The state of each one is carried by the chip, so the eye
   finds "where am I" before it reads a single word. */
QLabel#stepDone {{
    background: {OKC}; color: #10210f; border-radius: 11px;
    min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
    font-size: 12px; font-weight: 700;
}}
QLabel#stepNow {{
    background: {GOLD}; color: #1a170f; border-radius: 11px;
    min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
    font-size: 12px; font-weight: 700;
}}
QLabel#stepNext {{
    background: {SUNK}; color: {FAINT}; border-radius: 11px;
    min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
    font-size: 12px; font-weight: 700;
}}
QLabel#stepHeadNow  {{ font-size: 14px; font-weight: 600; color: {INK}; }}
QLabel#stepHeadNext {{ font-size: 14px; font-weight: 600; color: {FAINT}; }}
QLabel#stepHeadDone {{ font-size: 14px; font-weight: 600; color: {MUTED}; }}
QLabel#status     {{ font-size: 12px; color: {MUTED}; font-family: {MONO}; }}
QLabel#headline   {{ font-size: 18px; font-weight: 600; font-family: {MONO};
                     letter-spacing: -0.3px; color: {PARTIC}; }}
QLabel#optWhy     {{ font-size: 12px; color: {FAINT}; }}
QLabel#fileLine   {{ font-size: 12.5px; color: {MUTED}; font-weight: 500; }}
QLabel#valueLine  {{ font-size: 12px; color: {PARTIC}; font-family: {MONO}; }}

QFrame#card {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 12px;
}}
QFrame#summary {{
    background: {CARD};
    border: 1px solid {LINE};
    border-left: 3px solid {PARTIC};
    border-radius: 10px;
}}
/* One option per row, each on its own recessed strip, so the label and the
   sentence under it read as one field and not as four loose lines. */
QFrame#optRow {{
    background: {SUNK};
    border: 1px solid {LINE2};
    border-radius: 9px;
}}
QFrame#optRow:hover {{ border-color: {GOLD_DK}; background: #241f14; }}
QFrame#writtenRow {{
    background: {SUNK};
    border: 1px solid {LINE2};
    border-radius: 8px;
}}
QFrame#rule {{ background: {LINE2}; max-height: 1px; border: none; }}
QFrame#drop {{
    background: {CARD};
    border: 2px dashed {GOLD_DK};
    border-radius: 14px;
}}
QFrame#dropActive {{
    background: #221d10;
    border: 2px dashed {GOLD};
    border-radius: 14px;
}}
QLabel#dropTitle  {{ font-size: 19px; font-weight: 500; }}
QLabel#dropOr     {{ font-size: 12px; color: {FAINT}; }}

/* The exception box is MAGENTA, not amber. On gold-on-black an amber warning
   is a shade of the chrome and vanishes into it; magenta is the one hue on
   this screen that means only "this could not be read". */
QFrame#warnBox {{
    background: {MAGBG};
    border: 1px solid #5c2a44;
    border-left: 3px solid {MAGENTA};
    border-radius: 10px;
}}
QLabel#warnHead {{ font-size: 13px; font-weight: 600; color: {MAGENTA}; }}
QLabel#warnLine {{ font-size: 12px; color: {MAGENTA}; font-family: {MONO}; }}
QLabel#warnNote {{ font-size: 12px; color: {MUTED}; }}

QPushButton {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 9px;
    padding: 10px 18px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton:hover    {{ background: #221e15; border-color: {GOLD_DK}; }}
QPushButton:disabled {{ color: {FAINT}; background: #14120d; }}
QPushButton#primary {{
    background: {GOLD}; border: 1px solid {GOLD};
    color: #1a170f; padding: 11px 26px; font-weight: 700;
}}
QPushButton#primary:hover    {{ background: {ACCENT2}; border-color: {ACCENT2}; }}
QPushButton#primary:disabled {{ background: #4a3f1d; border-color: #4a3f1d;
                                color: #8a7c55; }}

QCheckBox {{ font-size: 13px; font-weight: 500; spacing: 9px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border: 1.5px solid {LINE}; border-radius: 5px; background: {SUNK};
}}
QCheckBox::indicator:checked {{
    background: {GOLD};
    border-color: {GOLD};
    image: url({TICK});
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QProgressBar {{
    background: {SUNK}; border: none; border-radius: 3px;
    height: 6px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {PARTIC}; border-radius: 3px; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent;
                                                border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 5px;
                               min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


def _missing_toolkit_message():
    return ("vanilla: the desktop window needs PySide6.\n"
            "    pip install PySide6-Essentials\n"
            "The library and the command line do not: run `vanilla --help`.")


def build(qt, session=None):
    """Construct the window. `qt` is the imported PySide6 namespace tuple."""
    QtCore, QtGui, QtWidgets = qt

    class Worker(QtCore.QThread):
        tick = QtCore.Signal(int, int, str)
        stage = QtCore.Signal(str)
        done = QtCore.Signal(object)
        failed = QtCore.Signal(str)
        cancelled = QtCore.Signal()

        def __init__(self, session):
            super().__init__()
            self.session = session
            self._cancel = False

        def cancel(self):
            """Ask the run to stop at the next document boundary."""
            self._cancel = True

        def run(self):
            try:
                result = self.session.run(
                    on_progress=lambda n, t, name: self.tick.emit(n, t, name),
                    on_stage=self.stage.emit,
                    should_cancel=lambda: self._cancel)
                self.done.emit(result)
            except Cancelled:
                self.cancelled.emit()
            except Exception as exc:                      # noqa: BLE001
                self.failed.emit(f"{type(exc).__name__}: {exc}")

    class DropZone(QtWidgets.QFrame):
        """The target. Accepts a folder, or a file and takes its folder."""

        dropped = QtCore.Signal(list)

        def __init__(self):
            super().__init__()
            self.setObjectName("drop")
            self.setAcceptDrops(True)
            self.setMinimumHeight(168)
            lay = QtWidgets.QVBoxLayout(self)
            lay.setAlignment(QtCore.Qt.AlignCenter)
            lay.setSpacing(4)
            self.title = QtWidgets.QLabel("Drop a folder of documents here")
            self.title.setObjectName("dropTitle")
            self.title.setAlignment(QtCore.Qt.AlignCenter)
            self.sub = QtWidgets.QLabel("or")
            self.sub.setObjectName("dropOr")
            self.sub.setAlignment(QtCore.Qt.AlignCenter)
            self.button = QtWidgets.QPushButton("Choose a folder…")
            self.button.setCursor(QtCore.Qt.PointingHandCursor)
            row = QtWidgets.QHBoxLayout()
            row.addStretch(1)
            row.addWidget(self.button)
            row.addStretch(1)
            lay.addWidget(self.title)
            lay.addWidget(self.sub)
            lay.addSpacing(4)
            lay.addLayout(row)

        def _restyle(self, name):
            self.setObjectName(name)
            self.style().unpolish(self)
            self.style().polish(self)

        def dragEnterEvent(self, event):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                self._restyle("dropActive")

        def dragLeaveEvent(self, _event):
            self._restyle("drop")

        def dropEvent(self, event):
            self._restyle("drop")
            urls = event.mimeData().urls()
            if not urls:
                return
            # A Qt drop carries real local paths, and all of them. That is the
            # whole reason this is a native window: the datasheet records
            # owner, permissions and creation time, none of which survives a
            # browser's file drop -- and a browser cannot hand over a folder's
            # location at all, only copies of the files inside it.
            self.dropped.emit([u.toLocalFile() for u in urls])
            event.acceptProposedAction()

    class Window(QtWidgets.QWidget):
        def __init__(self, session):
            super().__init__()
            self.session = session
            self.worker = None
            self._running = False
            self.steps = {}
            self.setObjectName("root")
            self.setWindowTitle("Vanilla Extract")
            # Geared for 1920x1080: wide enough to put the results beside the
            # workflow rather than below it, and short enough to clear a
            # taskbar and a title bar on a 1080-high screen. Type size does not
            # change with the window -- a wider screen is for showing more at
            # once, not for making the same content smaller.
            self.resize(1560, 940)
            self.setMinimumSize(720, 640)
            self.setStyleSheet(STYLE)
            self._build()
            self._progress_through("start")

        # -- steps
        def _step(self, number, text):
            """A numbered heading whose chip carries its state.

            Three states, and the chip says which without being read: grey and
            numbered means not yet, blue and numbered means this is where you
            are, green and ticked means done. The heading's colour follows.
            """
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(11)
            chip = QtWidgets.QLabel(str(number))
            chip.setObjectName("stepNext")
            chip.setAlignment(QtCore.Qt.AlignCenter)
            head = QtWidgets.QLabel(text)
            head.setObjectName("stepHeadNext")
            row.addWidget(chip)
            row.addWidget(head)
            row.addStretch(1)
            self.steps[number] = (chip, head, str(number))
            return row

        def _set_step(self, number, state):
            chip, head, label = self.steps[number]
            chip.setText("\u2713" if state == "done" else label)
            chip.setObjectName({"done": "stepDone", "now": "stepNow",
                                "next": "stepNext"}[state])
            head.setObjectName({"done": "stepHeadDone", "now": "stepHeadNow",
                                "next": "stepHeadNext"}[state])
            for widget in (chip, head):
                widget.style().unpolish(widget)
                widget.style().polish(widget)

        def _progress_through(self, stage):
            """Which step the person is on, from what they have actually done."""
            loaded = self.session.folder is not None
            ran = self.session.result is not None
            self._set_step(1, "done" if loaded else "now")
            self._set_step(2, "now" if loaded and not ran else
                           ("done" if ran else "next"))
            self._set_step(3, "done" if ran else ("now" if loaded else "next"))

        # -- layout
        def _build(self):
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            outer.addWidget(scroll)
            page = QtWidgets.QWidget()
            page.setObjectName("root")
            scroll.setWidget(page)

            # Two columns on a wide screen, one on a narrow one. The workflow
            # reads top to bottom on the left; the results appear on the right
            # rather than pushing the controls off the bottom of the screen,
            # which is what happened when everything was one column.
            self.columns = QtWidgets.QHBoxLayout(page)
            self.columns.setContentsMargins(38, 34, 38, 34)
            self.columns.setSpacing(30)

            left = QtWidgets.QWidget()
            left.setObjectName("root")
            left.setMaximumWidth(760)      # prose stops being readable past this
            col = QtWidgets.QVBoxLayout(left)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            self.columns.addWidget(left, 0)

            title = QtWidgets.QLabel("Vanilla Extract")
            title.setObjectName("title")
            blurb = QtWidgets.QLabel(
                "Drop a folder of documents. Nothing is uploaded, and nothing "
                "is written into the folder you drop.")
            blurb.setObjectName("blurb")
            blurb.setWordWrap(True)
            col.addWidget(title)
            col.addSpacing(4)
            col.addWidget(blurb)
            col.addSpacing(24)

            col.addLayout(self._step(1, "Choose a folder"))
            col.addSpacing(10)
            self.zone = DropZone()
            self.zone.dropped.connect(self._load)
            self.zone.button.clicked.connect(self._choose)
            col.addWidget(self.zone)
            col.addSpacing(20)

            self.summary = QtWidgets.QFrame()
            self.summary.setObjectName("summary")
            sl = QtWidgets.QVBoxLayout(self.summary)
            sl.setContentsMargins(18, 14, 18, 15)
            sl.setSpacing(3)
            self.folder_path = QtWidgets.QLabel("")
            self.folder_path.setObjectName("folderPath")
            self.folder_path.setWordWrap(True)
            self.index_line = QtWidgets.QLabel("")
            self.index_line.setObjectName("indexLine")
            sl.addWidget(self.folder_path)
            sl.addWidget(self.index_line)
            self.summary.hide()          # nothing to summarise until a drop
            col.addWidget(self.summary)
            col.addSpacing(20)

            col.addLayout(self._step(2, "Choose what to do with it"))
            col.addSpacing(10)
            col.addWidget(self._options_card())
            col.addSpacing(22)
            col.addLayout(self._step(3, "Run it"))
            col.addSpacing(10)

            row = QtWidgets.QHBoxLayout()
            row.setSpacing(10)
            self.run_btn = QtWidgets.QPushButton("Run")
            self.run_btn.setObjectName("primary")
            self.run_btn.setCursor(QtCore.Qt.PointingHandCursor)
            self.run_btn.setEnabled(False)
            self.run_btn.clicked.connect(self._primary)
            self.report_btn = QtWidgets.QPushButton("Open report  ↗")
            self.report_btn.setEnabled(False)
            self.report_btn.clicked.connect(self._open_report)
            self.files_btn = QtWidgets.QPushButton("Show files")
            self.files_btn.setEnabled(False)
            self.files_btn.clicked.connect(self._open_out)
            self.clear_btn = QtWidgets.QPushButton("Start over")
            self.clear_btn.clicked.connect(self._clear)
            self.clear_btn.setVisible(False)
            for b in (self.run_btn, self.report_btn, self.files_btn):
                row.addWidget(b)
            row.addStretch(1)
            row.addWidget(self.clear_btn)
            col.addLayout(row)
            col.addSpacing(16)

            self.bar = QtWidgets.QProgressBar()
            self.bar.setTextVisible(False)
            self.bar.hide()
            col.addWidget(self.bar)
            self.status = QtWidgets.QLabel("")
            self.status.setObjectName("status")
            self.status.setWordWrap(True)
            col.addSpacing(8)
            col.addWidget(self.status)

            col.addStretch(1)

            # The right-hand column: empty until there is something to put in
            # it, so an unused half of the window is not sitting there looking
            # like something failed to load.
            self.right = QtWidgets.QWidget()
            self.right.setObjectName("root")
            self.results = QtWidgets.QVBoxLayout(self.right)
            self.results.setContentsMargins(0, 0, 0, 0)
            self.results.setSpacing(0)
            self.results.addStretch(1)
            self.right.hide()
            self.columns.addWidget(self.right, 1)

        def _options_card(self):
            card = QtWidgets.QFrame()
            card.setObjectName("card")
            lay = QtWidgets.QVBoxLayout(card)
            lay.setContentsMargins(22, 18, 22, 20)
            lay.setSpacing(2)
            self.boxes = {}
            for key, text, why in (
                    ("recognize", "Discover the fields",
                     "find the labels these documents share and make them columns"),
                    ("datasheet", "Record each file's state as found",
                     "size, timestamps, permissions and owner, as they were when read"),
                    ("workspace", "Keep the originals",
                     "archive every original and extraction with its SHA-256"),
                    ("include_text", "Carry the full text",
                     "put each document's whole text in the CSV as well")):
                box = QtWidgets.QCheckBox(text)
                box.setChecked(getattr(self.session.options, key))
                box.setCursor(QtCore.Qt.PointingHandCursor)
                self.boxes[key] = box
                why_label = QtWidgets.QLabel(why)
                why_label.setObjectName("optWhy")
                row = QtWidgets.QFrame()
                row.setObjectName("optRow")
                rl = QtWidgets.QVBoxLayout(row)
                rl.setContentsMargins(14, 11, 14, 12)
                rl.setSpacing(2)
                rl.addWidget(box)
                indent = QtWidgets.QHBoxLayout()
                indent.setContentsMargins(26, 0, 0, 0)
                why_label.setWordWrap(True)
                indent.addWidget(why_label)
                rl.addLayout(indent)
                lay.addWidget(row)
                lay.addSpacing(8)
            return card

        # -- behaviour
        def _choose(self):
            chosen = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Choose a folder of documents")
            if chosen:
                self._load([chosen], add=True)

        def _clear(self):
            self.session = Session()
            for key, box in self.boxes.items():
                setattr(self.session.options, key, box.isChecked())
            self.summary.hide()
            self.zone.title.setText("Drop a folder of documents here")
            self.run_btn.setEnabled(False)
            self.report_btn.setEnabled(False)
            self.files_btn.setEnabled(False)
            self._clear_results()
            self._say("")
            self._progress_through("start")

        def _load(self, raw, add=True):
            """Drops ADD. Four folders dropped one at a time are one job."""
            try:
                folders = folders_from_drop(raw)
                index = self.session.load(folders, add=add)
            except DropError as exc:
                self._say(str(exc), MAGENTA)
                return
            if len(index.folders) == 1:
                self.folder_path.setText(index.folders[0])
            else:
                self.folder_path.setText("\n".join(
                    f"{f}   ({index.per_folder.get(f, 0)})"
                    for f in index.folders))
            self.index_line.setText(index.summary())
            self.summary.show()
            self.zone.title.setText("Drop another folder to add it")
            self.clear_btn.setVisible(True)
            self.run_btn.setEnabled(bool(index.count))
            self.report_btn.setEnabled(False)
            self.files_btn.setEnabled(False)
            self._clear_results()
            self._progress_through("loaded")
            self._say("" if index.count
                      else "nothing in that folder can be read", MAGENTA)

        def _primary(self):
            """The run button is Stop while a scan is running, Run otherwise."""
            if self._running:
                self._stop()
            else:
                self._run()

        def _run(self):
            for key, box in self.boxes.items():
                setattr(self.session.options, key, box.isChecked())
            self._running = True
            self.run_btn.setText("Stop")            # the button now cancels
            self.report_btn.setEnabled(False)
            self.files_btn.setEnabled(False)
            self._clear_results()
            self.bar.setRange(0, max(self.session.index.count, 1))
            self.bar.setValue(0)
            self.bar.show()
            self.worker = Worker(self.session)
            self.worker.tick.connect(self._tick)
            self.worker.stage.connect(lambda t: self._say(t, MUTED))
            self.worker.done.connect(self._finish)
            self.worker.failed.connect(self._failed)
            self.worker.cancelled.connect(self._stopped)
            self.worker.start()

        def _stop(self):
            if self.worker is not None:
                self.worker.cancel()
            self.run_btn.setEnabled(False)
            self.run_btn.setText("Stopping…")

        def _stopped(self):
            self._running = False
            self.bar.hide()
            self.run_btn.setEnabled(True)
            self.run_btn.setText("Run")
            # "discarded" would be a lie: nothing here deletes the dated
            # directory, and output_dir() cannot even name it after the fact
            # (it re-stamps the time on every call). Say what is true.
            self._say("stopped — partial output left in place", MUTED)

        def _tick(self, n, total, name):
            self.bar.setRange(0, max(total, 1))
            self.bar.setValue(n)
            self._say(f"{n} of {total}   ·   {name}", MUTED)

        def _failed(self, message):
            self._running = False
            self.bar.hide()
            self.run_btn.setEnabled(True)
            self.run_btn.setText("Run")
            self._say(message, MAGENTA)

        def _finish(self, result):
            self._running = False
            self.bar.hide()
            self.run_btn.setEnabled(True)
            self.run_btn.setText("Run again")
            self.report_btn.setEnabled(True)
            self.files_btn.setEnabled(True)
            self._say(f"written to {result.out_dir}", OKC)
            self._progress_through("ran")
            self._show(result)

        # -- results
        def _clear_results(self):
            while self.results.count():
                item = self.results.takeAt(0)
                widget = item.widget()
                if widget:
                    widget.deleteLater()
            self.results.addStretch(1)
            self.right.hide()

        def _show(self, result):
            card = QtWidgets.QFrame()
            card.setObjectName("card")
            lay = QtWidgets.QVBoxLayout(card)
            lay.setContentsMargins(22, 20, 22, 22)
            lay.setSpacing(0)

            head = QtWidgets.QLabel(result.headline())
            head.setObjectName("headline")
            lay.addWidget(head)

            if result.fields:
                lay.addSpacing(12)
                rule = QtWidgets.QFrame()
                rule.setObjectName("rule")
                rule.setFixedHeight(1)
                lay.addWidget(rule)
                lay.addSpacing(12)
                fields = QtWidgets.QLabel(
                    "Columns discovered:  " + " · ".join(result.fields))
                fields.setObjectName("fileLine")
                fields.setWordWrap(True)
                lay.addWidget(fields)

            if result.exceptions:
                lay.addSpacing(16)
                warn = QtWidgets.QFrame()
                warn.setObjectName("warnBox")
                wl = QtWidgets.QVBoxLayout(warn)
                wl.setContentsMargins(16, 13, 16, 14)
                wl.setSpacing(3)
                head2 = QtWidgets.QLabel(
                    f"{len(result.exceptions)} could not be read")
                head2.setObjectName("warnHead")
                wl.addWidget(head2)
                for exc in result.exceptions[:6]:
                    line = QtWidgets.QLabel(
                        f"{os.path.basename(str(exc.get('file', '')))}"
                        f"   —   {exc.get('reason', '')}")
                    line.setObjectName("warnLine")
                    wl.addWidget(line)
                if len(result.exceptions) > 6:
                    more = QtWidgets.QLabel(
                        f"and {len(result.exceptions) - 6} more, all in the report")
                    more.setObjectName("warnLine")
                    wl.addWidget(more)
                note = QtWidgets.QLabel(
                    "Each is named with a reason rather than returned as a "
                    "blank row — which is the whole point of the exceptions "
                    "table.")
                note.setObjectName("warnNote")
                note.setWordWrap(True)
                wl.addSpacing(6)
                wl.addWidget(note)
                lay.addWidget(warn)

            lay.addSpacing(16)
            cap = QtWidgets.QLabel("Written")
            cap.setObjectName("sectionCap")
            lay.addWidget(cap)
            lay.addSpacing(7)
            for name, path in (("review report", result.report_path),
                               ("file-state datasheet", result.datasheet_path),
                               ("results CSV", result.csv_path),
                               ("originals and hashes", result.workspace_path)):
                if not path:
                    continue
                row = QtWidgets.QFrame()
                row.setObjectName("writtenRow")
                rl = QtWidgets.QHBoxLayout(row)
                rl.setContentsMargins(13, 9, 13, 9)
                what = QtWidgets.QLabel(name)
                what.setObjectName("fileLine")
                where = QtWidgets.QLabel(os.path.basename(path))
                where.setObjectName("folderPath")
                rl.addWidget(what)
                rl.addStretch(1)
                rl.addWidget(where)
                lay.addWidget(row)
                lay.addSpacing(6)
            self.results.insertWidget(0, card)
            self.right.show()

        # -- opening
        def _open_report(self):
            r = self.session.result
            if r and r.report_path and os.path.exists(r.report_path):
                webbrowser.open("file://" + os.path.abspath(r.report_path))

        def _open_out(self):
            r = self.session.result
            if not r:
                return
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(os.path.abspath(r.out_dir)))

        def _say(self, text, colour=MUTED):
            self.status.setText(text)
            self.status.setStyleSheet(f"color: {colour};")

    return Window(session or Session())


def main(argv=None):
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError:
        raise SystemExit(_missing_toolkit_message())
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("Vanilla Extract")
    window = build((QtCore, QtGui, QtWidgets))
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
