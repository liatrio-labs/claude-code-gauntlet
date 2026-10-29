"""Location proof travels from the context writer to actual dry-run forge sites."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

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


def _flow_command(script, work, *args):
    # Workflow roots use forward slashes even when the subprocess cwd is native.
    return [
        "node",
        "--input-type=module",
        "-e",
        script,
        ROOT.as_posix(),
        work.as_posix(),
        *args,
    ]


@pytest.mark.parametrize(
    "root,work",
    [
        (PurePosixPath("/plugin"), PurePosixPath("/tmp/work")),
        (
            PureWindowsPath(r"C:\Program Files\plugin"),
            PureWindowsPath(r"D:\Temp\pytest/work"),
        ),
    ],
)
def test_flow_command_roots_satisfy_workflow_contract(monkeypatch, root, work):
    paths_module = (ROOT / "workflows/src/paths.js").as_uri()
    script = f"""
import {{normalizeAbsoluteRoot}} from {json.dumps(paths_module)};
process.stdout.write(JSON.stringify(process.argv.slice(1).map(normalizeAbsoluteRoot)));
"""
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)
    result = subprocess.run(
        _flow_command(script, work),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == [root.as_posix(), work.as_posix()]


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
        _flow_command(FLOW, work, sha, str(claimed)),
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


def test_sites_require_resolution_and_confirmation(tmp_path):
    quote = "return process(value);"
    paths = ["primary.txt", *(f"site-{n}.txt" for n in range(4))]
    patch = ""
    for path in paths:
        lines = [f"context-{n}" for n in range(100)]
        lines[6] = quote
        (tmp_path / path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        patch += f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -6,2 +6,2 @@\n context-5\n {quote}\n"
    for command in (
        ["init", "-q"],
        ["add", "."],
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
        subprocess.run(["git", *command], cwd=tmp_path, check=True, capture_output=True)
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True, encoding="utf-8"
    ).strip()
    (tmp_path / "saved.patch").write_text(patch, encoding="utf-8")
    script = r"""
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const [root, work, sha] = process.argv.slice(1);
const load = n => import(pathToFileURL(`${root}/workflows/src/${n}.js`));
const {verifyStage} = await load('verifyStage');
const {validateStage} = await load('reviewStages');
const {normalizePhaseFindings} = await load('checkpoints');
const {writerPayload} = await load('persistPlan');
const {disposeFindings} = await load('dispose');
const {shellSplit} = await import(pathToFileURL(`${root}/workflows/test/helpers/shellWords.js`));
const request = file => ({file, line_start:90, line_end:90, anchor:'return process(value);', location_status:'confirmed', confirmed:true});
const raw = {findings:[{id:'ISSUE',file:'primary.txt',line_start:7,line_end:7,anchor:'return process(value);',title:'Stale value',description:'A code claim.',severity:'high',confidence:90,dimension:'bug',origin:'new',
 sites:[...Array.from({length:4},(_,n)=>request(`${work}/site-${n}.txt`)), request('../escape'), {...request('site-0.txt'),anchor:'missing quote'}]}]};
normalizePhaseFindings('merge',raw,false,{repoRoot:work});
const input = {...raw,pluginRoot:root,python:'python',headShaShort:sha,nonce:'sites',policy:{},limits:{},verify:{targetKind:'branch',baseBranch:'HEAD',diffPath:`${work}/saved.patch`,inputPathBase:`${work}/in`,outputPathBase:`${work}/out`}};
const context = tamper => ({agent:async prompt=>{const c=shellSplit(prompt.split('\n').at(-1));execFileSync(c[0],c.slice(1),{cwd:work,env:process.env});const env=JSON.parse(readFileSync(c[c.indexOf('--output')+1],'utf8'));if(tamper)env.result.deltas[0].resolved_sites[0].line_start=9;return env;}});
const verified = await verifyStage(context(false),input);
const confirmations = verified.findings[0].resolved_sites.map(s=>({file:s.file,line_start:s.line_start,line_end:s.line_end,confirmed:true}));
const persist = async list => {
 const validated = await validateStage({parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async(_,o)=>{
  if(!o.schema.properties.validations.items.properties.site_confirmations)throw Error('missing return metadata');
  return {validations:[{finding_id:'ISSUE',confidence:90,...(list===null?{}:{site_confirmations:list})}]};
 }},{findings:structuredClone(verified.findings)});
 const out=disposeFindings(validated);
 return writerPayload({findings:out.findings,postReview:out.findings,checkpoints:{challenge:validated}});
};
const all = await persist(confirmations);
const some = await persist([{...confirmations[0],confirmed:true},{...confirmations[1],confirmed:false},{...confirmations[3],confirmed:'true'},{file:'foreign.txt',line_start:7,line_end:7,confirmed:true}]);
const missing = await persist(null);
const tampered = await verifyStage(context(true),input);
process.stdout.write(JSON.stringify({all,some,missing,tampered,gaps:raw.gaps,resolved:verified.findings[0]}));
"""
    result = subprocess.run(
        _flow_command(script, tmp_path, sha),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    value = json.loads(result.stdout)
    assert len(value["gaps"]) == 1
    assert len(value["resolved"]["site_requests"]) == 5
    assert "sites" not in value["resolved"]
    sites = value["all"]["postReview"][0]["sites"]
    assert len(sites) == 4
    assert all(
        (s["line_start"], s["line_end"], s["location_status"], s["diff_validation"])
        == (7, 7, "relocated", {"in_diff": True})
        for s in sites
    )
    assert [s["file"] for s in value["some"]["findings"][0]["sites"]] == ["site-0.txt"]
    assert value["missing"]["findings"][0]["sites"] == []
    assert "site_requests" not in value["all"]["findings"][0]
    assert (
        len(value["all"]["checkpoints"]["challenge"]["findings"][0]["site_requests"])
        == 5
    )
    assert not value["tampered"]["verified"]
    assert value["tampered"]["findings"][0]["sites"] == []
