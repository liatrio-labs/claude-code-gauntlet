"""Contract rendering and the developer command's generated-target boundary."""

import re
import shutil
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from gauntlet import contract_gen as gen
from gauntlet.cli import CliError

from tests.support.generator_inputs import declared_inputs

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    "agents/security-reviewer.md",
    "agents/cross-file-impact.md",
    "agents/test-analyzer.md",
    "agents/code-simplifier.md",
    "agents/conventions-and-intent.md",
    "skills/code-gauntlet/references/report-format.md",
    "skills/code-gauntlet/references/delivery-guide.md",
    "skills/code-gauntlet/SKILL.md",
    "skills/code-gauntlet/references/phase2-triage.md",
    "skills/code-gauntlet/references/phase1-preflight.md",
    "skills/code-gauntlet/references/headless-mode.md",
    "skills/code-gauntlet/references/phase8-delivery.md",
    "scripts/gauntlet/registry.py",
)


@pytest.fixture
def registry_tree(tmp_path):
    for rel in set(TARGETS) | declared_inputs(str(ROOT)):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / rel).read_bytes())
    return tmp_path


IDENTITY = {
    "brand": {"mark": "MARK", "name": "NAME"},
    "severityEmoji": {"critical": "C", "low": "L"},
    "ruleSourceLabels": {
        "documented_rule": "DR",
        "code_comment": "CC",
        "repo_precedent": "RP",
        "self_inconsistency": "SI",
    },
    "ruleSourceLabelFallback": "RF",
    "deriveWhen": {"gamma": "the gamma condition holds"},
    "derivedFrom": {"delta": "the delta source is present"},
    "waistRequired": ["nested", "limits"],
    "prIdentityFields": [
        {"name": "owner", "required": True, "describe": "a non-empty string"},
        {
            "name": "repo",
            "required": True,
            "describe": "a non-empty string",
        },
        {
            "name": "pr_number",
            "required": True,
            "describe": "a positive safe integer",
        },
        {
            "name": "sha_full",
            "required": True,
            "describe": "a 40-character lowercase hex commit id",
        },
        {
            "name": "platform",
            "required": True,
            "describe": "one of github, gitlab",
        },
        {
            "name": "web_origin",
            "required": True,
            "describe": "an http(s) origin: scheme, host and optional port only",
        },
        {
            "name": "title",
            "required": False,
            "describe": "a non-empty string when present",
        },
    ],
    "permalinkTemplates": {
        "github": {
            "blob": "{origin}/{owner}/{repo}/blob/{sha}/{path}",
            "line": "#L{start}",
            "range": "#L{start}-L{end}",
            "ref": "#{number}",
            "refUrl": "{origin}/{owner}/{repo}/pull/{number}",
        },
        "gitlab": {
            "blob": "{origin}/{owner}/{repo}/-/blob/{sha}/{path}",
            "line": "#L{start}",
            "range": "#L{start}-{end}",
            "ref": "!{number}",
            "refUrl": "{origin}/{owner}/{repo}/-/merge_requests/{number}",
        },
    },
    "knobs": [
        {
            "key": "alpha",
            "modes": ["headless", "interactive"],
            "allowedSources": {
                "headless": ["env", "default"],
                "interactive": ["fixed"],
            },
            "rule": {
                "headless": {"kind": "enum", "values": ["h-alpha"]},
                "interactive": {"kind": "enum", "values": ["i-alpha"]},
            },
            "env": "CODE_GAUNTLET_ALPHA",
            "reviewMdKey": None,
            "defaults": {
                "headless": ["h-alpha", "env"],
                "interactive": ["i-alpha", "fixed"],
            },
            "type": "string",
            "waistPath": None,
            "waistMap": None,
            "derivedFrom": None,
            "deriveWhen": None,
            "nullReceipt": [],
            "resolvedKey": True,
        },
        {
            "key": "beta",
            "modes": ["headless"],
            "allowedSources": {"headless": ["default"]},
            "rule": {"kind": "enum", "values": ["h-beta"]},
            "env": "CODE_GAUNTLET_BETA",
            "reviewMdKey": None,
            "defaults": {"headless": ["h-beta", "default"]},
            "type": "string",
            "waistPath": "optional.beta",
            "waistMap": None,
            "derivedFrom": None,
            "deriveWhen": None,
            "nullReceipt": ["headless"],
            "resolvedKey": False,
        },
        {
            "key": "gamma",
            "modes": ["headless", "interactive"],
            "allowedSources": {
                "headless": ["default"],
                "interactive": ["default"],
            },
            "rule": {"kind": "enum", "values": ["raw"]},
            "env": None,
            "reviewMdKey": None,
            "defaults": {
                "headless": ["raw", "default"],
                "interactive": ["raw", "default"],
            },
            "type": "string",
            "waistPath": "nested.gamma",
            "waistMap": {"raw": "mapped"},
            "derivedFrom": None,
            "deriveWhen": "gamma",
            "nullReceipt": [],
            "resolvedKey": False,
        },
        {
            "key": "epsilon",
            "modes": ["interactive"],
            "allowedSources": {"interactive": ["default"]},
            "rule": {"kind": "digits_or_null"},
            "env": None,
            "reviewMdKey": None,
            "defaults": {"interactive": ["null", "default"]},
            "type": "int_or_null",
            "waistPath": "limits.nullable",
            "waistMap": None,
            "derivedFrom": None,
            "deriveWhen": None,
            "nullReceipt": ["interactive"],
            "resolvedKey": False,
        },
        {
            "key": "zeta",
            "modes": ["headless"],
            "allowedSources": {"headless": ["default"]},
            "rule": {"kind": "positive_digits"},
            "env": None,
            "reviewMdKey": None,
            "defaults": {"headless": ["1", "default"]},
            "type": "int_or_null",
            "waistPath": "delivery.count",
            "waistMap": None,
            "derivedFrom": None,
            "deriveWhen": None,
            "nullReceipt": [],
            "resolvedKey": False,
        },
        {
            "key": "eta",
            "modes": ["headless"],
            "allowedSources": {"headless": ["default"]},
            "rule": {"kind": "csv_subset", "values": ["a", "b"]},
            "env": None,
            "reviewMdKey": None,
            "defaults": {"headless": ["a,b", "default"]},
            "type": "csv_list",
            "waistPath": "delivery.items",
            "waistMap": None,
            "derivedFrom": None,
            "deriveWhen": None,
            "nullReceipt": [],
            "resolvedKey": False,
        },
        {
            "key": "delta",
            "modes": ["interactive"],
            "allowedSources": {"interactive": ["discovery"]},
            "rule": {"kind": "enum", "values": ["present"]},
            "env": None,
            "reviewMdKey": None,
            "defaults": {"interactive": ["present", "discovery"]},
            "type": "string",
            "waistPath": None,
            "waistMap": None,
            "derivedFrom": "delta",
            "deriveWhen": None,
            "nullReceipt": [],
            "resolvedKey": False,
        },
    ],
}

_EXPECTED_INLINE_SAMPLE = (
    "````markdown\n"
    "**{emoji} [SEVERITY] {finding.title}**\n"
    "\n"
    "{body}\n"
    "\n"
    "**Suggested fix:**\n"
    "{suggestion}\n"
    "\n"
    "**{rule_source_label}:**\n"
    "> {claude_md_rule, falling back to spec_text \u2014 blockquoted, one `>` line per source line}\n"
    "\n"
    "```suggestion\n"
    "{suggested_fix_code}\n"
    "```\n"
    "\n"
    "MARK *NAME*\n"
    "````"
)

_EXPECTED_DERIVED_WAIST_FIELDS = (
    "The workflow derives these waist fields from the copied `configEcho` receipt. Do not stamp a derived field; when a listed derivation applies, a stamped value that disagrees with its receipt is refused before dispatch.\n"
    "\n"
    "- `optional.beta` (headless runs): from `configEcho.beta`. Leave `beta` out of any stamped `optional`.\n"
    "- `nested.gamma` (headless and interactive runs, when the gamma condition holds): from `configEcho.gamma`; `raw` derives as `mapped`. Stamp `nested` and leave `gamma` out of it.\n"
    "- `limits.nullable` (interactive runs): from `configEcho.epsilon`; digits derive as a JSON number; on interactive runs the receipt spelling `null` derives as JSON `null`. Stamp `limits` and leave `nullable` out of it.\n"
    "- `delivery.count` (headless runs): from `configEcho.zeta`; digits derive as a JSON number. Leave `count` out of any stamped `delivery`.\n"
    "- `delivery.items` (headless runs): from `configEcho.eta`; the comma-separated value derives as a list. Leave `items` out of any stamped `delivery`.\n"
    "\n"
    "The workflow fills these receipt entries itself; never stamp them.\n"
    "\n"
    "- `configEcho.delta` (interactive runs): the delta source is present."
)

_EXPECTED_PR_IDENTITY_FIELDS = (
    "`delivery.prIdentity` fields:\n"
    "\n"
    "- `owner` (required): a non-empty string.\n"
    "- `repo` (required): a non-empty string.\n"
    "- `pr_number` (required): a positive safe integer.\n"
    "- `sha_full` (required): a 40-character lowercase hex commit id.\n"
    "- `platform` (required): one of github, gitlab.\n"
    "- `web_origin` (required): an http(s) origin: scheme, host and optional port only.\n"
    "- `title` (optional): a non-empty string when present.\n"
    "\n"
    "Producer command:\n"
    "\n"
    "`python3 {plugin_root}/scripts/resolve_pr_identity.py --platform github|gitlab --url <PR/MR web url> --sha <git rev-parse HEAD> [--title <text>]`"
)

_EXPECTED_PERMALINK_FORMATS = (
    "- `github`: blob `{origin}/{owner}/{repo}/blob/{sha}/{path}`; line `#L{start}`; range `#L{start}-L{end}`; ref `#{number}`; ref URL `{origin}/{owner}/{repo}/pull/{number}`.\n"
    "- `gitlab`: blob `{origin}/{owner}/{repo}/-/blob/{sha}/{path}`; line `#L{start}`; range `#L{start}-{end}`; ref `!{number}`; ref URL `{origin}/{owner}/{repo}/-/merge_requests/{number}`.\n"
    "- Encode owner, repo, and file paths one segment at a time with `encodeURIComponent` semantics; also percent-encode `!`, `'`, `(`, `)`, and `*`, while preserving `/` separators. A file path containing an empty, `.`, or `..` segment renders as a plain code span."
)

EXPECTED_BODIES = {
    ("skills/code-gauntlet/references/report-format.md", "severity_legend"): (
        "Product mark: MARK (NAME). Severity emoji: C critical, L low.\n"
        "Rule source labels: documented_rule -> DR, code_comment -> CC, repo_precedent -> RP, self_inconsistency -> SI; unknown values -> RF.\n"
        "Always use the Unicode characters, never GitHub shortcodes "
        "(`:red_circle:`) \u2014 shortcodes do\n"
        "not render in terminal/chat output."
    ),
    (
        "skills/code-gauntlet/references/report-format.md",
        "permalink_formats",
    ): _EXPECTED_PERMALINK_FORMATS,
    ("skills/code-gauntlet/references/report-format.md", "inline_legend"): (
        "`{emoji}` is C critical / L low, `{SEVERITY}` is the normalized severity uppercased."
    ),
    (
        "skills/code-gauntlet/references/report-format.md",
        "inline_sample",
    ): _EXPECTED_INLINE_SAMPLE,
    (
        "skills/code-gauntlet/references/delivery-guide.md",
        "severity_legend",
    ): (
        "Product mark: MARK (NAME). Severity emojis: C critical, L low.\n"
        "Rule source labels: documented_rule -> DR, code_comment -> CC, repo_precedent -> RP, self_inconsistency -> SI; unknown values -> RF."
    ),
    (
        "skills/code-gauntlet/references/delivery-guide.md",
        "summary_header",
    ): "### MARK NAME",
    (
        "skills/code-gauntlet/references/delivery-guide.md",
        "inline_sample",
    ): _EXPECTED_INLINE_SAMPLE,
    (
        "skills/code-gauntlet/references/delivery-guide.md",
        "delivery_identity",
    ): (
        "- **Identity:** prepends `### MARK NAME` to `review_body` and appends "
        "`MARK *NAME*` to every rendered comment body \u2014 one mark per delivered "
        "surface, never one per finding. Never hand-type either."
    ),
    ("skills/code-gauntlet/SKILL.md", "chat_identity"): (
        "The final delivery summary opens with `MARK NAME` on its first line and "
        "carries no other\n"
        "emoji, except severity emoji when listing findings."
    ),
    ("skills/code-gauntlet/SKILL.md", "config_receipt"): (
        "The resolver owns the printed configuration block and the keyed `configEcho` receipt.\n"
        "\n"
        "**Interactive block:**\n"
        "\n"
        "```text\n"
        "Resolved config:\n"
        "  alpha=i-alpha (fixed)\n"
        "  gamma=raw (default)\n"
        "  epsilon=null (default)\n"
        "  pipeline_version={pipeline_version} (bundle)\n"
        "  plugin_root=/absolute/path/to/claude-code-gauntlet (resolved)\n"
        "```\n"
        "\n"
        "**Interactive receipt:**\n"
        "\n"
        "```json\n"
        "{\n"
        '    "alpha": {\n'
        '        "value": "i-alpha",\n'
        '        "source": "fixed"\n'
        "    },\n"
        '    "gamma": {\n'
        '        "value": "raw",\n'
        '        "source": "default"\n'
        "    },\n"
        '    "epsilon": {\n'
        '        "value": "null",\n'
        '        "source": "default"\n'
        "    }\n"
        "}\n"
        "```\n"
        "\n"
        "**Headless block:**\n"
        "\n"
        "```text\n"
        "Headless config:\n"
        "  alpha=h-alpha (env)\n"
        "  beta=h-beta (default)\n"
        "  gamma=raw (default)\n"
        "  zeta=1 (default)\n"
        "  eta=a,b (default)\n"
        "  pipeline_version={pipeline_version} (bundle)\n"
        "  plugin_root=/absolute/path/to/claude-code-gauntlet (resolved)\n"
        "```\n"
        "\n"
        "**Headless receipt:**\n"
        "\n"
        "```json\n"
        "{\n"
        '    "alpha": {\n'
        '        "value": "h-alpha",\n'
        '        "source": "env"\n'
        "    },\n"
        '    "beta": {\n'
        '        "value": "h-beta",\n'
        '        "source": "default"\n'
        "    },\n"
        '    "gamma": {\n'
        '        "value": "raw",\n'
        '        "source": "default"\n'
        "    },\n"
        '    "zeta": {\n'
        '        "value": "1",\n'
        '        "source": "default"\n'
        "    },\n"
        '    "eta": {\n'
        '        "value": "a,b",\n'
        '        "source": "default"\n'
        "    }\n"
        "}\n"
        "```"
    ),
    (
        "skills/code-gauntlet/SKILL.md",
        "derived_waist_fields",
    ): _EXPECTED_DERIVED_WAIST_FIELDS,
    (
        "skills/code-gauntlet/SKILL.md",
        "pr_identity_fields",
    ): _EXPECTED_PR_IDENTITY_FIELDS,
    (
        "skills/code-gauntlet/references/phase2-triage.md",
        "derived_waist_fields",
    ): _EXPECTED_DERIVED_WAIST_FIELDS,
    (
        "skills/code-gauntlet/references/phase2-triage.md",
        "pr_identity_fields",
    ): _EXPECTED_PR_IDENTITY_FIELDS,
    (
        "skills/code-gauntlet/references/phase1-preflight.md",
        "derived_waist_fields",
    ): _EXPECTED_DERIVED_WAIST_FIELDS,
    ("skills/code-gauntlet/references/headless-mode.md", "headless_env_table"): (
        "| Variable | Values | Default |\n"
        "| --- | --- | --- |\n"
        "| `CODE_GAUNTLET_ALPHA` | `h-alpha` | `h-alpha` |\n"
        "| `CODE_GAUNTLET_BETA` | `h-beta` | `h-beta` |"
    ),
    (
        "skills/code-gauntlet/references/phase8-delivery.md",
        "permalink_formats",
    ): _EXPECTED_PERMALINK_FORMATS,
}


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param(
            ["criticality", "failure_scenario"],
            "`criticality` and `failure_scenario` are required by the dispatch schema "
            "\u2014 a finding missing any of them is rejected at the StructuredOutput "
            "boundary and retried, so all must always be present.",
            id="two-fields",
        ),
        pytest.param(
            ["a", "b", "c"],
            "`a`, `b`, and `c` are required by the dispatch schema \u2014 a finding "
            "missing any of them is rejected at the StructuredOutput boundary and "
            "retried, so all must always be present.",
            id="three-fields",
        ),
    ],
)
def test_dispatch_sentence(fields, expected):
    assert gen.dispatch_required_sentence(fields) == expected


def test_conditional_paragraphs():
    paragraphs = gen.conditional_paragraphs(
        [
            {"dimension": "convention", "requiredWhenDimension": ["claude_md_rule"]},
            {"dimension": "intent", "requiredWhenDimension": ["spec_text"]},
            {"dimension": "comment_accuracy", "requiredWhenDimension": []},
        ]
    )
    assert len(paragraphs) == 2
    assert paragraphs[0] == (
        "For convention findings: the `claude_md_rule` field MUST be non-null and MUST "
        "quote the specific rule. Findings without a cited rule will be rejected. "
        "`claude_md_rule` is a dimension-conditional dispatch requirement. On a dispatch "
        "that targets the first-party API directly (no third-party provider, no gateway), "
        "the schema enforces it specifically for findings whose dimension is convention "
        "\u2014 sibling intent/comment_accuracy findings correctly omit it \u2014 and this "
        "contract is the enforcement floor on every run, including third-party providers "
        "and gateway sessions where the schema stays flat. This agent's dispatch mixes "
        "convention, intent, and comment_accuracy findings in ONE schema, so a "
        "dimension-blind schema requirement (the flat `requiredExtra` mechanism, which "
        "only single-dimension agents can use) was never an option here \u2014 omitting "
        "the field on the wrong dimension is correct while omitting it on this one is "
        "a contract violation."
    )


@pytest.mark.parametrize(
    "sentence",
    [
        pytest.param(
            "`a` is required by the dispatch schema \u2014 a finding without it is "
            "rejected at the StructuredOutput boundary and retried, so it must always be "
            "present.",
            id="one-field",
        ),
        pytest.param(
            "`a` and `b` are required by the dispatch schema \u2014 a finding "
            "missing any of them is rejected at the StructuredOutput boundary and "
            "retried, so all must always be present.",
            id="two-fields",
        ),
        pytest.param(
            "`a`, `b`, and `c` are required by the dispatch schema \u2014 a finding "
            "missing any of them is rejected at the StructuredOutput boundary and "
            "retried, so all must always be present.",
            id="three-fields",
        ),
        pytest.param(
            "`a`, `b`, `c`, and `d` are required by the dispatch schema \u2014 a finding "
            "missing any of them is rejected at the StructuredOutput boundary and "
            "retried, so all must always be present.",
            id="four-fields",
        ),
    ],
)
def test_block_anchor(sentence):
    assert gen.splice(
        "before\n" + sentence + "\nafter\n", gen._SINGLE_SENTENCE_ANCHOR, "new"
    ) == (
        "before\n<!-- generated-from-registry: do not edit; "
        "scripts/generate_contract_requirements.py -->\nnew\n"
        "<!-- /generated-from-registry -->\nafter\n"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param(
            "unrelated\n",
            "no generated block and no recognizable anchor text to replace",
            id="no-anchor",
        ),
        pytest.param(
            "<!-- generated-from-registry: do not edit; scripts/generate_contract_requirements.py -->\n",
            "malformed generated-block markers (1 open, 0 close)",
            id="malformed",
        ),
        pytest.param(
            "<!-- generated-from-registry: do not edit; scripts/generate_contract_requirements.py -->\none\n<!-- /generated-from-registry -->\n"
            "<!-- generated-from-registry: do not edit; scripts/generate_contract_requirements.py -->\ntwo\n<!-- /generated-from-registry -->\n",
            "malformed generated-block markers (2 open, 2 close)",
            id="two-blocks",
        ),
        pytest.param(
            "<!-- generated-from-registry: do not edit; scripts/generate_contract_requirements.py -->body<!-- /generated-from-registry -->",
            "found MARKER_OPEN but block regex did not match",
            id="block-regex",
        ),
    ],
)
def test_block_diagnostic(text, message):
    with pytest.raises(CliError, match=re.escape(message)):
        gen.splice(text, gen._SINGLE_SENTENCE_ANCHOR, "new")


@pytest.fixture
def field_registry():
    return {
        "required": ["id"],
        "canonicalFields": ["id", "claude_md_rule"],
        "dimensions": [
            {
                "requiredExtra": ["attack_vector"],
                "requiredWhenDimension": [],
                "extraFields": ["attack_vector"],
            },
            {
                "requiredExtra": [],
                "requiredWhenDimension": ["claude_md_rule"],
                "extraFields": [],
            },
        ],
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "| `attack_vector` | string | security | no | exploit |\n",
            "| `attack_vector` | string | security | yes | exploit |\n",
            id="per-dimension",
        ),
        pytest.param(
            "| `not_a_real_field` | string | yes | invented |\n",
            "| `not_a_real_field` | string | yes | invented |\n",
            id="unknown-field",
        ),
        pytest.param(
            "| `claude_md_rule` | string | no | d |",
            "| `claude_md_rule` | string | conditional | d |",
            id="no-trailing-newline",
        ),
        pytest.param(
            "| `id` | string | MAYBE | invalid cell |\n",
            "| `id` | string | MAYBE | invalid cell |\n",
            id="invalid-required",
        ),
    ],
)
def test_required_column(field_registry, text, expected):
    assert gen.rewrite_required_column(text, field_registry) == expected


@pytest.mark.parametrize(
    ("rel", "symbol", "expected"),
    [
        pytest.param(rel, symbol, body, id=rel.split("/")[-1] + ":" + symbol)
        for (rel, symbol), body in EXPECTED_BODIES.items()
    ],
)
def test_identity_body(rel, symbol, expected):
    assert (
        "\n".join(gen.identity_body(rel, symbol, deepcopy(IDENTITY), str(ROOT)))
        == expected
    )


def test_identity_body_inventory():
    assert set(EXPECTED_BODIES) | {
        (gen.REPORT_FORMAT_REL, "full_report_template"),
        (gen.REPORT_FORMAT_REL, "permalink_sample"),
    } == {
        (rel, symbol)
        for rel, symbols in gen.IDENTITY_FENCES.items()
        for symbol in symbols
    }


@pytest.mark.parametrize(
    ("markers", "message"),
    [
        pytest.param(
            ["alpha", "alpha", "/alpha"],
            "duplicate open identity marker for alpha (lines 1 and 2)",
            id="duplicate-open",
        ),
        pytest.param(
            ["alpha", "/alpha", "/alpha"],
            "duplicate close identity marker for alpha (lines 2 and 3)",
            id="duplicate-close",
        ),
        pytest.param(
            ["/alpha", "alpha"],
            "close identity marker for alpha precedes its open marker (lines 1 and 2)",
            id="close-before-open",
        ),
        pytest.param(
            ["alpha", "beta", "/beta", "/alpha"],
            "beta's identity fence is nested inside alpha's (lines 1-4)",
            id="nested",
        ),
    ],
)
def test_identity_pair_diagnostic(markers, message):
    lines = [
        f"# {marker[:1] if marker.startswith('/') else ''}generated-from-registry-identity:{marker.lstrip('/')}"
        for marker in markers
    ]
    with pytest.raises(CliError, match=re.escape(message)):
        gen.find_identity_pairs(lines, "x.py")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            "deriveWhen",
            "identity_body: identity lacks a deriveWhen key",
            id="missing-deriveWhen",
        ),
        pytest.param(
            "derivedFrom",
            "identity_body: identity lacks a derivedFrom key",
            id="missing-derivedFrom",
        ),
        pytest.param(
            "waistRequired",
            "identity_body: identity lacks a waistRequired key",
            id="missing-waistRequired",
        ),
        pytest.param(
            "missing-description",
            "deriveWhen has no describe entry for 'gamma'",
            id="missing-description",
        ),
        pytest.param(
            "empty-description",
            "deriveWhen description for 'gamma' is empty or whitespace",
            id="empty-description",
        ),
        pytest.param(
            "unknown-type", "row 'beta' has unknown type 'number'", id="unknown-type"
        ),
    ],
)
def test_derived_identity_diagnostic(change, message):
    identity = deepcopy(IDENTITY)
    if change in {"deriveWhen", "derivedFrom", "waistRequired"}:
        del identity[change]
    elif change == "missing-description":
        del identity["deriveWhen"]["gamma"]
    elif change == "empty-description":
        identity["deriveWhen"]["gamma"] = "  \t"
    else:
        identity["knobs"][1]["type"] = "number"
    with pytest.raises(CliError, match=re.escape(message)):
        gen.identity_body("x.md", "derived_waist_fields", identity, str(ROOT))


def test_unknown_identity_body():
    with pytest.raises(CliError, match=r"no identity body for 'nope' in x.md"):
        gen.identity_body("x.md", "nope", IDENTITY, str(ROOT))


def test_undeclared_identity_fence():
    rel = "skills/code-gauntlet/references/delivery-guide.md"
    text = "\n".join(
        line
        for symbol in [
            "severity_legend",
            "summary_header",
            "inline_sample",
            "delivery_identity",
            "bogus",
        ]
        for line in [
            f"<!-- generated-from-registry-identity:{symbol} -->",
            f"<!-- /generated-from-registry-identity:{symbol} -->",
        ]
    )
    with pytest.raises(
        CliError, match="identity marker pair\\(s\\) bogus match no declared symbol"
    ):
        gen.fill_identity_fences(text, rel, IDENTITY, str(ROOT))


@pytest.mark.parametrize("failure", ["missing", "failed", "invalid-json"])
def test_node_diagnostic(monkeypatch, tmp_path, failure, invoke):
    def run(command, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("node")
        if failure == "invalid-json":
            return gen.proc.CompletedProcess(command, 0, stdout="not json", stderr="")
        raise gen.proc.CalledProcessError(
            1, command, stderr="early\nforced JS failure\n"
        )

    monkeypatch.setattr(gen.proc, "run", run)
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(tmp_path), "--check"],
        tmp_path,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    message = result.stderr.decode().rstrip("\n")
    if failure == "invalid-json":
        assert message.startswith("generate_contract_requirements: ")
    else:
        assert message.startswith(
            "generate_contract_requirements: node 24 command failed: node --input-type=module -e "
        )
    assert "\n" not in message
    if failure == "failed":
        assert message.endswith(": forced JS failure")


def test_command_drift(registry_tree, invoke):
    target = registry_tree / "agents/test-analyzer.md"
    original = target.read_bytes()
    target.write_text(
        original.decode().replace("all must always be present", "x"), encoding="utf-8"
    )
    broken = target.read_bytes()
    stale = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree), "--check"],
        registry_tree,
    )
    assert stale.returncode == 1
    assert stale.stdout == b""
    assert stale.stderr == (
        b"generate_contract_requirements: stale generated registry blocks: agents/test-analyzer.md; run: python3 scripts/generate_contract_requirements.py\n"
    )
    assert target.read_bytes() == broken
    repaired = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree)],
        registry_tree,
    )
    assert repaired.returncode == 0
    assert repaired.stderr == b""
    assert repaired.stdout == b"regenerated: agents/test-analyzer.md\n"
    assert target.read_bytes() == original
    current = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree), "--check"],
        registry_tree,
    )
    assert current.returncode == 0
    assert current.stdout == b"generated registry blocks are current\n"


def test_added_knob(registry_tree, invoke):
    path = registry_tree / "workflows/src/args.js"
    source = path.read_text(encoding="utf-8")
    row = (
        "  { key: 'added', modes: ['headless'], allowedSources: { headless: ['default'] }, "
        "rule: { kind: 'enum', values: ['value'] }, env: null, "
        "reviewMdKey: null, defaults: { headless: ['value', 'default'] }, type: 'string', "
        "waistPath: 'nested.added', waistMap: null, derivedFrom: null, deriveWhen: null, "
        "nullReceipt: [], resolvedKey: false },\n"
    )
    path.write_text(source.replace("\n];", "\n" + row + "];", 1), encoding="utf-8")
    args = ["--repo-root", str(registry_tree)]
    stale = invoke("generate_contract_requirements", [*args, "--check"], registry_tree)
    assert stale.returncode == 1
    assert stale.stderr == (
        b"generate_contract_requirements: stale generated registry blocks: skills/code-gauntlet/SKILL.md, "
        b"skills/code-gauntlet/references/phase2-triage.md, "
        b"skills/code-gauntlet/references/phase1-preflight.md, scripts/gauntlet/registry.py; run: python3 scripts/generate_contract_requirements.py\n"
    )
    assert invoke("generate_contract_requirements", args, registry_tree).returncode == 0
    assert (
        invoke(
            "generate_contract_requirements", [*args, "--check"], registry_tree
        ).returncode
        == 0
    )


@pytest.mark.parametrize("damage", ["missing", "orphaned"])
def test_command_identity_diagnostic(registry_tree, damage, invoke):
    target = registry_tree / "skills/code-gauntlet/references/delivery-guide.md"
    text = target.read_text(encoding="utf-8")
    if damage == "missing":
        text = re.sub(
            r"<!-- generated-from-registry-identity:summary_header[^\n]*\n.*?<!-- /generated-from-registry-identity:summary_header -->\n",
            "",
            text,
            flags=re.DOTALL,
        )
        message = "no marker pair for identity symbol(s) summary_header"
    else:
        text = text.replace(
            "<!-- /generated-from-registry-identity:severity_legend -->\n", ""
        )
        message = "unmatched identity marker(s) for severity_legend"
    target.write_text(text, encoding="utf-8")
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree), "--check"],
        registry_tree,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert message in result.stderr.decode()
    assert re.fullmatch(
        r"generate_contract_requirements: [^\n]+\n", result.stderr.decode()
    )


def test_both_report_regions(registry_tree, invoke):
    target = registry_tree / "skills/code-gauntlet/references/report-format.md"
    original = target.read_text(encoding="utf-8")
    broken = re.sub(r"(\| `id` \|[^\n]*?\| )yes( \|)", r"\g<1>no\2", original, count=1)
    broken = broken.replace("Product mark:", "HAND-EDITED LEGEND:", 1)
    assert broken != original
    target.write_text(broken, encoding="utf-8")
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree)],
        registry_tree,
    )
    assert result.returncode == 0
    assert target.read_text(encoding="utf-8") == original


def test_missing_registry(registry_tree, invoke):
    target = registry_tree / "scripts/gauntlet/registry.py"
    original = target.read_bytes()
    target.unlink()
    args = ["--repo-root", str(registry_tree)]
    stale = invoke("generate_contract_requirements", [*args, "--check"], registry_tree)
    assert stale.returncode == 1
    assert b"scripts/gauntlet/registry.py" in stale.stderr
    assert not target.exists()
    assert invoke("generate_contract_requirements", args, registry_tree).returncode == 0
    assert target.read_bytes() == original


def test_cold_registry_repair(registry_tree):
    shutil.copytree(ROOT / "scripts", registry_tree / "scripts", dirs_exist_ok=True)
    target = registry_tree / "scripts/gauntlet/registry.py"
    original = target.read_bytes()
    target.write_text(
        original.decode().replace("BRAND_MARK =", "ABSENT_BRAND_MARK =", 1),
        encoding="utf-8",
    )
    command = [sys.executable, "scripts/generate_contract_requirements.py"]
    stale = gen.proc.run([*command, "--check"], cwd=str(registry_tree))
    assert stale.returncode == 1
    assert "scripts/gauntlet/registry.py" in stale.stderr
    repaired = gen.proc.run(command, cwd=str(registry_tree))
    assert repaired.returncode == 0
    assert target.read_bytes() == original
    assert gen.proc.run([*command, "--check"], cwd=str(registry_tree)).returncode == 0


def test_identity_fill_order():
    rel = "skills/code-gauntlet/references/delivery-guide.md"
    text = "\n".join(
        line
        for symbol in [
            "summary_header",
            "delivery_identity",
            "severity_legend",
            "inline_sample",
        ]
        for line in [
            f"<!-- generated-from-registry-identity:{symbol} -->",
            "STALE",
            f"<!-- /generated-from-registry-identity:{symbol} -->",
        ]
    )
    filled = gen.fill_identity_fences(text, rel, IDENTITY, str(ROOT))
    assert "STALE" not in filled
    expected = "\n".join(
        line
        for symbol in [
            "summary_header",
            "delivery_identity",
            "severity_legend",
            "inline_sample",
        ]
        for line in [
            f"<!-- generated-from-registry-identity:{symbol} -->",
            EXPECTED_BODIES[(rel, symbol)],
            f"<!-- /generated-from-registry-identity:{symbol} -->",
        ]
    )
    assert filled == expected
    assert gen.fill_identity_fences(expected, rel, IDENTITY, str(ROOT)) == expected


@pytest.fixture(scope="module")
def live_identity():
    return gen.load_registry(str(ROOT))


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        pytest.param(
            "non-json", "cannot render non-JSON registry value: 7", id="non-json"
        ),
        pytest.param(
            "schema-type",
            "unknown finding schema type for id: 'boolean'",
            id="schema-type",
        ),
        pytest.param(
            "required-field",
            "required finding field absent from schema",
            id="required-field",
        ),
        pytest.param("delta-keys", "invalid ordered delta keys", id="delta-keys"),
        pytest.param(
            "slice-field",
            "verify slice field absent from finding schema",
            id="slice-field",
        ),
        pytest.param(
            "artifact-keys", "unexpected artifact path keys", id="artifact-keys"
        ),
        pytest.param(
            "artifact-path", "invalid artifact path for findings", id="artifact-path"
        ),
        pytest.param(
            "artifact-basename",
            "artifact path is not a basename for findings",
            id="artifact-basename",
        ),
        pytest.param("fix-bound", "invalid fix bound: fixMaxLines", id="fix-bound"),
        pytest.param(
            "ordered-values",
            "invalid ordered values: severityOrder",
            id="ordered-values",
        ),
    ],
)
def test_registry_diagnostic(
    registry_tree, live_identity, monkeypatch, invoke, damage, message
):
    identity = deepcopy(live_identity)
    if damage == "non-json":
        identity["severityEmojiFallback"] = 7
    elif damage == "schema-type":
        identity["findingTypes"]["id"] = "boolean"
    elif damage == "required-field":
        identity["required"].append("absent")
    elif damage == "delta-keys":
        identity["deltaKeys"] = ["verified", "id"]
    elif damage == "slice-field":
        identity["verifySliceFields"].append("absent")
    elif damage == "artifact-keys":
        del identity["artifactPaths"]["findings"]
    elif damage == "artifact-path":
        identity["artifactPaths"]["findings"] = "wrong-root"
    elif damage == "artifact-basename":
        identity["artifactPaths"]["findings"] = (
            "/__gauntlet_registry_root__/nested/__GAUNTLET_SHA__.json"
        )
    elif damage == "fix-bound":
        identity["fixMaxLines"] = 0
    else:
        identity["severityOrder"] = []
    monkeypatch.setattr(gen, "load_registry", lambda root: identity)
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree), "--check"],
        registry_tree,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert message in result.stderr.decode()
    assert re.fullmatch(
        r"generate_contract_requirements: [^\n]+\n", result.stderr.decode()
    )


def test_resolver_load_diagnostic(registry_tree, monkeypatch, invoke):
    monkeypatch.setattr(
        gen.importlib.util, "spec_from_file_location", lambda *args: None
    )
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree), "--check"],
        registry_tree,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert "cannot load resolver module from " in result.stderr.decode()
    assert re.fullmatch(
        r"generate_contract_requirements: [^\n]+\n", result.stderr.decode()
    )


def test_partial_write_order(registry_tree, invoke):
    early = registry_tree / "agents/security-reviewer.md"
    original = early.read_bytes()
    early.write_text(
        original.decode().replace("it must always be present", "STALE"),
        encoding="utf-8",
    )
    late = registry_tree / "skills/code-gauntlet/references/delivery-guide.md"
    late.write_text(
        late.read_text(encoding="utf-8").replace(
            "<!-- /generated-from-registry-identity:severity_legend -->", ""
        ),
        encoding="utf-8",
    )
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree)],
        registry_tree,
    )
    assert result.returncode == 1
    assert b"unmatched identity marker" in result.stderr
    assert early.read_bytes() == original


def test_identity_markers_inside_code_fence():
    assert gen.find_identity_pairs(
        [
            "```python",
            "# generated-from-registry-identity:alpha",
            "old",
            "# /generated-from-registry-identity:alpha",
            "```",
        ],
        "x.py",
    ) == {"alpha": (1, 3)}


def test_target_steps_compose(monkeypatch):
    path = "agents/code-simplifier.md"
    identity = {
        "dimensions": [
            {
                "agentType": "code-gauntlet:code-simplifier",
                "requiredExtra": ["x"],
            }
        ],
        "brand": {"mark": "MARK", "name": "NAME"},
        "severityEmoji": {},
        "ruleSourceLabels": {},
    }
    monkeypatch.setattr(gen, "IDENTITY_FENCES", {path: ["summary_header"]})
    text = (
        "`x` is required by the dispatch schema - old wording.\n"
        "<!-- generated-from-registry-identity:summary_header -->\n"
        "STALE\n<!-- /generated-from-registry-identity:summary_header -->\n"
    )
    # The fence step must see the result of the preceding dispatch splice.
    fill = gen.fill_identity_fences

    def fill_after_splice(current, **kwargs):
        assert "old wording" not in current
        return fill(current, **kwargs)

    monkeypatch.setattr(gen, "fill_identity_fences", fill_after_splice)
    target = gen.rendered_targets(str(ROOT), identity, "registry")[path]
    assert callable(target)
    assert target(text) == (
        "<!-- generated-from-registry: do not edit; "
        "scripts/generate_contract_requirements.py -->\n"
        "`x` is required by the dispatch schema \u2014 a finding without it is "
        "rejected at the StructuredOutput boundary and retried, so it must always be "
        "present.\n<!-- /generated-from-registry -->\n"
        "<!-- generated-from-registry-identity:summary_header -->\n"
        "### MARK NAME\n<!-- /generated-from-registry-identity:summary_header -->\n"
    )


def test_conditional_anchor_command(registry_tree, invoke):
    path = registry_tree / "agents/conventions-and-intent.md"
    original = path.read_bytes()
    stripped = b"\n".join(
        line
        for line in original.split(b"\n")
        if not line.startswith(
            (b"<!-- generated-from-registry:", b"<!-- /generated-from-registry -->")
        )
    )
    path.write_bytes(stripped)
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree)],
        registry_tree,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert path.read_bytes() == original


def test_no_conditional_fields():
    assert (
        gen.conditional_paragraphs([{"dimension": "bug", "requiredWhenDimension": []}])
        == []
    )


@pytest.mark.parametrize(
    ("source", "old", "new", "projection"),
    [
        pytest.param(
            "filterFindings.js",
            "FIX_MAX_LINES = 100",
            "FIX_MAX_LINES = 101",
            "FIX_MAX_LINES = 101",
            id="fix-bound",
        ),
        pytest.param(
            "applyValidations.js",
            "'uncertain'",
            "'unsure'",
            '    "unsure",',
            id="reachability",
        ),
        pytest.param(
            "stages.js",
            "'cross_file_refs', 'origin'];",
            "'cross_file_refs', 'title'];",
            '    "title",',
            id="verify-slice",
        ),
        pytest.param(
            "registry.js",
            "Code Gauntlet",
            "Changed Gauntlet",
            'BRAND_NAME = "Changed Gauntlet"',
            id="brand",
        ),
    ],
)
def test_registry_source_projection(
    registry_tree, invoke, source, old, new, projection
):
    path = registry_tree / "workflows/src" / source
    contents = path.read_text(encoding="utf-8")
    assert old in contents
    path.write_text(contents.replace(old, new), encoding="utf-8")
    args = ["--repo-root", str(registry_tree)]
    stale = invoke("generate_contract_requirements", [*args, "--check"], registry_tree)
    assert stale.returncode == 1
    assert stale.stdout == b""
    assert stale.stderr.decode().startswith(
        "generate_contract_requirements: stale generated registry blocks: "
    )
    assert "scripts/gauntlet/registry.py" in stale.stderr.decode()
    repaired = invoke("generate_contract_requirements", args, registry_tree)
    assert repaired.returncode == 0
    assert repaired.stderr == b""
    module = (registry_tree / "scripts/gauntlet/registry.py").read_text(
        encoding="utf-8"
    )
    assert projection in module.splitlines()
