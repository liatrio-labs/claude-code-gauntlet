// deriveFromPlan.js — an INDEPENDENT reimplementation of a persistPlan's derivation
// rules, deliberately not shared with production code (workflows/src/stages.js), so a
// test using it proves the derivation RULES themselves, not that one function equals
// itself. Mirrors scripts/assemble_artifacts.py's projection-by-id exactly: given the
// bytes that actually landed in findings.json, reconstruct the post-review delivery
// document and the resume checkpoint the SAME way the real assembler would, so a test
// can assert `plan.derive[]`'s pre-computed chars/checksum agree with what an honest
// derivation of the ACTUAL on-disk content produces.
//
// Shared by stages_persist.test.js (the plan/derivation contract itself) and
// stages_delivery.test.js (the #213 replay-belt regression: proving the projected
// post-review document stays stripped even though assemble_artifacts.py derives it
// from findings.json on disk, never from the in-memory postReview array).
export function deriveFromPlan(plan, findingsJson) {
  const source = JSON.parse(findingsJson);
  const byId = new Map(source.map((f) => [f.id, f]));
  const projected = plan.postReview.ids.map((id) => byId.get(id));
  const postReview = plan.postReview.wrapper === null
    ? projected
    : { ...plan.postReview.wrapper, findings: projected };
  const strip = new Set(plan.checkpoint.stripAliasFields);
  const checkpoints = JSON.parse(JSON.stringify(plan.checkpoint.skeleton));
  const challenge = checkpoints.phases && checkpoints.phases.challenge;
  if (challenge) {
    challenge.findings = plan.checkpoint.challengeFindingIds.map((id) => {
      const out = {};
      for (const [k, v] of Object.entries(byId.get(id))) if (!strip.has(k)) out[k] = v;
      return out;
    });
  }
  return { postReview, checkpoints };
}
