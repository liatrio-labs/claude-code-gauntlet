// Independent projection-by-id keeps byte proofs separate from the production serializer.
// Shared by persistPlan.test.js and the delivery projection wiring rows.
export function deriveFromPlan(plan, findingsJson) {
  const source = JSON.parse(findingsJson);
  const byId = new Map(source.map((f) => [f.id, f]));
  const projected = plan.postReview.ids.map((id) => byId.get(id));
  const postReview = plan.postReview.wrapper === null
    ? projected
    : { ...plan.postReview.wrapper, findings: projected };
  const strip = new Set(plan.checkpoint.stripAliasFields);
  const checkpoints = JSON.parse(JSON.stringify(plan.checkpoint.skeleton));
  if (plan.planVersion === 4) {
    for (const { at, id, keys, overrides } of plan.checkpoint.projections) {
      const finding = Object.fromEntries(keys.map((key) => [key,
        Object.hasOwn(overrides, key) ? overrides[key] : byId.get(id)[key]]));
      const parent = at.slice(0, -1).reduce((node, key) => node[key], checkpoints);
      parent[at.at(-1)] = finding;
    }
    return { postReview, checkpoints };
  }
  if (plan.checkpoint.mode === 'literal') return { postReview, checkpoints };
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
