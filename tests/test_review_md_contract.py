import re
from pathlib import Path

from tests.support.js_values import js_values

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "skills" / "code-gauntlet" / "references" / "review-md-spec.md"


# Fixed markers let surrounding prose change without weakening the defaults contract.
def _extract_doc_defaults(text: str) -> dict[str, int | str]:
    m = re.search(
        r"<!--\s*code-gauntlet-defaults\s*-->(.*?)<!--\s*/code-gauntlet-defaults\s*-->",
        text,
        re.DOTALL,
    )
    assert m, "missing defaults marker block"
    block = m.group(1)

    def _num(label: str) -> int:
        pattern = r"`" + re.escape(label) + r"`\D*?\*\*(\d+)\*\*"
        found = re.search(pattern, block)
        assert found, f"missing default for {label}"
        return int(found.group(1))

    severity = re.search(r"`severity_threshold`\s*\n?\s*\*\*(\w+)\*\*", block)
    assert severity, "missing severity default"

    return {
        "confidence_threshold_nonsecurity": _num("confidence_threshold"),
        "security_min_confidence": _num("security_min_confidence"),
        "severity_threshold": severity.group(1),
    }


def test_doc_numbers_match_js_constants() -> None:
    doc = _extract_doc_defaults(SPEC_PATH.read_text(encoding="utf-8"))
    assert doc == js_values()["REVIEW_DEFAULTS"]
