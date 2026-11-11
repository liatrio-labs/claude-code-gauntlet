import { READ_PLAN_MAX_BYTES, READ_PLAN_MAX_LINES } from './contextPlan.js';
import { defineAgentCall } from './agentCalls.js';
import { fnv1a32, pythonArgv, shellWord } from './wire.js';
import { trustedScriptPath } from './paths.js';
import { modelFor, TRUSTED_SCRIPT_BASENAMES, DISCOVERY_TRANSPORT_LIMITS, DISCOVERY_AGENT_TYPES } from './registry.js';

export const DISCOVERY_REQUEST_CHARS = DISCOVERY_TRANSPORT_LIMITS.requestChars;
export const DISCOVERY_RECEIPT_CHARS = DISCOVERY_TRANSPORT_LIMITS.receiptChars;
const HASH = /^[0-9a-f]{64}$/;
export const discoveryArtifactName = (kind, sha, nonce = '') => `code-gauntlet-${kind}${nonce ? `-${fnv1a32(nonce).slice(-8)}` : ''}-${sha}.${kind === 'gap-assignment' ? 'md' : 'json'}`;
export const unavailableCoverage = () => ({version:2,status:'unavailable',regions:[],uncovered:[],remaining_uncovered:[],omitted:[],hunters:[],budget_bytes:READ_PLAN_MAX_BYTES,assigned_bytes:0,omitted_fact_packets:0});
const integer = {type:'integer'};
const string = {type:'string'};
const callPlanner = defineAgentCall('discover:plan', () => ({type:'object',properties:{
  status:string, nonce_checksum:string, part:integer,parts:integer,input_checksum:string,request_checksum:string,receipt_checksum:string,reason:string,
  result:{type:'object',properties:{stored:{type:'boolean'},available:{type:'boolean'},reason:string,
    ledger_checksum:string,state_checksum:string,assigned_count:integer,assignment_checksum:{type:['string','null']},assignment_lines:integer,assignment_prompt:string,
    summary:{type:'object',properties:{version:integer,status:string,region_count:integer,uncovered_count:integer,omitted_count:integer,hunter_count:integer,
      hunters:{type:'array',items:{type:'object',properties:{}}},omitted:{type:'array',items:{type:'object',properties:{}}},budget_bytes:integer,assigned_bytes:integer,omitted_fact_packets:integer},additionalProperties:false},
  },additionalProperties:false},
},required:['status'],additionalProperties:false}));

const spanText = ([a,b]) => a === b ? String(a) : `${a}-${b}`;
function mergedSpans(spans) {
  const out=[];
  for(const [a,b] of spans.sort((x,y)=>x[0]-y[0] || x[1]-y[1])) {
    if(!Number.isSafeInteger(a) || !Number.isSafeInteger(b) || a<1 || b<a) continue;
    const last=out.at(-1);
    if(last && a<=last[1]+1)last[1]=Math.max(last[1],b);else out.push([a,b]);
  }
  return out;
}

// File indexes name the existing snapshot manifest, never model-copied path text.
// Each part is independently readable JSON; only complete records cross a boundary.
export function discoveryRequests(input,phase,request) {
  const files=[...new Set(input.changedFiles || [])].sort();
  const indexes=new Map(files.map((f,i)=>[f,i]));
  const reviewers=new Map(DISCOVERY_AGENT_TYPES.map((agent,i)=>[agent,String(i)]));
  const base={files_checksum:fnv1a32(JSON.stringify({files,reviewers:DISCOVERY_AGENT_TYPES})),...(phase==='select'
    ? {mode:request.enabled ? 'full' : request.disabled_reason}
    : {state_checksum:request.state_checksum,hunters:request.hunters.map(h=>({agent:reviewers.get(h.agent),status:h.status}))})};
  const records=[];
  if(phase==='select')for(const risk of ['high','low']) {
    const wanted=[...new Set((request.risks || []).filter(r=>r.risk===risk && indexes.has(r.path)).map(r=>indexes.get(r.path)))].sort((a,b)=>a-b);
    for(const i of wanted)records.push({kind:risk,index:i});
  }
  const references=phase==='select' ? request.refs : request.hunters.flatMap(h=>h.refs);
  const grouped=new Map();
  for(const [agent,file,a,b] of references) {
    if(!indexes.has(file))continue;
    const name=reviewers.get(agent);if(name===undefined)return null;
    const key=`${name}/${indexes.get(file)}`;
    if(!grouped.has(key))grouped.set(key,{agent:name,index:indexes.get(file),spans:[]});
    grouped.get(key).spans.push([a,b]);
  }
  for(const row of [...grouped.values()].sort((a,b)=>Number(a.agent)-Number(b.agent)||a.index-b.index))
    for(const span of mergedSpans(row.spans))records.push({kind:'citation',agent:row.agent,index:row.index,text:spanText(span)});
  if(phase==='finalize')for(const h of request.hunters) {
    const anchors=h.anchors.map(([id,file,line])=>[String(id),indexes.get(file) ?? -1,line])
      .sort((a,b)=>a[1]-b[1] || a[2]-b[2] || (a[0]<b[0]?-1:a[0]>b[0]?1:0));
    for(const anchor of anchors)records.push({kind:'anchor',agent:reviewers.get(h.agent),anchor});
  }
  const parts=[];
  let value={...base};
  const append=(target,r)=>{
    if(r.kind==='citation') {
      target.citations ||= {};
      const rows=target.citations[r.agent] || '';
      target.citations[r.agent]=rows && rows.split(';').at(-1).split(':')[0]===String(r.index)
        ? `${rows},${r.text}` : `${rows}${rows ? ';' : ''}${r.index}:${r.text}`;
    } else if(r.kind==='anchor') {
      target.anchors ||= {};
      (target.anchors[r.agent] ||= []).push(r.anchor);
    } else {
      const spans=(target[r.kind] || '').split(',').filter(Boolean);
      const last=spans.at(-1), end=last ? Number(last.split('-').at(-1)) : -2;
      if(r.index===end+1)spans[spans.length-1]=`${last.split('-')[0]}-${r.index}`;else spans.push(String(r.index));
      target[r.kind]=spans.join(',');
    }
  };
  for(const record of records) {
    const next=JSON.parse(JSON.stringify(value));append(next,record);
    if(JSON.stringify(next).length<=DISCOVERY_REQUEST_CHARS)value=next;
    else {
      parts.push(value);value={...base};append(value,record);
      if(parts.length>=DISCOVERY_TRANSPORT_LIMITS.maxParts || JSON.stringify(value).length>DISCOVERY_REQUEST_CHARS)return null;
    }
  }
  parts.push(value);
  return parts.every(p=>JSON.stringify(p).length<=DISCOVERY_REQUEST_CHARS) ? parts : null;
}

export function plannerReceiptChecksum(phase,nonce,receipt) {
  const result=JSON.stringify(receipt.result,(_key,value)=>value && typeof value==='object' && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).sort(([a],[b])=>a<b?-1:a>b?1:0)) : value);
  return fnv1a32(`${phase}|${receipt.part}|${receipt.parts}|${fnv1a32(nonce)}|${receipt.input_checksum}|${receipt.request_checksum}|${result}`);
}

// Like verify receipts, these proofs detect transcription drift, not a malicious executor.
export async function planOnDisk(ctx, input, phase, request) {
  const ref = input.discoveryPlanRef;
  const expected = discoveryArtifactName('discovery-plan', input.headShaShort);
  if (!ref || typeof ref !== 'object' || ref.path !== expected || !HASH.test(ref.sha256 || '')
    || !input.outputDir || !input.pluginRoot || !input.nonce || !input.contextPath) return null;
  const requests=discoveryRequests(input,phase,request);
  if(!requests)return null;
  const inputChecksum=fnv1a32(JSON.stringify(requests)),parts=requests.length;
  let result=null;
  for(let part=0;part<parts;part++) {
    const plain=JSON.stringify(requests[part]);
    const command = [...pythonArgv(input.python),trustedScriptPath(input.pluginRoot,TRUSTED_SCRIPT_BASENAMES.discovery),
      '--output-dir',input.outputDir,'--sha',input.headShaShort,'--nonce',input.nonce,'--phase',phase,
      '--plan-checksum',ref.sha256,'--input-checksum',inputChecksum,'--part',String(part),'--parts',String(parts),'--request',plain].map(shellWord).join(' ');
    let env;
    try {
      env = await callPlanner(ctx,`Run exactly once and return the complete JSON stdout unchanged:\n${command}`, {
        label:`discovery-plan-${phase}-${part}`,agentType:'code-gauntlet:executor',model:modelFor('code-gauntlet:executor',input.policy || {}),
      });
    } catch { return null; }
    if (!env || JSON.stringify(env).length > DISCOVERY_RECEIPT_CHARS || env.status !== 'ok'
      || env.nonce_checksum !== fnv1a32(input.nonce) || env.part !== part || env.parts !== parts
      || env.input_checksum !== inputChecksum || env.request_checksum !== fnv1a32(plain)
      || !env.result || env.receipt_checksum !== plannerReceiptChecksum(phase,input.nonce,env)) return null;
    result=env.result;
    if(part<parts-1 && result.stored!==true)return null;
  }
  if (!result?.available) return null;
  const summary = result.summary;
  if (!HASH.test(result.ledger_checksum || '') || !HASH.test(result.state_checksum || '')
    || !summary || summary.version !== 2 || summary.status !== 'trimmed'
    || !['region_count','uncovered_count','omitted_count','hunter_count','assigned_bytes','omitted_fact_packets'].every((key) => Number.isSafeInteger(summary[key]) && summary[key] >= 0)
    || summary.budget_bytes !== READ_PLAN_MAX_BYTES || summary.assigned_bytes > READ_PLAN_MAX_BYTES
    || !Array.isArray(summary.hunters) || summary.hunters.length || !Array.isArray(summary.omitted) || summary.omitted.length
    || !Number.isSafeInteger(result.assigned_count) || result.assigned_count < 0
    || !Number.isSafeInteger(result.assignment_lines) || result.assignment_lines < 0 || result.assignment_lines > READ_PLAN_MAX_LINES) return null;
  if (phase === 'select' && result.assigned_count > 0 && (!HASH.test(result.assignment_checksum || '')
    || typeof result.assignment_prompt !== 'string' || result.assignment_prompt.length > 2048)) return null;
  return {...result,artifact:{path:discoveryArtifactName('coverage-ledger',input.headShaShort,input.nonce),sha256:result.ledger_checksum}};
}
