import * as measurement from '../../src/measurementTrace.js';
import { makeFinding } from './pipelineMock.js';

export function largeMeasurement(producer = measurement) {
  const trace = producer.createMeasurementTrace('large');
  const findings = Array.from({ length: 400 }, (_, i) => producer.stampDiscovery(trace,
    makeFinding(`F${i}`, { description: `Defect ${i}: ${'long claim with concrete evidence. '.repeat(35)}`,
      evidence: 'inspected source context '.repeat(200) }),
    { agentType: 'bug-detector', dimensions: ['bug'] }, 0, i));
  for (const stage of ['discover', 'merge', 'verify', 'validate', 'filter', 'admission', 'challenge', 'cite', 'dispose', 'select']) {
    for (const finding of findings) Object.assign(finding, {
      verified: stage !== 'discover', validation: stage, challenge: stage,
      diff_validation: { in_diff: true, reason: `${stage}: ${'resolved context '.repeat(100)}` },
      reference_diagnostics: [{ field: stage, reason: 'diagnostic '.repeat(100), action: 'kept' }],
    });
    producer.recordStage(trace, stage, findings);
  }
  trace.complete = true;
  const envelope = { ok: true, review: 'primary recovery data '.repeat(4000) };
  return { trace, envelope, out: producer.appendMeasurement(envelope, trace) };
}
