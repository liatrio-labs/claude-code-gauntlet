#!/usr/bin/env python3
"""bench/report.py — regenerable, self-contained HTML dashboard of deep-review
benchmark performance over time.

Reads ONLY the committed ledger (``bench/experiments.jsonl``) and baselines
(``bench/baselines.json``); emits one static HTML file with all CSS inline in a
single ``<style>``, charts as inline SVG, and a small inline tooltip script —
zero external requests. Re-run after appending a scored ledger row to refresh
the page::

    python3 bench/report.py [--ledger PATH] [--baselines PATH] [--out PATH]

stdlib only (CLAUDE.md): no pip dependencies, no language assumptions.
"""

import argparse
import html
import json
import subprocess
from datetime import date, datetime
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_LEDGER = BENCH_DIR / "experiments.jsonl"
DEFAULT_BASELINES = BENCH_DIR / "baselines.json"
DEFAULT_OUT = BENCH_DIR / "report.html"

# Metric fields that define a scored run's result; two ledger rows with an
# identical signature are byte-identical re-scores of the same candidate set.
METRIC_KEYS = (
    "golden_recall",
    "valid_extra_rate",
    "noise_rate",
    "precision_strict",
    "f1_strict",
)

# Over-time line series: (ledger field, display name, CSS custom property).
SERIES = (
    ("golden_recall", "Golden recall", "--series-golden"),
    ("noise_rate", "Noise rate", "--series-noise"),
    ("precision_strict", "Precision (strict)", "--series-precision"),
)

# vs-anchors bars: (label, CSS var, emphasized, source key). "v2" pulls from the
# baseline_v2 object; the rest from baselines.anchors.rows[<key>].
ANCHOR_TOOLS = (
    ("deep-review v2", "--tool-deepreview", True, "v2"),
    ("claude", "--tool-claude", False, "claude"),
    ("claude-code", "--tool-claude-code", False, "claude-code"),
    ("coderabbit", "--tool-coderabbit", False, "coderabbit"),
)

# vs-anchors metrics: (baseline_v2 field, anchors.rows field, display title).
ANCHOR_METRICS = (
    ("golden_recall", "recall", "Golden recall"),
    ("noise_rate", "noise_rate", "Noise rate"),
    ("precision_strict", "precision_strict", "Precision (strict)"),
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_ledger(path):
    """Parse the NDJSON ledger into a list of row dicts (blank lines skipped)."""
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_baselines(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def git_short_sha(cwd=None):
    """Short HEAD sha, or "uncommitted" if git is unavailable/not a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(cwd or BENCH_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        sha = out.stdout.strip()
        if out.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return "uncommitted"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def fmt_pct(x, digits=1):
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def fmt_money(x):
    if x is None:
        return "—"
    return f"${x:,.2f}"


def fmt_int(x):
    if x is None:
        return "—"
    return f"{int(round(x)):,}"


def fmt_date(ts):
    """ISO timestamp -> "Jul 18" (leading zero on day dropped)."""
    if not ts:
        return "—"
    try:
        d = datetime.strptime(ts[:10], "%Y-%m-%d")
    except ValueError:
        return "—"
    return f"{d.strftime('%b')} {d.day}"


def truncate_middle(s, max_len=30):
    if len(s) <= max_len:
        return s
    keep = max_len - 1
    head = (keep + 1) // 2
    tail = keep - head
    return s[:head] + "…" + s[-tail:]


def _metric_signature(row):
    return tuple(row.get(k) for k in METRIC_KEYS)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def deep_review_points(rows):
    """One point per scored deep-review-v2 run, time-ordered.

    Rows sharing a run_id with an identical metric signature (the k=5 judge
    re-scores) collapse into a single point carrying ``count`` and ``identical``.
    """
    groups = {}
    order = []
    for r in rows:
        if r.get("tool") != "deep-review-v2":
            continue
        rid = r.get("run_id")
        if rid not in groups:
            groups[rid] = []
            order.append(rid)
        groups[rid].append(r)
    points = []
    for rid in order:
        grp = groups[rid]
        rep = sorted(grp, key=lambda r: r.get("ts", ""))[0]
        points.append(
            {
                "run_id": rid,
                "row": rep,
                "count": len(grp),
                "identical": len({_metric_signature(r) for r in grp}) == 1,
                "ts": min(r.get("ts", "") for r in grp),
                "tier": rep.get("tier"),
            }
        )
    points.sort(key=lambda p: p["ts"])
    return points


def latest_subset_row(rows):
    """Most recent deep-review-v2 subset ledger row (source for the tiles)."""
    subset = [
        r
        for r in rows
        if r.get("tool") == "deep-review-v2" and r.get("tier") == "subset"
    ]
    if not subset:
        return None
    return max(subset, key=lambda r: r.get("ts", ""))


def ledger_groups(rows):
    """Table rows: collapse identical re-scores of the same (run_id, tool)."""
    groups = {}
    order = []
    for r in rows:
        key = (r.get("run_id"), r.get("tool"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    out = []
    for key in order:
        grp = groups[key]
        rep = sorted(grp, key=lambda r: r.get("ts", ""))[0]
        out.append(
            {
                "row": rep,
                "count": len(grp),
                "identical": len({_metric_signature(r) for r in grp}) == 1,
                "ts": min(r.get("ts", "") for r in grp),
            }
        )
    out.sort(key=lambda g: g["ts"])
    return out


# ---------------------------------------------------------------------------
# SVG builders
# ---------------------------------------------------------------------------
def _hbar_path(x, y, w, h, r, var):
    """Horizontal bar rounded on the value (right) end only, square at baseline."""
    if w <= 0:
        return ""
    r = min(r, w, h / 2)
    if r <= 0:
        d = f"M{x:.1f},{y:.1f} H{x + w:.1f} V{y + h:.1f} H{x:.1f} Z"
    else:
        d = (
            f"M{x:.1f},{y:.1f} "
            f"H{x + w - r:.1f} "
            f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} "
            f"Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} "
            f"H{x:.1f} Z"
        )
    return f'<path class="bar" d="{d}" style="fill:var({var})" />'


def build_over_time_svg(points):
    """Ordinal-x, percent-y line chart: three series across scored v2 runs."""
    W, H = 720, 300
    m_top, m_right, m_bottom, m_left = 24, 156, 46, 52
    inner_w = W - m_left - m_right
    inner_h = H - m_top - m_bottom
    n = len(points)

    def x_of(i):
        if n <= 1:
            return m_left + inner_w / 2
        return m_left + inner_w * i / (n - 1)

    def y_of(v):
        return m_top + (1 - v) * inner_h

    parts = [
        f'<svg class="chart" viewBox="0 0 {W} {H}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="Golden recall, noise rate and strict precision across '
        f'scored deep-review-v2 runs">'
    ]

    # Horizontal gridlines + percent axis labels.
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = y_of(t)
        cls = "baseline" if t == 0 else "grid"
        parts.append(
            f'<line class="{cls}" x1="{m_left:.1f}" y1="{y:.1f}" '
            f'x2="{m_left + inner_w:.1f}" y2="{y:.1f}" />'
        )
        parts.append(
            f'<text class="axis" x="{m_left - 8:.1f}" y="{y + 3.5:.1f}" '
            f'text-anchor="end">{int(t * 100)}%</text>'
        )

    # x tick labels: date + tier (+ collapse count).
    for i, p in enumerate(points):
        x = x_of(i)
        label = f"{fmt_date(p['ts'])} · {html.escape(str(p['tier']))}"
        if p["count"] > 1:
            label += f" ×{p['count']}"
        parts.append(
            f'<text class="axis" x="{x:.1f}" y="{m_top + inner_h + 18:.1f}" '
            f'text-anchor="middle">{html.escape(label)}</text>'
        )

    # Series lines, markers, hit targets; collect last points for direct labels.
    last_labels = []
    for key, name, var in SERIES:
        xy = []
        for i, p in enumerate(points):
            v = p["row"].get(key)
            if v is not None:
                xy.append((x_of(i), y_of(v), v, p))
        if not xy:
            continue
        if len(xy) >= 2:
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in xy)
            parts.append(
                f'<polyline class="line" points="{pts}" style="stroke:var({var})" />'
            )
        for x, y, v, p in xy:
            parts.append(
                f'<circle class="marker" cx="{x:.1f}" cy="{y:.1f}" r="5" '
                f'style="fill:var({var})" />'
            )
            tip = f"{name} · {fmt_date(p['ts'])} {p['tier']} · {fmt_pct(v)}"
            if p["count"] > 1 and p["identical"]:
                tip += f" · ×{p['count']} judge re-scores (identical)"
            tip = html.escape(tip)
            parts.append(
                f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="11" '
                f'tabindex="0" role="img" aria-label="{tip}" data-tip="{tip}" />'
            )
        last_labels.append([xy[-1][1], name, var, xy[-1][2]])

    # Declutter direct labels vertically, then draw swatch + ink text.
    last_labels.sort(key=lambda a: a[0])
    for i in range(1, len(last_labels)):
        if last_labels[i][0] - last_labels[i - 1][0] < 15:
            last_labels[i][0] = last_labels[i - 1][0] + 15
    label_x = m_left + inner_w + 12
    for ly, name, var, _lv in last_labels:
        parts.append(
            f'<circle class="swatch" cx="{label_x:.1f}" cy="{ly - 3:.1f}" r="4" '
            f'style="fill:var({var})" />'
        )
        parts.append(
            f'<text class="direct" x="{label_x + 10:.1f}" y="{ly:.1f}">'
            f"{html.escape(name)}</text>"
        )

    parts.append("</svg>")
    return "\n".join(parts)


def build_anchor_svg(title, bv2_key, anchor_key, baseline_v2, anchor_rows):
    """One small-multiple: 4 horizontal bars (v2 + three anchors) on a 0..1 scale."""
    W, H = 320, 150
    label_w = 96
    bar_x = label_w + 5
    val_w = 48
    bar_max_w = W - bar_x - val_w
    top = 26
    bar_h = 18
    gap = 2
    parts = [
        f'<svg class="chart" viewBox="0 0 {W} {H}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{html.escape(title)} by tool">'
    ]
    for idx, (label, var, emph, src) in enumerate(ANCHOR_TOOLS):
        v = baseline_v2.get(bv2_key) if src == "v2" else anchor_rows.get(src, {}).get(anchor_key)
        y = top + idx * (bar_h + gap)
        cls = "toollabel emph" if emph else "toollabel"
        parts.append(
            f'<text class="{cls}" x="{label_w:.1f}" y="{y + bar_h * 0.68:.1f}" '
            f'text-anchor="end">{html.escape(label)}</text>'
        )
        if v is None:
            continue
        w = max(0.0, min(1.0, v)) * bar_max_w
        parts.append(_hbar_path(bar_x, y, w, bar_h, 4, var))
        parts.append(
            f'<text class="barval" x="{bar_x + w + 5:.1f}" '
            f'y="{y + bar_h * 0.68:.1f}">{fmt_pct(v)}</text>'
        )
        tip = html.escape(f"{label} · {title} · {fmt_pct(v)}")
        parts.append(
            f'<rect class="hit" x="{bar_x:.1f}" y="{y:.1f}" '
            f'width="{max(w, 16):.1f}" height="{bar_h:.1f}" tabindex="0" '
            f'role="img" aria-label="{tip}" data-tip="{tip}" />'
        )
    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# HTML section builders
# ---------------------------------------------------------------------------
def build_legend_html():
    items = "".join(
        f'<span class="legend-item"><span class="legend-dot" '
        f'style="background:var({var})"></span>{html.escape(name)}</span>'
        for _, name, var in SERIES
    )
    return f'<div class="legend">{items}</div>'


def build_tiles_html(row, baselines):
    bv2 = baselines.get("baseline_v2", {})
    per_bucket = (row or {}).get("per_bucket", {})
    total = sum(per_bucket.values()) if per_bucket else None
    total_txt = str(total) if total is not None else "—"
    n_prs = (row or {}).get("n_prs", "—")
    n_goldens = bv2.get("n_goldens", "—")
    runs = bv2.get("runs", 1)
    g = (row or {}).get
    tiles = [
        (fmt_pct(g("golden_recall")), "Golden recall",
         f"{n_prs} gate PRs · {n_goldens} goldens · N={runs}"),
        (fmt_pct(g("noise_rate")), "Noise rate",
         f"{total_txt} candidates scored · N={runs}"),
        (fmt_pct(g("precision_strict")), "Precision (strict)",
         f"true positives ÷ {total_txt} candidates"),
        (fmt_pct(g("f1_strict")), "F1 (strict)",
         "recall + precision blend"),
        (fmt_money(g("cost_usd")), "Run cost",
         "one subset pass · review spend"),
    ]
    cells = "".join(
        '<div class="tile">'
        f'<div class="tile-value">{html.escape(value)}</div>'
        f'<div class="tile-label">{html.escape(label)}</div>'
        f'<div class="tile-caption">{html.escape(caption)}</div>'
        "</div>"
        for value, label, caption in tiles
    )
    return f'<div class="tiles">{cells}</div>'


def build_anchor_section_html(baselines):
    anchor_rows = baselines.get("anchors", {}).get("rows", {})
    bv2 = baselines.get("baseline_v2", {})
    charts = "".join(
        f'<figure class="anchor-chart"><figcaption>{html.escape(title)}</figcaption>'
        f"{build_anchor_svg(title, bv2_key, anchor_key, bv2, anchor_rows)}</figure>"
        for bv2_key, anchor_key, title in ANCHOR_METRICS
    )
    caption = (
        "Anchors are the upstream tools’ stored candidates re-judged under our "
        "pinned judge; <em>claude</em> is the published Claude Code CLI row. "
        "deep-review v2 is the current subset baseline."
    )
    return (
        f'<div class="anchor-row">{charts}</div>'
        f'<p class="caption">{caption}</p>'
    )


def build_table_html(groups):
    heads = [
        "Date", "Run", "Tool", "Tier", "PRs", "Recall", "Valid-extra",
        "Noise", "Precision", "F1", "Tokens", "Cost",
    ]
    thead = "".join(f"<th>{html.escape(h)}</th>" for h in heads)
    body = []
    for grp in groups:
        r = grp["row"]
        rid = str(r.get("run_id", ""))
        run = html.escape(truncate_middle(rid))
        note = ""
        if grp["count"] > 1 and grp["identical"]:
            note = f' <span class="note">×{grp["count"]} identical</span>'
        cells = (
            f'<td>{html.escape(fmt_date(r.get("ts", "")))}</td>'
            f'<td class="mono" title="{html.escape(rid)}">{run}{note}</td>'
            f'<td>{html.escape(str(r.get("tool", "")))}</td>'
            f'<td>{html.escape(str(r.get("tier", "")))}</td>'
            f'<td class="num">{html.escape(str(r.get("n_prs", "—")))}</td>'
            f'<td class="num">{fmt_pct(r.get("golden_recall"))}</td>'
            f'<td class="num">{fmt_pct(r.get("valid_extra_rate"))}</td>'
            f'<td class="num">{fmt_pct(r.get("noise_rate"))}</td>'
            f'<td class="num">{fmt_pct(r.get("precision_strict"))}</td>'
            f'<td class="num">{fmt_pct(r.get("f1_strict"))}</td>'
            f'<td class="num">{fmt_int(r.get("tokens_total"))}</td>'
            f'<td class="num">{fmt_money(r.get("cost_usd"))}</td>'
        )
        body.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-wrap"><table>'
        f"<thead><tr>{thead}</tr></thead>"
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def build_footnotes_html(baselines):
    judge = str(baselines.get("judge_pin", "—"))
    adj = str(baselines.get("adjudicator_pin", "—"))
    dn = baselines.get("delta_noise_proposed", {}) or {}
    dn_val = dn.get("value")
    dn_txt = fmt_pct(dn_val, 0) if isinstance(dn_val, (int, float)) else "—"
    pin_line = f'Judge &amp; adjudicator pinned to <span class="mono">{html.escape(judge)}</span>'
    if adj != judge:
        pin_line += f' / <span class="mono">{html.escape(adj)}</span>'
    pin_line += (
        " at temperature 0; the k=5 re-scores are byte-identical (judge_sd=0)."
    )
    items = [
        pin_line,
        "Owner-amended N=1 protocol: runs 2–3, baseline extension, and full-50 "
        "tracking were dropped under the budget cap.",
        f"δ_noise = {dn_txt} proposed {html.escape(str(dn.get('proposed', '')))}, "
        "pending owner sign-off.",
        "Costs are review-invocation spend only — judge/adjudicator scoring spend "
        "is not included in cost_usd.",
        "Source data: bench/experiments.jsonl (append-only ledger) and "
        "bench/baselines.json; see bench/README.md.",
    ]
    lis = "".join(f"<li>{it}</li>" for it in items)
    return f'<ol class="footnotes">{lis}</ol>'


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
CSS = """
*, *::before, *::after { box-sizing: border-box; }
:root { color-scheme: light dark; }
.viz-root {
  --surface: #fcfcfb;
  --page: #f9f9f7;
  --ink-primary: #0b0b0b;
  --ink-secondary: #52514e;
  --ink-muted: #898781;
  --hairline: rgba(11, 11, 11, .10);
  --grid: #e1e0d9;
  --baseline: #c3c2b7;
  --series-golden: #2a78d6;
  --series-noise: #e87ba4;
  --series-precision: #008300;
  --tool-deepreview: #2a78d6;
  --tool-claude: #008300;
  --tool-claude-code: #e87ba4;
  --tool-coderabbit: #eda100;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    --surface: #1a1a19;
    --page: #0d0d0d;
    --ink-primary: #ffffff;
    --ink-secondary: #c3c2b7;
    --ink-muted: #898781;
    --hairline: rgba(255, 255, 255, .10);
    --grid: #2c2c2a;
    --baseline: #383835;
    --series-golden: #3987e5;
    --series-noise: #d55181;
    --series-precision: #008300;
    --tool-deepreview: #3987e5;
    --tool-claude: #008300;
    --tool-claude-code: #d55181;
    --tool-coderabbit: #c98500;
  }
}
:root[data-theme="dark"] .viz-root {
  --surface: #1a1a19;
  --page: #0d0d0d;
  --ink-primary: #ffffff;
  --ink-secondary: #c3c2b7;
  --ink-muted: #898781;
  --hairline: rgba(255, 255, 255, .10);
  --grid: #2c2c2a;
  --baseline: #383835;
  --series-golden: #3987e5;
  --series-noise: #d55181;
  --series-precision: #008300;
  --tool-deepreview: #3987e5;
  --tool-claude: #008300;
  --tool-claude-code: #d55181;
  --tool-coderabbit: #c98500;
}
html, body { margin: 0; padding: 0; }
body {
  background: var(--page);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.viz-root {
  position: relative;
  max-width: 960px;
  margin: 0 auto;
  padding: 32px 20px 56px;
  background: var(--page);
  color: var(--ink-primary);
}
.viz-root h1 {
  margin: 0 0 4px;
  font-size: 26px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.subtitle {
  margin: 0 0 28px;
  color: var(--ink-secondary);
  font-size: 13px;
}
.viz-root h2 {
  margin: 40px 0 14px;
  font-size: 16px;
  font-weight: 600;
}
.caption {
  margin: 10px 2px 0;
  color: var(--ink-secondary);
  font-size: 12.5px;
}
.caption em { font-style: normal; font-weight: 600; }
.card {
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: 10px;
  padding: 18px 18px 12px;
}
/* Stat tiles */
.tiles {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
}
.tile {
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: 10px;
  padding: 14px 14px 12px;
}
.tile-value {
  font-size: 25px;
  font-weight: 640;
  font-variant-numeric: proportional-nums;
  letter-spacing: -0.01em;
}
.tile-label {
  margin-top: 3px;
  color: var(--ink-muted);
  font-size: 12px;
}
.tile-caption {
  margin-top: 6px;
  color: var(--ink-secondary);
  font-size: 11px;
  line-height: 1.35;
}
/* Legend */
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 10px;
  color: var(--ink-secondary);
  font-size: 12.5px;
}
.legend-item { display: inline-flex; align-items: center; }
.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
}
/* Charts */
.chart { display: block; overflow: visible; }
.chart .grid { stroke: var(--grid); stroke-width: 1; shape-rendering: crispEdges; }
.chart .baseline { stroke: var(--baseline); stroke-width: 1; shape-rendering: crispEdges; }
.chart .axis {
  fill: var(--ink-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.chart .line {
  fill: none;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.chart .marker { stroke: var(--surface); stroke-width: 1.5; }
.chart .direct { fill: var(--ink-primary); font-size: 12px; }
.chart .toollabel { fill: var(--ink-secondary); font-size: 10.5px; }
.chart .toollabel.emph { fill: var(--ink-primary); font-weight: 700; }
.chart .barval {
  fill: var(--ink-primary);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.chart .hit {
  fill: transparent;
  cursor: pointer;
  outline: none;
}
.chart .hit:focus-visible {
  outline: 2px solid var(--ink-primary);
  outline-offset: 1px;
  border-radius: 3px;
}
/* Anchor small multiples */
.anchor-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.anchor-chart {
  flex: 1 1 260px;
  min-width: 260px;
  margin: 0;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: 10px;
  padding: 14px 14px 8px;
}
.anchor-chart figcaption {
  color: var(--ink-secondary);
  font-size: 12.5px;
  font-weight: 600;
  margin-bottom: 4px;
}
/* Table */
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  background: var(--surface);
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
thead th {
  text-align: left;
  font-weight: 600;
  color: var(--ink-secondary);
  padding: 10px 12px;
  border-bottom: 1px solid var(--hairline);
  white-space: nowrap;
}
tbody td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--hairline);
  white-space: nowrap;
}
tbody tr:last-child td { border-bottom: none; }
td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
td.mono {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 11.5px;
}
.note { color: var(--ink-muted); font-size: 10.5px; }
/* Footnotes */
.footnotes {
  margin: 14px 0 0;
  padding-left: 20px;
  color: var(--ink-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.footnotes .mono {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 11px;
}
/* Tooltip */
.tooltip {
  position: absolute;
  z-index: 20;
  top: 0;
  left: 0;
  max-width: 260px;
  padding: 6px 9px;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: 7px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, .16);
  color: var(--ink-primary);
  font-size: 12px;
  line-height: 1.35;
  pointer-events: none;
  opacity: 0;
  transition: opacity .08s ease;
}
@media (max-width: 620px) {
  .tiles { grid-template-columns: repeat(2, 1fr); }
}
"""


TOOLTIP_JS = """
(function () {
  var root = document.querySelector('.viz-root');
  var tip = document.getElementById('viz-tooltip');
  if (!root || !tip) return;
  function show(el, x, y) {
    var text = el.getAttribute('data-tip');
    if (!text) return;
    tip.textContent = text;
    tip.style.opacity = '1';
    var rb = root.getBoundingClientRect();
    var tb = tip.getBoundingClientRect();
    var left = x - rb.left + 12;
    var top = y - rb.top + 12;
    if (left + tb.width > rb.width) left = rb.width - tb.width - 4;
    if (left < 0) left = 4;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  function hide() { tip.style.opacity = '0'; }
  root.addEventListener('mousemove', function (e) {
    var el = e.target.closest('[data-tip]');
    if (el) show(el, e.clientX, e.clientY); else hide();
  });
  root.addEventListener('mouseleave', hide);
  root.addEventListener('focusin', function (e) {
    var el = e.target.closest('[data-tip]');
    if (!el) return;
    var b = el.getBoundingClientRect();
    show(el, b.left + b.width / 2, b.top);
  });
  root.addEventListener('focusout', hide);
})();
"""


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------
def render_html(rows, baselines, sha, generated):
    points = deep_review_points(rows)
    subset_row = latest_subset_row(rows)
    groups = ledger_groups(rows)
    judge = html.escape(str(baselines.get("judge_pin", "—")))
    subtitle = (
        f"Generated {html.escape(generated)} · commit "
        f'<span class="mono">{html.escape(sha)}</span> · judge '
        f'<span class="mono">{judge}</span>'
    )
    embedded = json.dumps(
        {"ledger": rows, "baselines": baselines}, ensure_ascii=False
    ).replace("<", "\\u003c")

    body = [
        '<div class="viz-root">',
        '<div id="viz-tooltip" class="tooltip" role="status" aria-live="polite"></div>',
        "<header>",
        "<h1>deep-review bench — performance</h1>",
        f'<p class="subtitle">{subtitle}</p>',
        "</header>",
        build_tiles_html(subset_row, baselines),
        "<h2>Performance over runs</h2>",
        '<div class="card">',
        build_legend_html(),
        build_over_time_svg(points),
        "</div>",
        '<p class="caption">One point per scored deep-review-v2 run, in time '
        "order; the subset point aggregates its identical judge re-scores.</p>",
        "<h2>vs anchors (judge-only, 15-PR gate)</h2>",
        build_anchor_section_html(baselines),
        "<h2>Run ledger</h2>",
        build_table_html(groups),
        "<h2>Notes</h2>",
        build_footnotes_html(baselines),
        f'<script type="application/json" id="bench-data">{embedded}</script>',
        "</div>",
    ]

    doc = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>deep-review bench — performance</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        "\n".join(body),
        f"<script>{TOOLTIP_JS}</script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(doc)


def _normalize(text):
    """Strip trailing whitespace per line and end with exactly one newline.

    Keeps the committed HTML compliant with the trailing-whitespace and
    end-of-file-fixer pre-commit hooks regardless of template spacing.
    """
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate the deep-review bench performance dashboard."
    )
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--baselines", default=str(DEFAULT_BASELINES))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    rows = load_ledger(args.ledger)
    baselines = load_baselines(args.baselines)
    doc = render_html(rows, baselines, git_short_sha(), date.today().isoformat())
    out = _normalize(doc)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f"wrote {args.out} ({len(out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
