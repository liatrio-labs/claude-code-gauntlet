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
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

# Section break inside a tooltip / aria string. U+2028 (LINE SEPARATOR) renders as
# a hard break in the browser but is NOT a "\n", so it survives _normalize() on one
# physical line and never splits a JSON/attribute value across file lines.
TIP_BREAK = "\u2028"

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

# Tools plotted as points on the over-runs chart (in time order). Anchor rows
# (anchor-claude, …) are excluded here — they surface as the reference *lines*
# (top-anchor recall) and in the vs-anchors small multiples, not as our runs.
TRACKED_TOOLS = ("deep-review-v2", "deep-review-v3", "naive-anchor")

# Per-tool display + colour. Colour encodes the *tool identity* on the over-runs
# chart (never the metric or the rank); tier is a separate marker-shape channel.
TOOL_STYLE = {
    "deep-review-v2": ("deep-review v2", "--tool-deepreview"),
    "deep-review-v3": ("deep-review v3", "--tool-v3"),
    "naive-anchor": ("naive anchor", "--tool-naive"),
}

# Run tiers, weakest-to-strongest evidence: (key, label, one-line meaning).
TIER_INFO = (
    ("smoke", "smoke", "3 PRs / 4 goldens — directional only"),
    ("subset", "subset", "15 PRs / 59 goldens — gate-grade"),
    ("holdout", "holdout", "10 fresh PRs — confirmation"),
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
    """Most recent gate-grade (subset-tier) deep-review run — source for the tiles.

    Spans v2 and v3 so the headline tiles track whichever subset ran last (the
    Gate-1 v3 subset today), not a stale v2 baseline. Holdout is excluded: it is a
    confirmation pass, and the tiles report the run that must clear the gate.
    """
    subset = [
        r
        for r in rows
        if str(r.get("tool", "")).startswith("deep-review") and r.get("tier") == "subset"
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


def classify(row, top_anchor, ceiling):
    """Verdict for one run: (kind, glyph, one-line meaning).

    Kinds drive both the chart (``gate`` = headline halo, ``reverted`` = faded) and
    the ledger's verdict column. Gate-grade runs are judged against the two live
    bars (recall must clear the top anchor, noise must stay under the ceiling);
    smoke runs are judged by whether their experiment stuck.
    """
    tool = str(row.get("tool", "") or "")
    tier = row.get("tier")
    change = str(row.get("change") or "")
    recall = row.get("golden_recall")
    noise = row.get("noise_rate")
    if tool.startswith("anchor") or tool == "naive-anchor":
        return ("anchor", "◇", "external anchor — reference only")
    if "REVERT" in change.upper():
        return ("reverted", "✕", "regressed and was reverted")
    if tier in ("subset", "holdout") and recall is not None and noise is not None:
        if recall >= top_anchor and noise <= ceiling:
            return ("gate", "★",
                    "gate milestone — recall over the top-anchor bar, noise under the ceiling")
        return ("miss", "○",
                "below gate — recall under the bar or noise over the ceiling")
    if row.get("hypothesis"):
        return ("hit", "✓", "kept — improvement carried forward")
    return ("base", "·", "baseline / reference run")


def short_label(pt):
    """Compact x-axis tag for one run point (semantic, not a raw id)."""
    tool = pt["tool"]
    tier = pt["tier"]
    if tool == "naive-anchor":
        return "naive"
    if tool == "deep-review-v2":
        return "v2 base" if tier == "subset" else "v2 smoke"
    if tier == "holdout":
        return "Holdout"
    if tier == "subset":
        return "Gate-1" if pt["kind"] == "gate" else "v3 subset"
    m = re.search(r"iter\s*(\d+)", pt.get("hypothesis") or "")
    return f"iter{m.group(1)}" if m else "v3 smoke"


def run_tooltip(pt):
    """Multi-line hover/focus text: identity, both gated metrics, why + what."""
    style = TOOL_STYLE.get(pt["tool"], (pt["tool"], ""))
    lines = [
        f"{short_label(pt)} · {style[0]} · {pt['tier']} · {fmt_date(pt['ts'])}",
        f"recall {fmt_pct(pt['recall'])}  ·  noise {fmt_pct(pt['noise'])}",
    ]
    if pt.get("hypothesis"):
        lines.append("Why: " + pt["hypothesis"])
    if pt.get("change"):
        lines.append("Change: " + pt["change"])
    return html.escape(TIP_BREAK.join(lines))


def scored_run_points(rows, top_anchor, ceiling):
    """One point per tracked run (v2 / v3 / naive), time-ordered.

    Collapses a run_id's identical judge re-scores into a single point and tags it
    with tool, tier, both gated metrics, its verdict ``kind`` and the ledger
    hypothesis/change annotations used for on-hover context.
    """
    groups = {}
    order = []
    for r in rows:
        if r.get("tool") not in TRACKED_TOOLS:
            continue
        key = (r.get("run_id"), r.get("tool"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    points = []
    for key in order:
        grp = groups[key]
        rep = sorted(grp, key=lambda r: r.get("ts", ""))[0]
        kind, _glyph, _desc = classify(rep, top_anchor, ceiling)
        points.append(
            {
                "run_id": key[0],
                "tool": key[1],
                "tier": rep.get("tier"),
                "ts": min(r.get("ts", "") for r in grp),
                "count": len(grp),
                "identical": len({_metric_signature(r) for r in grp}) == 1,
                "recall": rep.get("golden_recall"),
                "noise": rep.get("noise_rate"),
                "hypothesis": rep.get("hypothesis"),
                "change": rep.get("change"),
                "kind": kind,
                "headline": kind == "gate",
                "reverted": kind == "reverted",
            }
        )
    points.sort(key=lambda p: p["ts"])
    return points


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


def _run_marker(x, y, tier, var, reverted, headline):
    """One data marker: shape = tier, fill = tool colour, faded if reverted,
    haloed if it is a gate headline. Filled marks carry a 2px surface ring."""
    g = []
    if headline:
        g.append(
            f'<circle class="halo" cx="{x:.1f}" cy="{y:.1f}" r="10" '
            f'style="stroke:var({var})" />'
        )
    if tier == "smoke":
        g.append(
            f'<circle class="mk mk-open" cx="{x:.1f}" cy="{y:.1f}" r="4.6" '
            f'style="stroke:var({var})" />'
        )
    elif tier == "holdout":
        r = 6.2
        d = (f"M{x:.1f},{y - r:.1f} L{x + r:.1f},{y:.1f} "
             f"L{x:.1f},{y + r:.1f} L{x - r:.1f},{y:.1f} Z")
        g.append(f'<path class="mk mk-fill" d="{d}" style="fill:var({var})" />')
    else:  # subset
        g.append(
            f'<circle class="mk mk-fill" cx="{x:.1f}" cy="{y:.1f}" r="5.6" '
            f'style="fill:var({var})" />'
        )
    op = ' opacity="0.38"' if reverted else ""
    return f"<g{op}>" + "".join(g) + "</g>"


def build_runs_svg(points, top_anchor, v2_base, ceiling):
    """Two stacked panels on a shared, time-ordered run axis.

    Panel A is golden recall (0–100%) with the v2-baseline and top-anchor bars; panel
    B is noise rate (0–40%) with the ceiling. Colour encodes the tool, marker shape
    encodes the tier, reverted experiments are faded, and the two gate headlines carry
    a halo + direct label. Every marker has a ≥24px hover/focus target.
    """
    W = 760
    m_left, m_right = 46, 150
    inner_w = W - m_left - m_right
    aY, aH = 30, 140            # recall panel: domain 0..1
    bY, bH = 206, 82            # noise panel: domain 0..0.4
    A_DOM, B_DOM = 1.0, 0.40
    H = 364
    n = len(points)

    def x_of(i):
        if n <= 1:
            return m_left + inner_w / 2
        return m_left + inner_w * i / (n - 1)

    def yA(v):
        return aY + (1 - max(0.0, min(v, A_DOM)) / A_DOM) * aH

    def yB(v):
        return bY + (1 - max(0.0, min(v, B_DOM)) / B_DOM) * bH

    parts = [
        f'<svg class="chart" viewBox="0 0 {W} {H}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="Golden recall and noise rate for every scored deep-review run '
        f'in time order, v2 and v3, against the v2-baseline, top-anchor and '
        f'noise-ceiling bars.">'
    ]

    # Headline spotlight bands (drawn first, behind everything), spanning both panels.
    for i, p in enumerate(points):
        if p["headline"]:
            x = x_of(i)
            parts.append(
                f'<rect class="spotlight" x="{x - 13:.1f}" y="{aY:.1f}" '
                f'width="26" height="{bY + bH - aY:.1f}" />'
            )

    # Panel gridlines + percent axis labels.
    def panel_grid(ticks, yfn, dom):
        for t in ticks:
            y = yfn(t)
            cls = "baseline" if t == 0 else "grid"
            parts.append(
                f'<line class="{cls}" x1="{m_left:.1f}" y1="{y:.1f}" '
                f'x2="{m_left + inner_w:.1f}" y2="{y:.1f}" />'
            )
            parts.append(
                f'<text class="axis" x="{m_left - 8:.1f}" y="{y + 3.5:.1f}" '
                f'text-anchor="end">{int(round(t * 100))}%</text>'
            )

    panel_grid((0.0, 0.25, 0.5, 0.75, 1.0), yA, A_DOM)
    panel_grid((0.0, 0.1, 0.2, 0.3, 0.4), yB, B_DOM)

    # Panel titles.
    parts.append(
        f'<text class="paneltitle" x="{m_left:.1f}" y="{aY - 12:.1f}">'
        "Golden recall — share of known-good findings caught (higher is better)</text>"
    )
    parts.append(
        f'<text class="paneltitle" x="{m_left:.1f}" y="{bY - 12:.1f}">'
        "Noise rate — share of delivered comments that are junk (lower is better)</text>"
    )

    # Reference lines: coloured, solid (never dashed), each named in ink at the right.
    def refline(y, var, label):
        parts.append(
            f'<line class="refline" x1="{m_left:.1f}" y1="{y:.1f}" '
            f'x2="{m_left + inner_w:.1f}" y2="{y:.1f}" style="stroke:var({var})" />'
        )
        parts.append(
            f'<text class="reflabel" x="{m_left + inner_w + 6:.1f}" '
            f'y="{y + 3.5:.1f}">{html.escape(label)}</text>'
        )

    if top_anchor is not None:
        refline(yA(top_anchor), "--ref-goal", f"top-anchor {fmt_pct(top_anchor)}")
    if v2_base is not None:
        refline(yA(v2_base), "--tool-deepreview", f"v2 baseline {fmt_pct(v2_base)}")
    if ceiling is not None:
        refline(yB(ceiling), "--ref-ceiling", f"noise ceiling {fmt_pct(ceiling)}")

    # Connectors: one thin, recessive line per tool per panel, in time order.
    for tool in ("deep-review-v2", "deep-review-v3"):
        var = TOOL_STYLE[tool][1]
        seq = [(i, p) for i, p in enumerate(points) if p["tool"] == tool]
        for yfn, key in ((yA, "recall"), (yB, "noise")):
            xy = [(x_of(i), yfn(p[key])) for i, p in seq if p[key] is not None]
            if len(xy) >= 2:
                pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
                parts.append(
                    f'<polyline class="runline" points="{pts}" '
                    f'style="stroke:var({var})" />'
                )

    # Markers + hit targets, both panels.
    for i, p in enumerate(points):
        x = x_of(i)
        var = TOOL_STYLE.get(p["tool"], ("", "--tool-naive"))[1]
        tip = run_tooltip(p)
        for yfn, key in ((yA, "recall"), (yB, "noise")):
            v = p[key]
            if v is None:
                continue
            y = yfn(v)
            parts.append(
                _run_marker(x, y, p["tier"], var, p["reverted"], p["headline"])
            )
            parts.append(
                f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="12" '
                f'tabindex="0" role="img" aria-label="{tip}" data-tip="{tip}" />'
            )

    # Direct labels on the two gate headlines (recall panel only).
    for i, p in enumerate(points):
        if not p["headline"] or p["recall"] is None:
            continue
        x = x_of(i)
        y = yA(p["recall"]) - 10
        last = i == n - 1
        anchor = "start" if last else "end"
        dx = 9 if last else -9
        parts.append(
            f'<text class="directlabel" x="{x + dx:.1f}" y="{y:.1f}" '
            f'text-anchor="{anchor}">{html.escape(short_label(p))} '
            f'{fmt_pct(p["recall"])}</text>'
        )

    # Rotated x-axis run tags.
    for i, p in enumerate(points):
        x = x_of(i)
        y = bY + bH + 14
        parts.append(
            f'<text class="xtick" x="{x:.1f}" y="{y:.1f}" text-anchor="end" '
            f'transform="rotate(-32 {x:.1f} {y:.1f})">'
            f"{html.escape(short_label(p))}</text>"
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
_TIER_GLYPH_SVG = {
    "smoke": '<svg class="tier-glyph" viewBox="0 0 12 12" aria-hidden="true">'
             '<circle cx="6" cy="6" r="3.6" fill="none" stroke="currentColor" '
             'stroke-width="1.6"/></svg>',
    "subset": '<svg class="tier-glyph" viewBox="0 0 12 12" aria-hidden="true">'
              '<circle cx="6" cy="6" r="4" fill="currentColor"/></svg>',
    "holdout": '<svg class="tier-glyph" viewBox="0 0 12 12" aria-hidden="true">'
               '<path d="M6 1.6 L10.4 6 L6 10.4 L1.6 6 Z" fill="currentColor"/></svg>',
}


def build_runs_legend_html():
    """Two keyed groups: tool (colour) and run type (marker shape), plus states."""
    tools = "".join(
        f'<span class="legend-item"><span class="legend-dot" '
        f'style="background:var({var})"></span>{html.escape(name)}</span>'
        for name, var in (
            TOOL_STYLE["deep-review-v2"], TOOL_STYLE["deep-review-v3"],
            TOOL_STYLE["naive-anchor"],
        )
    )
    tiers = "".join(
        f'<span class="legend-item">{_TIER_GLYPH_SVG[key]}{html.escape(label)} '
        f'<span class="legend-sub">({meaning.split("—")[-1].strip()})</span></span>'
        for key, label, meaning in TIER_INFO
    )
    states = (
        '<span class="legend-item"><span class="legend-state-faded">◇</span>'
        "reverted (faded)</span>"
        '<span class="legend-item"><span class="legend-halo">★</span>'
        "gate milestone (haloed + labelled)</span>"
    )
    return (
        '<div class="runs-legend">'
        f'<div class="legend-group"><span class="legend-title">Tool</span>{tools}</div>'
        f'<div class="legend-group"><span class="legend-title">Run type</span>{tiers}</div>'
        f'<div class="legend-group"><span class="legend-title">State</span>{states}</div>'
        "</div>"
    )


def build_explainer_html(top_anchor, v2_base, ceiling):
    """Compact 'how to read this' card for a reader with zero project context."""
    buckets = (
        ("--ref-goal", "golden-matched",
         "matched a known-good finding from the hand-labelled golden set"),
        ("--tool-deepreview", "valid-extra",
         "a real issue that just isn't in the golden set — never counted against us"),
        ("--ref-ceiling", "noise",
         "wrong, or not worth a reviewer's time"),
    )
    chips = "".join(
        f'<li><span class="chip-dot" style="background:var({var})"></span>'
        f"<b>{html.escape(name)}</b> — {html.escape(desc)}</li>"
        for var, name, desc in buckets
    )
    metrics = "".join(
        f"<li><b>{html.escape(term)}</b> {html.escape(desc)}</li>"
        for term, desc in (
            ("Recall", "= goldens caught ÷ all goldens. The headline, gated metric."),
            ("Noise rate", "= noise ÷ all delivered comments. Gated — must stay under the ceiling."),
            ("Valid-extra", "= real issues beyond the golden set. Reported, never penalised."),
            ("Precision", "= how on-target the delivered comments are. Reported for context, not gated."),
        )
    )
    bars = "".join(
        f'<li><span class="bar-key" style="background:var({var})"></span>'
        f"<b>{html.escape(val)}</b> — {html.escape(desc)}</li>"
        for var, val, desc in (
            ("--ref-goal", fmt_pct(top_anchor),
             "top-anchor recall (best external tool) — the bar a run must beat"),
            ("--tool-deepreview", fmt_pct(v2_base),
             "deep-review v2 recall — the prior baseline"),
            ("--ref-ceiling", fmt_pct(ceiling),
             "noise ceiling — a run above it fails the gate"),
        )
    )
    tiers = "".join(
        f"<li><b>{html.escape(label.capitalize())}</b> — {html.escape(meaning)}</li>"
        for _key, label, meaning in TIER_INFO
    )
    return (
        '<section class="explainer card" aria-label="How to read this dashboard">'
        '<div class="explainer-grid">'
        '<div class="explainer-col"><h3>Every delivered comment lands in one bucket</h3>'
        f'<ul class="bucket-list">{chips}</ul></div>'
        f'<div class="explainer-col"><h3>The metrics</h3><ul class="def-list">{metrics}</ul></div>'
        f'<div class="explainer-col"><h3>The three bars that define success</h3>'
        f'<ul class="def-list">{bars}</ul></div>'
        '<div class="explainer-col"><h3>How much a run is trusted</h3>'
        f'<ul class="def-list">{tiers}</ul>'
        '<p class="explainer-foot">Smoke numbers are directional and never pass or '
        'fail a gate; a subset run is gate-grade; a holdout run confirms it on fresh '
        'PRs.</p></div>'
        "</div></section>"
    )


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


def build_table_html(groups, top_anchor, ceiling):
    heads = [
        "", "Date", "Run", "Tool", "Tier", "PRs", "Recall", "Valid-extra",
        "Noise", "Precision", "F1", "Tokens", "Cost", "What changed",
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
        kind, glyph, desc = classify(r, top_anchor, ceiling)
        # "What changed" cell: the outcome line, else the hypothesis; full text on hover.
        changed_full = r.get("change") or r.get("hypothesis") or ""
        parts_full = []
        if r.get("hypothesis"):
            parts_full.append("Hypothesis: " + r["hypothesis"])
        if r.get("change"):
            parts_full.append("Change: " + r["change"])
        title_full = html.escape(TIP_BREAK.join(parts_full)) if parts_full else ""
        changed_cell = (
            f'<td class="changed" title="{title_full}">'
            f"{html.escape(truncate_middle(changed_full, 72))}</td>"
            if changed_full
            else '<td class="changed muted-cell">—</td>'
        )
        cells = (
            f'<td class="verdict verdict-{kind}" title="{html.escape(desc)}">{glyph}</td>'
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
            f"{changed_cell}"
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
  --tool-v3: #eb6834;
  --tool-naive: #898781;
  --ref-goal: #006300;
  --ref-ceiling: #d03b3b;
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
    --tool-v3: #d95926;
    --tool-naive: #898781;
    --ref-goal: #0ca30c;
    --ref-ceiling: #e66767;
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
  --tool-v3: #d95926;
  --tool-naive: #898781;
  --ref-goal: #0ca30c;
  --ref-ceiling: #e66767;
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
/* Metrics explainer */
.explainer { margin: 0 0 22px; padding: 18px 18px 14px; }
.explainer-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 26px;
}
.explainer-col h3 {
  margin: 0 0 7px;
  font-size: 12.5px;
  font-weight: 650;
  color: var(--ink-primary);
}
.explainer-col ul {
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
  line-height: 1.5;
  color: var(--ink-secondary);
}
.explainer-col li { margin: 0 0 5px; }
.explainer-col li b { color: var(--ink-primary); font-weight: 620; }
.bucket-list li { position: relative; padding-left: 16px; }
.chip-dot {
  position: absolute;
  left: 0;
  top: 5px;
  width: 9px;
  height: 9px;
  border-radius: 2px;
}
.bar-key {
  display: inline-block;
  width: 14px;
  height: 3px;
  border-radius: 2px;
  vertical-align: middle;
  margin-right: 7px;
}
.explainer-foot { margin: 8px 0 0; font-size: 11px; color: var(--ink-muted); line-height: 1.45; }
.tiles-source {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--ink-secondary);
}
.tiles-source b { color: var(--ink-primary); }
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
/* Over-runs chart legend */
.runs-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 22px;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--ink-secondary);
}
.runs-legend .legend-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
}
.legend-title {
  font-size: 10.5px;
  font-weight: 650;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--ink-muted);
}
.legend-sub { color: var(--ink-muted); }
.tier-glyph {
  width: 12px;
  height: 12px;
  margin-right: 5px;
  color: var(--ink-secondary);
  flex: none;
}
.legend-state-faded { margin-right: 5px; opacity: .4; color: var(--ink-primary); }
.legend-halo { margin-right: 5px; color: var(--ref-goal); }
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
/* Over-runs two-panel chart */
.chart .paneltitle {
  fill: var(--ink-secondary);
  font-size: 11.5px;
  font-weight: 600;
}
.chart .refline { stroke-width: 1.5; shape-rendering: crispEdges; opacity: .9; }
.chart .reflabel {
  fill: var(--ink-secondary);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
}
.chart .spotlight { fill: var(--tool-v3); opacity: .09; }
.chart .runline {
  fill: none;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  opacity: .32;
}
.chart .mk-fill { stroke: var(--surface); stroke-width: 2; }
.chart .mk-open { fill: var(--surface); stroke-width: 2; }
.chart .halo { fill: none; stroke-width: 2; opacity: .5; }
.chart .directlabel {
  fill: var(--ink-primary);
  font-size: 11px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.chart .xtick {
  fill: var(--ink-secondary);
  font-size: 10.5px;
}
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
td.verdict {
  text-align: center;
  font-size: 13px;
  padding-left: 10px;
  padding-right: 6px;
  cursor: help;
}
.verdict-gate { color: var(--ref-goal); }
.verdict-reverted { color: var(--ref-ceiling); }
.verdict-miss { color: var(--ink-muted); }
.verdict-hit { color: var(--tool-deepreview); }
.verdict-anchor, .verdict-base { color: var(--ink-muted); }
td.changed {
  white-space: normal;
  min-width: 200px;
  max-width: 340px;
  color: var(--ink-secondary);
  font-size: 11.5px;
  line-height: 1.35;
}
td.muted-cell { color: var(--ink-muted); }
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
  max-width: 320px;
  padding: 7px 10px;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: 7px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, .16);
  color: var(--ink-primary);
  font-size: 12px;
  line-height: 1.4;
  pointer-events: none;
  opacity: 0;
  transition: opacity .08s ease;
}
@media (max-width: 720px) {
  .explainer-grid { grid-template-columns: minmax(0, 1fr); }
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
def _thresholds(baselines):
    """The three live success bars, read from baselines (never hard-coded)."""
    anchor_rows = baselines.get("anchors", {}).get("rows", {})
    recalls = [v.get("recall") for v in anchor_rows.values() if v.get("recall") is not None]
    top_anchor = max(recalls) if recalls else None
    v2_base = baselines.get("baseline_v2", {}).get("golden_recall")
    ceiling = baselines.get("delta_noise")
    if ceiling is None:
        ceiling = (baselines.get("delta_noise_proposed", {}) or {}).get("value")
    return top_anchor, v2_base, ceiling


def render_html(rows, baselines, sha, generated):
    top_anchor, v2_base, ceiling = _thresholds(baselines)
    points = scored_run_points(rows, top_anchor, ceiling)
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

    src_label = ""
    if subset_row:
        tname = TOOL_STYLE.get(subset_row.get("tool"), (str(subset_row.get("tool")), ""))[0]
        src_label = (
            '<p class="tiles-source">Headline numbers below are the latest gate-grade '
            f"run: <b>{html.escape(tname)}</b> · {html.escape(str(subset_row.get('tier', '')))}"
            " tier · run "
            f'<span class="mono">{html.escape(truncate_middle(str(subset_row.get("run_id", "")), 32))}</span>'
            f" · {html.escape(fmt_date(subset_row.get('ts', '')))}</p>"
        )

    runs_caption = (
        "Every scored run in time order — the v2 baseline, then the v3 hill-climb. "
        "Colour is the tool, marker shape is the run type; faded markers were reverted, "
        "and the two haloed markers are the Gate-1 subset and its holdout confirmation. "
        "The labelled reference lines are the v2 baseline, the top-anchor bar to beat, "
        "and the noise ceiling. Hover or focus any point for its hypothesis and result."
    )

    body = [
        '<div class="viz-root">',
        '<div id="viz-tooltip" class="tooltip" role="status" aria-live="polite"></div>',
        "<header>",
        "<h1>deep-review bench — performance</h1>",
        f'<p class="subtitle">{subtitle}</p>',
        "</header>",
        build_explainer_html(top_anchor, v2_base, ceiling),
        src_label,
        build_tiles_html(subset_row, baselines),
        "<h2>Performance over runs</h2>",
        '<div class="card">',
        build_runs_legend_html(),
        build_runs_svg(points, top_anchor, v2_base, ceiling),
        "</div>",
        f'<p class="caption">{runs_caption}</p>',
        "<h2>vs anchors (judge-only, 15-PR gate)</h2>",
        build_anchor_section_html(baselines),
        "<h2>Run ledger</h2>",
        build_table_html(groups, top_anchor, ceiling),
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
