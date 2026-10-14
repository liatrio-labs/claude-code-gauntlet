"""Returned artifact selection, validation, verbatim writes, and receipts."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from gauntlet import artifacts, materialize, tasks

from tests.support.artifacts import (
    NONCE,
    artifact_plan,
    record_task_output,
    returned_task,
    seal_plan,
    task_file,
)

EMPTY_ASSEMBLE = {
    "ok": True,
    "planVersion": 2,
    "planChecksum": "proof",
    "verified": [],
    "written": [],
    "errors": [],
}


@pytest.mark.parametrize(
    "mode,task,nonce,scanned,source",
    [
        pytest.param("named", "task", None, 1, "named", id="SOURCE-task-only"),
        pytest.param(
            "sweep",
            None,
            "run",
            2,
            "matching",
            id="SOURCE-nonce-newest-foreign-skipped",
        ),
        pytest.param("foreign", "task", "run", 1, None, id="SOURCE-foreign-nonce"),
        pytest.param(
            "missing-id", "missing", None, 0, None, id="SOURCE-unresolved-id-zero"
        ),
        pytest.param(
            "cli-path-nonce",
            "missing.output",
            "run",
            2,
            "named",
            id="SOURCE-cli-missing-path-nonce-sweeps-roots",
        ),
        pytest.param("channel", "task", None, 1, None, id="SOURCE-foreign-channel"),
    ],
)
def test_source_selection(
    mode, task, nonce, scanned, source, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "out"
    output.mkdir()
    plan = output / "plan.json"
    payload = {
        "channel": "return",
        "nonce": "run",
        "planPath": str(plan),
        "entries": [{"path": str(plan), "text": "{}"}],
    }
    root = tmp_path / "root[g]"
    named = task_file(root, "task")
    if mode == "foreign":
        payload["nonce"] = "other"
    if mode == "channel":
        payload["channel"] = "other"
    returned_task(named, payload)
    os.utime(named, (10, 10))
    matching = None
    if mode == "sweep":
        matching = task_file(root, "match", mtime=1)
        returned_task(matching, payload)
        os.utime(matching, (1, 1))
        foreign = task_file(root, "foreign", mtime=20)
        returned_task(foreign, dict(payload, nonce="other"))
        os.utime(foreign, (20, 20))
        # Remove the otherwise matching named file from this content-only sweep.
        named.unlink()
    monkeypatch.setattr(materialize, "assemble", lambda _: EMPTY_ASSEMBLE)
    roots = tasks.TaskRoots((str(root),), None)
    if mode == "sweep":
        monkeypatch.setenv("CODE_GAUNTLET_TASKS_DIR", str(matching.parent))
        monkeypatch.setenv("CODE_GAUNTLET_TASK_ROOTS", str(tmp_path / "empty-root"))
        assert (
            materialize.CLI.invoke(["--output-dir", str(output), "--nonce", nonce]) == 0
        )
        receipt = json.loads(capsys.readouterr().out)
    elif mode == "cli-path-nonce":
        monkeypatch.setenv("CODE_GAUNTLET_TASK_ROOTS", str(root))
        assert (
            materialize.CLI.invoke(
                ["--output-dir", str(output), "--task", task, "--nonce", nonce]
            )
            == 0
        )
        receipt = json.loads(capsys.readouterr().out)
    else:
        receipt = materialize.materialize(task, nonce, str(output), roots)
    assert receipt["scanned"] == scanned
    assert receipt["source"] == (
        str(named if source == "named" else matching) if source else None
    )
    assert receipt["ok"] == bool(source)
    assert receipt["gaps"] == []
    if source:
        assert receipt["errors"] == []
        assert plan.read_text(encoding="utf-8") == "{}"
    else:
        assert receipt["materialized"] == []
        assert not plan.exists()
        assert receipt["errors"] == [
            "no task output file carrying this run's returned artifacts was found "
            f"(looked at {scanned} candidate file(s) for target "
            f"{task!r} / nonce {nonce!r})"
        ]


@pytest.mark.parametrize(
    "defect,message",
    [
        pytest.param(
            "nonobject", "entry 2 is not an object", id="FENCE-later-nonobject"
        ),
        pytest.param(
            "path",
            "entry 1 has no usable string path",
            id="FENCE-missing-path",
        ),
        pytest.param(
            "escape",
            "entry 1 writes outside the output directory: BAD is not inside ROOT",
            id="FENCE-lexical-escape",
        ),
        pytest.param(
            "not-entry",
            "the named persist plan BAD is not among the entries this payload carries",
            id="FENCE-plan-not-entry",
        ),
    ],
)
def test_validation_precedes_every_write(defect, message, tmp_path, monkeypatch):
    root = tmp_path / "out"
    root.mkdir()
    primary = root / "primary"
    plan = root / "plan.json"
    bad = root / "bad.json"
    payload = {
        "channel": "return",
        "planPath": str(plan),
        "entries": [
            {"path": str(primary), "text": "x"},
            {"path": str(plan), "text": "{}"},
        ],
    }
    if defect == "nonobject":
        payload["entries"].append(3)
    elif defect == "escape":
        bad = root / ".." / "escape"
        payload["entries"][1]["path"] = str(bad)
    elif defect == "path":
        payload["entries"][1]["path"] = ""
    else:
        payload["planPath"] = str(bad)
    task = returned_task(tmp_path / "task.output", payload)
    monkeypatch.setattr(
        artifacts,
        "write_atomic",
        lambda *_: pytest.fail("validation wrote a primary"),
    )
    receipt = materialize.materialize(task, None, str(root), tasks.TaskRoots((), None))
    assert receipt["ok"] is False
    assert receipt["errors"] == [
        message.replace("BAD", str(bad)).replace("ROOT", os.path.realpath(root))
    ]
    assert receipt["materialized"] == []
    assert receipt["assemble"] is None
    assert list(root.iterdir()) == []


def test_symlink_escape_is_refused_before_write(tmp_path, symlink_or_skip):
    root, outside = tmp_path / "out", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    escaped = root / "link" / "plan.json"
    primary = root / "primary"
    task = returned_task(
        tmp_path / "task.output",
        {
            "channel": "return",
            "planPath": str(escaped),
            "entries": [
                {"path": str(primary), "text": "x"},
                {"path": str(escaped), "text": "{}"},
            ],
        },
    )
    receipt = materialize.materialize(task, None, str(root), tasks.TaskRoots((), None))
    assert receipt["errors"] == [
        f"entry 1 writes outside the output directory: {escaped} is "
        f"not inside {os.path.realpath(root)}"
    ]
    assert receipt["materialized"] == []
    assert not primary.exists()
    assert list(outside.iterdir()) == []


def test_verbatim_writes_utf16_and_idempotence(tmp_path, monkeypatch):
    primary, plan = tmp_path / "primary", tmp_path / "plan.json"
    text = '\\"receipt\\" \U0001f600\r\n\n'
    task = returned_task(
        tmp_path / "task.output",
        {
            "channel": "return",
            "planPath": str(plan),
            "entries": [
                {"path": str(primary), "text": text},
                {"path": str(plan), "text": "{}"},
            ],
        },
    )
    monkeypatch.setattr(materialize, "assemble", lambda _: EMPTY_ASSEMBLE)
    receipt = materialize.materialize(
        task, None, str(tmp_path), tasks.TaskRoots((), None)
    )
    assert receipt["ok"] is True
    assert receipt["materialized"][0] == {
        "path": str(primary),
        "chars": 17,
        "checksum": "fnv1a32:0x759a4f8b",
    }
    assert primary.read_bytes() == b'\\"receipt\\" \xf0\x9f\x98\x80\r\n\n'
    again = materialize.materialize(
        task, None, str(tmp_path), tasks.TaskRoots((), None)
    )
    assert again == receipt
    assert primary.read_bytes() == b'\\"receipt\\" \xf0\x9f\x98\x80\r\n\n'


def test_partial_primary_writes_skip_derivation(tmp_path, monkeypatch):
    parent = "missing"
    primary = tmp_path / parent / "primary"
    plan = tmp_path / "plan.json"
    task = returned_task(
        tmp_path / "task.output",
        {
            "channel": "return",
            "planPath": str(plan),
            "entries": [
                {"path": str(primary), "text": "x"},
                {"path": str(plan), "text": "{}"},
            ],
        },
    )
    monkeypatch.setattr(
        materialize, "assemble", lambda *_: pytest.fail("derived after partial write")
    )
    receipt = materialize.materialize(
        task, None, str(tmp_path), tasks.TaskRoots((), None)
    )
    assert receipt["ok"] is False
    assert [e["path"] for e in receipt["materialized"]] == [str(plan)]
    assert receipt["assemble"] is None
    assert len(receipt["errors"]) == 1
    assert receipt["errors"][0].startswith(
        f"could not write {primary} (FileNotFoundError:"
    )
    assert not primary.parent.exists()
    assert plan.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize(
    "mode,wrote,want,gap,errors",
    [
        (
            "primary",
            None,
            None,
            "artifact-content-proof: primary on disk differs from the bytes the "
            "workflow returned (expected 1 chars/want, got 2/got)",
            [],
        ),
        (
            "derived",
            (2, "same"),
            (1, "same"),
            "artifact-content-proof: derived document derived does not match the "
            "pipeline's own derivation (wrote 2 chars/same, the "
            "pipeline derived 1/same)",
            [],
        ),
        (
            "derived",
            (1, "got"),
            (1, "want"),
            "artifact-content-proof: derived document derived does not match the "
            "pipeline's own derivation (wrote 1 chars/got, the "
            "pipeline derived 1/want)",
            [],
        ),
        (
            "derived",
            (1, "got"),
            None,
            "artifact-content-proof: the persist plan carries no derived-content "
            "expectation for derived (no content proof)",
            [],
        ),
        (
            "refused",
            None,
            None,
            "artifact-content-proof: primary on disk differs from the bytes the "
            "workflow returned (expected 1 chars/want, got 2/got)",
            ["refused"],
        ),
        ("duplicate", (1, "same"), (1, "same"), None, []),
    ],
)
def test_proof_disclosure(mode, wrote, want, gap, errors, tmp_path, monkeypatch):
    reply = dict(EMPTY_ASSEMBLE, ok=not errors, errors=errors)
    if mode in ("primary", "refused"):
        reply["verified"] = [
            {
                "path": "primary",
                "content_proof": "mismatch",
                "expected_chars": 1,
                "expected_checksum": "want",
                "chars": 2,
                "checksum": "got",
            }
        ]
    if wrote:
        reply["written"] = [
            {"path": "derived", "chars": wrote[0], "checksum": wrote[1]}
        ]
    plan_text = (
        json.dumps(
            {"derive": [{"path": "derived", "chars": want[0], "checksum": want[1]}]}
        )
        if want
        else "{}"
    )
    plan = tmp_path / "plan.json"
    entries = [{"path": str(plan), "text": plan_text}]
    if mode == "duplicate":
        entries.insert(0, {"path": str(plan), "text": "broken"})
    task = returned_task(
        tmp_path / "task.output",
        {
            "channel": "return",
            "planPath": str(plan),
            "entries": entries,
        },
    )
    monkeypatch.setattr(materialize, "assemble", lambda _: reply)
    receipt = materialize.materialize(
        task, None, str(tmp_path), tasks.TaskRoots((), None)
    )
    assert receipt["ok"] == (mode == "duplicate")
    assert receipt["gaps"] == ([gap] if gap else [])
    assert receipt["errors"] == errors
    assert receipt["assemble"] == reply
    assert plan.read_text(encoding="utf-8") == plan_text
    assert len(receipt["materialized"]) == (2 if mode == "duplicate" else 1)


@pytest.mark.skipif(
    sys.platform == "win32", reason="pipeline outputDir requires a POSIX path"
)
def test_real_node_return_channel(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node unavailable")
    task, output = record_task_output(str(tmp_path))
    payload = json.loads(Path(task).read_text(encoding="utf-8"))["result"][
        "persistReturn"
    ]
    receipt = materialize.materialize(task, NONCE, output, tasks.TaskRoots((), None))
    assert receipt["ok"] is True
    assert receipt["gaps"] == []
    assert receipt["errors"] == []
    for entry in payload["entries"]:
        with open(entry["path"], encoding="utf-8", newline="") as handle:
            assert handle.read() == entry["text"]
    findings = json.loads(
        Path(output, "code-gauntlet-findings-abc1234.json").read_text(encoding="utf-8")
    )
    description = findings[0]["description"]
    assert '\\"receipt\\"' in description
    assert "C:\\tmp\\out" in description
    assert "\U0001f600" in description
    assert "\U0001d54f" in description
    assert len(description) > 1000
    assert findings[0]["body"] == description
    assert [e["content_proof"] for e in receipt["assemble"]["verified"]] == [
        "match",
        "match",
    ]
    for name in ("post-review", "checkpoint-all"):
        document = json.loads(
            Path(output, f"code-gauntlet-{name}-abc1234.json").read_text(
                encoding="utf-8"
            )
        )
        projected = (
            document
            if name == "post-review"
            else document["phases"]["challenge"]["findings"]
        )
        plan = json.loads(payload["entries"][2]["text"])
        ids = (
            plan["postReview"]["ids"]
            if name == "post-review"
            else plan["checkpoint"]["challengeFindingIds"]
        )
        assert [f["id"] for f in projected] == ids


def test_unexpected_failure(tmp_path, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("injected")

    monkeypatch.setattr(materialize, "select_source", fail)
    assert materialize.materialize(
        "missing.output", None, str(tmp_path), tasks.TaskRoots((), None)
    ) == {
        "ok": False,
        "channel": "return",
        "source": None,
        "scanned": 0,
        "materialized": [],
        "assemble": None,
        "gaps": [],
        "errors": ["materializer failed unexpectedly: RuntimeError: injected"],
    }
    assert (
        materialize.CLI.invoke(
            ["--output-dir", str(tmp_path), "--task", "missing.output"]
        )
        == 1
    )
    assert (
        capsys.readouterr().out.strip()
        == '{"ok": false, "channel": "return", "source": null, "scanned": '
        '0, "materialized": [], "assemble": null, "gaps": [], '
        '"errors": ["materializer failed unexpectedly: RuntimeError: '
        'injected"]}'
    )


def test_command_discovery_failure(monkeypatch, capsys):
    target_flags = ["--nonce", "run"]

    def fail(*args):
        raise OSError("discovery failed")

    monkeypatch.setattr(tasks, "roots_from_environment", fail)
    assert materialize.CLI.invoke(["--output-dir", ".", *target_flags]) == 1
    assert (
        capsys.readouterr().out.strip()
        == '{"ok": false, "channel": "return", "source": null, "scanned": '
        '0, "materialized": [], "assemble": null, "gaps": [], '
        '"errors": ["materializer failed unexpectedly: OSError: '
        'discovery failed"]}'
    )


def test_explicit_path_bypasses_discovery_failure(monkeypatch, capsys):
    def fail(*args):
        raise OSError("discovery failed")

    monkeypatch.setattr(tasks, "roots_from_environment", fail)
    assert (
        materialize.CLI.invoke(["--output-dir", ".", "--task", "missing.output"]) == 1
    )
    assert json.loads(capsys.readouterr().out)["errors"] == [
        "no task output file carrying this run's returned artifacts "
        "was found (looked at 1 candidate file(s) for target "
        "'missing.output' / nonce None)"
    ]


def test_native_return_fixture_derives_with_literal_proofs(tmp_path, capsys):
    output = tmp_path / "native-caf\u00e9"
    output.mkdir()
    source = output / "findings.json"
    plan_path = output / "plan.json"
    plan = artifact_plan(source=str(source))
    plan["postReview"]["path"] = str(output / "post.json")
    plan["checkpoint"]["path"] = str(output / "checkpoint.json")
    plan["expect"] = [
        {"path": str(source), "chars": 32, "checksum": "fnv1a32:0x71b72159"}
    ]
    plan["derive"] = [
        {
            "path": str(output / "post.json"),
            "chars": 57,
            "checksum": "fnv1a32:0xda7cfb7d",
        },
        {
            "path": str(output / "checkpoint.json"),
            "chars": 151,
            "checksum": "fnv1a32:0x428a53ce",
        },
    ]
    sealed = seal_plan(plan_path, plan)
    plan_path.unlink()
    task = returned_task(
        tmp_path / "task.output",
        {
            "channel": "return",
            "planPath": str(plan_path),
            "entries": [
                {"path": str(source), "text": '[{"id":"A","line":4,"body":"b"}]'},
                {"path": str(plan_path), "text": json.dumps(sealed)},
            ],
        },
    )
    assert materialize.CLI.invoke(["--output-dir", str(output), "--task", task]) == 0
    captured = capsys.readouterr()
    assert "native-caf\u00e9" in captured.out
    assert "caf\\u00e9" not in captured.out
    receipt = json.loads(captured.out)
    assert receipt["ok"] is True
    assert receipt["gaps"] == []
    assert receipt["errors"] == []
    assert receipt["materialized"][0] == {
        "path": str(source),
        "chars": 32,
        "checksum": "fnv1a32:0x71b72159",
    }
    assert receipt["assemble"]["written"] == plan["derive"]
    assert source.read_bytes() == b'[{"id":"A","line":4,"body":"b"}]'
    assert json.loads((output / "post.json").read_text(encoding="utf-8")) == [
        {"id": "A", "line": 4, "body": "b"}
    ]
    assert captured.err == ""
