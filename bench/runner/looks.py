"""Append-only registration, gate looks and the final holdout authorization."""

import argparse
import fcntl
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Literal, TypedDict

from bench.runner.epochs import canonical, digest, read_json
from bench.runner.observations import exclusive_json
from bench.runner.validity import timestamp, utc_now

LEDGER = Path(__file__).resolve().parents[1] / "golden/looks.jsonl"


class LookEntry(TypedDict, total=False):
    kind: Literal["registration", "look", "unseal", "holdout-use"]
    at: str
    wave: str
    rule_sha256: str
    look: int
    treatments: list[str]
    score_refs: list[dict[str, str]]
    verdict: Literal["pass", "fail", "inconclusive"]
    report_sha256: str
    final_sha: str
    use: Literal["run", "audit", "compare"]
    receipt_sha256: str


def entries() -> list[LookEntry]:
    return [
        json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines()
    ]


def append(row: LookEntry, check):
    with LEDGER.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        check([json.loads(line) for line in stream])
        stream.write(canonical(row).decode() + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def registration(wave, rule_sha256, *, started=None, rows=None):
    rows = entries() if rows is None else rows
    registered = [r for r in rows if r["kind"] == "registration" and r["wave"] == wave]
    if len(registered) != 1 or registered[0]["rule_sha256"] != rule_sha256:
        raise ValueError("wave rule is not preregistered")
    if started and timestamp(registered[0]["at"]) >= timestamp(started):
        raise ValueError("rule registration must precede candidate runs")
    return registered[0]


def next_look(wave, rule_sha256, *, rows=None):
    rows = entries() if rows is None else rows
    registration(wave, rule_sha256, rows=rows)
    prior = [r for r in rows if r["kind"] == "look" and r["wave"] == wave]
    if len(prior) >= 2 or (
        prior and prior[0]["verdict"] not in ("fail", "inconclusive")
    ):
        raise ValueError("wave has no remaining gate look")
    return len(prior) + 1


def register(wave, rule):
    def check(rows):
        if any(r.get("wave") == wave for r in rows):
            raise ValueError("wave already registered")

    append(
        dict(kind="registration", wave=wave, rule_sha256=digest(rule), at=utc_now()),
        check,
    )


def record(window, spec, report):
    row = dict(
        kind="look",
        wave=window.wave_id,
        at=utc_now(),
        rule_sha256=report.identity["rule_sha256"],
        look=report.identity["look"],
        treatments=[a.treatment_git_sha for a in (spec.baseline, spec.candidate)],
        score_refs=[asdict(r) for r in report.input_score_refs],
        verdict=report.promotion.verdict,
        report_sha256=digest(asdict(report)),
    )

    def check(rows):
        if next_look(window.wave_id, row["rule_sha256"], rows=rows) != row["look"]:
            raise ValueError("gate look changed during comparison")

    append(row, check)


def unseal(approved_final_treatment_sha, out):
    row = dict(kind="unseal", final_sha=approved_final_treatment_sha, at=utc_now())

    def check(rows):
        if not re.fullmatch(r"[0-9a-f]{40}", approved_final_treatment_sha) or any(
            r["kind"] == "unseal" for r in rows
        ):
            raise ValueError("holdout already unsealed or invalid final SHA")

    append(row, check)
    exclusive_json(Path(out), row)


def holdout(receipt, sha, *, use=None):
    row = read_json(receipt) if receipt else None
    if (
        not row
        or row.get("kind") != "unseal"
        or row.get("final_sha") != sha
        or row not in entries()
    ):
        raise ValueError("holdout sealed: approved final SHA receipt required")
    if use:

        def check(rows):
            if any(r["kind"] == "holdout-use" and r["use"] == use for r in rows):
                raise ValueError("holdout second use refused")

        append(
            dict(kind="holdout-use", use=use, receipt_sha256=digest(row), at=utc_now()),
            check,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wave")
    parser.add_argument("--rule", type=Path)
    parser.add_argument("--approved-final-sha")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.approved_final_sha:
        unseal(args.approved_final_sha, args.out)
    else:
        from bench.runner.compare import PromotionRule
        from bench.runner.decode import decode

        rule = decode(PromotionRule, read_json(args.rule))
        if not rule.frozen or not args.wave:
            raise ValueError("frozen rule and wave required")
        register(args.wave, asdict(rule))


if __name__ == "__main__":
    main()
