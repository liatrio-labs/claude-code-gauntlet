import { execFileSync } from 'node:child_process';

// Exercise the shared transport decoder rather than maintaining a second test codec.
export function measurementText(measurement) {
  if (!measurement.encoding) return measurement.text;
  return execFileSync(process.env.PYTHON || 'python3', ['-c',
    'import json,sys; sys.path.insert(0,"scripts"); from gauntlet.measurement import measurement_text; m=json.load(sys.stdin); sys.stdout.write(measurement_text(m["text"], m.get("encoding")))'], {
    input: JSON.stringify(measurement), encoding: 'utf8', cwd: new URL('../../..', import.meta.url), maxBuffer: 64 * 1024 * 1024,
  });
}

export function decodeMeasurement(measurement) { return JSON.parse(measurementText(measurement)); }
