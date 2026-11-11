"""Compare typed validation replays and volume policies without judging unmatched places."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Literal

Route = Literal[
    "posted",
    "posted-latent",
    "posted-artifact",
    "report-only",
    "eliminated",
    "not-established",
]
ROUTES: tuple[Route, ...] = (
    "posted",
    "posted-latent",
    "posted-artifact",
    "report-only",
    "eliminated",
    "not-established",
)


@dataclass(frozen=True, slots=True)
class Sample:
    arm: str
    pass_id: str
    pr: str
    findings: tuple[dict[str, Any], ...]
    bugs: frozenset[str]
    changed_lines: int


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_replays(root):
    if read_json(root / "replay.json")["dry_run"]:
        raise ValueError("dry runs contain no verdict observations")
    manifest = read_json(root / "replay.json")
    samples = []
    for path in sorted(root.glob("replay-*/*/outcomes.json")):
        result = read_json(path)
        source = read_json(path.with_name("source.json"))
        samples.append(
            Sample(
                "treatment",
                path.parent.parent.name,
                path.parent.name,
                tuple(result["findings"]),
                frozenset(source["bugs"]),
                result["changed_lines"],
            )
        )
    expected = manifest["replays"]
    if len({s.pass_id for s in samples}) != expected:
        raise ValueError("missing replay passes")
    if "prs" in manifest:
        expected_prs = set(manifest["prs"])
        for pass_id in {s.pass_id for s in samples}:
            if {s.pr for s in samples if s.pass_id == pass_id} != expected_prs:
                raise ValueError("incomplete PR population")
    return samples


def load_baseline(pack):
    known = read_json(pack / "known_bugs.json")
    bugs = {
        row["pr"]: frozenset(bug["id"] for bug in row["known_bugs"])
        for row in known.values()
    }
    samples = []
    for path in sorted((pack / "validator_passes").glob("pass*/*.json")):
        data = read_json(path)
        rows = data["findings"] if isinstance(data, dict) else data
        findings = []
        for index, original in enumerate(rows):
            row = dict(original)
            row["route"] = (
                (
                    "posted-latent"
                    if row.get("reachability") == "future_change_only"
                    else "posted"
                )
                if row["passes_floor"]
                else "eliminated"
            )
            row["rank_index"] = index
            findings.append(row)
        diffs = sorted((pack / "run").glob("*/diff.patch"))
        # The pack's PR directory names are prefixed, unlike its validator filenames.
        candidates = [
            p for p in diffs if p.parent.name.removeprefix("pr-") == path.stem
        ]
        if len(candidates) != 1:
            raise ValueError(f"missing baseline diff for {path.stem}")
        samples.append(
            Sample(
                "baseline",
                path.parent.name,
                path.stem,
                tuple(findings),
                bugs[path.stem],
                patch_size(candidates[0]),
            )
        )
    return samples


def patch_size(path):
    return sum(
        line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def place_keys(findings, proximity):
    by_file = defaultdict(list)
    for original in findings:
        f = {
            **original,
            "line_start": original.get(
                "line_start", original.get("claimed_line_start")
            ),
        }
        if (
            not isinstance(f.get("line_start"), int)
            or f["line_start"] < 1
            or not f.get("file")
        ):
            raise ValueError(f"finding lacks a source place: {f.get('id')}")
        by_file[f["file"]].append(f)
    keys = {}
    for file, rows in by_file.items():
        previous, cluster = -proximity - 1, 0
        for f in sorted(rows, key=lambda f: (f["line_start"], f["id"])):
            if f["line_start"] - previous > proximity:
                cluster += 1
            keys[f["id"]] = (file, cluster)
            previous = f["line_start"]
    return keys


def policy_routes(sample, bounded):
    routes = {f["id"]: f["route"] for f in sample.findings}
    if any(route not in ROUTES for route in routes.values()) or len(routes) != len(
        sample.findings
    ):
        raise ValueError("invalid routes or duplicate finding IDs")
    cap = 2 + math.ceil(math.sqrt(sample.changed_lines / 100))
    candidates = sorted(
        (
            f
            for f in sample.findings
            if routes[f["id"]].startswith("posted")
            and (f.get("severity") == "low" or routes[f["id"]] == "posted-latent")
        ),
        key=lambda f: (
            10**9 if f.get("rank_index") is None else f["rank_index"],
            f["id"],
        ),
    )
    if bounded:
        for f in candidates[cap:]:
            routes[f["id"]] = "report-only"
    return routes, cap


def sample_tables(sample, reader, bounded, proximity):
    routes, cap = policy_routes(sample, bounded)
    places = place_keys(sample.findings, proximity)
    known_places = {
        places[f["id"]] for f in sample.findings if f.get("matched_bug_ids")
    }
    counts = {}
    for route in ROUTES:
        rows = [f for f in sample.findings if routes[f["id"]] == route]
        bugs = {bug for f in rows for bug in f.get("matched_bug_ids", [])}
        if not bugs.issubset(sample.bugs):
            raise ValueError("unknown bug attribution")
        selected = {places[f["id"]] for f in rows}
        rated = [
            (f, reader[(sample.pr, f["id"])])
            for f in rows
            if (sample.pr, f["id"]) in reader
        ]
        counts[route] = dict(
            reader_matches=len(rated),
            reader_claim_agreements=sum(
                {
                    "confirmed": "holds",
                    "refuted": "refuted",
                    "not_established": "unverified",
                }.get((f.get("verdict") or {}).get("claim"))
                == r["claim"]
                for f, r in rated
            ),
            reader_worth_yes=sum(r["worth"] == "yes" for _, r in rated),
            reader_worth_no=sum(r["worth"] == "no" for _, r in rated),
            known_bugs=len(bugs),
            matching_places=len(selected & known_places),
            unmatched_places=len(selected - known_places),
            findings=len(rows),
        )
    claim, worth = Counter(), Counter()
    matched = 0
    for f in sample.findings:
        independent = reader.get((sample.pr, f["id"]))
        if not independent:
            continue
        matched += 1
        verdict = f.get("verdict") or {}
        claim[(verdict.get("claim", "no_typed_verdict"), independent["claim"])] += 1
        consequence = verdict.get("consequence")
        worth[
            (
                "not_applicable"
                if verdict.get("claim") in ("refuted", "not_established")
                else "yes"
                if consequence in ("present_failure", "latent", "artifact_defect")
                else "no"
                if consequence == "no_practical_effect"
                else "no_typed_verdict",
                independent["worth"],
            )
        ] += 1
    return dict(
        pr=sample.pr,
        cap=cap,
        changed_lines=sample.changed_lines,
        routes=counts,
        claim_confusion=dict_table(claim),
        worth_confusion=dict_table(worth),
        reader_matches=matched,
        known_bug_denominator=len(sample.bugs),
    ), routes


def dict_table(counter):
    table = defaultdict(dict)
    for (predicted, actual), count in sorted(counter.items()):
        table[predicted][actual] = count
    return dict(table)


def total_tables(rows):
    routes = {
        route: {
            key: sum(row["routes"][route][key] for row in rows)
            for key in rows[0]["routes"][route]
        }
        for route in ROUTES
    }
    tables = {}
    for name in ("claim_confusion", "worth_confusion"):
        counter = Counter()
        for row in rows:
            for predicted, actuals in row[name].items():
                for actual, count in actuals.items():
                    counter[predicted, actual] += count
        tables[name] = dict_table(counter)
    return dict(
        routes=routes,
        **tables,
        reader_matches=sum(row["reader_matches"] for row in rows),
        known_bug_denominator=sum(row["known_bug_denominator"] for row in rows),
    )


def average_tables(totals):
    # Missing confusion cells are zero observations, never absent from the denominator.
    result = {
        "routes": {
            route: {
                key: mean(t["routes"][route][key] for t in totals)
                for key in totals[0]["routes"][route]
            }
            for route in ROUTES
        }
    }
    for name in ("claim_confusion", "worth_confusion"):
        pairs = {
            (p, a) for t in totals for p, actuals in t[name].items() for a in actuals
        }
        result[name] = dict_table(
            Counter(
                {
                    (p, a): mean(t[name].get(p, {}).get(a, 0) for t in totals)
                    for p, a in pairs
                }
            )
        )
    result["reader_matches"] = mean(t["reader_matches"] for t in totals)
    result["known_bug_denominator"] = totals[0]["known_bug_denominator"]
    return result


def analyze(samples, reader_rows, expected_bugs=61, proximity=5):
    reader = {(row["pr"], row["id"]): row for row in reader_rows}
    if len(reader) != len(reader_rows):
        raise ValueError("duplicate independent reader IDs")
    result = {
        "policies": {},
        "definitions": {
            "bounded_cap": "2 + ceil(sqrt(changed_lines / 100)); shared budget for low severity and latent findings per PR",
            "places": f"canonical or claimed start in same file, consecutive starts at most {proximity} lines apart; clusters fixed before routing; a place matches if any member matches",
            "confusion_axes": "rows=validator; columns=independent reader; worth yes=present_failure, latent or artifact_defect",
            "baseline": "stored passes_floor routes, not a reconstruction of full production delivery; no typed answers inferred from scores",
        },
    }
    for policy in ("all", "bounded"):
        arms = {}
        for arm in sorted({s.arm for s in samples}):
            passes = []
            previous = None
            observations = []
            reference = None
            for pass_id in sorted({s.pass_id for s in samples if s.arm == arm}):
                batch = sorted(
                    (s for s in samples if s.arm == arm and s.pass_id == pass_id),
                    key=lambda s: s.pr,
                )
                signatures = {(s.pr, f["id"]) for s in batch for f in s.findings}
                if reference is not None and signatures != reference:
                    raise ValueError("passes have different findings or PR coverage")
                reference = signatures
                if len({s.pr for s in batch}) != len(batch):
                    raise ValueError("duplicate PR sample")
                bugs = {(s.pr, b) for s in batch for b in s.bugs}
                if len(bugs) != expected_bugs:
                    raise ValueError(
                        f"expected {expected_bugs} known bugs, found {len(bugs)}"
                    )
                rows, current = [], {}
                for sample in batch:
                    table, routes = sample_tables(
                        sample, reader, policy == "bounded", proximity
                    )
                    rows.append(table)
                    current.update(
                        {(sample.pr, fid): route for fid, route in routes.items()}
                    )
                changed = (
                    None
                    if previous is None
                    else sum(current[k] != previous[k] for k in current)
                )
                passes.append(
                    dict(
                        pass_id=pass_id,
                        per_pr=rows,
                        total=total_tables(rows),
                        changed_since_previous=changed,
                    )
                )
                observations.append(current)
                previous = current
            varying = sum(
                len({row[key] for row in observations}) > 1 for key in reference
            )
            average_per_pr = {
                pr: average_tables(
                    [
                        next(row for row in p["per_pr"] if row["pr"] == pr)
                        for p in passes
                    ]
                )
                for pr in sorted({s.pr for s in samples if s.arm == arm})
            }
            arms[arm] = dict(
                passes=passes,
                average=average_tables([p["total"] for p in passes]),
                average_per_pr=average_per_pr,
                findings_changing_route=varying,
                average_adjacent_changes=mean(
                    p["changed_since_previous"] for p in passes[1:]
                )
                if len(passes) > 1
                else None,
            )
        result["policies"][policy] = arms
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--second-reader", type=Path, required=True)
    parser.add_argument("--baseline-pack", type=Path)
    parser.add_argument("--expected-known-bugs", type=int, default=61)
    parser.add_argument("--nearby-lines", type=int, default=5)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.nearby_lines < 0:
        parser.error("nearby-lines must be nonnegative")
    samples = load_replays(args.replay_dir)
    if args.baseline_pack:
        samples.extend(load_baseline(args.baseline_pack))
    result = analyze(
        samples,
        read_json(args.second_reader),
        args.expected_known_bugs,
        args.nearby_lines,
    )
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0
