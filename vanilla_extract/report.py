"""Self-contained HTML report: preview, correct, re-export.

A CSV is a poor delivery format for extraction work, because the client cannot
see WHERE a value came from. When a number looks wrong they have no way to
check it short of opening the original document, so they either trust
everything or trust nothing.

This writes one HTML file with no external assets -- openable from a file://
URL, e-mailable, no server -- containing:

  * the results table, with every cell editable in place;
  * the source text of each document, one click away, so a suspect value can
    be checked against the page it came from;
  * the exceptions table, given equal billing rather than a footnote;
  * a button that exports the corrected table back out as CSV.

The edits live in the page. Nothing is sent anywhere -- there is nowhere to
send it to -- so a client can review a delivery containing their own sensitive
documents without it leaving their machine.
"""

import datetime
import html
import json


_CSS = """
:root{--bg:#f7f7f6;--fg:#1a1a18;--mut:#6b6b66;--line:#dcdcd6;--card:#fff;
      --warn:#8a5a00;--warnbg:#fdf4e3;--accent:#1f4d8f}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
      --bg:#16161a;--fg:#e8e8e4;--mut:#9a9a94;--line:#32323a;--card:#1e1e24;
      --warn:#e0b060;--warnbg:#2a2318;--accent:#7aa8e8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,
     BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;padding:24px 16px}
.wrap{max-width:1200px;margin:0 auto}
h1{font-size:20px;margin:0 0 2px}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;
      padding:10px 14px;min-width:110px}
.stat b{display:block;font-size:21px;line-height:1.2}
.stat span{color:var(--mut);font-size:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
input[type=search]{flex:1;min-width:180px;padding:8px 10px;border:1px solid var(--line);
      border-radius:7px;background:var(--card);color:var(--fg);font-size:13px}
button{padding:8px 13px;border:1px solid var(--line);border-radius:7px;
      background:var(--card);color:var(--fg);font-size:13px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:hover{filter:brightness(1.06)}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);
      border-radius:9px;margin-bottom:22px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);
      vertical-align:top}
th{font-weight:600;font-size:12px;color:var(--mut);text-transform:uppercase;
   letter-spacing:.04em;white-space:nowrap;position:sticky;top:0;background:var(--card)}
td[contenteditable]{min-width:90px}
td[contenteditable]:focus{outline:2px solid var(--accent);outline-offset:-2px;border-radius:3px}
td.edited{background:color-mix(in srgb,var(--accent) 12%,transparent)}
tr:last-child td{border-bottom:none}
.file{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
      max-width:280px;overflow-wrap:anywhere}
.nofields{display:inline-block;border-radius:20px;padding:1px 8px;margin-right:6px;
      font-size:11px;background:var(--warnbg);color:var(--warn);white-space:nowrap;
      border:1px solid transparent;cursor:help}
.view{cursor:pointer;color:var(--accent);text-decoration:underline;
      background:none;border:none;padding:0;font-size:12px}
h2{font-size:15px;margin:0 0 9px}
h2 .count{color:var(--mut);font-weight:400}
.exc{background:var(--warnbg)}
.reason{color:var(--warn);font-weight:600;white-space:nowrap}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--card);
      color:var(--fg);max-width:min(900px,92vw);width:100%;padding:0}
dialog::backdrop{background:rgba(0,0,0,.5)}
.dlghead{display:flex;justify-content:space-between;align-items:center;gap:12px;
      padding:12px 16px;border-bottom:1px solid var(--line)}
.dlghead strong{font-size:13px;font-family:ui-monospace,Menlo,monospace;
      overflow-wrap:anywhere}
pre{margin:0;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere;
      max-height:65vh;overflow:auto;font:12px/1.55 ui-monospace,Menlo,monospace}
.empty{padding:16px;color:var(--mut)}
@media(max-width:640px){body{padding:16px 12px}.file{max-width:150px}}
"""

_JS = """
const DOCS = __DOCS__;
document.querySelectorAll('td[contenteditable]').forEach(td=>{
  const original = td.textContent;
  td.addEventListener('input',()=>{
    td.classList.toggle('edited', td.textContent !== original);
  });
});
const search = document.getElementById('q');
if(search){
  search.addEventListener('input',()=>{
    const q = search.value.toLowerCase();
    document.querySelectorAll('tbody tr').forEach(tr=>{
      tr.hidden = q && !tr.textContent.toLowerCase().includes(q);
    });
  });
}
const dlg = document.getElementById('preview');
document.querySelectorAll('.view').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const i = +btn.dataset.i;
    document.getElementById('dlgfile').textContent = DOCS[i].file;
    document.getElementById('dlgtext').textContent =
      DOCS[i].text || '(no text stored in this report)';
    dlg.showModal();
  });
});
document.getElementById('dlgclose')?.addEventListener('click',()=>dlg.close());

function csvCell(s){
  s = (s ?? '').toString();
  // Same formula-injection guard the Python writer applies: a cell starting
  // = + - @ tab or CR is evaluated by Excel and Sheets.
  if(/^[=+\\-@\\t\\r]/.test(s)) s = "'" + s;
  return /[",\\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s;
}
document.getElementById('export')?.addEventListener('click',()=>{
  // The header must lead, or DictReader on the other side eats the first
  // DOCUMENT as field names: that row vanishes from the revision, every key
  // is wrong, and the diff reports the whole table as removed-and-re-added.
  // #results is the tbody, so the header has to be gathered separately.
  const head=[...document.querySelectorAll('#resultshead th')]
    .filter(th=>!th.classList.contains('noexport'))
    .map(th=>csvCell(th.dataset.col ?? th.textContent.trim())).join(',');
  const rows=[head, ...[...document.querySelectorAll('#results tr')].map(tr=>
    [...tr.children].filter(c=>!c.classList.contains('noexport'))
      // dataset.full carries the document's full path where the cell shows
      // only its name. Exporting what is displayed would write basenames into
      // the corrected CSV, and --import-csv matches a revision to its
      // originals by path -- so the whole table would fail to match.
      .map(c=>csvCell((c.dataset.full ?? c.innerText).trim())).join(','))];
  const blob=new Blob([rows.join('\\n')],{type:'text/csv'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='corrected.csv';
  a.click();
  URL.revokeObjectURL(a.href);
});
"""


def _esc(value):
    return html.escape("" if value is None else str(value))


def _json_for_script(data):
    r"""JSON safe to interpolate into an inline <script> block.

    json.dumps does not escape `</script>`, so a document containing that
    string closes the block and everything after it is parsed as markup. The
    report is built to be e-mailed to clients and carries all of their
    extracted text, so this let a malicious document run script in the
    reviewer's browser with the whole delivery in reach.

    U+2028 and U+2029 are also escaped: both are line terminators in
    JavaScript but legal inside a JSON string.
    """
    return (json.dumps(data)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def write_report(results, exceptions, path, columns=None, title="Extraction report",
                 include_text=True):
    """Write a self-contained HTML report. Returns `path`."""
    results = list(results)
    exceptions = list(exceptions)
    if columns is None:
        columns = [c for c in (results[0].keys() if results else ["file"])
                   if c != "text"]

    docs = [{"file": r.get("file", ""),
             "text": (r.get("text", "") if include_text else "")}
            for r in results]

    head = "".join(f'<th data-col="{_esc(c)}">{_esc(c)}</th>' for c in columns)

    # Columns the tool computed rather than read out of the document. Making
    # them editable invites a reviewer to correct a number the tool derived,
    # and the correction then travels into the exported CSV as though it were
    # a value from the document.
    derived = {"file", "characters"}
    value_columns = [c for c in columns if c not in derived]

    body = []
    for i, row in enumerate(results):
        cells = []
        for col in columns:
            editable = "" if col in derived else ' contenteditable="plaintext-only"'
            if col == "file":
                # Show the name; keep the path. A directory of documents has
                # one long path prefix repeated on every row, which pushes the
                # values a reviewer is here to read off the side of the table.
                # The full path stays in the title and in the export, because
                # it is what identifies the document.
                full = str(row.get(col, ""))
                shown = full.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or full
                cells.append(f'<td class="file" title="{_esc(full)}" '
                             f'data-full="{_esc(full)}">{_esc(shown)}</td>')
                continue
            cells.append(f"<td{editable}>{_esc(row.get(col, ''))}</td>")
        # A row where no field matched is not the same as a row of empty
        # values, and a reviewer cannot tell them apart from blank cells.
        found = any(str(row.get(c, "")).strip() for c in value_columns)
        note = "" if found or not value_columns else (
            '<span class="nofields" title="This document was read, but none of '
            'the discovered labels appear in it -- often a different vendor\'s '
            'layout. Its full text is in the source view.">no fields matched'
            "</span> ")
        cells.append(f'<td class="noexport">{note}<button class="view" data-i="{i}">'
                     f"view source</button></td>")
        body.append("<tr>" + "".join(cells) + "</tr>")

    exc_rows = "".join(
        f'<tr class="exc"><td class="file">{_esc(e.get("file", ""))}</td>'
        f'<td class="reason">{_esc(e.get("reason", ""))}</td>'
        f'<td>{_esc(e.get("detail", ""))}</td></tr>'
        for e in exceptions)

    total = len(results) + len(exceptions)
    rate = f"{100 * len(results) / total:.0f}%" if total else "--"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    exc_section = (
        f'<h2>Could not be read <span class="count">({len(exceptions)})</span></h2>'
        f'<div class="tablewrap"><table><thead><tr><th>File</th><th>Reason</th>'
        f"<th>Detail</th></tr></thead><tbody>{exc_rows}</tbody></table></div>"
        if exceptions else
        '<h2>Could not be read <span class="count">(0)</span></h2>'
        '<div class="tablewrap"><p class="empty">Every document was read.</p></div>')

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{_esc(title)}</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>{_esc(title)}</h1>
<div class="sub">Generated {stamp} by vanilla-extract. Edits stay in this file; nothing is uploaded.</div>
<div class="stats">
  <div class="stat"><b>{len(results)}</b><span>documents read</span></div>
  <div class="stat"><b>{len(exceptions)}</b><span>could not be read</span></div>
  <div class="stat"><b>{rate}</b><span>read rate</span></div>
  <div class="stat"><b>{len(columns) - 1}</b><span>fields</span></div>
</div>
<div class="bar">
  <input type="search" id="q" placeholder="Filter rows...">
  <button class="primary" id="export">Export corrected CSV</button>
</div>
<h2>Extracted <span class="count">({len(results)})</span></h2>
<div class="tablewrap"><table><thead><tr id="resultshead">{head}<th class="noexport"></th></tr></thead>
<tbody id="results">{"".join(body) or '<tr><td class="empty">No documents read.</td></tr>'}</tbody>
</table></div>
{exc_section}
<dialog id="preview"><div class="dlghead"><strong id="dlgfile"></strong>
<button id="dlgclose">Close</button></div><pre id="dlgtext"></pre></dialog>
</div><script>{_JS.replace("__DOCS__", _json_for_script(docs))}</script></body></html>"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


_SHEET_JS = """
const rows = () => [...document.querySelectorAll('#sheet tbody tr')];
const q = document.getElementById('q');
q.addEventListener('input', () => {
  const needle = q.value.toLowerCase();
  let shown = 0;
  rows().forEach(tr => {
    const hit = !needle || tr.textContent.toLowerCase().includes(needle);
    tr.hidden = !hit;
    if (hit) shown++;
  });
  document.getElementById('count').textContent = shown + ' of ' + rows().length;
});

// Click a header to sort. Numeric columns sort numerically, which matters for
// size and for character counts -- lexical order puts "9 KB" after "10 MB".
let sortCol = -1, asc = true;
document.querySelectorAll('#sheet thead th').forEach((th, i) => {
  th.addEventListener('click', () => {
    asc = (sortCol === i) ? !asc : true;
    sortCol = i;
    const body = document.querySelector('#sheet tbody');
    const sorted = rows().sort((a, b) => {
      const x = a.children[i].dataset.sort ?? a.children[i].textContent.trim();
      const y = b.children[i].dataset.sort ?? b.children[i].textContent.trim();
      const nx = parseFloat(x), ny = parseFloat(y);
      const both = !isNaN(nx) && !isNaN(ny) && x !== '' && y !== '';
      const cmp = both ? nx - ny : x.localeCompare(y);
      return asc ? cmp : -cmp;
    });
    sorted.forEach(tr => body.appendChild(tr));
    document.querySelectorAll('#sheet thead th').forEach(h => h.dataset.dir = '');
    th.dataset.dir = asc ? 'up' : 'down';
  });
});

document.getElementById('export').addEventListener('click', () => {
  const head = [...document.querySelectorAll('#sheet thead th')]
      .map(th => th.dataset.col);
  const out = [head.map(csvCell).join(',')];
  rows().filter(tr => !tr.hidden).forEach(tr => {
    out.push([...tr.children].map(td => csvCell(td.textContent.trim())).join(','));
  });
  const blob = new Blob([out.join('\\n')], {type: 'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'datasheet.csv';
  a.click();
  URL.revokeObjectURL(a.href);
});

function csvCell(s){
  s = (s ?? '').toString();
  if(/^[=+\\-@\\t\\r]/.test(s)) s = "'" + s;
  return /[",\\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s;
}
"""

_SHEET_CSS = """
th{cursor:pointer;user-select:none}
th[data-dir=up]::after{content:" \\2191"}
th[data-dir=down]::after{content:" \\2193"}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.flag{color:var(--warn);font-weight:600}
.note{background:var(--warnbg);border:1px solid var(--line);border-radius:8px;
      padding:10px 13px;margin-bottom:16px;font-size:13px}
"""

_NUMERIC = {"size_bytes", "compressed_bytes", "compression_ratio", "hard_links",
            "inode", "owner_uid", "group_gid", "characters_extracted"}
_MONO = {"path", "permissions", "mode_octal", "crc32", "sha256", "modified",
         "created", "inode_changed", "accessed", "captured_at"}


def write_datasheet(rows, path, columns=None, title="File state datasheet"):
    """Write a searchable, sortable HTML datasheet of file metadata."""
    rows = list(rows)
    columns = columns or (list(rows[0].keys()) if rows else ["name"])

    # Drop columns that are empty for every file in this set. The record has
    # 29 fields because a file can have 29 interesting properties, not because
    # any one file has; a corpus with no archives carries four permanently
    # blank columns, and on a narrow screen those are four columns of
    # horizontal scrolling between the reader and the data. Nothing is
    # invented and nothing is summarised -- the CSV form still carries every
    # field, and the page says which ones it left out.
    def _blank(value):
        return value is None or str(value).strip() == ""

    dropped = [c for c in columns
               if rows and all(_blank(r.get(c)) for r in rows)]
    if dropped:
        columns = [c for c in columns if c not in dropped]

    head = "".join(
        f'<th data-col="{_esc(c)}">{_esc(c.replace("_", " "))}</th>' for c in columns)

    body = []
    for row in rows:
        cells = []
        for col in columns:
            value = row.get(col)
            shown = "" if value is None else str(value)
            cls = "num" if col in _NUMERIC else ("mono" if col in _MONO else "")
            if col == "ownership_reliable" and value is False:
                cells.append('<td class="flag">not reliable</td>')
                continue
            sort_attr = f' data-sort="{_esc(row.get("size_bytes", ""))}"' if col == "size" else ""
            cells.append(f'<td class="{cls}"{sort_attr}>{_esc(shown)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")

    # A ZIP member has no owner and no creation time BY FORMAT, which is a
    # different fact from a mounted NTFS volume synthesising them. Counting
    # both into one banner told the reader something false about where their
    # documents live.
    members = [r for r in rows if r.get("source") == "zip-central-directory"]
    on_disk = [r for r in rows if r.get("source") != "zip-central-directory"]
    unreliable = sum(1 for r in on_disk if r.get("ownership_reliable") is False)
    no_created = sum(1 for r in on_disk if not r.get("created"))
    notes = []
    if dropped:
        shown = ", ".join(c.replace("_", " ") for c in dropped)
        notes.append(
            f"<strong>{len(dropped)} column"
            f"{'s are' if len(dropped) != 1 else ' is'} not shown</strong> "
            f"because no file in this set has a value for "
            f"{'them' if len(dropped) != 1 else 'it'}: {_esc(shown)}. "
            f"The CSV form of this datasheet still carries every field.")
    if members:
        notes.append(
            f"<strong>{len(members)} of {len(rows)} entries</strong> were found "
            f"inside ZIP archives. The archive format records no owner and no "
            f"creation time, so those columns are empty for them by format "
            f"rather than by any limitation of this machine.")
    if unreliable:
        notes.append(
            f"<strong>{unreliable} of {len(on_disk)} files on disk</strong> sit on a filesystem "
            f"that reports ownership and permissions from mount options rather than "
            f"from the files themselves (NTFS, exFAT, SMB and similar). Those "
            f"columns are marked <span class=\"flag\">not reliable</span> and should "
            f"not be read as the file's real attributes.")
    if no_created:
        notes.append(
            f"<strong>{no_created} of {len(on_disk)} files on disk</strong> have no creation "
            f"time. Creation time is not universally available: Windows records it, "
            f"macOS and the BSDs expose it, and on Linux it exists only on some "
            f"filesystems. The <em>created source</em> column says where each value "
            f"came from, and the field is left empty rather than substituting the "
            f"inode change time, which is a different fact.")

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{_esc(title)}</title><style>{_CSS}{_SHEET_CSS}</style></head><body><div class="wrap">
<h1>{_esc(title)}</h1>
<div class="sub">{len(rows)} files &middot; state as found, captured
{datetime.datetime.now().strftime("%Y-%m-%d %H:%M")} &middot; click a column to sort</div>
{"".join(f'<div class="note">{n}</div>' for n in notes)}
<div class="bar">
  <input type="search" id="q" placeholder="Filter by name, owner, date, permissions...">
  <button class="primary" id="export">Export filtered CSV</button>
  <span class="sub" id="count" style="margin:0">{len(rows)} of {len(rows)}</span>
</div>
<div class="tablewrap"><table id="sheet"><thead><tr>{head}</tr></thead>
<tbody>{"".join(body) or '<tr><td class="empty">No files.</td></tr>'}</tbody></table></div>
</div><script>{_SHEET_JS}</script></body></html>"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path
