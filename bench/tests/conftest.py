"""Benchmark-local pytest fixtures for adapter and real-poster tests."""

import pytest
from gauntlet.delivery import post

from tests.support.forge import install_forge_factory


@pytest.fixture
def git_repo(tmp_path):
    """A disposable local history, including real index and squash semantics."""
    import subprocess

    root = tmp_path / "git-fixture"
    root.mkdir()

    def git(*args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    return root, git


@pytest.fixture
def forge_factory(monkeypatch, request):
    factory = install_forge_factory(monkeypatch, post)
    if request.instance is not None:
        request.instance.forge_factory = factory
    return factory


@pytest.fixture
def score_case(tmp_path):
    import json
    from dataclasses import dataclass
    from pathlib import Path

    from bench.runner import score
    from bench.runner.epochs import DEFAULT_ROOT, load_epoch, read_json

    @dataclass(frozen=True, slots=True)
    class ScoreCase:
        run: Path
        ledger: Path
        url: str
        payload: Path
        calls: list

        def execute(self, **kwargs):
            adjudicator = kwargs.pop(
                "adjudicator", lambda *args: {"bucket": "noise", "reason": "synthetic"}
            )
            return score.score_run(
                self.run,
                env={"BENCH_JUDGE_API_KEY": "synthetic"},
                ledger_path=self.ledger,
                run_scorer=self.scorer,
                adjudicator=adjudicator,
                **kwargs,
            )

        def scorer(self, pin, key, model_dir):
            self.calls.append(model_dir)
            golden = read_json(model_dir.parent / "benchmark_data.json")
            candidates = read_json(model_dir / "candidates.json")
            evaluations, groups = {}, {}
            for url, tools in candidates.items():
                for tool, entries in tools.items():
                    claims = golden[url]["golden_comments"]
                    tp = (
                        [
                            {
                                "golden_comment": c["comment"],
                                "category": c.get("category"),
                                "matched_candidate": entries[0]["text"],
                                "confidence": 0.9,
                                "reasoning": "synthetic",
                            }
                            for c in claims[:2]
                        ]
                        if entries
                        else []
                    )
                    fn = [
                        {"golden_comment": c["comment"], "category": c.get("category")}
                        for c in claims[len(tp) :]
                    ]
                    evaluations.setdefault(url, {})[tool] = {
                        "true_positives": tp,
                        "false_negatives": fn,
                        "tp": len(tp),
                        "fn": len(fn),
                        "fp": max(0, len(entries) - 2),
                        "errors": [],
                    }
                    groups.setdefault(url, {})[tool] = (
                        [[0, 1]] if len(entries) >= 2 else []
                    )
            for name, value in (
                ("evaluations.json", evaluations),
                ("dedup_groups.json", groups),
            ):
                (model_dir / name).write_text(json.dumps(value), encoding="utf-8")

    epoch = load_epoch(root=DEFAULT_ROOT)
    gate = read_json(DEFAULT_ROOT / "subsets.json")["gate"]
    url = next(u for u in gate if sum(b.pr_url == u for b in epoch.bugs) >= 3)
    run = tmp_path / "run"
    (run / "state").mkdir(parents=True)
    (run / "state/a.json").write_text(
        json.dumps({"url": url, "status": "ok"}), encoding="utf-8"
    )
    pr = run / score.invoke.pr_dir_name({"url": url})
    pr.mkdir()
    payload = pr / "post-review-payload.json"
    payload.write_text(
        json.dumps(
            {
                "platform": "github",
                "payload": {
                    "comments": [
                        {"body": "matched candidate", "path": "a.py", "line": 1},
                        {"body": "dedup sibling", "path": "a.py", "line": 2},
                        {"body": "extra candidate", "path": "a.py", "line": 3},
                    ]
                },
                "skipped": [],
            }
        ),
        encoding="utf-8",
    )
    (pr / "diff.patch").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,3 @@\n+one\n+two\n+three\n",
        encoding="utf-8",
    )
    (run / "run.json").write_text(
        json.dumps({"run_id": "run", "urls": [url]}), encoding="utf-8"
    )
    return ScoreCase(run, tmp_path / "ledger.jsonl", url, payload, [])


@pytest.fixture
def population_case(tmp_path):
    from pathlib import Path

    from bench.runner import populations
    from bench.runner.epochs import DEFAULT_ROOT, load_epoch, read_json

    epoch = load_epoch(root=DEFAULT_ROOT)
    receipt = tmp_path / "receipt.json"
    populations.prepare_leak_manifest_v2(
        Path(__file__).parent / "fixtures/leak-manifest-v2.json",
        epoch.root,
        epoch,
        receipt,
    )
    contamination = populations.ingest_leak_manifest_v2(receipt, epoch)
    contamination.update(
        status="frozen",
        agent_read_sha256="baseline",
        baseline_treatment_sha="baseline",
        leak_report_sha256="synthetic-report-digest",
        frozen_at="2026-10-20T00:00:00Z",
    )
    subsets = read_json(DEFAULT_ROOT / "subsets.json")
    manifest = populations.freeze_populations(
        epoch,
        subsets,
        contamination,
        out=tmp_path / "populations.json",
        frozen_at="2026-10-20T00:00:00Z",
    )
    return epoch, subsets, contamination, manifest, receipt
