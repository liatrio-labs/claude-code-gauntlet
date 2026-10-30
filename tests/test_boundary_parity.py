"""Persisted findings and resolver payloads cross the live runtime boundaries."""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import gauntlet.text as outbound_text
import pytest
from gauntlet import config as resolve_config
from gauntlet import patches as report_patches
from gauntlet.delivery import compose
from gauntlet.delivery.input import Finding
from gauntlet.verify import wire as verify_wire

from bench.runner import invoke
from tests.support.forge import FakeForgeFactory
from tests.support.pipeline import (
    REPO,
    PersistedPayload,
    build_gh_diff,
    methodology_input,
    run_node,
)
from tests.support.posting import invoke_posting, review
from tests.support.verify_parity import project_findings, run_verify_slice

READ_INPUT = (
    "let source = ''; for await (const chunk of process.stdin) source += chunk;"
)
RENDER_REPORT = (
    "import { renderReport } from './workflows/src/renderReport.js';"
    + READ_INPUT
    + "process.stdout.write(renderReport(JSON.parse(source)));"
)
RENDER_SUMMARIES = (
    "import { renderReport, renderSummaryBody } "
    "from './workflows/src/renderReport.js';"
    + READ_INPUT
    + "const x=JSON.parse(source);"
)


@pytest.mark.parametrize(
    ("filepath", "status", "expected"),
    [
        ("app/[[...slug]]/page.tsx", "confirmed", "`app/[\uff3b...slug]]/page.tsx:12`"),
        ("src/![a](u).py", "confirmed", "`src/!\uff3ba](u).py:12`"),
        ("src/item.py", "unconfirmed", "`src/item.py (location unconfirmed)`"),
    ],
)
def test_summary_and_skipped_location_bytes(
    filepath: str, status: str, expected: str
) -> None:
    finding = {
        "id": "location",
        "severity": "high",
        "file": filepath,
        "line_start": 12,
        "line_end": 12,
        "location_status": status,
        "admission": {"version": 1, "status": "admitted", "input_proof": "fixture"},
        "disposition": "deliver",
        "title": "Location",
    }
    summary = run_node(
        "import { renderSummaryBody } from './workflows/src/renderReport.js';"
        + READ_INPUT
        + "process.stdout.write(renderSummaryBody(JSON.parse(source)));",
        {"findings": [finding]},
    ).stdout
    assert expected in summary
    assert outbound_text.prepare_prose(summary) == summary
    skipped = compose.build_skipped_section(
        [compose.SkippedEntry(filepath, 12, finding)]
    )
    location = next(
        line[5:] for line in skipped.splitlines() if line.startswith("#### ")
    )
    assert location == expected
    assert location.encode("utf-8") in summary.encode("utf-8")


def test_permalinked_summary_slice_and_guard_equality() -> None:
    fixture = {
        "prIdentity": {
            "platform": "github",
            "web_origin": "https://github.com",
            "owner": "o",
            "repo": "r",
            "sha_full": "a" * 40,
        },
        "findings": [
            {
                "id": "p",
                "severity": "high",
                "file": "app/@modal/<Slot>.tsx",
                "line_start": 8,
                "line_end": 8,
                "location_status": "confirmed",
                "admission": {
                    "version": 1,
                    "status": "admitted",
                    "input_proof": "fixture",
                },
                "disposition": "deliver",
                "title": (
                    "Use `foo()` and `<Slot>`; mail dev@example.test; &#\u0666\u0664;"
                ),
            },
            {
                "id": "crossing-location",
                "severity": "low",
                "file": "src/a<`b.py",
                "line_start": 1,
                "line_end": 1,
                "location_status": "confirmed",
                "admission": {
                    "version": 1,
                    "status": "admitted",
                    "input_proof": "fixture",
                },
                "disposition": "deliver",
                "title": "Path boundary",
            },
        ],
    }
    rendered: list[str] = json.loads(
        run_node(
            RENDER_SUMMARIES + "process.stdout.write(JSON.stringify([renderReport(x),"
            "renderSummaryBody(x)]));",
            fixture,
        ).stdout
    )
    report, summary = rendered
    assert compose.summary_body_from_report(report) == summary
    assert outbound_text.prepare_prose(summary) == summary
    assert "app/\uff20modal/\uff1cSlot>.tsx" in summary
    assert "dev@example.test" in summary
    assert "&#\u0666\u0664;" in summary
    assert "%40modal" in summary
    assert "%3CSlot%3E.tsx" in summary
    assert "src/a\uff1c`b.py" in summary
    assert "src/a%3C%60b.py" in summary


def test_withheld_primary_summary_cross_runtime() -> None:
    findings = [
        {
            "id": "p",
            "title": "Primary",
            "file": "a.js",
            "line_start": 1,
            "line_end": 1,
            "location_status": "confirmed",
            "admission": {"version": 1, "status": "admitted", "input_proof": "fixture"},
            "disposition": "deliver",
            "severity": "high",
            "confidence": 10,
            "consolidation_key": "g",
            "consolidation_primary": True,
        },
        {
            "id": "c",
            "title": "Lower ranked child",
            "file": "lower.js",
            "line_start": 11,
            "line_end": 11,
            "location_status": "confirmed",
            "admission": {"version": 1, "status": "admitted", "input_proof": "fixture"},
            "disposition": "deliver",
            "severity": "high",
            "confidence": 60,
            "consolidation_key": "g",
            "consolidation_primary": False,
        },
        {
            "id": "h",
            "title": "Higher ranked child",
            "file": "higher.js",
            "line_start": 12,
            "line_end": 12,
            "location_status": "confirmed",
            "admission": {"version": 1, "status": "admitted", "input_proof": "fixture"},
            "disposition": "deliver",
            "severity": "high",
            "confidence": 95,
            "consolidation_key": "g",
            "consolidation_primary": False,
        },
    ]
    rendered: list[str] = json.loads(
        run_node(
            RENDER_SUMMARIES + "x.delivered=x.deliveredIndexes.map(i=>x.findings[i]);"
            "delete x.deliveredIndexes;"
            "process.stdout.write(JSON.stringify([renderReport(x),"
            "renderSummaryBody(x)]));",
            {
                "summary": "The PR claims to change things.",
                "findings": findings,
                "deliveredIndexes": [2, 1],
            },
        ).stdout
    )
    report, body = rendered
    assert compose.summary_body_from_report(report) == body
    delivered = cast(list[Finding], [findings[2], findings[1]])
    groups = compose.consolidate_delivery(delivered)
    bullets = [line for line in body.splitlines() if line.startswith("- ")]
    assert len(bullets) == len(groups) == 1
    assert groups[0].primary["title"] == "Higher ranked child"
    assert groups[0].primary["title"] in bullets[0]
    assert groups[0].primary["file"] in bullets[0]
    assert "Lower ranked child" not in bullets[0]
    assert "The PR claims" not in body


@pytest.mark.parametrize("body_primary", [False, True])
@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_mixed_disposition_group_cross_runtime(
    body_primary, platform, tmp_path, monkeypatch, capsys, forge_factory
):
    findings = [
        {
            "id": stamp,
            "trace_id": f"nonce:0:{index}",
            "file": "app.js",
            "line_start": line,
            "line_end": line,
            "location_status": "confirmed",
            "anchor": "return process(value);",
            "title": f"{stamp} claim",
            "description": f"Evidence from {stamp}",
            "severity": "high",
            "confidence": 99 if (stamp == "body") == body_primary else 90,
            "agent": agent,
            "dimension": "bug",
            "diff_validation": {"in_diff": stamp == "deliver", "reason": stamp},
        }
        for index, (stamp, line, agent) in enumerate(
            [("body", 4, "security-reviewer"), ("deliver", 3, "bug-detector")]
        )
    ]
    disposed = json.loads(
        run_node(
            "import { disposeFindings } from './workflows/src/dispose.js';"
            "import { evaluateAdmission } from './workflows/src/admission.js';"
            + READ_INPUT
            + "const input=JSON.parse(source);input.findings=input.findings.map(f=>evaluateAdmission({...f,origin:'new',path_role:'source',validation:'validated',policy_subject:'ordinary',challenge:'survived'}));"
            + "process.stdout.write(JSON.stringify(disposeFindings(input)));",
            {"findings": findings},
        ).stdout
    )
    members = disposed["findings"]
    assert [f["disposition"] for f in members] == ["body", "deliver"]
    assert members[0]["consolidation_key"] == members[1]["consolidation_key"]
    groups = compose.consolidate_delivery(cast(list[Finding], members))
    assert len(groups) == 1
    primary, child = members[1], members[0]
    assert groups[0].primary == primary
    assert groups[0].corroborators == (child,)
    folded = [d for d in disposed["decisions"] if d["reason_code"] == "folded"]
    assert len(folded) == 1 and folded[0]["trace_id"] == child["trace_id"]
    assert folded[0]["related_trace_ids"] == [primary["trace_id"]]
    for finding in members:
        finding["body"] = finding["description"]
    result = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        review(platform, members, review_body=""),
        diff=(
            "diff --git a/app.js b/app.js\n--- a/app.js\n+++ b/app.js\n"
            "@@ -3 +3 @@\n-old_value();\n+return process(value);\n"
        ),
    )
    assert result.code == 0, result.err
    payload = result.payload
    assert payload is not None
    summary = (
        payload["payload"]["body"]
        if platform == "github"
        else payload["summary"]["body"]
    )
    comments = (
        payload["payload"]["comments"]
        if platform == "github"
        else payload["discussions"]
    )
    assert len(comments) == 1
    assert all(f["title"] in comments[0]["body"] for f in members)
    assert "Corroborating finding" in comments[0]["body"]
    assert all(f["title"] not in summary for f in members)


def test_pipeline_fields_reach_poster(
    pipeline_payload: PersistedPayload,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forge_factory: FakeForgeFactory,
) -> None:
    findings = pipeline_payload["findings"]
    assert [finding["id"] for finding in findings] == ["F1", "F2"]
    assert findings[0]["suggestion"] == (
        "Guard the member lookup before dereferencing it on the API-key path."
    )
    assert findings[0]["claude_md_rule"] == (
        "Every auth path must null-check the member (CLAUDE.md section 4)"
    )
    assert findings[1]["suggestion"] == (
        "Add a test that raises PaymentGatewayError and asserts the rollback."
    )
    assert findings[1]["spec_text"] == (
        "A failed payment MUST leave no partial transaction."
    )
    assert findings[1]["criticality"] == 9
    assert findings[1]["failure_scenario"] == (
        "A regression dropping the rollback would go undetected."
    )
    result = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        pipeline_payload["postReview"],
        diff=build_gh_diff(findings),
    )
    assert result.code == 0, result.err
    assert result.payload is not None
    assert result.payload["platform"] == "github"
    assert (
        pipeline_payload["postReview"]["web_origin"] == "https://git.example.com:8443"
    )
    assert result.forge_factory_calls == (("github", "git.example.com"),)
    comments = result.payload["payload"]["comments"]
    assert len(comments) == 2
    for finding, comment in zip(findings, comments, strict=True):
        body = comment["body"]
        assert "**Suggested fix:**" in body
        assert finding["suggestion"] in body
        assert "**Cited rule:**" in body
        assert (finding.get("claude_md_rule") or finding["spec_text"]) in body
        # failure_scenario is artifact-only and must never reach a PR comment.
        assert findings[1]["failure_scenario"] not in body
        trailer = "\u2694\ufe0f *Code Gauntlet*"
        assert body.splitlines()[-1] == trailer
        assert body.count(trailer) == 1


def test_missing_line_alias_degrades_gracefully_not_a_crash(
    pipeline_payload: PersistedPayload,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forge_factory: FakeForgeFactory,
) -> None:
    findings = pipeline_payload["findings"]
    stripped = [
        {
            key: value
            for key, value in finding.items()
            if key not in ("line", "end_line", "body")
        }
        for finding in findings
    ]
    result = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        {
            "platform": "github",
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "review_body": "Code gauntlet summary",
            "findings": stripped,
        },
        diff=build_gh_diff(findings),
    )
    assert result.code == 0, result.err
    assert result.payload is not None
    payload = result.payload["payload"]
    assert len(payload["comments"]) == 2
    assert "could not be anchored inline" not in payload["body"]


@pytest.mark.parametrize(
    ("drop_end_alias", "kept", "reasons"),
    [
        pytest.param(False, 2, {}, id="kept"),
        pytest.param(True, 2, {}, id="missing-end-alias"),
    ],
)
def test_pipeline_patch_alias_contract(
    drop_end_alias: bool,
    kept: int,
    reasons: dict[str, int],
    pipeline_payload: PersistedPayload,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    findings = pipeline_payload["findings"]
    diff = build_gh_diff(findings)
    for finding in findings:
        finding["suggested_fix_code"] = "const patched = true;"
        if drop_end_alias:
            finding.pop("end_line", None)
    (tmp_path / "code-gauntlet-findings-abc1234.json").write_text(
        json.dumps(findings), encoding="utf-8"
    )
    (tmp_path / "code-gauntlet-diff-abc1234.patch").write_text(diff, encoding="utf-8")
    code = report_patches.CLI.invoke(
        ["--output-dir", str(tmp_path), "--head-sha", "abc1234"]
    )
    output = capsys.readouterr().out
    assert code == 0, output
    receipt: dict[str, Any] = json.loads(output.strip().splitlines()[-1])
    assert receipt["kept"] == kept
    assert receipt["reasons"] == reasons
    assert receipt["findings"] == receipt["candidates"] == 2
    assert receipt["downgraded"] == 2 - kept


@pytest.mark.parametrize("mode", ["headless", "interactive"])
def test_both_receipt_modes_cross_the_bench_parsers(mode: str) -> None:
    if mode == "headless":
        echo = {
            "model_tier": {"value": "optimized", "source": "env"},
            "delivery": {"value": "pr_comments,markdown", "source": "env"},
            "post_mode": {"value": "dry-run", "source": "env"},
            "pr_comment_cap": {"value": "25", "source": "env"},
            "delivery_tier": {"value": "all", "source": "default"},
            "draft_policy": {"value": "review", "source": "env"},
            "reviewed_policy": {"value": "full", "source": "env"},
            "pr_not_found_policy": {"value": "error", "source": "env"},
            "trivial_scope": {"value": "full", "source": "env"},
        }
        expected = dict(invoke.EXPECTED_ECHO)
    else:
        echo = {
            "model_tier": {"value": "optimized", "source": "fixed"},
            "pr_comment_cap": {"value": "null", "source": "default"},
            "delivery_tier": {"value": "all", "source": "default"},
            "review_md": {"value": "absent", "source": "discovery"},
        }
        expected = {
            "model_tier": "optimized",
            "delivery_tier": "all",
            "pr_comment_cap": "null",
            "review_md": "absent",
        }
    report = run_node(
        RENDER_REPORT,
        methodology_input(mode, echo, delivery_cap=25 if mode == "headless" else None),
    ).stdout
    with patch.object(invoke, "EXPECTED_ECHO", expected):
        assert invoke._echo_in_text(report), mode
    assert dict(invoke._IDENTITY_LINE_RE.findall(report)) == {
        "pipeline_version": "3.26.0",
        "plugin_root": "/absolute/path/to/claude-code-gauntlet",
    }


@pytest.mark.parametrize(
    ("mode", "carrier", "value", "token"),
    [
        pytest.param("headless", None, None, None, id="headless-default"),
        pytest.param("interactive", None, None, None, id="interactive-default"),
        pytest.param("headless", "value", "`code`", "code", id="backticks"),
        pytest.param("interactive", "source", "env\rinjected", "env injected", id="cr"),
        pytest.param("headless", "pipeline_version", "v\nnext", "v next", id="lf"),
        pytest.param("interactive", "plugin_root", "\t/root\t", "/root", id="trim"),
        pytest.param("headless", "value", "a  b", "a b", id="spaces"),
        pytest.param("interactive", "source", "", "unknown", id="empty-source"),
    ],
)
def test_receipt_rendering_parity(
    mode: str, carrier: str | None, value: str | None, token: str | None
) -> None:
    resolved = resolve_config.resolve(mode, {}, None, "pr")
    echo = resolved["configEcho"]
    if mode == "interactive":
        echo["review_md"] = {"value": "absent", "source": "discovery"}
    identity = {"pipeline_version": "version", "plugin_root": "/absolute/root"}
    if carrier in ("value", "source"):
        echo["model_tier"][carrier] = value
    elif carrier is not None:
        assert value is not None
        identity[carrier] = value
    expected = resolve_config.render_block(mode, echo, identity)
    report = run_node(
        RENDER_REPORT,
        methodology_input(
            mode,
            echo,
            pipeline_version=identity["pipeline_version"],
            plugin_root=identity["plugin_root"],
        ),
    ).stdout
    label = "Headless" if mode == "headless" else "Resolved"
    match = re.search(rf"```text\n({label} config:\n(?:  [^\n]+\n)+)```", report)
    assert match is not None
    actual = match.group(1).rstrip("\n")
    assert actual == expected
    if carrier is None:
        source = "default" if mode == "headless" else "fixed"
        assert f"  model_tier=optimized ({source})" in actual
        assert "  pipeline_version=version (bundle)" in actual
        assert "  plugin_root=/absolute/root (resolved)" in actual
        assert "  delivery_tier=all (default)" in actual
    else:
        key = carrier if carrier in identity else "model_tier"
        if carrier == "source":
            assert f"  {key}=optimized ({token})" in actual
        else:
            assert f"  {key}={token} (" in actual


@pytest.mark.parametrize(
    ("mode", "env", "light_eligible", "with_detector", "cap", "tier"),
    [
        pytest.param("headless", {}, False, False, 6, "all", id="headless"),
        pytest.param(
            "headless",
            {"CODE_GAUNTLET_MEASUREMENT_TRACE": "1"},
            False,
            False,
            6,
            "all",
            id="measurement-trace",
        ),
        pytest.param(
            "headless",
            {
                "CODE_GAUNTLET_PR_COMMENT_CAP": "25",
                "CODE_GAUNTLET_DELIVERY_TIER": "main_only",
                "CODE_GAUNTLET_REVIEWED_POLICY": "skip",
                "CODE_GAUNTLET_TRIVIAL_SCOPE": "light",
            },
            True,
            True,
            25,
            "main_only",
            id="combined-with-detector",
        ),
        pytest.param(
            "headless",
            {"CODE_GAUNTLET_TRIVIAL_SCOPE": "light"},
            True,
            False,
            6,
            "all",
            id="eligible-light",
        ),
        pytest.param("interactive", {}, False, False, None, "all", id="interactive"),
        pytest.param(
            "interactive",
            {
                "CODE_GAUNTLET_PR_COMMENT_CAP": "0",
                "CODE_GAUNTLET_DELIVERY_TIER": "main_only",
            },
            False,
            False,
            0,
            "main_only",
            id="zero-cap",
        ),
        pytest.param(
            "interactive",
            {"CODE_GAUNTLET_PR_COMMENT_CAP": "null"},
            False,
            False,
            None,
            "all",
            id="explicit-null",
        ),
    ],
)
def test_resolver_payload_crosses_args_waist(
    mode: str,
    env: dict[str, str],
    light_eligible: bool,
    with_detector: bool,
    cap: int | None,
    tier: str,
) -> None:
    payload = resolve_config.resolve(mode, env, None, "pr")
    payload["mode"] = mode
    if with_detector:
        payload["reviewScope"] = {
            "kind": "full",
            "since": None,
            "commits": None,
            "detector": {
                "previously_reviewed": True,
                "sha_resolvable": True,
                "head_advanced": True,
                "sha_is_ancestor": True,
                "incremental_safe": True,
                "error": None,
            },
        }
    result: dict[str, Any] = json.loads(
        run_node(
            "import { normalizeArgsReport, validateArgs } "
            "from './workflows/src/args.js';"
            "import { validArgs } from './workflows/test/helpers/argsFixtures.js';"
            + READ_INPUT
            + "const {payload,lightEligible}=JSON.parse(source);"
            "const args=validArgs();"
            "args.mode=payload.mode; Object.assign(args,payload.waist);"
            "args.limits={...args.limits}; delete args.limits.deliveryCap;"
            "delete args.delivery; delete args.scopeAnswer;"
            "if (Object.hasOwn(payload,'reviewScope')) "
            "args.reviewScope=payload.reviewScope;"
            "if (lightEligible) {args.riskTable=[{path:'a.js',risk:'low'}];"
            "args.changedLines=1;}"
            "const normalized=normalizeArgsReport(args).args;"
            "process.stdout.write(JSON.stringify({"
            "result:validateArgs(normalized),args:normalized}));",
            {"payload": payload, "lightEligible": light_eligible},
        ).stdout
    )
    assert result["result"] == {"ok": True, "errors": []}
    args = result["args"]
    for key, value in payload["waist"]["configEcho"].items():
        assert args["configEcho"][key] == value
    assert args.get("measurementTrace") == payload["waist"].get("measurementTrace")
    assert args["limits"]["deliveryCap"] == cap
    assert args["delivery"]["tier"] == tier
    assert args["reviewScope"]["requested"] == "full"
    if light_eligible:
        assert args["scopeAnswer"] == "light"
    else:
        assert "scopeAnswer" not in args


def test_identity_payload_crosses_args_waist() -> None:
    produced = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/resolve_pr_identity.py"),
            "--platform",
            "gitlab",
            "--sha",
            "0123456789abcdef0123456789abcdef01234567",
        ],
        cwd=REPO,
        input=json.dumps(
            {
                "web_url": "http://gitlab.example:8080/group/sub/repo/-/merge_requests/281"
            }
        ),
        capture_output=True,
        text=True,
        timeout=10,
        encoding="utf-8",
        check=True,
    )
    identity: dict[str, Any] = json.loads(produced.stdout)
    result: dict[str, Any] = json.loads(
        run_node(
            "import { validateArgs } from './workflows/src/args.js';"
            "import { validArgs } from './workflows/test/helpers/argsFixtures.js';"
            + READ_INPUT
            + "const identity=JSON.parse(source);"
            "const args=validArgs({delivery:{tier:'all',prIdentity:identity}});"
            "process.stdout.write(JSON.stringify(validateArgs(args)));",
            identity,
        ).stdout
    )
    assert result == {"ok": True, "errors": []}


def test_verify_consumes_pipeline_findings_without_error(
    tmp_path: Path, pipeline_payload: PersistedPayload, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = pipeline_payload["findings"]
    diff_path = tmp_path / "diff.patch"
    diff_path.write_text(build_gh_diff(findings), encoding="utf-8")
    monkeypatch.chdir(REPO)
    envelope, result = run_verify_slice(
        {"findings": findings, "base_branch": "main"},
        tmp_path,
        head_sha="abcd",
        nonce="boundary",
        diff_file=diff_path,
    )
    assert result.stdout == ""
    assert list(envelope) == ["status", "receipt", "result"]
    assert envelope["status"] == "ok"
    assert list(envelope["receipt"]) == [
        "sha",
        "n_in",
        "nonce",
        "deltas_checksum",
        "inline_checksum",
        "base_branch",
        "diff_source",
        "record_proofs",
        "input_checksum",
    ]
    declared = json.loads(
        run_node(
            "import { verifyStage } from './workflows/src/verifyStage.js';"
            "import { verifyCtx, verifyInput } from './workflows/test/helpers/verifyDelta.js';"
            + READ_INPUT
            + "const findings=JSON.parse(source); const ctx=verifyCtx(()=>null);"
            "await verifyStage(ctx,verifyInput(findings));"
            "process.stdout.write(JSON.stringify({"
            "keys:Object.keys(ctx.calls[0].schema.properties.receipt.properties),"
            "prompt:ctx.calls[0].prompt}));",
            findings,
        ).stdout
    )
    assert set(declared["keys"]) == set(envelope["receipt"])
    assert all(key in declared["prompt"] for key in declared["keys"])
    assert envelope["receipt"]["sha"] == "abcd"
    assert envelope["receipt"]["n_in"] == 2
    assert envelope["receipt"]["nonce"] == "boundary"
    assert envelope["receipt"]["diff_source"] == {
        "path": str(diff_path),
        "read": True,
    }
    assert list(envelope["result"]) == ["deltas", "verified", "eliminated", "stats"]
    assert [
        (delta["id"], delta["verified"]) for delta in envelope["result"]["deltas"]
    ] == [("F1", True), ("F2", True)]
    assert envelope["result"]["eliminated"] == []
    assert [finding["id"] for finding in envelope["result"]["verified"]] == [
        "F1",
        "F2",
    ]
    assert envelope["result"]["stats"] == {
        "total": 2,
        "new": 2,
        "surfaced": 0,
        "eliminated": 0,
    }
    saved: dict[str, Any] = json.loads(
        (tmp_path / "slice.json").read_text(encoding="utf-8")
    )
    assert saved == {
        "findings": project_findings(findings),
        "base_branch": "main",
    }


@pytest.mark.parametrize("fault", ["symbols", "has_cross_file_refs"])
def test_verify_localises_real_receipt_input_typo(
    fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verify_git
) -> None:
    findings = [
        {
            "id": f"F{i}",
            "file": "source",
            "line_start": 1,
            "line_end": 1,
            "location_status": "confirmed",
            "admission": {"version": 1, "status": "admitted", "input_proof": "fixture"},
            "disposition": "deliver",
            "severity": "high",
            "confidence": 80,
            "description": "`alpha_token` and `beta_token`",
            "cross_file_refs": ["other:1"],
        }
        for i in range(3)
    ]
    (tmp_path / "source").write_text("alpha_token beta_token\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    verify_git[1]["rev-parse"] = (str(tmp_path), "", 0)
    document = {"findings": project_findings(findings), "base_branch": "main"}
    if fault == "symbols":
        document["findings"][1]["symbols"] = "alpha_token"
    else:
        del document["findings"][1]["has_cross_file_refs"]
    token = run_node(
        "import { encodeSliceInline } from './workflows/src/verifyWire.js';"
        + READ_INPUT
        + "process.stdout.write(encodeSliceInline(JSON.parse(source)));",
        document,
    ).stdout
    args = verify_wire.CLI.parser.parse_args(
        [
            "--target-kind",
            "branch",
            "--input",
            str(tmp_path / "slice.json"),
            "--input-inline",
            token,
            "--head-sha",
            "abc123",
            "--nonce",
            "n-1.0",
        ]
    )
    envelope = verify_wire.run_receipt(args)
    assert envelope["status"] == "ok"
    assert [row["id"] for row in envelope["receipt"]["record_proofs"]] == [
        "F0",
        "F1",
        "F2",
    ]
    stage = json.loads(
        run_node(
            "import { verifyStage } from './workflows/src/verifyStage.js';"
            "import { verifyCtx, verifyInput, dispatchedFindings } "
            "from './workflows/test/helpers/verifyDelta.js';"
            + READ_INPUT
            + "const x=JSON.parse(source);"
            "const ctx=verifyCtx((_i,attempt)=>attempt===1?x.envelope:{status:'failed'});"
            "const input=verifyInput(x.findings); input.verify.diffPath=null;"
            "const out=await verifyStage(ctx,input);"
            "process.stdout.write(JSON.stringify({out,"
            "attempts:ctx.calls.map(c=>dispatchedFindings(c.prompt).map(f=>f.id))}));",
            {"envelope": envelope, "findings": findings},
        ).stdout
    )
    assert stage["attempts"] == [["F0", "F1", "F2"], ["F1"]]
    assert stage["out"]["verified"] is False
    assert [(f["id"], f["origin"]) for f in stage["out"]["findings"]] == [
        ("F0", "surfaced"),
        ("F1", "unknown"),
        ("F2", "surfaced"),
    ]
