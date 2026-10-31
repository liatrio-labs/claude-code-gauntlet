"""Wire proofs at the inline and receipt boundaries."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet import fs, jsjson
from gauntlet.registry import CITE_VERIFIER_VERSION
from gauntlet.verify import wire as verify

from tests.support.citations import citation_cli


def test_cite_mode_receipts_bind_requests_and_source(tmp_path, monkeypatch, invoke):
    call, q, capture, root, output = citation_cli(tmp_path, monkeypatch, invoke)
    requests = [
        q,
        {**q, "request_id": "other", "finding_id": "foreign"},
        {**q, "request_id": "repeat"},
        {**q, "request_id": "repeat"},
    ]
    env, data, token = call(requests, real=True)
    r = env["receipt"]
    assert r == {
        "sha": capture["head"][:8],
        "n_in": 4,
        "nonce": "run.12345678.1",
        "base_branch": "main",
        "diff_source": {"path": str(output / "diff.patch"), "read": True},
        "batch_id": "12345678",
        "attempt": 1,
        "inline_checksum": jsjson.fnv1a32(token),
        "input_checksum": jsjson.checksum_or_none(data),
        "deltas_checksum": jsjson.checksum_or_none(env["result"]["citations"]),
        "record_proofs": [
            {"id": row["id"], "checksum": jsjson.checksum_or_none(row)}
            for row in data["requests"]
        ],
    }
    rows = env["result"]["citations"]
    assert [row["status"] for row in rows] == [
        "matched",
        "rejected",
        "rejected",
        "rejected",
    ]
    assert rows[0]["request"] == q
    assert rows[0]["source"] == {
        "snapshot_id": capture["manifest_sha256"],
        "target_kind": "branch",
        "head": capture["head"],
        "base": capture["base"],
        "diff_sha256": capture["diff_sha256"],
        "verifier_version": CITE_VERIFIER_VERSION,
    }
    changed, changed_data, changed_token = call(
        [{**q, "quote": "return another_value;"}], batch="87654321", real=True
    )
    assert changed["receipt"]["input_checksum"] != r["input_checksum"]
    assert changed["receipt"]["record_proofs"][0] != r["record_proofs"][0]
    assert changed["result"]["citations"][0]["status"] == "unmatched"
    for index, extra in enumerate(
        [
            ["--target-kind", "local"],
            ["--head-sha", "ffff"],
            ["--source-capture", json.dumps({**capture, "base": "a" * 40})],
        ]
    ):
        bad, _, _ = call([q], batch=f"aaaaaaa{index}", extra=extra, real=True)
        assert bad["result"]["citations"][0]["status"] == "unavailable"
    project = Path(__file__).resolve().parents[1]
    packet = {
        "nonce": "run",
        "headShaShort": capture["head"][:8],
        "repoRoot": root.as_posix(),
        "pluginRoot": project.as_posix(),
        "outputDir": output.as_posix(),
        "python": "python",
        "verify": {
            "sourceCapture": capture,
            "targetKind": "branch",
            "baseBranch": "main",
            "diffPath": (output / "diff.patch").as_posix(),
            "inputPathBase": (output / "in").as_posix(),
            "outputPathBase": (output / "out").as_posix(),
        },
        "findings": [{"id": "F"}],
        "citation_request": q,
    }
    program = (
        f"import {{citeStage}} from {json.dumps((project / 'workflows/src/citeStage.js').as_uri())};"
        f"import {{skepticalAssessment}} from {json.dumps((project / 'workflows/test/helpers/verdicts.js').as_uri())};"
        f"import {{shellSplit}} from {json.dumps((project / 'workflows/test/helpers/shellWords.js').as_uri())};"
        "import {execFileSync} from 'node:child_process'; import fs from 'node:fs';"
        "let s=''; for await (const c of process.stdin) s+=c; const inp=JSON.parse(s); let calls=0;"
        "inp.findings[0].skeptical_assessments=[skepticalAssessment('F','validator',{citations:[inp.citation_request]})];"
        "const ctx={agent:async (prompt,opts)=>{calls++; if(calls===1)throw new Error('dispatch never launched');"
        "const [exe,...argv]=shellSplit(prompt.split('\\n').at(-1)); execFileSync(exe,argv,{cwd:inp.repoRoot});"
        "return JSON.parse(fs.readFileSync(inp.verify.outputPathBase+'.cite'+opts.label.split('-')[1]+'.json','utf8'));}};"
        "const first=await citeStage(ctx,inp); const resumed=await citeStage(ctx,JSON.parse(JSON.stringify(first)));"
        "process.stdout.write(JSON.stringify({calls,resumed}));"
    )
    integrated = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", program],
            input=json.dumps(packet),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        ).stdout
    )
    assert integrated["calls"] == 2
    assert (
        integrated["resumed"]["findings"][0]["cite_results"][0]["status"] == "matched"
    )


VECTORS = Path(__file__).parent / "fixtures/parity/slice_inline"
EMPTY = '{"findings":[],"base_branch":"main"}'
EMPTY_BYTES = b"""{
  "status": "ok",
  "receipt": {
    "sha": "abcd",
    "n_in": 0,
    "nonce": "cli",
    "deltas_checksum": "fnv1a32:0x741638a5",
    "inline_checksum": "fnv1a32:0x8c59538c",
    "base_branch": "main",
    "diff_source": {
      "path": null,
      "read": true
    },
    "record_proofs": [],
    "input_checksum": "fnv1a32:0x2eb50aa0"
  },
  "result": {
    "deltas": [],
    "verified": [],
    "eliminated": [],
    "stats": {
      "total": 0,
      "new": 0,
      "surfaced": 0,
      "eliminated": 0
    }
  }
}"""


def receipt(invoke, tmp_path, token=EMPTY, extra=()):
    return invoke(
        "verify_findings",
        [
            "--target-kind",
            "branch",
            "--input",
            str(tmp_path / "slice.json"),
            "--input-inline",
            token,
            "--head-sha",
            "abcd",
            "--nonce",
            "cli",
            *extra,
        ],
        tmp_path,
    )


def test_target_kind_is_required(invoke, tmp_path):
    with pytest.raises(TypeError, match="target_kind"):
        verify.decide.VerifyContext("repo", "base")
    result = invoke(
        "verify_findings",
        ["--input", "slice.json", "--input-inline", EMPTY],
        tmp_path,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        b"",
        b"verify_findings: the following arguments are required: --target-kind\n",
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("astral", id="INLINE-astral"),
        pytest.param("control_chars", id="INLINE-control_chars"),
        pytest.param("empty_findings", id="INLINE-empty_findings"),
        pytest.param("lone_surrogates", id="INLINE-lone_surrogates"),
        pytest.param("nested_cross_file_refs", id="INLINE-nested_cross_file_refs"),
        pytest.param("non_ascii_keys", id="INLINE-non_ascii_keys"),
        pytest.param("percent_forms", id="INLINE-percent_forms"),
        pytest.param("safe_punctuation", id="INLINE-safe_punctuation"),
        pytest.param("surrogate_pair", id="INLINE-surrogate_pair"),
        pytest.param("three_findings", id="INLINE-three_findings"),
    ],
)
def test_inline(case):
    doc = json.loads((VECTORS / case / "input.json").read_text(encoding="utf-8"))["doc"]
    golden = json.loads((VECTORS / case / "expected.json").read_text(encoding="utf-8"))
    decoded = verify.decode_inline_slice(golden["encoded"])
    assert decoded == doc
    assert jsjson.checksum_or_none(decoded) == golden["checksum"]
    assert jsjson.fnv1a32(golden["encoded"]) == golden["token_checksum"]


@pytest.mark.parametrize(
    ("token", "message"),
    [
        pytest.param(
            '{"findings":[{"evidence":"`"}]}',
            "raw U+0060 at $.findings[0].evidence",
            id="REJECT-raw-nested",
        ),
        pytest.param(
            '{"findings":[{"evidence":"%4"}]}',
            "invalid percent escape at $.findings[0].evidence",
            id="REJECT-truncated",
        ),
        pytest.param(
            '{"findings":[],"s":"\\u0041"}',
            "JSON escape sequences are not canonical at $ (offending U+005C)",
            id="REJECT-json-unicode",
        ),
        pytest.param(
            '{"findings":[{"evidence":"%41"}]}',
            "non-canonical percent escape %41 at $.findings[0].evidence (byte is SAFE "
            "ASCII)",
            id="REJECT-encoded-safe-value",
        ),
        pytest.param(
            '{"findings":[{"evidence":"%FF"}]}',
            "invalid UTF-8 at $.findings[0].evidence "
            "(offending bytes FF, 'utf-8' codec "
            "can't decode byte 0xff in position 0: invalid start byte)",
            id="REJECT-invalid-utf8",
        ),
        pytest.param(
            '{"findings":[{"evidence":"%u0041"}]}',
            "invalid %u escape U+0041 at $.findings[0].evidence (only surrogates are "
            "allowed)",
            id="REJECT-non-surrogate-u",
        ),
        pytest.param(
            '{"findings":[{"evidence":"%uD83D%uDE00"}]}',
            "non-canonical surrogate pair %uD83D%uDE00 at $.findings[0].evidence (an "
            "astral character is spelled as its UTF-8 bytes)",
            id="REJECT-adjacent-pair-at-end",
        ),
        pytest.param(
            '{"findings":[],"findings":[]}',
            "invalid JSON at $ (duplicate object key 'findings')",
            id="REJECT-duplicate-key",
        ),
        pytest.param(
            '{"findings":[],"n":NaN}',
            "invalid JSON at $ (non-finite JSON constant NaN)",
            id="REJECT-nonfinite",
        ),
        pytest.param(
            "{}", "missing required 'findings' array at $", id="REJECT-missing-findings"
        ),
        pytest.param(
            '{"findings":{}}', "'findings' must be an array at $", id="REJECT-nonarray"
        ),
    ],
)
def test_reject(token, message, invoke, tmp_path, verify_git):
    expected = "inline slice-input rejected: " + message
    result = receipt(invoke, tmp_path, token)
    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "status": "failed",
        "exitCode": 1,
        "stderr": expected,
    }
    assert not (tmp_path / "slice.json").exists()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            {
                "line_start": "10",
                "line_end": "11",
                "line": "12",
                "end_line": "13",
                "confidence": "80",
                "unknown": "9",
            },
            {
                "line_start": 10,
                "line_end": 11,
                "line": 12,
                "end_line": 13,
                "confidence": 80,
                "unknown": "9",
            },
            id="COERCE-five-fields",
        ),
        pytest.param(
            {
                "confidence": 64.5,
                "line_end": -64.5,
                "line_start": 1.49,
                "line": -1.51,
            },
            {"confidence": 65, "line_end": -64, "line_start": 1, "line": -2},
            id="COERCE-half-up",
        ),
    ],
)
def test_coerce(before, after):
    finding = copy.deepcopy(before)
    assert verify.coerce_numeric_fields(finding) is finding
    assert finding == after


def test_delta_half_up():
    findings = [
        {"id": "a", "confidence": 64.5, "location_status": "unconfirmed"},
        {"id": "b", "confidence": -64.5, "location_status": "unconfirmed"},
    ]
    assert [
        delta["confidence"] for delta in verify.build_deltas(findings, findings)
    ] == [65, -64]


@pytest.mark.parametrize(
    "destination",
    [
        pytest.param("stdout", id="RECEIPT-stdout-exact-bytes"),
        pytest.param("file", id="RECEIPT-file-no-final-LF"),
    ],
)
def test_receipt_bytes(destination, invoke, tmp_path, verify_git):
    extra = ["--output", str(tmp_path / "out.json")] if destination == "file" else []
    result = receipt(invoke, tmp_path, extra=extra)
    assert result.returncode == 0
    if destination == "file":
        assert result.stdout == b""
        assert (tmp_path / "out.json").read_bytes() == EMPTY_BYTES
    else:
        assert result.stdout == EMPTY_BYTES + b"\n"
    assert (
        tmp_path / "slice.json"
    ).read_bytes() == b'{\n  "findings": [],\n  "base_branch": "main"\n}'
    assert result.stderr == b"Diff source: git diff main...HEAD (three-dot), 0 bytes\n"


@pytest.mark.parametrize(
    "source", ["empty", "content", "missing", "directory", "git", "failed-git"]
)
def test_receipt_diff_source(source, invoke, tmp_path, verify_git):
    path = tmp_path / "patch"
    if source in ("empty", "content"):
        path.write_text("" if source == "empty" else "diff content\n", encoding="utf-8")
    elif source == "directory":
        path.mkdir()
    if source == "failed-git":
        verify_git[1]["diff"] = ("", "diff unavailable", 1)
    from_git = source in ("git", "failed-git")
    flags = [] if from_git else ["--diff-file", str(path)]
    result = receipt(invoke, tmp_path, extra=flags)
    assert result.returncode == 0
    assert json.loads(result.stdout)["receipt"]["diff_source"] == {
        "path": None if from_git else str(path),
        "read": source in ("empty", "content", "git"),
    }
    if not from_git:
        assert all(cmd[1] != "diff" for cmd, _ in verify_git[0])


def test_receipt_pre_coercion(invoke, tmp_path, verify_git):
    (tmp_path / "source").write_text("code\n", encoding="utf-8")
    token = (
        '{"findings":[{"id":"a","file":"source","line_start":"1","confidence":"80"}]}'
    )
    result = receipt(invoke, tmp_path, token)
    assert result.returncode == 0
    env = json.loads(result.stdout)
    assert env["receipt"].pop("deltas_checksum") == verify.checksum_or_none(
        env["result"]["deltas"]
    )
    assert env["receipt"] == {
        "sha": "abcd",
        "n_in": 1,
        "nonce": "cli",
        "inline_checksum": "fnv1a32:0x17c759ec",
        "base_branch": "main",
        "diff_source": {"path": None, "read": True},
        "record_proofs": [{"id": "a", "checksum": "fnv1a32:0x2fc424e8"}],
        "input_checksum": "fnv1a32:0x6754b4b8",
    }
    assert (tmp_path / "slice.json").read_bytes() == (
        b'{\n  "findings": [\n    {\n      "id": "a",\n     '
        b' "file": "source",\n      "line_start": "1",\n  '
        b'    "confidence": "80"\n    }\n  ]\n}'
    )
    assert env["result"]["verified"] == [
        {
            "id": "a",
            "file": "source",
            "claimed_line_start": 1,
            "location_status": "unconfirmed",
            "confidence": 80,
            "path_role": "source",
            "file_added": False,
            "blame_class": "unknown",
            "blame_source": "unavailable",
            "resolved_references": {},
            "blame_metadata": {
                "classification": "new",
                "author": None,
                "date": None,
                "original_severity": "",
            },
            "origin": "new",
            "factual_verification": {
                "verified": True,
                "reason": "location unconfirmed - positional checks skipped",
                "code_at_lines": None,
            },
            "diff_validation": {
                "in_diff": None,
                "reason": "location unconfirmed - diff validation skipped",
            },
        }
    ]
    assert env["result"]["eliminated"] == []
    assert env["result"]["deltas"] == [
        {
            "id": "a",
            "verified": True,
            "origin": "new",
            "confidence": 80,
            "location_status": "unconfirmed",
            "diff_validation": {"in_diff": None},
            "path_role": "source",
            "file_added": False,
            "blame_class": "unknown",
            "blame_source": "unavailable",
            "resolved_references": {},
        }
    ]
    assert env["result"]["stats"] == {
        "total": 1,
        "new": 1,
        "surfaced": 0,
        "eliminated": 0,
    }


def test_receipt_token_value_spelling(invoke, tmp_path, verify_git):
    for spelling, token_proof in [
        ["0", "fnv1a32:0xd176d439"],
        ["-0", "fnv1a32:0x8e8176c6"],
    ]:
        env = json.loads(
            receipt(invoke, tmp_path, '{"findings":[],"n":' + spelling + "}").stdout
        )
        assert env["receipt"]["inline_checksum"] == token_proof
        assert env["receipt"]["input_checksum"] == "fnv1a32:0x55975cc7"
        assert (
            tmp_path / "slice.json"
        ).read_bytes() == b'{\n  "findings": [],\n  "n": 0\n}'


def test_receipt_fallback(invoke, tmp_path, verify_git):
    env = json.loads(
        receipt(invoke, tmp_path, '{"findings":[],"text":"caf%C3%A9","n":7.5}').stdout
    )
    assert env["status"] == "ok"
    assert list(env["receipt"]) == [
        "sha",
        "n_in",
        "nonce",
        "deltas_checksum",
        "inline_checksum",
        "base_branch",
        "diff_source",
        "record_proofs",
    ]
    assert env["receipt"]["deltas_checksum"] == "fnv1a32:0x741638a5"
    assert (
        tmp_path / "slice.json"
    ).read_bytes() == b'{\n  "findings": [],\n  "text": "caf\\u00e9",\n  "n": 7.5\n}'


def test_receipt_surrogate_ascii(invoke, tmp_path, verify_git):
    result = receipt(
        invoke, tmp_path, '{"findings":[{"id":"%uDCFF","description":"%uD800"}]}'
    )
    assert result.returncode == 0
    assert result.stdout.isascii()
    env = json.loads(result.stdout)
    assert env["result"]["deltas"] == [
        {
            "id": "\udcff",
            "verified": True,
            "origin": "new",
            "location_status": "unconfirmed",
            "diff_validation": {"in_diff": None},
            "path_role": "unknown",
            "blame_class": "unknown",
            "blame_source": "unavailable",
            "resolved_references": {},
        }
    ]
    assert env["result"]["verified"][0]["description"] == "\ud800"
    assert env["receipt"].pop("deltas_checksum") == verify.checksum_or_none(
        env["result"]["deltas"]
    )
    assert env["receipt"] == {
        "sha": "abcd",
        "n_in": 1,
        "nonce": "cli",
        "inline_checksum": "fnv1a32:0xc771c3d9",
        "base_branch": "main",
        "diff_source": {"path": None, "read": True},
        "record_proofs": [{"id": "\udcff", "checksum": "fnv1a32:0x09a08d9b"}],
        "input_checksum": "fnv1a32:0xefcb2f8b",
    }
    assert (tmp_path / "slice.json").read_bytes() == (
        b'{\n  "findings": [\n    {\n      "id": '
        b'"\\udcff",\n      "description": "\\ud800"\n    '
        b"}\n  ]\n}"
    )
    assert b'"id": "\\udcff"' in result.stdout
    assert b'"description": "\\ud800"' in result.stdout


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("write", id="RECEIPT-atomic-write-failure"),
        pytest.param("verification", id="RECEIPT-verification-exception"),
        pytest.param("output", id="RECEIPT-output-write-failure"),
        pytest.param("stdout", id="RECEIPT-stdout-write-failure"),
    ],
)
def test_receipt_failure(failure, invoke, tmp_path, verify_git, monkeypatch):
    path = tmp_path / "slice.json"
    path.write_bytes(b"untouched")
    if failure == "write":
        monkeypatch.setattr(
            fs.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("write denied"))
        )
        message = "write denied"
    elif failure in ("output", "stdout"):
        original = Path.open
        attempts = []

        def open_output(path, *args, **kwargs):
            if path.name == "out.json":
                attempts.append("file")
                raise OSError("output denied")
            return original(path, *args, **kwargs)

        def write_stdout(text):
            attempts.append(text)
            raise OSError("output denied")

        if failure == "output":
            monkeypatch.setattr(Path, "open", open_output)
            extra = ["--output", str(tmp_path / "out.json")]
        else:
            monkeypatch.setattr(sys.stdout, "write", write_stdout)
            extra = []
        result = receipt(invoke, tmp_path, extra=extra)
        assert result.returncode == 1
        assert attempts == (["file"] if failure == "output" else [EMPTY_BYTES.decode()])
        assert result.stdout == b""
        assert result.stderr == (
            b"Diff source: git diff main...HEAD "
            b"(three-dot), 0 bytes\nOSError: output denied\n"
        )
        assert path.read_bytes() == b'{\n  "findings": [],\n  "base_branch": "main"\n}'
        return
    else:
        verify_git[1]["diff"] = RuntimeError("verification failed")
        message = "verification failed"
    result = receipt(invoke, tmp_path)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "status": "failed",
        "exitCode": 1,
        "stderr": message,
    }
    assert path.read_bytes() == (
        b"untouched"
        if failure == "write"
        else b'{\n  "findings": [],\n  "base_branch": "main"\n}'
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param(
            ["--input-inline", EMPTY],
            "--input-inline requires --input for receipt mode",
            id="ARGV-inline-without-input",
        ),
        pytest.param(
            ["--input", "slice.json"],
            "--input requires --input-inline for receipt mode",
            id="ARGV-input-without-inline",
        ),
        pytest.param(
            [],
            "--input and --input-inline are required for receipt mode",
            id="ARGV-neither-input",
        ),
        pytest.param(
            ["slice.json"],
            "unrecognized arguments: slice.json",
            id="ARGV-positional-rejected",
        ),
    ],
)
def test_argv(argv, message, invoke, tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    result = invoke("verify_findings", ["--target-kind", "branch", *argv], tmp_path)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == f"verify_findings: {message}\n".encode()


def test_head_sha(invoke, tmp_path, verify_git):
    (tmp_path / "patch").write_text("", encoding="utf-8")
    verify_git[1]["rev-parse"] = lambda argv: (
        ("c0ffee\n", "", 0)
        if argv == ["git", "rev-parse", "--short", "HEAD"]
        else (str(tmp_path), "", 0)
    )
    result = invoke(
        "verify_findings",
        [
            "--target-kind",
            "branch",
            "--input",
            "slice.json",
            "--input-inline",
            EMPTY,
            "--diff-file",
            "patch",
        ],
        tmp_path,
    )
    assert json.loads(result.stdout)["receipt"]["sha"] == "c0ffee"
    assert verify_git[0] == [
        (["git", "rev-parse", "--show-toplevel"], {}),
        (["git", "rev-parse", "--short", "HEAD"], {}),
        (["git", "rev-parse", "HEAD"], {"cwd": str(tmp_path), "timeout": 3}),
    ]


@pytest.mark.parametrize(
    "token,flags,base",
    [
        pytest.param(
            '{"findings":[],"base_branch":"document"}',
            ["--base-branch", "flag"],
            "document",
            id="BASE-document-override",
        ),
        pytest.param('{"findings":[]}', [], "main", id="BASE-default-main"),
    ],
)
def test_base_branch(token, flags, base, invoke, tmp_path, verify_git):
    env = json.loads(receipt(invoke, tmp_path, token, flags).stdout)
    assert env["status"] == "ok"
    assert [cmd for cmd, _ in verify_git[0] if cmd[1] == "diff"] == [
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--end-of-options",
            f"{base}...HEAD",
        ]
    ]


@pytest.mark.parametrize(
    "head_reply",
    [
        pytest.param(("untrusted", "", 1), id="RECEIPT-failed-head-ignores-stdout"),
    ],
)
def test_receipt_defaults(head_reply, invoke, tmp_path, verify_git):
    (tmp_path / "patch").write_text("", encoding="utf-8")
    verify_git[1]["rev-parse"] = lambda argv: (
        head_reply
        if argv == ["git", "rev-parse", "--short", "HEAD"]
        else (str(tmp_path), "", 0)
    )
    result = invoke(
        "verify_findings",
        [
            "--target-kind",
            "branch",
            "--input",
            "slice.json",
            "--input-inline",
            EMPTY,
            "--diff-file",
            "patch",
        ],
        tmp_path,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["receipt"] == {
        "sha": "",
        "n_in": 0,
        "nonce": None,
        "deltas_checksum": "fnv1a32:0x741638a5",
        "inline_checksum": "fnv1a32:0x8c59538c",
        "base_branch": "main",
        "diff_source": {"path": "patch", "read": True},
        "record_proofs": [],
        "input_checksum": "fnv1a32:0x2eb50aa0",
    }
    assert verify_git[0] == [
        (["git", "rev-parse", "--show-toplevel"], {}),
        (["git", "rev-parse", "--short", "HEAD"], {}),
        (["git", "rev-parse", "HEAD"], {"cwd": str(tmp_path), "timeout": 3}),
    ]


def test_inline_progress_regression():
    # This subprocess bounds a decoder stall when this row is selected alone.
    probe = (
        "from gauntlet.verify.wire import CLI, decode_inline_slice; "
        "assert CLI.parser.prog == 'verify_findings'; "
        'assert decode_inline_slice(\'{"findings":[],"s":"a%C3%A9%uDCFFz"}\') '
        "== {'findings': [], 's': 'a\u00e9\\udcffz'}; print('decoded')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[1] / "scripts",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == b"decoded"


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "version",
        "sha",
        "context_sha256",
        "diff_sha256",
        "status",
        "entry_side",
        "entry_line",
        "entry_file",
        "entry_key",
        "entry_extra",
        "missing",
        "json",
    ],
)
def test_context_map_identity_controls_translation(
    tmp_path, invoke, verify_git, damage
):
    from tests.support.phase2 import write_context_inputs

    text = "perform_action(value)"
    (tmp_path / "item").write_text(f"{text}\n{text}\n", encoding="utf-8")
    diff = f"--- item\n+++ item\n@@ -1,2 +1,2 @@\n {text}\n {text}\n".encode()
    output = tmp_path / "out"
    write_context_inputs(output, b"rules\n" * 30, diff, sha="abcd")
    result = invoke(
        "write_shared_context",
        ["--output-dir", str(output), "--head-sha", "abcd"],
        tmp_path,
        b"triage",
    )
    assert result.returncode == 0, result.stderr
    context_path = output / "code-gauntlet-context-abcd.md"
    map_path = output / "code-gauntlet-context-map-abcd.json"
    raw = json.loads(map_path.read_bytes())
    claimed = int(
        next(key for key, value in raw["entries"].items() if value["line"] == 2)
    )
    if damage in ("version", "sha", "context_sha256", "diff_sha256", "status"):
        raw[damage] = "wrong"
    elif damage == "entry_key":
        raw["entries"]["01"] = next(iter(raw["entries"].values()))
    elif damage and damage.startswith("entry_"):
        field = damage.removeprefix("entry_")
        next(iter(raw["entries"].values()))[field] = {
            "side": "wrong",
            "line": True,
            "file": "",
            "extra": 1,
        }[field]
    map_path.write_text(json.dumps(raw) if damage != "json" else "{", encoding="utf-8")
    if damage == "missing":
        map_path = output / "missing.json"
    token = json.dumps(
        {
            "findings": [
                {"id": "f", "file": "item", "line_start": claimed, "anchor": text}
            ]
        },
        separators=(",", ":"),
    )
    token = token.replace("(", "%28").replace(")", "%29")
    result = receipt(
        invoke,
        tmp_path,
        token,
        [
            "--context-map",
            str(map_path),
            "--context-file",
            str(context_path),
            "--diff-file",
            str(output / "code-gauntlet-diff-abcd.patch"),
            "--target-kind",
            "pr",
        ],
    )
    env = json.loads(result.stdout)
    assert env["status"] == "ok", env
    delta = env["result"]["deltas"][0]
    if damage is None:
        assert (delta["line_start"], delta["line_end"], delta["location_status"]) == (
            2,
            2,
            "relocated",
        )
    else:
        assert delta["location_status"] == "unconfirmed"
        assert "line_start" not in delta and "line_end" not in delta
        assert b"context map unavailable" in result.stderr


@pytest.mark.parametrize(
    "anchor,end,accepted",
    [
        ("perform_action(value)", 1, True),
        ("perform_action(value)", 2, False),
        ("", 1, False),
        ("short", 1, False),
        ("O1 -|perform_action(value)", 1, False),
        ("perform_action(value)\n" * 5, 4, False),
    ],
)
def test_delta_anchor_authority(anchor, end, accepted):
    finding = {
        "id": "f",
        "anchor": anchor,
        "line_start": 1,
        "line_end": end,
        "location_status": "confirmed",
    }
    if accepted:
        assert verify.build_deltas([finding], [finding])[0]["line_end"] == end
    else:
        with pytest.raises(ValueError, match="anchor span"):
            verify.build_deltas([finding], [finding])


def test_plain_delta_does_not_echo_input_anchor():
    finding = {
        "id": "plain",
        "anchor": "perform_action(value)",
        "line_start": 1,
        "line_end": 1,
        "location_status": "confirmed",
    }
    delta = verify.build_deltas([finding], [finding])[0]
    assert "anchor" not in delta
    assert delta["line_start"] == 1


def test_presentation_quote_proof_reaches_trusted_js_join():
    from tests.support.pipeline import run_node

    anchor = "\n".join(
        f"{'9' * 110}\tN{i + 1} +|perform_action(value)" for i in range(4)
    )
    finding = {
        "id": "f",
        "anchor": anchor,
        "line_start": 1,
        "line_end": 4,
        "location_status": "confirmed",
    }
    deltas = verify.build_deltas([finding], [finding])
    checksum = verify.checksum_or_none(deltas)
    assert len(anchor) > 400
    result = run_node(
        """
import assert from 'node:assert/strict';
import { verifyStage } from './workflows/src/verifyStage.js';
import { verifyCtx, verifyInput, deltaEnvelope } from './workflows/test/helpers/verifyDelta.js';
let source = ''; for await (const chunk of process.stdin) source += chunk;
const { finding, deltas, checksum } = JSON.parse(source);
const ctx = verifyCtx(() => deltaEnvelope([finding], { nonce: 'n-1.0', deltas, checksum }));
const out = await verifyStage(ctx, verifyInput([finding]));
assert.equal(out.verified, true);
assert.equal(ctx.calls.length, 1);
assert.equal(out.findings[0].line_end, 4);
console.log('presentation quote accepted');
""",
        {"finding": finding, "deltas": deltas, "checksum": checksum},
    )
    assert result.stdout.strip() == "presentation quote accepted"
