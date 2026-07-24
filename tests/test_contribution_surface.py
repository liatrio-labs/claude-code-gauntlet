"""Contribution-surface guards (Issue #30).

The public contribution surface — issue forms, label taxonomy, CONTRIBUTING, the PR
template, SECURITY.md, and the maintainer work-queue standard — drifted behind the
shipped v3 architecture and behind the CI-enforced JS gates. Two failure modes were
silent rather than loud, so they are pinned here:

1. GitHub applies an issue-form label only if the label already exists in the repo,
   so a form referencing an absent label silently no-ops its triage labeling. The
   taxonomy is therefore checked in as `.github/labels.json` and every form label
   must resolve against it.
2. Phase names in `bug_report.yml` are only useful while they match the shipped
   pipeline. README's Architecture section is the source of truth and is compared
   directly rather than re-transcribed.

Stdlib only: CI installs pytest and nothing else, so these tests carry a small
reader for the narrow issue-form YAML subset they inspect (a form's top-level
`labels:` block, and a field's `options:` / `placeholder:` / `required:`).
`check-yaml` in pre-commit already guards actual YAML syntax.
"""

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORMS = REPO / ".github" / "ISSUE_TEMPLATE"

ADVISORY_URL = "https://github.com/liatrio-labs/claude-code-gauntlet/security/advisories/new"

# Requirement 1 of Issue #30: the taxonomy the work-queue and the issue forms depend on.
REQUIRED_TRIAGE_LABELS = {"needs-triage", "work-queue"}
REQUIRED_TOPIC_LABELS = {
    "determinism", "benchmarking", "reliability",
    "bench", "latency", "policy", "tooling", "process", "verify-boundary",
}
REQUIRED_AREA_LABELS = {
    "area:workflows", "area:agents", "area:scripts", "area:skills",
    "area:bench", "area:docs", "area:ci",
}
LABEL_CATEGORIES = {"type", "triage", "topic", "area"}

# Plural area forms are canonical; singular drift in a sibling draft is a bug.
SINGULAR_AREA_DRIFT = re.compile(r"area:(workflow|skill|script|doc)(?![a-z])")


def _read(path):
    return (REPO / path).read_text()


def _form_fields(text):
    """Map field id -> parsed field for one issue form's `body:` items.

    Each body item starts at two-space indentation (`  - type: <kind>`); everything
    up to the next such line belongs to it.
    """
    items = []
    current = None
    for line in text.splitlines():
        match = re.match(r"^  - type: (\S+)", line)
        if match:
            current = {"type": match.group(1), "lines": []}
            items.append(current)
        elif current is not None:
            current["lines"].append(line)

    fields = {}
    for item in items:
        body = "\n".join(item["lines"])
        id_match = re.search(r"^    id: (\S+)", body, re.M)
        if not id_match:
            continue
        options_match = re.search(r"^      options:\n((?:        - .*\n?)+)", body + "\n", re.M)
        options = []
        if options_match:
            options = [
                line.strip()[2:].strip()
                for line in options_match.group(1).splitlines()
                if line.strip()
            ]
        placeholder = re.search(r"^      placeholder: (.*)$", body, re.M)
        fields[id_match.group(1)] = {
            "type": item["type"],
            "options": options,
            "placeholder": placeholder.group(1).strip() if placeholder else None,
            "required": "required: true" in body,
        }
    return fields


def _form_labels(text):
    """The labels a form auto-applies (its top-level `labels:` block)."""
    match = re.search(r"^labels:\n((?:  - .*\n)+)", text, re.M)
    if not match:
        return []
    return [line.strip()[2:].strip() for line in match.group(1).splitlines() if line.strip()]


def _readme_phases():
    """`Phase N — Name` for every phase in README's Architecture list (source of truth)."""
    architecture = _read("README.md").split("\n## Architecture", 1)[1].split("\n## ", 1)[0]
    return [
        f"Phase {number} \u2014 {name}"
        for number, name in re.findall(r"^(\d+)\. \*\*(.+?)\*\* \u2014", architecture, re.M)
    ]


def _labels():
    return json.loads(_read(".github/labels.json"))["labels"]


class TestLabelTaxonomy(unittest.TestCase):
    def test_taxonomy_covers_the_required_label_set(self):
        names = {label["name"] for label in _labels()}
        missing = (REQUIRED_TRIAGE_LABELS | REQUIRED_TOPIC_LABELS | REQUIRED_AREA_LABELS) - names
        self.assertEqual(missing, set(), f"labels.json is missing {sorted(missing)}")

    def test_taxonomy_entries_are_well_formed_and_sorted(self):
        labels = _labels()
        names = [label["name"] for label in labels]
        self.assertEqual(names, sorted(names), "labels.json entries must be name-sorted")
        self.assertEqual(len(names), len(set(names)), "duplicate label names in labels.json")
        for label in labels:
            with self.subTest(label=label.get("name")):
                self.assertRegex(label["color"], r"^[0-9a-f]{6}$",
                                 "color must be 6-digit lowercase hex, no leading '#'")
                self.assertTrue(label["description"].strip(), "description must not be empty")
                self.assertLessEqual(len(label["description"]), 100,
                                     "GitHub truncates label descriptions past 100 characters")
                self.assertIn(label["category"], LABEL_CATEGORIES)

    def test_area_labels_are_plural_canonical(self):
        area = {label["name"] for label in _labels() if label["category"] == "area"}
        self.assertEqual(area, REQUIRED_AREA_LABELS)

    def test_no_singular_area_label_drift_in_contribution_surface(self):
        offenders = {}
        candidates = [
            path for path in list((REPO / ".github").rglob("*")) + list((REPO / "docs").rglob("*"))
            if path.is_file() and path.suffix in {".md", ".yml", ".yaml", ".json"}
        ] + [REPO / "CONTRIBUTING.md", REPO / "README.md"]
        for path in candidates:
            hits = sorted(set(SINGULAR_AREA_DRIFT.findall(path.read_text())))
            if hits:
                offenders[str(path.relative_to(REPO))] = hits
        self.assertEqual(offenders, {}, f"singular area-label drift: {offenders}")


class TestIssueForms(unittest.TestCase):
    def test_every_auto_applied_label_exists_in_the_taxonomy(self):
        known = {label["name"] for label in _labels()}
        for form in sorted(FORMS.glob("*.yml")):
            if form.name == "config.yml":
                continue
            for label in _form_labels(form.read_text()):
                with self.subTest(form=form.name, label=label):
                    # GitHub drops form labels that do not pre-exist, silently.
                    self.assertIn(label, known)

    def test_all_three_forms_request_triage(self):
        for name in ("bug_report.yml", "feature_request.yml", "question.yml"):
            with self.subTest(form=name):
                self.assertIn("needs-triage", _form_labels((FORMS / name).read_text()))

    def test_bug_form_phase_options_match_readme_architecture(self):
        fields = _form_fields((FORMS / "bug_report.yml").read_text())
        options = fields["phase"]["options"]
        self.assertEqual([o for o in options if o.startswith("Phase ")], _readme_phases())

    def test_bug_form_components_include_workflows_and_bench(self):
        options = _form_fields((FORMS / "bug_report.yml").read_text())["phase"]["options"]
        joined = "\n".join(options)
        self.assertIn("workflows/", joined)
        self.assertIn("bench/", joined)

    def test_bug_form_requires_install_method(self):
        field = _form_fields((FORMS / "bug_report.yml").read_text()).get("install_method")
        self.assertIsNotNone(field, "bug_report.yml must ask how the plugin was installed")
        self.assertEqual(field["type"], "dropdown")
        self.assertTrue(field["required"], "install method is a first-order diagnostic")
        options = " ".join(field["options"]).lower()
        for expected in ("marketplace", "--plugin-dir", "clone"):
            with self.subTest(option=expected):
                self.assertIn(expected, options)

    def test_bug_form_plugin_version_placeholder_is_3x(self):
        field = _form_fields((FORMS / "bug_report.yml").read_text())["plugin_version"]
        self.assertIsNotNone(field["placeholder"])
        self.assertRegex(field["placeholder"], r"3\.\d+\.\d+")
        self.assertNotRegex(field["placeholder"], r"\b2\.\d+\.\d+")

    def test_feature_form_area_options_include_workflows_and_bench(self):
        options = "\n".join(_form_fields((FORMS / "feature_request.yml").read_text())["area"]["options"])
        self.assertIn("workflows/", options)
        self.assertIn("bench/", options)

    def test_no_form_points_at_disabled_discussions(self):
        for form in sorted(FORMS.glob("*.yml")):
            with self.subTest(form=form.name):
                self.assertNotRegex(form.read_text(), r"(?i)discussion")

    def test_config_links_the_security_advisory_form(self):
        config = _read(".github/ISSUE_TEMPLATE/config.yml")
        self.assertIn(ADVISORY_URL, config)
        self.assertRegex(config, r"(?i)name:.*security")


class TestSecurityPolicy(unittest.TestCase):
    def test_security_md_exists_with_scope_and_supported_versions(self):
        text = _read("SECURITY.md")
        self.assertIn(ADVISORY_URL, text, "must point at the enabled private advisory form")
        for heading in ("reporting", "supported versions", "scope"):
            with self.subTest(heading=heading):
                self.assertRegex(text, rf"(?im)^#{{2,3}} .*{heading}")
        self.assertRegex(text, r"3\.\d+", "supported versions must name the shipped 3.x line")


class TestContributingDocs(unittest.TestCase):
    def test_contributing_lists_the_ci_enforced_commands_verbatim(self):
        text = _read("CONTRIBUTING.md")
        for command in ("python -m pytest tests/ -q",
                        "node --test workflows/test/*.test.js",
                        "pre-commit run --all-files",
                        "node workflows/build.js"):
            with self.subTest(command=command):
                self.assertIn(command, text)
        self.assertIn("bare directory form is not a valid `node --test` target on node 24", text)
        self.assertIn("workflows/test/tools/record_parity.py", text)

    def test_contributing_describes_the_v3_js_pipeline_areas(self):
        text = _read("CONTRIBUTING.md")
        self.assertIn("workflows/src/", text)
        self.assertIn("`bench/`", text)
        self.assertIn("docs/maintainer-issues.md", text)

    def test_pr_template_covers_the_js_gates(self):
        text = _read(".github/pull_request_template.md")
        for gate in ("node --test workflows/test/*.test.js",
                     "node workflows/build.js",
                     "workflows/test/tools/record_parity.py"):
            with self.subTest(gate=gate):
                self.assertIn(gate, text)

    def test_maintainer_issue_standard_has_the_required_sections(self):
        text = _read("docs/maintainer-issues.md")
        for section in ("Problem / Goal", "Requirements", "Evidence", "Candidate directions",
                        "Out of scope", "Verification"):
            with self.subTest(section=section):
                self.assertRegex(text, rf"(?m)^#{{2,4}} {re.escape(section)}")

    def test_maintainer_issue_standard_defers_tier_definitions_to_the_runbook(self):
        text = _read("docs/maintainer-issues.md")
        for tier in ("suites-only", "smoke", "paired-mini"):
            with self.subTest(tier=tier):
                self.assertIn(tier, text)
        self.assertIn("bench/MEASUREMENT.md", text)

    def test_cspell_covers_the_new_public_docs(self):
        hooks = _read(".pre-commit-config.yaml")
        files_pattern = re.search(r"^        files: \^\((.*)\)\$", hooks, re.M)
        self.assertIsNotNone(files_pattern, "cspell hook must keep an explicit files: scope")
        scope = re.compile(rf"^({files_pattern.group(1)})$")
        for path in ("README.md", "CONTRIBUTING.md", "SECURITY.md", "docs/maintainer-issues.md"):
            with self.subTest(path=path):
                self.assertTrue(scope.match(path), f"cspell scope does not cover {path}")


if __name__ == "__main__":
    unittest.main()
