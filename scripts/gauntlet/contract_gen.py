#!/usr/bin/env python3
"""Generate registry contracts; dispatch phrases stay exact because tools parse them."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import types
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from typing import Any, Literal

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser
from gauntlet.generate import finish, sync_targets
from gauntlet.paths import ENTRY_ROOT

MARKER_OPEN = (
    "<!-- generated-from-registry: do not edit; "
    "scripts/generate_contract_requirements.py -->"
)
MARKER_CLOSE = "<!-- /generated-from-registry -->"

REPORT_FORMAT_REL = "skills/code-gauntlet/references/report-format.md"

_IDENTITY_TAG = "generated-from-registry-identity"
# Detect both syntaxes so undeclared orphan markers cannot evade validation.
_IDENTITY_MARKER_RE = re.compile(
    rf"^\s*(?:#|<!--)\s*(?P<close>/)?{re.escape(_IDENTITY_TAG)}:(?P<symbol>"
    rf"[A-Za-z0-9_]+)"
)

IDENTITY_FENCES = {
    REPORT_FORMAT_REL: [
        "severity_legend",
        "permalink_formats",
        "permalink_sample",
        "inline_legend",
        "inline_sample",
        "full_report_template",
    ],
    "skills/code-gauntlet/references/delivery-guide.md": [
        "severity_legend",
        "summary_header",
        "inline_sample",
        "delivery_identity",
    ],
    # Generate the prose mark too so a registry edit updates every identity surface.
    "skills/code-gauntlet/SKILL.md": [
        "chat_identity",
        "config_receipt",
        "derived_waist_fields",
        "pr_identity_fields",
    ],
    "skills/code-gauntlet/references/phase2-triage.md": [
        "pr_identity_fields",
        "derived_waist_fields",
    ],
    "skills/code-gauntlet/references/phase1-preflight.md": ["derived_waist_fields"],
    "skills/code-gauntlet/references/headless-mode.md": ["headless_env_table"],
    "skills/code-gauntlet/references/phase8-delivery.md": ["permalink_formats"],
}

# The registry carries field names, not the prose nouns their templates need.
_CONDITIONAL_NOUNS = {
    "claude_md_rule": ("rule", "rule"),
    "spec_text": ("spec text", "spec"),
}


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _stderr_tail(stderr: str | None) -> str:
    for line in reversed((stderr or "").splitlines()):
        clean = _CONTROL_RE.sub("", line)
        if clean.strip():
            return clean[-200:]
    return ""


def _node_failure_message(command: Sequence[str], stderr: str | None = None) -> str:
    command_text = " ".join(_CONTROL_RE.sub("", part) for part in command)
    message = "node 24 command failed: " + command_text
    tail = _stderr_tail(stderr)
    return f"{message}: {tail}" if tail else message


def _run_node(node_src: str, repo_root: str) -> proc.CompletedProcess[str]:
    command = ["node", "--input-type=module", "-e", node_src]
    try:
        return proc.run(command, cwd=repo_root, check=True)
    except FileNotFoundError:
        raise CliError(_node_failure_message(command)) from None
    except proc.CalledProcessError as error:
        raise CliError(_node_failure_message(command, error.stderr)) from None


# Import order binds the module aliases m, a, v, f, o and w used below.
_REGISTRY_MODULES = (
    "registry",
    "knobs",
    "applyValidations",
    "injection",
    "outputPaths",
    "verifyWire",
)
# Field order is the JSON key order the generated registry is checked against.
_REGISTRY_FIELDS = (
    "required: m.FINDING_REQUIRED",
    "waistRequired: m.REQUIRED",
    "canonicalFields: Object.keys(m.FINDING_PROP_TYPES)",
    "findingTypes: m.FINDING_PROP_TYPES",
    "dimensions: m.DIMENSIONS.map(d => ({dimension: d.dimension,"
    " agentType: d.agentType, requiredExtra: d.requiredExtra || [],"
    " requiredWhenDimension: d.requiredWhenDimension || [],"
    " extraFields: Object.keys(d.schemaExtra || {})}))",
    "brand: {mark: m.BRAND_MARK, name: m.BRAND_NAME}",
    "severityEmoji: m.SEVERITY_EMOJI",
    "severityOrder: m.SEVERITY_ORDER",
    "reachability: v.REACHABILITY_VALUES",
    "deltaKeys: w.DELTA_KEYS",
    "verifySliceFields: w.VERIFY_SLICE_FIELDS",
    "fixMaxLines: f.FIX_MAX_LINES",
    "fixMaxChars: f.FIX_MAX_CHARS",
    "artifactPaths: o.plannedArtifactPaths("
    "'/__gauntlet_registry_root__', '__GAUNTLET_SHA__')",
    "jsTrimChars: Array.from({length: 65536}, (_, i) => String.fromCharCode(i))"
    ".filter(c => c.trim() === '' && c !== '').join('')",
    "severityEmojiFallback: m.SEVERITY_EMOJI_FALLBACK",
    "ruleSourceLabels: m.RULE_SOURCE_LABELS",
    "ruleSourceLabelFallback: m.RULE_SOURCE_LABEL_FALLBACK",
    "codeOwnedHeadings: m.CODE_OWNED_HEADINGS",
    "agents: m.AGENTS",
    "prIdentityFields: m.PR_IDENTITY_FIELDS.map(f => ({name: f.name,"
    " required: f.required, describe: f.describe}))",
    "permalinkTemplates: m.PERMALINK_TEMPLATES",
    "shaFullRe: m.SHA_FULL_RE.source",
    "webOriginRe: m.WEB_ORIGIN_RE.source",
    "knobs: a.KNOB_REGISTRY.map(d => ({...d}))",
    "knobKeys: a.KNOB_REGISTRY.map(d => Object.keys(d))",
    "deriveWhen: Object.fromEntries(Object.entries(a.DERIVE_WHEN)"
    ".map(([name, d]) => [name, d.describe]))",
    "derivedFrom: Object.fromEntries(Object.entries(a.DERIVED_FROM)"
    ".map(([name, d]) => [name, d.describe]))",
)


def load_registry(repo_root: str) -> Mapping[str, Any]:
    """Import the live schemas and keep finding and waist required lists distinct."""
    fields = ",".join(_REGISTRY_FIELDS)
    node_src = (
        "Promise.all(["
        + ", ".join(
            f"import('./workflows/src/{name}.js')" for name in _REGISTRY_MODULES
        )
        + "]).then(([m, a, v, f, o, w]) => console.log(JSON.stringify({"
        + fields
        + "})))"
    )
    out = _run_node(node_src, repo_root)
    registry: Mapping[str, Any] = json.loads(out.stdout)
    return registry


def dispatch_required_sentence(fields: Sequence[str]) -> str:
    """Render all required fields so schema growth cannot truncate the sentence."""
    if len(fields) == 1:
        return (
            f"`{fields[0]}` is required by the dispatch schema — a finding "
            f"without it is "
            "rejected at the StructuredOutput boundary and retried, so it "
            "must always be present."
        )
    backticked = [f"`{f}`" for f in fields]
    if len(backticked) == 2:
        joined = f"{backticked[0]} and {backticked[1]}"
    else:
        joined = ", ".join(backticked[:-1]) + f", and {backticked[-1]}"
    return (
        f"{joined} are required by the dispatch schema — a finding missing "
        f"any of them is "
        "rejected at the StructuredOutput boundary and retried, so all must always be "
        "present."
    )


def _conditional_paragraph(
    field: str,
    dimension: str,
    siblings: Sequence[str],
    all_dims: Sequence[str],
    first_field: str | None,
) -> str:
    noun, noun2 = _CONDITIONAL_NOUNS[field]
    lead = (
        f"For {dimension} findings: the `{field}` field MUST be non-null "
        f"and MUST quote "
        f"the specific {noun}. Findings without a cited {noun2} will be rejected. "
        f"`{field}` is a dimension-conditional dispatch requirement"
    )
    if field == first_field:
        sib = "/".join(siblings)
        dims = ", ".join(all_dims[:-1]) + f", and {all_dims[-1]}"
        body = (
            ". On a dispatch that targets the first-party API directly (no third-party "
            "provider, no gateway), the schema enforces it specifically "
            "for findings whose "
            f"dimension is {dimension} — sibling {sib} findings correctly "
            f"omit it — and this "
            "contract is the enforcement floor on every run, including "
            "third-party providers "
            "and gateway sessions where the schema stays flat. This "
            "agent's dispatch mixes "
            f"{dims} findings in ONE schema, so a dimension-blind schema "
            f"requirement (the flat "
            "`requiredExtra` mechanism, which only single-dimension agents "
            "can use) was never "
            "an option here — omitting the field on the wrong dimension is "
            "correct while omitting it on this one is a contract violation."
        )
    else:
        body = (
            f", on the same terms as {first_field} above: enforced by the "
            f"schema for findings "
            f"whose dimension is {dimension} only on a first-party-direct "
            f"dispatch, and by "
            "this contract as the floor everywhere else — omitting the "
            "field on the wrong "
            "dimension is correct while omitting it on this one is a "
            "contract violation."
        )
    return lead + body


def conditional_paragraphs(agent_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    all_dims = [row["dimension"] for row in agent_rows]
    conditional_fields = [
        (row["dimension"], field)
        for row in agent_rows
        for field in row["requiredWhenDimension"]
    ]
    first_field = conditional_fields[0][1] if conditional_fields else None
    paragraphs = []
    for dimension, field in conditional_fields:
        siblings = [d for d in all_dims if d != dimension]
        paragraphs.append(
            _conditional_paragraph(field, dimension, siblings, all_dims, first_field)
        )
    return paragraphs


_EXISTING_BLOCK = re.compile(
    re.escape(MARKER_OPEN) + r"\n.*?\n" + re.escape(MARKER_CLOSE), re.DOTALL
)

# Loose first-run anchors swallow earlier hand-written wording into the generated block.
_SINGLE_SENTENCE_ANCHOR = re.compile(
    "`[a-z_]+`(?:, `[a-z_]+`)*(?:,? and `[a-z_]+`)? (?:is|are) required by "
    "the dispatch "
    r"schema[^\n]*"
)
_CONDITIONAL_ANCHOR = re.compile(
    r"For convention findings:.*?\n\nFor intent findings:.*?(?=\n\n)", re.DOTALL
)


def splice(text: str, anchor_re: re.Pattern[str], body: str) -> str:
    """Reject broken markers so --check cannot silently accept stale debris."""
    open_count = text.count(MARKER_OPEN)
    close_count = text.count(MARKER_CLOSE)
    if open_count > 1 or open_count != close_count:
        raise CliError(
            f"malformed generated-block markers ({open_count} open, "
            f"{close_count} close) "
            "— expected exactly one matched pair or none; fix by hand "
            "before regenerating"
        )
    new_block = f"{MARKER_OPEN}\n{body}\n{MARKER_CLOSE}"
    if open_count == 1:
        replaced, count = _EXISTING_BLOCK.subn(new_block, text, count=1)
        if count != 1:
            raise CliError("found MARKER_OPEN but block regex did not match")
        return replaced
    match = anchor_re.search(text)
    if not match:
        raise CliError("no generated block and no recognizable anchor text to replace")
    return text[: match.start()] + new_block + text[match.end() :]


def single_dimension_targets(registry: Mapping[str, Any]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for row in registry["dimensions"]:
        fields = row["requiredExtra"]
        if not fields:
            continue
        path = f"agents/{row['agentType'].split(':', 1)[1]}.md"
        targets[path] = dispatch_required_sentence(fields)
    return targets


def field_required_status(
    field: str, registry: Mapping[str, Any]
) -> Literal["yes", "conditional", "no"]:
    if field in registry["required"]:
        return "yes"
    for row in registry["dimensions"]:
        if field in row["requiredExtra"]:
            return "yes"
    for row in registry["dimensions"]:
        if field in row["requiredWhenDimension"]:
            return "conditional"
    return "no"


_CANONICAL_ROW = re.compile(r"^(\| `([a-z_]+)` \| \w+ \| )(yes|no|conditional)( \|.*)$")
_PERDIM_ROW = re.compile(
    r"^(\| `([a-z_]+)` \| \w+ \| \w+ \| )(yes|no|conditional)( \|.*)$"
)


def rewrite_required_column(text: str, registry: Mapping[str, Any]) -> str:
    known = set(registry["canonicalFields"])
    for row in registry["dimensions"]:
        known.update(row["extraFields"])
    out_lines = []
    for line in text.splitlines():
        rewritten = line
        for pattern in (_CANONICAL_ROW, _PERDIM_ROW):
            m = pattern.match(line)
            if m:
                field = m.group(2)
                if field not in known:
                    break
                status = field_required_status(field, registry)
                rewritten = m.group(1) + status + m.group(4)
                break
        out_lines.append(rewritten)
    text_out = "\n".join(out_lines)
    if text.endswith("\n"):
        text_out += "\n"
    return text_out


def _python_literal(value: object, indent: int = 0, *, column: int = 0) -> str:
    """Render JSON-safe data with short scalar sequences on one line."""
    pad = " " * indent
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        is_tuple = isinstance(value, tuple)
        if not value:
            return "()" if is_tuple else "[]"
        if all(not isinstance(item, (list, tuple, dict)) for item in value):
            members = ", ".join(_python_literal(item) for item in value)
            if is_tuple and len(value) == 1:
                members += ","
            flat = ("(" if is_tuple else "[") + members + (")" if is_tuple else "]")
            if column + len(flat) + 1 <= 88:
                return flat
        lines = ["(" if is_tuple else "["]
        for item in value:
            rendered = _python_literal(item, indent + 4, column=indent + 4)
            item_lines = rendered.splitlines()
            lines.append(" " * (indent + 4) + item_lines[0])
            lines.extend(item_lines[1:])
            lines[-1] += ","
        lines.append(pad + (")" if is_tuple else "]"))
        return "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for key, item in value.items():
            prefix = (
                " " * (indent + 4) + json.dumps(str(key), ensure_ascii=False) + ": "
            )
            rendered = _python_literal(item, indent + 4, column=len(prefix))
            item_lines = rendered.splitlines()
            lines.append(prefix + item_lines[0])
            lines.extend(item_lines[1:])
            lines[-1] += ","
        lines.append(pad + "}")
        return "\n".join(lines)
    raise CliError(f"cannot render non-JSON registry value: {value!r}")


def _detail_fields(identity: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    citation_fields = {"claude_md_rule", "spec_text"} & set(identity["canonicalFields"])
    return {
        row["dimension"]: tuple(
            [
                *row["extraFields"],
                *(
                    field
                    for field in row["requiredWhenDimension"]
                    if field in citation_fields and field not in row["extraFields"]
                ),
            ]
        )
        for row in identity["dimensions"]
    }


def render_python_registry(identity: Mapping[str, Any]) -> str:
    field_types = identity["findingTypes"]
    for field, schema in field_types.items():
        if schema not in (
            "string",
            "number",
            {"type": "array", "items": {"type": "string"}},
        ):
            raise CliError(f"unknown finding schema type for {field}: {schema!r}")
    delta_keys = identity["deltaKeys"]
    delta_value_fields = delta_keys[2:]
    root = "/__gauntlet_registry_root__/"
    sha = "__GAUNTLET_SHA__"
    templates = {
        key: path[len(root) :].replace(sha, "{sha}")
        for key, path in identity["artifactPaths"].items()
    }

    def literal_type(values: Sequence[str]) -> str:
        members = ", ".join(json.dumps(v) for v in values)
        if len(members) <= 80:
            return (
                "Literal[" + members + "]"
                if len(members) <= 60
                else "Literal[\n    " + members + "\n]"
            )
        return "Literal[\n" + "\n".join(f"    {json.dumps(v)}," for v in values) + "\n]"

    lines = [
        '"""Generated by scripts/generate_contract_requirements.py. Do not edit."""',
        "",
        "from types import MappingProxyType",
        "from typing import Any, Literal, TypedDict",
        "",
        f"Severity = {literal_type(identity['severityOrder'])}",
        f"Reachability = {literal_type(identity['reachability'])}",
        f"DeltaKey = {literal_type(delta_keys)}",
        f"DeltaValueField = {literal_type(delta_value_fields)}",
        "",
    ]
    values = {
        "SEVERITY_ORDER": tuple(identity["severityOrder"]),
        "REACHABILITY_VALUES": tuple(identity["reachability"]),
        "DELTA_KEYS": tuple(delta_keys),
        "DELTA_VALUE_FIELDS": tuple(delta_value_fields),
        "VERIFY_SLICE_FIELDS": tuple(identity["verifySliceFields"]),
        "BRAND_MARK": identity["brand"]["mark"],
        "BRAND_NAME": identity["brand"]["name"],
        "SEVERITY_EMOJI": identity["severityEmoji"],
        "SEVERITY_EMOJI_FALLBACK": identity["severityEmojiFallback"],
        "RULE_SOURCE_LABELS": identity["ruleSourceLabels"],
        "RULE_SOURCE_LABEL_FALLBACK": identity["ruleSourceLabelFallback"],
        "CODE_OWNED_HEADINGS": identity["codeOwnedHeadings"],
        "DETAIL_FIELDS_BY_DIMENSION": _detail_fields(identity),
        "KNOB_REGISTRY": tuple(identity["knobs"]),
    }
    annotations = {
        "SEVERITY_ORDER": "tuple[Severity, ...]",
        "REACHABILITY_VALUES": "tuple[Reachability, ...]",
        "DELTA_KEYS": "tuple[DeltaKey, ...]",
        "DELTA_VALUE_FIELDS": "tuple[DeltaValueField, ...]",
        "VERIFY_SLICE_FIELDS": "tuple[str, ...]",
    }
    for name, value in values.items():
        annotation = f": {annotations[name]}" if name in annotations else ""
        prefix = f"{name}{annotation} = "
        lines.append(prefix + _python_literal(value, column=len(prefix)))
        lines.append("")
    lines.extend(
        [
            "",
            "class _DeltaRequired(TypedDict):",
            "    id: str",
            "    verified: bool",
            "",
            "",
            "class Delta(_DeltaRequired, total=False):",
            *(
                f"    {field}: {'int' if field == 'confidence' else 'object'}"
                for field in delta_value_fields
            ),
            "",
            "",
            "class VerifySliceFinding(TypedDict, total=False):",
            *(f"    {field}: Any" for field in identity["verifySliceFields"]),
            "",
            "",
        ]
    )
    lines.extend(
        [
            "ARTIFACT_PATH_TEMPLATES = MappingProxyType(\n    "
            + _python_literal(templates).replace("\n", "\n    ")
            + "\n)",
            "ARTIFACT_BASENAMES = tuple(ARTIFACT_PATH_TEMPLATES.values())",
            f"FIX_MAX_LINES = {identity['fixMaxLines']}",
            f"FIX_MAX_CHARS = {identity['fixMaxChars']}",
            "JS_TRIM_CHARS = (\n"
            + "\n".join(
                "    "
                + json.dumps(identity["jsTrimChars"][i : i + 6], ensure_ascii=True)
                for i in range(0, len(identity["jsTrimChars"]) or 1, 6)
            )
            + "\n)",
            "",
        ]
    )
    return "\n".join(lines)


def _load_resolver(repo_root: str) -> types.ModuleType:
    """Load gauntlet/config.py by path under a unique module name."""
    path = os.path.join(repo_root, "scripts", "gauntlet", "config.py")
    module_name = f"_contract_resolver_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise CliError(f"cannot load resolver module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Render placeholders through the real renderer so the copyable template cannot drift.
def _placeholders(prefix: str, fields: str) -> dict[str, str]:
    return {field: "{" + prefix + "." + field + "}" for field in fields.split()}


_TEMPLATE_FINDING = _placeholders(
    "finding", "id file line_start title description confidence dimension"
)

_TEMPLATE_FIXTURE: dict[str, Any] = {
    "mode": "interactive",
    "configEcho": {
        "model_tier": {"value": "optimized", "source": "fixed"},
        "pr_comment_cap": {"value": "null", "source": "default"},
        "delivery_tier": {"value": "all", "source": "default"},
        "review_md": {"value": "absent", "source": "discovery"},
    },
    "pluginRoot": "/absolute/path/to/claude-code-gauntlet",
    "pipelineVersion": "{pipeline_version}",
    "reviewScope": {
        "requested": "full",
        "kind": "full",
        "since": None,
        "commits": None,
        "detector": None,
    },
    "policy": {"tier": "optimized", "provider": "firstParty", "gateway": False},
    "deliveryTier": "all",
    "deliveryCap": None,
    "gapCount": 0,
    "summary": "{summary}",
    "findings": [
        {
            **_TEMPLATE_FINDING,
            "severity": "critical",
            "origin": "surfaced",
            **_placeholders(
                "finding",
                "line_end evidence suggestion claude_md_rule rule_source spec_text "
                "cross_file_refs affected_consumers attack_vector behavior_preserved "
                "criticality failure_scenario hidden_errors invalid_state_example",
            ),
            "challenge_contested": True,
            "corroborations": [
                _placeholders(
                    "corroboration", "agent dimension confidence title description"
                )
            ],
        },
        {**_TEMPLATE_FINDING, "severity": "high"},
        {**_TEMPLATE_FINDING, "severity": "medium"},
        {
            **_TEMPLATE_FINDING,
            "severity": "low",
            "report_tag": "suggestion",
            "demoted_by": "reachability",
        },
    ],
    "unverified": [
        {
            **_TEMPLATE_FINDING,
            "severity": "medium",
            "origin": "unknown",
            "challenge": "skipped",
            "evidence": "{finding.evidence}",
        }
    ],
    "dimensions": {"dispatched": [], "degraded": []},
    "stats": {
        "discovered": "{stats.discovered}",
        "validate": {
            "accepted": "{stats.validate.accepted}",
            "rejected": "{stats.validate.rejected}",
        },
        "filter": {
            "accepted": "{stats.filter.accepted}",
            "rejected": "{stats.filter.rejected}",
        },
        "challenge": {
            "accepted": "{stats.challenge.accepted}",
            "rejected": "{stats.challenge.rejected}",
        },
        "merge": {
            "findings_per_channel": {
                "ndjson": "{stats.merge.ndjson}",
                "text_fallback": "{stats.merge.text_fallback}",
            },
            "duplicates_resolved": "{stats.merge.duplicates_resolved}",
            "dropped_no_id": "{stats.merge.dropped_no_id}",
            "truncation_warnings": "{stats.merge.truncation_warnings}",
            "validation_warnings": "{stats.merge.validation_warnings}",
        },
    },
    "generatedAt": "{generatedAt}",
    "headShaShort": "{head_sha_short}",
    "prIdentity": {
        "owner": "{owner}",
        "repo": "{repo}",
        "pr_number": "{n}",
        "sha_full": "{full_sha}",
        "platform": "github",
        "web_origin": "https://github.com",
        "title": "{pr_title}",
    },
}


def _template_fixture() -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads(json.dumps(_TEMPLATE_FIXTURE))
    return fixture


def _render_report(repo_root: str, fixture: Mapping[str, Any]) -> str:
    node_src = (
        "import('./workflows/src/renderReport.js').then(m => "
        "process.stdout.write(m.renderReport(" + json.dumps(fixture) + ")))"
    )
    return _run_node(node_src, repo_root).stdout


def render_template_block(repo_root: str, identity: Mapping[str, Any]) -> str:
    fixture = _template_fixture()
    fixture["dimensions"]["dispatched"] = identity["agents"]
    return "````markdown\n" + _render_report(repo_root, fixture) + "\n````"


def render_permalink_sample(repo_root: str) -> list[str]:
    fixture = _template_fixture()
    fixture["findings"] = [fixture["findings"][0]]
    fixture["unverified"] = []
    fixture["prIdentity"].update(
        {
            "pr_number": 7,
            "sha_full": "0123456789abcdef0123456789abcdef01234567",
        }
    )
    sections = []
    identities = [
        ("GitHub", "github", "https://github.com", "{owner}"),
        ("GitLab", "gitlab", "https://gitlab.com", "group/sub"),
    ]
    for label, platform, origin, owner in identities:
        fixture["prIdentity"].update(
            {"platform": platform, "web_origin": origin, "owner": owner}
        )
        report = _render_report(repo_root, fixture).splitlines()
        identity_line = next(line for line in report if line.startswith("Reviewed"))
        location_line = next(
            line for line in report if line.startswith("- **Location:**")
        )
        sections.extend([f"**{label}:**", "", identity_line, "", location_line])
        if label != identities[-1][0]:
            sections.append("")
    return sections


def permalink_formats_body(identity: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for platform, templates in identity["permalinkTemplates"].items():
        lines.append(
            f"- `{platform}`: blob `{templates['blob']}`; line `{templates['line']}`; "
            f"range `{templates['range']}`; ref `{templates['ref']}`; ref URL "
            f"`{templates['refUrl']}`."
        )
    lines.append(
        "- Encode owner, repo, and file paths one segment at a time with "
        "`encodeURIComponent` semantics; also percent-encode `!`, `'`, `(`, `)`, and "
        "`*`, while preserving `/` separators. A file path containing an empty, `.`, "
        "or `..` segment renders as a plain code span."
    )
    return lines


def pr_identity_fields_body(identity: Mapping[str, Any]) -> list[str]:
    lines = ["`delivery.prIdentity` fields:", ""]
    for field in identity["prIdentityFields"]:
        requirement = "required" if field["required"] else "optional"
        lines.append(f"- `{field['name']}` ({requirement}): {field['describe']}.")
    producer_command = (
        "`python3 {plugin_root}/scripts/resolve_pr_identity.py --platform "
        + "github|gitlab --url <PR/MR web url> --sha <git rev-parse HEAD> "
        + "[--title <text>]`"
    )
    lines.extend(["", "Producer command:", "", producer_command])
    return lines


_INLINE_SAMPLE_FINDING = {
    "severity": "severity",
    "title": "{finding.title}",
    "body": "{body}",
    "suggestion": "{suggestion}",
    "claude_md_rule": (
        "{claude_md_rule, falling back to spec_text — blockquoted, one `>` "
        "line per source line}"
    ),
    "rule_source": "documented_rule",
    "suggested_fix_code": "{suggested_fix_code}",
}


def render_inline_comment_sample(identity: Mapping[str, Any]) -> str:
    # Repair must work even when the generated registry cannot be imported.
    from gauntlet.delivery import compose

    # Explicit style keeps isolated generator inputs from changing registry globals.
    style = compose.CommentStyle(
        trailer=f"{identity['brand']['mark']} *{identity['brand']['name']}*",
        severity_emoji={"severity": "{emoji}"},
        severity_emoji_fallback="{emoji}",
        rule_source_labels={"documented_rule": "{rule_source_label}"},
        rule_source_label_fallback="{rule_source_fallback}",
    )
    rendered = compose.render_comment_body(_INLINE_SAMPLE_FINDING, style=style)
    # Keep generator control comments outside the copyable fenced sample.
    return "````markdown\n" + rendered + "\n````"


def _modes_phrase(modes: Sequence[str]) -> str:
    names = list(modes)
    if len(names) == 1:
        return f"{names[0]} runs"
    if len(names) == 2:
        return f"{names[0]} and {names[1]} runs"
    return f"{', '.join(names[:-1])}, and {names[-1]} runs"


def _derived_waist_instruction(
    row: Mapping[str, Any], waist_required: Sequence[str]
) -> str:
    path = row["waistPath"]
    parts = path.split(".")
    if len(parts) == 1:
        return "Leave it out."
    root, leaf = parts[0], parts[-1]
    if root in waist_required:
        return f"Stamp `{root}` and leave `{leaf}` out of it."
    return f"Leave `{leaf}` out of any stamped `{root}`."


def _derived_waist_body(identity: Mapping[str, Any]) -> list[str]:
    rows = identity["knobs"]
    waist_rows = [row for row in rows if row["waistPath"] is not None]
    derived_rows = [row for row in rows if row["derivedFrom"] is not None]
    lines: list[str] = []
    if waist_rows:
        lines.extend(
            [
                (
                    "The workflow derives these waist fields from the copied "
                    "`configEcho` receipt. Do not stamp a derived field; when "
                    "a listed derivation applies, a stamped value that "
                    "disagrees with its receipt is refused before dispatch."
                ),
                "",
            ]
        )
        for row in waist_rows:
            type_note = ""
            if row["type"] == "int_or_null":
                type_note = "; digits derive as a JSON number"
                if row["nullReceipt"]:
                    type_note += (
                        f"; on {_modes_phrase(row['nullReceipt'])} the "
                        f"receipt spelling "
                        "`null` derives as JSON `null`"
                    )
            elif row["type"] == "csv_list":
                type_note = "; the comma-separated value derives as a list"
            condition = ""
            if row["deriveWhen"] is not None:
                condition = ", when " + identity["deriveWhen"][row["deriveWhen"]]
            line = (
                f"- `{row['waistPath']}` ({_modes_phrase(row['modes'])}{condition}): "
                f"from `configEcho.{row['key']}`{type_note}"
            )
            if row["waistMap"] is not None:
                for source, target in row["waistMap"].items():
                    line += f"; `{source}` derives as `{target}`"
            line += ". " + _derived_waist_instruction(row, identity["waistRequired"])
            lines.append(line)
    if derived_rows:
        if lines:
            lines.extend(
                [
                    "",
                    "The workflow fills these receipt entries itself; never stamp them.",
                    "",
                ]
            )
        else:
            lines.append(
                "The workflow fills these receipt entries itself; never stamp them."
            )
        for row in derived_rows:
            description = identity["derivedFrom"][row["derivedFrom"]]
            lines.append(
                f"- `configEcho.{row['key']}` ({_modes_phrase(row['modes'])}): "
                f"{description}."
            )
    return lines


def identity_body(
    rel_path: str, symbol: str, identity: Mapping[str, Any], repo_root: str
) -> list[str]:
    """Legends differ by file, so dispatch on both the file and the symbol."""
    mark = identity["brand"]["mark"]
    name = identity["brand"]["name"]
    pairs = [(emoji, severity) for severity, emoji in identity["severityEmoji"].items()]
    rule_source_pairs = identity["ruleSourceLabels"].items()
    commas = ", ".join(f"{emoji} {severity}" for emoji, severity in pairs)
    slashes = " / ".join(f"{emoji} {severity}" for emoji, severity in pairs)
    rule_source_labels = ", ".join(
        f"{kind} -> {label}" for kind, label in rule_source_pairs
    )
    key = (rel_path, symbol)
    if key == (REPORT_FORMAT_REL, "full_report_template"):
        return render_template_block(repo_root, identity).split("\n")
    if key == (REPORT_FORMAT_REL, "permalink_sample"):
        return render_permalink_sample(repo_root)
    if symbol == "permalink_formats":
        return permalink_formats_body(identity)
    if symbol == "pr_identity_fields":
        return pr_identity_fields_body(identity)
    if symbol == "summary_header":
        return [f"### {mark} {name}"]
    if symbol == "inline_sample":
        return render_inline_comment_sample(identity).split("\n")
    if symbol == "delivery_identity":
        return [
            f"- **Identity:** prepends `### {mark} {name}` to "
            f"`review_body` and appends "
            f"`{mark} *{name}*` to every rendered comment body — one mark "
            f"per delivered "
            "surface, never one per finding. Never hand-type either."
        ]
    if symbol == "headless_env_table":
        lines = [
            "| Variable | Values | Default |",
            "| --- | --- | --- |",
        ]
        for row in identity["knobs"]:
            if not row["env"]:
                continue
            env_name = row["env"]
            rule = row["rule"]
            if "kind" not in rule:
                rule = rule["headless"]
            kind = rule["kind"]
            if kind == "positive_digits":
                values = "positive integer"
            elif kind == "digits_or_null":
                values = "digits or null"
            else:
                values = ",".join(rule["values"])
            default = row["defaults"]["headless"][0]
            lines.append(f"| `{env_name}` | `{values}` | `{default}` |")
        return lines
    if symbol == "chat_identity":
        return [
            (
                f"The final delivery summary opens with `{mark} {name}` on "
                f"its first line and " + "carries no other"
            ),
            "emoji, except severity emoji when listing findings.",
        ]
    if symbol == "config_receipt":
        resolver = _load_resolver(repo_root)
        docs_identity = {
            "pipeline_version": "{pipeline_version}",
            "plugin_root": "/absolute/path/to/claude-code-gauntlet",
        }
        lines = [
            "The resolver owns the printed configuration block and the "
            "keyed `configEcho` receipt.",
        ]
        for mode in ("interactive", "headless"):
            resolved = resolver.resolve(
                mode, {}, None, "pr", registry=identity["knobs"]
            )
            block = resolver.render_block(
                mode, resolved["configEcho"], docs_identity, registry=identity["knobs"]
            )
            receipt = resolver.serialize_receipt(
                mode, resolved["configEcho"], registry=identity["knobs"]
            )
            lines.extend(
                [
                    "",
                    f"**{mode.title()} block:**",
                    "",
                    "```text",
                    *block.splitlines(),
                    "```",
                    "",
                    f"**{mode.title()} receipt:**",
                    "",
                    "```json",
                    *receipt.splitlines(),
                    "```",
                ]
            )
        return lines
    if symbol == "derived_waist_fields":
        return _derived_waist_body(identity)
    if symbol == "inline_legend":
        return [
            f"`{{emoji}}` is {slashes}, `{{SEVERITY}}` is the normalized "
            f"severity uppercased.",
        ]
    if key == (REPORT_FORMAT_REL, "severity_legend"):
        return [
            f"Product mark: {mark} ({name}). Severity emoji: {commas}.",
            f"Rule source labels: {rule_source_labels}; unknown values -> "
            f"{identity['ruleSourceLabelFallback']}.",
            (
                "Always use the Unicode characters, never GitHub "
                "shortcodes (`:red_circle:`) — " + "shortcodes do"
            ),
            "not render in terminal/chat output.",
        ]
    if symbol == "severity_legend":
        return [
            f"Product mark: {mark} ({name}). Severity emojis: {commas}.",
            f"Rule source labels: {rule_source_labels}; unknown values -> "
            f"{identity['ruleSourceLabelFallback']}.",
        ]
    raise CliError(f"no identity body for {symbol!r} in {rel_path}")


def find_identity_pairs(
    lines: Sequence[str], rel_path: str
) -> dict[str, tuple[int, int]]:
    """Validate per symbol because a file can carry several independent fences."""
    opens: dict[str, int] = {}
    closes: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = _IDENTITY_MARKER_RE.match(line)
        if not match:
            continue
        bucket = closes if match.group("close") else opens
        symbol = match.group("symbol")
        if symbol in bucket:
            raise CliError(
                f"{rel_path}: duplicate {'close' if match.group('close') else 'open'} "
                f"identity marker for {symbol} (lines {bucket[symbol] + 1} and "
                f"{index + 1}) — expected exactly one matched pair per "
                f"symbol; fix by hand"
            )
        bucket[symbol] = index
    unmatched = sorted(set(opens) ^ set(closes))
    if unmatched:
        raise CliError(
            f"{rel_path}: unmatched identity marker(s) for {', '.join(unmatched)} — an "
            "orphaned marker would make --check call the file current "
            "while real debris sits in it; fix by hand"
        )
    pairs: dict[str, tuple[int, int]] = {}
    for symbol, open_index in opens.items():
        close_index = closes[symbol]
        if close_index <= open_index:
            raise CliError(
                f"{rel_path}: close identity marker for {symbol} precedes "
                f"its open marker "
                f"(lines {close_index + 1} and {open_index + 1}); fix by hand"
            )
        pairs[symbol] = (open_index, close_index)
    for symbol, (open_index, close_index) in pairs.items():
        for other, (other_open, _) in pairs.items():
            if other != symbol and open_index < other_open < close_index:
                raise CliError(
                    f"{rel_path}: {other}'s identity fence is nested inside {symbol}'s "
                    f"(lines {open_index + 1}-{close_index + 1}); fix by hand"
                )
    return pairs


def fill_identity_fences(
    text: str, rel_path: str, identity: Mapping[str, Any], repo_root: str
) -> str:
    """Refuse missing/undeclared fences so unmaintained copies cannot ship."""
    lines = text.split("\n")
    pairs = find_identity_pairs(lines, rel_path)
    expected = set(IDENTITY_FENCES[rel_path])
    missing = sorted(expected - set(pairs))
    if missing:
        raise CliError(
            f"{rel_path}: no marker pair for identity symbol(s) {', '.join(missing)} — "
            "place an empty pair where the declaration belongs, then rerun"
        )
    orphans = sorted(set(pairs) - expected)
    if orphans:
        raise CliError(
            f"{rel_path}: identity marker pair(s) {', '.join(orphans)} "
            f"match no declared "
            "symbol — remove the fence or add it to IDENTITY_FENCES"
        )
    for symbol in sorted(pairs, key=lambda s: pairs[s][0], reverse=True):
        open_index, close_index = pairs[symbol]
        lines[open_index + 1 : close_index] = identity_body(
            rel_path, symbol, identity, repo_root
        )
    return "\n".join(lines)


def rendered_targets(
    repo_root: str, registry: Mapping[str, Any], source: str
) -> dict[str, str | Callable[[str], str]]:
    steps: dict[str, list[Callable[[str], str]]] = {}
    for rel_path, sentence in single_dimension_targets(registry).items():
        steps.setdefault(rel_path, []).append(
            partial(splice, anchor_re=_SINGLE_SENTENCE_ANCHOR, body=sentence)
        )
    rows = [
        row
        for row in registry["dimensions"]
        if row["agentType"] == "code-gauntlet:conventions-and-intent"
    ]
    steps.setdefault("agents/conventions-and-intent.md", []).append(
        partial(
            splice,
            anchor_re=_CONDITIONAL_ANCHOR,
            body="\n\n".join(conditional_paragraphs(rows)),
        )
    )
    steps.setdefault(REPORT_FORMAT_REL, []).append(
        partial(rewrite_required_column, registry=registry)
    )
    for rel_path in IDENTITY_FENCES:
        path_steps = steps.setdefault(rel_path, [])
        path_steps.append(
            partial(
                fill_identity_fences,
                rel_path=rel_path,
                identity=registry,
                repo_root=repo_root,
            )
        )

    def apply_steps(text: str, *, transforms: Sequence[Callable[[str], str]]) -> str:
        for transform in transforms:
            text = transform(text)
        return text

    targets: dict[str, str | Callable[[str], str]] = {
        rel_path: partial(apply_steps, transforms=transforms)
        for rel_path, transforms in steps.items()
    }
    targets["scripts/gauntlet/registry.py"] = source
    return targets


@contextmanager
def _rendered_registry_module(source: str) -> Iterator[None]:
    name = "gauntlet.registry"
    module = types.ModuleType(name)
    exec(source, module.__dict__)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def main(args: argparse.Namespace) -> int:
    try:
        registry = load_registry(args.repo_root)
        source = render_python_registry(registry)
        targets = rendered_targets(args.repo_root, registry, source)
        # Repair needs inline composition to use the freshly rendered registry.
        with _rendered_registry_module(source):
            stale = sync_targets(args.repo_root, targets, args.check)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(str(exc)) from exc
    return finish(
        stale,
        args.check,
        current_message="generated registry blocks are current",
        stale_description="stale generated registry blocks",
        command="python3 scripts/generate_contract_requirements.py",
    )


parser = Parser(
    prog="generate_contract_requirements",
    description=(
        "Generate Python registry and Markdown contracts from live workflow sources."
    ),
)
parser.add_argument("--repo-root", default=ENTRY_ROOT)
parser.add_argument(
    "--check",
    action="store_true",
    help="report stale generated blocks without writing",
)
CLI = Command(parser=parser, main=main)
