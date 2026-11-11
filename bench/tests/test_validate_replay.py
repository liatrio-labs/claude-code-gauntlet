"""Replay boundaries use frozen findings, product stages and persisted Workflow returns."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from bench.runner import validation_replay as replay

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "bench/tests/fixtures/validate_replay.json"


def test_product_replay_contracts():
    subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("validate_replay.test.mjs"))],
        check=True,
    )


def test_attribution_counts_distinct_bugs_and_unmatched_findings_and_spreads():
    source = replay.SourcePR(
        "pr", {}, {}, (), (("bug-a", ("a", "b")), ("bug-b", ())), {}
    )
    first = replay.summarize(
        source,
        {
            "findings": [
                dict(
                    id="a",
                    verdict={"claim": "not_established"},
                    route="not-established",
                    passes_filter=False,
                ),
                dict(
                    id="b",
                    verdict={"claim": "confirmed", "consequence": "latent"},
                    route="posted-latent",
                    passes_filter=True,
                ),
                dict(
                    id="c",
                    verdict={"claim": "confirmed", "consequence": "present_failure"},
                    route="posted",
                    passes_filter=True,
                ),
            ]
        },
    )
    assert first["summary"] == dict(
        known_bugs=2,
        known_bugs_passing=1,
        bug_outcomes={"bug-a": True, "bug-b": False},
        unmatched_passing=1,
        findings=3,
        passing=2,
        filtered_out=1,
    )
    second = copy.deepcopy(first)
    second["findings"][0].update(
        verdict={"claim": "confirmed", "consequence": "present_failure"},
        route="posted",
        passes_filter=True,
    )
    spread = replay.spreads([("pr", first), ("pr", second)])[0]
    assert spread["routes"] == ["not-established", "posted"]
    assert spread["verdicts"] == [
        first["findings"][0]["verdict"],
        second["findings"][0]["verdict"],
    ]
    assert spread["route_flips"] == 1
    assert spread["filter_pass_count"] == 1
    assert spread["filter_flips"] == 1


def test_workflow_results_require_completed_record_for_exact_script(tmp_path):
    script = tmp_path / "validate.js"
    script.write_text("return {};", encoding="utf-8")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    path = workflows / "wf_one.json"
    record = dict(
        scriptPath=str(script),
        script=script.read_text(encoding="utf-8"),
        args={},
        status="completed",
        result=dict(kind="bench-validate-replay", dispatches=[]),
    )
    replay.write_json(path, record)
    assert replay.workflow_result(tmp_path, script) == record["result"]
    record["status"] = "running"
    replay.write_json(path, record)
    with pytest.raises(ValueError, match="failed replay"):
        replay.workflow_result(tmp_path, script)
    record.update(status="completed", scriptPath=str(tmp_path / "other.js"))
    replay.write_json(path, record)
    with pytest.raises(ValueError, match="missing"):
        replay.workflow_result(tmp_path, script)


def test_dry_run_writes_literal_scripts_and_explicit_commands_without_launch(
    tmp_path, monkeypatch
):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload = fixture["payload"]
    artifacts = []
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for name, content in [
        (
            f"code-gauntlet-context-{payload['args']['headShaShort']}.md",
            "Read /frozen/context/file\n",
        ),
        (f"code-gauntlet-files-{payload['args']['headShaShort']}.json", "[]"),
        (f"code-gauntlet-diff-{payload['args']['headShaShort']}.patch", "saved diff\n"),
    ]:
        path = source_dir / name
        path.write_text(content, encoding="utf-8")
        artifacts.append(("artifacts/" + name, path))
    source = replay.SourcePR(
        fixture["source"]["pr_url"],
        dict(owner="owner", repo="repo", pr_number=5, head_sha="a" * 40),
        payload,
        tuple(artifacts),
        (),
        {},
    )
    monkeypatch.setattr(replay, "load_sources", lambda *args: [source])
    monkeypatch.setattr(
        replay, "launch", lambda *args: pytest.fail("dry run launched a child")
    )
    mirrors = tmp_path / "mirrors"
    (mirrors / replay._mirror_dirname("https://github.com/owner/repo.git")).mkdir(
        parents=True
    )
    out = tmp_path / "out"
    assert (
        replay.main(
            [
                "--source-run",
                str(source_dir),
                "--score-id",
                "score",
                "--treatment-tree",
                str(ROOT),
                "--mirrors-dir",
                str(mirrors),
                "--replays",
                "2",
                "--out",
                str(out),
                "--child-auth",
                "subscription",
                "--dry-run",
            ]
        )
        == 0
    )
    manifest = replay.read_json(out / "replay.json")
    assert manifest["child_model"] == "sonnet"
    assert manifest["child_effort"] == "low"
    assert manifest["child_auth"] == "subscription"
    scripts = list(out.glob("replay-*/*/validate-round-1.js"))
    assert len(scripts) == 2
    for script in scripts:
        assert "code-gauntlet:validator" in script.read_text(encoding="utf-8")
        commands = replay.read_json(script.with_name("commands-round-1.json"))
        child = commands["child"]
        assert child[child.index("--plugin-dir") + 1] == str(ROOT)
        assert child[child.index("--model") + 1] == "sonnet"
        assert child[child.index("--effort") + 1] == "low"
        assert commands["checkout"][-1][-1] == "a" * 40
        assert not script.with_name("worktree").exists()
    assert not (out / "summary.json").exists()


def test_live_cli_reads_workflow_records_and_aggregates_without_model_transcription(
    tmp_path, monkeypatch
):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload = fixture["payload"]
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    artifacts = []
    for stem, extension, content in [
        ("context", "md", "context\n"),
        ("files", "json", "[]"),
        ("diff", "patch", "diff\n"),
    ]:
        name = f"code-gauntlet-{stem}-{payload['args']['headShaShort']}.{extension}"
        path = source_dir / name
        path.write_text(content, encoding="utf-8")
        artifacts.append(("artifacts/" + name, path))
    source = replay.SourcePR(
        "pr",
        dict(owner="owner", repo="repo", pr_number=5, head_sha="a" * 40),
        payload,
        tuple(artifacts),
        (("known", ("bug-detector-1", "cross-file-impact-1")),),
        {},
    )
    monkeypatch.setattr(replay, "load_sources", lambda *args: [source])
    mirrors = tmp_path / "mirrors"
    (mirrors / replay._mirror_dirname("https://github.com/owner/repo.git")).mkdir(
        parents=True
    )
    run = subprocess.run
    git_commands = []

    def fake_git(command, **kwargs):
        if command[0] == "git":
            git_commands.append(command)
            return subprocess.CompletedProcess(command, 0)
        return run(command, **kwargs)

    def fake_launch(command, source, dest, auth, timeout):
        script = dest / "validate-round-1.js"
        plan = replay.read_json(dest / "input.json")
        prepared = replay.node("prepare", {"tree": str(ROOT), "payload": plan})
        result = fixture["dispatches"][0]["result"]
        dispatches = [dict(**call, result=result) for call in prepared["calls"]]
        (dest / "workflows").mkdir()
        replay.write_json(
            dest / "workflows/wf_replay.json",
            dict(
                scriptPath=str(script),
                script=script.read_text(encoding="utf-8"),
                args={},
                status="completed",
                result=dict(kind="bench-validate-replay", dispatches=dispatches),
            ),
        )

    monkeypatch.setattr(replay.subprocess, "run", fake_git)
    monkeypatch.setattr(replay, "launch", fake_launch)
    out = tmp_path / "out"
    replay.main(
        [
            "--source-run",
            str(source_dir),
            "--score-id",
            "score",
            "--treatment-tree",
            str(ROOT),
            "--mirrors-dir",
            str(mirrors),
            "--replays",
            "2",
            "--out",
            str(out),
            "--child-auth",
            "api",
        ]
    )
    summary = replay.read_json(out / "summary.json")
    assert [row["replay"] for row in summary["per_pr"]] == [1, 2]
    assert [row["known_bugs_passing"] for row in summary["per_replay"]] == [1, 1]
    assert all(row["route_flips"] == 0 for row in replay.read_json(out / "spread.json"))
    assert len(git_commands) == 4
    assert len(list(out.glob("replay-*/*/outcomes.json"))) == 2
