import { READ_PLAN_MAX_BYTES, READ_PLAN_MAX_LINES } from './contextPlan.js';
import { defineAgentCall } from './agentCalls.js';
import { compressMeasurement } from './measurementCompression.js';
import { fnv1a32, pythonArgv, shellWord } from './wire.js';
import { trustedScriptPath } from './paths.js';
import { modelFor, TRUSTED_SCRIPT_BASENAMES, DISCOVERY_TRANSPORT_LIMITS } from './registry.js';

export const DISCOVERY_CHUNK_CHARS = DISCOVERY_TRANSPORT_LIMITS.chunkChars;
export const DISCOVERY_RECEIPT_CHARS = DISCOVERY_TRANSPORT_LIMITS.receiptChars;
const HASH = /^[0-9a-f]{64}$/;
export const discoveryArtifactName = (kind, sha, nonce = '') => `code-gauntlet-${kind}${nonce ? `-${fnv1a32(nonce).slice(-8)}` : ''}-${sha}.${kind === 'gap-assignment' ? 'md' : 'json'}`;
export const unavailableCoverage = () => ({version:2,status:'unavailable',regions:[],uncovered:[],remaining_uncovered:[],omitted:[],hunters:[],budget_bytes:READ_PLAN_MAX_BYTES,assigned_bytes:0,omitted_fact_packets:0});
const callPlanner = defineAgentCall('discover:plan', () => ({type:'object',properties:{
  status:{type:'string'}, nonce_checksum:{type:'string'}, part:{type:'integer'},parts:{type:'integer'},input_checksum:{type:'string'},chunk_checksum:{type:'string'},token:{type:'string',maxLength:DISCOVERY_RECEIPT_CHARS},token_checksum:{type:'string'},receipt_checksum:{type:'string'},reason:{type:'string'},
},required:['status'],additionalProperties:false}));

// Input and output proofs catch a confused executor, like the verify bridge; they
// do not authenticate an executor deliberately fabricating script results.
export async function planOnDisk(ctx, input, phase, request) {
  const ref = input.discoveryPlanRef;
  const expected = discoveryArtifactName('discovery-plan', input.headShaShort);
  if (!ref || typeof ref !== 'object' || ref.path !== expected || !HASH.test(ref.sha256 || '')
    || !input.outputDir || !input.pluginRoot || !input.nonce || !input.contextPath) return null;
  const token = compressMeasurement(JSON.stringify(request));
  const inputChecksum = fnv1a32(token);
  const parts = Math.ceil(token.length / DISCOVERY_CHUNK_CHARS);
  if (parts > DISCOVERY_TRANSPORT_LIMITS.maxChunks) return null;
  let result = null;
  for (let part = 0; part < parts; part++) {
    const chunk = token.slice(part * DISCOVERY_CHUNK_CHARS, (part + 1) * DISCOVERY_CHUNK_CHARS);
    const command = [...pythonArgv(input.python),trustedScriptPath(input.pluginRoot,TRUSTED_SCRIPT_BASENAMES.discovery),
      '--output-dir',input.outputDir,'--sha',input.headShaShort,'--nonce',input.nonce,'--phase',phase,
      '--plan-checksum',ref.sha256,'--input-checksum',inputChecksum,'--part',String(part),'--parts',String(parts),'--chunk',chunk].map(shellWord).join(' ');
    let env;
    try {
      env = await callPlanner(ctx,`Run exactly once and return the complete JSON stdout unchanged:\n${command}`, {
        label:`discovery-plan-${phase}-${part}`,agentType:'code-gauntlet:executor',model:modelFor('code-gauntlet:executor',input.policy || {}),
      });
    } catch { return null; }
    if (!env || JSON.stringify(env).length > DISCOVERY_RECEIPT_CHARS || env.status !== 'ok'
      || env.nonce_checksum !== fnv1a32(input.nonce) || env.part !== part || env.parts !== parts
      || env.input_checksum !== inputChecksum || env.chunk_checksum !== fnv1a32(chunk)
      || typeof env.token !== 'string' || env.token_checksum !== fnv1a32(env.token)
      || env.receipt_checksum !== fnv1a32(`${phase}|${part}|${parts}|${fnv1a32(input.nonce)}|${inputChecksum}|${fnv1a32(chunk)}|${env.token}`)) return null;
    try { result = JSON.parse(env.token); } catch { return null; }
    if (part < parts - 1 && result?.stored !== true) return null;
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
