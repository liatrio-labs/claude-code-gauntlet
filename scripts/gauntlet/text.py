"""Shared report severity normalization and JavaScript-compatible trim alphabet.

This stdlib-only library mirrors renderReport.js::normalizeReportSeverity.
JS_TRIM_CHARS is the single shipped Python twin of JS String.prototype.trim,
used by normalize_report_severity and gauntlet.config.one_line.
It has no CLI and emits no receipt.
"""

from collections.abc import Mapping

from gauntlet.registry import JS_TRIM_CHARS


def normalize_report_severity(raw: object, labels: Mapping[str, object]) -> str:
    """Return a JS-trimmed lowercase key of labels, or the literal fallback."""
    # Twin fallback: workflows/src/renderReport.js::normalizeReportSeverity.
    fallback = "low"
    if not isinstance(raw, str):
        return fallback
    normalized = raw.strip(JS_TRIM_CHARS).lower()
    return normalized if normalized in labels else fallback
