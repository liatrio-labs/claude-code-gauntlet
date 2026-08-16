"""
Tests for the REVIEW.md defaults contract (issue #94).

references/review-md-spec.md carries one canonical, machine-anchored statement of
the built-in confidence/severity defaults, inside an HTML marker comment pair
(`<!-- code-gauntlet-defaults -->` ... `<!-- /code-gauntlet-defaults -->`). This
test extracts the three numbers from that marker and asserts they match the
actual constants in both filter twins — scripts/filter_findings.py and
workflows/src/filterFindings.js. A skew between the doc and either runtime turns
this suite red; it is not prose-matching (the marker is a fixed anchor, not a
sentence the test re-parses), so rewording the surrounding paragraph does not
break it.
"""

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "skills" / "code-gauntlet" / "references" / "review-md-spec.md"
FILTER_PY_PATH = REPO_ROOT / "scripts" / "filter_findings.py"
FILTER_JS_PATH = REPO_ROOT / "workflows" / "src" / "filterFindings.js"

sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _extract_doc_defaults(text):
    m = re.search(
        r"<!--\s*code-gauntlet-defaults\s*-->(.*?)<!--\s*/code-gauntlet-defaults\s*-->",
        text,
        re.DOTALL,
    )
    if not m:
        raise AssertionError(
            "review-md-spec.md is missing the <!-- code-gauntlet-defaults --> "
            "marker block that this test anchors on."
        )
    block = m.group(1)

    def _num(label):
        pattern = r"`" + re.escape(label) + r"`\D*?\*\*(\d+)\*\*"
        found = re.search(pattern, block)
        if not found:
            raise AssertionError(
                f"Could not find a bolded number for `{label}` inside the "
                "code-gauntlet-defaults marker block."
            )
        return int(found.group(1))

    severity = re.search(r"`severity_threshold`\s*\n?\s*\*\*(\w+)\*\*", block)
    if not severity:
        raise AssertionError(
            "Could not find a bolded value for `severity_threshold` inside the "
            "code-gauntlet-defaults marker block."
        )

    return {
        "confidence_threshold_nonsecurity": _num("confidence_threshold"),
        "security_min_confidence": _num("security_min_confidence"),
        "severity_threshold": severity.group(1),
    }


def _extract_py_constants(text):
    def _int_const(name):
        found = re.search(rf"^{re.escape(name)}\s*=\s*(\d+)", text, re.MULTILINE)
        assert found, f"{name} not found in scripts/filter_findings.py"
        return int(found.group(1))

    def _str_const(name):
        found = re.search(rf'^{re.escape(name)}\s*=\s*"(\w+)"', text, re.MULTILINE)
        assert found, f"{name} not found in scripts/filter_findings.py"
        return found.group(1)

    return {
        "confidence_threshold_nonsecurity": _int_const(
            "DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD"
        ),
        "security_min_confidence": _int_const("DEFAULT_SECURITY_MIN_CONFIDENCE"),
        "severity_threshold": _str_const("DEFAULT_SEVERITY_THRESHOLD"),
    }


def _extract_js_constants(text):
    def _int_const(name):
        found = re.search(
            rf"^const\s+{re.escape(name)}\s*=\s*(\d+);", text, re.MULTILINE
        )
        assert found, f"{name} not found in workflows/src/filterFindings.js"
        return int(found.group(1))

    def _str_const(name):
        found = re.search(
            rf"^const\s+{re.escape(name)}\s*=\s*'(\w+)';", text, re.MULTILINE
        )
        assert found, f"{name} not found in workflows/src/filterFindings.js"
        return found.group(1)

    return {
        "confidence_threshold_nonsecurity": _int_const(
            "DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD"
        ),
        "security_min_confidence": _int_const("DEFAULT_SECURITY_MIN_CONFIDENCE"),
        "severity_threshold": _str_const("DEFAULT_SEVERITY_THRESHOLD"),
    }


class TestReviewMdDefaultsContract(unittest.TestCase):
    def setUp(self):
        self.spec_text = SPEC_PATH.read_text()
        self.py_text = FILTER_PY_PATH.read_text()
        self.js_text = FILTER_JS_PATH.read_text()

    def test_doc_numbers_match_python_constants(self):
        doc = _extract_doc_defaults(self.spec_text)
        py = _extract_py_constants(self.py_text)
        self.assertEqual(doc, py)

    def test_doc_numbers_match_js_constants(self):
        doc = _extract_doc_defaults(self.spec_text)
        js = _extract_js_constants(self.js_text)
        self.assertEqual(doc, js)

    def test_python_and_js_constants_agree_with_each_other(self):
        py = _extract_py_constants(self.py_text)
        js = _extract_js_constants(self.js_text)
        self.assertEqual(py, js)


if __name__ == "__main__":
    unittest.main()
