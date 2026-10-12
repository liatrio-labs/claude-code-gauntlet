import { pathEscapeReason, persistPlanPath, plannedArtifactPaths, requireAbsoluteOutputDir } from './outputPaths.js';
import { hardenEscapeRuns, persistDerivable, persistPlan, persistPrimaries, writerPayload } from './persistPlan.js';
import { modelFor } from './registry.js';
import { RETURN_CHAR_BUDGET, shellWord } from './wire.js';

const WRITER_SCHEMA = {
  type: 'object',
  properties: { artifactPaths: { type: 'object', properties: {} } },
};
const WRITTEN_SCHEMA = {
  type: 'object',
  properties: { written: { type: 'array', items: { type: 'string' } } },
};

// The schema accepts empty or partial echoes, so all planned paths need independent coverage.
// A self-reported echo checks consistency and liveness; it cannot prove a disk write.
function writerEchoCoversPaths(echoed, paths) {
  if (!echoed || typeof echoed !== 'object') return false;
  return Object.keys(paths).every((k) => echoed[k] === paths[k]);
}

function receiptRows(receipt, key) {
  return Array.isArray(receipt[key]) ? receipt[key] : [];
}

function persistErrorMessage(e) {
  return (e && e.message) || 'unknown';
}

function gradePrimaryRow(row, expect) {
  if (!row) return 'missing';
  if (row.expected_chars !== expect.chars || row.expected_checksum !== expect.checksum) return 'foreign';
  const same = row.chars === expect.chars && row.checksum === expect.checksum;
  if ((row.content_proof === 'match') !== same) return 'incoherent';
  return same ? 'match' : 'differs';
}

// Returns { artifactPaths, gaps, partial }, plus persistReturn on the return channel.
// Persistence failures are disclosed as partial artifacts.
// Requires an absolute confined outputDir; invalid roots or stamped paths throw.
// Serialization stays inside the persistence guard because it can fail before any dispatch.
export async function writeArtifacts(ctx, input) {
  // bench/runner/check.py keys on `partial-artifacts` and `no write proof` in these gaps.
  const partial = (reason, extraGaps, salvaged) => ({
    artifactPaths: {
      findings: (salvaged && salvaged.findings) || null,
      report: (salvaged && salvaged.report) || null,
      postReview: null,
      checkpoints: null,
    },
    gaps: extraGaps.concat([
      `writeArtifacts: ${reason} — artifacts not persisted (partial-artifacts)`
      + (salvaged && (salvaged.findings || salvaged.report)
        ? ` — content-proven on disk and still usable: ${[salvaged.findings, salvaged.report].filter(Boolean).join(', ')}`
        : ''),
    ]),
    partial: true,
  });

  // Invalid roots and path stamps are caller errors, so they throw outside the persistence guard.
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const outputDir = requireAbsoluteOutputDir(inp.outputDir);
  const sha = inp.headShaShort || 'head';
  const policy = inp.policy || {};
  const paths = plannedArtifactPaths(outputDir, sha);

  try {
    const persist = inp.persist || {};
    const scriptPath = typeof persist.assembleScriptPath === 'string' && persist.assembleScriptPath !== ''
      ? persist.assembleScriptPath
      : null;
    // The return channel needs no executor script, so its opt-in is independent of scriptPath.
    const wantsReturn = persist.returnPrimaries === true;
    if (scriptPath || wantsReturn) {
      const derivable = persistDerivable(inp);
      if (derivable.ok) {
        // Prefer the return channel to avoid model transcription; oversized returns fall back with disclosure.
        if (wantsReturn) {
          const viaReturn = returnChannelPersist(inp, paths, outputDir, sha);
          if (viaReturn.ok) return viaReturn.result;
          const gap = `writeArtifacts: ${viaReturn.reason} — persisted through the artifact-writer instead, which transcribes the bytes`;
          if (!scriptPath) return await writeArtifactsLegacy(ctx, inp, paths, outputDir, policy, partial, [gap]);
          const fallback = await writeArtifactsDerived(ctx, inp, paths, outputDir, sha, policy, partial, scriptPath);
          return { ...fallback, gaps: [gap].concat(fallback.gaps) };
        }
        return await writeArtifactsDerived(ctx, inp, paths, outputDir, sha, policy, partial, scriptPath);
      }
      return await writeArtifactsLegacy(ctx, inp, paths, outputDir, policy, partial, [
        `writeArtifacts: derived persistence unavailable (${derivable.reason}) — persisted the full by-value payload instead`,
      ]);
    }
    // A caller without persistence options loses no artifacts, so the by-value path adds no gap.
    return await writeArtifactsLegacy(ctx, inp, paths, outputDir, policy, partial, []);
  } catch (e) {
    return partial(`persistence threw before any artifact was written (${persistErrorMessage(e)})`, []);
  }
}

// Fallback disclosures must survive both success and partial-artifact returns.
async function writeArtifactsLegacy(c, inp, paths, outputDir, policy, partial, extraGaps) {
  const model = modelFor('code-gauntlet:artifact-writer', policy);
  try {
    const result = await c.agent(writeArtifactsPrompt(inp, paths), {
      label: 'artifact-writer',
      agentType: 'code-gauntlet:artifact-writer',
      model,
      schema: WRITER_SCHEMA,
    });
    if (!result) return partial('writer returned null', extraGaps);
    const echoed = result.artifactPaths;
    const escape = pathEscapeReason(
      outputDir,
      Object.keys(paths).map((k) => ({
        name: k,
        path: echoed ? echoed[k] : undefined,
        present: !!(echoed && Object.hasOwn(echoed, k)),
      })),
    );
    if (escape) return partial(escape, extraGaps);
    if (!writerEchoCoversPaths(echoed, paths)) {
      return partial('writer echo did not account for all four planned artifact paths (no write proof)', extraGaps);
    }
    return { artifactPaths: paths, gaps: extraGaps, partial: false };
  } catch (e) {
    return partial(`writer agent threw (${persistErrorMessage(e)})`, extraGaps);
  }
}

// Both channels share serialization so their strings and checksum expectations cannot drift.
// Plan escape hardening preserves parsed values, so the same spelling also serves the return channel.
function persistEntries(inp, paths, outputDir, sha) {
  const planPath = persistPlanPath(outputDir, sha);
  const { findingsJson, reportMd } = persistPrimaries(inp);
  // Independent expectations prevent the receipt from grading itself.
  const plan = persistPlan(inp, paths);
  return {
    plan,
    planPath,
    entries: [
      { path: paths.findings, text: findingsJson },
      { path: paths.report, text: reportMd },
      { path: planPath, text: hardenEscapeRuns(JSON.stringify(plan, null, 2)) },
    ],
  };
}

// The harness carries these bytes without model transcription; the caller must materialize them.
// Returned artifact paths describe intended destinations, not files already on disk.
function returnChannelPersist(inp, paths, outputDir, sha) {
  const { planPath, entries } = persistEntries(inp, paths, outputDir, sha);
  const chars = JSON.stringify(entries).length;
  if (chars > RETURN_CHAR_BUDGET) {
    return {
      ok: false,
      reason: `the persisted primaries serialize to ${chars} chars, over the ${RETURN_CHAR_BUDGET}-char return budget`,
    };
  }
  return {
    ok: true,
    result: {
      artifactPaths: paths,
      gaps: [],
      partial: false,
      // The nonce lets the caller identify this run's returned payload when no task id is available.
      persistReturn: { channel: 'return', nonce: inp.nonce || null, planPath, entries },
    },
  };
}

// Retry only an assemble refusal: a fresh writer sample can repair the transcribed inputs.
// A tolerated primary mismatch keeps usable on-disk truth; retrying would replace it with unknown bytes.
// The by-value writer cannot check content, so it cannot safely recover a failed content proof.
async function writeArtifactsDerived(c, inp, paths, outputDir, sha, policy, partial, assembleScriptPath) {
  const { plan, planPath, entries } = persistEntries(inp, paths, outputDir, sha);
  const attempt = () => attemptDerivedPersist(c, entries, planPath, plan, paths, outputDir, policy, assembleScriptPath);

  // Only the last attempt can prove salvageable bytes because retrying overwrites the files.
  const salvage = (att) => {
    const proven = new Set(att.proven || []);
    return {
      findings: proven.has(paths.findings) ? paths.findings : null,
      report: proven.has(paths.report) ? paths.report : null,
    };
  };

  const first = await attempt();
  if (first.ok) return { artifactPaths: paths, gaps: first.gaps, partial: false };
  if (!first.retryable) return partial(first.reason, [], salvage(first));

  const second = await attempt();
  if (second.ok) {
    return {
      artifactPaths: paths,
      gaps: [`artifact-persist-retry: the first derived persist attempt failed (${first.reason}); a second artifact-writer dispatch succeeded and the artifacts below are from that attempt`]
        .concat(second.gaps),
      partial: false,
    };
  }
  return partial(`${second.reason} — retried once after the first attempt failed (${first.reason})`, [], salvage(second));
}

// Only a receipt refusal is retryable; an incomplete dispatch has no assemble proof to repair.
async function attemptDerivedPersist(c, entries, planPath, plan, paths, outputDir, policy, assembleScriptPath) {
  const fail = (reason, retryable) => ({ ok: false, retryable: !!retryable, reason });

  let writerOut;
  try {
    writerOut = await c.agent(finalArtifactsWriterPrompt(entries), {
      label: 'artifact-writer',
      agentType: 'code-gauntlet:artifact-writer',
      model: modelFor('code-gauntlet:artifact-writer', policy),
      schema: WRITTEN_SCHEMA,
    });
  } catch (e) {
    return fail(`writer agent threw (${persistErrorMessage(e)})`);
  }
  if (!writerOut) return fail('writer returned null');
  // The schema permits an empty written list; coverage prevents assembly from reading unwritten primaries.
  const writtenList = Array.isArray(writerOut.written) ? writerOut.written : [];
  const writtenEscape = pathEscapeReason(
    outputDir,
    writtenList.map((p, i) => ({ name: `written[${i}]`, path: p, present: true })),
  );
  if (writtenEscape) return fail(writtenEscape);
  const written = new Set(writtenList);
  if (!entries.every((e) => written.has(e.path))) {
    return fail('writer echo did not cover all three primary artifact paths (no write proof)');
  }

  let receipt;
  try {
    receipt = await c.agent(assemblePrompt(assembleScriptPath, planPath), {
      label: 'assemble-artifacts',
      agentType: 'code-gauntlet:executor',
      model: modelFor('code-gauntlet:executor', policy),
      schema: ASSEMBLE_RECEIPT_SCHEMA,
    });
  } catch (e) {
    return fail(`assemble executor threw (${persistErrorMessage(e)})`);
  }
  if (receipt && typeof receipt === 'object') {
    const verified = receiptRows(receipt, 'verified');
    const receiptWritten = receiptRows(receipt, 'written');
    const receiptEscape = pathEscapeReason(outputDir, [
      ...verified.map((e, i) => ({ name: `verified[${i}]`, path: e && e.path, present: true })),
      ...receiptWritten.map((e, i) => ({ name: `receipt.written[${i}]`, path: e && e.path, present: true })),
    ]);
    if (receiptEscape) return fail(receiptEscape);
  }
  const trust = trustAssembleReceipt(receipt, paths, plan);
  if (!trust.ok) return { ...fail(trust.reason, true), proven: provenPrimaryPaths(receipt, paths, plan) };
  return { ok: true, gaps: trust.gaps };
}

// Independent expectations reject stale or incoherent receipts instead of letting a receipt grade itself.
// The executor can read the plan, so these checks provide consistency rather than authentication.
function trustAssembleReceipt(receipt, paths, plan) {
  if (!receipt || typeof receipt !== 'object') return { ok: false, reason: 'assemble executor returned no receipt' };
  if (receipt.ok !== true) {
    const errors = Array.isArray(receipt.errors) ? receipt.errors.join('; ') : '';
    return { ok: false, reason: `assemble script reported failure (${errors || 'no reason given'})` };
  }
  if (receipt.planChecksum !== plan.planChecksum) {
    return {
      ok: false,
      reason: `assemble receipt echoed plan checksum ${receipt.planChecksum === undefined ? 'none' : receipt.planChecksum} but the pipeline computed ${plan.planChecksum} — the executor did not run this persist plan`,
    };
  }
  const verified = receiptRows(receipt, 'verified');
  const written = receiptRows(receipt, 'written');
  const verifiedByPath = new Map(verified.map((e) => [(e && e.path), e]));
  const expectByPath = new Map(plan.expect.map((e) => [e.path, e]));
  for (const p of [paths.findings, paths.report]) {
    const got = verifiedByPath.get(p);
    const want = expectByPath.get(p);
    const grade = gradePrimaryRow(got, want);
    if (grade === 'missing') return { ok: false, reason: `assemble receipt did not verify ${p} (no content proof)` };
    if (grade === 'foreign') {
      return {
        ok: false,
        reason: `assemble receipt checked ${p} against a foreign expectation (receipt says ${got.expected_chars} chars/${got.expected_checksum}, the pipeline handed the writer ${want.chars}/${want.checksum})`,
      };
    }
    if (grade === 'incoherent') {
      return {
        ok: false,
        reason: `assemble receipt is incoherent for ${p}: content_proof:"${got.content_proof}" contradicts its own chars/checksum (${got.chars}/${got.checksum} vs expected ${want.chars}/${want.checksum})`,
      };
    }
  }
  // A derived mismatch has no independent on-disk source to fall back to and is structural.
  // When findings itself diverges, its projections can legitimately differ; disclose those differences.
  // The primary mismatch remains usable on-disk truth, so that exception preserves its non-fatal treatment.
  const writtenByPath = new Map(written.map((e) => [(e && e.path), e]));
  const deriveByPath = new Map(plan.derive.map((e) => [e.path, e]));
  const findingsProof = verifiedByPath.get(paths.findings);
  const sourceDiverged = findingsProof.content_proof === 'mismatch';
  const derivedGaps = [];
  for (const p of [paths.postReview, paths.checkpoints]) {
    if (!writtenByPath.has(p)) return { ok: false, reason: `assemble receipt did not write ${p} (no write proof)` };
    const want = deriveByPath.get(p);
    const got = writtenByPath.get(p);
    if (got.chars === want.chars && got.checksum === want.checksum) continue;
    const detail = `derived document ${p} does not match the pipeline's own derivation (receipt reports ${got.chars == null ? 'no' : got.chars} chars/${got.checksum == null ? 'no checksum' : got.checksum}, the pipeline derived ${want.chars}/${want.checksum})`;
    if (sourceDiverged) {
      derivedGaps.push(`artifact-content-proof: ${detail} — expected, since ${paths.findings} it was derived from also diverged`);
      continue;
    }
    return { ok: false, reason: `${detail} — the delivered payload is not what this run produced` };
  }
  // Primary gaps precede derived gaps so the cause appears before its consequences.
  const gaps = [];
  for (const e of verified) {
    if (e && e.content_proof === 'mismatch') {
      gaps.push(`artifact-content-proof: ${e.path} bytes on disk differ from the payload handed to the writer (expected ${e.expected_chars} chars/checksum ${e.expected_checksum}, got ${e.chars}/${e.checksum})`);
    }
  }
  gaps.push(...derivedGaps);
  return { ok: true, gaps };
}

// Returns primary paths independently proven against this plan, even when the receipt is refused.
// Salvage preserves usable bytes without declaring the overall persist successful.
function provenPrimaryPaths(receipt, paths, plan) {
  if (!receipt || typeof receipt !== 'object') return [];
  if (receipt.planChecksum !== plan.planChecksum) return [];
  const byPath = new Map(receiptRows(receipt, 'verified').map((e) => [(e && e.path), e]));
  const expectByPath = new Map(plan.expect.map((e) => [e.path, e]));
  const proven = [];
  for (const p of [paths.findings, paths.report]) {
    const got = byPath.get(p);
    const want = expectByPath.get(p);
    if (gradePrimaryRow(got, want) === 'match') proven.push(p);
  }
  return proven;
}

// A single JSON line after the final marker makes the payload recoverable without parsing prompt prose.
const WRITER_PAYLOAD_MARKER = 'PAYLOAD_JSON:';

// Returns the payload after the final marker, or null if absent; throws for invalid JSON.
export function parseWriterPayload(prompt) {
  const idx = (prompt || '').lastIndexOf(WRITER_PAYLOAD_MARKER);
  if (idx === -1) return null;
  return JSON.parse(prompt.slice(idx + WRITER_PAYLOAD_MARKER.length));
}

function writeArtifactsPrompt(inp, paths) {
  const payload = JSON.stringify(writerPayload(inp));
  return `Persist these code-gauntlet artifacts to disk exactly as given (the workflow has no disk access). Write the payload's findings (as pretty JSON) to ${paths.findings}, the payload's report (verbatim markdown) to ${paths.report}, the payload's postReview (the pre-selected delivery set, as pretty JSON) to ${paths.postReview}, and the payload's checkpoints (as JSON) to ${paths.checkpoints}. Return { artifactPaths } echoing the paths you wrote. The payload is the single JSON line after the marker below.\n${WRITER_PAYLOAD_MARKER}${payload}`;
}

// Exact strings constrain the writer to the byte spelling the content proof checks.
function finalArtifactsWriterPrompt(entries) {
  const payload = JSON.stringify(entries);
  return `Persist these code-gauntlet artifacts to disk exactly as given (the workflow has no disk access). For every entry in the payload, write its "text" VERBATIM to its "path" — byte for byte, nothing before it and NOTHING AFTER THE FINAL BYTE (no trailing commentary, no tool-call markup). Do not reformat, re-indent, or re-serialize. Return { written } listing the paths you wrote. The payload is the single JSON line after the marker below.\n${WRITER_PAYLOAD_MARKER}${payload}`;
}

const ASSEMBLE_RECEIPT_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    planVersion: { type: 'number' },
    planChecksum: { type: 'string' },
    verified: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          chars: { type: 'number' },
          expected_chars: { type: 'number' },
          checksum: { type: 'string' },
          expected_checksum: { type: 'string' },
          content_proof: { type: 'string' },
        },
      },
    },
    written: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          chars: { type: 'number' },
          checksum: { type: 'string' },
        },
      },
    },
    errors: { type: 'array', items: { type: 'string' } },
  },
};

// Shell quoting keeps paths with spaces in one argument without exposing shell operators.
function assemblePrompt(scriptPath, planPath) {
  return `Run exactly this command, then return its single line of stdout JSON verbatim via the schema:\npython3 ${shellWord(scriptPath)} --plan ${shellWord(planPath)}`;
}
