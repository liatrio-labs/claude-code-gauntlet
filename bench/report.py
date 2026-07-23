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
    ("custom", "custom", "variable PRs, efficiency & mini-subset runs — pre-registered paired design"),
)

# Tiers whose recall/noise are graded against the two live bars (top-anchor +
# noise ceiling). Smoke is directional-only; the rest are gate-grade.
GATE_TIERS = ("subset", "holdout", "custom")

# Child-model experiments: CLAUDE_CODE_SUBAGENT_MODEL was pinned to a sonnet
# child, and the pin cascaded onto the pipeline agents — dragging the [1m]
# context variant with it (visible as claude-sonnet-5[1m] in one run's per_model).
# That confounds their token deltas, so both were reverted and are shown faded.
# They carry no hypothesis/change annotation and are metrically indistinguishable
# from a clean smoke, so the two run_ids are named here explicitly; every NUMBER
# rendered for them (recall, noise, tokens, the child-model label) is still read
# from the ledger row. Source: the Gate-2 custom run's change note.
CONFOUNDED_RUNS = frozenset({
    "smoke-20260721-160606-83eadfe",   # sonnet child pin
    "smoke-20260721-172943-0b24d95",   # sonnet[1m] child pin
})

# Superseded runs, measured but voided post-hoc for reasons unrelated to a
# child-model confound (see CONFOUNDED_RUNS above) — one environment-contaminated
# smoke, one custom-tier mini-subset attempt that failed its paired bar and was
# followed by a surgical revert + re-run (the re-run is custom-20260723-102149-381e9ff,
# the v3.1 run of record). Same faded/"reverted" chart-and-table treatment as
# CONFOUNDED_RUNS, keyed to their own tooltip text rather than the child-model one.
VOID_RUNS = {
    "smoke-20260723-033811-6ea1737": "contamination-void — environment-contaminated run, discard",
    "custom-20260723-070640-c1dd46f": "failed paired bar → item-4 revert → re-run",
}

# x-axis / tooltip label overrides for milestone runs that would otherwise collide
# under the generic per-tier label in short_label() — three custom-tier runs now
# exist (Gate-2, its failed mini-subset attempt, and the v3.1 mini-subset of
# record), so "custom" -> "Gate-2" is no longer a safe default for all of them.
MILESTONE_LABELS = {
    "custom-20260721-220637-5337f6c": "Gate-2",
    "custom-20260723-070640-c1dd46f": "mini-A (failed)",
    "custom-20260723-102149-381e9ff": "v3.1",
}

# The Gate-2 final-config run, pinned explicitly. Before v3.1, it was also the
# *only* custom-tier row, so "most recent custom row" and "the Gate-2 run" were
# the same thing; that's no longer true, so the History section below looks this
# run_id up directly rather than taking whatever custom-tier row is newest.
GATE2_RUN_ID = "custom-20260721-220637-5337f6c"

# Long-term release-progression ledger — the headline section of the report.
# Newest first. Each release lists the ledger run_id(s) that define it; every
# number rendered for a leg (recall, noise, tokens, cost) is read live from the
# ledger via row_by_run_id(), never hardcoded here. Only what the ledger cannot
# supply — prose describing what changed, and one owner-supplied paired-context
# comparison that has no ledger row of its own — is a literal string below.
# Adding a future release is one more entry in this list.
RELEASES = (
    {
        "label": "v3.1",
        "legs": (
            {
                "name": "mini-subset A · paired, 6 PRs",
                "run_id": "custom-20260723-102149-381e9ff",
                "n_goldens": 30,
            },
        ),
        "what_changed": (
            "Schema-fidelity hardening (echo fidelity gate, numeric confidence, "
            "write-proofs), a location-based blind challenger, light scope, "
            "orchestrator full-ID pinning, and an NDJSON contract scrub."
        ),
        "bar_check": True,
        "extra_note": (
            "Small-N caveat: a single 6-PR mini-subset pass. Paired same-6-PR "
            "context: 18/30 goldens here vs v3.0's 20/30 on the identical 6 PRs."
        ),
    },
    {
        "label": "v3.0",
        "legs": (
            {"name": "gate · 15 PRs", "run_id": "subset-20260721-015119-639e4bc", "n_goldens": 59},
            {"name": "holdout · 10 fresh PRs", "run_id": "holdout-20260721-085348-eec15be"},
        ),
        "what_changed": (
            "Full workflow-native rewrite: an 8-stage pipeline running in the "
            "Workflow runtime, −49% tokens vs v2."
        ),
        "bar_check": False,
        "extra_note": "",
    },
    {
        "label": "v2",
        "legs": (
            {"name": "baseline · 15 PRs", "run_id": "subset-20260718-031746-27875ca", "n_goldens": 59},
        ),
        "what_changed": "The starting architecture.",
        "bar_check": False,
        "extra_note": "",
    },
)

# vs-anchors bars: (label, CSS var, emphasized, source key). "v3" pulls from the
# latest v3 gate subset row, "v3_holdout" from the holdout confirmation row, "v2"
# from baseline_v2, the rest from baselines.anchors.rows[<key>]. Our tool leads on
# both the gate and the fresh-PR holdout, and both are emphasized.
ANCHOR_TOOLS = (
    ("deep-review v3", "--tool-v3", True, "v3"),
    ("v3 · holdout", "--tool-v3", True, "v3_holdout"),
    ("deep-review v2", "--tool-deepreview", True, "v2"),
    ("claude", "--tool-claude", False, "claude"),
    ("claude-code", "--tool-claude-code", False, "claude-code"),
    ("coderabbit", "--tool-coderabbit", False, "coderabbit"),
)

# vs-anchors metrics: (v2/v3 ledger field, anchors.rows field, display title).
# Recall and noise only — precision is reported-not-gated (see the table footnote).
ANCHOR_METRICS = (
    ("golden_recall", "recall", "Golden recall — higher is better"),
    ("noise_rate", "noise_rate", "Noise rate — lower is better"),
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


def fmt_millions(x, digits=1):
    """Compact token magnitude, e.g. 41_060_587 -> "41.1M"."""
    if x is None:
        return "—"
    return f"{x / 1e6:.{digits}f}M"


def fmt_delta_pp(new, old):
    """Signed percentage-point change between two rates (0..1)."""
    if new is None or old is None:
        return "—"
    return f"{(new - old) * 100:+.1f}pp"


def fmt_delta_pct(new, old):
    """Signed relative change, e.g. tokens −49%."""
    if new is None or not old:
        return "—"
    return f"{(new - old) / old * 100:+.0f}%"


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


def _dominant_model(row):
    """The per_model key that burned the most tokens — the run's de-facto workhorse."""
    pm = row.get("per_model") or {}
    if not pm:
        return None
    return max(pm.items(), key=lambda kv: (kv[1] or {}).get("tokens", 0) or 0)[0]


def _short_model(name):
    """"claude-sonnet-5[1m]" -> "sonnet[1m]"; "claude-opus-4-8[1m]" -> "opus[1m]"."""
    if not name:
        return None
    variant = ""
    base = name
    if base.endswith("[1m]"):
        variant = "[1m]"
        base = base[: -len("[1m]")]
    core = base[len("claude-"):] if base.startswith("claude-") else base
    for fam in ("sonnet", "opus", "haiku"):
        if core.startswith(fam):
            return fam + variant
    return core.split("-")[0] + variant


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


def row_by_run_id(rows, run_id):
    """Exact-match ledger row for a frozen release run_id.

    Earliest ts wins on a k=5 re-score (byte-identical per judge_sd=0), matching
    the "representative row" convention used everywhere else in this module.
    """
    cands = [r for r in rows if r.get("run_id") == run_id]
    return sorted(cands, key=lambda r: r.get("ts", ""))[0] if cands else None


def current_release_leg(rows, releases=RELEASES):
    """(release, leg, row) for the newest release whose defining leg is actually
    present in the loaded ledger — lets the dashboard degrade gracefully against
    a smaller/offline fixture instead of rendering a release with no numbers."""
    for rel in releases:
        for leg in rel["legs"]:
            row = row_by_run_id(rows, leg["run_id"])
            if row is not None:
                return rel, leg, row
    return None, None, None


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
    if row.get("run_id") in CONFOUNDED_RUNS:
        return ("reverted", "✕",
                "confounded child-model experiment — the subagent-model pin cascaded "
                "the [1m] variant onto the pipeline agents; reverted")
    if row.get("run_id") in VOID_RUNS:
        return ("reverted", "✕", VOID_RUNS[row.get("run_id")])
    if "REVERT" in change.upper():
        return ("reverted", "✕", "regressed and was reverted")
    if tier in GATE_TIERS and recall is not None and noise is not None:
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
    if pt.get("run_id") in MILESTONE_LABELS:
        return MILESTONE_LABELS[pt["run_id"]]
    tool = pt["tool"]
    tier = pt["tier"]
    if tool == "naive-anchor":
        return "naive"
    if tool == "deep-review-v2":
        return "v2 base" if tier == "subset" else "v2 smoke"
    if pt.get("child_model"):        # confounded child-model experiment
        return pt["child_model"]     # e.g. "sonnet" / "sonnet[1m]"
    if tier == "holdout":
        return "Holdout"
    if tier == "custom":
        return "custom"              # no override registered — generic fallback
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
        confounded = rep.get("run_id") in CONFOUNDED_RUNS
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
                "child_model": _short_model(_dominant_model(rep)) if confounded else None,
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
    elif tier == "custom":
        s = 5.0
        g.append(
            f'<rect class="mk mk-fill" x="{x - s:.1f}" y="{y - s:.1f}" '
            f'width="{2 * s:.1f}" height="{2 * s:.1f}" rx="1.2" '
            f'style="fill:var({var})" />'
        )
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

    # Direct labels on the gate headlines (recall panel only). Headlines that crowd
    # together horizontally (Gate-1 + its holdout sit ~35px apart) get their labels
    # stacked upward so they never overlap; isolated ones (Gate-2) reset the stack.
    prev_x = None
    level = 0
    for i, p in enumerate(points):
        if not p["headline"] or p["recall"] is None:
            continue
        x = x_of(i)
        if prev_x is not None and abs(x - prev_x) < 74:
            level += 1
        else:
            level = 0
        prev_x = x
        y = max(aY + 4, yA(p["recall"]) - 10 - level * 12)
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


def build_anchor_svg(title, bars):
    """One small-multiple: horizontal bars (our tools + anchors) on a 0..1 scale.

    ``bars`` is a list of ``(label, css_var, emphasized, value)``; None-valued bars
    are dropped by the caller. Our tools lead and render emphasized."""
    W = 330
    label_w = 100
    bar_x = label_w + 5
    val_w = 48
    bar_max_w = W - bar_x - val_w
    top = 8
    bar_h = 18
    gap = 4
    H = top + len(bars) * (bar_h + gap) + 4
    parts = [
        f'<svg class="chart" viewBox="0 0 {W} {H}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{html.escape(title)} by tool">'
    ]
    for idx, (label, var, emph, v) in enumerate(bars):
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


def _anchor_value(src, v2v3_key, anchor_key, bv2, v3_row, v3_holdout_row, anchor_rows):
    if src == "v3":
        return (v3_row or {}).get(v2v3_key)
    if src == "v3_holdout":
        return (v3_holdout_row or {}).get(v2v3_key)
    if src == "v2":
        return bv2.get(v2v3_key)
    return anchor_rows.get(src, {}).get(anchor_key)


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
    "custom": '<svg class="tier-glyph" viewBox="0 0 12 12" aria-hidden="true">'
              '<rect x="2" y="2" width="8" height="8" rx="1.4" fill="currentColor"/></svg>',
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
            ("Noise rate", "= noise ÷ all delivered comments — ‘how often are we wrong’. Gated: must stay under the ceiling."),
            ("Valid-extra", "= real issues beyond the golden set. Reported, never penalised."),
            ("Precision †", "counts valid-extras as misses, so it sinks as volume grows. Reported, not gated — read noise rate instead."),
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
        'PRs; a custom run is the pre-registered paired design behind Gate-2. By owner '
        'decision v3 delivers ~4× the comments of v2, so its lower precision is a '
        'bigger denominator, not weaker findings. Gate-2 re-baselined its token target '
        'to the v2 baseline on 2026-07-21: the original −20%-vs-Gate-1 target was '
        'self-referential (it measured only −1.8%) and was retired. Two child-model '
        'smokes render faded because they are confounded experiments — a subagent-model '
        'pin cascaded the [1m] context variant onto the pipeline agents — and were '
        'reverted.</p></div>'
        "</div></section>"
    )


def _release_leg_html(rows, leg):
    """One measurement line inside a release card, or "" if its run is missing
    from the loaded ledger (offline fixture, or a future release not yet run)."""
    row = row_by_run_id(rows, leg["run_id"])
    if row is None:
        return ""
    recall = row.get("golden_recall")
    noise = row.get("noise_rate")
    tokens = row.get("tokens_total")
    cost = row.get("cost_usd")
    n_goldens = leg.get("n_goldens")
    metrics = [f"recall {fmt_pct(recall)}", f"noise {fmt_pct(noise)}"]
    if n_goldens and recall is not None:
        metrics[0] += f" ({round(recall * n_goldens)}/{n_goldens})"
    if tokens:
        metrics.append(f"{fmt_millions(tokens)} tokens")
    if cost:
        metrics.append(fmt_money(cost))
    return (
        '<div class="release-leg">'
        f'<span class="release-leg-name">{html.escape(leg["name"])}</span>'
        f'<span class="release-leg-metrics">{html.escape(" · ".join(metrics))}</span>'
        f'<span class="release-leg-run mono">{html.escape(leg["run_id"])}</span>'
        "</div>"
    )


def build_releases_html(rows, top_anchor, v2_base, ceiling):
    """The headline section: one card per release, newest first, each with its
    release-defining measurement leg(s) and a one-line "what changed". This is
    the long-term progression record; the v2->v3 rewrite is one chapter of it,
    demoted to the "History" section below (see build_verdict_html/build_gate2_html).
    Adding a future release is one more RELEASES entry — nothing here hardcodes
    a release count or a specific number of legs.
    """
    cards = []
    for rel in RELEASES:
        leg_htmls = [_release_leg_html(rows, leg) for leg in rel["legs"]]
        leg_htmls = [h for h in leg_htmls if h]
        note_bits = []
        if rel.get("bar_check") and rel["legs"]:
            head_row = row_by_run_id(rows, rel["legs"][0]["run_id"])
            recall = (head_row or {}).get("golden_recall")
            noise = (head_row or {}).get("noise_rate")
            if recall is not None and noise is not None and top_anchor is not None:
                clears = recall >= top_anchor and (ceiling is None or noise <= ceiling)
                note_bits.append(
                    ("Clears both hard bars (" if clears else "Misses a hard bar (") +
                    f"top-anchor {fmt_pct(top_anchor)}"
                    + (f", v2 {fmt_pct(v2_base)}" if v2_base is not None else "")
                    + ")."
                )
        if rel.get("extra_note"):
            note_bits.append(rel["extra_note"])
        note_html = (
            f'<p class="release-note">{html.escape(" ".join(note_bits))}</p>'
            if note_bits else ""
        )
        cards.append(
            '<article class="release-card">'
            f'<h3 class="release-label">{html.escape(rel["label"])}</h3>'
            f'<div class="release-legs">{"".join(leg_htmls)}</div>'
            f'<p class="release-changed"><b>What changed:</b> '
            f'{html.escape(rel["what_changed"])}</p>'
            f"{note_html}"
            "</article>"
        )
    return f'<div class="release-progression">{"".join(cards)}</div>'


def subset_comparison(rows, baselines):
    """The two rows the verdict panel compares: the v2 baseline subset and the
    latest v3 subset run on the SAME subset size (same PRs / goldens).

    Returns ``(v2_row, v3_row, n_goldens)`` or ``(None, None, n)`` when either
    side is missing (e.g. the offline test fixture with no v3 subset)."""
    bv2 = baselines.get("baseline_v2", {})
    n_goldens = bv2.get("n_goldens")
    v2_row = next(
        (r for r in rows
         if r.get("tool") == "deep-review-v2" and r.get("run_id") == bv2.get("run_id")),
        None,
    )
    v3_candidates = [
        r for r in rows
        if r.get("tool") == "deep-review-v3" and r.get("tier") == "subset"
        and (v2_row is None or r.get("n_prs") == v2_row.get("n_prs"))
    ]
    v3_row = max(v3_candidates, key=lambda r: r.get("ts", "")) if v3_candidates else None
    return v2_row, v3_row, n_goldens


def _efficiency(row, n_goldens):
    """Per-run value-efficiency figures. ``goldens`` is tp = round(recall × N) —
    distinct known issues caught, the numerator of golden_recall — NOT the
    per_bucket golden_matched comment count (a comment can match several goldens)."""
    recall = row.get("golden_recall")
    tokens = row.get("tokens_total")
    cost = row.get("cost_usd")
    per_bucket = row.get("per_bucket") or {}
    delivered = sum(per_bucket.values()) if per_bucket else row.get("total_candidates")
    goldens = round(recall * n_goldens) if (recall is not None and n_goldens) else None
    return {
        "recall": recall,
        "noise": row.get("noise_rate"),
        "tokens": tokens,
        "cost": cost,
        "delivered": delivered,
        "goldens": goldens,
        "tok_per_gold": tokens / goldens if (tokens and goldens) else None,
        "cost_per_gold": cost / goldens if (cost and goldens) else None,
    }


def build_verdict_html(rows, baselines, ceiling):
    """Headline panel: did the v3 rewrite beat the v2 baseline on the same subset?

    One row per metric with v2 → v3 values, a signed delta coloured better/worse,
    and volume-normalised efficiency rows (per golden found) that defuse v3's larger
    comment volume. Returns "" if the comparison can't be formed."""
    v2_row, v3_row, n_goldens = subset_comparison(rows, baselines)
    if not (v2_row and v3_row and n_goldens):
        return ""
    a = _efficiency(v2_row, n_goldens)
    b = _efficiency(v3_row, n_goldens)

    def arrow(new, old):
        if new is None or old is None or new == old:
            return "→"
        return "▲" if new > old else "▼"

    # (label, v2 text, v3 text, delta text, kind, note) — kind ∈ good/neutral.
    noise_ok = b["noise"] is not None and ceiling is not None and b["noise"] <= ceiling
    metric_rows = [
        ("Golden recall", fmt_pct(a["recall"]), fmt_pct(b["recall"]),
         fmt_delta_pp(b["recall"], a["recall"]), arrow(b["recall"], a["recall"]),
         "good", "of {n} known issues".format(n=n_goldens)),
        ("Noise rate", fmt_pct(a["noise"]), fmt_pct(b["noise"]),
         fmt_delta_pp(b["noise"], a["noise"]), arrow(b["noise"], a["noise"]),
         "neutral", ("under the " + fmt_pct(ceiling, 0) + " ceiling" if noise_ok
                     else "OVER the " + fmt_pct(ceiling, 0) + " ceiling")),
        ("Goldens found", str(a["goldens"]), str(b["goldens"]),
         f"{b['goldens'] - a['goldens']:+d}", arrow(b["goldens"], a["goldens"]),
         "good", f"of {n_goldens}"),
        ("Tokens per pass", fmt_millions(a["tokens"]), fmt_millions(b["tokens"]),
         fmt_delta_pct(b["tokens"], a["tokens"]), arrow(b["tokens"], a["tokens"]),
         "good", "total review spend, tokens"),
        ("Tokens per golden found", fmt_millions(a["tok_per_gold"], 2),
         fmt_millions(b["tok_per_gold"], 2),
         fmt_delta_pct(b["tok_per_gold"], a["tok_per_gold"]),
         arrow(b["tok_per_gold"], a["tok_per_gold"]), "good",
         "the volume-normalised efficiency"),
        ("Cost per golden found", fmt_money(a["cost_per_gold"]),
         fmt_money(b["cost_per_gold"]),
         fmt_delta_pct(b["cost_per_gold"], a["cost_per_gold"]),
         arrow(b["cost_per_gold"], a["cost_per_gold"]), "good", ""),
        ("Comments delivered", fmt_int(a["delivered"]), fmt_int(b["delivered"]),
         (f"{b['delivered'] / a['delivered']:.1f}×" if a["delivered"] else "—"),
         arrow(b["delivered"], a["delivered"]), "context",
         "~4× more by design — a bigger denominator, not worse findings"),
    ]

    body = []
    for label, v2s, v3s, delta, arr, kind, note in metric_rows:
        note_html = f'<span class="verdict-note">{html.escape(note)}</span>' if note else ""
        body.append(
            "<tr>"
            f'<th scope="row">{html.escape(label)}{note_html}</th>'
            f'<td class="vnum">{html.escape(v2s)}</td>'
            f'<td class="vnum vnum-new">{html.escape(v3s)}</td>'
            f'<td class="vdelta vdelta-{kind}">{arr} {html.escape(delta)}</td>'
            "</tr>"
        )

    # Takeaway sentence, generated from the numbers above.
    ratio = (a["tok_per_gold"] / b["tok_per_gold"]) if b["tok_per_gold"] else None
    ratio_txt = f"{ratio:.1f}× leaner" if ratio else "leaner"
    noise_dir = "rises" if (b["noise"] or 0) > (a["noise"] or 0) else "falls"
    takeaway = (
        f"v3 catches {b['goldens']} of the {n_goldens} known issues to v2's "
        f"{a['goldens']} (+{b['goldens'] - a['goldens']}), at "
        f"{fmt_millions(b['tok_per_gold'], 2)} tokens each vs "
        f"{fmt_millions(a['tok_per_gold'], 2)} ({ratio_txt}); noise {noise_dir} "
        f"{fmt_delta_pp(b['noise'], a['noise']).lstrip('+')} to {fmt_pct(b['noise'])}, "
        f"{'still under' if noise_ok else 'OVER'} the {fmt_pct(ceiling, 0)} ceiling."
    )

    n_prs = v2_row.get("n_prs") or v3_row.get("n_prs")
    subset_desc = f"{n_prs}-PR / {n_goldens}-golden" if n_prs else f"{n_goldens}-golden"
    return (
        '<section class="verdict-panel card" aria-label="v2 versus v3 verdict">'
        '<div class="verdict-head">'
        "<h2>Did the v3 rewrite improve on v2?</h2>"
        f'<p class="verdict-takeaway">{html.escape(takeaway)}</p>'
        "</div>"
        '<div class="table-wrap"><table class="verdict-table">'
        '<thead><tr><th scope="col">Metric</th><th scope="col">v2 baseline</th>'
        '<th scope="col">v3 gate (Gate-1)</th><th scope="col">Change</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
        f'<p class="verdict-foot">Same {html.escape(subset_desc)} subset, judge-pinned. '
        "Efficiency rows divide by <b>goldens found</b> (distinct known issues caught, "
        "the recall numerator) so v3's larger comment volume can't flatter or penalise "
        "it. Green = better; noise rose slightly but cleared the gate.</p>"
        "</section>"
    )


def latest_holdout_row(rows):
    """Most recent v3 holdout-tier run — the fresh-PR confirmation of the gate."""
    cands = [
        r for r in rows
        if str(r.get("tool", "")).startswith("deep-review") and r.get("tier") == "holdout"
    ]
    return max(cands, key=lambda r: r.get("ts", "")) if cands else None


def gate2_row(rows):
    """The Gate-2 final-config run, pinned by run_id (GATE2_RUN_ID).

    Before v3.1, "most recent custom-tier row" and "the Gate-2 run" were the same
    thing; the v3.1 mini-subset experiments are also custom-tier and newer, so that
    heuristic would silently pick one of them up instead. Falls back to the old
    most-recent-custom-row heuristic only when the pinned id isn't in the ledger
    at all (e.g. an offline test fixture predating Gate-2).
    """
    row = row_by_run_id(rows, GATE2_RUN_ID)
    if row is not None:
        return row
    cands = [
        r for r in rows
        if str(r.get("tool", "")).startswith("deep-review") and r.get("tier") == "custom"
    ]
    return max(cands, key=lambda r: r.get("ts", "")) if cands else None


def _tokens_per_pr(row):
    if not row:
        return None
    tok = row.get("tokens_total")
    n = row.get("n_prs")
    return tok / n if (tok and n) else None


def build_gate2_html(rows, baselines, top_anchor, ceiling):
    """Second verdict panel: the FINAL config (Gate-2 custom run) beside the v2
    baseline. Gate-2 ran a different 8-PR set, so its win is stated per-PR-token
    against the owner-amended v2 baseline (recall/noise are graded against the two
    live bars, not v2). Returns "" when no custom run is in the ledger."""
    g2 = gate2_row(rows)
    if not g2:
        return ""
    bv2 = baselines.get("baseline_v2", {})
    v2_row = next(
        (r for r in rows
         if r.get("tool") == "deep-review-v2" and r.get("run_id") == bv2.get("run_id")),
        None,
    )
    g2_tpp = _tokens_per_pr(g2)
    v2_tpp = _tokens_per_pr(v2_row)
    recall = g2.get("golden_recall")
    noise = g2.get("noise_rate")
    n_prs = g2.get("n_prs")

    tok_delta = (
        f"{(g2_tpp - v2_tpp) / v2_tpp * 100:+.1f}%"
        if (g2_tpp and v2_tpp) else "—"
    )
    recall_ok = recall is not None and top_anchor is not None and recall >= top_anchor
    noise_ok = noise is not None and ceiling is not None and noise <= ceiling

    # (label, baseline/bar text, gate-2 text, verdict text, arrow, kind, note)
    metric_rows = [
        ("Tokens per PR", fmt_millions(v2_tpp, 2), fmt_millions(g2_tpp, 2), tok_delta,
         ("▼" if (g2_tpp and v2_tpp and g2_tpp < v2_tpp) else "▲"), "good",
         "vs the v2 baseline, PR-normalised"),
        ("Golden recall", fmt_pct(top_anchor), fmt_pct(recall),
         "held" if recall_ok else "under", ("✓" if recall_ok else "○"),
         "good" if recall_ok else "neutral", "over the top-anchor bar"),
        ("Noise rate", fmt_pct(ceiling), fmt_pct(noise),
         "under" if noise_ok else "OVER", ("✓" if noise_ok else "○"),
         "good" if noise_ok else "neutral", "under the noise ceiling"),
    ]
    body = []
    for label, base_s, g2_s, verdict, arr, kind, note in metric_rows:
        note_html = f'<span class="verdict-note">{html.escape(note)}</span>' if note else ""
        body.append(
            "<tr>"
            f'<th scope="row">{html.escape(label)}{note_html}</th>'
            f'<td class="vnum">{html.escape(base_s)}</td>'
            f'<td class="vnum vnum-new">{html.escape(g2_s)}</td>'
            f'<td class="vdelta vdelta-{kind}">{arr} {html.escape(verdict)}</td>'
            "</tr>"
        )

    takeaway = (
        f"The final config reviews at {fmt_millions(g2_tpp, 2)} tokens/PR against the "
        f"v2 baseline's {fmt_millions(v2_tpp, 2)} ({tok_delta}), while holding the bars "
        f"at {fmt_pct(recall)} recall and {fmt_pct(noise)} noise across {n_prs} fresh "
        "PRs — the Gate-2 pass."
    )
    # Footnote quotes the amendment verbatim from the run's ledger change note.
    amendment = str(g2.get("change") or "").strip()
    foot = (
        "Gate-2 ran a different 8-PR set, so the win is stated as tokens per PR against "
        "the <b>owner-amended v2 baseline</b> (recall and noise are graded against the "
        "two live bars, not v2)."
    )
    if amendment:
        foot += f' Amendment, quoted from the ledger: <q>{html.escape(amendment)}</q>'
    return (
        '<section class="verdict-panel card" aria-label="Gate-2 final config efficiency">'
        '<div class="verdict-head">'
        "<h2>Final config — Gate-2 token efficiency</h2>"
        f'<p class="verdict-takeaway">{html.escape(takeaway)}</p>'
        "</div>"
        '<div class="table-wrap"><table class="verdict-table">'
        '<thead><tr><th scope="col">Metric</th><th scope="col">Baseline / bar</th>'
        '<th scope="col">Gate-2 final</th><th scope="col">Verdict</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
        f'<p class="verdict-foot">{foot}</p>'
        "</section>"
    )


def build_tiles_html(row, n_prs, n_goldens, runs=1):
    """Headline stat tiles for one measurement row. Release-aware: the caller
    picks ``row``/``n_prs``/``n_goldens`` — normally the current release's
    defining leg (see current_release_leg) — rather than this function assuming
    any particular tier or baseline source."""
    per_bucket = (row or {}).get("per_bucket", {})
    total = sum(per_bucket.values()) if per_bucket else None
    total_txt = str(total) if total is not None else "—"
    g = (row or {}).get
    tiles = [
        (fmt_pct(g("golden_recall")), "Golden recall",
         f"{n_prs} PRs · {n_goldens} goldens · N={runs}"),
        (fmt_pct(g("noise_rate")), "Noise rate",
         f"{total_txt} candidates scored · N={runs}"),
        (fmt_pct(g("precision_strict")), "Precision (strict) †",
         "reported, not gated — counts valid-extras as misses"),
        (fmt_pct(g("f1_strict")), "F1 (strict)",
         "recall + precision blend"),
        (fmt_money(g("cost_usd")), "Run cost",
         "one review pass · review spend"),
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


def build_anchor_section_html(baselines, v3_row, v3_holdout_row):
    anchor_rows = baselines.get("anchors", {}).get("rows", {})
    bv2 = baselines.get("baseline_v2", {})
    figs = []
    for v2v3_key, anchor_key, title in ANCHOR_METRICS:
        bars = [
            (label, var, emph,
             _anchor_value(src, v2v3_key, anchor_key, bv2, v3_row, v3_holdout_row,
                           anchor_rows))
            for label, var, emph, src in ANCHOR_TOOLS
        ]
        bars = [b for b in bars if b[3] is not None]
        figs.append(
            f'<figure class="anchor-chart"><figcaption>{html.escape(title)}</figcaption>'
            f"{build_anchor_svg(title, bars)}</figure>"
        )
    gate_n = (v3_row or {}).get("n_prs")
    gate_desc = f"the same {gate_n}-PR gate" if gate_n else "the same gate"
    holdout_n = (v3_holdout_row or {}).get("n_prs")
    corroboration = (
        f" The <em>v3 · holdout</em> bar ({holdout_n} fresh PRs) corroborates the lead "
        "on unseen PRs." if v3_holdout_row else ""
    )
    caption = (
        f"Every tool judged on {gate_desc}. <em>deep-review v3</em> leads on "
        "recall while holding noise far below the external tools; <em>claude</em> is "
        "the published Claude Code CLI row. Anchors are the upstream tools’ stored "
        "candidates re-judged under our pinned judge." + corroboration
    )
    return (
        f'<div class="anchor-row">{"".join(figs)}</div>'
        f'<p class="caption">{caption}</p>'
    )


def build_table_html(groups, top_anchor, ceiling):
    heads = [
        "", "Date", "Run", "Tool", "Tier", "PRs", "Delivered", "Recall",
        "Valid-extra", "Noise", "Precision †", "F1", "Tokens", "Cost",
        "What changed",
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
        pb = r.get("per_bucket") or {}
        delivered = sum(pb.values()) if pb else r.get("total_candidates")
        kind, glyph, desc = classify(r, top_anchor, ceiling)
        # "What changed" cell: the outcome line, else the hypothesis; full text on hover.
        # Confounded child-model experiments carry no annotation — synthesize one from
        # the reverted verdict and the run's own dominant (child) model.
        changed_full = r.get("change") or r.get("hypothesis") or ""
        parts_full = []
        if r.get("hypothesis"):
            parts_full.append("Hypothesis: " + r["hypothesis"])
        if r.get("change"):
            parts_full.append("Change: " + r["change"])
        if not changed_full and rid in CONFOUNDED_RUNS:
            child = _short_model(_dominant_model(r)) or "child model"
            changed_full = (
                f"Confounded experiment ({child} subagent pin cascaded the [1m] variant "
                "onto the pipeline agents); reverted."
            )
            parts_full.append(changed_full)
        if not changed_full and rid in VOID_RUNS:
            changed_full = VOID_RUNS[rid]
            parts_full.append(changed_full)
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
            f'<td class="num">{fmt_int(delivered)}</td>'
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
        "<b>† Precision (strict)</b> is reported, not gated: it counts valid-extras "
        "— real issues outside the golden answer key — as misses, so it drops as a "
        "run delivers more comments even when quality holds. Noise rate is the gated "
        "‘how often are we wrong’ metric.",
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
/* Release progression (headline section) */
.release-progression {
  display: grid;
  gap: 12px;
  margin: 0 0 22px;
}
.release-card {
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: 10px;
  padding: 14px 16px 12px;
}
.release-card:first-child {
  border-color: var(--ref-goal);
}
.release-label {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.release-legs { display: grid; gap: 4px; margin-bottom: 8px; }
.release-leg {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
  font-size: 12.5px;
}
.release-leg-name { color: var(--ink-secondary); font-weight: 600; min-width: 15ch; }
.release-leg-metrics {
  color: var(--ink-primary);
  font-variant-numeric: tabular-nums;
}
.release-leg-run {
  color: var(--ink-muted);
  font-size: 11px;
}
.release-changed {
  margin: 0 0 4px;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--ink-primary);
  max-width: 74ch;
}
.release-changed b { color: var(--ink-secondary); font-weight: 650; }
.release-note {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--ink-muted);
  max-width: 74ch;
}
/* History (demoted v2->v3 rewrite panels) */
.history-section {
  margin: 0 0 22px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  background: var(--surface);
  padding: 4px 16px;
}
.history-section summary {
  cursor: pointer;
  padding: 12px 0;
  font-size: 14px;
  font-weight: 650;
  color: var(--ink-secondary);
}
.history-section[open] summary { color: var(--ink-primary); }
.history-body { padding: 4px 0 14px; }
.history-body .verdict-panel {
  border: none;
  background: transparent;
  padding: 10px 0 0;
}
/* Verdict panel (v2 vs v3 headline) */
.verdict-panel { margin: 0 0 22px; padding: 18px 18px 14px; }
.verdict-head h2 { margin: 0 0 6px; font-size: 17px; font-weight: 650; }
.verdict-takeaway {
  margin: 0 0 12px;
  font-size: 13.5px;
  line-height: 1.5;
  color: var(--ink-primary);
  max-width: 62ch;
}
.verdict-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.verdict-table thead th {
  font-size: 11px;
  font-weight: 650;
  text-transform: uppercase;
  letter-spacing: .03em;
  color: var(--ink-muted);
  padding: 4px 12px 8px;
  border-bottom: 1px solid var(--hairline);
  white-space: nowrap;
}
.verdict-table thead th:first-child { text-align: left; }
.verdict-table thead th:not(:first-child) { text-align: right; }
.verdict-table tbody th {
  text-align: left;
  font-weight: 550;
  color: var(--ink-primary);
  padding: 8px 12px;
  border-bottom: 1px solid var(--hairline);
}
.verdict-note {
  display: block;
  font-weight: 400;
  font-size: 11px;
  color: var(--ink-muted);
  margin-top: 1px;
}
.verdict-table tbody tr:last-child th,
.verdict-table tbody tr:last-child td { border-bottom: none; }
td.vnum {
  text-align: right;
  padding: 8px 12px;
  border-bottom: 1px solid var(--hairline);
  font-variant-numeric: tabular-nums;
  color: var(--ink-secondary);
  white-space: nowrap;
}
td.vnum-new { color: var(--ink-primary); font-weight: 640; }
td.vdelta {
  text-align: right;
  padding: 8px 12px;
  border-bottom: 1px solid var(--hairline);
  font-variant-numeric: tabular-nums;
  font-weight: 640;
  white-space: nowrap;
}
.vdelta-good { color: var(--ref-goal); }
.vdelta-neutral { color: var(--ink-secondary); }
.vdelta-context { color: var(--ink-muted); font-weight: 550; }
.verdict-foot {
  margin: 12px 0 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--ink-muted);
  max-width: 74ch;
}
.verdict-foot b { color: var(--ink-secondary); }
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

    _v2_row, v3_gate_row, _ng = subset_comparison(rows, baselines)
    v3_holdout_row = latest_holdout_row(rows)

    # Headline stat tiles track the current release's defining measurement (the
    # v3.1 mini-subset, once it's in the ledger) rather than a fixed tier; this
    # falls back to the pre-v3.1 "latest gate-grade subset" behaviour when none
    # of the RELEASES run_ids are present (e.g. an offline test fixture).
    rel, leg, release_row = current_release_leg(rows)
    bv2 = baselines.get("baseline_v2", {})
    if release_row is not None:
        src_label = (
            '<p class="tiles-source">Headline numbers below are the current release: '
            f'<b>{html.escape(rel["label"])}</b> · {html.escape(leg["name"])} · run '
            f'<span class="mono">{html.escape(truncate_middle(str(leg["run_id"]), 32))}</span>'
            f" · {html.escape(fmt_date(release_row.get('ts', '')))}</p>"
        )
        tiles_html = build_tiles_html(
            release_row,
            release_row.get("n_prs", "—"),
            leg.get("n_goldens", bv2.get("n_goldens", "—")),
        )
    else:
        subset_row = latest_subset_row(rows)
        src_label = ""
        if subset_row:
            tname = TOOL_STYLE.get(
                subset_row.get("tool"), (str(subset_row.get("tool")), "")
            )[0]
            src_label = (
                '<p class="tiles-source">Headline numbers below are the latest gate-grade '
                f"run: <b>{html.escape(tname)}</b> · {html.escape(str(subset_row.get('tier', '')))}"
                " tier · run "
                f'<span class="mono">{html.escape(truncate_middle(str(subset_row.get("run_id", "")), 32))}</span>'
                f" · {html.escape(fmt_date(subset_row.get('ts', '')))}</p>"
            )
        tiles_html = build_tiles_html(
            subset_row, (subset_row or {}).get("n_prs", "—"), bv2.get("n_goldens", "—")
        )

    runs_caption = (
        "Every scored run in time order — the v2 baseline, then the v3 hill-climb, "
        "then the v3.1 mini-subset experiments. Colour is the tool, marker shape is "
        "the run type; faded markers were reverted or voided (confounded child-model "
        "smokes, a contaminated smoke, and a mini-subset attempt that failed its "
        "paired bar), and the haloed markers are the Gate-1 subset, its holdout "
        "confirmation, the Gate-2 final config, and the v3.1 mini-subset of record. "
        "The labelled reference lines are the v2 baseline, the top-anchor bar to "
        "beat, and the noise ceiling. Hover or focus any point for its hypothesis "
        "and result."
    )

    history_html = (
        '<details class="history-section">'
        "<summary>History: the v2→v3 rewrite</summary>"
        '<div class="history-body">'
        + build_verdict_html(rows, baselines, ceiling)
        + build_gate2_html(rows, baselines, top_anchor, ceiling)
        + "</div></details>"
    )

    body = [
        '<div class="viz-root">',
        '<div id="viz-tooltip" class="tooltip" role="status" aria-live="polite"></div>',
        "<header>",
        "<h1>code-gauntlet bench — release progression</h1>",
        f'<p class="subtitle">{subtitle}</p>',
        "</header>",
        "<h2>Release progression</h2>",
        build_releases_html(rows, top_anchor, v2_base, ceiling),
        build_explainer_html(top_anchor, v2_base, ceiling),
        src_label,
        tiles_html,
        "<h2>Performance over runs</h2>",
        '<div class="card">',
        build_runs_legend_html(),
        build_runs_svg(points, top_anchor, v2_base, ceiling),
        "</div>",
        f'<p class="caption">{runs_caption}</p>',
        "<h2>vs anchors (judge-only)</h2>",
        build_anchor_section_html(baselines, v3_gate_row, v3_holdout_row),
        history_html,
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
        "<title>code-gauntlet bench — release progression</title>",
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
