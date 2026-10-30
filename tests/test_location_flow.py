"""Location proof travels from the context writer to actual dry-run forge sites."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet import shared_context
from gauntlet.delivery.post import CLI
from gauntlet.forge import JsonFetch

from tests.support.forge import FakeForge, FakeGitLab, install_forge_factory

ROOT = Path(__file__).resolve().parents[1]
QUOTE = "    first_value = compute_value()\n    return first_value"
PATCH = (
    "diff --git a/source.txt b/source.txt\n--- a/source.txt\n+++ b/source.txt\n"
    "@@ -6,4 +6,5 @@\n source_6\n+    first_value = compute_value()\n"
    "-removed_value()\n+    return first_value\n source_9\n source_10\n"
)
FLOW = r"""
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [plugin, work, sha, claimed] = process.argv.slice(1);
const load = (name) => import(pathToFileURL(`${plugin}/workflows/src/${name}.js`));
const { verifyStage } = await load('verifyStage');
const { writerPayload } = await load('persistPlan');
const { disposeFindings } = await load('dispose');
const { shellSplit } = await import(pathToFileURL(`${plugin}/workflows/test/helpers/shellWords.js`));
const prefix = `${work}/code-gauntlet-`;
const findings = [{ id: 'F1', file: 'source.txt', line_start: Number(claimed), line_end: Number(claimed) + 3,
  anchor: '    first_value = compute_value()\n    return first_value',
  title: 'Quoted observation', description: 'A retained observation.', severity: 'high', confidence: 90,
  dimension: 'bug', agent: 'bug-detector', evidence: 'The value is stale.' }];
let calls = 0;
const ctx = { agent: async (prompt) => {
  calls++;
  const command = shellSplit(prompt.split('\n').at(-1));
  execFileSync(command[0], command.slice(1), { cwd: work, env: process.env });
  return JSON.parse(readFileSync(command[command.indexOf('--output') + 1], 'utf8'));
} };
const result = await verifyStage(ctx, { findings, pluginRoot: plugin, python: 'python', headShaShort: sha,
  nonce: 'flow', limits: { verifySliceSize: 10 }, policy: {}, verify: {
    targetKind: 'branch', baseBranch: 'HEAD', diffPath: `${prefix}diff-${sha}.patch`,
    contextMap: `${prefix}context-map-${sha}.json`, contextFile: `${prefix}context-${sha}.md`,
    inputPathBase: `${work}/slice-in`, outputPathBase: `${work}/slice-out`,
  } });
if (!result.verified || calls !== 1) throw Error(JSON.stringify(result));
const disposed = disposeFindings({ findings: result.findings });
const payload = writerPayload({ findings: disposed.findings, postReview: disposed.findings,
  prIdentity: { owner: 'o', repo: 'r', pr_number: 5, sha_full: sha, platform: 'github', web_origin: 'https://github.com' },
  reviewBody: 'Summary' });
process.stdout.write(JSON.stringify(payload));
"""


def _placement(tmp_path, monkeypatch, capsys, platform, prefix_lines, bypass=False):
    work = tmp_path / ("bypass" if bypass else "normal")
    work.mkdir()
    head = [f"source_{i}" for i in range(1, 901)]
    head[6:8] = QUOTE.split("\n")
    (work / "source.txt").write_text("\n".join(head) + "\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "source.txt"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=work, text=True, encoding="utf-8"
    ).strip()
    (work / f"code-gauntlet-project-rules-{sha}.md").write_text(
        "rules\n" + "prefix\n" * prefix_lines, encoding="utf-8"
    )
    (work / f"code-gauntlet-diff-{sha}.patch").write_text(PATCH, encoding="utf-8")
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(b"low risk\n"), encoding="utf-8")
    )
    assert (
        shared_context.CLI.invoke(["--output-dir", str(work), "--head-sha", sha]) == 0
    )
    context_map = json.loads(
        (work / f"code-gauntlet-context-map-{sha}.json").read_text(encoding="utf-8")
    )
    claimed = next(
        int(index)
        for index, entry in context_map["entries"].items()
        if entry == {"file": "source.txt", "side": "head", "line": 7}
    )
    assert 8 < claimed < 900
    env = dict(os.environ)
    if bypass:
        hook = work / "hook"
        hook.mkdir()
        (hook / "sitecustomize.py").write_text(
            "import gauntlet.location as location\n"
            "location.resolve_location = lambda request, *args: location.Resolution(\n"
            "    request['line_start'], request['line_start'] + 1, 'confirmed', 'claimed')\n",
            encoding="utf-8",
        )
        env["PYTHONPATH"] = os.pathsep.join((str(hook), str(ROOT / "scripts")))
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            FLOW,
            ROOT.as_posix(),
            work.as_posix(),
            sha,
            str(claimed),
        ],
        cwd=work,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    payload = json.loads(result.stdout)
    finding = payload["findings"][0]
    assert (finding["line_start"], finding["line_end"], finding["location_status"]) == (
        7,
        8,
        "relocated",
    ), "canonical site"
    assert (finding["claimed_line_start"], finding["claimed_line_end"]) == (
        claimed,
        claimed + 3,
    )
    assert (finding["line"], finding["end_line"]) == (7, 8)
    assert head[6:8] == finding["anchor"].split("\n")
    data = payload["postReview"]
    data["platform"] = platform
    path = work / "delivery.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    factory = install_forge_factory(monkeypatch, sys.modules[CLI.main.__module__])
    fake = (
        FakeForge(diffs=[(PATCH, "", 0)])
        if platform == "github"
        else FakeGitLab(
            diffs=[(PATCH, "", 0)],
            refs=[
                JsonFetch(
                    [
                        {
                            "base_commit_sha": sha,
                            "head_commit_sha": sha,
                            "start_commit_sha": sha,
                        }
                    ],
                    None,
                )
            ],
        )
    )
    factory.configure(fake)
    assert CLI.invoke([str(path), "--dry-run"]) == 0
    posted = json.loads((work / "post-review-payload.json").read_text(encoding="utf-8"))
    if platform == "github":
        comment = posted["payload"]["comments"][0]
        assert (
            comment["start_line"],
            comment["line"],
            comment["side"],
            comment["start_side"],
        ) == (7, 8, "RIGHT", "RIGHT")
    else:
        assert posted["discussions"][0]["position"]["new_line"] == 7


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("prefix_lines", [0, 600])
def test_context_coordinate_reaches_dry_run_site(
    tmp_path, monkeypatch, capsys, platform, prefix_lines
):
    _placement(tmp_path, monkeypatch, capsys, platform, prefix_lines)


def test_resolution_bypass_breaks_positive_placement(tmp_path, monkeypatch, capsys):
    with pytest.raises(AssertionError, match="canonical site"):
        _placement(tmp_path, monkeypatch, capsys, "github", 600, bypass=True)
