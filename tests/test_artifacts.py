"""Persist-plan structure, proofs, projection bytes, and partial writes."""

import json
import shutil
import subprocess

import pytest
from gauntlet import artifacts, cli

from tests.support.artifacts import artifact_plan, seal_plan

SOURCE = '[{"id":"A","line":4,"body":"b"}]'
POST = '[\n  {\n    "id": "A",\n    "line": 4,\n    "body": "b"\n  }\n]'
CHECKPOINT = """{
  "phases": {
    "challenge": {
      "before": 1,
      "findings": [
        {
          "id": "A"
        }
      ],
      "after": 2
    }
  }
}"""


@pytest.mark.parametrize(
    "defect,errors",
    [
        pytest.param(
            "plan-object",
            ["plan must be a JSON object: plan.json"],
            id="STRUCTURE-plan-object",
        ),
        pytest.param(
            "version",
            ["unsupported planVersion 3 (expected 2)"],
            id="STRUCTURE-version",
        ),
        pytest.param(
            "expected-read",
            ["expected artifact not found or unreadable: report.md (denied)"],
            id="STRUCTURE-expected-read",
        ),
        pytest.param(
            "expected-json",
            ["expected artifact is not valid JSON: expected.json (Extra data"],
            id="STRUCTURE-expected-json",
        ),
        pytest.param(
            "source-read",
            ["source not found or unreadable: findings.json (denied)"],
            id="STRUCTURE-cached-source-read",
        ),
        pytest.param(
            "source-array",
            ["source must be a JSON array of findings: findings.json"],
            id="STRUCTURE-source-array",
        ),
        pytest.param(
            "source-entry",
            ["source entry 0 is not an object: findings.json"],
            id="STRUCTURE-source-entry",
        ),
        pytest.param(
            "source-id",
            ["source entry 0 has no usable string id: findings.json"],
            id="STRUCTURE-source-id",
        ),
        pytest.param(
            "source-missing-id",
            ["source entry 0 has no usable string id: findings.json"],
            id="STRUCTURE-source-missing-id",
        ),
        pytest.param(
            "duplicate",
            ["duplicate id 'A' in source: findings.json"],
            id="STRUCTURE-duplicate-id",
        ),
        pytest.param(
            "post-id",
            ["postReview id 'GHOST' not present in source findings.json"],
            id="STRUCTURE-post-id",
        ),
        pytest.param(
            "challenge-id",
            ["challenge id 'GHOST' not present in source findings.json"],
            id="STRUCTURE-challenge-id",
        ),
        pytest.param(
            "slot",
            [
                "checkpoint skeleton has no phases.challenge.findings array to receive 1 challenge finding(s)"
            ],
            id="STRUCTURE-challenge-slot",
        ),
        pytest.param(
            "missing-phase",
            [
                "checkpoint skeleton has no phases.challenge.findings array to receive 1 challenge finding(s)"
            ],
            id="STRUCTURE-missing-challenge-phase",
        ),
    ],
)
def test_structural_failure(defect, errors, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = {
        "source-array": "{}",
        "source-entry": "[7]",
        "source-id": '[{"id":""}]',
        "source-missing-id": '[{"title":"x"}]',
        "duplicate": '[{"id":"A"},{"id":"A"}]',
    }.get(defect, SOURCE)
    (tmp_path / "findings.json").write_text(source, encoding="utf-8")
    plan = artifact_plan()
    if defect == "version":
        plan["planVersion"] = 3
    elif defect == "expected-read":
        plan["expect"] = [
            {"path": "report.md", "chars": 1, "checksum": "fnv1a32:0xfd0c5087"}
        ]
    elif defect == "expected-json":
        (tmp_path / "expected.json").write_text("[] trailing", encoding="utf-8")
        plan["expect"] = [{"path": "expected.json"}]
    elif defect == "post-id":
        plan["postReview"]["ids"] = ["GHOST"]
    elif defect == "challenge-id":
        plan["checkpoint"]["challengeFindingIds"] = ["GHOST"]
    elif defect == "slot":
        plan["checkpoint"]["skeleton"] = {"phases": {"challenge": {"findings": None}}}
    elif defect == "missing-phase":
        plan["checkpoint"]["skeleton"] = {"phases": {}, "completed": []}
    seal_plan(tmp_path / "plan.json", plan)
    if defect == "plan-object":
        (tmp_path / "plan.json").write_text("[]", encoding="utf-8")
    if defect in ("expected-read", "source-read"):
        blocked = {
            "expected-read": "report.md",
            "source-read": "findings.json",
        }[defect]
        real = artifacts._read_content

        def read(path):
            if path == blocked:
                raise OSError("denied")
            return real(path)

        monkeypatch.setattr(artifacts, "_read_content", read)
    # Existing outputs must survive structural refusal just as absent outputs must stay absent.
    (tmp_path / "post.json").write_text("old", encoding="utf-8")
    receipt = artifacts.assemble("plan.json")
    assert receipt["ok"] is False
    if defect == "expected-json":
        assert len(receipt["errors"]) == 1
        assert receipt["errors"][0].startswith(errors[0])
        assert receipt["errors"][0].endswith(")")
    else:
        assert receipt["errors"] == errors
    assert receipt["written"] == []
    assert (tmp_path / "post.json").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "checkpoint.json").exists()


@pytest.mark.parametrize(
    ("parse_site", "error_text"),
    [
        (
            "source",
            "source is not valid JSON: findings.json (injected parser depth failure)",
        ),
        ("plan", "plan is not valid JSON: plan.json (injected parser depth failure)"),
        (
            "expected",
            "expected artifact is not valid JSON: expected.json (injected parser depth failure)",
        ),
    ],
    ids=[
        "STRUCTURE-source-recursion",
        "STRUCTURE-plan-recursion",
        "STRUCTURE-expected-recursion",
    ],
)
def test_recursion_errors_during_json_parsing_are_structural(
    tmp_path, monkeypatch, parse_site, error_text
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(SOURCE, encoding="utf-8")
    plan = artifact_plan()
    if parse_site == "expected":
        (tmp_path / "expected.json").write_text("[]", encoding="utf-8")
        plan["expect"] = [{"path": "expected.json"}]
    seal_plan(tmp_path / "plan.json", plan)
    failing_text = {
        "source": SOURCE,
        "plan": (tmp_path / "plan.json").read_text(encoding="utf-8"),
        "expected": "[]",
    }[parse_site]
    original_loads = json.loads
    raised = False

    def loads_with_targeted_recursion_error(content, *args, **kwargs):
        nonlocal raised
        if content == failing_text and not raised:
            raised = True
            raise RecursionError("injected parser depth failure")
        return original_loads(content, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads_with_targeted_recursion_error)
    receipt = artifacts.assemble("plan.json")
    assert raised
    assert not receipt["ok"]
    assert receipt["errors"] == [error_text]
    assert receipt["written"] == []


@pytest.mark.parametrize(
    "site,path,error_prefix",
    [
        pytest.param(
            "source",
            "findings.json",
            "source not found or unreadable: findings.json ('utf-8' codec can't decode byte",
            id="STRUCTURE-source-invalid-utf8",
        ),
        pytest.param(
            "plan",
            "plan.json",
            "plan not found or unreadable: plan.json ('utf-8' codec can't decode byte",
            id="STRUCTURE-plan-invalid-utf8",
        ),
        pytest.param(
            "expected",
            "report.md",
            "expected artifact not found or unreadable: report.md ('utf-8' codec can't decode byte",
            id="STRUCTURE-expected-invalid-utf8",
        ),
    ],
)
def test_strict_utf8(site, path, error_prefix, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(SOURCE, encoding="utf-8")
    plan = artifact_plan()
    if site == "expected":
        plan["expect"] = [
            {"path": "report.md", "chars": 1, "checksum": "fnv1a32:0xfd0c5087"}
        ]
    seal_plan(tmp_path / "plan.json", plan)
    (tmp_path / path).write_bytes(b"\xff")
    receipt = artifacts.assemble("plan.json")
    assert receipt["ok"] is False
    assert len(receipt["errors"]) == 1
    assert receipt["errors"][0].startswith(error_prefix)
    assert receipt["errors"][0].endswith(")")
    assert receipt["written"] == []


@pytest.mark.parametrize(
    "declared",
    [
        None,
    ],
    ids=[
        "PROOF-missing",
    ],
)
def test_unproven_instructions_are_refused(declared, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = artifact_plan()
    if declared is not None:
        plan["planChecksum"] = declared
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    receipt = artifacts.assemble("plan.json")
    assert receipt == {
        "ok": False,
        "planVersion": 2,
        "planChecksum": None,
        "verified": [],
        "written": [],
        "errors": [
            "plan carries no planChecksum \u2014 an unproven instruction set is not executed: plan.json"
        ],
    }


@pytest.mark.parametrize(
    "field,value,expected_chars,expected_checksum",
    [
        pytest.param("chars", 99, 99, "fnv1a32:0xfd0c5087", id="PROOF-primary-chars"),
        pytest.param("checksum", "bad", 1, "bad", id="PROOF-primary-checksum"),
    ],
)
def test_primary_mismatch_is_nonfatal(
    field, value, expected_chars, expected_checksum, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "report.md").write_text("x", encoding="utf-8")
    plan = artifact_plan()
    entry = {"path": "report.md", "chars": 1, "checksum": "fnv1a32:0xfd0c5087"}
    entry[field] = value
    plan["expect"] = [
        {"path": "findings.json", "chars": 32, "checksum": "fnv1a32:0x71b72159"},
        entry,
    ]
    seal_plan(tmp_path / "plan.json", plan)
    receipt = artifacts.assemble("plan.json")
    assert receipt["ok"] is True
    assert receipt["errors"] == []
    assert receipt["verified"] == [
        {
            "path": "findings.json",
            "chars": 32,
            "expected_chars": 32,
            "checksum": "fnv1a32:0x71b72159",
            "expected_checksum": "fnv1a32:0x71b72159",
            "content_proof": "match",
        },
        {
            "path": "report.md",
            "chars": 1,
            "expected_chars": expected_chars,
            "checksum": "fnv1a32:0xfd0c5087",
            "expected_checksum": expected_checksum,
            "content_proof": "mismatch",
        },
    ]
    assert (
        capsys.readouterr().err
        == f"content-proof mismatch: report.md (expected {expected_chars} chars / {expected_checksum}, got 1 / fnv1a32:0xfd0c5087)\n"
    )
    assert (tmp_path / "post.json").read_text(encoding="utf-8") == POST


def test_projection_receipt_and_bytes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(SOURCE, encoding="utf-8")
    plan = artifact_plan()
    plan["expect"] = [
        {"path": "findings.json", "chars": 32, "checksum": "fnv1a32:0x71b72159"}
    ]
    seal_plan(tmp_path / "plan.json", plan)
    assert artifacts.CLI.invoke(["--plan", "plan.json"]) == 0
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert captured.out.strip() == (
        '{"ok": true, "planVersion": 2, "planChecksum": "fnv1a32:0x9c74474d", '
        '"verified": [{"path": "findings.json", "chars": 32, "expected_chars": 32, '
        '"checksum": "fnv1a32:0x71b72159", "expected_checksum": "fnv1a32:0x71b72159", '
        '"content_proof": "match"}], "written": [{"path": "post.json", "chars": 57, '
        '"checksum": "fnv1a32:0xda7cfb7d"}, {"path": "checkpoint.json", "chars": 151, '
        '"checksum": "fnv1a32:0x428a53ce"}], "errors": []}'
    )
    assert receipt["verified"] == [
        {
            "path": "findings.json",
            "chars": 32,
            "expected_chars": 32,
            "checksum": "fnv1a32:0x71b72159",
            "expected_checksum": "fnv1a32:0x71b72159",
            "content_proof": "match",
        }
    ]
    assert receipt["written"] == [
        {"path": "post.json", "chars": 57, "checksum": "fnv1a32:0xda7cfb7d"},
        {"path": "checkpoint.json", "chars": 151, "checksum": "fnv1a32:0x428a53ce"},
    ]
    assert (tmp_path / "post.json").read_bytes() == POST.encode("utf-8")
    assert (tmp_path / "checkpoint.json").read_bytes() == CHECKPOINT.encode("utf-8")
    assert list(receipt) == [
        "ok",
        "planVersion",
        "planChecksum",
        "verified",
        "written",
        "errors",
    ]
    assert len(captured.out.splitlines()) == 1
    assert captured.err == ""


@pytest.mark.parametrize(
    "mode",
    [
        "wrapper",
        "wrapper-append",
        "missing-slot",
        "nonarray-slot",
    ],
    ids=[
        "ORDER-wrapper-existing-findings-key",
        "ORDER-wrapper-append-findings-key",
        "ORDER-no-fabricated-slot",
        "ORDER-preserved-nonarray",
    ],
)
def test_projection_order(mode, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(
        '[{"id":"A","line":4,"body":"b"},{"id":"B","line":5,"body":"c"}]',
        encoding="utf-8",
    )
    ids = [] if mode in ("missing-slot", "nonarray-slot") else ["B", "A", "B"]
    plan = artifact_plan(ids=ids)
    if mode == "wrapper":
        plan["postReview"]["wrapper"] = {"owner": "o", "findings": ["old"], "tail": 7}
    elif mode == "wrapper-append":
        plan["postReview"]["wrapper"] = {"owner": "o", "tail": 7}
    if mode == "missing-slot":
        plan["checkpoint"]["skeleton"] = {"phases": {"challenge": {"stats": {}}}}
    if mode == "nonarray-slot":
        plan["checkpoint"]["skeleton"] = {
            "phases": {"challenge": {"findings": "truncated"}}
        }
    seal_plan(tmp_path / "plan.json", plan)
    receipt = artifacts.assemble("plan.json")
    assert receipt["ok"] is True
    post = json.loads((tmp_path / "post.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    projected = post["findings"] if mode in ("wrapper", "wrapper-append") else post
    assert [item["id"] for item in projected] == ids
    if mode in ("wrapper", "wrapper-append"):
        assert list(post) == (
            ["owner", "findings", "tail"]
            if mode == "wrapper"
            else ["owner", "tail", "findings"]
        )
        assert post["owner"] == "o"
        assert post["tail"] == 7
    challenge = checkpoint["phases"]["challenge"]
    if mode == "missing-slot":
        assert challenge == {"stats": {}}
    elif mode == "nonarray-slot":
        assert challenge == {"findings": "truncated"}
    else:
        assert list(challenge) == ["before", "findings", "after"]
        assert challenge["findings"] == [{"id": fid} for fid in ids]


@pytest.mark.parametrize(
    "prefix,suffix,report,chars,checksum,state",
    [
        pytest.param(
            "\ufeff", "", "x", 1, "fnv1a32:0xfd0c5087", "match", id="NORMALIZE-bom"
        ),
        pytest.param(
            "", "\n", "x", 1, "fnv1a32:0xfd0c5087", "match", id="NORMALIZE-single-lf"
        ),
        pytest.param(
            "",
            "\r\n",
            "x",
            1,
            "fnv1a32:0xfd0c5087",
            "match",
            id="NORMALIZE-single-crlf",
        ),
        pytest.param(
            "",
            "\n\n",
            "x",
            2,
            "fnv1a32:0xe762cdf7",
            "mismatch",
            id="NORMALIZE-two-tails",
        ),
        pytest.param(
            "",
            "",
            "x\r\ny",
            4,
            "fnv1a32:0xd3cef7bf",
            "match",
            id="NORMALIZE-internal-crlf",
        ),
    ],
)
def test_content_normalization(
    prefix, suffix, report, chars, checksum, state, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "report.md").write_bytes((prefix + report + suffix).encode("utf-8"))
    expected_chars = 1 if state == "mismatch" else chars
    expected_checksum = "fnv1a32:0xfd0c5087" if state == "mismatch" else checksum
    plan = artifact_plan()
    plan["expect"] = [
        {"path": "report.md", "chars": expected_chars, "checksum": expected_checksum}
    ]
    seal_plan(tmp_path / "plan.json", plan)
    # Plan proof tolerates the same transport normalization as primary reads.
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(
        (
            prefix
            + plan_path.read_text(encoding="utf-8")
            + (suffix if state == "match" else "")
        ).encode("utf-8")
    )
    receipt = artifacts.assemble("plan.json")
    assert receipt["ok"] is True
    assert receipt["verified"] == [
        {
            "path": "report.md",
            "chars": chars,
            "expected_chars": expected_chars,
            "checksum": checksum,
            "expected_checksum": expected_checksum,
            "content_proof": state,
        }
    ]


@pytest.mark.parametrize(
    "spelling",
    [
        "ordinary",
        "surrogate",
        "unicode-keys",
    ],
    ids=[
        "JS-ordinary-backslashes",
        "JS-lone-surrogate",
        "JS-astral-cjk-integer-keys",
    ],
)
def test_real_node_serialization(spelling, tmp_path, monkeypatch):
    if shutil.which("node") is None:
        pytest.skip("node unavailable")
    monkeypatch.chdir(tmp_path)
    if spelling == "surrogate":
        source = '[{"id":"A","text":"lone \\ud800"}]'
    elif spelling == "unicode-keys":
        source = '[{"id":"A","text":"\u65e5\u672c\u8a9e \U0001f600 \U0001d54f","9":"nine","2":"two"}]'
    else:
        source = json.dumps([{"id": "A", "description": '\\"receipt\\" C:\\tmp\\out'}])
    (tmp_path / "findings.json").write_text(source, encoding="utf-8")
    plan = artifact_plan()
    plan["checkpoint"]["stripAliasFields"] = []
    proof_code = (
        "const text=process.argv[1];let h=0x811c9dc5;"
        "for(let i=0;i<text.length;i++)h=Math.imul(h^text.charCodeAt(i),0x1000193)>>>0;"
        "process.stdout.write(JSON.stringify({chars:text.length,"
        "checksum:'fnv1a32:0x'+h.toString(16).padStart(8,'0')}));"
    )
    proof_result = subprocess.run(
        ["node", "-e", proof_code, source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    primary_proof = json.loads(proof_result.stdout)
    plan["expect"] = [{"path": "findings.json", **primary_proof}]
    seal_plan(tmp_path / "plan.json", plan)
    receipt = artifacts.assemble("plan.json")
    assert receipt["ok"] is True
    assert receipt["verified"] == [
        {
            "path": "findings.json",
            "chars": primary_proof["chars"],
            "expected_chars": primary_proof["chars"],
            "checksum": primary_proof["checksum"],
            "expected_checksum": primary_proof["checksum"],
            "content_proof": "match",
        }
    ]
    code = "const source=JSON.parse(process.argv[1]);let skeleton=JSON.parse(process.argv[2]);skeleton.phases.challenge.findings=source;function h(s){let n=0x811c9dc5;for(let i=0;i<s.length;i++)n=Math.imul(n^s.charCodeAt(i),0x1000193)>>>0;return 'fnv1a32:0x'+n.toString(16).padStart(8,'0');}process.stdout.write(JSON.stringify([source,skeleton].map(v=>{const text=JSON.stringify(v,null,2);return {text,chars:text.length,checksum:h(text)};})));"
    result = subprocess.run(
        ["node", "-e", code, source, json.dumps(plan["checkpoint"]["skeleton"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    expected = json.loads(result.stdout)
    for index, path in enumerate(("post.json", "checkpoint.json")):
        assert (tmp_path / path).read_bytes() == expected[index]["text"].encode("utf-8")
        assert receipt["written"][index] == {
            "path": path,
            "chars": expected[index]["chars"],
            "checksum": expected[index]["checksum"],
        }
    if spelling == "ordinary":
        assert (
            json.loads((tmp_path / "post.json").read_text(encoding="utf-8"))[0][
                "description"
            ]
            == '\\"receipt\\" C:\\tmp\\out'
        )
    elif spelling == "surrogate":
        assert b"\\ud800" in (tmp_path / "post.json").read_bytes()
    else:
        assert list(
            json.loads((tmp_path / "post.json").read_text(encoding="utf-8"))[0]
        ) == ["2", "9", "id", "text"]


@pytest.mark.parametrize(
    "token,detail",
    [
        pytest.param(
            "0.9",
            "non-integer number at $[0].confidence (0.9): JS and Python spell such numbers differently, so the derived artifact would diverge",
            id="JS-refuse-fraction",
        ),
        pytest.param(
            "NaN",
            "non-integer number at $[0].confidence (nan): JS and Python spell such numbers differently, so the derived artifact would diverge",
            id="JS-refuse-nan",
        ),
        pytest.param(
            "9007199254740993",
            "integer at $[0].confidence is outside JS's safe integer range (9007199254740993)",
            id="JS-refuse-unsafe-integer",
        ),
    ],
)
def test_source_numeric_refusal(token, detail, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(
        '[{"id":"A","confidence":' + token + "}]", encoding="utf-8"
    )
    seal_plan(tmp_path / "plan.json", artifact_plan())
    receipt = artifacts.assemble("plan.json")
    assert receipt["errors"] == [
        f"could not serialize the post-review artifact: JsSerializationError: {detail}",
        f"could not serialize the checkpoint artifact: JsSerializationError: {detail.replace('$[0]', '$.phases.challenge.findings[0]')}",
    ]
    assert receipt["written"] == []
    assert not (tmp_path / "post.json").exists()


@pytest.mark.parametrize(
    "mode",
    [
        "serialize",
        "write-first",
        "write-second",
    ],
    ids=[
        "WRITE-both-serialized-before-write",
        "WRITE-first-fails-continues",
        "WRITE-second-fails-accounted",
    ],
)
def test_write_accounting(mode, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(SOURCE, encoding="utf-8")
    seal_plan(tmp_path / "plan.json", artifact_plan())
    for path in ("post.json", "checkpoint.json"):
        (tmp_path / path).write_text("old", encoding="utf-8")
    if mode == "serialize":
        real = artifacts.js_stringify_pretty

        def stringify(document):
            if document == {
                "phases": {
                    "challenge": {"before": 1, "findings": [{"id": "A"}], "after": 2}
                }
            }:
                raise ValueError("no checkpoint")
            return real(document)

        monkeypatch.setattr(artifacts, "js_stringify_pretty", stringify)
        monkeypatch.setattr(
            artifacts,
            "write_atomic",
            lambda *_: pytest.fail("wrote before both serialized"),
        )
    elif mode.startswith("write-"):
        failed_path = "post.json" if mode == "write-first" else "checkpoint.json"
        real_write = artifacts.write_atomic

        def write(path, text):
            if path == failed_path:
                raise OSError("denied")
            real_write(path, text)

        monkeypatch.setattr(artifacts, "write_atomic", write)
    receipt = artifacts.assemble("plan.json")
    if mode == "serialize":
        assert receipt["errors"] == [
            "could not serialize the checkpoint artifact: ValueError: no checkpoint"
        ]
        assert receipt["written"] == []
        assert (tmp_path / "post.json").read_text(encoding="utf-8") == "old"
        assert (tmp_path / "checkpoint.json").read_text(encoding="utf-8") == "old"
    else:
        assert receipt["ok"] is False
        assert receipt["errors"] == [f"could not write {failed_path} (OSError: denied)"]
        expected = [
            {"path": "post.json", "chars": 57, "checksum": "fnv1a32:0xda7cfb7d"},
            {"path": "checkpoint.json", "chars": 151, "checksum": "fnv1a32:0x428a53ce"},
        ]
        assert receipt["written"] == [
            entry for entry in expected if entry["path"] != failed_path
        ]
        for path, text in (("post.json", POST), ("checkpoint.json", CHECKPOINT)):
            assert (tmp_path / path).read_bytes() == (
                text if path != failed_path else "old"
            ).encode("utf-8")


def test_library_unexpected_failure(monkeypatch):
    def fail(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(artifacts, "_assemble", fail)
    assert artifacts.assemble("plan.json") == {
        "ok": False,
        "planVersion": None,
        "planChecksum": None,
        "verified": [],
        "written": [],
        "errors": ["assembler failed unexpectedly: RuntimeError: boom"],
    }


@pytest.mark.parametrize(
    "token",
    [
        "NaN",
    ],
    ids=[
        "FAULT-nan-receipt",
    ],
)
def test_strict_receipt_encoder(token, tmp_path, capsys):
    plan = tmp_path / "plan.json"
    plan.write_text('{"planVersion":' + token + "}", encoding="utf-8")
    assert artifacts.CLI.invoke(["--plan", str(plan)]) == 1
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    errors = receipt.pop("errors")
    assert len(errors) == 1
    assert errors[0].startswith(
        "receipt could not be serialized: ValueError: Out of range float values are not JSON compliant"
    )
    assert receipt == {
        "ok": False,
        "planVersion": None,
        "planChecksum": None,
        "verified": [],
        "written": [],
    }
    assert len(captured.out.splitlines()) == 1
    assert captured.err == ""


@pytest.mark.parametrize(
    "failure",
    [
        "first",
        "all",
    ],
    ids=[
        "FAULT-ascii-fallback",
        "FAULT-constant",
    ],
)
def test_encoding_fallback(failure, monkeypatch, capsys):
    original = cli.dumps

    def encode(receipt, **kwargs):
        if failure == "all" or not kwargs.get("ascii"):
            raise ValueError("caf\u00e9")
        return original(receipt, **kwargs)

    monkeypatch.setattr(cli, "dumps", encode)
    assert artifacts.CLI.invoke(["--plan", "missing"]) == 1
    expected = '{"ok": false, "planVersion": null, "planChecksum": null, "verified": [], "written": [], "errors": ["receipt could not be serializedSUFFIX"]}'
    assert capsys.readouterr().out.strip() == expected.replace(
        "SUFFIX", ": ValueError: caf\\u00e9" if failure == "first" else ""
    )


def test_receipt_unicode_and_surrogate_bytes(monkeypatch, capsys):
    def read(*args):
        raise OSError("caf\u00e9 \ud800")

    monkeypatch.setattr(artifacts, "_read_content", read)
    assert artifacts.CLI.invoke(["--plan", "plan.json"]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip().encode("utf-8") == (
        b'{"ok": false, "planVersion": null, "planChecksum": null, "verified": [], "written": [], '
        b'"errors": ["plan not found or unreadable: plan.json (caf\xc3\xa9 \\ud800)"]}'
    )
    assert captured.err == ""


def test_reordered_delivery_ids_change_the_plan_proof(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    plan = artifact_plan()
    plan["postReview"]["ids"] = ["A", "B"]
    sealed = seal_plan(tmp_path / "plan.json", plan)
    sealed["postReview"]["ids"] = ["B", "A"]
    (tmp_path / "plan.json").write_text(json.dumps(sealed), encoding="utf-8")
    assert artifacts.CLI.invoke(["--plan", "plan.json"]) == 1
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert receipt["ok"] is False
    assert receipt["planVersion"] == 2
    assert receipt["planChecksum"] == "fnv1a32:0x993be0ea"
    assert receipt["errors"] == [
        "plan checksum mismatch: declared fnv1a32:0x82e5bbf8, recomputed fnv1a32:0x993be0ea \u2014 "
        "the persist plan changed in transit; it is the instruction set for which findings reach "
        "the post-review artifact, so it is NOT executed"
    ]
    assert (
        captured.err
        == "plan checksum mismatch: declared fnv1a32:0x82e5bbf8, recomputed fnv1a32:0x993be0ea\n"
    )
    assert receipt["written"] == []
