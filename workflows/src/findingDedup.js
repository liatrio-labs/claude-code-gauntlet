// findingDedup.js — canonical dedup by id. NDJSON (priority 2) beats text (1).
// Single source of truth: reused by mergeFindings and filterFindings.
export function dedupById(ndjsonFindings, textFindings) {
  const seen = new Map(); // id -> { finding, priority }
  let duplicatesResolved = 0;
  let droppedNoId = 0;

  const add = (finding, priority) => {
    const fid = finding.id;
    if (fid === undefined || fid === null) { droppedNoId += 1; return; }
    if (seen.has(fid)) {
      if (priority > seen.get(fid).priority) seen.set(fid, { finding, priority });
      duplicatesResolved += 1;
    } else {
      seen.set(fid, { finding, priority });
    }
  };

  for (const findings of Object.values(textFindings || {})) for (const f of findings) add(f, 1);
  for (const findings of Object.values(ndjsonFindings || {})) for (const f of findings) add(f, 2);

  return { merged: [...seen.values()].map((v) => v.finding), duplicatesResolved, droppedNoId };
}
