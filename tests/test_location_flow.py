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


def test_test_and_quality_admission_boundaries(tmp_path, monkeypatch):
    from tests.support.git_diff import git, init_git_repo

    init_git_repo(tmp_path, monkeypatch)
    for name, text in {
        "engine": "return old_value;\n",
        "tests/check": "assert old_value;\n",
        "tests/old-check": "assert untouched_value;\n",
        "tests/assertion": "assert compute_value() == expected_value;\n",
        "tests/mock": "mock_dependency.return_value = fixture_value;\nassert worker_result() == fixture_value;\n",
        "tests/wait": "await finish_operation();\nassert completed_state;\n",
        "precedent": "return existing_value;\n",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    for name, text in {
        "engine": "return current_value;\n",
        "tests/check": "assert current_value;\n",
        "tests/assertion": "assert compute_value() == wrong_value;\n",
        "tests/mock": "unused_mock.return_value = fixture_value;\nassert worker_result() == fixture_value;\n",
        "tests/wait": "finish_operation();\nassert completed_state;\n",
        "peer": "return new_value;\n",
    }.items():
        (tmp_path / name).write_text(text, encoding="utf-8", newline="")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "head")
    sha = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "saved.patch").write_text(
        git(tmp_path, "diff", base, sha), encoding="utf-8", newline=""
    )
    script = r"""
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const [root, work, sha, base] = process.argv.slice(1);
const load = n => import(pathToFileURL(`${root}/workflows/src/${n}.js`));
const {verifyStage} = await load('verifyStage');
const {validateStage, challengeStage} = await load('reviewStages');
const {evaluateAdmission, admissionRequests, hasAdmission} = await load('admission');
const {requiredAssessments} = await load('registry');
const {disposeFindings} = await load('dispose');
const {deliveryAllows} = await load('location');
const {shellSplit} = await import(pathToFileURL(`${root}/workflows/test/helpers/shellWords.js`));
const ref = (file,anchor) => ({file,anchor,line_start:1,line_end:1});
const primary = ref('engine','return current_value;');
const test = ref('tests/check','assert current_value;');
const precedent = ref('precedent','return existing_value;');
const gap = {test_kind:'critical_gap',criticality:8,failure_scenario:'Complete behavior failure',
 searched_paths:['tests'],candidate_tests:[{...test,input:'current_value',assertion:'current_value'}],production_element:primary};
const rows = [
 ['assertion','test_coverage',{...ref('tests/assertion','assert compute_value() == wrong_value;'),test_kind:'defective_test'},['confirmed_test_defect'],'admitted'],
 ['mock','test_coverage',{...ref('tests/mock','unused_mock.return_value = fixture_value;'),test_kind:'defective_test'},['confirmed_test_defect'],'admitted'],
 ['wait','test_coverage',{...ref('tests/wait','finish_operation();'),test_kind:'defective_test'},['confirmed_test_defect'],'admitted'],
 ['sound-test','test_coverage',{...test,test_kind:'defective_test'},['confirmed_test_defect'],'report_only','ordinary','confirmed_test_defect'],
 ['source-test','test_coverage',{test_kind:'defective_test'},['confirmed_test_defect'],'report_only'],
 ['gap','test_coverage',gap,['resolved_production_element','complete_searches','complete_behavioral_absence'],'admitted'],
 ['production','test_coverage',{test_kind:'production_defect'},['present_defect'],'admitted'],
 ['additional','test_coverage',{test_kind:'additional_case'},[],'report_only'],
 ['terminal-coverage','test_coverage',{...gap,test_kind:'additional_case'},['resolved_production_element','complete_searches','complete_behavioral_absence'],'report_only','coverage'],
 ['correctness','type_design',{quality_basis:'current_correctness'},['present_defect'],'admitted'],
 ['duplication','simplification',{quality_basis:'duplication',duplicate_of:precedent},['confirmed_duplication'],'admitted'],
 ['equivalent','type_design',{quality_basis:'simpler_equivalent',behavior_preserved:'Same behavior',suggestion:'Return the same value directly'},['concrete_equivalent','behavior_preserved'],'admitted'],
 ['idiom','simplification',{quality_basis:'stack_idiom',precedent},['applicable'],'admitted'],
 ['other','type_design',{quality_basis:'other'},[],'report_only'],
 ['terminal-test','type_design',{...test,quality_basis:'other'},['confirmed_test_defect'],'report_only','test_defect'],
 ['classification','bug',{},[],'held_back','unresolved'],
 ['cross-test','bug',{...test,effective_test_kind:'production_defect'},['confirmed_test_defect'],'admitted','test_defect'],
 ['cross-gap','bug',gap,['resolved_production_element','complete_searches','complete_behavioral_absence'],'admitted','coverage'],
 ['convention','convention',{...test,claude_md_rule:'Applicable rule'},[],'admitted'],
 ['seven','test_coverage',{...gap,criticality:7},['resolved_production_element','complete_searches','complete_behavioral_absence'],'report_only'],
 ['partial','test_coverage',gap,['resolved_production_element','complete_searches','complete_behavioral_absence'],'report_only','ordinary','complete_behavioral_absence'],
 ['non-production','test_coverage',gap,['resolved_production_element','complete_searches','complete_behavioral_absence'],'report_only','ordinary','resolved_production_element'],
 ['incomplete-search','test_coverage',gap,['resolved_production_element','complete_searches','complete_behavioral_absence'],'report_only','ordinary','complete_searches'],
 ['indirect','test_coverage',gap,['resolved_production_element','complete_searches','complete_behavioral_absence'],'report_only','ordinary','complete_behavioral_absence'],
 ['new-idiom','type_design',{quality_basis:'stack_idiom',precedent:ref('peer','return new_value;')},['applicable'],'report_only'],
 ['missing-idiom','type_design',{quality_basis:'stack_idiom'},['applicable'],'report_only'],
 ['bad-idiom','type_design',{quality_basis:'stack_idiom',precedent:ref('absent','unknown')},['applicable'],'held_back'],
 ['missing-duplicate','simplification',{quality_basis:'duplication'},['confirmed_duplication'],'report_only'],
 ['distinct-source','simplification',{quality_basis:'duplication',duplicate_of:precedent},['confirmed_duplication'],'report_only','ordinary','confirmed_duplication'],
 ['inapplicable','type_design',{quality_basis:'stack_idiom',precedent},['applicable'],'report_only','ordinary','applicable'],
 ['bad-duplicate','simplification',{quality_basis:'duplication',duplicate_of:ref('absent','unknown')},['confirmed_duplication'],'held_back'],
 ['no-fix','simplification',{quality_basis:'simpler_equivalent',behavior_preserved:'same'},['concrete_equivalent','behavior_preserved'],'report_only'],
 ['changed-behavior','simplification',{quality_basis:'simpler_equivalent',suggestion:'Return directly',behavior_preserved:'same'},['concrete_equivalent','behavior_preserved'],'report_only','ordinary','behavior_preserved'],
 ['not-equivalent','simplification',{quality_basis:'simpler_equivalent',suggestion:'Return directly',behavior_preserved:'same'},['concrete_equivalent','behavior_preserved'],'report_only','ordinary','concrete_equivalent'],
 ['no-production','bug',{...gap,production_element:undefined},['complete_searches'],'report_only','coverage'],
 ['bad-production','bug',{...gap,production_element:ref('absent','unknown')},['resolved_production_element','complete_searches','complete_behavioral_absence'],'held_back','coverage'],
 ['unfinished-search','test_coverage',{...gap,searched_paths:[]},['resolved_production_element','complete_searches','complete_behavioral_absence'],'held_back'],
 ['unread-search','test_coverage',{...gap,searched_paths:['absent']},['resolved_production_element','complete_searches','complete_behavioral_absence'],'held_back'],
 ['bad-candidate','test_coverage',{...gap,candidate_tests:[{...test,anchor:'wrong',input:'input',assertion:'assertion'}]},['resolved_production_element','complete_searches','complete_behavioral_absence'],'held_back'],
 ['missing-assessment','test_coverage',{test_kind:'production_defect'},[],'held_back'],
 ['rejected','type_design',{quality_basis:'current_correctness'},['present_defect'],'report_only','ordinary','present_defect'],
 ['unresolved','type_design',{quality_basis:'current_correctness'},['present_defect'],'held_back','ordinary','present_defect','unresolved'],
 ['foreign','type_design',{quality_basis:'current_correctness'},['present_defect'],'held_back','ordinary',null,'foreign'],
 ['duplicate','type_design',{quality_basis:'current_correctness'},['present_defect'],'held_back','ordinary',null,'duplicate'],
 ['kindless','test_coverage',{},[],'held_back'],
 ['untouched-test','test_coverage',{...ref('tests/old-check','assert untouched_value;'),test_kind:'defective_test'},['confirmed_test_defect'],'report_only'],
];
const findings = rows.map(([id,dimension,extra])=>({id,dimension,...primary,title:id,description:'A retained source claim.',severity:'high',confidence:95,origin:'new',...extra}));
const verified = await verifyStage({agent:async prompt=>{const c=shellSplit(prompt.split('\n').at(-1));execFileSync(c[0],c.slice(1),{cwd:work,env:process.env});return JSON.parse(readFileSync(c[c.indexOf('--output')+1],'utf8'));}},
 {findings,pluginRoot:root,python:'python',headShaShort:sha,nonce:'admission',limits:{},verify:{targetKind:'branch',baseBranch:base,diffPath:`${work}/saved.patch`,inputPathBase:`${work}/in`,outputPathBase:`${work}/out`}});
assert.equal(verified.verified,true);
const validated = await validateStage({parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async()=>({validations: verified.findings.map(f=>{
 const row=rows.find(r=>r[0]===f.id), requests=Object.keys(admissionRequests(f));
 const references={confirmed_test_defect:['primary'],present_defect:['primary'],resolved_production_element:['production_element'],complete_searches:['searched_paths:','candidate_tests:'],complete_behavioral_absence:['production_element','searched_paths:','candidate_tests:'],confirmed_duplication:['primary','duplicate_of'],concrete_equivalent:['primary'],behavior_preserved:['primary'],applicable:['primary','precedent']};
 let entries=row[3].map(predicate=>({predicate,status:predicate===row[6]?(row[7]==='unresolved'?'unresolved':'rejected'):'confirmed',references:requests.filter(key=>references[predicate].some(name=>name.endsWith(':')?key.startsWith(name):key===name))}));
 for(const predicate of requiredAssessments(f,row[5]||'ordinary'))if(!entries.some(e=>e.predicate===predicate))entries.push({predicate,status:'unresolved',references:requests.filter(key=>references[predicate].some(name=>name.endsWith(':')?key.startsWith(name):key===name))});
 if(row[7]==='foreign')entries[0].references=['foreign'];
 if(row[7]==='duplicate')entries.push(entries[0]);
 return {finding_id:f.id,confidence:99,policy_subject:row[5]||'ordinary',reachability:'current',admission_assessments:entries};
})})},{findings:verified.findings});
for(const f of validated.findings){evaluateAdmission(f);assert.equal(f.admission.status,rows.find(r=>r[0]===f.id)[4],f.id);assert.ok(hasAdmission(f));}
const mapped=validated.findings.find(f=>f.id==='cross-test');
assert.equal(mapped.effective_test_kind,'defective_test');assert.equal(mapped.test_kind,undefined);
const challenged=await challengeStage({parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async()=>({score:90})},{findings:validated.findings});
const out=disposeFindings(challenged);
for(const f of [...out.findings,...out.unverified])assert.equal(deliveryAllows(f,'body'),f.admission.status==='admitted',f.id);
assert.equal(out.findings.find(f=>f.id==='source-test').disposition,'report_only');
const first=out.findings.find(f=>f.id==='gap');first.criticality=7;
assert.equal(disposeFindings({findings:[first]}).unverified[0].disposition,'held_back');
process.stdout.write(JSON.stringify({rows:rows.length,findings:out.findings.length}));
"""
    result = subprocess.run(
        _flow_command(script, tmp_path, sha, base),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["rows"] == 46


FLOW = r"""
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [plugin, work, sha, claimed] = process.argv.slice(1);
const load = (name) => import(pathToFileURL(`${plugin}/workflows/src/${name}.js`));
const { verifyStage } = await load('verifyStage');
const { writerPayload } = await load('persistPlan');
const { disposeFindings } = await load('dispose');
const { evaluateAdmission } = await load('admission');
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
const disposed = disposeFindings({ findings: result.findings.map(f=>evaluateAdmission({...f,validation:'validated',policy_subject:'ordinary',challenge:'survived'})) });
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


def test_sites_require_resolution_and_confirmation(tmp_path, monkeypatch):
    quote = "return process(value);"
    paths = ["primary.txt", *(f"site-{n}.txt" for n in range(4))]
    patch = ""
    for path in paths:
        lines = [f"context-{n}" for n in range(100)]
        lines[6] = quote
        (tmp_path / path).write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline=""
        )
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
    (tmp_path / "saved.patch").write_text(patch, encoding="utf-8", newline="")
    script = r"""
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const [root, work, sha] = process.argv.slice(1);
const load = n => import(pathToFileURL(`${root}/workflows/src/${n}.js`));
const {verifyStage} = await load('verifyStage');
const {validateStage} = await load('reviewStages');
const {selectDelivery} = await load('reviewStages');
const {discover} = await load('discover');
const {mergeStage} = await load('mergeFindings');
const {normalizePhaseFindings} = await load('checkpoints');
const {writerPayload} = await load('persistPlan');
const {disposeFindings} = await load('dispose');
const {shellSplit} = await import(pathToFileURL(`${root}/workflows/test/helpers/shellWords.js`));
const {evaluateAdmission} = await load('admission');
const request = file => ({file, line_start:90, line_end:90, anchor:'return process(value);', location_status:'confirmed', confirmed:true});
const candidate = {id:'ISSUE',file:'primary.txt',line_start:7,line_end:7,anchor:'return process(value);',title:'Stale value',description:'A code claim.',severity:'high',confidence:90,dimension:'bug',origin:'new',
 sites:[...Array.from({length:4},(_,n)=>request(`${work}/site-${n}.txt`)), request('../escape'), {...request('site-0.txt'),anchor:'missing quote'}]};
const discovered = await discover({parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async(_,o)=>{
 if(!o.schema.properties.findings.items.properties.sites)throw Error('missing discovery sites');
 const findings = o.agentType.endsWith(':bug-detector') ? [candidate]
  : o.agentType.endsWith(':security-reviewer') ? [{...candidate,id:'CONSTITUENT',dimension:'security',attack_vector:'Shared defect',sites:[]}]:[];
 return {findings,complete:true,total_seen:findings.length,removed_test_audit:[]};
}},{removedTests:{status:'available',items:[],omitted:0}});
const raw = mergeStage(discovered,{});
normalizePhaseFindings('merge',raw,false,{repoRoot:work});
const input = {...raw,pluginRoot:root,python:'python',headShaShort:sha,nonce:'sites',policy:{},limits:{},verify:{targetKind:'branch',baseBranch:'HEAD',diffPath:`${work}/saved.patch`,inputPathBase:`${work}/in`,outputPathBase:`${work}/out`}};
const context = tamper => ({agent:async prompt=>{const c=shellSplit(prompt.split('\n').at(-1));execFileSync(c[0],c.slice(1),{cwd:work,env:process.env});const env=JSON.parse(readFileSync(c[c.indexOf('--output')+1],'utf8'));if(tamper)env.result.deltas[0].resolved_sites[0].line_start=9;return env;}});
const verified = await verifyStage(context(false),input);
const confirmations = verified.findings[0].resolved_sites.map(s=>({file:s.file,line_start:s.line_start,line_end:s.line_end,confirmed:true}));
const persist = async list => {
 const validated = await validateStage({parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async(prompt,o)=>{
  if(!o.schema.properties.validations.items.properties.site_confirmations)throw Error('missing return metadata');
  if(!prompt.split('\nReturn ')[1]?.includes('site_confirmations') || !prompt.includes('same defect requiring the same correction'))throw Error('missing semantic contract');
  const packet=JSON.parse(prompt.split(' | site_requests: ')[1].split(' | admission_context: ')[0]);
  if(packet.length!==4 || packet.some(s=>s.line_start!==7))throw Error('requests are not resolved');
  return {validations:[{finding_id:'ISSUE',confidence:90,policy_subject:'ordinary',...(list===null?{}:{site_confirmations:list})},{finding_id:'CONSTITUENT',confidence:85,policy_subject:'ordinary'}]};
 }},{findings:structuredClone(verified.findings)});
 const out=disposeFindings({...validated,findings:validated.findings.map(f=>evaluateAdmission({...f,challenge:'survived'}))});
 return writerPayload({findings:out.findings,postReview:selectDelivery(out.findings,1,'all'),checkpoints:{challenge:validated}});
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
    assert [f["id"] for f in value["all"]["postReview"]] == ["ISSUE", "CONSTITUENT"]
    assert all(
        (s["line_start"], s["line_end"], s["location_status"], s["diff_validation"])
        == (7, 7, "relocated", {"in_diff": True})
        for s in sites
    )
    assert [s["file"] for s in value["some"]["findings"][0]["sites"]] == ["site-0.txt"]
    assert value["missing"]["findings"][0]["sites"] == []
    assert len(value["all"]["findings"][0]["site_requests"]) == 5
    assert (
        len(value["all"]["checkpoints"]["challenge"]["findings"][0]["site_requests"])
        == 5
    )
    assert not value["tampered"]["verified"]
    assert value["tampered"]["findings"][0]["sites"] == []
    delivery = tmp_path / "delivery.json"
    delivery.write_text(
        json.dumps(
            {
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "sha": sha,
                "findings": value["all"]["postReview"],
            }
        ),
        encoding="utf-8",
        newline="",
    )
    factory = install_forge_factory(monkeypatch, sys.modules[CLI.main.__module__])
    factory.configure(FakeForge(diffs=[(patch, "", 0)]))
    assert CLI.invoke([str(delivery), "--dry-run"]) == 0
    posted = json.loads(
        (tmp_path / "post-review-payload.json").read_text(encoding="utf-8")
    )
    comments = posted["payload"]["comments"]
    assert len(comments) == 1
    for n in range(4):
        assert f"site-{n}.txt:7" in comments[0]["body"]


def test_admission_references_are_snapshot_bound(
    tmp_path, monkeypatch, symlink_or_skip
):
    from tests.support.git_diff import git, init_git_repo

    init_git_repo(tmp_path, monkeypatch)
    files = {
        "src/engine": "return original_value;\n",
        "tests/nested/check": "assert original_value == expected_value;\n",
        "lib/precedent": "return existing_idiom;\n",
        "vendor/tests/generated": "assert generated_value == expected_value;\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    (tmp_path / "binary").write_bytes(b"\0unreadable")
    outside = tmp_path.parent / "outside-source"
    outside.write_text("return escaped_value;\n", encoding="utf-8", newline="")
    (tmp_path / "escape").symlink_to(outside)
    (tmp_path / "escape-dir").symlink_to(tmp_path.parent, target_is_directory=True)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "src/engine").write_text(
        "return changed_value;\n", encoding="utf-8", newline=""
    )
    (tmp_path / "lib/added").write_text(
        "return new_idiom;\n", encoding="utf-8", newline=""
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "target")
    sha = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "saved.patch").write_text(
        git(tmp_path, "diff", base, sha) + "\n", encoding="utf-8", newline=""
    )
    (tmp_path / "lib/precedent").write_text(
        "return local_idiom;\n", encoding="utf-8", newline=""
    )
    script = r"""
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const [root, work, sha, base] = process.argv.slice(1);
const load = n => import(pathToFileURL(`${root}/workflows/src/${n}.js`));
const {verifyStage} = await load('verifyStage');
const {normalizePhaseFindings} = await load('checkpoints');
const {encodeSliceInline, projectVerifySliceFinding, deltaContentProof} = await load('verifyWire');
const {verifyInlineCost} = await load('capacity');
const {shellSplit} = await import(pathToFileURL(`${root}/workflows/test/helpers/shellWords.js`));
const ref = (file, anchor) => ({file,line_start:1,line_end:1,anchor});
const candidate = {id:'FACT',file:'src/engine',line_start:1,line_end:1,anchor:'return changed_value;',severity:'high',confidence:90,
 description:'Unused prose '.repeat(1000), searched_paths:['tests','missing','escape-dir','../outside-source'],
 candidate_tests:[ref(`${work}/tests/nested/check`,'assert original_value == expected_value;'),
 ref('escape','return escaped_value;'),ref('escape-dir/outside-source','return escaped_value;'),
 ref('missing','return absent_value;'),ref('binary','return absent_value;'),
 ref('tests/nested/check','assert wrong_value == expected_value;'),
 ref('vendor/tests/generated','assert generated_value == expected_value;')].map(r=>({...r,input:'input '.repeat(1000),assertion:'assertion '.repeat(1000)})),
 production_element:ref('src/engine','return changed_value;'),duplicate_of:ref('lib/precedent','return existing_idiom;'),precedent:ref('lib/added','return new_idiom;'),
 sites:[ref('tests/nested/check','assert original_value == expected_value;')]};
const normalized={findings:[candidate]};
normalizePhaseFindings('merge',normalized,false,{repoRoot:work});
const findings=normalized.findings;
const wireMeasurements=[];
const run = async (targetKind, fault) => verifyStage({agent:async prompt=>{
 const c=shellSplit(prompt.split('\n').at(-1));execFileSync(c[0],c.slice(1),{cwd:work,env:process.env});
 const env=JSON.parse(readFileSync(c[c.indexOf('--output')+1],'utf8'));
 if(!fault)wireMeasurements.push({targetKind,input:c[c.indexOf('--input-inline')+1].length,delta:JSON.stringify(env.result.deltas).length,deltaPretty:JSON.stringify(env.result.deltas,null,2).length});
 if(fault==='content')env.result.deltas[0].resolved_references.precedent.file_added=false;
 if(fault==='nonce')env.receipt.nonce='stale';
 if(fault==='input'){env.receipt.input_checksum='changed';env.receipt.record_proofs[0].checksum='changed';}
 if(fault==='foreign'){env.result.deltas[0].id='FOREIGN';env.receipt.record_proofs[0].id='FOREIGN';env.receipt.deltas_checksum=deltaContentProof(['FOREIGN'],env.result.deltas);}
 return env;
}},{findings,pluginRoot:root,python:'python',headShaShort:sha,nonce:'facts',limits:{},policy:{},
 verify:{targetKind,baseBranch:base,diffPath:`${work}/saved.patch`,inputPathBase:`${work}/in`,outputPathBase:`${work}/out`}});
const branch=await run('branch');
const local=await run('local');
const failed=[];for(const fault of ['content','nonce','input','foreign'])failed.push(await run('branch',fault));
const encoded=encodeSliceInline({findings:findings.map(projectVerifySliceFinding),base_branch:base});
process.stdout.write(JSON.stringify({branch,local,failed,wireMeasurements,encoded:encoded.length,windows:verifyInlineCost(encoded,'windows'),projected:projectVerifySliceFinding(findings[0])}));
"""
    result = subprocess.run(
        _flow_command(script, tmp_path, sha, base),
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    value = json.loads(result.stdout)
    assert value["branch"]["verified"] and value["local"]["verified"]
    branch = value["branch"]["findings"][0]
    refs = branch["resolved_references"]
    assert refs["production_element"]["location_status"] == "confirmed"
    assert refs["production_element"]["diff_validation"] == {"in_diff": True}
    assert refs["duplicate_of"]["location_status"] == "confirmed"
    assert refs["precedent"]["file_added"] is True
    assert refs["duplicate_of"]["file_added"] is False
    assert refs["candidate_tests"][0]["path_role"] == "test"
    assert refs["candidate_tests"][0]["file"] == "tests/nested/check"
    assert [r["location_status"] for r in refs["candidate_tests"]] == [
        "confirmed",
        *(["unconfirmed"] * 5),
        "confirmed",
    ]
    assert refs["candidate_tests"][-1]["path_role"] == "non_source"
    for field in ("candidate_tests", "production_element", "duplicate_of", "precedent"):
        facts = refs[field] if isinstance(refs[field], list) else [refs[field]]
        assert all("anchor" not in fact and "diagnostic" not in fact for fact in facts)
    assert [s["available"] for s in refs["searched_paths"]] == [True, False, False]
    assert refs["searched_paths"][0]["kind"] == "directory"
    assert branch["reference_diagnostics"]
    assert branch["resolved_sites"][0]["diff_validation"] == {"in_diff": False}
    assert "sites" not in branch and len(branch["site_requests"]) == 1
    local = value["local"]["findings"][0]["resolved_references"]
    assert local["duplicate_of"]["location_status"] == "unconfirmed"
    assert [r["location_status"] for r in local["candidate_tests"]] == [
        "confirmed",
        *(["unconfirmed"] * 5),
        "confirmed",
    ]
    for failed in value["failed"]:
        assert not failed["verified"]
        assert "resolved_references" not in failed["findings"][0]
        assert failed["findings"][0]["origin"] == "unknown"
    assert "description" not in value["projected"]
    assert "input" not in value["projected"]["candidate_tests"][0]
    assert len(branch["candidate_tests"][0]["input"]) == 6000
    assert 0 < value["encoded"] < value["windows"] < 10000
    print(
        f"reference slice encoded={value['encoded']} Windows argv cost={value['windows']}"
    )
    for measurement in value["wireMeasurements"]:
        assert measurement["delta"] > 0
        print(
            f"reference slice {measurement['targetKind']}: input={measurement['input']} "
            f"delta compact={measurement['delta']} pretty={measurement['deltaPretty']}"
        )
