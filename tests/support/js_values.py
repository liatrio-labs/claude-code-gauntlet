import json
import subprocess
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def js_values() -> dict:
    script = (
        "const t = v => typeof v === 'string' ? v : v.type;"
        "Promise.all([import('./workflows/src/registry.js'), "
        "import('./workflows/src/filterFindings.js')]).then(([m, f]) => "
        "console.log(JSON.stringify({registryProjection: {"
        "propTypes: Object.fromEntries(Object.entries(m.FINDING_PROP_TYPES).map(([k, v]) => [k, t(v)])),"
        "required: m.FINDING_REQUIRED,"
        "dimensions: m.DIMENSIONS.map(d => ({"
        "dimension: d.dimension, agentType: d.agentType,"
        "extras: Object.fromEntries(Object.entries(d.schemaExtra || {}).map(([k, v]) => [k, t(v)])),"
        "requiredExtra: d.requiredExtra || [],"
        "requiredWhenDimension: d.requiredWhenDimension || [],"
        "})),}, FIELD_RENAMES: m.FIELD_RENAMES, "
        "SEVERITY_ORDER: m.SEVERITY_ORDER, REVIEW_DEFAULTS: {"
        "confidence_threshold_nonsecurity: f.FILTER_DEFAULTS.nonsecurityConfidenceThreshold, "
        "security_min_confidence: f.FILTER_DEFAULTS.securityMinConfidence, "
        "severity_threshold: f.FILTER_DEFAULTS.severityThreshold}})))"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    values: dict = json.loads(result.stdout)
    return values
