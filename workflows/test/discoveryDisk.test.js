import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, rmSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { discover } from '../src/discover.js';
import { createMeasurementTrace } from '../src/measurementTrace.js';
import { normalizeArgsReport } from '../src/args.js';
import { planOnDisk, discoveryRequests, plannerReceiptChecksum, DISCOVERY_REQUEST_CHARS, DISCOVERY_RECEIPT_CHARS, discoveryArtifactName } from '../src/discoveryDisk.js';
import { DISCOVERY_AGENT_TYPES } from '../src/registry.js';
import { agentSpecs } from '../src/discover.js';
import { fnv1a32, shellWord } from '../src/wire.js';
import { preparePlan,executorAdapter,readLedger } from './helpers/diskDiscovery.js';
import { shellSplit } from './helpers/shellWords.js';

const plan = (count=2) => ({version:2,status:'available',fact_packets:[],regions:Array.from({length:count},(_,i)=>({id:`r${i}`,file:`src/${i}`,line_start:1,line_end:2,reads:[{offset:3,limit:2,bytes:100}]}))});
const request = (input,extra={}) => ({context:input.contextPath,context_lines:input.contextLines,refs:[],risks:[],enabled:true,disabled_reason:'scope',...extra});

test('the planner boundary carries plain ascending file and source-line ranges',()=>{
  const parts=discoveryRequests({changedFiles:['z','a','b']},'select',{enabled:true,risks:[{path:'z',risk:'high'},{path:'a',risk:'high'}],refs:[['code-gauntlet:bug-detector','z',8,8],['code-gauntlet:bug-detector','a',15,17],['code-gauntlet:bug-detector','z',1,1],['code-gauntlet:bug-detector','a',12,14]]});
  assert.deepEqual(DISCOVERY_AGENT_TYPES,agentSpecs().map(spec=>spec.agentType));
  assert.deepEqual(parts,[{files_checksum:fnv1a32(JSON.stringify({files:['a','b','z'],reviewers:DISCOVERY_AGENT_TYPES})),mode:'full',high:'0,2',citations:{'0':'0:12-17;2:1,8'}}]);
});

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
    assert.ok(adapter.executions.every(e=>{const a=shellSplit(e.prompt.split('\n').at(-1));const plain=a[a.indexOf('--request')+1];assert.ok(JSON.parse(plain).files_checksum);assert.equal(a.includes('--chunk'),false);return plain.length<=DISCOVERY_REQUEST_CHARS;}));
    assert.ok(JSON.stringify(out).length<4096);
    assert.ok(JSON.stringify(input.measurementTrace.coverage_ledger).length<4096);
    assert.equal(out.coverageLedger.uncovered_count,2000);
    assert.equal(out.coverageLedger.remaining_uncovered_count,1999);
    assert.equal(input.measurementTrace.coverage_ledger.uncovered_count,2000);
    assert.equal(input.measurementTrace.coverage_ledger.remaining_uncovered_count,1999);
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

for(const field of ['nonce_checksum','input_checksum','request_checksum','receipt_checksum','budget'])test(`planner rejects a mismatched ${field} receipt without a hunter`,async()=>{
  const input={};const root=preparePlan(input,plan());const ctx={agent:async()=>null};const adapter=executorAdapter(ctx,input);const execute=ctx.agent;
  ctx.agent=async(p,o)=>{const result=await execute(p,o);if(field==='budget'){result.result.summary.assigned_bytes=24001;result.receipt_checksum=plannerReceiptChecksum('select',input.nonce,result);}else result[field]='wrong';return result;};
  try {assert.equal(await planOnDisk(ctx,input,'select',request(input)),null);} finally {adapter.restore();rmSync(root,{recursive:true,force:true});}
});

test('plain requests keep sixty findings over a hundred files and two thousand regions in one call per operation',async()=>{
  const regions=plan(2000);regions.regions.forEach((r,i)=>{r.file=`src/${i%100}`;r.line_start=100+Math.floor(i/100)*3;r.line_end=r.line_start+1;});
  const input={};const root=preparePlan(input,regions);const ctx={agent:async()=>null};const adapter=executorAdapter(ctx,input);
  const agents=['bug-detector','security-reviewer','cross-file-impact','test-analyzer','conventions-and-intent','type-design-analyzer','code-simplifier'];
  const refs=Array.from({length:60},(_,i)=>[`code-gauntlet:${agents[i%agents.length]}`,`src/${i}`,100,101]);
  try {
    const parts=discoveryRequests(input,'select',request(input,{refs}));
    assert.equal(parts.length,1);assert.ok(JSON.stringify(parts[0]).length<900);
    const selected=await planOnDisk(ctx,input,'select',request(input,{refs}));
    assert.ok(selected);assert.equal(adapter.executions.length,1);assert.equal(readLedger(input,selected.artifact).uncovered.length,1940);
    const final=await planOnDisk(ctx,input,'finalize',{state_checksum:selected.state_checksum,hunters:[{agent:'code-gauntlet:gap-hunter',status:'complete',anchors:[['gap-hunter-1','outside',10]],refs:[['code-gauntlet:gap-hunter','src/80',1,2]]}]});
    assert.ok(final);assert.equal(adapter.executions.length,2);
    for(const e of adapter.executions){const a=shellSplit(e.prompt.split('\n').at(-1));const plain=a[a.indexOf('--request')+1];assert.ok(plain.startsWith('{'));assert.ok(JSON.parse(plain).files_checksum);assert.ok(plain.length<900);assert.equal(a.includes('--chunk'),false);assert.ok(JSON.stringify(e.result).length<=2048);assert.equal(typeof e.result.result,'object');}
  }finally{adapter.restore();rmSync(root,{recursive:true,force:true});}
});

test('plain parts are individually bounded and excessive citation traffic disables coverage before dispatch',async()=>{
  const input={};const root=preparePlan(input,plan(2000));const ctx={agent:async()=>null};const adapter=executorAdapter(ctx,input);
  const refs=n=>Array.from({length:n},(_,i)=>['code-gauntlet:bug-detector',`src/${i}`,1,1]);
  try {
    assert.ok(await planOnDisk(ctx,input,'select',request(input,{refs:refs(300)})));
    assert.ok(adapter.executions.length>1 && adapter.executions.length<=4);
    for(const e of adapter.executions){const a=shellSplit(e.prompt.split('\n').at(-1));assert.ok(a[a.indexOf('--request')+1].length<=1000);JSON.parse(a[a.indexOf('--request')+1]);}
    const count=adapter.executions.length;
    assert.equal(await planOnDisk(ctx,input,'select',request(input,{refs:refs(1000)})),null);
    assert.equal(adapter.executions.length,count);
  }finally{adapter.restore();rmSync(root,{recursive:true,force:true});}
});


test('a semantically identical short receipt survives host property reordering',async()=>{
  const input={};const root=preparePlan(input,plan());const ctx={agent:async()=>null};const adapter=executorAdapter(ctx,input);const execute=ctx.agent;
  ctx.agent=async(p,o)=>{const env=await execute(p,o);env.result=Object.fromEntries(Object.entries(env.result).reverse());if(env.result.summary)env.result.summary=Object.fromEntries(Object.entries(env.result.summary).reverse());return env;};
  try{assert.ok(await planOnDisk(ctx,input,'select',request(input)));}finally{adapter.restore();rmSync(root,{recursive:true,force:true});}
});


test('a single copied digit drift is rejected on the first stored part',async()=>{
  const input={};const root=preparePlan(input,plan(2000));const ctx={agent:async()=>null};const adapter=executorAdapter(ctx,input);const execute=ctx.agent;
  ctx.agent=async(p,o)=>{const a=shellSplit(p.split('\n').at(-1));const at=a.indexOf('--request')+1;const value=JSON.parse(a[at]);const first=Object.keys(value.citations)[0];value.citations[first]=value.citations[first].replace(':1',':3');a[at]=JSON.stringify(value);return execute(`Run exactly once:\n${a.map(shellWord).join(' ')}`,o);};
  const refs=Array.from({length:300},(_,i)=>['code-gauntlet:bug-detector',`src/${i}`,1,1]);
  try{assert.equal(await planOnDisk(ctx,input,'select',request(input,{refs})),null);assert.equal(adapter.executions.length,1);assert.equal(adapter.executions[0].result.result.stored,true);assert.equal(existsSync(join(root,discoveryArtifactName('coverage-ledger',input.headShaShort,input.nonce))),false);}finally{adapter.restore();rmSync(root,{recursive:true,force:true});}
});

test('exceeding the plain request cap preserves primary discovery and records unavailable coverage',async()=>{
  const input={};const root=preparePlan(input,plan(2000));const calls=[];
  const ctx={parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async(_p,o)=>{calls.push(o.agentType);return {findings:o.agentType==='code-gauntlet:bug-detector'?[{file:'src/0',line_start:1,title:'Present defect',description:'A supported input fails.',severity:'high',confidence:90,anchor:'return value',evidence:'src/0:1',change_origin:'introduced',cross_file_refs:Array.from({length:1000},(_,i)=>`src/${i}:1`)}]:[],complete:true,total_seen:1};}};
  const adapter=executorAdapter(ctx,input);
  try{const out=await discover(ctx,input);assert.equal(out.findings.length,1);assert.equal(calls.length,7);assert.equal(adapter.executions.length,0);assert.equal(out.coverageLedger.status,'unavailable');assert.ok(out.gaps.some(g=>g.includes('coverage unavailable')));}finally{adapter.restore();rmSync(root,{recursive:true,force:true});}
});

test('an omitted plan reference discloses missing full-review coverage without dispatching the planner',async()=>{
  for(const deep of [true,false]) {
    const input={agentFlags:{deep},measurementTrace:createMeasurementTrace('absent')};const calls=[];
    const ctx={parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async(_p,o)=>{calls.push(o.agentType);return {findings:[],complete:true,total_seen:0};}};
    const out=await discover(ctx,input);
    assert.equal(out.gaps.some(g=>g.includes('discovery coverage unavailable')),deep);
    assert.equal(Object.hasOwn(out,'coverageLedger'),deep);
    if(deep){assert.equal(out.coverageLedger.status,'unavailable');assert.equal(input.measurementTrace.coverage_ledger.status,'unavailable');}
    assert.equal(calls.includes('code-gauntlet:executor'),false);
  }
});

test('a rejected primary record does not degrade clean sibling reviewers or disable the hunter',async()=>{
  const input={};const root=preparePlan(input,plan());const ctx={parallel:async ts=>Promise.all(ts.map(t=>t())),agent:async(_p,o)=>({findings:o.agentType==='code-gauntlet:bug-detector'?[null]:[],complete:true,total_seen:1})};const adapter=executorAdapter(ctx,input);
  try{const out=await discover(ctx,input);assert.deepEqual(out.degraded, ['test_coverage']);assert.ok(out.dispatched.includes('code-gauntlet:gap-hunter'));}finally{adapter.restore();rmSync(root,{recursive:true,force:true});}
});
