import { deepClone, firstUnsafeNumber, fnv1a32, normalizeForChecksum } from './wire.js';

// The persisted findings must satisfy BOTH downstream boundaries: verify_findings.py
// reads canonical names (file/line_start/line_end/description...) and the retained
// post_review.py INDEXES the v2 names f["file"]/f["line"] and reads body/end_line.
// So at the persist boundary each finding carries the v2 aliases ALONGSIDE the
// canonical fields (a union schema): line<-line_start, end_line<-line_end,
// body<-description. Existing v2 keys are never overwritten.
function toV2Aliased(f) {
  const out = { ...f };
  if (out.line === undefined && out.line_start !== undefined) out.line = out.line_start;
  if (out.end_line === undefined && out.line_end !== undefined) out.end_line = out.line_end;
  if (out.body === undefined && out.description !== undefined) out.body = out.description;
  return out;
}

export function postReviewWrapper(id, reviewBody) {
  return {
    owner: id.owner,
    repo: id.repo,
    pr_number: id.pr_number,
    sha: id.sha_full,
    platform: id.platform,
    review_body: typeof reviewBody === 'string' ? reviewBody : '',
  };
}

// The by-value payload the writer agent persists. findings/postReview are aliased to the
// union schema so the persisted JSON is consumable by BOTH boundary scripts unchanged.
// `postReview` is the deterministic delivery set (selectDelivery output): every
// challenge-survivor, ranked and capped, each carrying its report_tag — persisted so Phase 8
// posts it verbatim without re-selecting. The pipeline-degraded `unverified` bucket is NOT
// carried here: it is persisted to no file (findings.json is the high-confidence set only),
// the report already renders it, and the slimmed checkpoint's challenge entry carries it for
// resume — so re-sending it in the writer prompt was dead by-value weight (each finding-scale
// piece now crosses the writer prompt exactly once). Pure + exported so tests (and the node
// recorder) can assert the persist output is REAL pipeline output, not a hand-authored fixture.
export function writerPayload(inp) {
  const postReviewSet = (inp.postReview || []).map(toV2Aliased);
  const id = inp.prIdentity;
  return {
    findings: (inp.findings || []).map(toV2Aliased),
    // With a PR identity (delivery.prIdentity, live-run L3) the persisted post-review
    // artifact IS the post_review.py input wrapper — Phase 8 posts it without hand-
    // assembling { owner, repo, pr_number, ... } around a bare array (the wrap was
    // documented but got reverse-engineered anyway, ~8 turns in the PR-310 run).
    // review_body is the renderer-owned Summary body. sha pins the reviewed commit, and
    // platform carries the already-resolved target instead of guessing from a remote.
    // The findings SET is byte-identical either way; only the envelope changes.
    postReview: id
      ? { ...postReviewWrapper(id, inp.reviewBody), findings: postReviewSet }
      : postReviewSet,
    report: inp.report || '',
    checkpoints: inp.checkpoints || {},
  };
}

// --- Derived persistence (D3) -------------------------------------

// The v2 aliases toV2Aliased ADDS at the persist boundary. The checkpoint's
// challenge findings are the alias-stripped twin of findings.json, so removing
// exactly these keys restores the canonical shape (and, because the aliases are
// appended LAST, the key order too).
const PERSIST_ALIAS_FIELDS = ['line', 'end_line', 'body'];

function stripPersistAliases(f) {
  const out = {};
  for (const [k, v] of Object.entries(f)) {
    if (!PERSIST_ALIAS_FIELDS.includes(k)) out[k] = v;
  }
  return out;
}

// hardenEscapeRuns(json) -> the same JSON document with every escaped backslash (`\\`)
// respelled as `\u005c`, so the text carries NO run of two or more consecutive
// backslashes — whatever the data says.
//
// WHY (measured, run wf_adc1a803-912, 2026-07-30 — the run that lost every artifact).
// The artifact-writer is a model, and models do not transcribe backslash runs. A
// challenger's prose contained ONE literal backslash (it wrote `\"the executor's
// receipt…\"`); that backslash is escaped TWICE on the way to the writer — once by
// findings.json's own serialization, once by the PAYLOAD_JSON wire line — so one
// backslash arrives as a run of SEVEN. Counting runs in that run's payload against the
// bytes the writer actually wrote:
//
//     run length in findings.json               count   transcribed correctly
//     1  (\n, \")                                  53    53
//     2                                             2     2
//     3  (\\\" — a literal \ ahead of a quote)     18     0   <- every one collapsed
//
// The first collapse produced `\\"` = escaped-backslash + an UNTERMINATED string, so
// assemble_artifacts.py refused the document (correctly — unparseable JSON is
// structural) and the run degraded with all four artifactPaths null. This is NOT
// sampling drift: the retry, a fresh sample, corrupted the same 18 sites and the parse
// failed at the identical byte offset. A second dispatch cannot fix a deterministic
// failure, so the WIRE has to stop producing the input that triggers it.
//
// `\u005c` is one backslash followed by the letters u005c, so afterwards the longest run in
// the document is 1, and in the writer's PAYLOAD_JSON line 3 — the everyday spelling of
// an escaped quote, which the same run transcribed 45 times without an error. The bound
// is DATA-INDEPENDENT: k literal backslashes become k separate `\u005c` escapes rather
// than one run of 2k. That is the durable property; the run-length table above is only
// how it was found.
//
// Transparent to both consumers, which is why no Python change pairs with this:
//   * `\u005c` is a standard JSON escape, so json.loads yields the identical value and
//     the documents assemble_artifacts.py derives out of findings.json are unchanged.
//   * the primaries are proven on RAW BYTES and persistPrimaries returns the hardened
//     string, so the checksum both runtimes compare is over the same text.
//   * the plan's proof is VALUE-level (parse, drop planChecksum, re-serialize), so
//     hardening the plan's emitted bytes cannot move its checksum.
//
// Correctness of the scan: in JSON.stringify output a backslash only ever BEGINS an
// escape, and `\\` is the only escape whose payload is itself a backslash. A
// left-to-right non-overlapping replace therefore consumes whole escapes and can never
// split one — `\\\"` becomes `\u005c\"`, never `\u005c` plus a stray quote. Do not
// "simplify" this to a per-character scan of the parsed values; the transform is defined
// over serializer OUTPUT, which is what the writer has to reproduce.
export function hardenEscapeRuns(json) {
  return typeof json === 'string' ? json.replace(/\\\\/g, '\\u005c') : json;
}

// The two PRIMARY strings — the only genuinely unique content the writer persists.
// Pure and exported so a test can assert the writer prompt carries them verbatim.
// findingsJson is escape-hardened (see hardenEscapeRuns) BEFORE it is returned, so the
// string the writer is handed, the string persistPlan checksums, and the bytes
// assemble_artifacts.py reads back are all one and the same. reportMd is markdown, not
// JSON — a mangled backslash there is a tolerated content-proof mismatch, not a parse
// failure, so it is deliberately left alone rather than given a format it does not have.
export function persistPrimaries(inp) {
  return {
    findingsJson: hardenEscapeRuns(JSON.stringify(((inp || {}).findings || []).map(toV2Aliased), null, 2)),
    reportMd: (inp || {}).report || '',
  };
}

// persistPlan(inp, paths) -> the plan scripts/gauntlet/artifacts.py consumes.
// PURE (deep-clones the checkpoint before emptying the challenge findings), and
// exported so the projection rules are directly unit-testable — the in-run
// byte-identity test applies them to the primaries and asserts the result equals
// writerPayload(inp).postReview / .checkpoints.
//
// The skeleton is derived FROM slimPersistedCheckpoints' output by EMPTYING (not
// deleting) phases.challenge.findings, so there is exactly one source of truth for the
// checkpoint shape and the key keeps its position — the derived document is
// key-order-identical to the one the pipeline holds in memory.
//
// THE PLAN'S SELF-PROOF (`planChecksum`). The plan is transcribed to
// disk by the artifact-writer agent exactly like findings.json and report.md — but
// unlike them it is not data to be checked, it is the INSTRUCTION SET. `postReview.ids`
// is the sole authority for which finding ids reach the delivered post-review artifact,
// so a writer that elides two entries from that list produces a silently smaller
// delivered set with a self-consistent ok:true receipt and no gap. The two primaries had
// expect[].chars/checksum to prove them; the plan had nothing. So it now carries a
// checksum of itself, computed over the plan MINUS that field:
//
//   planChecksum = fnv1a32(JSON.stringify(planWithoutPlanChecksum, null, 2))
//
// Unambiguous in both runtimes because (a) the field is appended LAST, so deleting it
// on the Python side restores this exact object (JS orders array-index keys first, and
// the Python serializer applies the same order), and (b) the serializer is the same pretty
// printer that produces the derived artifacts — which makes the plan checksum a canary
// for serializer divergence too: a Python/JS spelling difference over the plan's content
// fails the proof before a divergent artifact is written.
//
// THE DERIVED DOCUMENTS' CONTENT PROOF (`derive`). The two
// primaries are proven by `expect[].chars/checksum`; the two DERIVED documents —
// post-review.json and checkpoint-all.json, i.e. the payload that actually reaches the user
// — were gated on PATH PRESENCE only ("the script says it wrote something there"). So the
// plan also carries a { path, chars, checksum } expectation for each derived document,
// computed here from writerPayload(inp).postReview / .checkpoints through the SAME
// normalizeForChecksum + fnv1a32 the primaries use. It costs nothing at dispatch (~40 bytes
// each; the documents themselves are still never sent) and it makes the byte-identity claim
// PROVABLE for the delivered payload rather than merely argued.
//
// WHAT THIS IS NOT: authentication. The plan is on disk and the executor can read it, so a
// BYZANTINE executor could echo back any value the plan names — exactly the threat model
// trustSlice already documents for the verify receipt. This is a consistency/liveness check
// against a STALE, CONFUSED or HALLUCINATING executor (a receipt from another run, a
// half-finished derivation, an invented success), and against a real serializer divergence
// between the two runtimes. Do not build a security argument on top of it.
//
// WHY A DERIVED MISMATCH IS FATAL WHERE A PRIMARY MISMATCH IS NOT: a primary mismatch still
// leaves on-disk truth to derive from, so the run keeps its artifacts and raises a loud gap.
// A derived mismatch means the DERIVATION disagreed with the pipeline — there is no other
// copy of that document to fall back to — so trustAssembleReceipt treats it as structural.
//
// WHY THE ID LISTS STAY (the weak point is transcription, not encoding). No wire format
// removes the failure mode: the plan must cross an LLM agent's transcription to reach
// disk (the sandbox has no disk access at all), and no encoding stops an agent from
// writing fewer bytes than it was handed. The alternatives are strictly worse — deriving
// the delivery set on the Python side means reimplementing selectDelivery's ranking and
// cap in a second language, i.e. an order-sensitive stage whose divergence would silently
// change the delivered findings; and "all of findings.json, in order" is simply false,
// delivery is a ranked capped subset. So the list stays explicit and gains a proof.
export function persistPlan(inp, paths) {
  const { findingsJson, reportMd } = persistPrimaries(inp);
  const id = inp.prIdentity;
  const skeleton = deepClone(inp.checkpoints || {});
  const challenge = (skeleton.phases || {}).challenge;
  const challengeFindingIds = challenge && Array.isArray(challenge.findings)
    ? challenge.findings.map((f) => f && f.id)
    : [];
  if (challenge && Array.isArray(challenge.findings)) challenge.findings = [];
  const expectOf = (path, text) => {
    const normalized = normalizeForChecksum(text);
    return { path, chars: normalized.length, checksum: fnv1a32(normalized) };
  };
  // The derived documents, as the pipeline itself would serialize them — the same pretty
  // printer that js_stringify_pretty in scripts/gauntlet/jsjson.py reproduces, and the same source
  // (writerPayload) the legacy by-value path persists. Only their chars/checksum travel.
  const held = writerPayload(inp || {});
  const plan = {
    planVersion: 2,
    expect: [expectOf(paths.findings, findingsJson), expectOf(paths.report, reportMd)],
    derive: [
      expectOf(paths.postReview, JSON.stringify(held.postReview, null, 2)),
      expectOf(paths.checkpoints, JSON.stringify(held.checkpoints, null, 2)),
    ],
    postReview: {
      path: paths.postReview,
      source: paths.findings,
      ids: (inp.postReview || []).map((f) => f && f.id),
      // Same envelope decision and helper as writerPayload: with a PR identity the
      // artifact is the post_review.py input wrapper; otherwise it is a bare array.
      wrapper: id ? postReviewWrapper(id, inp.reviewBody) : null,
    },
    checkpoint: {
      path: paths.checkpoints,
      source: paths.findings,
      challengeFindingIds,
      stripAliasFields: PERSIST_ALIAS_FIELDS,
      skeleton,
    },
  };
  // Appended LAST and computed over the object WITHOUT it — see the construction note
  // above. assemble_artifacts.py deletes exactly this key and recomputes.
  plan.planChecksum = fnv1a32(JSON.stringify(plan, null, 2));
  return plan;
}

// persistDerivable(inp) -> { ok } | { ok:false, reason }
// The guard on the derived path. Every projection is BY FINDING ID out of
// findings.json, so the derivation is only faithful when:
//   0. every number in the persisted content is a JS-safe integer (see
//      firstUnsafeNumber — otherwise the Python serializer spells it differently);
//   1. every finding carries a unique, non-empty string id;
//   2. every postReview / challenge entry has a twin in the findings set; and
//   3. that twin is byte-identical to it under the projection rules — including the
//      alias round trip, because toV2Aliased only ADDS an alias when absent, so a
//      finding that ALREADY carries `line`/`end_line`/`body` would silently lose it to
//      the checkpoint's alias strip.
// A refusal is NOT a degradation: writeArtifacts falls back to the legacy full
// by-value writer and records the reason as a gap. Pure and exported for direct tests.
export function persistDerivable(inp) {
  // Everything the derived documents are built from: findings.json's content, the
  // checkpoint skeleton, and the post-review envelope's pr_number.
  for (const [label, value] of [
    ['findings', (inp || {}).findings],
    ['postReview', (inp || {}).postReview],
    ['checkpoints', (inp || {}).checkpoints],
    ['prIdentity', (inp || {}).prIdentity],
  ]) {
    if (value === undefined || value === null) continue;
    const bad = firstUnsafeNumber(value, label);
    if (bad !== null) {
      return { ok: false, reason: `${bad} is not a JS-safe integer — the Python assembler cannot spell it byte-identically, so the derived artifact would diverge` };
    }
  }
  const findings = (inp || {}).findings || [];
  const byId = new Map();
  for (let i = 0; i < findings.length; i += 1) {
    const f = findings[i];
    const fid = f && f.id;
    if (typeof fid !== 'string' || fid === '') {
      return { ok: false, reason: `finding at index ${i} has no usable string id` };
    }
    if (byId.has(fid)) return { ok: false, reason: `duplicate finding id ${fid}` };
    byId.set(fid, f);
    if (JSON.stringify(stripPersistAliases(toV2Aliased(f))) !== JSON.stringify(f)) {
      return { ok: false, reason: `finding ${fid} already carries a v2 alias field (${PERSIST_ALIAS_FIELDS.join('/')}) — the checkpoint alias strip is not reversible` };
    }
  }
  const twinCheck = (list, label, project) => {
    for (let i = 0; i < list.length; i += 1) {
      const entry = list[i];
      const fid = entry && entry.id;
      if (typeof fid !== 'string' || !byId.has(fid)) {
        return `${label} entry at index ${i} (id ${fid === undefined ? 'missing' : fid}) is not present in the findings set`;
      }
      if (JSON.stringify(project(entry)) !== JSON.stringify(project(byId.get(fid)))) {
        return `${label} entry ${fid} differs from the findings entry with the same id`;
      }
    }
    return null;
  };
  const postReviewBad = twinCheck(inp.postReview || [], 'postReview', toV2Aliased);
  if (postReviewBad) return { ok: false, reason: postReviewBad };
  const challenge = ((inp.checkpoints || {}).phases || {}).challenge;
  const challengeFindings = challenge && Array.isArray(challenge.findings) ? challenge.findings : [];
  const challengeBad = twinCheck(challengeFindings, 'checkpoint challenge', (f) => f);
  if (challengeBad) return { ok: false, reason: challengeBad };
  return { ok: true };
}
