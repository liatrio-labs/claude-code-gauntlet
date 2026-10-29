"""Persist-plan structure, proofs, projection bytes, and partial writes."""

import json

import pytest
from gauntlet import artifacts, cli, materialize

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
    "label,path",
    [("postReview", "../outside/derived.json"), ("checkpoint", "link/derived.json")],
)
def test_derived_paths_are_confined_before_any_write(
    label, path, tmp_path, monkeypatch, request, capsys
):
    directory, outside = tmp_path / "plan-directory", tmp_path / "outside"
    directory.mkdir()
    outside.mkdir()
    monkeypatch.chdir(directory)
    if path.startswith("link/"):
        request.getfixturevalue("symlink_or_skip")
        (directory / "link").symlink_to(outside, target_is_directory=True)
    (directory / "findings.json").write_text(SOURCE, encoding="utf-8")
    plan = artifact_plan()
    plan[label]["path"] = path
    seal_plan(directory / "plan.json", plan)
    assert artifacts.CLI.invoke(["--plan", "plan.json"]) == 1
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert receipt["errors"] == [
        f"{label} path is not inside the plan directory: {path}"
    ]
    assert receipt["written"] == []
    assert captured.err == ""
    for destination in (
        directory / "post.json",
        directory / "checkpoint.json",
        outside / "derived.json",
    ):
        assert not destination.exists()


@pytest.mark.parametrize(
    "section,updates,blocked,error",
    [
        (None, {"planVersion": 4}, None, "unsupported planVersion 4 (expected 3)"),
        (
            None,
            {"expect": [{"path": "report.md"}]},
            "report.md",
            "expected artifact not found or unreadable: report.md (denied)",
        ),
        (
            None,
            {"expect": [{"path": "expected.json"}]},
            None,
            "expected artifact is not valid JSON: expected.json (Extra data",
        ),
        (
            None,
            {},
            "findings.json",
            "source not found or unreadable: findings.json (denied)",
        ),
        (
            "postReview",
            {"ids": ["GHOST"]},
            None,
            "postReview id 'GHOST' not present in source findings.json",
        ),
        (
            "checkpoint",
            {"skeleton": {"phases": {"challenge": {"findings": None}}}},
            None,
            "checkpoint skeleton has no phases.challenge.findings array "
            "to receive 1 challenge finding(s)",
        ),
        pytest.param(
            None,
            {"source": '[{"id":"A"},{"id":"A"}]'},
            None,
            "duplicate id 'A' in source: findings.json",
            id="STRUCTURE-duplicate-id",
        ),
        pytest.param(
            None,
            {"source": SOURCE, "parser_error": "injected parser depth failure"},
            None,
            "source is not valid JSON: findings.json (injected parser depth failure)",
            id="STRUCTURE-source-recursion",
        ),
    ],
)
def test_structural_failure(section, updates, blocked, error, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = updates.get("source", SOURCE)
    parser_error = updates.get("parser_error")
    (tmp_path / "findings.json").write_text(source, encoding="utf-8")
    (tmp_path / "expected.json").write_text("[] trailing", encoding="utf-8")
    plan = artifact_plan()
    plan_updates = {
        key: value
        for key, value in updates.items()
        if key not in {"source", "parser_error"}
    }
    (plan[section] if section else plan).update(plan_updates)
    seal_plan(tmp_path / "plan.json", plan)
    if parser_error:
        original_loads = json.loads

        def loads_with_targeted_recursion_error(content, *args, **kwargs):
            if content == source:
                raise RecursionError(parser_error)
            return original_loads(content, *args, **kwargs)

        monkeypatch.setattr(json, "loads", loads_with_targeted_recursion_error)
    if blocked:
        real_read = artifacts._read_content

        def read(path):
            if path == blocked:
                raise OSError("denied")
            return real_read(path)

        monkeypatch.setattr(artifacts, "_read_content", read)
    (tmp_path / "post.json").write_text("old", encoding="utf-8")
    receipt = artifacts.assemble("plan.json")
    assert receipt["ok"] is False
    assert len(receipt["errors"]) == 1
    assert receipt["errors"][0].startswith(error)
    assert receipt["written"] == []
    assert (tmp_path / "post.json").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "checkpoint.json").exists()


def test_unproven_instructions_are_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = artifact_plan()
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    receipt = artifacts.assemble("plan.json")
    assert receipt == {
        "ok": False,
        "planVersion": 2,
        "planChecksum": None,
        "verified": [],
        "written": [],
        "errors": [
            "plan carries no planChecksum \u2014 an unproven "
            "instruction set is not executed: plan.json"
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
        == f"content-proof mismatch: report.md (expected {expected_chars} "
        f"chars / {expected_checksum}, got 1 / fnv1a32:0xfd0c5087)\n"
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
    assert captured.out.strip() == (
        '{"ok": true, "planVersion": 2, "planChecksum": "fnv1a32:0x9c74474d", '
        '"verified": [{"path": "findings.json", "chars": 32, "expected_chars": 32, '
        '"checksum": "fnv1a32:0x71b72159", "expected_checksum": "fnv1a32:0x71b72159", '
        '"content_proof": "match"}], "written": [{"path": "post.json", "chars": 57, '
        '"checksum": "fnv1a32:0xda7cfb7d"}, {"path": "checkpoint.json", "chars": 151, '
        '"checksum": "fnv1a32:0x428a53ce"}], "errors": []}'
    )
    assert (tmp_path / "post.json").read_bytes() == POST.encode("utf-8")
    assert (tmp_path / "checkpoint.json").read_bytes() == CHECKPOINT.encode("utf-8")
    assert len(captured.out.splitlines()) == 1
    assert captured.err == ""


def test_projection_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(
        '[{"id":"A","line":4,"body":"b"},{"id":"B","line":5,"body":"c"}]',
        encoding="utf-8",
    )
    ids = ["B", "A", "B"]
    plan = artifact_plan(ids=ids)
    plan["postReview"]["wrapper"] = {"owner": "o", "findings": ["old"], "tail": 7}
    seal_plan(tmp_path / "plan.json", plan)
    assert artifacts.assemble("plan.json")["ok"] is True
    post = json.loads((tmp_path / "post.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert post == {
        "owner": "o",
        "findings": [
            {"id": fid, "line": line, "body": body}
            for fid, line, body in [("B", 5, "c"), ("A", 4, "b"), ("B", 5, "c")]
        ],
        "tail": 7,
    }
    assert list(post) == ["owner", "findings", "tail"]
    challenge = checkpoint["phases"]["challenge"]
    assert list(challenge) == ["before", "findings", "after"]
    assert challenge["findings"] == [{"id": fid} for fid in ids]


@pytest.mark.parametrize(
    "raw,chars,checksum,state",
    [
        (b"\xef\xbb\xbfx\r\ny\r\n", 4, "fnv1a32:0xd3cef7bf", "match"),
        (b"x\n\n", 2, "fnv1a32:0xe762cdf7", "mismatch"),
    ],
)
def test_content_normalization(raw, chars, checksum, state, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "findings.json").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "report.md").write_bytes(raw)
    plan = artifact_plan()
    plan["expect"] = [
        {"path": "report.md", "chars": 4, "checksum": "fnv1a32:0xd3cef7bf"}
    ]
    seal_plan(tmp_path / "plan.json", plan)
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(b"\xef\xbb\xbf" + plan_path.read_bytes() + b"\r\n")
    receipt = artifacts.assemble("plan.json")
    assert receipt["ok"] is True
    assert receipt["verified"] == [
        {
            "path": "report.md",
            "chars": chars,
            "expected_chars": 4,
            "checksum": checksum,
            "expected_checksum": "fnv1a32:0xd3cef7bf",
            "content_proof": state,
        }
    ]


@pytest.mark.parametrize(
    "mode",
    ["serialize", "write-first"],
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
    else:
        failed_path = "post.json"
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


@pytest.mark.parametrize("command", ["assemble", "materialize"])
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
def test_encoding_fallback(command, failure, monkeypatch, capsys):
    original = cli.dumps

    def encode(receipt, **kwargs):
        if failure == "all" or not kwargs.get("ascii"):
            raise ValueError("caf\u00e9")
        return original(receipt, **kwargs)

    monkeypatch.setattr(cli, "dumps", encode)
    if command == "assemble":
        assert artifacts.CLI.invoke(["--plan", "missing"]) == 1
        expected = (
            '{"ok": false, "planVersion": null, "planChecksum": null, '
            '"verified": [], "written": [], "errors": ["receipt could not '
            'be serializedSUFFIX"]}'
        )
    else:
        assert (
            materialize.CLI.invoke(["--output-dir", ".", "--task", "missing.output"])
            == 1
        )
        expected = (
            '{"ok": false, "channel": "return", "source": null, "scanned": 0, '
            '"materialized": [], "assemble": null, "gaps": [], '
            '"errors": ["receipt could not be serializedSUFFIX"]}'
        )
    assert capsys.readouterr().out.strip() == expected.replace(
        "SUFFIX", ": ValueError: caf\\u00e9" if failure == "first" else ""
    )


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
        "plan checksum mismatch: declared fnv1a32:0x82e5bbf8, "
        "recomputed fnv1a32:0x993be0ea \u2014 "
        "the persist plan changed in transit; it is the instruction "
        "set for which findings reach "
        "the post-review artifact, so it is NOT executed"
    ]
    assert (
        captured.err == "plan checksum mismatch: declared fnv1a32:0x82e5bbf8, "
        "recomputed fnv1a32:0x993be0ea\n"
    )
    assert receipt["written"] == []
