import json
import re
from pathlib import Path

import pytest
from gauntlet import contract_gen as gen
from gauntlet import registry as python_registry
from gauntlet.style import extract_rules

from tests.support.js_values import js_values
from tests.support.repository import tracked_paths

ROOT = Path(__file__).resolve().parents[1]
REPORT_FORMAT = ROOT / "skills/code-gauntlet/references/report-format.md"

RULE_SOURCES = (
    "documented_rule",
    "code_comment",
    "repo_precedent",
    "self_inconsistency",
)

# Fence casing affects Markdown display, so only the marker is case-insensitive.
_JSON_BLOCK = re.compile("```(?i:json)\\n(.*?)\\n```", re.DOTALL)

# Match value positions so a quoted string containing an angle bracket stays intact.
_UNQUOTED_PLACEHOLDER = re.compile("(?<=:)\\s*<[^>]*>")

_FIELD_ROW = re.compile("^\\|\\s*`([a-z_][a-z0-9_]*)`\\s*\\|(.+)\\|\\s*$")

_BACKTICKED_FIELD = re.compile("`([a-z_][a-z0-9_]*)`")


def registry() -> dict:
    projection: dict = js_values()["registryProjection"]
    return projection


def agent_name(agent_type):
    return agent_type.split(":")[-1]


def declared_by_agent():
    reg = registry()
    out = {}
    for row in reg["dimensions"]:
        out.setdefault(row["agentType"], dict(reg["propTypes"]))
        out[row["agentType"]].update(row["extras"])
    return out


def all_extras():
    owner = {}
    for row in registry()["dimensions"]:
        for field in row["extras"]:
            owner[field] = row["dimension"]
    return owner


def field_carries_omit_instruction(raw_block, field, source=None):
    # Scope OMIT to this field so a neighbouring placeholder cannot satisfy the guard.
    key_match = re.compile('"' + re.escape(field) + '"\\s*:\\s*').search(raw_block)
    if not key_match:
        return False
    rest = raw_block[key_match.end() :]
    quoted = re.match('"((?:[^"\\\\]|\\\\.)*)"', rest)
    if quoted:
        return "omit" in quoted.group(1).lower()
    # Mandatory commas keep unclosed whitespace-separated arrays from backtracking.
    array_of_strings = re.match(
        '\\[\\s*(?:"(?:[^"\\\\]|\\\\.)*"(?:\\s*,\\s*"(?:[^"\\\\]|\\\\.)*")*\\s*)?\\]',
        rest,
    )
    if array_of_strings:
        return "omit" in array_of_strings.group(0).lower()
    angle_bracket = re.match("<[^>]*>", rest)
    if angle_bracket:
        return "omit" in angle_bracket.group(0).lower()
    bare_literal = re.match("(?:-?\\d+(?:\\.\\d+)?|true|false|null)\\s*(?=[,}])", rest)
    if bare_literal:
        return False
    raise AssertionError(f"unsupported value for {field!r} in {source}: {rest[:80]!r}")


_DISPATCH_REQUIRED_PHRASE = "required by the dispatch schema"

_DIMENSION_CONDITIONAL_PHRASE = "dimension-conditional dispatch requirement"


def phrase_claims(name, phrase):
    text = (ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8")
    claimed = set()
    for line in text.splitlines():
        if phrase in line:
            claimed |= set(_BACKTICKED_FIELD.findall(line))
    return claimed


def raw_contract_blocks(name):
    text = (ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8")
    raw_blocks = _JSON_BLOCK.findall(text)
    assert raw_blocks
    return raw_blocks


def contract_blocks(name):
    blocks = []
    for raw in raw_contract_blocks(name):
        normalized = _UNQUOTED_PLACEHOLDER.sub(" 0", raw)
        obj = json.loads(normalized)
        assert isinstance(obj, dict)
        blocks.append(obj)
    return blocks


def report_format_tables():
    text = REPORT_FORMAT.read_text(encoding="utf-8")
    match = re.search(
        "^## Finding Fields Reference\\n(.*?)(?=^## )", text, re.DOTALL | re.MULTILINE
    )
    if not match:
        raise AssertionError(
            f"{REPORT_FORMAT.relative_to(ROOT)} has no '## Finding Fields "
            f"Reference' section (or no following H2 to bound it) \u2014 "
            f"the field documentation this test pins is gone."
        )
    tables, current = ({}, None)
    for line in match.group(1).splitlines():
        heading = re.match("^###\\s+(.+?)\\s*$", line)
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
    hits = [h for h in tables if keyword.lower() in h.lower()]
    if len(hits) != 1:
        raise AssertionError(
            f"expected exactly one Finding Fields Reference table whose "
            f"heading mentions {keyword!r}, found {hits} among "
            f"{list(tables)}"
        )
    return tables[hits[0]]


def delivery_guide_json_object(text: str) -> dict:
    blocks = _JSON_BLOCK.findall(text)
    assert len(blocks) == 1
    obj = json.loads(blocks[0])
    assert isinstance(obj, dict)
    return obj


_ROW = re.compile(
    "^\\|\\s*`([^`]+)`\\s*\\|\\s*([^|]+)\\|\\s*([^|]+)\\|\\s*([^|]*)\\|\\s*$"
)

_COMPOSITE_ROW = re.compile(
    "^\\|\\s*((?:`[^`]+`\\s*/\\s*)+`[^`]+`)\\s*\\|\\s*([^|]+)\\|\\s*([^|]+)"
    "\\|\\s*([^|]*)\\|\\s*$"
)

NO_PARSER = "\u2014"


def strip_ticks(token):
    token = token.strip()
    return token[1:-1] if token.startswith("`") and token.endswith("`") else token


def path_cells(producers, parsers, notes):
    return {
        "producers": [strip_ticks(p) for p in producers.split(",") if p.strip()],
        "parsers": [strip_ticks(p) for p in parsers.split(",") if p.strip()],
        "notes": notes,
    }


def parse_registry(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        string, producers, parsers, notes = (c.strip() for c in m.groups())
        if string == "string" or set(string) <= {"-", " "}:
            continue
        rows.append({"string": string, **path_cells(producers, parsers, notes)})
    return rows


def parse_composite_rows(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = _COMPOSITE_ROW.match(line)
        if not m:
            continue
        string_cell, producers, parsers, notes = (c.strip() for c in m.groups())
        rows.append(
            {
                "strings": re.findall("`([^`]+)`", string_cell),
                **path_cells(producers, parsers, notes),
            }
        )
    return rows


DISCOVERY = (
    "bug-detector",
    "security-reviewer",
    "cross-file-impact",
    "test-analyzer",
    "conventions-and-intent",
    "type-design-analyzer",
    "code-simplifier",
)
READERS = (*DISCOVERY, "validator", "challenger", "change-summarizer")
READ_CANON = "skills/code-gauntlet/references/complete-read-contract.md"
READ_MARKER = (
    "<!-- Canonical source: references/complete-read-contract.md — keep all "
    "agent copies in sync -->"
)


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def tracked(*patterns: str) -> list[str]:
    return tracked_paths(ROOT, patterns)


@pytest.mark.parametrize("agent_type", sorted(declared_by_agent()), ids=agent_name)
def test_contract_schema(agent_type: str) -> None:
    declared = declared_by_agent()[agent_type]
    name = agent_name(agent_type)
    text = read(f"agents/{name}.md")
    blocks = contract_blocks(name)
    fields = {key for block in blocks for key in block}
    assert fields == set(declared) - {"origin"}
    assert "origin" in declared
    templates = [
        block
        for block in blocks
        if any(
            isinstance(value, str) and value.startswith("<") for value in block.values()
        )
    ]
    examples = [block for block in blocks if block not in templates]
    for block in blocks:
        assert all(value is not None for value in block.values())
        for value in block.values():
            if isinstance(value, str):
                assert not re.search(
                    r"\b(?:otherwise|or)\s*,?\s+null\b", value, re.IGNORECASE
                )
                assert "\\'" not in value
    for field in (set(all_extras()) | {"claude_md_rule"}) & declared.keys():
        if declared[field] == "string":
            assert not re.search(r'"' + re.escape(field) + r'"\s*:\s*null', text), (
                agent_type,
                field,
            )
            if field in text:
                assert "otherwise null" not in text.split(field, 1)[1][:120], (
                    agent_type,
                    field,
                )
    for template in templates:
        for field, value in template.items():
            if (
                isinstance(value, str)
                and examples
                and not all(field in example for example in examples)
            ):
                assert "OMIT this field" in value
    for row in registry()["dimensions"]:
        if row["agentType"] != agent_type:
            continue
        assert phrase_claims(name, _DISPATCH_REQUIRED_PHRASE) & set(
            row["extras"]
        ) == set(row["requiredExtra"])
        for field in row["requiredExtra"]:
            assert not any(
                field_carries_omit_instruction(raw, field, source=name)
                for raw in raw_contract_blocks(name)
            )
        for field in row["requiredWhenDimension"]:
            assert any(
                field_carries_omit_instruction(raw, field, source=name)
                for raw in raw_contract_blocks(name)
            )
            assert f"REQUIRED for {row['dimension']}" in text
    expected_conditional = {
        field
        for row in registry()["dimensions"]
        if row["agentType"] == agent_type
        for field in row["requiredWhenDimension"]
    }
    assert phrase_claims(name, _DIMENSION_CONDITIONAL_PHRASE) == expected_conditional


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"field": "<OMIT this field when inapplicable>"}', True),
        ('{"field": "live", "neighbour": "<OMIT this field>"}', False),
    ],
)
def test_omit_parser(raw, expected):
    assert field_carries_omit_instruction(raw, "field") is expected


def test_convention_vocabulary() -> None:
    assert _DISPATCH_REQUIRED_PHRASE not in _DIMENSION_CONDITIONAL_PHRASE
    text = read("agents/conventions-and-intent.md")
    section = re.search(
        r"^### Convention output requirements\n(.*?)(?=^## )",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert section is not None
    values = re.search(r"^Allowed values are (.+)\.$", section.group(1), re.MULTILINE)
    assert values is not None
    assert set(re.findall(r"`([^`]+)`", values.group(1))) == set(RULE_SOURCES)
    examples = [
        block
        for block in contract_blocks("conventions-and-intent")
        if not any(
            isinstance(value, str) and value.startswith("<") for value in block.values()
        )
    ]
    assert len(examples) == 1
    assert examples[0]["rule_source"] in RULE_SOURCES


def test_handwritten_field_tables():
    tables = report_format_tables()
    assert len(tables) == 2
    canonical = table_named(tables, "Canonical")
    per_dimension = table_named(tables, "Per-dimension")
    assert {field for field, cells in canonical} == set(registry()["propTypes"])
    assert {field for field, cells in per_dimension} == set(all_extras())
    required = set(registry()["required"])
    extra_required = {
        field for row in registry()["dimensions"] for field in row["requiredExtra"]
    }
    conditional = {
        field
        for row in registry()["dimensions"]
        for field in row["requiredWhenDimension"]
    }
    for field, cells in canonical:
        assert cells[0] == registry()["propTypes"][field]
        expected = (
            "yes"
            if field in required
            else "conditional"
            if field in conditional
            else "no"
        )
        assert cells[1].lower() == expected
    for field, cells in per_dimension:
        row = next(row for row in registry()["dimensions"] if field in row["extras"])
        assert cells[0] == row["extras"][field]
        assert cells[1] == row["dimension"]
        expected = (
            "yes"
            if field in extra_required
            else "conditional"
            if field in conditional
            else "no"
        )
        assert cells[2].lower() == expected


@pytest.mark.parametrize("surface", ["delivery-json", "delivery-bash"])
def test_delivery_vocabulary(surface):
    assert js_values()["FIELD_RENAMES"]["body"] == "description"
    text = read("skills/code-gauntlet/references/delivery-guide.md")
    if surface == "delivery-json":
        obj = delivery_guide_json_object(text)
        assert obj["findings"]
        assert "body" in obj["findings"][0]
        assert "description" not in obj["findings"][0]
    else:
        match = re.search(
            r"\*\*Example workflow:\*\*(.*?)(?=^\*\*Script behavior:\*\*)",
            text,
            re.MULTILINE | re.DOTALL,
        )
        assert match is not None
        assert re.search(r"(?<![a-zA-Z_])['\"]body['\"]\s*:", match.group(1))
        assert not re.search(r"['\"]description['\"]\s*:", match.group(1))


def test_identity_owner() -> None:
    text = read("skills/code-gauntlet/references/phase2-triage.md")
    marker = re.search(
        r"^<!-- generated-from-registry-identity:pr_identity_fields\b",
        text,
        re.MULTILINE,
    )
    assert marker is not None
    assert (
        text.index("## Args Preparation")
        < marker.start()
        < text.index("**`policy` (model policy the workflow runs under):**")
    )


def test_marker_discovery():
    discovered = {}
    marker = re.compile(
        r"^\s*(?:#|<!--)\s*/?generated-from-registry-identity:([A-Za-z0-9_]+)"
    )
    for rel in tracked():
        try:
            text = read(rel)
        except (OSError, UnicodeError):
            continue
        for line in text.splitlines():
            match = marker.match(line)
            if match:
                discovered.setdefault(rel, set()).add(match.group(1))
    assert discovered == {
        rel: set(symbols) for rel, symbols in gen.IDENTITY_FENCES.items()
    }


def test_js_identity():
    identity = gen.load_registry(str(ROOT))
    assert tuple(map(ord, identity["brand"]["mark"])) == (0x2694, 0xFE0F)
    assert identity["brand"]["mark"] != "\u26a0\ufe0f"
    assert identity["brand"]["mark"] not in identity["severityEmoji"].values()
    assert list(identity["severityEmoji"]) == js_values()["SEVERITY_ORDER"]
    for rel in ("workflows/src/registry.js", "workflows/pipeline.js"):
        assert identity["brand"]["mark"][0] not in read(rel)
    with pytest.raises(TypeError):
        python_registry.ARTIFACT_PATH_TEMPLATES["findings"] = "elsewhere-{sha}.json"


def test_emoji_allowlist():
    root = ROOT / "skills/code-gauntlet"
    offenders = set()
    marker = re.compile(r"^\s*<!--\s*(/)?generated-from-registry-identity:")
    for path in [root / "SKILL.md", *sorted((root / "references").glob("*.md"))]:
        inside = False
        for line in path.read_text(encoding="utf-8").splitlines():
            match = marker.match(line)
            if match:
                inside = not bool(match.group(1))
            elif not inside and any(
                ord(char) >= 0x1F000
                or (
                    ord(char) < 0x10000
                    and index + 1 < len(line)
                    and ord(line[index + 1]) == 0xFE0F
                )
                for index, char in enumerate(line)
            ):
                offenders.add((path.relative_to(ROOT).as_posix(), line.strip()))
    assert offenders == set()


def test_machine_string_registry():
    text = read("docs/machine-parsed-strings.md")
    rows = parse_registry(text)
    assert {
        "Headless config:",
        "code-gauntlet-findings",
        "Generated by code-gauntlet",
        "Reviewed up to:",
        "HEADLESS CONFIG ERROR:",
        "HEADLESS INPUT ERROR:",
        "code-gauntlet v3 requires Claude Code >= 2.1.154 with dynamic "
        "workflows. Install the pre-rename deep-review v2.x for older CLIs.",
        "the deltas carry a checksum",
        "PAYLOAD_JSON:",
        'PRODUCT = "code-gauntlet"',
    } <= {row["string"] for row in rows}
    for row in rows:
        assert row["producers"] and row["parsers"]
        if NO_PARSER in row["parsers"]:
            assert row["parsers"] == [NO_PARSER] and row["notes"]
        for rel in row["producers"] + [
            rel for rel in row["parsers"] if rel != NO_PARSER
        ]:
            assert row["string"] in read(rel)
    composite = [
        row
        for row in parse_composite_rows(text)
        if set(row["strings"]) == set(RULE_SOURCES)
    ]
    assert len(composite) == 1
    assert composite[0]["producers"] == ["agents/conventions-and-intent.md"]
    assert composite[0]["parsers"] == [
        "workflows/src/renderReport.js",
        "scripts/gauntlet/delivery/compose.py",
        "bench/runner/citations.py",
    ]


def test_machine_parser():
    assert parse_registry(
        "| `string` | producer path(s) | parser path(s) | notes |\n"
        "| `---` | --- | --- | --- |\nunrelated text\n"
        "| `Tok` | `a.py`, `b.py` | `\u2014` | honest empty |\n"
    ) == [
        {
            "string": "Tok",
            "producers": ["a.py", "b.py"],
            "parsers": ["\u2014"],
            "notes": "honest empty",
        }
    ]


def test_agent_manifest():
    manifest = json.loads(read(".claude-plugin/plugin.json"))
    assert manifest["agents"] == [
        f"./agents/{path.name}"
        for path in sorted((ROOT / "agents").rglob("*.md"))
        if path.name != "AGENTS.md"
    ]


def test_agent_emission_contract():
    for name in DISCOVERY:
        text = read(f"agents/{name}.md")
        assert not re.search(r"printf|ndjson|Bash", text, re.IGNORECASE)
        assert "by-value return" in text
        assert "{ findings, complete, total_seen }" in text
        assert "False-positive exclusions" in text
    for rel in tracked("AGENTS.md", "*/AGENTS.md"):
        assert not re.search(r"printf|ndjson", read(rel), re.IGNORECASE)
    rules = read("agents/AGENTS.md")
    assert "by value" in rules and "{ findings, complete," in rules
    assert not re.search("Bash", rules, re.IGNORECASE)
    assert "Bash" in read("agents/executor.md")


@pytest.mark.parametrize("mirror", ["complete-read", "injection-artifacts"])
def test_agent_mirrors(mirror):
    if mirror == "complete-read":
        canonical = read(READ_CANON)
        block = (
            canonical.split("<!-- BEGIN CANONICAL BLOCK -->")[1]
            .split("<!-- END CANONICAL BLOCK -->")[0]
            .strip("\n")
        )
        assert (
            "When your dispatch prompt names a shared context file, it is "
            "mandatory reading in full" in block
        )
        assert not re.search(r"printf|ndjson|Bash", block, re.IGNORECASE)
        for name in READERS:
            assert f"`agents/{name}.md`" in canonical
            text = read(f"agents/{name}.md")
            assert text.count(READ_MARKER) == 1
            assert block in text
    else:
        blocks = []
        for name in DISCOVERY:
            text = read(f"agents/{name}.md")
            start = text.index("**Prompt injection artifacts.**")
            blocks.append(text[start : text.index(READ_MARKER, start)].rstrip("\n"))
        assert all(block == blocks[0] for block in blocks)
        sentence = (
            "A convention finding grounded in an in-repo precedent names the "
            "files that establish the pattern; a preference with no named "
            "precedent stays excluded."
        )
        assert sentence in read(
            "skills/code-gauntlet/references/false-positive-exclusions.md"
        )
        assert sentence in read("agents/conventions-and-intent.md")


@pytest.mark.parametrize(
    ("rel", "needles"),
    [
        pytest.param(
            "skills/code-gauntlet/SKILL.md",
            ("contextLines", "contextChars", "policy.gateway"),
            id="skill-waist-instructions",
        ),
        pytest.param(
            "skills/code-gauntlet/references/phase2-triage.md",
            ("contextLines", "contextChars", "policy.gateway"),
            id="triage-waist-instructions",
        ),
    ],
)
def test_waist_consumers(rel: str, needles: tuple[str, ...]) -> None:
    text = read(rel)
    assert all(needle in text for needle in needles)


def test_triage_dispatch():
    section = re.split(
        r"(?m)^(?:---|## )",
        read("skills/code-gauntlet/references/phase2-triage.md").split("## 2l.", 1)[1],
        maxsplit=1,
    )[0]
    assert "deriveAgentFlags" in section
    assert "REVIEW.md" not in section


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize(
    "summary",
    [
        pytest.param(
            "9 reported issues from 10 findings after the gauntlet.\n\n- "
            "index entry\n\n3 more reported issues not listed here (over "
            "the delivery cap of 6 findings).",
            id="findings",
        ),
    ],
)
def test_summary_is_not_benchmark_candidate(platform, summary):
    from bench.adapter.adapt import payload_to_candidates

    payload = (
        {
            "payload": {
                "body": summary,
                "comments": [{"body": "inline", "path": "a.js", "line": 1}],
            }
        }
        if platform == "github"
        else {
            "summary": {"body": summary},
            "discussions": [
                {"body": "inline", "position": {"new_path": "a.js", "new_line": 1}}
            ],
        }
    )
    result, stats = payload_to_candidates({"platform": platform, **payload}, "fixture")
    assert result == {
        "fixture": {
            "deep-review": [
                {"text": "inline", "path": "a.js", "line": 1, "source": "extracted"}
            ]
        }
    }
    assert stats == {"n_candidates": 1, "n_skipped": 0}


@pytest.mark.parametrize("source", ["wording-rules.md", "cadence-rules.md"])
def test_style_source_contract(source: str) -> None:
    assert extract_rules(read("docs/style/" + source), source)


def test_mark_producer_registry():
    rows = parse_registry(read("docs/machine-parsed-strings.md"))
    marked = [row for row in rows if row["string"] == "\u2694\ufe0f"]
    assert len(marked) == 1
    # Discover producer bytes independently so an added mirror cannot under-list itself.
    producers = {"scripts/gauntlet/registry.py"}
    for rel in tracked(
        "skills/code-gauntlet/*.md", "skills/code-gauntlet/references/*.md"
    ):
        text = read(rel)
        bodies = re.findall(
            "<!-- generated-from-registry-identity:[^\\n]*\\n(.*?)<!-- "
            "/generated-from-registry-identity:[^\\n]*",
            text,
            re.DOTALL,
        )
        if any("\u2694\ufe0f" in body for body in bodies):
            producers.add(rel)
    assert "\u2694\ufe0f" in read("scripts/gauntlet/registry.py")
    assert set(marked[0]["producers"]) == producers


@pytest.mark.parametrize("surface", ["rendered", "handwritten-remainder"])
def test_full_report_predicates(surface):
    if surface == "rendered":
        text = gen.render_template_block(str(ROOT), gen.load_registry(str(ROOT)))
        assert "{finding.description}" in text
    else:
        match = re.search(
            r"^## Full Report Template\n(.*?)(?=^## PR Comment Format\b)",
            read("skills/code-gauntlet/references/report-format.md"),
            re.MULTILINE | re.DOTALL,
        )
        assert match is not None
        text, count = re.subn(
            "<!-- generated-from-registry-identity:full_report_template[^\\"
            "n]*\\n.*?<!-- /generated-from-registry-identity:full_report_template -->",
            "",
            match.group(1),
            flags=re.DOTALL,
        )
        assert count == 1
    assert "{finding.body}" not in text
    assert "suggested_fix_code" not in text
    assert "not apply-checked" not in text
