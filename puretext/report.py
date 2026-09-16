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
  return /[",\\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s;
}
document.getElementById('export')?.addEventListener('click',()=>{
  const rows=[...document.querySelectorAll('#results tr')].map(tr=>
    [...tr.children].filter(c=>!c.classList.contains('noexport'))
      .map(c=>csvCell(c.innerText.trim())).join(','));
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

    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = []
    for i, row in enumerate(results):
        cells = []
        for col in columns:
            editable = "" if col == "file" else ' contenteditable="plaintext-only"'
            cls = ' class="file"' if col == "file" else ""
            cells.append(f"<td{cls}{editable}>{_esc(row.get(col, ''))}</td>")
        cells.append(f'<td class="noexport"><button class="view" data-i="{i}">'
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
<div class="sub">Generated {stamp} by puretext. Edits stay in this file; nothing is uploaded.</div>
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
<div class="tablewrap"><table><thead><tr>{head}<th class="noexport"></th></tr></thead>
<tbody id="results">{"".join(body) or '<tr><td class="empty">No documents read.</td></tr>'}</tbody>
</table></div>
{exc_section}
<dialog id="preview"><div class="dlghead"><strong id="dlgfile"></strong>
<button id="dlgclose">Close</button></div><pre id="dlgtext"></pre></dialog>
</div><script>{_JS.replace("__DOCS__", json.dumps(docs))}</script></body></html>"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path
