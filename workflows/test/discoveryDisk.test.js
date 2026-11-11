import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { discover } from '../src/discover.js';
import { createMeasurementTrace } from '../src/measurementTrace.js';
import { normalizeArgsReport } from '../src/args.js';
import { planOnDisk, DISCOVERY_CHUNK_CHARS, DISCOVERY_RECEIPT_CHARS, discoveryArtifactName } from '../src/discoveryDisk.js';
import { fnv1a32 } from '../src/wire.js';
import { preparePlan,executorAdapter,readLedger } from './helpers/diskDiscovery.js';
import { shellSplit } from './helpers/shellWords.js';

const plan = (count=2) => ({version:2,status:'available',fact_packets:[],regions:Array.from({length:count},(_,i)=>({id:`r${i}`,file:`src/${i}`,line_start:1,line_end:2,reads:[{offset:3,limit:2,bytes:100}]}))});
const request = (input,extra={}) => ({context:input.contextPath,context_lines:input.contextLines,refs:[],risks:[],enabled:true,disabled_reason:'scope',...extra});

test('a large disk plan keeps args, receipts, phase output and trace coverage bounded', async () => {
  const input={measurementTrace:createMeasurementTrace('disk'),removedTests:{status:'available',items:[],omitted:0}};
  const root=preparePlan(input,plan(2000));
  const calls=[];
  const ctx={parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async(prompt,options)=>{
    calls.push({prompt,...options});
    if(options.agentType==='code-gauntlet:gap-hunter')return{findings:[{file:'outside',line_start:10,title:'Present defect',description:'The changed operation fails for a supported input.',severity:'high',confidence:90,anchor:'return value',evidence:'src/0:1 supplies the triggering input',change_origin:'introduced'}],complete:true,total_seen:1};
    return{findings:[],complete:true,total_seen:0,removed_test_audit:[]};
  }};
  const adapter=executorAdapter(ctx,input);
  try {
    assert.ok(JSON.stringify(input.discoveryPlanRef).length<180);
    const out=await discover(ctx,input);
    assert.equal(calls.length,8);
    assert.equal(adapter.executions.length,2);
    assert.ok(adapter.executions.every(e=>e.options.model==='claude-sonnet-5-5' && JSON.stringify(e.result).length<=DISCOVERY_RECEIPT_CHARS));
    assert.ok(adapter.executions.every(e=>{const a=shellSplit(e.prompt.split('\n').at(-1));return a[a.indexOf('--chunk')+1].length<=DISCOVERY_CHUNK_CHARS;}));
    assert.ok(JSON.stringify(out).length<4096);
    assert.ok(JSON.stringify(input.measurementTrace.coverage_ledger).length<4096);
    assert.equal(out.findings.length,1);
    const ledger=readLedger(input,out.coverageLedgerArtifact);
    assert.equal(ledger.regions.length,2000);
    assert.equal(ledger.uncovered.length,2000);
    assert.equal(ledger.remaining_uncovered.includes('r0'),false);
    assert.deepEqual(ledger.hunters[0].outside_assignment,['gap-hunter-1']);
    assert.ok(ledger.omitted.some(r=>r.reason==='budget'));
    assert.deepEqual(input.measurementTrace.coverage_ledger_artifact,out.coverageLedgerArtifact);
    const text=readFileSync(join(root,discoveryArtifactName('gap-assignment',input.headShaShort,input.nonce)),'utf8');
    assert.ok(Math.max(...text.split('\n').map(line=>line.length))<2000);
    const packet=JSON.parse(text.split('\n').slice(2,-2).join('\n'));
    const hunter=calls.find(c=>c.agentType==='code-gauntlet:gap-hunter');
    const readBytes=packet.reads.reduce((n,r)=>n+r.bytes,0);
    assert.ok(Buffer.byteLength(text)+12*text.split('\n').slice(0,-1).length+Buffer.byteLength(hunter.prompt)+readBytes<=24000);
    assert.ok(text.split('\n').length-1+packet.reads.reduce((n,r)=>n+r.limit,0)<=750);
  } finally {adapter.restore();rmSync(root,{recursive:true,force:true});}
});

test('inline plans are removed at the args waist instead of being trusted or copied',()=>{
  const large=plan(2000);
  const out=normalizeArgsReport({discoveryPlan:large,contextReceipt:{discoveryPlan:large}});
  assert.equal(Object.hasOwn(out.args,'discoveryPlan'),false);
  assert.equal(Object.hasOwn(out.args.contextReceipt,'discoveryPlan'),false);
  assert.ok(JSON.stringify(out.args).length<100);
  assert.ok(out.contextReceiptGaps.some(g=>g.includes('inline discoveryPlan')));
});

for(const phase of ['select','finalize'])test(`a failed ${phase} transport preserves available reviewer findings`,async()=>{
  const input={measurementTrace:createMeasurementTrace('fallback')};const root=preparePlan(input,plan());
  const calls=[];const ctx={parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async(_p,o)=>{
    calls.push(o.agentType);
    const findings=['code-gauntlet:bug-detector','code-gauntlet:gap-hunter'].includes(o.agentType)
      ? [{file:'outside',line_start:10,title:'Present defect',description:'A supported input fails.',severity:'high',confidence:90,anchor:'return value',evidence:'outside:10',change_origin:'introduced'}] : [];
    return{findings,complete:true,total_seen:findings.length};
  }};
  const adapter=executorAdapter(ctx,input);const execute=ctx.agent;
  ctx.agent=async(p,o)=>o.label===`discovery-plan-${phase}-0`?null:execute(p,o);
  try{
    const out=await discover(ctx,input);
    assert.equal(out.findings.length,phase==='select'?1:2);
    assert.equal(calls.includes('code-gauntlet:gap-hunter'),phase==='finalize');
    assert.equal(out.coverageLedger.status,'unavailable');
    assert.equal(out.coverageLedgerArtifact,undefined);
    assert.ok(out.gaps.some(g=>g.includes('coverage unavailable')));
    assert.equal(input.measurementTrace.snapshots[0].coverage,'complete');
  }finally{adapter.restore();rmSync(root,{recursive:true,force:true});}
});

for(const field of ['nonce_checksum','input_checksum','chunk_checksum','token_checksum','receipt_checksum','budget'])test(`planner rejects a mismatched ${field} receipt without a hunter`,async()=>{
  const input={};const root=preparePlan(input,plan());const ctx={agent:async()=>null};const adapter=executorAdapter(ctx,input);const execute=ctx.agent;
  ctx.agent=async(p,o)=>{const result=await execute(p,o);if(field==='budget'){const v=JSON.parse(result.token);v.summary.assigned_bytes=24001;result.token=JSON.stringify(v);result.token_checksum=fnv1a32(result.token);result.receipt_checksum=fnv1a32(`select|${result.part}|${result.parts}|${result.nonce_checksum}|${result.input_checksum}|${result.chunk_checksum}|${result.token}`);}else result[field]='wrong';return result;};
  try {assert.equal(await planOnDisk(ctx,input,'select',request(input)),null);} finally {adapter.restore();rmSync(root,{recursive:true,force:true});}
});

test('large citation requests are chunked before any model must copy them',async()=>{
  const input={};const root=preparePlan(input,plan());const ctx={agent:async()=>null};const adapter=executorAdapter(ctx,input);
  const refs=Array.from({length:300},(_,i)=>['reviewer',createHash('sha256').update(String(i)).digest('hex'),i+1,i+1]);
  try {
    assert.ok(await planOnDisk(ctx,input,'select',request(input,{refs})));
    assert.ok(adapter.executions.length>1);
    for(const e of adapter.executions){const a=shellSplit(e.prompt.split('\n').at(-1));assert.ok(a[a.indexOf('--chunk')+1].length<=DISCOVERY_CHUNK_CHARS);assert.ok(JSON.stringify(e.result).length<=DISCOVERY_RECEIPT_CHARS);}
  }finally{adapter.restore();rmSync(root,{recursive:true,force:true});}
});
