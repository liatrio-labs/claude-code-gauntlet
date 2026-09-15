"""Shared report severity normalization and JavaScript-compatible trim alphabet.

This stdlib-only library mirrors renderReport.js::normalizeReportSeverity.
JS_TRIM_CHARS is the single shipped Python twin of JS String.prototype.trim,
used by normalize_report_severity and scripts.resolve_config.one_line.
It has no CLI and emits no receipt.
"""

from collections.abc import Mapping

# One hand-written alphabet mirrors JS String.prototype.trim.
# Shared by normalize_report_severity and scripts/resolve_config.py::one_line.
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
