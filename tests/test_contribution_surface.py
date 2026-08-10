"""Contribution-surface guards (Issue #30).

The public contribution surface — issue forms, label taxonomy, CONTRIBUTING, the PR
template, SECURITY.md, and the maintainer work-queue standard — drifted behind the
shipped v3 architecture and behind the CI-enforced JS gates. Three failure modes here
are silent rather than loud, so they are pinned:

1. GitHub applies an issue-form label only if the label already exists in the repo, so
   a form referencing an absent label silently no-ops its triage labeling. The taxonomy
   is checked in as `.github/labels.json` and every form label must resolve against it.
2. A form that violates GitHub's issue-form schema — a duplicated dropdown option, a
   misplaced `validations` block — fails to render, which is a public-facing outage with
   no local signal. `check-yaml` validates YAML syntax only and knows nothing of the
   form schema, so the schema constraints are asserted here.
3. Phase names in `bug_report.yml` are only useful while they match the shipped
   pipeline. README's Architecture section is the source of truth and is compared
   directly rather than re-transcribed.

Stdlib only: CI installs pytest and nothing else, so these tests carry a small reader
for the narrow issue-form YAML subset they inspect. The reader is deliberately strict —
it raises on shapes it does not understand rather than returning an empty result that
would make a test vacuously pass.
"""

import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORMS = REPO / ".github" / "ISSUE_TEMPLATE"
LABELS_DIFF = REPO / ".github" / "labels_diff.py"

ADVISORY_URL = (
    "https://github.com/liatrio-labs/claude-code-gauntlet/security/advisories/new"
)

# Requirement 1 of Issue #30: the taxonomy the work-queue and the issue forms depend on.
REQUIRED_TRIAGE_LABELS = {"needs-triage", "work-queue"}
REQUIRED_TOPIC_LABELS = {
    "determinism",
    "benchmarking",
    "reliability",
    "bench",
    "latency",
    "policy",
    "tooling",
    "process",
    "verify-boundary",
}
REQUIRED_AREA_LABELS = {
    "area:workflows",
    "area:agents",
    "area:scripts",
    "area:skills",
    "area:bench",
    "area:docs",
    "area:ci",
}
LABEL_CATEGORIES = {"type", "state", "resolution", "outreach", "topic", "area"}

# Plural area forms are canonical; singular drift in a sibling draft is a bug.
SINGULAR_AREA_DRIFT = re.compile(r"area:(workflow|skill|script|doc|agent)(?![a-z])")

# GitHub's issue-form schema (docs.github.com "Syntax for issue forms").
FORM_TOP_LEVEL_KEYS = {
    "name",
    "description",
    "title",
    "labels",
    "assignees",
    "projects",
    "type",
    "body",
}
FORM_FIELD_TYPES = {"markdown", "input", "textarea", "dropdown", "checkboxes"}
FORM_ITEM_KEYS = {"type", "id", "attributes", "validations"}
FORM_TOP_LEVEL_REQUIRED = {"name", "description", "body"}
# Per-type attributes GitHub accepts. An unknown one is a validation error, not a
# silently ignored extra.
FORM_ATTRIBUTES = {
    "markdown": {"value"},
    "input": {"label", "description", "placeholder", "value"},
    "textarea": {"label", "description", "placeholder", "value", "render"},
    "dropdown": {"label", "description", "multiple", "options", "default"},
    "checkboxes": {"label", "description", "options"},
}
# `markdown` is display-only: it takes no id and cannot be required.
FORM_TYPES_WITHOUT_VALIDATIONS = {"markdown"}
FORM_TYPES_WITH_OPTIONS = {"dropdown", "checkboxes"}
# checkboxes options are mappings (`- label: ...`), not bare scalars.
FORM_TYPES_WITH_MAPPING_OPTIONS = {"checkboxes"}
# GitHub rejects these as dropdown options: empty, or a value YAML would read as a bool
# or as the reserved "None".
RESERVED_OPTIONS = {"none", "true", "false", "yes", "no", "on", "off"}


def _read(path):
    return (REPO / path).read_text(encoding="utf-8")


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _form_files():
    """Every issue form, excluding the config. Both YAML extensions GitHub accepts."""
    return sorted(
        p
        for p in list(FORMS.glob("*.yml")) + list(FORMS.glob("*.yaml"))
        if p.name not in {"config.yml", "config.yaml"}
    )


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _sequence_after(lines, start, indent):
    """Items of a block sequence whose dashes sit at `indent`, beginning at `lines[start]`.

    A sequence of mappings indents its continuation keys past the dash — a checkboxes
    option's `required:` is the case here — so those lines are skipped rather than
    ending the sequence. Getting that order wrong truncates the list silently, which is
    worse than reading nothing.
    """
    items = []
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent(line) > indent:
            continue  # continuation of the previous item, not the end of the sequence
        if _indent(line) < indent or not line.lstrip(" ").startswith("- "):
            break
        items.append(_unquote(line.lstrip(" ")[2:]))
    return items


def _form_labels(text):
    """The labels a form auto-applies, from block or flow sequence form.

    Raises rather than returning [] for an unrecognized shape: a silently empty result
    would make the "every form label exists" guard pass without checking anything.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("labels:"):
            continue
        inline = line[len("labels:") :].strip()
        if inline.startswith("[") and inline.endswith("]"):
            return [_unquote(part) for part in inline[1:-1].split(",") if part.strip()]
        if inline:
            raise ValueError(f"unsupported labels: scalar {inline!r}")
        items = _sequence_after(lines, index + 1, indent=2)
        if not items:
            raise ValueError("labels: block sequence is empty or unreadable")
        return items
    return []


def _single_key(block, name):
    """The value of a body item's own `name:` key, wherever it sits in the item.

    GitHub does not impose key order, so scanning the whole item avoids rejecting a form
    that renders fine. Two of the same key is a duplicate-key bug worth raising on.
    """
    found = [line for line in block if re.match(rf"^    {name}:", line)]
    if len(found) > 1:
        raise ValueError(f"duplicate {name}: keys in one body item: {found}")
    return _unquote(found[0].split(":", 1)[1]) if found else None


def _form_fields(text):
    """Parse one issue form's `body:` items into a list of field dicts.

    Each body item starts at two-space indentation (`  - type: <kind>`); everything up
    to the next such line belongs to it.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^  - \w+:", line)]
    fields = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = lines[start:end]
        # Normalize the leading `- ` so the item's own keys all sit at four spaces.
        block[0] = "    " + block[0].lstrip(" ")[2:]

        item_keys, attributes, options, validations = [], {}, None, {}
        section = None
        for index, line in enumerate(block):
            if not line.strip():
                continue
            key_match = re.match(r"^    (\w+):(.*)$", line)
            if key_match:
                item_keys.append(key_match.group(1))
                section = key_match.group(1)
                continue
            sub_match = re.match(r"^      (\w+):(.*)$", line)
            if sub_match and section == "attributes":
                name, inline = sub_match.group(1), sub_match.group(2).strip()
                attributes[name] = _unquote(inline) if inline else None
                if name == "options" and not inline:
                    options = _sequence_after(block, index + 1, indent=8)
            elif sub_match and section == "validations":
                validations[sub_match.group(1)] = sub_match.group(2).strip()

        field_type = _single_key(block, "type")
        if field_type in FORM_TYPES_WITH_MAPPING_OPTIONS and options is not None:
            # A checkboxes option is a mapping, so the raw item reads `label: <text>`.
            # Carrying that prefix into the option value makes every emptiness and
            # reserved-word assertion unfalsifiable, so the label text is extracted here.
            options = [re.sub(r"^label:\s*", "", option) for option in options]
        fields.append(
            {
                "type": field_type,
                "id": _single_key(block, "id"),
                "keys": item_keys,
                "attributes": attributes,
                "options": options,
                # `required` counts only inside a `validations:` block that is a sibling of
                # `attributes:`. GitHub rejects a nested one, and prose in a `description:`
                # must never read as a requirement.
                "required": validations.get("required") == "true",
            }
        )
    return fields


def _field(form_name, field_id):
    form = next(path for path in _form_files() if path.stem == Path(form_name).stem)
    for field in _form_fields(form.read_text(encoding="utf-8")):
        if field["id"] == field_id:
            return field
    return None


def _contact_links(text):
    """`config.yml` contact_links as (name, url) pairs, ignoring comments."""
    links, current = [], None
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        if re.match(r"^  - name:", line):
            current = {"name": _unquote(line.split(":", 1)[1])}
            links.append(current)
        elif current is not None:
            match = re.match(r"^    (\w+):(.*)$", line)
            if match:
                current[match.group(1)] = _unquote(match.group(2))
    return [(link.get("name", ""), link.get("url", "")) for link in links]


def _readme_phases():
    """`Phase N — Name` for every phase in README's Architecture list (source of truth)."""
    architecture = (
        _read("README.md").split("\n## Architecture", 1)[1].split("\n## ", 1)[0]
    )
    phases = [
        f"Phase {number} \u2014 {name}"
        for number, name in re.findall(
            r"^(\d+)\. \*\*(.+?)\*\* \u2014", architecture, re.M
        )
    ]
    if not phases:
        raise ValueError(
            "README Architecture section no longer lists phases as `N. **Name** —`"
        )
    return phases


def _labels():
    return json.loads(_read(".github/labels.json"))["labels"]


class TestLabelTaxonomy(unittest.TestCase):
    def test_taxonomy_covers_the_required_label_set(self):
        names = {label["name"] for label in _labels()}
        missing = (
            REQUIRED_TRIAGE_LABELS | REQUIRED_TOPIC_LABELS | REQUIRED_AREA_LABELS
        ) - names
        self.assertEqual(missing, set(), f"labels.json is missing {sorted(missing)}")

    def test_taxonomy_entries_are_well_formed_and_sorted(self):
        labels = _labels()
        names = [label["name"] for label in labels]
        self.assertEqual(
            names, sorted(names), "labels.json entries must be name-sorted"
        )
        self.assertEqual(
            len(names), len(set(names)), "duplicate label names in labels.json"
        )
        for label in labels:
            with self.subTest(label=label.get("name")):
                self.assertEqual(
                    set(label), {"name", "color", "description", "category"}
                )
                self.assertRegex(
                    label["color"],
                    r"^[0-9a-f]{6}$",
                    "color must be 6-digit lowercase hex, no leading '#'",
                )
                self.assertTrue(
                    label["description"].strip(), "description must not be empty"
                )
                # The labels API rejects a description over 100 characters (422); it does
                # not truncate, so `gh label create` fails outright.
                self.assertLessEqual(len(label["description"]), 100)
                self.assertIn(label["category"], LABEL_CATEGORIES)
                # A leading dash would be parsed as a flag by the documented sync recipe.
                self.assertFalse(
                    label["name"].startswith("-"), "label name must not start with '-'"
                )
                self.assertFalse(label["description"].startswith("-"))

    def test_area_labels_are_plural_canonical(self):
        area = {label["name"] for label in _labels() if label["category"] == "area"}
        self.assertEqual(area, REQUIRED_AREA_LABELS)

    def test_no_singular_area_label_drift_in_contribution_surface(self):
        offenders = {}
        candidates = [
            path
            for path in list((REPO / ".github").rglob("*"))
            + list((REPO / "docs").rglob("*"))
            if path.is_file() and path.suffix in {".md", ".yml", ".yaml", ".json"}
        ] + [
            REPO / name
            for name in (
                "CONTRIBUTING.md",
                "README.md",
                "SECURITY.md",
                "CLAUDE.md",
                "AGENTS.md",
            )
        ]
        for path in candidates:
            hits = sorted(
                set(SINGULAR_AREA_DRIFT.findall(path.read_text(encoding="utf-8")))
            )
            if hits:
                offenders[str(path.relative_to(REPO))] = hits
        self.assertEqual(offenders, {}, f"singular area-label drift: {offenders}")


class TestLabelsDiffHelper(unittest.TestCase):
    """`.github/labels_diff.py` is what makes the manifest verifiable instead of
    aspirational, so its own behavior is pinned here."""

    def _run(self, *args, live=None):
        with tempfile.TemporaryDirectory() as tmp:
            argv = [sys.executable, str(LABELS_DIFF), *args]
            if live is not None:
                path = Path(tmp) / "live.json"
                path.write_text(json.dumps(live), encoding="utf-8")
                argv += ["--live", str(path)]
            return subprocess.run(argv, cwd=REPO, capture_output=True, text=True)

    @staticmethod
    def _as_live(labels):
        return [
            {
                "name": label["name"],
                "color": label["color"].upper(),
                "description": label["description"],
            }
            for label in labels
        ]

    def test_commands_cover_every_manifest_label_and_survive_the_shell(self):
        result = self._run("--commands")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), len(_labels()))
        emitted = {}
        for line in lines:
            argv = shlex.split(line)
            self.assertEqual(argv[:3], ["gh", "label", "create"])
            self.assertIn("--force", argv)
            emitted[argv[3]] = argv[argv.index("--description") + 1]
        # Names with spaces and descriptions with apostrophes must round-trip intact.
        for label in _labels():
            with self.subTest(label=label["name"]):
                self.assertEqual(emitted[label["name"]], label["description"])

    def test_in_sync_repo_reports_no_drift(self):
        # Live colors come back from the API upper-cased; that is not drift.
        result = self._run(live=self._as_live(_labels()))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("in sync", result.stdout)

    def test_missing_and_diverging_labels_fail_loudly(self):
        live = self._as_live(_labels())
        dropped = live.pop(0)["name"]
        live[0]["description"] = "stale description"
        changed = live[0]["name"]
        live.append(
            {"name": "hand-made", "color": "ffffff", "description": "not managed here"}
        )

        result = self._run(live=live)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"missing: {dropped}", result.stdout)
        self.assertIn(f"diverging: {changed}", result.stdout)
        # A label absent from the manifest is reported but never deleted.
        self.assertIn("unmanaged", result.stdout)
        self.assertIn("hand-made", result.stdout)

        narrowed = self._run("--commands", live=live).stdout.strip().splitlines()
        self.assertEqual(
            {shlex.split(line)[3] for line in narrowed}, {dropped, changed}
        )

    def test_a_label_the_manifest_does_not_manage_is_reported_but_not_drift(self):
        # An extra label in the repo is informational: the sync never deletes, so it is
        # not something the manifest can or should close. Exit stays 0.
        live = self._as_live(_labels())
        live.append(
            {"name": "hand-made", "color": "ffffff", "description": "not managed here"}
        )
        result = self._run(live=live)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unmanaged", result.stdout)
        self.assertIn("hand-made", result.stdout)
        self.assertNotIn("missing:", result.stdout)
        self.assertNotIn("diverging:", result.stdout)
        self.assertIn("in sync", result.stdout)

    def test_paginated_and_slurped_gh_output_are_both_accepted(self):
        labels = self._as_live(_labels())
        half = len(labels) // 2
        pages = json.dumps(labels[:half]) + "\n" + json.dumps(labels[half:])
        slurped = json.dumps([labels[:half], labels[half:]])
        for name, payload in (("--paginate", pages), ("--paginate --slurp", slurped)):
            with self.subTest(gh_form=name):
                result = subprocess.run(
                    [sys.executable, str(LABELS_DIFF), "--live", "-"],
                    cwd=REPO,
                    input=payload,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("in sync", result.stdout)

    def test_live_is_required_for_a_drift_report(self):
        self.assertEqual(self._run().returncode, 2)

    def test_repo_flag_targets_the_emitted_commands(self):
        # Without --repo, `gh` infers the target from the working directory's remote,
        # which is how a taxonomy ends up synced into the wrong repository.
        plain = self._run("--commands").stdout.splitlines()[0]
        self.assertNotIn("--repo", plain)
        targeted = self._run(
            "--commands", "--repo", "acme/widgets"
        ).stdout.splitlines()[0]
        self.assertIn("--repo acme/widgets", targeted)

    def test_unusable_gh_output_is_refused_by_name_not_by_traceback(self):
        cases = {
            "empty": "",
            "error payload": '{"message": "Not Found", "status": "404"}',
            "single object": '{"name": "bug", "color": "d73a4a"}',
            "names only (--jq)": '["bug", "enhancement"]',
            "not json": "gh: command not found",
        }
        for name, payload in cases.items():
            with self.subTest(payload=name):
                result = subprocess.run(
                    [sys.executable, str(LABELS_DIFF), "--live", "-"],
                    cwd=REPO,
                    input=payload,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                self.assertTrue(
                    result.stderr.strip(), "the refusal must say what was wrong"
                )

    def test_an_unusable_manifest_is_refused_rather_than_read_as_drift(self):
        # Exit 1 means drift and the workflow treats it that way, so a corrupt manifest
        # must exit 2 instead of producing a plausible-looking report.
        cases = {
            "not json": "{oops",
            "no labels key": '{"tags": []}',
            "labels is a mapping": '{"labels": {"bug": "d73a4a"}}',
            "labels is empty": '{"labels": []}',
            "entry is a string": '{"labels": ["bug"]}',
            "entry missing color": '{"labels": [{"name": "bug", "description": "x"}]}',
        }
        for name, payload in cases.items():
            with self.subTest(manifest=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "labels.json"
                path.write_text(payload, encoding="utf-8")
                for mode in (["--commands"], ["--live", str(path)]):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(LABELS_DIFF),
                            "--manifest",
                            str(path),
                            *mode,
                        ],
                        cwd=REPO,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 2, f"{mode}: {result.stdout}")
                    self.assertNotIn("Traceback", result.stderr)

    def test_a_missing_file_is_refused_rather_than_read_as_drift(self):
        for args in (
            ["--manifest", "/nonexistent/labels.json", "--commands"],
            ["--live", "/nonexistent/live.json"],
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, str(LABELS_DIFF), *args],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_a_manifest_that_cannot_be_shell_quoted_is_refused(self):
        # The documented recipe pipes this output into a shell, so a value that would
        # not survive a command line must stop the run rather than be emitted.
        unusable = [
            {"name": "-wip", "color": "ffffff", "description": "leading dash in name"},
            {
                "name": "ok",
                "color": "ffffff",
                "description": "-leading dash in description",
            },
            {"name": "ok", "color": "fff; echo pwned", "description": "not hex"},
        ]
        for entry in unusable:
            with (
                self.subTest(entry=entry["description"]),
                tempfile.TemporaryDirectory() as tmp,
            ):
                path = Path(tmp) / "labels.json"
                path.write_text(json.dumps({"labels": [entry]}), encoding="utf-8")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(LABELS_DIFF),
                        "--manifest",
                        str(path),
                        "--commands",
                    ],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout, "", "nothing may be emitted")


class TestIssueFormSchema(unittest.TestCase):
    """A form that violates GitHub's schema does not render, with no local signal."""

    def test_top_level_keys_are_permitted_and_complete(self):
        for form in _form_files():
            with self.subTest(form=form.name):
                keys = {
                    line.split(":", 1)[0]
                    for line in form.read_text(encoding="utf-8").splitlines()
                    if re.match(r"^\w+:", line)
                }
                self.assertLessEqual(keys, FORM_TOP_LEVEL_KEYS)
                # GitHub requires all three; a form missing one does not render.
                self.assertLessEqual(FORM_TOP_LEVEL_REQUIRED, keys)

    def test_body_items_are_well_formed(self):
        for form in _form_files():
            fields = _form_fields(form.read_text(encoding="utf-8"))
            self.assertTrue(fields, f"{form.name} has no body items")
            ids = [field["id"] for field in fields if field["id"]]
            self.assertEqual(
                len(ids), len(set(ids)), f"duplicate field ids in {form.name}"
            )
            labels = [
                field["attributes"].get("label")
                for field in fields
                if field["type"] != "markdown"
            ]
            self.assertEqual(
                len(labels), len(set(labels)), f"duplicate field labels in {form.name}"
            )
            for field in fields:
                with self.subTest(form=form.name, field=field["id"] or field["type"]):
                    self.assertIn(field["type"], FORM_FIELD_TYPES)
                    self.assertLessEqual(set(field["keys"]), FORM_ITEM_KEYS)
                    self.assertLessEqual(
                        set(field["attributes"]),
                        FORM_ATTRIBUTES[field["type"]],
                        "unknown attribute for this field type",
                    )
                    if field["type"] in FORM_TYPES_WITHOUT_VALIDATIONS:
                        self.assertNotIn(
                            "validations",
                            field["keys"],
                            "a markdown block cannot be required",
                        )
                        self.assertIsNone(field["id"], "a markdown block takes no id")
                        self.assertIn("value", field["attributes"])
                    else:
                        self.assertRegex(field["id"] or "", r"^[A-Za-z0-9_-]+$")
                        self.assertTrue(field["attributes"].get("label"))

    @staticmethod
    def _declared_options(text):
        """Count option lines directly, without going through the reader under test."""
        lines = text.splitlines()
        total = 0
        for index, line in enumerate(lines):
            if line.rstrip() != "      options:":
                continue
            for candidate in lines[index + 1 :]:
                if not candidate.strip() or candidate.lstrip().startswith("#"):
                    continue
                if _indent(candidate) > 8:
                    continue
                if _indent(candidate) < 8 or not candidate.lstrip().startswith("- "):
                    break
                total += 1
        return total

    def test_the_reader_reads_every_declared_option(self):
        # Two shapes end a sequence early for a naive reader: a checkboxes option is a
        # mapping, so its `required:` line is indented past the dash, and a YAML comment
        # sits at the sequence's own indent. Either way the reader reports a subset of
        # the options and every assertion over them is born vacuous — a duplicated
        # option hidden behind a comment would stop the form rendering while the schema
        # tests stayed green. Parsed count is compared against an independent count.
        for form in _form_files():
            text = form.read_text(encoding="utf-8")
            declared = self._declared_options(text)
            parsed = sum(
                len(field["options"] or [])
                for field in _form_fields(text)
                if field["type"] in FORM_TYPES_WITH_OPTIONS
            )
            with self.subTest(form=form.name):
                self.assertEqual(parsed, declared)

    def test_option_lists_are_unique_and_renderable(self):
        for form in _form_files():
            for field in _form_fields(form.read_text(encoding="utf-8")):
                if field["type"] not in FORM_TYPES_WITH_OPTIONS:
                    continue
                with self.subTest(form=form.name, field=field["id"]):
                    options = field["options"]
                    self.assertTrue(options, f"a {field['type']} must declare options")
                    # A duplicated option makes the whole form fail to render.
                    self.assertEqual(len(options), len(set(options)))
                    for option in options:
                        self.assertTrue(option.strip())
                        self.assertNotIn(option.strip().lower(), RESERVED_OPTIONS)


class TestIssueForms(unittest.TestCase):
    def test_every_auto_applied_label_exists_in_the_taxonomy(self):
        known = {label["name"] for label in _labels()}
        for form in _form_files():
            labels = _form_labels(form.read_text(encoding="utf-8"))
            with self.subTest(form=form.name):
                self.assertTrue(labels, f"{form.name} auto-applies no labels")
            for label in labels:
                with self.subTest(form=form.name, label=label):
                    # GitHub drops form labels that do not pre-exist, silently.
                    self.assertIn(label, known)

    def test_every_form_requests_triage(self):
        for form in _form_files():
            with self.subTest(form=form.name):
                self.assertIn(
                    "needs-triage", _form_labels(form.read_text(encoding="utf-8"))
                )

    def test_bug_form_phase_options_match_readme_architecture(self):
        options = _field("bug_report.yml", "phase")["options"]
        self.assertEqual(
            [o for o in options if o.startswith("Phase ")], _readme_phases()
        )

    def test_bug_form_components_include_workflows_and_bench(self):
        joined = "\n".join(_field("bug_report.yml", "phase")["options"])
        self.assertIn("workflows/", joined)
        self.assertIn("bench/", joined)

    def test_bug_form_requires_install_method(self):
        field = _field("bug_report.yml", "install_method")
        self.assertIsNotNone(
            field, "bug_report.yml must ask how the plugin was installed"
        )
        self.assertEqual(field["type"], "dropdown")
        self.assertIn(
            "validations",
            field["keys"],
            "`validations` must be a sibling of `attributes`, not nested inside it",
        )
        self.assertTrue(field["required"], "install method is a first-order diagnostic")
        options = " ".join(field["options"]).lower()
        for expected in ("marketplace", "--plugin-dir", "clone"):
            with self.subTest(option=expected):
                self.assertIn(expected, options)

    def test_bug_form_plugin_version_placeholder_is_3x(self):
        placeholder = _field("bug_report.yml", "plugin_version")["attributes"][
            "placeholder"
        ]
        self.assertIsNotNone(placeholder)
        # Either a pinned example or the 3.x line; a pinned patch goes stale on every
        # release, since python-semantic-release never touches the form.
        self.assertRegex(placeholder, r"3\.(?:x|\d+\.\d+)")
        self.assertNotRegex(placeholder, r"2\.(?:x|\d+\.\d+)")
        self.assertIn(".claude-plugin/plugin.json", placeholder)

    def test_feature_form_area_options_include_workflows_and_bench(self):
        options = "\n".join(_field("feature_request.yml", "area")["options"])
        self.assertIn("workflows/", options)
        self.assertIn("bench/", options)

    def test_no_form_points_at_disabled_discussions(self):
        # Encodes the current owner decision: Discussions are disabled for this repo
        # (`has_discussions=false`), so no form may send anyone there. If Discussions are
        # ever enabled, that decision — and this test — get revisited together.
        for form in _form_files():
            with self.subTest(form=form.name):
                self.assertNotRegex(form.read_text(encoding="utf-8"), r"(?i)discussion")

    def test_config_links_the_security_advisory_form(self):
        links = _contact_links(_read(".github/ISSUE_TEMPLATE/config.yml"))
        matching = [
            name
            for name, url in links
            if url == ADVISORY_URL and re.search(r"(?i)security", name)
        ]
        self.assertTrue(matching, f"no security contact link among {links}")


class TestSecurityPolicy(unittest.TestCase):
    def test_security_md_exists_with_scope_and_supported_versions(self):
        text = _read("SECURITY.md")
        self.assertIn(
            ADVISORY_URL, text, "must point at the enabled private advisory form"
        )
        for heading in ("reporting", "supported versions", "scope"):
            with self.subTest(heading=heading):
                self.assertRegex(text, rf"(?im)^#{{2,3}} .*{heading}")

    def test_supported_versions_track_the_shipped_major(self):
        text = _read("SECURITY.md")
        major = json.loads(_read(".claude-plugin/plugin.json"))["version"].split(".")[0]
        self.assertIn(
            f"{major}.x", text, "supported versions must name the shipped major line"
        )
        # The retired architecture must not be advertised as supported.
        self.assertRegex(text, r"(?im)^\|\s*2\.x[^|]*\|\s*No")


class TestContributingDocs(unittest.TestCase):
    def test_contributing_lists_the_ci_enforced_commands_verbatim(self):
        text = _read("CONTRIBUTING.md")
        for command in (
            "python -m pytest tests/ -q",
            "node --test workflows/test/*.test.js",
            "pre-commit run --all-files",
            "node workflows/build.js",
        ):
            with self.subTest(command=command):
                self.assertIn(command, text)
        self.assertIn(
            "bare directory form is not a valid `node --test` target on node 24", text
        )
        self.assertIn("workflows/test/tools/record_parity.py", text)

    def test_contributing_describes_the_v3_js_pipeline_areas(self):
        text = _read("CONTRIBUTING.md")
        self.assertIn("workflows/src/", text)
        self.assertIn("`bench/`", text)
        self.assertIn("docs/maintainer-issues.md", text)

    def test_pr_template_covers_the_js_gates(self):
        text = _read(".github/pull_request_template.md")
        for gate in (
            "node --test workflows/test/*.test.js",
            "node workflows/build.js",
            "workflows/test/tools/record_parity.py",
        ):
            with self.subTest(gate=gate):
                self.assertIn(gate, text)

    def test_maintainer_issue_standard_has_the_required_sections(self):
        text = _read("docs/maintainer-issues.md")
        for section in (
            "Problem / Goal",
            "Requirements",
            "Evidence",
            "Candidate directions",
            "Out of scope",
            "Verification",
        ):
            with self.subTest(section=section):
                self.assertRegex(text, rf"(?m)^#{{2,4}} {re.escape(section)}")

    def test_maintainer_issue_standard_defers_tier_definitions_to_the_runbook(self):
        text = _read("docs/maintainer-issues.md")
        for tier in ("suites-only", "smoke", "paired-mini"):
            with self.subTest(tier=tier):
                self.assertIn(tier, text)
        # Requirement 8: reference the canonical home, do not restate the ladder. Costs
        # and `--tier` invocations are the tell that the ladder was copied in — but a
        # copy can be laundered into prose, so the ladder's own shape is rejected too.
        self.assertNotRegex(
            text, r"\$\d", "tier costs belong only in bench/MEASUREMENT.md"
        )
        self.assertNotIn(
            "--tier", text, "tier invocations belong only in bench/MEASUREMENT.md"
        )
        ladder_rows = [
            line
            for line in text.splitlines()
            if line.lstrip().startswith("|")
            and re.search(r"(?i)\b(tier|trigger|smoke|mini-subset|holdout)\b", line)
        ]
        self.assertEqual(
            ladder_rows,
            [],
            "a tier table here is the ladder restated; link the runbook instead",
        )
        link = re.search(r"\[`bench/MEASUREMENT\.md`\]\((\.\./[^)]+)\)", text)
        self.assertIsNotNone(
            link, "the canonical runbook must be linked, not just named"
        )
        self.assertTrue((REPO / "docs" / link.group(1)).resolve().is_file())

    def test_every_tier_slug_the_standard_mandates_resolves_in_the_runbook(self):
        # Read the standard's vocabulary rather than hardcoding it, so a slug coined here
        # cannot quietly become normative without existing in the canonical home a
        # reader is sent to. That drift shipped once already.
        standard = _read("docs/maintainer-issues.md")
        vocabulary = standard.split("names exactly one measurement tier", 1)[1].split(
            "\n\n", 2
        )[1]
        slugs = re.findall(r"^- `([a-z-]+)`", vocabulary, re.M)
        self.assertTrue(slugs, "the standard must list the tier slugs as a bullet list")
        runbook_slugs = set(
            re.findall(
                r"^\| [^|]+ \| `([a-z-]+)` \|", _read("bench/MEASUREMENT.md"), re.M
            )
        )
        for slug in slugs:
            with self.subTest(slug=slug):
                self.assertIn(
                    slug,
                    runbook_slugs,
                    "a slug the standard mandates must appear in the runbook's "
                    f"Slug column, which holds {sorted(runbook_slugs)}",
                )

    def test_cspell_scope_includes_the_new_public_docs(self):
        hooks = _read(".pre-commit-config.yaml")
        cspell_block = hooks.split("- id: cspell", 1)
        self.assertEqual(
            len(cspell_block), 2, "cspell hook missing from .pre-commit-config.yaml"
        )
        block = cspell_block[1].split("\n  - repo:", 1)[0]
        files_pattern = re.search(r"^        files: \^\((.*)\)\$", block, re.M)
        self.assertIsNotNone(
            files_pattern, "the cspell hook must keep an explicit files: scope"
        )
        scope = re.compile(rf"^({files_pattern.group(1)})$")
        for path in (
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "docs/maintainer-issues.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    scope.match(path), f"cspell scope does not cover {path}"
                )

    def test_contributing_python_lint_hook_ids_match_pre_commit_config(self):
        """Documented `pre-commit run <id>` commands stay tied to real hook ids."""
        expected = {"ruff-check", "ruff-format", "mypy"}
        contributing = _read("CONTRIBUTING.md")
        hooks = _read(".pre-commit-config.yaml")
        declared = set(re.findall(r"^\s+- id: ([^\s#]+)", hooks, re.M))
        documented = set(
            re.findall(
                r"pre-commit run (ruff-check|ruff-format|mypy) --all-files",
                contributing,
            )
        )
        self.assertEqual(
            documented,
            expected,
            "CONTRIBUTING.md must document exactly the Python lint/format/type hooks",
        )
        self.assertTrue(
            expected.issubset(declared),
            f"hooks {sorted(expected - declared)} missing from .pre-commit-config.yaml",
        )


# Required PR check-run names for ruleset 16049246 (protect-default-branch).
# Any new always-on PR gate must update THIS tuple and the live ruleset in the
# same change (see #108 / #102 / #105). GitHub matches check-run names, not
# workflow `name:` titles alone.
REQUIRED_PR_CHECK_CONTEXTS = (
    "Run Linting",
    "Run Tests (3.10)",
    "Run Tests (3.11)",
    "Run Tests (3.12)",
    "Run Workflow JS Lint",
    "Run Workflow JS Tests",
    "Run Bench Self-Tests (3.11)",
    "Run Bench Self-Tests (3.12)",
    "Validate plugin.json",
    "lint-pr-title",
)

# Every required check maps to the command a contributor runs locally to reproduce it,
# or to None when the check is CI-only. CONTRIBUTING claims the PR checklist covers the
# locally-runnable gates and names the CI-only ones; this mapping is what makes that
# claim checkable instead of a promise. A new required check must be added here, which
# forces the docs question to be answered rather than skipped.
LOCAL_COMMAND_FOR_REQUIRED_CHECK = {
    "Run Linting": "pre-commit run --all-files",
    "Run Tests (3.10)": "python -m pytest tests/ -q",
    "Run Tests (3.11)": "python -m pytest tests/ -q",
    "Run Tests (3.12)": "python -m pytest tests/ -q",
    "Run Workflow JS Lint": None,  # Biome is a checksum-pinned CI download.
    "Run Workflow JS Tests": "node --test workflows/test/*.test.js",
    "Run Bench Self-Tests (3.11)": "python -m pytest bench/tests/ -q",
    "Run Bench Self-Tests (3.12)": "python -m pytest bench/tests/ -q",
    "Validate plugin.json": "claude plugin validate .",
    "lint-pr-title": None,  # Needs the PR title; nothing local to run.
}

REQUIRED_PR_CHECK_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/validate.yml",
    ".github/workflows/pr-title-lint.yml",
)


def _derive_required_pr_check_contexts():
    """Expand the required checks for ruleset 16049246 (protect-default-branch).

    Update the frozen tuple and live ruleset together. Zero required approvals
    is deliberate; required checks, conversations, and linear history are the
    merge gates. Job-level name: wins; else the job id.
    """
    contexts = []
    job_header = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
    job_name = re.compile(r"^    name:\s*(.+?)\s*$")
    matrix_versions = re.compile(r"^        python-version:\s*\[(.*)\]\s*$")
    for rel in REQUIRED_PR_CHECK_WORKFLOWS:
        text = _read(rel)
        lines = text.splitlines()
        i = 0
        in_jobs = False
        while i < len(lines):
            line = lines[i]
            if line.startswith("jobs:"):
                in_jobs = True
                i += 1
                continue
            if not in_jobs:
                i += 1
                continue
            if line and not line.startswith(" ") and not line.startswith("#"):
                break
            m = job_header.match(line)
            if not m:
                i += 1
                continue
            job_id = m.group(1)
            display = None
            versions = None
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if job_header.match(nxt) or (
                    nxt and not nxt.startswith(" ") and not nxt.startswith("#")
                ):
                    break
                nm = job_name.match(nxt)
                if nm:
                    display = _unquote(nm.group(1))
                vm = matrix_versions.match(nxt)
                if vm:
                    versions = [
                        _unquote(p.strip()) for p in vm.group(1).split(",") if p.strip()
                    ]
                i += 1
            label = display if display is not None else job_id
            if versions:
                contexts.extend(f"{label} ({v})" for v in versions)
            else:
                contexts.append(label)
    return contexts


class TestRequiredPrCheckContexts(unittest.TestCase):
    def test_frozen_tuple_matches_workflow_derived_names(self):
        derived = tuple(sorted(_derive_required_pr_check_contexts()))
        frozen = tuple(sorted(REQUIRED_PR_CHECK_CONTEXTS))
        self.assertEqual(
            derived,
            frozen,
            "workflow job names drifted from REQUIRED_PR_CHECK_CONTEXTS — "
            "update the tuple AND ruleset 16049246 together",
        )

    def test_every_required_check_is_classified_local_or_ci_only(self):
        self.assertEqual(
            tuple(sorted(LOCAL_COMMAND_FOR_REQUIRED_CHECK)),
            tuple(sorted(REQUIRED_PR_CHECK_CONTEXTS)),
        )

    def test_locally_runnable_gates_are_on_the_pr_checklist(self):
        contributing = _read("CONTRIBUTING.md")
        template = _read(".github/pull_request_template.md")
        for context, command in sorted(LOCAL_COMMAND_FOR_REQUIRED_CHECK.items()):
            if command is None:
                continue
            with self.subTest(context=context):
                self.assertIn(command, contributing)
                self.assertIn(command, template)

    def test_ci_only_gates_are_named_in_contributing(self):
        contributing = _read("CONTRIBUTING.md")
        ci_only = [
            context
            for context, command in LOCAL_COMMAND_FOR_REQUIRED_CHECK.items()
            if command is None
        ]
        self.assertTrue(ci_only)
        for context in ci_only:
            with self.subTest(context=context):
                self.assertIn(context, contributing)

    def test_docstring_names_the_ruleset(self):
        doc = _derive_required_pr_check_contexts.__doc__ or ""
        self.assertIn("16049246", doc)
        self.assertIn("protect-default-branch", doc)


GATE_MARKERS = ("--cov-fail-under=", "--test-coverage-lines=")


def _agents_coverage_gate_commands(text: str) -> set[str]:
    """Commands inside the Coverage gates fenced bash block, split on blank lines."""
    # Find the section heading, then the first ```bash fence after it.
    heading = re.search(r"(?m)^Coverage gates\b.*$", text)
    if not heading:
        raise AssertionError("AGENTS.md missing 'Coverage gates' section")
    rest = text[heading.end() :]
    fence = re.search(r"```bash\n(.*?)```", rest, re.DOTALL)
    if not fence:
        raise AssertionError("AGENTS.md Coverage gates missing ```bash fence")
    body = fence.group(1)
    chunks = re.split(r"\n\s*\n", body.strip("\n"))
    # rstrip trailing newlines only — cosmetic blank lines after a command must
    # not create a confusing residual-\n false fail (interior blanks stay).
    commands = {c.rstrip("\n") for c in chunks if c.strip()}
    if len(commands) < 3:
        raise AssertionError(
            f"expected ≥3 gate commands in AGENTS.md, found {len(commands)}"
        )
    return commands


def _ci_run_blocks(text: str) -> list[str]:
    """Literal bodies of run: | / run: |- block scalars (stdlib; no PyYAML)."""
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)run:\s*\|-?\s*$", lines[i])
        if not m:
            i += 1
            continue
        base = len(m.group(1))
        i += 1
        content_indent = None
        collected = []
        while i < len(lines):
            line = lines[i]
            if line.strip() == "":
                collected.append("")
                i += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            if content_indent is None:
                if indent <= base:
                    break
                content_indent = indent
            if indent < content_indent and line.strip():
                break
            collected.append(
                line[content_indent:] if content_indent is not None else line
            )
            i += 1
        blocks.append("\n".join(collected).rstrip("\n"))
    return blocks


def _ci_gate_commands(text: str) -> set[str]:
    return {
        b for b in _ci_run_blocks(text) if any(marker in b for marker in GATE_MARKERS)
    }


def _ci_workflow_tests_node_version(text: str) -> str:
    """The node-version: value under the workflow-tests job (stdlib; no PyYAML)."""
    lines = text.splitlines()
    in_job = False
    for line in lines:
        if re.match(r"^  workflow-tests:\s*$", line):
            in_job = True
            continue
        if in_job and re.match(r"^  \S", line):
            break
        if not in_job:
            continue
        match = re.match(r'^\s+node-version:\s*["\']?([^"\']+)["\']?\s*$', line)
        if match:
            return match.group(1).strip()
    raise AssertionError("ci.yml workflow-tests job missing node-version:")


class TestCoverageGateCommandIdentity(unittest.TestCase):
    def test_agents_and_ci_gate_commands_are_byte_identical_sets(self):
        agents = _agents_coverage_gate_commands(_read("AGENTS.md"))
        ci = _ci_gate_commands(_read(".github/workflows/ci.yml"))
        self.assertEqual(agents, ci)

    def test_js_gate_includes_match_coverage_scope_json(self):
        scope = json.loads(_read("workflows/test/tools/coverage_scope.json"))
        agents = _agents_coverage_gate_commands(_read("AGENTS.md"))
        js_gates = [c for c in agents if "--test-coverage-lines=" in c]
        self.assertEqual(len(js_gates), 1, js_gates)
        flags = re.findall(r"--test-coverage-include='([^']+)'", js_gates[0])
        self.assertEqual(set(flags), set(scope["includes"]))

    def test_contributing_documents_the_ci_node_version_pin(self):
        version = _ci_workflow_tests_node_version(_read(".github/workflows/ci.yml"))
        contributing = _read("CONTRIBUTING.md")
        self.assertIn(f"`{version}`", contributing)
        self.assertIn(f'node-version: "{version}"', _read(".github/workflows/ci.yml"))


if __name__ == "__main__":
    unittest.main()
