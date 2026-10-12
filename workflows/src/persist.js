import { pathEscapeReason, persistPlanPath, plannedArtifactPaths, requireAbsoluteOutputDir } from './outputPaths.js';
import { hardenEscapeRuns, persistDerivable, persistPlan, persistPrimaries, writerPayload } from './persistPlan.js';
import { modelFor } from './registry.js';
import { RETURN_CHAR_BUDGET, shellWord } from './wire.js';

// --- Persistence: writeArtifacts --------------------------------------------

const WRITER_SCHEMA = {
  type: 'object',
  properties: { artifactPaths: { type: 'object', properties: {} } },
};
// The persist writer returns the list of paths it wrote.
const WRITTEN_SCHEMA = {
  type: 'object',
  properties: { written: { type: 'array', items: { type: 'string' } } },
};
const ARTIFACT_PATH_KEYS = ['findings', 'report', 'postReview', 'checkpoints'];

// Write-proof: the echoed artifactPaths must account for EVERY planned path (each key
// present and echoing the exact path we dispatched). WRITER_SCHEMA declares no `required`,
// so an empty {} echo is schema-valid — a writer under StructuredOutput retry pressure can
// return one having written nothing. Same threat model as trustSlice: a self-reported echo
// is a consistency/liveness check, not proof-of-write, but requiring the four exact paths
// stops a degenerate {} (or a partial echo) from passing as a full persist.
function writerEchoCoversPaths(echoed, paths) {
  if (!echoed || typeof echoed !== 'object') return false;
  return ARTIFACT_PATH_KEYS.every((k) => echoed[k] === paths[k]);
}

// writeArtifacts(ctx, { findings, postReview, report, checkpoints, outputDir,
// headShaShort, policy, persist }) -> { artifactPaths, gaps, partial }
// The workflow script has NO disk access, so agents persist findings.json + report.md
// + the post-review delivery set + the checkpoint/progress JSON to {output_dir}.
//
// THREE persistence paths, one PUBLIC contract (same return shape, the same four
// artifactPaths keys, the same partial-artifacts degradation):
//
//   RETURN (taken whenever args.persist.returnPrimaries is true — the primary path, and
//     independent of assembleScriptPath, which only the writer path's executor needs).
//     No agent is dispatched at all. The three primaries ride home in the workflow's
//     own return value, which the HARNESS serializes; Phase 8 materializes them with
//     scripts/gauntlet/materialize.py and derives the projections from disk. See
//     returnChannelPersist for the measurement and the failure mode it removes.
//
//   DERIVED (D3; taken when args.persist.assembleScriptPath is present and
//     the return channel was not asked for or could not carry the run).
//     Measured on a real run: of the 88,389 B the writer emitted, the post-review
//     findings array was canonically byte-identical to findings.json, the checkpoint's
//     phases.challenge.findings was the alias-stripped twin of the same array, and the
//     genuine residual was 383 B. So the writer now persists ONLY the unique content
//     (findings.json, report.md, the persist plan) and the executor runs the pinned
//     scripts/gauntlet/artifacts.py to DERIVE the two projections from what actually
//     landed on disk, returning a content-proof receipt.
//
//   LEGACY (no persist waist, or the id-integrity guard refused). One artifact-writer
//     dispatch carrying all four artifacts by value. Unchanged.
//
// Wrapped in its own try/catch: a throw OR null result degrades to
// a partial-artifacts gap with null paths and is NON-FATAL — it never bubbles to the
// top-level catch. The try covers the WHOLE body, not just the dispatches: the derived
// path computes the plan and the primaries (JSON.stringify over caller-supplied objects,
// a deep clone of the checkpoint) BEFORE any agent is called, and a throw there is exactly
// as non-fatal as a writer failure. Keeping those computations outside the guard is the
// regression this shape exists to prevent — SKILL.md's Error Recovery promises the caller
// that writer failure never ends the run.
// `salvaged` (optional, derived path only) names the primaries a refused assemble receipt
// nevertheless PROVED are on disk byte-identical to the payload — see provenPrimaryPaths.
// They are surfaced instead of nulled so Phase 8's documented "deliver whatever
// artifactPaths.report exists" branch is reachable. `partial` stays true and the failure
// gap stays, so nothing downstream mistakes this for a successful persist; the DERIVED
// paths are always null here because nothing derived them.
export async function writeArtifacts(ctx, input) {
  const partial = (reason, extraGaps, salvaged) => ({
    artifactPaths: {
      findings: (salvaged && salvaged.findings) || null,
      report: (salvaged && salvaged.report) || null,
      postReview: null,
      checkpoints: null,
    },
    gaps: (extraGaps || []).concat([
      `writeArtifacts: ${reason} — artifacts not persisted (partial-artifacts)`
      // Never silently imply more landed than did: the salvaged paths are named, and a
      // run with nothing salvaged reads exactly as it did before.
      + (salvaged && (salvaged.findings || salvaged.report)
        ? ` — content-proven on disk and still usable: ${[salvaged.findings, salvaged.report].filter(Boolean).join(', ')}`
        : ''),
    ]),
    partial: true,
  });

  // absolute outputDir + planned-path stamp BEFORE the outer try so a
  // programming-error / stamp regression throws to runWith ({ ok:false }) instead of
  // being softened into a partial-artifacts gap. No relative fallback — omitting or
  // passing a relative outputDir is the same class of failure as a stamp escape.
  const c = ctx;
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
    // The two flags are read INDEPENDENTLY. `returnPrimaries` used to be consulted only
    // inside the script-path branch, which made `{ returnPrimaries: true }` alone opt a
    // caller silently into the legacy by-value writer — the one path with no content proof
    // at all — while its args said it had opted out of transcription entirely. The return
    // channel has no use for the script: Phase 8's materializer imports the assembler
    // directly, so the path is only ever needed by the executor dispatch on the writer
    // path, and requiring it here would be a dependency that does not exist.
    const wantsReturn = persist.returnPrimaries === true;
    if (scriptPath || wantsReturn) {
      // The guard that keeps BOTH derived paths safe under pathological input. Every
      // projection is by finding id, so a missing/duplicate id — or a delivery/challenge
      // entry that is not a byte-identical twin of its findings.json row — makes the
      // derivation unfaithful. Rather than degrade the run (null paths, no artifacts) we
      // fall back to the legacy full by-value writer and name the reason in a gap.
      const derivable = persistDerivable(inp);
      if (derivable.ok) {
        // The RETURN channel first: it dispatches nothing and is the only path on which
        // no model transcribes the bytes. Its ONE refusal is size (see RETURN_CHAR_BUDGET),
        // and it falls through to a writer rather than degrading the run — the derived one
        // when a script path is in hand, the legacy one otherwise. Either way the fallback
        // says what it costs, because a run that quietly demoted to a transcriber is
        // exactly the failure this channel exists to remove.
        if (wantsReturn) {
          const viaReturn = returnChannelPersist(inp, paths, outputDir, sha);
          if (viaReturn.ok) return viaReturn.result;
          const gap = `writeArtifacts: ${viaReturn.reason} — persisted through the artifact-writer instead, which transcribes the bytes`;
          if (!scriptPath) return await writeArtifactsLegacy(c, inp, paths, outputDir, policy, partial, [gap]);
          const fallback = await writeArtifactsDerived(c, inp, paths, outputDir, sha, policy, partial, scriptPath);
          return { ...fallback, gaps: [gap].concat(fallback.gaps || []) };
        }
        return await writeArtifactsDerived(c, inp, paths, outputDir, sha, policy, partial, scriptPath);
      }
      return await writeArtifactsLegacy(c, inp, paths, outputDir, policy, partial, [
        `writeArtifacts: derived persistence unavailable (${derivable.reason}) — persisted the full by-value payload instead`,
      ]);
    }
    // No persist waist: the documented clean degradation for older callers (bench
    // included). Legacy path, no gap — nothing was lost, only latency.
    return await writeArtifactsLegacy(c, inp, paths, outputDir, policy, partial, []);
  } catch (e) {
    return partial(`persistence threw before any artifact was written (${(e && e.message) || 'unknown'})`, []);
  }
}

// The legacy full by-value persist: ONE artifact-writer dispatch carrying all four
// artifacts. `extraGaps` rides along on both the success and the degradation return so
// an id-integrity fallback is always visible in the envelope.
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
    // check EVERY echoed path field; one gap naming each escape; all-or-nothing
    // null via partial() (same shape as write-proof failure).
    const echoed = result.artifactPaths;
    const escape = pathEscapeReason(
      outputDir,
      ARTIFACT_PATH_KEYS.map((k) => ({
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
    return partial(`writer agent threw (${(e && e.message) || 'unknown'})`, extraGaps);
  }
}

// persistEntries(inp, paths, outputDir, sha) -> { plan, planPath, entries }
//
// The ONE serialization of the three primaries, shared by both channels that persist them:
// the RETURN channel carries these entries home for Phase 8 to write, the derived writer
// path dispatches these same entries to be transcribed. A second copy of this arithmetic
// would be a second spelling of the same three documents, drifting apart the moment either
// side changed — and the plan's `expect[]` proves the primaries by checksum, so a drift is
// a failed content proof on whichever channel did not get the edit.
//
// The plan text is escape-hardened for the WIRE (see hardenEscapeRuns), which is
// load-bearing on the writer path — the plan is transcribed by the same model and carries
// checkpoint prose, and a mangled plan is a HARD failure (bad planChecksum) rather than a
// tolerated mismatch — and dead weight on a channel with no transcriber. It is applied to
// both anyway: hardening cannot move a proof (assemble_artifacts.py recomputes
// planChecksum from the PARSED plan, and `\u005c` parses to the same backslash `\\` does),
// so the cost of one spelling is nothing and the cost of two is a divergence.
function persistEntries(inp, paths, outputDir, sha) {
  const planPath = persistPlanPath(outputDir, sha);
  const { findingsJson, reportMd } = persistPrimaries(inp);
  // Kept as an OBJECT as well as a string: trustAssembleReceipt grades the receipt against
  // the expectations the pipeline itself computed, never against the ones the receipt
  // echoes back at us.
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

// returnChannelPersist(inp, paths, outputDir, sha) -> { ok: true, result } | { ok:false, reason }
//
// The persist path that dispatches NOTHING. It hands the three primaries — findings.json,
// report.md, the persist plan — back to the caller inside the workflow's own return value,
// and Phase 8 (which has Bash) materializes them with scripts/gauntlet/materialize.py.
//
// WHY. Every other path puts the bytes on disk through an artifact-writer agent: a
// language model asked to reproduce ~50 KB of escape-dense JSON verbatim. Measured across
// every recorded run (38 writer journals / 84 artifacts): 26 of 73 attempted writes — 36% —
// failed their own content proof and 12 artifacts were never written at all. Nothing about
// a document predicts it (a 47 KB findings.json with 104 backslashes came back
// byte-perfect; a 6.4 KB zero-backslash .md was truncated), and the worst losses are
// SUMMARIZATION, which no encoding prevents: one checkpoint lost 29,132 chars because the
// writer dropped 11 fields from every finding, another lost 13,008 with its schema intact
// and its prose simply rewritten shorter. Both parse cleanly. Re-encoding (hardenEscapeRuns)
// and re-dispatching were both measured and both failed — the retry corrupted the same
// sites and died at the identical byte offset, twice.
//
// A workflow's return value is serialized by the HARNESS. Measured 2026-07-30 with a
// zero-subagent probe: byte-exact on disk at 200,000 / 500,000 / 4,000,000 chars, in
// 18-122 ms, with no ceiling found (see RETURN_CHAR_BUDGET). The largest run ever recorded
// carries ~66 KB of unique content — 1.6% of the proven-safe size. This already happened by
// accident once: a run whose writer failed and persisted nothing was found with all 58,949
// chars of its pipeline output sitting in that file, recoverable the whole time, because
// the design discarded it on the success path.
//
// THE PROOF IS UNCHANGED AND UNDUPLICATED. The plan's `expect[]` proves the two primaries,
// its `planChecksum` proves itself, and its `derive[]` proves the two projections — the
// same three gradings assemble_artifacts.py already performs, now applied to a
// harness-written copy rather than a model-written one. The materializer reuses that script
// instead of growing a second grader; nothing here recomputes a checksum.
//
// The primaries are the SAME strings the writer path would have been handed, hardening
// included — not by agreement but because persistEntries is the only place either channel
// serializes them.
//
// artifactPaths and `partial: false` are exactly as on the writer path: what changed is who
// writes the bytes, not which four paths the run produces. Nothing is on disk until Phase 8
// materializes — and that is a LOUD absence (the files are simply not there) where the
// writer's failure mode is a silent paraphrase.
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
      // `nonce` is the run's own id from the args waist. It is what lets the materializer
      // find THIS run's output file by content when no task id is in hand — the return
      // arrives inline on a fast run, and the file exists either way.
      persistReturn: { channel: 'return', nonce: inp.nonce || null, planPath, entries },
    },
  };
}

// The derived persist (D3.2): write the three primaries, then derive the two
// projections on disk. Failure at ANY step takes the same partial-artifacts degradation
// as the legacy path — a content-proof MISMATCH is the one exception (see below).
//
// ONE deterministic retry on a STRUCTURAL assemble failure (live smoke
// smoke-20260727-205454-f99d948). On discourse-graphite the writer transcribed
// findings.json with `\"` over-escaped to `\\"`, producing unparseable JSON at line 99;
// assemble_artifacts.py correctly refused to derive anything and the run lost ALL its
// artifacts. The writer is a sampled agent, not a deterministic function — the other two
// runs on the same commit produced parseable JSON — so a second dispatch is a genuinely
// fresh sample rather than a repeat of the same computation. Hence: retry the whole
// derived persist (writer + assembler) exactly once, then degrade.
//
// The boundaries are load-bearing:
//   * ONLY when the assemble script REFUSED (trustAssembleReceipt structural failure:
//     unparseable JSON, missing/duplicate id, bad plan checksum, a derived-document
//     mismatch). A tolerated PRIMARY content-proof mismatch never reaches here — it is a
//     successful persist with a disclosed divergence, and re-rolling it would trade a
//     known-divergent artifact for an unknown one.
//   * A writer throw / null / failed write-proof degrades immediately, as before: the
//     dispatch itself did not complete, so there is nothing the assembler could refuse.
//   * EXACTLY once. `attemptDerivedPersist` is called at most twice from here and calls
//     nothing recursively — there is no loop to unbound.
//   * NEVER fall back to the legacy by-value writer on this path. This was considered and
//     REJECTED: the legacy writer carries NO content proof, so falling back to it converts
//     a visible failure into a silent one. The same smoke run measured the by-value writer
//     diverging from its payload on 3 of 3 runs (16 chars, 8 chars, and the invalid JSON
//     above) — silent corruption is the normal case there, not the exception. Do not
//     "helpfully" add the fallback.
//   * BOTH attempts are disclosed, whichever way the retry lands, so a degraded (or
//     narrowly-rescued) run stays honest about what happened.
async function writeArtifactsDerived(c, inp, paths, outputDir, sha, policy, partial, assembleScriptPath) {
  const { plan, planPath, entries } = persistEntries(inp, paths, outputDir, sha);
  const attempt = () => attemptDerivedPersist(c, entries, planPath, plan, paths, outputDir, policy, assembleScriptPath);

  // The primaries a refused receipt still proved, keyed by artifactPaths name. Only the
  // LAST attempt's proof counts: a retry re-dispatches the writer, which overwrites the
  // files, so the earlier attempt's grading describes bytes that no longer exist.
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

// One derived-persist attempt: dispatch the writer for the three primaries, prove they
// landed, run the assembler, grade the receipt. Returns
//   { ok: true, gaps }                       — persisted (gaps may disclose a tolerated mismatch)
//   { ok: false, retryable, reason }         — failed; `retryable` iff the assemble script
//                                              structurally refused (see the caller).
// Never throws: every dispatch keeps its own try/catch, exactly as before.
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
    return fail(`writer agent threw (${(e && e.message) || 'unknown'})`);
  }
  if (!writerOut) return fail('writer returned null');
  // Write-proof, same threat model as the persist boundary: WRITTEN_SCHEMA declares
  // no `required`, so an empty { written: [] } is schema-valid and a writer under
  // StructuredOutput retry pressure can return one having written nothing. Without this
  // the assembler would then read primaries that never landed.
  const writtenList = Array.isArray(writerOut.written) ? writerOut.written : [];
  // every writer-reported written[] path; one reason naming each escape.
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
    return fail(`assemble executor threw (${(e && e.message) || 'unknown'})`);
  }
  // every receipt-reported path field (verified + written).
  if (receipt && typeof receipt === 'object') {
    const receiptFields = [];
    const verified = Array.isArray(receipt.verified) ? receipt.verified : [];
    const receiptWritten = Array.isArray(receipt.written) ? receipt.written : [];
    verified.forEach((e, i) => receiptFields.push({ name: `verified[${i}]`, path: e && e.path, present: true }));
    receiptWritten.forEach((e, i) => receiptFields.push({ name: `receipt.written[${i}]`, path: e && e.path, present: true }));
    const receiptEscape = pathEscapeReason(outputDir, receiptFields);
    if (receiptEscape) return fail(receiptEscape);
  }
  const trust = trustAssembleReceipt(receipt, paths, plan);
  // The one retryable class: the primaries reached disk (write-proof passed) and the
  // pinned script graded them and refused. A fresh writer sample can fix exactly this.
  // `proven` rides along so a final refusal keeps the primaries the receipt DID prove
  // rather than discarding a byte-perfect report with the corrupt findings — see
  // provenPrimaryPaths.
  if (!trust.ok) return { ...fail(trust.reason, true), proven: provenPrimaryPaths(receipt, paths, plan) };
  return { ok: true, gaps: trust.gaps };
}

// The assemble receipt gate. A STRUCTURAL failure (no receipt, ok:false, a path the
// script never verified/wrote) is untrustworthy persistence -> degrade. A content-proof
// MISMATCH is deliberately NOT a failure: the script derived from what is actually on
// disk and the artifacts are self-consistent with it, so the findings are still
// delivered and the divergence is raised as a LOUD gap instead of costing the run its
// artifacts (never-fabricate cuts both ways — inventing a new way to lose a run is
// exactly as wrong as inventing a success).
//
// A receipt must NEVER be allowed to grade itself. The receipt is a
// self-report relayed by an executor agent, so every claim it makes about what it was
// CHECKING AGAINST is compared here to the value the pipeline computed independently:
//
//   * receipt.planChecksum must equal the plan's own planChecksum. The script recomputes
//     it from the plan it actually read and refuses to run on a mismatch, so a receipt
//     echoing a different value did not execute THIS plan.
//   * each verified entry's expected_chars/expected_checksum must equal the plan's
//     expect[] entry for that path. Otherwise a wholly self-consistent fabricated
//     receipt ("expected 10, got 10, match") passes.
//   * content_proof must agree with the numbers the receipt itself reports, since the
//     script derives it as exactly `chars === expected_chars && checksum ===
//     expected_checksum`. An incoherent receipt is a broken relay, not a proof.
//   * each DERIVED document's reported chars/checksum must equal the plan's `derive[]` entry
//     for that path — the pipeline's own serialization of the document it holds. Path
//     presence proved only that something was written there.
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
  const verified = Array.isArray(receipt.verified) ? receipt.verified : [];
  const written = Array.isArray(receipt.written) ? receipt.written : [];
  const verifiedByPath = new Map(verified.map((e) => [(e && e.path), e]));
  const expectByPath = new Map((plan.expect || []).map((e) => [e.path, e]));
  const writtenPaths = new Set(written.map((e) => (e && e.path)));
  for (const p of [paths.findings, paths.report]) {
    const got = verifiedByPath.get(p);
    if (!got) return { ok: false, reason: `assemble receipt did not verify ${p} (no content proof)` };
    const want = expectByPath.get(p);
    if (!want || got.expected_chars !== want.chars || got.expected_checksum !== want.checksum) {
      return {
        ok: false,
        reason: `assemble receipt checked ${p} against a foreign expectation (receipt says ${got.expected_chars} chars/${got.expected_checksum}, the pipeline handed the writer ${want ? want.chars : 'none'}/${want ? want.checksum : 'none'})`,
      };
    }
    const same = got.chars === want.chars && got.checksum === want.checksum;
    if ((got.content_proof === 'match') !== same) {
      return {
        ok: false,
        reason: `assemble receipt is incoherent for ${p}: content_proof:"${got.content_proof}" contradicts its own chars/checksum (${got.chars}/${got.checksum} vs expected ${want.chars}/${want.checksum})`,
      };
    }
  }
  // The DERIVED documents (the delivered payload). Path presence alone proved nothing about
  // their content, so each is checked against the plan's own `derive` expectation — the
  // chars/checksum the pipeline computed for the document it holds in memory, compared to
  // what the script reports for the bytes it actually wrote.
  //
  // Threat model, stated plainly for whoever reads this next: the plan is on disk, so a
  // BYZANTINE executor can read these numbers and echo them back. This is NOT authentication.
  // It catches a stale/confused/hallucinating executor and a real Python-vs-JS serializer
  // divergence — the same bound trustSlice documents for the verify receipt.
  //
  // Unlike a primary mismatch, this is STRUCTURAL: a primary can still be derived from
  // on-disk truth, but a derived document that disagrees with the pipeline has no other
  // copy to fall back to — the derivation itself is what went wrong.
  //
  // ONE EXCEPTION, and it is not a loophole. Both derived documents are projections of
  // findings.json ALONE, so when findings.json's OWN content proof came back `mismatch` the
  // derived documents are EXPECTED to differ: the script faithfully projected the divergent
  // bytes that are actually on disk. Failing there would convert the deliberately non-fatal
  // primary mismatch into a lost run — inventing a new way to lose a run, which the
  // never-fabricate contract rules out in the same breath as inventing a success. The
  // difference is still reported, as a gap, right beside the primary mismatch that caused it.
  const writtenByPath = new Map(written.map((e) => [(e && e.path), e]));
  const deriveByPath = new Map((plan.derive || []).map((e) => [e.path, e]));
  const findingsProof = verifiedByPath.get(paths.findings);
  const sourceDiverged = !!(findingsProof && findingsProof.content_proof === 'mismatch');
  const derivedGaps = [];
  for (const p of [paths.postReview, paths.checkpoints]) {
    if (!writtenPaths.has(p)) return { ok: false, reason: `assemble receipt did not write ${p} (no write proof)` };
    const want = deriveByPath.get(p);
    if (!want) return { ok: false, reason: `persist plan carries no derived-content expectation for ${p} (no content proof)` };
    const got = writtenByPath.get(p);
    if (got.chars === want.chars && got.checksum === want.checksum) continue;
    const detail = `derived document ${p} does not match the pipeline's own derivation (receipt reports ${got.chars == null ? 'no' : got.chars} chars/${got.checksum == null ? 'no checksum' : got.checksum}, the pipeline derived ${want.chars}/${want.checksum})`;
    if (sourceDiverged) {
      derivedGaps.push(`artifact-content-proof: ${detail} — expected, since ${paths.findings} it was derived from also diverged`);
      continue;
    }
    return { ok: false, reason: `${detail} — the delivered payload is not what this run produced` };
  }
  // Primary mismatches first, then any derived difference they explain — the primary gap
  // ordering is unchanged for every run that has no derived difference.
  const gaps = [];
  for (const e of verified) {
    if (e && e.content_proof === 'mismatch') {
      gaps.push(`artifact-content-proof: ${e.path} bytes on disk differ from the payload handed to the writer (expected ${e.expected_chars} chars/checksum ${e.expected_checksum}, got ${e.chars}/${e.checksum})`);
    }
  }
  gaps.push(...derivedGaps);
  return { ok: true, gaps };
}

// provenPrimaryPaths(receipt, paths, plan) -> the primary paths a REFUSED receipt still
// proves are on disk byte-identical to what the writer was handed.
//
// WHY THIS EXISTS. A structural refusal used to cost the run all four artifactPaths, even
// when most of the persist demonstrably worked. On the 2026-07-30 run that motivated
// hardenEscapeRuns, findings.json was corrupt but report.md was transcribed byte-perfect
// and its content proof MATCHED — and the pipeline threw the path away anyway, so Phase 8
// had nothing to deliver and a completed 11-finding review reached the user as nothing at
// all. SKILL.md's degradation rule already says "deliver whatever artifactPaths.report
// exists"; that branch was simply unreachable, because the paths were unconditionally
// nulled. This makes the documented recovery real.
//
// The grading is EXACTLY trustAssembleReceipt's success-path grading, not a relaxation:
// the receipt must have run this plan (planChecksum), must have checked the path against
// the pipeline's OWN expectation (not a foreign or fabricated one), must report numbers
// equal to it, and must say `match`. A receipt cannot promote a path it did not prove.
//
// It does NOT resurrect the run: `partial` stays true, the failure gap stays, and the
// DERIVED documents are never salvaged — nothing derived them, so there is nothing to
// prove. This is strictly "stop discarding what was proven", not a new success path.
export function provenPrimaryPaths(receipt, paths, plan) {
  if (!receipt || typeof receipt !== 'object') return [];
  if (!plan || receipt.planChecksum !== plan.planChecksum) return [];
  const byPath = new Map((Array.isArray(receipt.verified) ? receipt.verified : []).map((e) => [(e && e.path), e]));
  const expectByPath = new Map((plan.expect || []).map((e) => [e.path, e]));
  const proven = [];
  for (const p of [paths.findings, paths.report]) {
    const got = byPath.get(p);
    const want = expectByPath.get(p);
    if (!got || !want) continue;
    if (got.expected_chars !== want.chars || got.expected_checksum !== want.checksum) continue;
    if (got.chars !== want.chars || got.checksum !== want.checksum) continue;
    if (got.content_proof !== 'match') continue;
    proven.push(p);
  }
  return proven;
}

// Wire format for the writer's by-value payload: the payload is a single JSON line
// at the END of the prompt, prefixed by this marker. The artifact-writer agent (and
// parseWriterPayload) split on the marker to recover the exact object to persist.
const WRITER_PAYLOAD_MARKER = 'PAYLOAD_JSON:';

// parseWriterPayload(prompt) -> the payload object the writer was asked to persist.
// Documents/round-trips the WRITER_PAYLOAD_MARKER wire format (JSON.stringify emits a
// single physical line, so everything after the last marker is the JSON object).
export function parseWriterPayload(prompt) {
  const idx = (prompt || '').lastIndexOf(WRITER_PAYLOAD_MARKER);
  if (idx === -1) return null;
  return JSON.parse(prompt.slice(idx + WRITER_PAYLOAD_MARKER.length));
}

function writeArtifactsPrompt(inp, paths) {
  const payload = JSON.stringify(writerPayload(inp));
  return `Persist these code-gauntlet artifacts to disk exactly as given (the workflow has no disk access). Write the payload's findings (as pretty JSON) to ${paths.findings}, the payload's report (verbatim markdown) to ${paths.report}, the payload's postReview (the pre-selected delivery set, as pretty JSON) to ${paths.postReview}, and the payload's checkpoints (as JSON) to ${paths.checkpoints}. Return { artifactPaths } echoing the paths you wrote. The payload is the single JSON line after the marker below.\n${WRITER_PAYLOAD_MARKER}${payload}`;
}

// The DERIVED writer payload: `[{ path, text }]` — each `text` is written VERBATIM.
// Sending the
// exact string removes every reformatting degree of freedom, which is what makes the
// content-proof checksum meaningful.
function finalArtifactsWriterPrompt(entries) {
  const payload = JSON.stringify(entries);
  return `Persist these code-gauntlet artifacts to disk exactly as given (the workflow has no disk access). For every entry in the payload, write its "text" VERBATIM to its "path" — byte for byte, nothing before it and NOTHING AFTER THE FINAL BYTE (no trailing commentary, no tool-call markup). Do not reformat, re-indent, or re-serialize. Return { written } listing the paths you wrote. The payload is the single JSON line after the marker below.\n${WRITER_PAYLOAD_MARKER}${payload}`;
}

// The assemble receipt shape (scripts/gauntlet/artifacts.py's single stdout line).
const ASSEMBLE_RECEIPT_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    planVersion: { type: 'number' },
    // The plan checksum the script RECOMPUTED from the plan it read; trustAssembleReceipt
    // compares it against the pipeline's own so the receipt cannot grade itself.
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

// The pinned command: a single `python3 <script> --plan <plan>` invocation whose tokens are
// AST-safe (no command substitution, heredocs, env prefix, or
// shell operators), each shellWord-quoted so a path bearing a space stays ONE argv word
// — exactly like verifyCommand.
function assemblePrompt(scriptPath, planPath) {
  return `Run exactly this command, then return its single line of stdout JSON verbatim via the schema:\npython3 ${shellWord(scriptPath)} --plan ${shellWord(planPath)}`;
}
