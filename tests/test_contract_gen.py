"""Contract rendering and the developer command's generated-target boundary."""

import re
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from gauntlet import contract_gen as gen
from gauntlet.cli import CliError

from tests.support.repository import copy_generator_tree

ROOT = Path(__file__).resolve().parents[1]
REPORT = "skills/code-gauntlet/references/report-format.md"
DELIVERY = "skills/code-gauntlet/references/delivery-guide.md"
SKILL = "skills/code-gauntlet/SKILL.md"
TRIAGE = "skills/code-gauntlet/references/phase2-triage.md"
PREFLIGHT = "skills/code-gauntlet/references/phase1-preflight.md"
HEADLESS = "skills/code-gauntlet/references/headless-mode.md"
DELIVER_PHASE = "skills/code-gauntlet/references/phase8-delivery.md"
TARGETS = (
    "agents/security-reviewer.md",
    "agents/cross-file-impact.md",
    "agents/test-analyzer.md",
    "agents/code-simplifier.md",
    "agents/conventions-and-intent.md",
    REPORT,
    DELIVERY,
    SKILL,
    TRIAGE,
    PREFLIGHT,
    HEADLESS,
    DELIVER_PHASE,
    "scripts/gauntlet/registry.py",
)


@pytest.fixture
def registry_tree(tmp_path: Path) -> Path:
    copy_generator_tree(ROOT, tmp_path, TARGETS)
    return tmp_path


KNOB_CASES: list[tuple[str, dict[str, list[str]], dict[str, Any], dict[str, Any]]] = [
    (
        "alpha",
        {"headless": ["h-alpha", "env"], "interactive": ["i-alpha", "fixed"]},
        {
            "headless": {"kind": "enum", "values": ["h-alpha"]},
            "interactive": {"kind": "enum", "values": ["i-alpha"]},
        },
        {
            "env": "CODE_GAUNTLET_ALPHA",
            "resolvedKey": True,
            "allowedSources": {
                "headless": ["env", "default"],
                "interactive": ["fixed"],
            },
        },
    ),
    (
        "beta",
        {"headless": ["h-beta", "default"]},
        {"kind": "enum", "values": ["h-beta"]},
        {
            "env": "CODE_GAUNTLET_BETA",
            "waistPath": "optional.beta",
            "nullReceipt": ["headless"],
        },
    ),
    (
        "gamma",
        {"headless": ["raw", "default"], "interactive": ["raw", "default"]},
        {"kind": "enum", "values": ["raw"]},
        {
            "waistPath": "nested.gamma",
            "waistMap": {"raw": "mapped"},
            "deriveWhen": "gamma",
        },
    ),
    (
        "epsilon",
        {"interactive": ["null", "default"]},
        {"kind": "digits_or_null"},
        {
            "type": "int_or_null",
            "waistPath": "limits.nullable",
            "nullReceipt": ["interactive"],
        },
    ),
    (
        "zeta",
        {"headless": ["1", "default"]},
        {"kind": "positive_digits"},
        {"type": "int_or_null", "waistPath": "delivery.count"},
    ),
    (
        "eta",
        {"headless": ["a,b", "default"]},
        {"kind": "csv_subset", "values": ["a", "b"]},
        {"type": "csv_list", "waistPath": "delivery.items"},
    ),
    (
        "delta",
        {"interactive": ["present", "discovery"]},
        {"kind": "enum", "values": ["present"]},
        {"derivedFrom": "delta"},
    ),
]

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
        {"name": name, "required": required, "describe": describe}
        for name, required, describe in [
            ("owner", True, "a non-empty string"),
            ("repo", True, "a non-empty string"),
            ("pr_number", True, "a positive safe integer"),
            ("sha_full", True, "a 40-character lowercase hex commit id"),
            ("platform", True, "one of github, gitlab"),
            (
                "web_origin",
                True,
                "an http(s) origin: scheme, host and optional port only",
            ),
            ("title", False, "a non-empty string when present"),
        ]
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
            "key": key,
            "modes": list(defaults),
            "allowedSources": {mode: [pair[1]] for mode, pair in defaults.items()},
            "defaults": defaults,
            "rule": rule,
            "env": None,
            "reviewMdKey": None,
            "type": "string",
            "waistPath": None,
            "waistMap": None,
            "derivedFrom": None,
            "deriveWhen": None,
            "nullReceipt": [],
            "resolvedKey": False,
            **extras,
        }
        for key, defaults, rule, extras in KNOB_CASES
    ],
}

_EXPECTED_INLINE_SAMPLE = (
    "````markdown\n**{emoji} [SEVERITY] {finding.title}**\n\n{body}\n\n"
    "**Suggested fix:**\n{suggestion}\n\n**{rule_source_label}:**\n"
    "> {claude_md_rule, falling back to spec_text — blockquoted, one `>` "
    "line per source line}\n\n```suggestion\n{suggested_fix_code}\n```\n\nMARK *NAME*\n"
    "````"
)

_EXPECTED_DERIVED_WAIST_FIELDS = (
    "The workflow derives these waist fields from the copied `configEcho` "
    "receipt. Do not stamp a derived field; when a listed derivation "
    "applies, a stamped value that disagrees with its receipt is refused "
    "before dispatch.\n\n"
    "- `optional.beta` (headless runs): from `configEcho.beta`. Leave "
    "`beta` out of any stamped `optional`.\n"
    "- `nested.gamma` (headless and interactive runs, when the gamma "
    "condition holds): from `configEcho.gamma`; `raw` derives as `mapped`. "
    "Stamp `nested` and leave `gamma` out of it.\n"
    "- `limits.nullable` (interactive runs): from `configEcho.epsilon`; "
    "digits derive as a JSON number; on interactive runs the receipt "
    "spelling `null` derives as JSON `null`. Stamp `limits` and leave "
    "`nullable` out of it.\n"
    "- `delivery.count` (headless runs): from `configEcho.zeta`; digits "
    "derive as a JSON number. Leave `count` out of any stamped `delivery`.\n"
    "- `delivery.items` (headless runs): from `configEcho.eta`; the "
    "comma-separated value derives as a list. Leave `items` out of any "
    "stamped `delivery`.\n\n"
    "The workflow fills these receipt entries itself; never stamp them.\n\n"
    "- `configEcho.delta` (interactive runs): the delta source is present."
)

_EXPECTED_PR_IDENTITY_FIELDS = (
    "`delivery.prIdentity` fields:\n\n- `owner` (required): a non-empty string.\n"
    "- `repo` (required): a non-empty string.\n"
    "- `pr_number` (required): a positive safe integer.\n"
    "- `sha_full` (required): a 40-character lowercase hex commit id.\n"
    "- `platform` (required): one of github, gitlab.\n"
    "- `web_origin` (required): an http(s) origin: scheme, host and "
    "optional port only.\n- `title` (optional): a non-empty string when present.\n\n"
    "Producer commands:\n\n"
    "`gh pr view {pr_number} --json url,title | {python} "
    '"{plugin_root}/scripts/resolve_pr_identity.py" --platform github '
    '--sha "$(git rev-parse HEAD)"`\n\n'
    "`glab mr view {pr_number} --output json | {python} "
    '"{plugin_root}/scripts/resolve_pr_identity.py" --platform gitlab '
    '--sha "$(git rev-parse HEAD)"`\n\n'
    "The title and URL reach the script only through the pipe."
)

_EXPECTED_PERMALINK_FORMATS = (
    "- `github`: blob `{origin}/{owner}/{repo}/blob/{sha}/{path}`; line "
    "`#L{start}`; range `#L{start}-L{end}`; ref `#{number}`; ref URL "
    "`{origin}/{owner}/{repo}/pull/{number}`.\n"
    "- `gitlab`: blob `{origin}/{owner}/{repo}/-/blob/{sha}/{path}`; line "
    "`#L{start}`; range `#L{start}-{end}`; ref `!{number}`; ref URL "
    "`{origin}/{owner}/{repo}/-/merge_requests/{number}`.\n"
    "- Encode owner, repo, and file paths one segment at a time with "
    "`encodeURIComponent` semantics; also percent-encode `!`, `'`, `(`, "
    "`)`, and `*`, while preserving `/` separators. A file path containing "
    "an empty, `.`, or `..` segment renders as a plain code span."
)

EXPECTED_BODIES = {
    (REPORT, "severity_legend"): (
        "Product mark: MARK (NAME). Severity emoji: C critical, L low.\n"
        "Rule source labels: documented_rule -> DR, code_comment -> CC, "
        "repo_precedent -> RP, self_inconsistency -> SI; unknown values -> RF.\n"
        "Always use the Unicode characters, never GitHub shortcodes "
        "(`:red_circle:`) \u2014 shortcodes do\nnot render in terminal/chat output."
    ),
    (REPORT, "permalink_formats"): _EXPECTED_PERMALINK_FORMATS,
    (REPORT, "inline_legend"): (
        "`{emoji}` is C critical / L low, `{SEVERITY}` is the normalized "
        "severity uppercased."
    ),
    (REPORT, "inline_sample"): _EXPECTED_INLINE_SAMPLE,
    (DELIVERY, "severity_legend"): (
        "Product mark: MARK (NAME). Severity emojis: C critical, L low.\n"
        "Rule source labels: documented_rule -> DR, code_comment -> CC, "
        "repo_precedent -> RP, self_inconsistency -> SI; unknown values -> RF."
    ),
    (DELIVERY, "summary_header"): "### MARK NAME",
    (DELIVERY, "inline_sample"): _EXPECTED_INLINE_SAMPLE,
    (DELIVERY, "delivery_identity"): (
        "- **Identity:** prepends `### MARK NAME` to `review_body` and appends "
        "`MARK *NAME*` to every rendered comment body \u2014 one mark per delivered "
        "surface, never one per finding. Never hand-type either."
    ),
    (SKILL, "chat_identity"): (
        "The final delivery summary opens with `MARK NAME` on its first line and "
        "carries no other\nemoji, except severity emoji when listing findings."
    ),
    (SKILL, "config_receipt"): (
        "The resolver owns the printed configuration block and the keyed "
        "`configEcho` receipt."
        """

**Interactive block:**

```text
Resolved config:
  alpha=i-alpha (fixed)
  gamma=raw (default)
  epsilon=null (default)
  pipeline_version={pipeline_version} (bundle)
  plugin_root=/absolute/path/to/claude-code-gauntlet (resolved)
```

**Interactive receipt:**

```json
{
    "alpha": {
        "value": "i-alpha",
        "source": "fixed"
    },
    "gamma": {
        "value": "raw",
        "source": "default"
    },
    "epsilon": {
        "value": "null",
        "source": "default"
    }
}
```

**Headless block:**

```text
Headless config:
  alpha=h-alpha (env)
  beta=h-beta (default)
  gamma=raw (default)
  zeta=1 (default)
  eta=a,b (default)
  pipeline_version={pipeline_version} (bundle)
  plugin_root=/absolute/path/to/claude-code-gauntlet (resolved)
```

**Headless receipt:**

```json
{
    "alpha": {
        "value": "h-alpha",
        "source": "env"
    },
    "beta": {
        "value": "h-beta",
        "source": "default"
    },
    "gamma": {
        "value": "raw",
        "source": "default"
    },
    "zeta": {
        "value": "1",
        "source": "default"
    },
    "eta": {
        "value": "a,b",
        "source": "default"
    }
}
```"""
    ),
    (SKILL, "derived_waist_fields"): _EXPECTED_DERIVED_WAIST_FIELDS,
    (SKILL, "pr_identity_fields"): _EXPECTED_PR_IDENTITY_FIELDS,
    (TRIAGE, "derived_waist_fields"): _EXPECTED_DERIVED_WAIST_FIELDS,
    (TRIAGE, "pr_identity_fields"): _EXPECTED_PR_IDENTITY_FIELDS,
    (PREFLIGHT, "derived_waist_fields"): _EXPECTED_DERIVED_WAIST_FIELDS,
    (HEADLESS, "headless_env_table"): (
        "| Variable | Values | Default |\n| --- | --- | --- |\n"
        "| `CODE_GAUNTLET_ALPHA` | `h-alpha` | `h-alpha` |\n"
        "| `CODE_GAUNTLET_BETA` | `h-beta` | `h-beta` |"
    ),
    (DELIVER_PHASE, "permalink_formats"): _EXPECTED_PERMALINK_FORMATS,
}


def test_dispatch_sentence():
    assert gen.dispatch_required_sentence(["criticality", "failure_scenario"]) == (
        "`criticality` and `failure_scenario` are required by the dispatch schema "
        "\u2014 a finding missing any of them is rejected at the StructuredOutput "
        "boundary and retried, so all must always be present."
    )


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
        "`claude_md_rule` is a dimension-conditional dispatch requirement. "
        "On a dispatch that targets the first-party API directly (no third-party "
        "provider, no gateway), "
        "the schema enforces it specifically for findings whose dimension "
        "is convention — sibling intent/comment_accuracy findings correctly omit it — "
        "and this contract is the enforcement floor on every run, including "
        "third-party providers "
        "and gateway sessions where the schema stays flat. This agent's dispatch mixes "
        "convention, intent, and comment_accuracy findings in ONE schema, so a "
        "dimension-blind schema requirement (the flat `requiredExtra` mechanism, which "
        "only single-dimension agents can use) was never an option here — omitting "
        "the field on the wrong dimension is correct while omitting it on this one is "
        "a contract violation."
    )


def test_block_anchor():
    assert gen.splice(
        "before\n`a` is required by the dispatch schema - old wording.\nafter\n",
        gen._SINGLE_SENTENCE_ANCHOR,
        "new",
    ) == (
        "before\n<!-- generated-from-registry: do not edit; "
        "scripts/generate_contract_requirements.py -->\nnew\n"
        "<!-- /generated-from-registry -->\nafter\n"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("unrelated\n", "no generated block and no recognizable anchor"),
        (gen.MARKER_OPEN + "\n", "malformed generated-block markers (1 open, 0 close)"),
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


def test_required_column(field_registry):
    assert gen.rewrite_required_column(
        "| `id` | string | no | identifier |\n"
        "| `attack_vector` | string | security | no | exploit |\n"
        "| `claude_md_rule` | string | no | citation |\n"
        "| `not_a_real_field` | string | yes | invented |\n",
        field_registry,
    ) == (
        "| `id` | string | yes | identifier |\n"
        "| `attack_vector` | string | security | yes | exploit |\n"
        "| `claude_md_rule` | string | conditional | citation |\n"
        "| `not_a_real_field` | string | yes | invented |\n"
    )


@pytest.mark.parametrize(
    ("rel", "symbol", "expected"),
    [
        pytest.param(rel, symbol, body, id=rel.split("/")[-1] + ":" + symbol)
        for (rel, symbol), body in EXPECTED_BODIES.items()
        if (rel, symbol)
        not in {
            (DELIVERY, "inline_sample"),
            (TRIAGE, "derived_waist_fields"),
            (TRIAGE, "pr_identity_fields"),
            (PREFLIGHT, "derived_waist_fields"),
            (DELIVER_PHASE, "permalink_formats"),
        }
    ],
)
def test_identity_body(rel, symbol, expected):
    body = gen.identity_body(rel, symbol, deepcopy(IDENTITY), str(ROOT))
    assert "\n".join(body) == expected


def test_identity_body_inventory():
    assert set(EXPECTED_BODIES) | {
        (gen.REPORT_FORMAT_REL, "full_report_template"),
        (gen.REPORT_FORMAT_REL, "permalink_sample"),
    } == {
        (rel, symbol)
        for rel, symbols in gen.IDENTITY_FENCES.items()
        for symbol in symbols
    }


def test_identity_pair_diagnostic():
    with pytest.raises(CliError, match="duplicate open identity marker for alpha"):
        gen.find_identity_pairs(
            [
                "<!-- generated-from-registry-identity:alpha -->",
                "<!-- generated-from-registry-identity:alpha -->",
                "<!-- /generated-from-registry-identity:alpha -->",
            ],
            "x.md",
        )


def test_unknown_identity_body():
    with pytest.raises(CliError, match=r"no identity body for 'nope' in x.md"):
        gen.identity_body("x.md", "nope", IDENTITY, str(ROOT))


def test_undeclared_identity_fence():
    rel = DELIVERY
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


def test_node_diagnostic(monkeypatch, tmp_path, invoke):
    def run(command, **kwargs):
        raise FileNotFoundError("node")

    monkeypatch.setattr(gen.proc, "run", run)
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(tmp_path), "--check"],
        tmp_path,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.decode().startswith(
        "generate_contract_requirements: node 24 command failed: node "
        "--input-type=module -e "
    )
    assert result.stderr.count(b"\n") == 1


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
        b"generate_contract_requirements: stale generated registry blocks: "
        b"agents/test-analyzer.md; run: python3 "
        b"scripts/generate_contract_requirements.py\n"
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
    path = registry_tree / "workflows/src/knobs.js"
    source = path.read_text(encoding="utf-8")
    row = (
        "  { key: 'added', modes: ['headless'], allowedSources: { "
        "headless: ['default'] }, "
        "rule: { kind: 'enum', values: ['value'] }, env: null, "
        "reviewMdKey: null, defaults: { headless: ['value', 'default'] }, "
        "type: 'string', waistPath: 'nested.added', waistMap: null, derivedFrom: null, "
        "deriveWhen: null, nullReceipt: [], resolvedKey: false },\n"
    )
    path.write_text(source.replace("\n]);", "\n" + row + "]);", 1), encoding="utf-8")
    args = ["--repo-root", str(registry_tree)]
    stale = invoke("generate_contract_requirements", [*args, "--check"], registry_tree)
    assert stale.returncode == 1
    assert stale.stderr == (
        b"generate_contract_requirements: stale generated registry blocks: "
        b"skills/code-gauntlet/SKILL.md, "
        b"skills/code-gauntlet/references/phase2-triage.md, "
        b"skills/code-gauntlet/references/phase1-preflight.md, "
        b"scripts/gauntlet/registry.py; run: python3 "
        b"scripts/generate_contract_requirements.py\n"
    )
    assert invoke("generate_contract_requirements", args, registry_tree).returncode == 0
    assert (
        invoke(
            "generate_contract_requirements", [*args, "--check"], registry_tree
        ).returncode
        == 0
    )


def test_command_identity_diagnostic(registry_tree, invoke):
    target = registry_tree / DELIVERY
    text = re.sub(
        r"<!-- generated-from-registry-identity:summary_header[^\n]*\n"
        r".*?<!-- /generated-from-registry-identity:summary_header -->\n",
        "",
        target.read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    target.write_text(text, encoding="utf-8")
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree), "--check"],
        registry_tree,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert "no marker pair for identity symbol(s) summary_header" in (
        result.stderr.decode()
    )
    assert re.fullmatch(
        r"generate_contract_requirements: [^\n]+\n", result.stderr.decode()
    )


def test_both_report_regions(registry_tree, invoke):
    target = registry_tree / REPORT
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
    rel = DELIVERY
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


def test_registry_diagnostic(registry_tree, live_identity, monkeypatch, invoke):
    identity = deepcopy(live_identity)
    identity["findingTypes"]["id"] = "boolean"
    monkeypatch.setattr(gen, "load_registry", lambda root: identity)
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(registry_tree), "--check"],
        registry_tree,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert "unknown finding schema type for id: 'boolean'" in result.stderr.decode()
    assert re.fullmatch(
        r"generate_contract_requirements: [^\n]+\n", result.stderr.decode()
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


def test_registry_source_projection(registry_tree, invoke):
    path = registry_tree / "workflows/src/injection.js"
    contents = path.read_text(encoding="utf-8")
    assert "FIX_MAX_LINES = 100" in contents
    path.write_text(
        contents.replace("FIX_MAX_LINES = 100", "FIX_MAX_LINES = 101"), encoding="utf-8"
    )
    args = ["--repo-root", str(registry_tree)]
    stale = invoke("generate_contract_requirements", [*args, "--check"], registry_tree)
    assert stale.returncode == 1
    assert stale.stdout == b""
    assert b"scripts/gauntlet/registry.py" in stale.stderr
    repaired = invoke("generate_contract_requirements", args, registry_tree)
    assert repaired.returncode == 0
    assert repaired.stderr == b""
    namespace: dict[str, Any] = {}
    exec(
        (registry_tree / "scripts/gauntlet/registry.py").read_text(encoding="utf-8"),
        namespace,
    )
    assert namespace["FIX_MAX_LINES"] == 101
