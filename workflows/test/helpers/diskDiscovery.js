// Model-host adapter: execute the pinned command and emulate Read of its assignment.
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { shellSplit } from './shellWords.js';
import { boundedCoverageLedger } from '../../src/coverageLedger.js';
import { discoveryArtifactName } from '../../src/discoveryDisk.js';

export function preparePlan(input, plan) {
  const root = mkdtempSync(join(tmpdir(),'gauntlet-plan-')).replace(/\\/g,'/');
  input.outputDir = root;
  input.headShaShort ||= 'abc1234';
  input.nonce ||= 'disk-discovery-test';
  input.pluginRoot = fileURLToPath(new URL('../../..',import.meta.url)).replace(/\\/g,'/').replace(/\/$/,'');
  input.python ||= 'python3';
  input.contextLines ||= input.contextReceipt?.contextLines || 40;
  input.contextPath = join(root,`code-gauntlet-context-${input.headShaShort}.md`).replace(/\\/g,'/');
  const context = 'row\n'.repeat(input.contextLines);
  writeFileSync(input.contextPath,context);
  const name = discoveryArtifactName('discovery-plan',input.headShaShort);
  const text = JSON.stringify({version:1,sha:input.headShaShort,context_sha256:createHash('sha256').update(context).digest('hex'),diff_sha256:'0'.repeat(64),context_lines:input.contextLines,plan});
  writeFileSync(join(root,name),text);
  input.discoveryPlanRef = {path:name,sha256:createHash('sha256').update(text).digest('hex')};
  delete input.discoveryPlan;
  if (input.contextReceipt) {
    input.contextReceipt = {...input.contextReceipt,discoveryPlanRef:input.discoveryPlanRef};
    delete input.contextReceipt.discoveryPlan;
  }
  return root;
}

export function executorAdapter(ctx,input,{readAssignments=false}={}) {
  const original = ctx.agent;
  const executions = [];
  ctx.agent = async (prompt,options) => {
    if (options.label.startsWith('discovery-plan-')) {
      const command = shellSplit(prompt.split('\n').at(-1));
      const result = JSON.parse(execFileSync(command[0],command.slice(1),{encoding:'utf8',cwd:input.pluginRoot}));
      executions.push({prompt,options,result});
      return result;
    }
    if (readAssignments && options.agentType === 'code-gauntlet:gap-hunter') {
      prompt = readFileSync(join(input.outputDir,discoveryArtifactName('gap-assignment',input.headShaShort,input.nonce)),'utf8');
    }
    return original(prompt,options);
  };
  return {executions,restore:()=>{ctx.agent=original;}};
}

export function readLedger(input,ref) {
  const data=readFileSync(join(input.outputDir,ref.path));
  assert.equal(createHash('sha256').update(data).digest('hex'),ref.sha256);
  return JSON.parse(data);
}

export async function withPreparedPlan(ctx,input,run,{materialize=true}={}) {
  const plan = input.discoveryPlan ?? input.contextReceipt?.discoveryPlan;
  if (plan === undefined && !Object.hasOwn(input,'discoveryPlan') && !Object.hasOwn(input.contextReceipt||{},'discoveryPlan')) return run(ctx,input);
  if (plan === undefined && input.discoveryPlan === undefined && !Object.hasOwn(input.contextReceipt||{},'discoveryPlan')) return run(ctx,input);
  const root=preparePlan(input,plan);
  const adapter=executorAdapter(ctx,input,{readAssignments:true});
  try {
    const out=await run(ctx,input);
    if (materialize && out.coverageLedgerArtifact) out.coverageLedger=boundedCoverageLedger(readLedger(input,out.coverageLedgerArtifact));
    if (materialize && input.measurementTrace?.coverage_ledger_artifact) input.measurementTrace.coverage_ledger=boundedCoverageLedger(readLedger(input,input.measurementTrace.coverage_ledger_artifact));
    if (materialize && out.measurement?.status==='available') {
      // Small integration traces remain plain; the production reader tests compression separately.
      const trace=JSON.parse(out.measurement.text);
      if (trace.coverage_ledger_artifact) trace.coverage_ledger=boundedCoverageLedger(readLedger(input,trace.coverage_ledger_artifact));
      out.measurement.text=JSON.stringify(trace);
    }
    return out;
  } finally {
    adapter.restore();
    rmSync(root,{recursive:true,force:true});
  }
}
