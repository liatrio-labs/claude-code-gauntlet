#!/usr/bin/env python3
"""Generate the registry-derived blocks in agent contracts, references, and the delivery renderer.

WHY THIS EXISTS — issue #238.

`workflows/src/registry.js` is the single authority for which fields are dispatch-required
(`requiredExtra`, unconditional) or dimension-conditionally required (`requiredWhenDimension`).
Before this script, the English sentences telling each agent's model about that requirement
were hand-written prose in `agents/*.md`, kept honest only by a pytest equality test that
diffed the prose against the registry. That is the shape this repo's own design rule forbids
("Add more text" is a design smell) applied to itself: the registry could grow a new required
field and nothing would force the matching sentence to exist except a test someone had to run.

This script closes the loop the way `scripts/sync_agent_rules.py` closes AGENTS.md ⇄ CLAUDE.md:
the registry is the source, the sentences are a generated, marker-fenced block, and a freshness
test runs this script in `--check` mode so drift fails the build instead of a hand-authored
lockstep comparison.

The configuration registry is also projected into the generated derived-waist fence. Its
`deriveWhen` and `derivedFrom` descriptions are checked against the live predicates and
provenance fillers before any target is written, so an unknown registry name or unsupported
receipt type fails loudly instead of producing an incomplete instruction.

Two anchor phrases are machine-parsed elsewhere (see `docs/machine-parsed-strings.md` and
`tests/test_dimensions_registry.py`): "required by the dispatch schema" (requiredExtra sense)
and "dimension-conditional dispatch requirement" (requiredWhenDimension sense). Both are baked
into the templates below verbatim — changing their wording here is changing the contract.

Usage:
    python3 scripts/generate_contract_requirements.py           # write the generated blocks
    python3 scripts/generate_contract_requirements.py --check   # exit 1 if any target is stale
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MARKER_OPEN = (
    "<!-- generated-from-registry: do not edit; "
    "scripts/generate_contract_requirements.py -->"
)
MARKER_CLOSE = "<!-- /generated-from-registry -->"

REPORT_FORMAT_REL = "skills/code-gauntlet/references/report-format.md"

_IDENTITY_TAG = "generated-from-registry-identity"
_IDENTITY_HINT = "do not edit; run scripts/generate_contract_requirements.py"
# Recognizes EITHER marker in EITHER comment syntax (Python `#`, Markdown `<!--`), so an
# orphan pair naming a symbol no target declares is reported rather than silently left to rot.
_IDENTITY_MARKER_RE = re.compile(
    rf"^\s*(?:#|<!--)\s*(?P<close>/)?{re.escape(_IDENTITY_TAG)}:(?P<symbol>[A-Za-z0-9_]+)"
)

# {rel_path: [symbol, ...]} — the fences this file must carry, exactly once each.
IDENTITY_FENCES = {
    "scripts/post_review.py": ["constants"],
    "scripts/render_fix_tasks.py": ["constants", "detail_fields"],
    "scripts/resolve_config.py": ["knob_registry"],
    REPORT_FORMAT_REL: [
        "severity_legend",
        "inline_legend",
        "summary_header",
        "inline_sample",
        "full_report_template",
    ],
    "skills/code-gauntlet/references/delivery-guide.md": [
        "severity_legend",
        "summary_header",
        "inline_sample",
        "delivery_identity",
    ],
    # D9's chat convention names the mark in prose. A hand-authored fourth copy would
    # break the one-edit property (a registry edit + a generator run + a hand edit
    # nothing turns red on), so it is generated like the rest.
    "skills/code-gauntlet/SKILL.md": [
        "chat_identity",
        "config_receipt",
        "derived_waist_fields",
    ],
    "skills/code-gauntlet/references/phase2-triage.md": ["derived_waist_fields"],
    "skills/code-gauntlet/references/phase1-preflight.md": ["derived_waist_fields"],
    "skills/code-gauntlet/references/headless-mode.md": ["headless_env_table"],
}

# English phrasing for fields that carry a dimension-conditional requirement. Not derivable
# from the registry (it has no room for prose nouns) — kept as the one small hand-authored
# table the templates below parameterize on.
_CONDITIONAL_NOUNS = {
    "claude_md_rule": ("rule", "rule"),
    "spec_text": ("spec text", "spec"),
}


_NODE_PROGRAM_ROOTS = (
    "workflows/src/registry.js",
    "workflows/src/args.js",
    "workflows/src/renderReport.js",
)
_WORKFLOW_RELATIVE_IMPORT_RE = re.compile(
    r"^\s*(?:import|export)\b.*?\bfrom\s+['\"](\.[^'\"]+)['\"]",
    re.MULTILINE,
)
_PYTHON_FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+import\s+"
    r"(?P<names>[^\n#]+)$",
    re.MULTILINE,
)
_PYTHON_IMPORT_RE = re.compile(
    r"^\s*import\s+(?P<modules>[^\n#]+)$",
    re.MULTILINE,
)
_DYNAMIC_SCRIPT_PATH_RE = re.compile(
    r"os\.path\.join\(\s*repo_root\s*,\s*['\"]scripts['\"]\s*,\s*"
    r"['\"](?P<path>[^'\"]+\.py)['\"]\s*\)",
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _script_module_path(module, repo_root):
    """Resolve a module name to a Python file under scripts/, if it exists."""
    if module == "scripts":
        return None
    if module.startswith("scripts."):
        module = module[len("scripts.") :]
    elif "." in module:
        return None
    rel_path = os.path.join("scripts", *module.split(".")) + ".py"
    if os.path.isfile(os.path.join(repo_root, rel_path)):
        return rel_path
    return None


def _python_imported_script_paths(source, repo_root):
    """Find local Python modules named by absolute or direct-invocation imports."""
    imported = set()
    for match in _PYTHON_FROM_IMPORT_RE.finditer(source):
        module = match.group("module")
        names = [part.strip().split()[0] for part in match.group("names").split(",")]
        if module == "scripts":
            for name in names:
                path = _script_module_path(f"scripts.{name}", repo_root)
                if path:
                    imported.add(path)
        else:
            path = _script_module_path(module, repo_root)
            if path:
                imported.add(path)
    for match in _PYTHON_IMPORT_RE.finditer(source):
        for part in match.group("modules").split(","):
            module = part.strip().split()[0]
            path = _script_module_path(module, repo_root)
            if path:
                imported.add(path)
    for match in _DYNAMIC_SCRIPT_PATH_RE.finditer(source):
        rel_path = os.path.normpath(os.path.join("scripts", match.group("path")))
        if rel_path.startswith("scripts/") and os.path.isfile(
            os.path.join(repo_root, rel_path)
        ):
            imported.add(rel_path)
    return imported


def _python_import_closure(repo_root):
    """Return the generator and every local Python module in its import closure."""
    pending = ["scripts/generate_contract_requirements.py"]
    seen = set()
    while pending:
        rel_path = pending.pop()
        if rel_path in seen:
            continue
        seen.add(rel_path)
        path = os.path.join(repo_root, rel_path)
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for imported in _python_imported_script_paths(source, repo_root):
            if imported not in seen:
                pending.append(imported)
    return seen


def _stderr_tail(stderr):
    """Return a single-line, bounded tail from a failed child process."""
    for line in reversed((stderr or "").splitlines()):
        clean = _CONTROL_RE.sub("", line)
        if clean.strip():
            return clean[-200:]
    return ""


def _node_failure_message(command, stderr=None):
    command_text = " ".join(_CONTROL_RE.sub("", part) for part in command)
    message = "generate_contract_requirements: node 24 command failed: " + command_text
    tail = _stderr_tail(stderr)
    return f"{message}: {tail}" if tail else message


def _run_node(node_src, repo_root):
    """Run one of the generator's Node programs with a concise failure diagnostic."""
    command = ["node", "--input-type=module", "-e", node_src]
    try:
        return subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        raise SystemExit(_node_failure_message(command)) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(_node_failure_message(command, error.stderr)) from None


def _workflow_import_closure(repo_root):
    """Return the relative workflows/src modules used by both Node programs."""
    pending = list(_NODE_PROGRAM_ROOTS)
    seen = set()
    while pending:
        rel_path = pending.pop()
        if rel_path in seen:
            continue
        seen.add(rel_path)
        path = os.path.join(repo_root, rel_path)
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        base = os.path.dirname(rel_path)
        for specifier in _WORKFLOW_RELATIVE_IMPORT_RE.findall(source):
            imported = os.path.normpath(os.path.join(base, specifier))
            if imported not in seen:
                pending.append(imported)
    return seen


def declared_inputs(repo_root=REPO_ROOT):
    """Return generator sources and every local module that can affect its output."""
    return _python_import_closure(repo_root) | _workflow_import_closure(repo_root)


def load_registry(repo_root=REPO_ROOT):
    """Import the live schemas and keep finding and waist required lists distinct."""
    node_src = (
        "Promise.all([import('./workflows/src/registry.js'), import('./workflows/src/args.js')]).then(([m, a]) => console.log(JSON.stringify({"
        "  required: m.FINDING_REQUIRED,"
        "  waistRequired: a.REQUIRED,"
        "  canonicalFields: Object.keys(m.FINDING_PROP_TYPES),"
        "  dimensions: m.DIMENSIONS.map(d => ({"
        "    dimension: d.dimension, agentType: d.agentType,"
        "    requiredExtra: d.requiredExtra || [],"
        "    requiredWhenDimension: d.requiredWhenDimension || [],"
        "    extraFields: Object.keys(d.schemaExtra || {}),"
        "  })),"
        "  brand: { mark: m.BRAND_MARK, name: m.BRAND_NAME },"
        "  severityEmoji: m.SEVERITY_EMOJI,"
        "  severityEmojiFallback: m.SEVERITY_EMOJI_FALLBACK,"
        "  ruleSourceLabels: m.RULE_SOURCE_LABELS,"
        "  ruleSourceLabelFallback: m.RULE_SOURCE_LABEL_FALLBACK,"
        "  agents: m.AGENTS,"
        "  knobs: a.KNOB_REGISTRY.map(d => ({ ...d })),"
        "  knobKeys: a.KNOB_REGISTRY.map(d => Object.keys(d)),"
        "  deriveWhen: Object.fromEntries(Object.entries(a.DERIVE_WHEN).map(([name, d]) => [name, d.describe])),"
        "  derivedFrom: Object.fromEntries(Object.entries(a.DERIVED_FROM).map(([name, d]) => [name, d.describe])),"
        "})))"
    )
    out = _run_node(node_src, repo_root)
    return json.loads(out.stdout)


def agent_name(agent_type):
    """'code-gauntlet:bug-detector' -> 'bug-detector'."""
    return agent_type.split(":", 1)[1]


def dispatch_required_sentence(fields):
    """The requiredExtra-sense sentence for one or more fields on the same agent row.

    Generalized over field count rather than hardcoding either/both wording for exactly
    two — a third field promoted to requiredExtra must not silently render an ungrammatical
    (or, worse, a truncated/wrong) sentence.
    """
    if len(fields) == 1:
        return (
            f"`{fields[0]}` is required by the dispatch schema — a finding without it is "
            "rejected at the StructuredOutput boundary and retried, so it must always be "
            "present."
        )
    backticked = [f"`{f}`" for f in fields]
    if len(backticked) == 2:
        joined = f"{backticked[0]} and {backticked[1]}"
    else:
        joined = ", ".join(backticked[:-1]) + f", and {backticked[-1]}"
    return (
        f"{joined} are required by the dispatch schema — a finding missing any of them is "
        "rejected at the StructuredOutput boundary and retried, so all must always be "
        "present."
    )


def _conditional_paragraph(field, dimension, siblings, all_dims, first_field):
    noun, noun2 = _CONDITIONAL_NOUNS[field]
    lead = (
        f"For {dimension} findings: the `{field}` field MUST be non-null and MUST quote "
        f"the specific {noun}. Findings without a cited {noun2} will be rejected. "
        f"`{field}` is a dimension-conditional dispatch requirement"
    )
    if field == first_field:
        sib = "/".join(siblings)
        dims = ", ".join(all_dims[:-1]) + f", and {all_dims[-1]}"
        body = (
            ". On a dispatch that targets the first-party API directly (no third-party "
            "provider, no gateway), the schema enforces it specifically for findings whose "
            f"dimension is {dimension} — sibling {sib} findings correctly omit it — and this "
            "contract is the enforcement floor on every run, including third-party providers "
            "and gateway sessions where the schema stays flat. This agent's dispatch mixes "
            f"{dims} findings in ONE schema, so a dimension-blind schema requirement (the flat "
            "`requiredExtra` mechanism, which only single-dimension agents can use) was never "
            "an option here — omitting the field on the wrong dimension is correct while "
            "omitting it on this one is a contract violation."
        )
    else:
        body = (
            f", on the same terms as {first_field} above: enforced by the schema for findings "
            f"whose dimension is {dimension} only on a first-party-direct dispatch, and by "
            "this contract as the floor everywhere else — omitting the field on the wrong "
            "dimension is correct while omitting it on this one is a contract violation."
        )
    return lead + body


def conditional_paragraphs(agent_rows):
    """The dimension-conditional paragraphs for one multi-dimension agent, in row order.

    `agent_rows` is the list of that agentType's DIMENSIONS rows (registry order).
    """
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


def wrap_block(body):
    return f"{MARKER_OPEN}\n{body}\n{MARKER_CLOSE}"


_EXISTING_BLOCK = re.compile(
    re.escape(MARKER_OPEN) + r"\n.*?\n" + re.escape(MARKER_CLOSE), re.DOTALL
)

# First-run anchors: the hand-written text this script's block replaces the first time it
# runs on a file that has never been generated. Matched loosely (DOTALL, non-greedy) so a
# reword before this script existed still gets swallowed into the first generated block.
_SINGLE_SENTENCE_ANCHOR = re.compile(
    r"`[a-z_]+`(?:, `[a-z_]+`)*(?:,? and `[a-z_]+`)? (?:is|are) required by the dispatch "
    r"schema[^\n]*"
)
_CONDITIONAL_ANCHOR = re.compile(
    r"For convention findings:.*?\n\nFor intent findings:.*?(?=\n\n)", re.DOTALL
)


def splice(text, anchor_re, body):
    """Replace the existing generated block, or (first run) the anchor text, with `body`.

    Fails loudly rather than silently leaving stale text behind. Two marker counts are
    checked before anything else: more than one MARKER_OPEN (a second block would be
    invisible to the single-block regex below and go stale forever) and an OPEN/CLOSE
    count mismatch (an orphaned marker — hand-edited or typo'd — that would otherwise
    make a subsequent `--check` call the file current while real debris sits in it).
    """
    open_count = text.count(MARKER_OPEN)
    close_count = text.count(MARKER_CLOSE)
    if open_count > 1 or open_count != close_count:
        raise SystemExit(
            f"malformed generated-block markers ({open_count} open, {close_count} close) "
            "— expected exactly one matched pair or none; fix by hand before regenerating"
        )
    new_block = wrap_block(body)
    if open_count == 1:
        replaced, count = _EXISTING_BLOCK.subn(new_block, text, count=1)
        if count != 1:
            raise SystemExit("found MARKER_OPEN but block regex did not match")
        return replaced
    match = anchor_re.search(text)
    if not match:
        raise SystemExit(
            "no generated block and no recognizable anchor text to replace"
        )
    return text[: match.start()] + new_block + text[match.end() :]


def single_dimension_targets(registry):
    """{relative agent path: sentence} for the four single-field/dual-field requiredExtra agents."""
    targets = {}
    for row in registry["dimensions"]:
        fields = row["requiredExtra"]
        if not fields:
            continue
        path = f"agents/{agent_name(row['agentType'])}.md"
        targets[path] = dispatch_required_sentence(fields)
    return targets


def conventions_and_intent_target(registry):
    rows = [
        r
        for r in registry["dimensions"]
        if r["agentType"] == "code-gauntlet:conventions-and-intent"
    ]
    paragraphs = conditional_paragraphs(rows)
    return "agents/conventions-and-intent.md", "\n\n".join(paragraphs)


def known_fields(registry):
    """Every field name the registry declares anywhere — canonical or per-dimension."""
    fields = set(registry["canonicalFields"])
    for row in registry["dimensions"]:
        fields.update(row["extraFields"])
    return fields


def field_required_status(field, registry):
    """'yes' / 'conditional' / 'no' — the tri-state Required column value for `field`."""
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


def rewrite_required_column(text, registry):
    """Rewrite only the Required cell of rows whose first cell names a registry-known field."""
    known = known_fields(registry)
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


# --- identity fences ---------------------------------------------------------
#
# The per-symbol marker-fence idiom of `scripts/generate_filter_patterns.py`
# (`_MARKER_RE` / `find_marker_pairs` / `fill_fences`), applied to the product
# identity declared once in `workflows/src/registry.js`. One file may carry several
# fences, so the pairs are validated per symbol rather than by a whole-file marker
# count.


def identity_marker_lines(symbol, rel_path):
    """The (open, close) marker lines for `symbol`, in `rel_path`'s comment syntax."""
    if rel_path.endswith(".py"):
        return (
            f"# {_IDENTITY_TAG}:{symbol} — {_IDENTITY_HINT}",
            f"# /{_IDENTITY_TAG}:{symbol}",
        )
    return (
        f"<!-- {_IDENTITY_TAG}:{symbol} — {_IDENTITY_HINT} -->",
        f"<!-- /{_IDENTITY_TAG}:{symbol} -->",
    )


def _severity_pairs(identity):
    """[(emoji, severity), ...] in registry declaration order."""
    return [(emoji, name) for name, emoji in identity["severityEmoji"].items()]


def _rule_source_pairs(identity):
    """[(kind, label), ...] in registry declaration order."""
    return list(identity["ruleSourceLabels"].items())


def _python_literal(value, indent=0):
    """Render JSON-safe data as a fully exploded, ruff-stable Python literal."""
    pad = " " * indent
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for item in value:
            rendered = _python_literal(item, indent + 4)
            item_lines = rendered.splitlines()
            lines.append(" " * (indent + 4) + item_lines[0])
            lines.extend(item_lines[1:])
            lines[-1] += ","
        lines.append(pad + "]")
        return "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for key, item in value.items():
            rendered = _python_literal(item, indent + 4)
            item_lines = rendered.splitlines()
            lines.append(
                " " * (indent + 4)
                + json.dumps(str(key), ensure_ascii=False)
                + ": "
                + item_lines[0]
            )
            lines.extend(item_lines[1:])
            lines[-1] += ","
        lines.append(pad + "}")
        return "\n".join(lines)
    raise SystemExit(f"cannot render non-JSON registry value: {value!r}")


def _load_resolver(repo_root):
    """Load resolve_config.py by path under a unique module name."""
    path = os.path.join(repo_root, "scripts", "resolve_config.py")
    module_name = f"_contract_resolver_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load resolver module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _rule_values(row, mode):
    rule = row.get("rule")
    if isinstance(rule, dict) and "kind" not in rule:
        rule = rule.get(mode)
    if not isinstance(rule, dict):
        return "valid value"
    kind = rule.get("kind")
    if kind == "positive_digits":
        return "positive integer"
    if kind == "digits_or_null":
        return "digits or null"
    values = rule.get("values")
    if isinstance(values, list):
        return ",".join(str(value) for value in values)
    return "valid value"


# A placeholder fixture whose every field value is its own placeholder, run through the REAL
# renderer, so the documented template IS the renderer's literal output. The critical finding
# carries every optional field; the other severity examples stay minimal.
_TEMPLATE_FINDING = {
    "id": "{finding.id}",
    "file": "{finding.file}",
    "line_start": "{finding.line_start}",
    "title": "{finding.title}",
    "description": "{finding.description}",
    "confidence": "{finding.confidence}",
    "dimension": "{finding.dimension}",
}

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
            "line_end": "{finding.line_end}",
            "origin": "surfaced",
            "evidence": "{finding.evidence}",
            "suggestion": "{finding.suggestion}",
            "claude_md_rule": "{finding.claude_md_rule}",
            "rule_source": "{finding.rule_source}",
            "spec_text": "{finding.spec_text}",
            "cross_file_refs": "{finding.cross_file_refs}",
            "affected_consumers": "{finding.affected_consumers}",
            "attack_vector": "{finding.attack_vector}",
            "behavior_preserved": "{finding.behavior_preserved}",
            "criticality": "{finding.criticality}",
            "failure_scenario": "{finding.failure_scenario}",
            "hidden_errors": "{finding.hidden_errors}",
            "invalid_state_example": "{finding.invalid_state_example}",
            "challenge_contested": True,
            "corroborations": [
                {
                    "agent": "{corroboration.agent}",
                    "dimension": "{corroboration.dimension}",
                    "confidence": "{corroboration.confidence}",
                    "title": "{corroboration.title}",
                    "description": "{corroboration.description}",
                }
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
        "title": "{pr_title}",
    },
}


def render_template_block(repo_root, identity):
    """Run the placeholder fixture through the real report renderer."""
    fixture = json.loads(json.dumps(_TEMPLATE_FIXTURE))
    fixture["dimensions"]["dispatched"] = identity["agents"]
    node_src = (
        "import('./workflows/src/renderReport.js').then(m => "
        "process.stdout.write(m.renderReport(" + json.dumps(fixture) + ")))"
    )
    out = _run_node(node_src, repo_root)
    return "````markdown\n" + out.stdout + "\n````"


_INLINE_SAMPLE_FINDING = {
    "severity": "severity",
    "title": "{finding.title}",
    "body": "{body}",
    "suggestion": "{suggestion}",
    "claude_md_rule": (
        "{claude_md_rule, falling back to spec_text — blockquoted, one `>` line per source line}"
    ),
    "rule_source": "documented_rule",
    "suggested_fix_code": "{suggested_fix_code}",
}


def render_inline_comment_sample(identity):
    """Render the copyable inline-comment sample through the real Python renderer.

    The finding values are placeholders so the result documents the renderer's shape,
    while the severity map and brand constants are temporarily supplied by *identity*
    for the isolated generator tests. The marker fence surrounds this whole block in
    the reference docs; it therefore cannot make generator control comments part of
    the sample a reader copies.
    """
    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from scripts import post_review

    saved = {
        name: getattr(post_review, name, None)
        for name in (
            "BRAND_MARK",
            "BRAND_NAME",
            "BRAND_TRAILER",
            "SEVERITY_EMOJI",
            "SEVERITY_EMOJI_FALLBACK",
            "RULE_SOURCE_LABELS",
            "RULE_SOURCE_LABEL_FALLBACK",
        )
    }
    try:
        post_review.BRAND_MARK = identity["brand"]["mark"]
        post_review.BRAND_NAME = identity["brand"]["name"]
        post_review.BRAND_TRAILER = (
            f"{post_review.BRAND_MARK} *{post_review.BRAND_NAME}*"
        )
        post_review.SEVERITY_EMOJI = {"severity": "{emoji}"}
        post_review.SEVERITY_EMOJI_FALLBACK = "{emoji}"
        post_review.RULE_SOURCE_LABELS = {"documented_rule": "{rule_source_label}"}
        post_review.RULE_SOURCE_LABEL_FALLBACK = "{rule_source_fallback}"
        rendered = post_review.render_comment_body(_INLINE_SAMPLE_FINDING)
    finally:
        for name, value in saved.items():
            setattr(post_review, name, value)
    return "````markdown\n" + rendered + "\n````"


_DERIVED_WAIST_TYPES = {"string", "csv_list", "int_or_null"}


def _identity_description(identity, table_name, name):
    if table_name not in identity:
        raise SystemExit(f"identity_body: identity lacks a {table_name} key")
    descriptions = identity[table_name]
    if name not in descriptions:
        raise SystemExit(
            f"identity_body: {table_name} has no describe entry for {name!r}"
        )
    description = descriptions[name]
    if not isinstance(description, str) or not description.strip():
        raise SystemExit(
            f"identity_body: {table_name} description for {name!r} is empty or whitespace"
        )
    return description


def _validate_derived_waist_identity(identity):
    """Validate every registry metadata value needed by the derived-waist renderer."""
    for table_name in ("deriveWhen", "derivedFrom", "waistRequired"):
        if table_name not in identity:
            raise SystemExit(f"identity_body: identity lacks a {table_name} key")
    for row in identity["knobs"]:
        row_type = row.get("type")
        if row_type not in _DERIVED_WAIST_TYPES:
            raise SystemExit(
                f"identity_body: row {row.get('key')!r} has unknown type {row_type!r}"
            )
        for table_name in ("deriveWhen", "derivedFrom"):
            name = row.get(table_name)
            if name is not None:
                _identity_description(identity, table_name, name)


def _modes_phrase(modes):
    names = list(modes)
    if len(names) == 1:
        return f"{names[0]} runs"
    if len(names) == 2:
        return f"{names[0]} and {names[1]} runs"
    return f"{', '.join(names[:-1])}, and {names[-1]} runs"


def _derived_waist_instruction(row, waist_required):
    path = row["waistPath"]
    parts = path.split(".")
    if len(parts) == 1:
        return "Leave it out."
    root, leaf = parts[0], parts[-1]
    if root in waist_required:
        return f"Stamp `{root}` and leave `{leaf}` out of it."
    return f"Leave `{leaf}` out of any stamped `{root}`."


def _derived_waist_body(identity):
    _validate_derived_waist_identity(identity)
    rows = identity["knobs"]
    waist_rows = [row for row in rows if row.get("waistPath") is not None]
    derived_rows = [row for row in rows if row.get("derivedFrom") is not None]
    lines = []
    if waist_rows:
        lines.extend(
            [
                "The workflow derives these waist fields from the copied `configEcho` receipt. Do not stamp a derived field; the receipt is its only source.",
                "",
            ]
        )
        for row in waist_rows:
            type_note = ""
            if row["type"] == "int_or_null":
                type_note = "; digits derive as a JSON number"
                if row.get("nullReceipt"):
                    type_note += (
                        f"; on {_modes_phrase(row['nullReceipt'])} the receipt spelling "
                        "`null` derives as JSON `null`"
                    )
            elif row["type"] == "csv_list":
                type_note = "; the comma-separated value derives as a list"
            condition = ""
            if row.get("deriveWhen") is not None:
                condition = ", when " + _identity_description(
                    identity, "deriveWhen", row["deriveWhen"]
                )
            line = (
                f"- `{row['waistPath']}` ({_modes_phrase(row['modes'])}{condition}): "
                f"from `configEcho.{row['key']}`{type_note}"
            )
            if row.get("waistMap") is not None:
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
            description = _identity_description(
                identity, "derivedFrom", row["derivedFrom"]
            )
            lines.append(
                f"- `configEcho.{row['key']}` ({_modes_phrase(row['modes'])}): "
                f"{description}."
            )
    return lines


def identity_body(rel_path, symbol, identity, repo_root=REPO_ROOT):
    """The generated lines for one fence — keyed by BOTH file and symbol.

    `severity_legend` renders differently in report-format.md and delivery-guide.md
    (the first also carries the no-shortcodes rule), so the body is a function of the
    pair, not of the symbol alone.
    """
    mark = identity["brand"]["mark"]
    name = identity["brand"]["name"]
    pairs = _severity_pairs(identity)
    rule_source_pairs = _rule_source_pairs(identity)
    commas = ", ".join(f"{emoji} {severity}" for emoji, severity in pairs)
    slashes = " / ".join(f"{emoji} {severity}" for emoji, severity in pairs)
    rule_source_labels = ", ".join(
        f"{kind} -> {label}" for kind, label in rule_source_pairs
    )
    key = (rel_path, symbol)
    if key == (REPORT_FORMAT_REL, "full_report_template"):
        return render_template_block(repo_root, identity).split("\n")
    if symbol == "constants":
        lines = [
            f'BRAND_MARK = "{mark}"',
            f'BRAND_NAME = "{name}"',
            "SEVERITY_EMOJI = {",
        ]
        lines += [f'    "{severity}": "{emoji}",' for emoji, severity in pairs]
        lines += [
            "}",
            f'SEVERITY_EMOJI_FALLBACK = "{identity["severityEmojiFallback"]}"',
            "RULE_SOURCE_LABELS = {",
            *[f'    "{kind}": "{label}",' for kind, label in rule_source_pairs],
            "}",
            f'RULE_SOURCE_LABEL_FALLBACK = "{identity["ruleSourceLabelFallback"]}"',
        ]
        return lines
    if symbol == "detail_fields":
        citation_fields = {
            field
            for field in ("claude_md_rule", "spec_text")
            if field in identity["canonicalFields"]
        }
        lines = ["_DETAIL_FIELDS_BY_DIMENSION = {"]
        for row in identity["dimensions"]:
            fields = list(row["extraFields"])
            fields.extend(
                field
                for field in row["requiredWhenDimension"]
                if field in citation_fields and field not in fields
            )
            dimension = row["dimension"]
            if not fields:
                lines.append(f'    "{dimension}": (),')
            elif len(fields) == 1:
                lines.append(f'    "{dimension}": ("{fields[0]}",),')
            else:
                lines.append(f'    "{dimension}": (')
                lines.extend(f'        "{field}",' for field in fields)
                lines.append("    ),")
        lines.append("}")
        return lines
    if symbol == "summary_header":
        return [f"### {mark} {name}"]
    if symbol == "inline_sample":
        return render_inline_comment_sample(identity).split("\n")
    if symbol == "delivery_identity":
        return [
            f"- **Identity:** prepends `### {mark} {name}` to `review_body` and appends "
            f"`{mark} *{name}*` to every rendered comment body — one mark per delivered "
            "surface, never one per finding. Never hand-type either."
        ]
    if symbol == "knob_registry":
        return [
            "KNOB_REGISTRY = " + _python_literal(identity["knobs"], indent=0),
        ]
    if symbol == "headless_env_table":
        lines = [
            "| Variable | Values | Default |",
            "| --- | --- | --- |",
        ]
        for row in identity["knobs"]:
            if not row.get("env"):
                continue
            mode = "headless"
            env_name = row["env"]
            values = _rule_values(row, mode)
            default = row.get("defaults", {}).get(mode, ["", ""])[0]
            lines.append(f"| `{env_name}` | `{values}` | `{default}` |")
        return lines
    if symbol == "chat_identity":
        return [
            (
                f"The final delivery summary opens with `{mark} {name}` on its first line and "
                + "carries no other"
            ),
            "emoji, except severity emoji when listing findings.",
        ]
    if symbol == "config_receipt":
        resolver = _load_resolver(repo_root)
        docs_identity = {
            "pipeline_version": "{pipeline_version}",
            "plugin_root": "/absolute/path/to/claude-code-gauntlet",
        }
        rendered = {}
        receipts = {}
        for mode in ("interactive", "headless"):
            resolved = resolver.resolve(
                mode,
                {},
                None,
                "pr",
                registry=identity["knobs"],
            )
            receipts[mode] = resolver.serialize_receipt(
                mode,
                resolved["configEcho"],
                registry=identity["knobs"],
            ).splitlines()
            rendered[mode] = resolver.render_block(
                mode,
                resolved["configEcho"],
                docs_identity,
                registry=identity["knobs"],
            ).splitlines()
        return [
            "The resolver owns the printed configuration block and the keyed `configEcho` receipt.",
            "",
            "**Interactive block:**",
            "",
            "```text",
            *rendered["interactive"],
            "```",
            "",
            "**Interactive receipt:**",
            "",
            "```json",
            *receipts["interactive"],
            "```",
            "",
            "**Headless block:**",
            "",
            "```text",
            *rendered["headless"],
            "```",
            "",
            "**Headless receipt:**",
            "",
            "```json",
            *receipts["headless"],
            "```",
        ]
    if symbol == "derived_waist_fields":
        return _derived_waist_body(identity)
    if symbol == "inline_legend":
        return [
            f"`{{emoji}}` is {slashes}, `{{SEVERITY}}` is the severity uppercased.",
        ]
    if key == (REPORT_FORMAT_REL, "severity_legend"):
        return [
            f"Product mark: {mark} ({name}). Severity emoji: {commas}.",
            f"Rule source labels: {rule_source_labels}; unknown values -> {identity['ruleSourceLabelFallback']}.",
            (
                "Always use the Unicode characters, never GitHub shortcodes (`:red_circle:`) — "
                + "shortcodes do"
            ),
            "not render in terminal/chat output.",
        ]
    if symbol == "severity_legend":
        return [
            f"Product mark: {mark} ({name}). Severity emojis: {commas}.",
            f"Rule source labels: {rule_source_labels}; unknown values -> {identity['ruleSourceLabelFallback']}.",
        ]
    raise SystemExit(
        f"generate_contract_requirements: no identity body for {symbol!r} in {rel_path}"
    )


def find_identity_pairs(lines, rel_path):
    """{symbol: (open_index, close_index)} for every identity marker pair in `lines`.

    Hard-fails PER SYMBOL, not on a whole-file marker count: a file carrying several
    independent pairs is the normal case here. What is malformed: a symbol whose open
    and close markers do not appear exactly once each, in that order, without another
    pair opening in between.
    """
    opens = {}
    closes = {}
    for index, line in enumerate(lines):
        match = _IDENTITY_MARKER_RE.match(line)
        if not match:
            continue
        bucket = closes if match.group("close") else opens
        symbol = match.group("symbol")
        if symbol in bucket:
            raise SystemExit(
                f"{rel_path}: duplicate {'close' if match.group('close') else 'open'} "
                f"identity marker for {symbol} (lines {bucket[symbol] + 1} and "
                f"{index + 1}) — expected exactly one matched pair per symbol; fix by hand"
            )
        bucket[symbol] = index
    unmatched = sorted(set(opens) ^ set(closes))
    if unmatched:
        raise SystemExit(
            f"{rel_path}: unmatched identity marker(s) for {', '.join(unmatched)} — an "
            "orphaned marker would make --check call the file current while real debris "
            "sits in it; fix by hand"
        )
    pairs = {}
    for symbol, open_index in opens.items():
        close_index = closes[symbol]
        if close_index <= open_index:
            raise SystemExit(
                f"{rel_path}: close identity marker for {symbol} precedes its open marker "
                f"(lines {close_index + 1} and {open_index + 1}); fix by hand"
            )
        pairs[symbol] = (open_index, close_index)
    for symbol, (open_index, close_index) in pairs.items():
        for other, (other_open, _) in pairs.items():
            if other != symbol and open_index < other_open < close_index:
                raise SystemExit(
                    f"{rel_path}: {other}'s identity fence is nested inside {symbol}'s "
                    f"(lines {open_index + 1}-{close_index + 1}); fix by hand"
                )
    return pairs


def fill_identity_fences(text, rel_path, identity, repo_root=REPO_ROOT):
    """Rewrite every declared identity fence in `text` from the registry.

    Both directions fail loudly: a declared symbol with no fence (which would silently
    ship an unmaintained hand-written copy) and a fence naming no declared symbol
    (stale debris a later --check would call current).
    """
    lines = text.split("\n")
    pairs = find_identity_pairs(lines, rel_path)
    expected = set(IDENTITY_FENCES[rel_path])
    missing = sorted(expected - set(pairs))
    if missing:
        raise SystemExit(
            f"{rel_path}: no marker pair for identity symbol(s) {', '.join(missing)} — "
            "place an empty pair where the declaration belongs, then rerun"
        )
    orphans = sorted(set(pairs) - expected)
    if orphans:
        raise SystemExit(
            f"{rel_path}: identity marker pair(s) {', '.join(orphans)} match no declared "
            "symbol — remove the fence or add it to IDENTITY_FENCES"
        )
    for symbol in sorted(pairs, key=lambda s: pairs[s][0], reverse=True):
        open_index, close_index = pairs[symbol]
        lines[open_index + 1 : close_index] = identity_body(
            rel_path, symbol, identity, repo_root
        )
    return "\n".join(lines)


def compute_targets(repo_root):
    """{rel_path: [(kind, anchor, payload), ...]} — MANY ops per file, in order.

    One file legitimately carries more than one generated region: report-format.md
    owns both the Required-column rewrite and its identity fences. A one-op-per-file
    mapping would let the second assignment silently destroy the first, and it would
    fail silently — the surviving op keeps the file current, so `--check` stays green
    while the dropped region quietly goes stale.
    """
    registry = load_registry(repo_root)
    targets = {}

    def add(rel_path, op):
        targets.setdefault(rel_path, []).append(op)

    for rel_path, sentence in single_dimension_targets(registry).items():
        add(rel_path, ("splice", _SINGLE_SENTENCE_ANCHOR, sentence))
    ci_path, ci_body = conventions_and_intent_target(registry)
    add(ci_path, ("splice", _CONDITIONAL_ANCHOR, ci_body))
    add(REPORT_FORMAT_REL, ("table", None, registry))
    for rel_path in IDENTITY_FENCES:
        add(rel_path, ("fence", repo_root, registry))
    return targets


def _apply_one(text, rel_path, kind, anchor, payload):
    if kind == "splice":
        return splice(text, anchor, payload)
    if kind == "table":
        return rewrite_required_column(text, payload)
    if kind == "fence":
        return fill_identity_fences(text, rel_path, payload, anchor)
    raise SystemExit(
        f"generate_contract_requirements: unknown target kind {kind!r} for {rel_path}"
    )


def apply_targets(repo_root, check_only=False):
    stale = []
    for rel_path, ops in compute_targets(repo_root).items():
        abs_path = os.path.join(repo_root, rel_path)
        with open(abs_path, encoding="utf-8") as handle:
            current = handle.read()
        expected = current
        for kind, anchor, payload in ops:
            expected = _apply_one(expected, rel_path, kind, anchor, payload)
        if expected == current:
            continue
        stale.append(rel_path)
        if not check_only:
            with open(abs_path, "w", encoding="utf-8") as handle:
                handle.write(expected)
    return stale


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=REPO_ROOT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report stale generated blocks without writing",
    )
    args = parser.parse_args(argv)

    stale = apply_targets(args.repo_root, check_only=args.check)
    if not stale:
        print("generated registry blocks are current")
        return 0
    if args.check:
        sys.stderr.write(
            f"stale generated registry blocks: {', '.join(stale)}\n"
            "run: python3 scripts/generate_contract_requirements.py\n"
        )
        return 1
    print(f"regenerated: {', '.join(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
