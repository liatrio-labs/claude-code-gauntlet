"""Registry ⇄ agent-contract ⇄ documentation lockstep.

`workflows/src/registry.js` declares the WHOLE finding schema — `FINDING_PROP_TYPES`
(canonical), `FINDING_REQUIRED` (the flat required subset) and each `DIMENSIONS` row's
`schemaExtra` (per-dimension). Three other places describe that same schema in prose: the
nine dimension names in agents/AGENTS.md, the field lists there,
and `references/report-format.md`'s Finding Fields Reference tables. A fourth — the seven
discovery agents' `.md` output contracts — tells the models what to emit.

Every one of those can drift from the registry silently, and drift is not cosmetic here: the
discovery item schema is CLOSED (`additionalProperties: false`, issue #53), so a field an agent
contract instructs but the registry does not declare is a schema violation the platform rejects
— the agent burns retries and, if it keeps emitting the field, returns nothing and its whole
dimension degrades. Before #53 the same drift was silent instead: the undeclared field was
simply discarded the instant the agent answered. Either way it never reaches merge, verify,
filter, challenge or report. That is issue #47 — `suggestion` and
`claude_md_rule` (instructed by all 7 contracts), `spec_text` (intent) and
`criticality`/`failure_scenario` (test_coverage) were instructed for the life of the v3
pipeline and declared by nothing, so `report-format.md` marked `suggestion` **required** while
the emission boundary guaranteed its absence. The gap was found by reading a delivered report.

These tests close that loop mechanically, in BOTH directions:

  contract  ⊆ schema   — an instructed field the schema drops fails the build (issue #47)
  schema    ⊆ contract — a declared field nobody emits fails too (dead schema noise, the
                         class registry.js's own comment records having removed once already)

The contract side is parsed EXACTLY, not grepped: each agent's ```json output blocks are
normalised (unquoted `<0-100>`-style placeholders become a scalar) and handed to json.loads.
A block that will not parse raises, naming the file — never a silent skip, which is the one
failure mode that would let this guard quietly stop guarding. A grep for the field NAME would
not do: "suggestion" occurs dozens of times across the repo as an ordinary English word and as
the `report_tag` value, and a prose-frequency check is exactly the kind of guard an adversarial
edit walks around (see CLAUDE.md, "Do not replace a structural property with a phrase count").
"""

import json
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO))

from scripts.filter_findings import _FIELD_RENAMES  # noqa: E402

DELIVERY_GUIDE = REPO / "skills/code-gauntlet/references/delivery-guide.md"

# Key-form scans for the Example-workflow bash fence. Left-boundary on body so
# 'review_body' does not false-positive; description requires a JSON/Python key
# colon so the Bash( description="..." ) kwarg at the fence header stays green.
_BODY_KEY = re.compile(r"(?<![a-zA-Z_])['\"]body['\"]\s*:")
_DESCRIPTION_KEY = re.compile(r"['\"]description['\"]\s*:")

# `origin` is the one canonical field NO agent emits — scripts/verify_findings.py stamps it
# during blame classification. Every other declared canonical field must appear in every
# discovery contract's output block. Adding a name here is a deliberate, reviewable act:
# it says "the pipeline fills this in", not "the contract forgot it".
PIPELINE_STAMPED = {"origin"}

REPORT_FORMAT = REPO / "skills/code-gauntlet/references/report-format.md"

# Each agent's output contract is a fenced ```json block. Both blocks count — the template
# (placeholder values) and the worked example — because a field present in one and missing
# from the other is itself a contract defect worth surfacing.
#
# The fence marker is matched CASE-INSENSITIVELY. An adversarial pass over this guard defeated
# it in one edit: a block fenced ```JSON instructing an undeclared field was invisible to
# `findall`, so `contract_blocks` still found the two lowercase blocks, raised nothing, and the
# whole suite stayed green while the contract instructed a field the schema drops — issue #47
# reproduced with the guard watching. Markdown renders every casing identically, so the model
# sees the block either way. IGNORECASE is scoped to the marker via the inline group; the
# block BODY is captured verbatim and parsed by json.loads, which is case-sensitive as it must be.
_JSON_BLOCK = re.compile(r"```(?i:json)\n(.*?)\n```", re.DOTALL)

# A template block is valid JSON except for UNQUOTED placeholder values: `"confidence": <0-100>`,
# `"line_start": <number>`, `"criticality": <1-10>`. Quoted placeholders ("<one-line summary>")
# are ordinary strings and need no help. The lookbehind pins the match to a VALUE position
# (immediately after the key's colon), so a `<` inside a quoted string is never touched.
_UNQUOTED_PLACEHOLDER = re.compile(r"(?<=:)\s*<[^>]*>")

# A `\uXXXX` escape in a contract block's RAW source. The parse-level guards cannot see these:
# `\u0027` decodes to a clean `'`, so it is invisible the moment json.loads runs. Matching the
# hex digits rather than one literal spelling makes the guard cover the class — `\u0022`,
# `\u0065`, any of them — because what leaks back is the v2 convention, not one character.
# Deliberately over-broad at one edge: an already-escaped backslash followed by `u` — a value
# that really means the literal text \u0027 — matches too. Nothing here needs that spelling,
# and the failure names the file and the escape, so a false positive would be loud and obvious.
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")

# A field row in one of report-format.md's reference tables: the first cell is a single
# backticked field name and nothing else.
_FIELD_ROW = re.compile(r"^\|\s*`([a-z_][a-z0-9_]*)`\s*\|(.+)\|\s*$")

# A backticked lowercase identifier — how the two enumeration bullets name fields.
# Deliberately excludes CamelCase (`schemaExtra`) and dotted names (`verify_findings.py`),
# so ordinary prose on those lines cannot be mistaken for a field.
_BACKTICKED_FIELD = re.compile(r"`([a-z_][a-z0-9_]*)`")

_REGISTRY_CACHE = None


def registry():
    """The live schema declaration, imported from the ESM source (not the built bundle).

    Type values are flattened to their JSON-Schema `type` name so a shorthand ('string') and
    a fragment ({type:'array', items:...}) compare the same way the docs spell them.
    """
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        node_src = (
            "const t = v => typeof v === 'string' ? v : v.type;"
            "import('./workflows/src/registry.js').then(m => console.log(JSON.stringify({"
            "  propTypes: Object.fromEntries(Object.entries(m.FINDING_PROP_TYPES).map(([k, v]) => [k, t(v)])),"
            "  required: m.FINDING_REQUIRED,"
            "  dimensions: m.DIMENSIONS.map(d => ({"
            "    dimension: d.dimension, agentType: d.agentType,"
            "    extras: Object.fromEntries(Object.entries(d.schemaExtra || {}).map(([k, v]) => [k, t(v)])),"
            "    requiredExtra: d.requiredExtra || [],"
            "  })),"
            "})))"
        )
        out = subprocess.run(
            ["node", "--input-type=module", "-e", node_src],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        )
        _REGISTRY_CACHE = json.loads(out.stdout)
    return _REGISTRY_CACHE


def agent_name(agent_type):
    """'code-gauntlet:bug-detector' -> 'bug-detector' (also its agents/<name>.md)."""
    return agent_type.split(":")[-1]


def declared_by_agent():
    """agentType -> {field: type} that agent's DISCOVERY dispatch schema declares.

    Mirrors `findingItemSchema(agentSpecs().schemaExtra)` in stages.js: the canonical map
    unioned with the extras of every dimension that agent covers. A multi-dimension agent
    (conventions-and-intent -> convention/intent/comment_accuracy) dispatches ONCE with the
    union of its rows, so scoping `spec_text` to the `intent` row still declares it on that
    agent's whole dispatch.
    """
    reg = registry()
    out = {}
    for row in reg["dimensions"]:
        out.setdefault(row["agentType"], dict(reg["propTypes"]))
        out[row["agentType"]].update(row["extras"])
    return out


def all_extras():
    """field -> the single dimension that owns it, across every DIMENSIONS row."""
    owner = {}
    for row in registry()["dimensions"]:
        for field in row["extras"]:
            owner[field] = row["dimension"]
    return owner


def all_required_extras():
    """The set of per-dimension fields that are dispatch-required somewhere (issue #66).

    Every field with a non-empty requiredExtra currently lives on a single-dimension agent
    (security, cross_file_impact, test_coverage, simplification), so the row-level set and the
    post-intersection agentSpecs()-effective set agree; this helper reads the row-level
    declaration directly, which is what report-format.md's per-field table documents.
    """
    required = set()
    for row in registry()["dimensions"]:
        required |= set(row["requiredExtra"])
    return required


def field_carries_omit_instruction(raw_block, field, source=None):
    """True if `field`'s own value inside one raw ```json contract block mentions OMIT.

    Scoped to the field's OWN value (not the whole block) so an OMIT instruction on a
    neighbouring field in the same block — e.g. claude_md_rule sitting beside attack_vector —
    can never false-positive this guard. Recognizes every value shape a contract block
    actually uses: a quoted string placeholder (`"field": "<...>"`), an array of quoted
    strings (`"field": ["<...>"]`, the affected_consumers shape — issue #66 promoted an
    ARRAY field and the original version of this guard only handled a scalar, silently
    never inspecting it), an unquoted angle-bracket placeholder (`"field": <1-10>`), and a
    bare JSON literal (`"field": 9`, the criticality worked example).

    A field present in the block whose value matches NONE of those shapes raises, naming
    `source` (the file) and `field`, rather than returning False — this file's own
    convention (see the module docstring): a silent skip is the one failure mode that would
    let this guard quietly stop guarding.
    """
    key_match = re.compile(r'"' + re.escape(field) + r'"\s*:\s*').search(raw_block)
    if not key_match:
        return False
    rest = raw_block[key_match.end() :]

    quoted = re.match(r'"((?:[^"\\]|\\.)*)"', rest)
    if quoted:
        return "omit" in quoted.group(1).lower()

    # First-string-then-comma-separated-rest, not a single starred group with an optional
    # comma: `(?:"…"\s*,?\s*)*` lets the engine partition inter-string whitespace ambiguously
    # and backtrack exponentially on an unclosed array (CodeQL py/redos on PR #217).
    array_of_strings = re.match(
        r'\[\s*(?:"(?:[^"\\]|\\.)*"(?:\s*,\s*"(?:[^"\\]|\\.)*")*\s*)?\]', rest
    )
    if array_of_strings:
        return "omit" in array_of_strings.group(0).lower()

    angle_bracket = re.match(r"<[^>]*>", rest)
    if angle_bracket:
        return "omit" in angle_bracket.group(0).lower()

    bare_literal = re.match(r"(?:-?\d+(?:\.\d+)?|true|false|null)\s*(?=[,}])", rest)
    if bare_literal:
        return False  # a bare number/bool/null cannot carry OMIT prose by construction

    raise AssertionError(
        f"field_carries_omit_instruction: {field!r} in "
        f"{source or 'a contract block'} has a value shape this parser does not recognize "
        "(not a quoted string, array-of-strings, angle-bracket placeholder, or bare "
        f"literal) — extend the parser rather than silently return False: {rest[:80]!r}"
    )


# The exact canonical phrase every requiredExtra field's owning contract must use (F5, issue
# #66) — pinned literally so a paraphrase silently stops matching this guard the same way a
# grep for "suggestion" would (see the module docstring's warning against prose-frequency
# checks). Matched per LINE, not per file, so the backticked field name(s) attributed to the
# claim are exactly the ones sharing that sentence — a phrase on one line making no claim
# about a field named three paragraphs away must not count as a claim about it.
_DISPATCH_REQUIRED_PHRASE = "required by the dispatch schema"


def dispatch_required_claims(name):
    """Field names agents/<name>.md's prose claims are 'required by the dispatch schema'.

    Scans line by line: a line containing the canonical phrase contributes every backticked
    field name ON THAT LINE to the claimed set.
    """
    text = (REPO / "agents" / f"{name}.md").read_text()
    claimed = set()
    for line in text.splitlines():
        if _DISPATCH_REQUIRED_PHRASE in line:
            claimed |= set(_BACKTICKED_FIELD.findall(line))
    return claimed


def raw_contract_blocks(name):
    """Every ```json block in agents/<name>.md, as raw source text.

    Source-level guards read from here and parse-level guards from `contract_blocks`, which
    is built on it — so the two can never disagree about which blocks exist.
    """
    text = (REPO / "agents" / f"{name}.md").read_text()
    raw_blocks = _JSON_BLOCK.findall(text)
    if not raw_blocks:
        raise AssertionError(
            f"agents/{name}.md has no ```json output-contract block — either the contract was "
            "removed or its fence changed, and this whole lockstep guard just stopped covering "
            "that agent. Restore the block or update the parser deliberately."
        )
    return raw_blocks


def contract_blocks(name):
    """Every ```json block in agents/<name>.md, parsed to a dict.

    Raises (rather than skipping) on a block that will not parse: an unparseable output
    contract is itself the defect — it is what the model is shown and told to reproduce.
    """
    blocks = []
    for raw in raw_contract_blocks(name):
        normalized = _UNQUOTED_PLACEHOLDER.sub(" 0", raw)
        try:
            obj = json.loads(normalized)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"agents/{name}.md: a ```json output-contract block is not valid JSON ({exc}). "
                "The block is what the model is shown as the shape to emit, so it must parse. "
                f"Block:\n{raw}"
            ) from exc
        if not isinstance(obj, dict):
            raise AssertionError(
                f"agents/{name}.md: a ```json block is not an object: {raw!r}"
            )
        blocks.append(obj)
    return blocks


def instructed_fields(name):
    """The union of every key across agents/<name>.md's output-contract blocks."""
    keys = set()
    for block in contract_blocks(name):
        keys |= set(block)
    return keys


def report_format_tables():
    """The Finding Fields Reference section's tables, keyed by their ### heading."""
    text = REPORT_FORMAT.read_text()
    match = re.search(
        r"^## Finding Fields Reference\n(.*?)(?=^## )", text, re.DOTALL | re.MULTILINE
    )
    if not match:
        raise AssertionError(
            f"{REPORT_FORMAT.relative_to(REPO)} has no '## Finding Fields Reference' section "
            "(or no following H2 to bound it) — the field documentation this test pins is gone."
        )
    tables, current = {}, None
    for line in match.group(1).splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1)
            tables[current] = []
            continue
        row = _FIELD_ROW.match(line)
        if row and current is not None:
            cells = [c.strip().strip("`") for c in row.group(2).split("|")]
            tables[current].append((row.group(1), cells))
    return tables


def table_named(tables, keyword):
    """The one table whose ### heading contains `keyword`."""
    hits = [h for h in tables if keyword.lower() in h.lower()]
    if len(hits) != 1:
        raise AssertionError(
            f"expected exactly one Finding Fields Reference table whose heading mentions "
            f"{keyword!r}, found {hits} among {list(tables)}"
        )
    return tables[hits[0]]


def claude_md_bullet(anchor):
    """The single agents/AGENTS.md line containing `anchor`."""
    lines = [
        line
        for line in (REPO / "agents" / "AGENTS.md").read_text().splitlines()
        if anchor in line
    ]
    if len(lines) != 1:
        raise AssertionError(
            f"expected exactly one agents/AGENTS.md line containing {anchor!r}, found {len(lines)}"
        )
    return lines[0]


def full_report_template_region(text: str) -> str:
    """Bytes between ## Full Report Template and ## PR Comment Format.

    Heading anchors — not fence-aware. The template fence self-terminates at the
    first bare fence closer (evidence block), so a fence parse would vacuous-green
    the alias-absent scan over most of the template.
    """
    start = re.search(r"^## Full Report Template\s*$", text, re.MULTILINE)
    end = re.search(r"^## PR Comment Format\b", text, re.MULTILINE)
    if start is None or end is None:
        raise AssertionError(
            "report-format.md must contain both '## Full Report Template' and "
            f"'## PR Comment Format' headings — found start={start is not None}, "
            f"end={end is not None}"
        )
    if end.start() <= start.start():
        raise AssertionError(
            "## PR Comment Format must appear after ## Full Report Template"
        )
    return text[start.end() : end.start()]


def delivery_guide_json_object(text: str) -> dict:
    """The exactly-one json fence in delivery-guide.md, parsed."""
    blocks = _JSON_BLOCK.findall(text)
    if len(blocks) != 1:
        raise AssertionError(
            "delivery-guide.md must contain exactly one json fence "
            f"(the findings-schema example); found {len(blocks)}"
        )
    try:
        obj = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"delivery-guide.md's json fence is not valid JSON ({exc}). "
            "Never skip a parse failure — silent skip is how this guard stops guarding."
        ) from exc
    if not isinstance(obj, dict):
        raise AssertionError("delivery-guide.md JSON fence must contain a JSON object.")
    return obj


class TestDimensionsRegistry(unittest.TestCase):
    def test_claude_md_dimension_list_matches_registry(self):
        registry_dims = {d["dimension"] for d in registry()["dimensions"]}
        # agents/AGENTS.md lists the dimensions on the bullet line that
        # contains "short name from agent output", each as a `"name"` token. The
        # leading token is `- `dimension`` (with backticks), so splitting on the
        # bare string "dimension —" would not match — grab the whole line instead.
        line = claude_md_bullet("short name from agent output")
        listed = set(re.findall(r'`"(\w+)"`', line))
        self.assertEqual(
            registry_dims,
            listed,
            f"registry {registry_dims} != agents/AGENTS.md {listed}",
        )

    def test_every_dimension_maps_to_an_agent_contract_file(self):
        for row in registry()["dimensions"]:
            path = REPO / "agents" / f"{agent_name(row['agentType'])}.md"
            self.assertTrue(
                path.is_file(),
                f"dimension {row['dimension']} names {row['agentType']}, "
                f"but {path.relative_to(REPO)} does not exist",
            )

    def test_required_extra_is_a_subset_of_the_rows_own_schema_extra(self):
        # issue #66 / F9: a requiredExtra entry must be a key of THIS row's own schemaExtra —
        # never a canonical FINDING_PROP_TYPES field. Narrowed deliberately: a canonical field
        # is, by definition, already emitted unconditionally by every dimension, so promoting
        # it belongs in FINDING_REQUIRED directly, where the Canonical fields table's
        # Required-column lockstep test actually looks — routed through one row's
        # requiredExtra instead, a canonical promotion would be documented nowhere.
        prop_types = set(registry()["propTypes"])
        for row in registry()["dimensions"]:
            declared = set(row["extras"])
            for field in row["requiredExtra"]:
                self.assertIn(
                    field,
                    declared,
                    f"{row['dimension']}: requiredExtra names {field!r}, which is not a key "
                    "of this row's own schemaExtra",
                )
                self.assertNotIn(
                    field,
                    prop_types,
                    f"{row['dimension']}: requiredExtra names {field!r}, a canonical "
                    "FINDING_PROP_TYPES field — promote it via FINDING_REQUIRED, not "
                    "requiredExtra",
                )

    def test_required_extra_is_disjoint_from_finding_required(self):
        required = set(registry()["required"])
        for row in registry()["dimensions"]:
            for field in row["requiredExtra"]:
                self.assertNotIn(
                    field,
                    required,
                    f"{row['dimension']}: {field!r} is already in FINDING_REQUIRED — "
                    "requiredExtra must not repeat it",
                )


class TestContractSchemaLockstep(unittest.TestCase):
    """The issue #47 guard: what a contract instructs and what the schema declares agree."""

    def test_every_instructed_field_is_schema_declared(self):
        # The #47 direction. Since #53 closed the item schema, an instructed-but-undeclared
        # field is REJECTED at the dispatch boundary on every run: schema retries, and a
        # model that keeps emitting it fails the agent and degrades the dimension.
        offenders = {}
        for agent_type, declared in declared_by_agent().items():
            name = agent_name(agent_type)
            missing = sorted(instructed_fields(name) - set(declared))
            if missing:
                offenders[name] = missing
        self.assertEqual(
            offenders,
            {},
            "agent contracts instruct fields no schema declares — the CLOSED item "
            "schema rejects them at the dispatch boundary, costing schema retries and "
            "risking a terminal agent failure that degrades the dimension. Declare them in "
            f"workflows/src/registry.js (canonical or the owning dimension's "
            f"schemaExtra): {offenders}",
        )

    def test_every_declared_field_is_contract_instructed(self):
        # The reverse direction: schema noise. registry.js records having removed a batch of
        # these once already (type_design encapsulation/invariants/...; simplification
        # before/after) — fields declared for agents that never emitted them.
        offenders = {}
        for agent_type, declared in declared_by_agent().items():
            name = agent_name(agent_type)
            unused = sorted(set(declared) - instructed_fields(name) - PIPELINE_STAMPED)
            if unused:
                offenders[name] = unused
        self.assertEqual(
            offenders,
            {},
            "the schema declares fields the agent contract never instructs — "
            "either instruct them or drop the declaration (pipeline-stamped "
            f"fields belong in PIPELINE_STAMPED): {offenders}",
        )

    def test_pipeline_stamped_fields_are_declared_but_never_instructed(self):
        # Keeps PIPELINE_STAMPED honest: a name parked there to silence the test above must
        # really be declared, and really be emitted by nobody.
        for agent_type, declared in declared_by_agent().items():
            name = agent_name(agent_type)
            for field in PIPELINE_STAMPED:
                self.assertIn(
                    field,
                    declared,
                    f"{field} is listed as pipeline-stamped but {name}'s schema "
                    "does not declare it",
                )
                self.assertNotIn(
                    field,
                    instructed_fields(name),
                    f"{name} instructs {field}, which is listed as "
                    "pipeline-stamped (emitted by the pipeline, not the agent)",
                )

    def test_no_worked_example_emits_null_for_a_declared_field(self):
        # The retry-storm trap. Every property is typed to a SINGLE type (the platform allows
        # no union types), so a null against a `string` field is a hard schema violation that
        # burns StructuredOutput retries. A not-applicable value is OMITTED. This is the
        # structural half of that rule: no worked example may show a null at all.
        offenders = {}
        for agent_type in declared_by_agent():
            name = agent_name(agent_type)
            nulls = sorted(
                {
                    k
                    for block in contract_blocks(name)
                    for k, v in block.items()
                    if v is None
                }
            )
            if nulls:
                offenders[name] = nulls
        self.assertEqual(
            offenders,
            {},
            "a worked example emits null for a schema-declared field. Every "
            "declared property is single-typed, so null is a schema violation "
            "that burns retries — delete the key from the example instead: "
            f"{offenders}",
        )

    def test_no_template_placeholder_offers_a_null_branch(self):
        # The prose half, backstopping the structural check above: a placeholder that tells
        # the model "otherwise null" produces the null the example no longer shows. Narrow by
        # design — it pins the two phrasings that actually shipped ("otherwise null" /
        # "or null"), and the example check above is what makes the rule structural.
        null_branch = re.compile(r"\b(?:otherwise|or)\s*,?\s+null\b", re.IGNORECASE)
        offenders = {}
        for agent_type, declared in declared_by_agent().items():
            name = agent_name(agent_type)
            for block in contract_blocks(name):
                for field, value in block.items():
                    if (
                        field in declared
                        and isinstance(value, str)
                        and null_branch.search(value)
                    ):
                        offenders.setdefault(name, []).append(field)
        self.assertEqual(
            offenders,
            {},
            "an output-contract placeholder still offers a null branch for a "
            "schema-declared field; say 'OMIT this field entirely — never emit "
            f"null' instead: {offenders}",
        )

    def test_a_field_the_example_omits_must_tell_the_model_to_omit_it(self):
        # The positive half of the omit-not-null rule, derived rather than hand-listed: a
        # field the template declares but the worked example leaves out is, by construction,
        # not-always-applicable — so the model needs to be told what to do when it does not
        # apply, and the only safe answer is OMIT (a null against a single-typed property
        # burns StructuredOutput retries). Before issue #47 this held for `hidden_errors` and
        # `invalid_state_example` and failed for `claude_md_rule`, which said "otherwise
        # null" — harmless only because nothing declared it.
        offenders = {}
        for agent_type, declared in declared_by_agent().items():
            name = agent_name(agent_type)
            blocks = contract_blocks(name)
            # The template is the block whose values are `<placeholder>` prose.
            templates = [
                b
                for b in blocks
                if any(isinstance(v, str) and v.startswith("<") for v in b.values())
            ]
            examples = [b for b in blocks if b not in templates]
            if not templates or not examples:
                continue
            for template in templates:
                for field, value in template.items():
                    if field not in declared or not isinstance(value, str):
                        continue
                    if all(field in ex for ex in examples):
                        continue  # always emitted — no omission branch to get wrong
                    if "OMIT this field" not in value:
                        offenders.setdefault(name, []).append(field)
        self.assertEqual(
            offenders,
            {},
            "a schema-declared field is absent from the worked example but its "
            "template placeholder never tells the model to OMIT it when it does "
            f"not apply — that is how a null gets emitted instead: {offenders}",
        )

    def test_no_worked_example_over_escapes_an_apostrophe(self):
        # Found by this guard's own parser on first run: three examples escaped apostrophes
        # the way the v2 printf/NDJSON path required. security-reviewer's `\\'` was not even
        # valid JSON (json.loads rejects the escape), and code-simplifier's and
        # type-design-analyzer's `\\\\'` parsed to a literal backslash-apostrophe, so the
        # example showed the model a mangled value to copy. An apostrophe NEVER needs
        # escaping inside a JSON string, so a surviving backslash-apostrophe in a PARSED
        # value can only be over-escaping. (The invalid form is already caught upstream by
        # contract_blocks refusing to parse; this catches the merely-wrong one.)
        offenders = {}
        for agent_type in declared_by_agent():
            name = agent_name(agent_type)
            for block in contract_blocks(name):
                for field, value in block.items():
                    if isinstance(value, str) and "\\'" in value:
                        offenders.setdefault(name, []).append(field)
        self.assertEqual(
            offenders,
            {},
            "an output-contract example over-escapes an apostrophe — v3 agents "
            "return findings by value through StructuredOutput, so there is no "
            f"shell quoting to escape for: {offenders}",
        )

    def test_no_contract_block_unicode_escapes_a_printable_character(self):
        # The sibling guard above reads PARSED values, so it is blind to this: `\u0027`
        # decodes to a clean `'` and survives every parse-level check. The convention it
        # comes from is still in the tree — the retained v2/bench emission contract spells
        # apostrophes that way — so a contract edit made with that rule in context puts it
        # back, which is exactly how the residue #68 removes got there. Pinned as a class,
        # not as one spelling: any printable ASCII character written as an escape is the
        # same mistake. Scoped to the discovery contracts, so the deliberate `\u0027` in
        # tests/test_validate_ndjson.py and in test_agent_contracts.py's comment stay clear.
        offenders = {}
        for agent_type in declared_by_agent():
            name = agent_name(agent_type)
            for raw in raw_contract_blocks(name):
                for match in _UNICODE_ESCAPE.finditer(raw):
                    char = chr(int(match.group(1), 16))
                    if " " <= char <= "~":
                        offenders.setdefault(name, []).append(
                            f"{match.group(0)} -> {char}"
                        )
        self.assertEqual(
            offenders,
            {},
            "an output-contract block spells a printable character as a unicode "
            "escape. That is the v2 printf/NDJSON emission convention, which still "
            "governs the retained v2/bench surface only — v3 discovery returns "
            "findings by value through StructuredOutput, so there is no shell "
            "quoting to escape for, and this block is the shape the model copies. "
            "Write the character literally — a double-quote and a backslash keep "
            f"their short JSON escapes: {offenders}",
        )

    def test_criticality_is_declared_as_a_number(self):
        # criticality is a 1-10 IMPACT scale sitting next to confidence's 0-100 CERTAINTY
        # scale. Typed `string` it would reach the same string-arithmetic class of bug the
        # confidence pin exists to prevent ("85" + 10 -> "8510").
        self.assertEqual(all_extras().get("criticality"), "test_coverage")
        types = declared_by_agent()["code-gauntlet:test-analyzer"]
        self.assertEqual(
            types["criticality"],
            "number",
            "criticality must be declared number, not string",
        )

    def test_required_extra_fields_carry_no_omit_instruction(self):
        # issue #66's promotion rule: a field may only enter requiredExtra when its owning
        # contract emits it UNCONDITIONALLY. If the contract still tells the model to OMIT the
        # field under some circumstance, the schema-required declaration and the contract
        # prose directly contradict each other — the model would be told two different things
        # about the same field, and whichever branch the OMIT applies to produces a schema
        # violation that burns the platform's capped retries (5) and, on exhaustion, fails
        # the agent terminally and degrades every dimension it owns.
        offenders = []
        for row in registry()["dimensions"]:
            name = agent_name(row["agentType"])
            for field in row["requiredExtra"]:
                for raw in raw_contract_blocks(name):
                    if field_carries_omit_instruction(
                        raw, field, source=f"agents/{name}.md"
                    ):
                        offenders.append(f"{name}.{field} ({row['dimension']})")
        self.assertEqual(
            offenders,
            [],
            "these fields are declared requiredExtra in registry.js but their owning "
            "contract still instructs an OMIT branch for them — either the field is not "
            f"really unconditional (drop it from requiredExtra) or the contract prose is "
            f"stale: {offenders}",
        )

    def test_field_carries_omit_instruction_parses_every_real_value_shape(self):
        # Self-test for field_carries_omit_instruction's parser, synthetic and isolated from
        # the live contracts so it pins the parser's behavior directly rather than through
        # whatever shapes the current .md files happen to contain.
        # Quoted-string value: OMIT text inside the string is detected.
        self.assertTrue(
            field_carries_omit_instruction(
                '{"attack_vector": "<...>. OMIT this field entirely when N/A."}',
                "attack_vector",
            )
        )
        self.assertFalse(
            field_carries_omit_instruction(
                '{"attack_vector": "<always present>"}', "attack_vector"
            )
        )
        # Array-of-strings value (the affected_consumers shape issue #66 promoted): the
        # original version of this parser only matched a scalar and silently never
        # inspected an array-valued field at all.
        self.assertTrue(
            field_carries_omit_instruction(
                '{"affected_consumers": ["<...>. OMIT this field entirely when the impact '
                'is local>"]}',
                "affected_consumers",
            )
        )
        self.assertFalse(
            field_carries_omit_instruction(
                '{"affected_consumers": ["<file paths>"]}', "affected_consumers"
            )
        )
        # Unquoted angle-bracket placeholder (criticality's <1-10> template form).
        self.assertFalse(
            field_carries_omit_instruction('{"criticality": <1-10>}', "criticality")
        )
        self.assertTrue(
            field_carries_omit_instruction(
                '{"criticality": <1-10, OMIT if not applicable>}', "criticality"
            )
        )
        # Bare JSON literal (criticality's worked-example form, "criticality":9) can never
        # carry OMIT prose and must not raise.
        self.assertFalse(
            field_carries_omit_instruction(
                '{"criticality":9,"confidence":90}', "criticality"
            )
        )
        # A field absent from the block entirely is False, not an error.
        self.assertFalse(
            field_carries_omit_instruction('{"other_field": "x"}', "attack_vector")
        )
        # An unhandled value shape (a nested object, which no real contract emits) raises,
        # naming the source and field, rather than silently returning False.
        with self.assertRaises(AssertionError) as ctx:
            field_carries_omit_instruction(
                '{"weird_field": {"nested": "object"}}',
                "weird_field",
                source="agents/x.md",
            )
        self.assertIn("weird_field", str(ctx.exception))
        self.assertIn("agents/x.md", str(ctx.exception))

    def test_field_carries_omit_instruction_array_parse_is_linear_time(self):
        # Regression for the CodeQL py/redos finding fixed on PR #217: the original
        # array-of-strings pattern (`(?:"…"\s*,?\s*)*`) backtracked exponentially because
        # the optional comma let a whitespace run split ambiguously between the two `\s*`s
        # on every iteration. The trigger is therefore a long UNCLOSED array of strings
        # separated by whitespace WITHOUT commas (comma-separated input parses one way and
        # cannot distinguish the two regex forms). The shipped
        # first-string-then-comma-separated-rest form gives up on that input immediately
        # and falls to the loud unrecognized-shape arm; the vulnerable form hangs.
        pathological = '{"affected_consumers": [' + '"a" ' * 200 + "x"
        start = time.perf_counter()
        with self.assertRaises(AssertionError):
            field_carries_omit_instruction(pathological, "affected_consumers")
        self.assertLess(
            time.perf_counter() - start,
            1.0,
            "array-of-strings OMIT parsing took over a second on an unclosed "
            "whitespace-separated array — the backtracking-prone regex form is back",
        )

    def test_dispatch_required_contract_sentences_match_requiredExtra_exactly(self):
        # F5, issue #66: bidirectional lockstep between the registry's requiredExtra and the
        # contract prose sentences claiming a field is dispatch-required.
        #   forward — every requiredExtra field's owning contract claims it (a promotion in
        #             registry.js with no matching sentence is undocumented to the model).
        #   reverse — no OTHER per-dimension field on that row is claimed dispatch-required
        #             (a stale or bogus claim would tell the model something the schema does
        #             not enforce).
        for row in registry()["dimensions"]:
            name = agent_name(row["agentType"])
            claimed = dispatch_required_claims(name)
            required = set(row["requiredExtra"])
            extras = set(row["extras"])

            missing = required - claimed
            self.assertEqual(
                missing,
                set(),
                f"agents/{name}.md never says {sorted(missing)} is "
                f"{_DISPATCH_REQUIRED_PHRASE!r} though registry.js's requiredExtra promotes "
                "it — add the canonical contract sentence",
            )

            over_claimed = (claimed & extras) - required
            self.assertEqual(
                over_claimed,
                set(),
                f"agents/{name}.md claims {sorted(over_claimed)} is "
                f"{_DISPATCH_REQUIRED_PHRASE!r} but registry.js's requiredExtra does not "
                "promote it — stale contract prose",
            )


class TestClaudeMdFieldLists(unittest.TestCase):
    """agents/AGENTS.md's two field enumerations are the human index of the registry."""

    def test_canonical_bullet_matches_registry(self):
        line = claude_md_bullet("**Canonical fields**")
        listed = set(_BACKTICKED_FIELD.findall(line))
        self.assertEqual(
            set(registry()["propTypes"]),
            listed,
            "agents/AGENTS.md's canonical field bullet has drifted from "
            "registry.js FINDING_PROP_TYPES",
        )

    def test_per_dimension_bullet_matches_registry(self):
        line = claude_md_bullet("**Per-dimension extras**")
        listed = set(_BACKTICKED_FIELD.findall(line))
        self.assertEqual(
            set(all_extras()),
            listed,
            "agents/AGENTS.md's per-dimension extras bullet has drifted from "
            "registry.js DIMENSIONS[].schemaExtra",
        )


class TestReportFormatFieldTables(unittest.TestCase):
    """report-format.md's tables promise reviewers a shape; pin them to the real one.

    The table marking `suggestion` **required** while no schema declared it is the delivered
    symptom issue #47 opens with. An unpinned table drifts back there the next time a field
    moves.
    """

    def test_the_section_holds_exactly_the_three_known_tables(self):
        # An adversarial pass walked straight through this guard: a FOURTH table
        # ("### Experimental fields") documenting a fabricated required field was added to the
        # section and every test below stayed green — because each one looks its own table up
        # by heading keyword and never asks what else is in the section. Counting the tables,
        # and accounting for every row below, makes a new table a deliberate edit to this test
        # rather than a quiet addition to the doc.
        tables = report_format_tables()
        self.assertEqual(
            3,
            len(tables),
            "the Finding Fields Reference section must hold exactly the canonical, "
            f"per-dimension and delivery-side tables — found {list(tables)}",
        )

    def test_every_field_row_in_the_section_is_a_field_the_code_knows(self):
        # Heading-agnostic backstop for the same hole: whatever the tables are called, the union
        # of every field they name must be exactly what the registry declares plus the one
        # documented delivery-side field. A row for a field nothing carries is the
        # `suggestion`-marked-required defect all over again.
        tables = report_format_tables()
        documented = {field for rows in tables.values() for field, _ in rows}
        expected = (
            set(registry()["propTypes"]) | set(all_extras()) | {"suggested_fix_code"}
        )
        self.assertEqual(
            expected,
            documented,
            "report-format.md documents a field set that is not "
            "registry \u222a {suggested_fix_code}",
        )

    def test_canonical_table_matches_registry(self):
        rows = table_named(report_format_tables(), "Canonical")
        self.assertEqual(
            set(registry()["propTypes"]),
            {f for f, _ in rows},
            "report-format.md's canonical field table has drifted from "
            "registry.js FINDING_PROP_TYPES",
        )

    def test_canonical_table_types_match_registry(self):
        types = registry()["propTypes"]
        for field, cells in table_named(report_format_tables(), "Canonical"):
            self.assertEqual(
                types[field],
                cells[0],
                f"report-format.md types {field} as {cells[0]!r}, "
                f"registry.js declares {types[field]!r}",
            )

    def test_canonical_table_required_column_matches_registry(self):
        required = set(registry()["required"])
        for field, cells in table_named(report_format_tables(), "Canonical"):
            expected = "yes" if field in required else "no"
            self.assertEqual(
                expected,
                cells[1].lower(),
                f"report-format.md marks {field} required={cells[1]!r}; "
                f"FINDING_REQUIRED says {expected}",
            )

    def test_per_dimension_table_required_column_matches_registry(self):
        # Mirrors test_canonical_table_required_column_matches_registry above, one column
        # index over: the per-dimension table's cells are (Type, Dimension, Required,
        # Description), so the Required column is cells[2].
        required = all_required_extras()
        for field, cells in table_named(report_format_tables(), "Per-dimension"):
            expected = "yes" if field in required else "no"
            self.assertEqual(
                expected,
                cells[2].lower(),
                f"report-format.md marks {field} required={cells[2]!r}; "
                f"registry.js requiredExtra says {expected}",
            )

    def test_per_dimension_table_matches_registry(self):
        rows = table_named(report_format_tables(), "Per-dimension")
        owner = all_extras()
        self.assertEqual(
            set(owner),
            {f for f, _ in rows},
            "report-format.md's per-dimension table has drifted from "
            "registry.js DIMENSIONS[].schemaExtra",
        )
        declared_type = {
            f: t for row in registry()["dimensions"] for f, t in row["extras"].items()
        }
        for field, cells in rows:
            self.assertEqual(
                owner[field],
                cells[1],
                f"report-format.md attributes {field} to {cells[1]!r}, "
                f"registry.js scopes it to {owner[field]!r}",
            )
            # Same pin as the canonical table's Type column. `criticality` is why it matters
            # here: a 1-10 impact scale documented as `string` invites the quoted emission the
            # registry comment and the test-analyzer contract both warn against.
            self.assertEqual(
                declared_type[field],
                cells[0],
                f"report-format.md types {field} as {cells[0]!r}, "
                f"registry.js declares {declared_type[field]!r}",
            )

    def test_delivery_side_table_holds_only_undeclared_fields(self):
        # suggested_fix_code is real on the delivery side (post_review.py renders it) but no
        # agent emits it and no schema declares it. Documenting it inside the pipeline tables
        # is what made it look shipped; documenting it here is the honest place — and this
        # test fails if it ever quietly becomes a declared field without the docs moving.
        rows = table_named(report_format_tables(), "Delivery-side")
        self.assertEqual({"suggested_fix_code"}, {f for f, _ in rows})
        declared = set(registry()["propTypes"]) | set(all_extras())
        for field, _ in rows:
            self.assertNotIn(
                field,
                declared,
                f"{field} is documented as delivery-side-only but the registry "
                "now declares it — move it into the canonical or per-dimension "
                "table and instruct it in the agent contracts",
            )


class TestDeliveryVocabularySurfaces(unittest.TestCase):
    """Pin report vs delivery field vocabulary to `_FIELD_RENAMES`.

    `_FIELD_RENAMES` is underscore-private on `scripts.filter_findings`; this
    import is deliberate — same pattern as other parity pins that reach private
    script surfaces — so a rename-map change fails this guard for the right
    reason instead of both docs drifting together away from code.

    Report markdown uses the canonical name; delivery-guide examples document
    the post_review *read* surface (aliases). The persisted findings.json is a
    union that also carries canonical names — if deliberately changing the
    delivery example to show the union, update this pin and the boundary
    callout together.
    """

    def setUp(self):
        self.assertIn(
            "body",
            _FIELD_RENAMES,
            "_FIELD_RENAMES no longer maps 'body' — this guard has nowhere to "
            "derive the alias pair from",
        )
        self.alias = "body"
        self.canonical = _FIELD_RENAMES["body"]

    def test_full_report_template_uses_canonical_not_alias(self):
        region = full_report_template_region(REPORT_FORMAT.read_text(encoding="utf-8"))
        canonical_token = f"{{finding.{self.canonical}}}"
        alias_token = f"{{finding.{self.alias}}}"
        self.assertIn(
            canonical_token,
            region,
            f"Full Report Template must interpolate {canonical_token} "
            f"(canonical name from _FIELD_RENAMES). Inline PR Comment Format is "
            f"a different, legitimate alias surface — do not 'fix' it into this "
            f"region.",
        )
        self.assertNotIn(
            alias_token,
            region,
            f"Full Report Template must not interpolate {alias_token}; that "
            f"alias belongs to the delivery / inline-comment read surface",
        )

    def test_delivery_guide_json_example_uses_alias_not_canonical(self):
        obj = delivery_guide_json_object(DELIVERY_GUIDE.read_text(encoding="utf-8"))
        self.assertIn(
            "findings",
            obj,
            "delivery-guide findings-schema example must include a 'findings' array",
        )
        findings = obj["findings"]
        self.assertTrue(
            isinstance(findings, list) and findings,
            "delivery-guide findings-schema example's 'findings' must be a non-empty array",
        )
        finding = findings[0]
        self.assertIn(
            self.alias,
            finding,
            f"delivery-guide findings-schema example must include key "
            f"{self.alias!r} (post_review read surface)",
        )
        self.assertNotIn(
            self.canonical,
            finding,
            f"delivery-guide findings-schema example must not include key "
            f"{self.canonical!r}. The example documents the read surface "
            f"(aliases); the persisted union also carries canonical names — if "
            f"deliberately changing the example to show the union, update this "
            f"pin and the boundary callout together.",
        )

    def test_delivery_guide_bash_example_uses_alias_not_canonical(self):
        text = DELIVERY_GUIDE.read_text(encoding="utf-8")
        # Bound loosely to the Example workflow section so we do not scan the
        # whole file's English prose; still structural key-form regexes inside.
        match = re.search(
            r"\*\*Example workflow:\*\*(.*?)(?=^\*\*Script behavior:\*\*)",
            text,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(
            match,
            "delivery-guide.md is missing the Example workflow / Script "
            "behavior anchors the loose bash scan needs",
        )
        region = match.group(1)
        self.assertRegex(
            region,
            _BODY_KEY,
            "Example workflow must assign a finding key 'body'/\"body\" "
            "(alias read surface); regex uses a left boundary so 'review_body' "
            "does not count",
        )
        self.assertIsNone(
            _DESCRIPTION_KEY.search(region),
            "Example workflow must not assign a finding key 'description' "
            f"(canonical belongs to the report surface); matched "
            f"{_DESCRIPTION_KEY.search(region)!r}",
        )


if __name__ == "__main__":
    unittest.main()
