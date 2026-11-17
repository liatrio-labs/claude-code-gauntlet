#!/usr/bin/env python3
"""Write the current release's benchmark results into README.md and bench/README.md.

The first ``RELEASES`` entry in bench/report.py holds the only copy of the
published numbers. Every span that shows them is generated here, and a bench
test fails when a committed span differs from a fresh render::

    python3 bench/publish_results.py          # rewrite the spans
    python3 bench/publish_results.py --check  # exit 1 when a span is stale

A span is either the lines between ``<!-- bench:NAME:begin -->`` and
``<!-- bench:NAME:end -->``, or one line ending in ``<!-- bench:NAME -->``.
"""

import argparse
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.report import DEFAULT_LEDGER, RELEASES, load_ledger  # noqa: E402

FILES = {"README.md": ("pitch", "cost", "results"), "bench/README.md": ("release",)}
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def _whole(value):
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    return "\n".join([*lines, *("| " + " | ".join(row) + " |" for row in rows)])


def _checked(release, run_ids):
    """Return the pooled entry once its shape agrees with itself and the ledger."""
    if "pooled" not in release:
        raise ValueError(f"{release['label']}: only a pooled release can be published")
    p = release["pooled"]
    keys = [m.key for m in p["metrics"]]
    missing = [run for lane in p["lanes"] for run in lane if run not in run_ids]
    problems = [
        "repeated metric keys" if len(set(keys)) != len(keys) else "",
        "a metric lacks a value per lane"
        if any(len(m.lanes) != len(p["lanes"]) for m in p["metrics"])
        else "",
        "auth modes do not cover every review"
        if sum(p["auth_modes"].values()) != len(p["lanes"]) * p["prs"]
        else "",
        f"lane runs missing from the ledger: {', '.join(missing)}" if missing else "",
    ]
    if any(problems):
        raise ValueError(f"{release['label']}: " + "; ".join(filter(None, problems)))
    return p


def render(release, run_ids):
    """Map each span name to its generated text."""
    p = _checked(release, run_ids)
    metrics = {m.key: m for m in p["metrics"]}
    lanes = len(p["lanes"])
    times = "once" if lanes == 1 else f"{WORDS.get(lanes, lanes)} times"
    runs = "one run" if lanes == 1 else f"{WORDS.get(lanes, lanes)} runs"
    reviews = lanes * p["prs"]
    minutes, cost, auth = p["minutes"], p["cost_usd"], p["auth_modes"]

    def pct(key):
        return f"{_whole(metrics[key].pooled.rstrip('%'))}%"

    pitch = (
        "- **It catches real bugs.** On a [public benchmark](#benchmark-results) of "
        f"{p['prs']} real PRs with {p['bugs']} known bugs, it caught {pct('full')} of them "
        f"over {runs}, and {pct('unseen')} on the PRs outside our tuning set."
    )
    session = (
        f"The benchmark below ran from a session on {p['session']}.\n"
        f"There, a review took a median of {_whole(minutes['median'])} minutes, "
        f"from {_whole(minutes['low'])} to {_whole(minutes['high'])}.\n"
        "Claude Code's own cost estimate at API prices had a median of "
        f"${cost['median']:.2f} per review, and 80% fell between ${cost['p10']:.2f} "
        f"and ${cost['p90']:.2f}."
    )
    results = "\n\n".join(
        [
            f"We reviewed the same {p['prs']} PRs {times} on Claude Code "
            f"{p['claude_code']}, from a session on {p['session']}.\n"
            f"A pinned judge, `{p['judge']}`, scored every comment.",
            _table(
                ("Measure", release["label"]),
                [(m.title(p), m.pooled) for m in metrics.values()],
            ),
            "\n".join(p["readme_note"]),
        ]
    )
    history = "\n\n".join(
        [
            f"The {release['label']} result covers the Full test set: {p['prs']} PRs "
            f"with {p['bugs']} bugs, reviewed {times}.\n"
            f"Session: {p['session']}; judge: `{p['judge']}`; Claude Code {p['claude_code']}.\n"
            + p["note"],
            "\n".join(
                f"- Lane {i}: " + " + ".join(f"`{run}`" for run in lane)
                for i, lane in enumerate(p["lanes"], 1)
            ),
            _table(
                ("Measure", *(f"Lane {i}" for i in range(1, lanes + 1)), "Pooled"),
                [
                    (
                        m.title(p),
                        *m.lanes,
                        f"{m.pooled} ({m.count})" if m.count else m.pooled,
                    )
                    for m in metrics.values()
                ],
            ),
            f"Time and cost cover the {reviews} pooled reviews.\n"
            "Wall time, from each attempt's recorded start and end, had a median of "
            f"{minutes['median']} minutes and ranged from {minutes['low']} to {minutes['high']}.\n"
            "Cost is Claude Code's own `total_cost_usd` for each review: "
            f"median ${cost['median']:.2f}, 10th percentile ${cost['p10']:.2f}, "
            f"90th percentile ${cost['p90']:.2f}.\n"
            f"{auth['subscription']} of the {reviews} reviews ran on a subscription, so for "
            "those the cost figure is Claude Code's estimate at API prices, not billed spend.\n"
            f"The other {auth['api']} are `auth_mode=api` spend.",
            "\n".join(p["history_note"]),
        ]
    )
    return {"pitch": pitch, "cost": session, "results": results, "release": history}


def apply(text, names, spans):
    """Replace each named span in ``text``; a missing or repeated marker is an error."""
    for name in names:
        body = spans[name]
        tag = f"<!-- bench:{name} -->"
        line = re.compile(r"^.*" + re.escape(tag) + r"$", re.M)
        block = re.compile(
            re.escape(f"<!-- bench:{name}:begin -->")
            + r"\n.*?"
            + re.escape(f"<!-- bench:{name}:end -->"),
            re.S,
        )
        pattern, new = (
            (line, f"{body} {tag}")
            if line.search(text)
            else (
                block,
                f"<!-- bench:{name}:begin -->\n{body}\n<!-- bench:{name}:end -->",
            )
        )
        if len(pattern.findall(text)) != 1:
            raise ValueError(f"expected one bench:{name} span")
        text = pattern.sub(lambda _match, new=new: new, text)
    return text


def main(argv=None, root=REPO_ROOT, releases=RELEASES, ledger=DEFAULT_LEDGER):
    parser = argparse.ArgumentParser(
        description="Publish benchmark results into the READMEs."
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when a span is stale"
    )
    args = parser.parse_args(argv)
    run_ids = {row.get("run_id") for row in load_ledger(ledger)}
    spans = render(releases[0], run_ids)
    pending = []
    for name, span_names in FILES.items():
        path = Path(root) / name
        text = path.read_text(encoding="utf-8")
        fresh = apply(text, span_names, spans)
        if fresh != text:
            pending.append((name, path, fresh))
    # Every span renders before any file is written, so the files never disagree.
    if not args.check:
        for _name, path, fresh in pending:
            path.write_text(fresh, encoding="utf-8")
    stale = [name for name, _path, _fresh in pending]
    verb = "stale" if args.check else "updated"
    for name in stale:
        print(f"{verb}: {name}")
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
