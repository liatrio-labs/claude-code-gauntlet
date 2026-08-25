#!/usr/bin/env python3
"""Generate the dispatch-requirement sentences in agent contracts from the registry.

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

Two anchor phrases are machine-parsed elsewhere (see `docs/machine-parsed-strings.md` and
`tests/test_dimensions_registry.py`): "required by the dispatch schema" (requiredExtra sense)
and "dimension-conditional dispatch requirement" (requiredWhenDimension sense). Both are baked
into the templates below verbatim — changing their wording here is changing the contract.

Usage:
    python3 scripts/generate_contract_requirements.py           # write the generated blocks
    python3 scripts/generate_contract_requirements.py --check   # exit 1 if any target is stale
"""

import argparse
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MARKER_OPEN = (
    "<!-- generated-from-registry: do not edit; "
    "scripts/generate_contract_requirements.py -->"
)
MARKER_CLOSE = "<!-- /generated-from-registry -->"

REPORT_FORMAT_REL = "skills/code-gauntlet/references/report-format.md"

# English phrasing for fields that carry a dimension-conditional requirement. Not derivable
# from the registry (it has no room for prose nouns) — kept as the one small hand-authored
# table the templates below parameterize on.
_CONDITIONAL_NOUNS = {
    "claude_md_rule": ("rule", "rule"),
    "spec_text": ("spec text", "spec"),
}


def load_registry(repo_root=REPO_ROOT):
    """The live schema declaration, imported from the ESM source (mirrors the test helper)."""
    node_src = (
        "const t = v => typeof v === 'string' ? v : v.type;"
        "import('./workflows/src/registry.js').then(m => console.log(JSON.stringify({"
        "  required: m.FINDING_REQUIRED,"
        "  canonicalFields: Object.keys(m.FINDING_PROP_TYPES),"
        "  dimensions: m.DIMENSIONS.map(d => ({"
        "    dimension: d.dimension, agentType: d.agentType,"
        "    requiredExtra: d.requiredExtra || [],"
        "    requiredWhenDimension: d.requiredWhenDimension || [],"
        "    extraFields: Object.keys(d.schemaExtra || {}),"
        "  })),"
        "})))"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", node_src],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
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
    r"`[a-z_]+`(?: and `[a-z_]+`)? (?:is|are) required by the dispatch schema[^\n]*"
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


def compute_targets(repo_root):
    registry = load_registry(repo_root)
    targets = {}
    for rel_path, sentence in single_dimension_targets(registry).items():
        anchor = _SINGLE_SENTENCE_ANCHOR
        targets[rel_path] = ("splice", anchor, sentence)
    ci_path, ci_body = conventions_and_intent_target(registry)
    targets[ci_path] = ("splice", _CONDITIONAL_ANCHOR, ci_body)
    targets[REPORT_FORMAT_REL] = ("table", None, registry)
    return targets


def apply_targets(repo_root, check_only=False):
    stale = []
    for rel_path, (kind, anchor, payload) in compute_targets(repo_root).items():
        abs_path = os.path.join(repo_root, rel_path)
        with open(abs_path, encoding="utf-8") as handle:
            current = handle.read()
        if kind == "splice":
            expected = splice(current, anchor, payload)
        else:
            expected = rewrite_required_column(current, payload)
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
        print("contract requirement sentences are current")
        return 0
    if args.check:
        sys.stderr.write(
            f"stale generated contract requirements: {', '.join(stale)}\n"
            "run: python3 scripts/generate_contract_requirements.py\n"
        )
        return 1
    print(f"regenerated: {', '.join(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
