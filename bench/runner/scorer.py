"""Private cwd-relative vendor execution through the provisioned environment."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from bench.runner.epochs import read_json
from bench.runner.observations import ScoreRequest, ScorerOutputs, exclusive_json

VENDOR_ENV = Path(__file__).resolve().parents[1] / "vendor/code-review-benchmark"
BASE_URL = "https://api.anthropic.com/v1/"


def stage_inputs(epoch, candidates, pin, work_dir):
    results = Path(work_dir) / "results"
    model_dir = results / pin.strip().replace("/", "_")
    model_dir.mkdir(parents=True, exist_ok=False)
    exclusive_json(model_dir / "candidates.json", candidates)
    golden = read_json(epoch.root / "benchmark_data.min.json")
    if set(candidates) - set(golden):
        raise ValueError("candidate URLs absent from named epoch")
    for url, entry in golden.items():
        entry["reviews"] = [
            {
                "tool": tool,
                "review_comments": [],
                "repo_name": entry["source_repo"],
                "pr_url": url,
            }
            for tool in candidates.get(url, {})
        ]
    exclusive_json(results / "benchmark_data.json", golden)
    return model_dir


def execute(
    epoch,
    pin,
    api_key,
    model_dir,
    *,
    tool="deep-review",
    env=None,
    base_url=BASE_URL,
    executor=subprocess.run,
):
    if not api_key:
        raise RuntimeError("no judge API key available to run the scorer")
    model_dir = Path(model_dir).resolve()
    work_dir = model_dir.parent.parent
    run_env = dict(os.environ if env is None else env)
    run_env.update(
        MARTIAN_MODEL=pin,
        MARTIAN_API_KEY=api_key,
        MARTIAN_BASE_URL=base_url,
        PYTHONPATH=str((epoch.root / "scorer").resolve()),
    )
    logs = []
    for stage, module in (
        ("dedup", "step2_5_dedup_candidates"),
        ("judge", "step3_judge_comments"),
    ):
        command = [
            "uv",
            "run",
            "--project",
            str(VENDOR_ENV.resolve()),
            "--no-sync",
            "python",
            "-m",
            "code_review_benchmark." + module,
        ]
        if stage == "judge":
            command += ["--dedup-groups", str(model_dir / "dedup_groups.json")]
        if tool:
            command += ["--tool", tool]
        result = executor(
            command,
            cwd=str(work_dir),
            env=run_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        log = work_dir / (stage + ".log")
        with log.open("x", encoding="utf-8") as stream:
            stream.write((result.stdout or "") + (result.stderr or ""))
        logs.append((stage, log))
        if result.returncode:
            tail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"scorer stage {stage!r} failed (exit {result.returncode}): {tail}"
            )
    return tuple(logs)


def run_stages(
    request: ScoreRequest, api_key: str | None, *, executor=subprocess.run, env=None
) -> ScorerOutputs:
    candidates = {}
    for url in request.requested_pr_urls:
        candidates[url] = {
            request.tool: [
                {"text": c.text, "path": c.path, "line": c.line, "source": "extracted"}
                for c in request.candidates
                if c.pr_url == url
            ]
        }
    model_dir = stage_inputs(
        request.epoch,
        candidates,
        request.evaluator.judge_pin,
        request.work_dir / "work/delivered",
    )
    logs = execute(
        request.epoch,
        request.evaluator.judge_pin,
        api_key,
        model_dir,
        tool=request.tool,
        executor=executor,
        env=env,
    )
    return ScorerOutputs(
        model_dir / "candidates.json",
        model_dir / "dedup_groups.json",
        model_dir / "evaluations.json",
        logs,
    )
