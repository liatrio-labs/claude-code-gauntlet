"""Measure rule-citation absence preambles in captured bench artifacts."""

import argparse
import json
import re
from pathlib import Path

# This is a lower-bound heuristic because agents can phrase the same absence many ways.
ABSENCE_PREAMBLE_RE = re.compile(
    r"\bno\s+(?:\w+\s+){0,3}(?:CLAUDE|AGENTS|REVIEW|QODO)\.md\b"
    r"|(?:CLAUDE|AGENTS|REVIEW|QODO)\.md(?:/\w+\.md)*(?:\s+\w+){0,3}\s+(?:does\s+not\s+exist|doesn't\s+exist|is\s+absent|is\s+missing|absent|missing)\b"
    r"|\bno\s+(?:\w+[\s-]+){0,3}(?:convention|rules?|house\s+style|style\s+guide)\s*(?:files?|documents?|guides?)?\b(?=[^\w]|$)"
    r"|\bno\s+(?:documented|written|explicit|project|repo(?:sitory)?|machine-readable|codified)\s+(?:\w+\s+){0,2}(?:rules?|conventions?|guidelines?|standards?)\b"
    r"|\bproject_rules_absent\b",
    re.IGNORECASE,
)

_FINDINGS_GLOB = "code-gauntlet-findings-*.json"
_POST_REVIEW_GLOB = "code-gauntlet-post-review-*.json"


def has_absence_preamble(value) -> bool:
    """Return whether *value* contains a rule-file or rule-absence preamble."""
    return isinstance(value, str) and ABSENCE_PREAMBLE_RE.search(value) is not None


def _pr_dirs(run_dir):
    """Return direct PR artifact directories, excluding archived trees."""
    path = Path(run_dir)
    if not path.is_dir():
        return []
    return sorted(
        child
        for child in path.iterdir()
        if child.is_dir() and child.name.startswith("pr-")
    )


def _iter_artifact_files(pr_dir, pattern):
    """Return direct regular-file matches for an artifact pattern."""
    return sorted(path for path in Path(pr_dir).glob(pattern) if path.is_file())


def _findings_list(data):
    """Normalize a bare or ``{"findings": [...]}`` artifact to a list."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        return data["findings"]
    return None


def _read_citations(pr_dir, pattern):
    """Yield citation tuples from direct JSON artifacts matching *pattern*."""
    for artifact in _iter_artifact_files(pr_dir, pattern):
        try:
            with artifact.open(encoding="utf-8") as handle:
                findings = _findings_list(json.load(handle))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if findings is None:
            continue
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            value = finding.get("claude_md_rule")
            if not isinstance(value, str) or not value.strip():
                continue
            yield (
                finding.get("id"),
                finding.get("dimension"),
                value,
                finding.get("rule_source"),
            )


def iter_citations(pr_dir):
    """Yield ``(finding_id, dimension, value, rule_source)`` citation tuples."""
    yield from _read_citations(pr_dir, _FINDINGS_GLOB)


def _iter_post_review_citations(pr_dir):
    yield from _read_citations(pr_dir, _POST_REVIEW_GLOB)


def _measure_block(citations, pr_names):
    by_dimension = {}
    rule_source = {"absent": 0}
    per_pr = {}
    total = 0
    absence = 0

    for pr_name in pr_names:
        per_pr[pr_name] = {"populated": 0, "absence_preamble": 0}

    for pr_name, pr_citations in citations.items():
        per_pr.setdefault(pr_name, {"populated": 0, "absence_preamble": 0})
        for _finding_id, dimension, value, source in pr_citations:
            total += 1
            per_pr[pr_name]["populated"] += 1
            is_absent = has_absence_preamble(value)
            absence += is_absent
            per_pr[pr_name]["absence_preamble"] += is_absent
            dimension_stats = by_dimension.setdefault(
                dimension, {"populated": 0, "absence_preamble": 0}
            )
            dimension_stats["populated"] += 1
            dimension_stats["absence_preamble"] += is_absent
            if isinstance(source, str) and source:
                rule_source[source] = rule_source.get(source, 0) + 1
            else:
                rule_source["absent"] = rule_source.get("absent", 0) + 1

    return {
        "populated": total,
        "absence_preamble": absence,
        "rate": absence / total if total else None,
        "by_dimension": by_dimension,
        "rule_source": rule_source,
        "per_pr": per_pr,
    }


def measure_run(run_dir) -> dict:
    """Measure findings survivors and delivered post-review findings separately."""
    pr_dirs = _pr_dirs(run_dir)
    pr_names = [path.name for path in pr_dirs]
    findings = {path.name: list(iter_citations(path)) for path in pr_dirs}
    post_review = {
        path.name: list(_iter_post_review_citations(path)) for path in pr_dirs
    }
    return {
        "findings": _measure_block(findings, pr_names),
        "post_review": _measure_block(post_review, pr_names),
    }


def _rate_text(block):
    populated = block["populated"]
    rate = "n/a" if not populated else f"{block['rate']:.3f}"
    return f"{block['absence_preamble']}/{populated} ({rate})"


def _convention_text(measurement):
    convention = measurement["findings"]["by_dimension"].get(
        "convention", {"populated": 0, "absence_preamble": 0}
    )
    return f"{convention['absence_preamble']}/{convention['populated']}"


def _merge_blocks(measurements, block_name):
    merged = {
        "populated": 0,
        "absence_preamble": 0,
        "rate": None,
        "by_dimension": {},
        "rule_source": {"absent": 0},
        "per_pr": {},
    }
    for run_id, measurement in measurements:
        block = measurement[block_name]
        merged["populated"] += block["populated"]
        merged["absence_preamble"] += block["absence_preamble"]
        for dimension, stats in block["by_dimension"].items():
            target = merged["by_dimension"].setdefault(
                dimension, {"populated": 0, "absence_preamble": 0}
            )
            target["populated"] += stats["populated"]
            target["absence_preamble"] += stats["absence_preamble"]
        for source, count in block["rule_source"].items():
            merged["rule_source"][source] = merged["rule_source"].get(source, 0) + count
        for pr_name, stats in block["per_pr"].items():
            target = merged["per_pr"].setdefault(
                f"{run_id}/{pr_name}", {"populated": 0, "absence_preamble": 0}
            )
            target["populated"] += stats["populated"]
            target["absence_preamble"] += stats["absence_preamble"]
    if merged["populated"]:
        merged["rate"] = merged["absence_preamble"] / merged["populated"]
    return merged


def _total_measurement(measurements):
    return {
        "findings": _merge_blocks(measurements, "findings"),
        "post_review": _merge_blocks(measurements, "post_review"),
    }


def _print_human(run_id, measurement):
    findings = measurement["findings"]
    post_review = measurement["post_review"]
    return (
        f"{run_id} findings={_rate_text(findings)} "
        f"post_review={_rate_text(post_review)} "
        f"convention={_convention_text(measurement)}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", metavar="RUN_DIR")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    measurements = [
        (Path(run_dir).name, measure_run(run_dir)) for run_dir in args.run_dirs
    ]
    total = _total_measurement(measurements)
    if args.as_json:
        records = [
            {"run_id": run_id, **measurement} for run_id, measurement in measurements
        ]
        records.append({"run_id": "total", **total})
        print(json.dumps(records, ensure_ascii=False))
    else:
        for run_id, measurement in measurements:
            print(_print_human(run_id, measurement))
        print(_print_human("total", total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
