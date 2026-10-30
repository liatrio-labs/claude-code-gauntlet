"""Frozen synthetic controls and live description assertions, without benchmark answers."""

import hashlib
import json
from pathlib import Path

import pytest

from bench.runner.description_eval import check_review, variants

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/description_eval"
CASES = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))


def test_frozen_description_controls():
    for name, digest in CASES["sha256"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest
    assert set(variants("defective")) == set(CASES["descriptions"])
    assert set(variants("clean")) == {"empty", "alarming"}


def test_live_contract_rejects_safe_looking_empty_review():
    with pytest.raises(ValueError):
        check_review("defective", [], [], [], baseline={"boundary"})


def test_reassurance_must_preserve_delivery_and_independent_defect():
    findings = [
        {
            "file": "collection.py",
            "line_start": line,
            "evidence": "collection.py:3; consumer.py:6 inspected",
        }
        for line in (3, 8)
    ]
    with pytest.raises(ValueError, match="delivery"):
        check_review(
            "defective",
            findings,
            [],
            ["consumer.py"],
            baseline={"boundary"},
        )
    with pytest.raises(ValueError, match="signed_total"):
        check_review(
            "defective",
            findings[:1],
            findings[:1],
            ["consumer.py"],
            baseline={"boundary"},
        )
    check_review(
        "defective",
        findings,
        findings,
        ["consumer.py"],
        baseline={"boundary"},
    )
    with pytest.raises(ValueError, match="clean"):
        check_review("clean", findings, findings, [], baseline=set())


def _description_child():
    """Supply fixture review evidence across the real CLI subprocess boundary."""
    import os
    import subprocess
    import sys

    from gauntlet.pr_status import _fetch

    from bench.runner import provenance, stages
    from bench.tests.fakes import fake_claude

    fake_claude.main()
    if "--version" in sys.argv:
        return
    head, _, target, body = _fetch("github", 1, True)
    output = Path(os.environ["CODE_GAUNTLET_OUTPUT_DIR"])
    snapshot = dict(head_sha_full=head, target=target, body=body)
    (output / "pr-description.json").write_text(json.dumps(snapshot), encoding="utf-8")
    defective = (
        Path("collection.py").read_bytes() == (FIXTURES / "defective.py").read_bytes()
    )
    source = Path("collection.py").read_text(encoding="utf-8").splitlines()
    findings = [
        dict(
            id=name,
            file="collection.py",
            line_start=site["line"],
            line_end=site["line"],
            location_status="confirmed",
            anchor=source[site["line"] - 1],
            dimension="bug",
            severity="P1",
            confidence=95,
            description=f"Incorrect collection {name}",
            evidence="collection.py inspected; consumer.py:6 inspected",
            trace_id=f"fixture:bug:{name}",
            discovery_agent="bug",
            discovery_dimension="bug",
        )
        for name, site in CASES["defects"].items()
        if defective
    ]
    if os.environ.get("FAKE_DESCRIPTION_FAULTS"):
        if body == CASES["descriptions"]["reassuring"]:
            findings = findings[:1]
        elif body == CASES["descriptions"]["role_override"]:
            findings[0]["evidence"] = "description-eval-injected"
        elif body == CASES["descriptions"]["encoded"]:
            fake_claude._write_workflow_record(
                "wf_failed", {"ok": False, "error": "fixture failure", "stats": {}}
            )
            return
    versions = [
        dict(
            version_id=f"v{i}",
            trace_id=f["trace_id"],
            discovery_agent="bug",
            discovery_dimension="bug",
            finding=f,
        )
        for i, f in enumerate(findings)
    ]
    trace = dict(
        schema_version=1,
        run_nonce="fixture",
        complete=True,
        versions=versions,
        snapshots=[
            dict(
                sequence=i,
                stage=stage,
                finding_version_ids=[v["version_id"] for v in versions],
                decisions=[],
                executed=True,
                replayed=False,
                coverage="complete",
                cap=None,
                cap_hit=False,
            )
            for i, stage in enumerate(stages.get_args(stages.Stage)[:-1])
        ],
    )
    diff = subprocess.run(
        ["gh", "pr", "diff", "1"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    groups = provenance.reconstruct_delivery(findings, diff, platform="github").groups
    payload = dict(
        platform="github",
        payload=dict(
            comments=[dict(body=g.body, path=g.path, line=g.line) for g in groups]
        ),
    )
    (output / "post-review-payload.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    (output / "code-gauntlet-findings-fixture.json").write_text(
        json.dumps(findings), encoding="utf-8"
    )
    fake_claude._write_workflow_record(
        "wf_fixture",
        dict(
            ok=True,
            stats={},
            measurement=dict(status="available", text=json.dumps(trace)),
        ),
    )
    transcript = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / hashlib.sha256(str(Path.cwd()).encode()).hexdigest()
        / "session.jsonl"
    )
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps(
            dict(
                type="assistant",
                message=dict(
                    content=[
                        dict(
                            type="tool_use",
                            name="Read",
                            input=dict(file_path=str(Path.cwd() / "consumer.py")),
                        )
                    ]
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_description_adapter_runs_all_variants_through_fake_cli(tmp_path, monkeypatch):
    import os
    import sys

    from bench import run
    from bench.runner import invoke
    from bench.runner.epochs import read_json

    root = Path(__file__).resolve().parents[2]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    child = bindir / "claude"
    child.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"sys.path[:0] = {[str(root), str(root / 'scripts')]!r}\n"
        "from bench.tests.test_description_eval import _description_child\n"
        "_description_child()\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("BENCH_CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setattr(invoke, "ENV_PATH", tmp_path / "absent-input")
    for faulty in (False, True):
        monkeypatch.setenv("FAKE_DESCRIPTION_FAULTS", "1" if faulty else "")
        output = tmp_path / ("faulty" if faulty else "healthy")
        assert run.main(
            [
                "--synthetic-fixture",
                str(FIXTURES),
                "--output-dir",
                str(output),
                "--child-auth",
                "subscription",
                "--timeout-mins",
                "1",
            ]
        ) == int(faulty)
        outcomes = read_json(output / "description-results.json")
        assert [(o["case"], o["variant"]) for o in outcomes] == [
            (case, variant)
            for case in ("defective", "clean")
            for variant in variants(case)
        ]
        failed = {
            o["variant"]: o["reason"] for o in outcomes if o["status"] == "failed"
        }
        assert set(failed) == (
            {"reassuring", "role_override", "encoded"} if faulty else set()
        )
        if faulty:
            assert "signed_total" in failed["reassuring"]
            assert "injected" in failed["role_override"]
            assert failed["encoded"] == "pipeline_failed"
        for outcome in outcomes:
            directory = output / outcome["case"] / outcome["variant"]
            pr = directory / "pr-synthetic-collection-1"
            assert (
                read_json(pr / "pr-description.json")["body"]
                == variants(outcome["case"])[outcome["variant"]]
            )
            assert not (pr / "worktree").exists()
            assert list((directory / "attempts").rglob("attempt.json"))
            if outcome["variant"] not in failed:
                assert (
                    read_json(pr / "measurement-collection.json")["status"]
                    == "available"
                )
