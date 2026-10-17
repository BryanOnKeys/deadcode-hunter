"""Self-contained HTML report generation (M3).

Produces a single HTML file with inline CSS/JS -- zero external
dependencies, no CDN, openable by double-clicking. No network access,
no LLM. All dynamic content is escaped with ``html.escape``.
"""

import html
import json
from pathlib import Path

from .models import Candidate
from .prover import BREAKS, PROVEN, SKIPPED

_CONTEXT_LINES = 3  # source lines shown around a dead symbol

# ---------------------------------------------------------------------------
# data assembly
# ---------------------------------------------------------------------------


def load_prove_results(prove_json_path: str | Path) -> tuple[dict, dict]:
    """Load a ``prove --format json`` payload.

    Returns ``(by_key, summary)`` where ``by_key`` maps
    ``(file, symbol_qualname)`` to the prove result dict and ``summary``
    holds the proven/breaks/skipped counts.
    """
    payload = json.loads(Path(prove_json_path).read_text(encoding="utf-8"))
    by_key: dict[tuple[str, str], dict] = {}
    for result in payload.get("results", []):
        by_key[(result.get("file", ""), result.get("symbol", ""))] = result
    return by_key, payload.get("summary", {})


def _read_snippet(
    root: Path, rel_file: str, lineno: int, end_lineno: int
) -> tuple[list[str], set[int], int]:
    """Read source lines around a symbol.

    Returns ``(lines, dead_line_nos, start_no)``: the display lines
    (with a few context lines), the 1-based line numbers to highlight,
    and the 1-based number of the first displayed line. Returns empty
    lines when the file cannot be read.
    """
    try:
        text = (root / rel_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], set(), 0
    src_lines = text.splitlines()
    end_lineno = end_lineno or lineno
    start = max(0, lineno - 1 - _CONTEXT_LINES)
    end = min(len(src_lines), end_lineno + _CONTEXT_LINES)
    return src_lines[start:end], set(range(lineno, end_lineno + 1)), start + 1


def build_report_data(
    candidates: list[Candidate],
    root: str | Path,
    prove_json: str | Path | None = None,
) -> dict:
    """Assemble everything the HTML template needs."""
    root_path = Path(root)
    prove_by_key, prove_summary = (
        load_prove_results(prove_json) if prove_json else ({}, {})
    )
    ordered = sorted(candidates, key=lambda c: (c.symbol.file, c.symbol.lineno))
    items = []
    for cand in ordered:
        sym = cand.symbol
        lines, dead_nos, start_no = _read_snippet(
            root_path, sym.file, sym.lineno, sym.end_lineno
        )
        prove = prove_by_key.get((sym.file, sym.qualname))
        items.append(
            {
                "file": sym.file,
                "qualname": sym.qualname,
                "name": sym.name,
                "kind": sym.kind,
                "confidence": cand.confidence,
                "lineno": sym.lineno,
                "evidence": list(cand.evidence),
                "lines": lines,
                "dead_nos": dead_nos,
                "start_no": start_no,
                "prove_status": prove.get("status") if prove else None,
                "prove_reason": (prove.get("reason") or "") if prove else "",
            }
        )
    counts = {"high": 0, "medium": 0, "low": 0}
    per_file: dict[str, int] = {}
    for cand in ordered:
        counts[cand.confidence] = counts.get(cand.confidence, 0) + 1
        per_file[cand.symbol.file] = per_file.get(cand.symbol.file, 0) + 1
    return {
        "items": items,
        "total": len(ordered),
        "counts": counts,
        "per_file": per_file,
        "prove_summary": prove_summary,
    }


# ---------------------------------------------------------------------------
# HTML rendering (hand-written template, no jinja2)
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #ffffff;
  --fg: #1f2328;
  --muted: #59636e;
  --card: #f6f8fa;
  --border: #d1d9e0;
  --code-bg: #f6f8fa;
  --hl: #fff8c5;
  --high: #cf222e;
  --medium: #9a6700;
  --low: #59636e;
  --proven: #1a7f37;
  --breaks: #cf222e;
  --skipped: #59636e;
  --accent: #0969da;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #8b949e;
    --card: #161b22;
    --border: #30363d;
    --code-bg: #161b22;
    --hl: #3b2f04;
    --high: #f85149;
    --medium: #d29922;
    --low: #8b949e;
    --proven: #3fb950;
    --breaks: #f85149;
    --skipped: #8b949e;
    --accent: #4493f8;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.5;
}
.topbar { padding: 20px 24px 0; max-width: 1200px; margin: 0 auto; }
.topbar h1 { font-size: 22px; margin: 0; }
.topbar h1 .sub { color: var(--muted); font-weight: 400; }
.topbar .path { color: var(--muted); font-size: 13px; margin-top: 4px;
  overflow-wrap: anywhere; }
.summary { display: flex; flex-wrap: wrap; gap: 12px; padding: 16px 24px 0;
  max-width: 1200px; margin: 0 auto; }
.stat { background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 18px; min-width: 96px; text-align: center; }
.stat .num { font-size: 24px; font-weight: 700; }
.stat .lbl { font-size: 12px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.04em; }
.stat .num.c-high { color: var(--high); }
.stat .num.c-medium { color: var(--medium); }
.stat .num.c-low { color: var(--low); }
.stat .num.c-proven { color: var(--proven); }
.stat .num.c-breaks { color: var(--breaks); }
.stat .num.c-skipped { color: var(--skipped); }
.byfile { max-width: 1200px; margin: 0 auto; padding: 16px 24px 0; }
.byfile h2, aside h2 { font-size: 14px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; margin: 0 0 8px; }
.byfile table { border-collapse: collapse; font-size: 14px; }
.byfile td { padding: 2px 16px 2px 0; }
.byfile td.n { text-align: right; font-variant-numeric: tabular-nums; }
.layout { display: flex; gap: 20px; max-width: 1200px; margin: 0 auto;
  padding: 16px 24px 40px; align-items: flex-start; }
aside { flex: 0 0 230px; position: sticky; top: 12px; }
.filetree { list-style: none; margin: 0; padding: 0; }
.filetree button { display: flex; justify-content: space-between; width: 100%;
  background: none; border: 1px solid transparent; border-radius: 6px;
  color: var(--fg); font-size: 13px; padding: 6px 8px; cursor: pointer;
  text-align: left; }
.filetree button:hover { background: var(--card); }
.filetree button.active { border-color: var(--accent); background: var(--card); }
.filetree .cnt { background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 0 8px; font-size: 12px; color: var(--muted); }
main { flex: 1; min-width: 0; }
#search { width: 100%; padding: 8px 12px; font-size: 14px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--card); color: var(--fg);
  margin-bottom: 12px; }
.card { border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px;
  background: var(--bg); overflow: hidden; }
.card summary { padding: 10px 14px; cursor: pointer; list-style: none;
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.card summary::-webkit-details-marker { display: none; }
.card summary:hover { background: var(--card); }
.qname { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }
.kind { font-size: 12px; color: var(--muted); }
.loc { font-size: 12px; color: var(--muted); margin-left: auto;
  font-family: ui-monospace, Menlo, Consolas, monospace; }
.badge { font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.04em; border-radius: 10px; padding: 2px 9px;
  border: 1px solid currentColor; white-space: nowrap; }
.badge.conf-high { color: var(--high); }
.badge.conf-medium { color: var(--medium); }
.badge.conf-low { color: var(--low); }
.badge.prove-proven { color: var(--proven); }
.badge.prove-breaks { color: var(--breaks); }
.badge.prove-skipped { color: var(--skipped); }
.card .body { padding: 0 14px 14px; border-top: 1px solid var(--border); }
.card h4 { font-size: 12px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.05em; margin: 12px 0 6px; }
.evidence { margin: 0; padding-left: 18px; font-size: 13px; }
.evidence li { margin-bottom: 2px; }
pre.code { background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 8px 0; overflow-x: auto; font-size: 13px; margin: 0; }
pre.code .line { display: block; padding: 0 12px 0 0; white-space: pre; }
pre.code .line.dead { background: var(--hl); }
pre.code .ln { display: inline-block; width: 3.5em; text-align: right;
  margin-right: 12px; color: var(--muted); user-select: none; }
.no-source { color: var(--muted); font-size: 13px; font-style: italic; }
#empty { color: var(--muted); }
footer { text-align: center; color: var(--muted); font-size: 12px;
  padding: 0 24px 24px; }
@media (max-width: 760px) {
  .layout { flex-direction: column; }
  aside { flex: none; width: 100%; position: static; }
}
"""


def _badge(kind: str, value: str, text: str) -> str:
    return (
        f'<span class="badge {kind}-{html.escape(value, quote=True)}">'
        f"{html.escape(text)}</span>"
    )


def _render_summary(data: dict) -> str:
    counts = data["counts"]
    parts = ['<section class="summary" aria-label="summary">']
    parts.append(
        f'<div class="stat"><div class="num" id="stat-total">{data["total"]}</div>'
        '<div class="lbl">candidates</div></div>'
    )
    for level in ("high", "medium", "low"):
        n = counts.get(level, 0)
        parts.append(
            f'<div class="stat"><div class="num c-{level}" id="stat-{level}">{n}</div>'
            f'<div class="lbl">{level}</div></div>'
        )
    prove = data["prove_summary"]
    if prove:
        for key, label in ((PROVEN, "proven"), (BREAKS, "breaks"), (SKIPPED, "skipped")):
            n = prove.get(key, 0)
            parts.append(
                f'<div class="stat"><div class="num c-{key}" id="stat-{key}">{n}</div>'
                f'<div class="lbl">{label}</div></div>'
            )
    parts.append("</section>")
    per_file = data["per_file"]
    if per_file:
        rows = "".join(
            f"<tr><td>{html.escape(f)}</td>"
            f'<td class="n">{n}</td></tr>'
            for f, n in sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        parts.append(
            '<section class="byfile" aria-label="candidates by file">'
            "<h2>By file</h2>"
            f"<table>{rows}</table>"
            "</section>"
        )
    return "".join(parts)


def _render_filetree(data: dict) -> str:
    per_file = data["per_file"]
    items = [
        '<li><button type="button" data-file="" class="active">'
        f"All files<span class=\"cnt\">{data['total']}</span></button></li>"
    ]
    for fname in sorted(per_file):
        items.append(
            "<li><button type=\"button\" "
            f'data-file="{html.escape(fname, quote=True)}">'
            f"{html.escape(fname)}"
            f'<span class="cnt">{per_file[fname]}</span></button></li>'
        )
    return (
        "<aside><h2>Files</h2>"
        f'<ul class="filetree">{"".join(items)}</ul></aside>'
    )


def _render_code(lines: list[str], dead_nos: set[int], start_no: int) -> str:
    if not lines:
        return '<p class="no-source">(source unavailable)</p>'
    out = ['<pre class="code">']
    for i, line in enumerate(lines):
        lineno = start_no + i
        cls = "line dead" if lineno in dead_nos else "line"
        out.append(
            f'<span class="{cls}"><span class="ln">{lineno}</span>'
            f"{html.escape(line) or ' '}</span>"
        )
    out.append("</pre>")
    return "".join(out)


def _render_card(item: dict) -> str:
    badges = _badge("conf", item["confidence"], item["confidence"])
    if item["prove_status"]:
        mark = {"proven": " ✓", "breaks": " ✗"}.get(item["prove_status"], "")
        badges += _badge("prove", item["prove_status"], item["prove_status"] + mark)
    evidence = "".join(f"<li>{html.escape(e)}</li>" for e in item["evidence"])
    code = _render_code(item["lines"], item["dead_nos"], item["start_no"])
    reason = (
        f"<p>{html.escape(item['prove_reason'])}</p>" if item["prove_reason"] else ""
    )
    return (
        f'<article class="card" data-file="{html.escape(item["file"], quote=True)}" '
        f'data-name="{html.escape(item["qualname"].lower(), quote=True)}">'
        "<details><summary>"
        f'<span class="qname">{html.escape(item["qualname"])}</span>'
        f'<span class="kind">{html.escape(item["kind"])}</span>'
        f"{badges}"
        f'<span class="loc">{html.escape(item["file"])}:{item["lineno"]}</span>'
        "</summary>"
        '<div class="body">'
        "<h4>Evidence</h4>"
        f'<ul class="evidence">{evidence}</ul>'
        f"{reason}"
        "<h4>Source</h4>"
        f"{code}"
        "</div></details></article>"
    )


_JS = """
(function () {
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var search = document.getElementById('search');
  var empty = document.getElementById('empty');
  var file = '';
  function apply() {
    var q = search.value.trim().toLowerCase();
    var shown = 0;
    cards.forEach(function (c) {
      var okF = !file || c.getAttribute('data-file') === file;
      var okQ = !q || c.getAttribute('data-name').indexOf(q) !== -1;
      var show = okF && okQ;
      c.style.display = show ? '' : 'none';
      if (show) shown++;
    });
    empty.hidden = shown !== 0;
    document.querySelectorAll('.filetree button').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-file') === file);
    });
  }
  search.addEventListener('input', apply);
  document.querySelectorAll('.filetree button').forEach(function (b) {
    b.addEventListener('click', function () {
      file = b.getAttribute('data-file');
      apply();
    });
  });
})();
"""


def render_html(data: dict, path_label: str) -> str:
    """Render the full self-contained HTML page."""
    cards = "".join(_render_card(item) for item in data["items"])
    page = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>deadcode-hunter report</title>\n"
        "<style>" + _CSS + "</style>\n"
        "</head>\n<body>\n"
        '<header class="topbar"><h1>deadcode-hunter <span class="sub">report</span></h1>'
        f'<div class="path">{html.escape(path_label)}</div></header>\n'
        + _render_summary(data)
        + '<div class="layout">\n'
        + _render_filetree(data)
        + "<main>\n"
        '<input id="search" type="search" placeholder="Filter by symbol name\u2026" '
        'aria-label="Filter by symbol name">\n'
        '<p id="empty" hidden>No candidates match the current filter.</p>\n'
        + cards
        + "</main>\n</div>\n"
        "<footer>generated by deadcode-hunter \u2014 static analysis, no network, no LLM</footer>\n"
        "<script>" + _JS + "</script>\n"
        "</body>\n</html>\n"
    )
    return page


def write_report(
    candidates: list[Candidate],
    root: str | Path,
    output_path: str | Path,
    prove_json: str | Path | None = None,
    path_label: str = "",
) -> Path:
    """Build report data and write the HTML file. Returns the output path."""
    data = build_report_data(candidates, root, prove_json=prove_json)
    out = Path(output_path)
    out.write_text(
        render_html(data, path_label or str(root)), encoding="utf-8"
    )
    return out
