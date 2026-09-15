"""Shared severity normalization for the report poster and FIX-task renderer.

This stdlib-only library mirrors renderReport.js::normalizeReportSeverity.
It has no CLI and emits no receipt.
"""

from collections.abc import Mapping

# Keep this hand-written JS trim alphabet in sync with normalizeReportSeverity and
# scripts/resolve_config.py::_JS_TRIM_RE; neither copy is generated.
JS_TRIM_CHARS = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def normalize_report_severity(raw: object, labels: Mapping[str, object]) -> str:
    """Return a JS-trimmed lowercase key of labels, or the literal fallback."""
    # Twin fallback: workflows/src/renderReport.js::normalizeReportSeverity.
    fallback = "low"
    if not isinstance(raw, str):
        return fallback
    normalized = raw.strip(JS_TRIM_CHARS).lower()
    return normalized if normalized in labels else fallback
