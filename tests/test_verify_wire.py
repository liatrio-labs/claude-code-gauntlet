"""Wire proofs at the inline and receipt boundaries."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet import fs, jsjson
from gauntlet.verify import wire as verify

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
            '{"findings":[{"evidence":"%GG"}]}',
            "invalid percent escape at $.findings[0].evidence",
            id="REJECT-nonhex",
        ),
        pytest.param(
            '{"findings":[],"s":"\\u0041"}',
            "JSON escape sequences are not canonical at $ (offending U+005C)",
            id="REJECT-json-unicode",
        ),
        pytest.param(
            '{"findings":[],"n":Infinity}',
            "invalid JSON at $ (non-finite JSON constant Infinity)",
            id="REJECT-infinity",
        ),
        pytest.param(
            '{"findings":[],"n":-Infinity}',
            "invalid JSON at $ (non-finite JSON constant -Infinity)",
            id="REJECT-negative-infinity",
        ),
        pytest.param(
            '{"findings":[{"`":"ok"}]}',
            "raw U+0060 at $.findings[0].<key>",
            id="REJECT-unsafe-key",
        ),
        pytest.param(
            '{"findings":[{"evidence":"%41"}]}',
            "non-canonical percent escape %41 at $.findings[0].evidence (byte is SAFE "
            "ASCII)",
            id="REJECT-encoded-safe-value",
        ),
        pytest.param(
            '{"findings":[{"evidence":"%FF"}]}',
            "invalid UTF-8 at $.findings[0].evidence (offending bytes FF, 'utf-8' codec "
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
        pytest.param("[]", "root must be an object at $", id="REJECT-nonobject"),
        pytest.param(
            "{}", "missing required 'findings' array at $", id="REJECT-missing-findings"
        ),
        pytest.param(
            '{"findings":{}}', "'findings' must be an array at $", id="REJECT-nonarray"
        ),
        pytest.param(
            '{"findings":[],"n":1,"n":2}',
            "invalid JSON at $ (duplicate object key 'n')",
            id="REJECT-later-duplicate-key",
        ),
        pytest.param(
            '{"findings":[],"s":"%uD7FF"}',
            "invalid %u escape U+D7FF at $.s (only surrogates are allowed)",
            id="REJECT-below-surrogate-range",
        ),
        pytest.param(
            '{"findings":[],"s":"%uE000"}',
            "invalid %u escape U+E000 at $.s (only surrogates are allowed)",
            id="REJECT-above-surrogate-range",
        ),
        pytest.param(
            '{"findings":[],"s":"%uD800%uDC00"}',
            "non-canonical surrogate pair %uD800%uDC00 at $.s "
            "(an astral character is spelled as its UTF-8 bytes)",
            id="REJECT-minimum-surrogate-pair",
        ),
        pytest.param(
            '{"findings":[],"s":"%uDBFF%uDFFF"}',
            "non-canonical surrogate pair %uDBFF%uDFFF at $.s "
            "(an astral character is spelled as its UTF-8 bytes)",
            id="REJECT-maximum-surrogate-pair",
        ),
        pytest.param(
            '{"findings":[],"s":"%uD800%uE000"}',
            "invalid %u escape U+E000 at $.s (only surrogates are allowed)",
            id="REJECT-high-then-nonsurrogate",
        ),
        pytest.param(
            '{"findings":[],"s":"%uD800%uDC000"}',
            "non-canonical surrogate pair %uD800%uDC00 at $.s "
            "(an astral character is spelled as its UTF-8 bytes)",
            id="REJECT-pair-before-hex-suffix",
        ),
        pytest.param(
            '{"findings":invalid,"s":"\\n"}',
            "JSON escape sequences are not canonical at $ (offending U+005C)",
            id="REJECT-json-escape-before-parse",
        ),
    ],
)
def test_reject(token, message, invoke, tmp_path, verify_git):
    expected = "inline slice-input rejected: " + message
    with pytest.raises(ValueError) as caught:
        verify.validate_input_shape(verify.decode_inline_slice(token))
    assert str(caught.value) == expected
    result = receipt(invoke, tmp_path, token)
    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "status": "failed",
        "exitCode": 1,
        "stderr": expected,
    }
    assert not (tmp_path / "slice.json").exists()


def test_reject_surrogate_acceptance():
    pairs = [
        ["%uD800%uDBFF", "\ud800\udbff"],
        ["%uDFFF%uD800", "\udfff\ud800"],
        ["tail%uD800", "tail\ud800"],
        ["%uD800x%uDFFF", "\ud800x\udfff"],
        ["%uD800%C3%A9", "\ud800\xe9"],
        ["%C3%A9%uDCFF", "\xe9\udcff"],
        ["%F0%9F%98%80", "\U0001f600"],
        ["%F4%8F%BF%BF", "\U0010ffff"],
    ]
    for token, value in pairs:
        assert verify.decode_inline_slice('{"findings":[],"s":"' + token + '"}') == {
            "findings": [],
            "s": value,
        }


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
            {"confidence": "\u0085+80\u001c", "line_start": " -2 "},
            {"confidence": 80, "line_start": -2},
            id="COERCE-signed-whitespace",
        ),
        pytest.param(
            {"confidence": "٨٠"}, {"confidence": 80}, id="COERCE-unicode-digits"
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
        pytest.param(
            {"confidence": 80.0}, {"confidence": 80}, id="COERCE-integral-float"
        ),
        pytest.param(
            {
                "confidence": True,
                "line_start": None,
                "line_end": "bad",
                "line": "1.5",
            },
            {
                "confidence": True,
                "line_start": None,
                "line_end": "bad",
                "line": "1.5",
            },
            id="COERCE-junk",
        ),
        pytest.param(["untouched"], ["untouched"], id="COERCE-nondict"),
    ],
)
def test_coerce(before, after):
    finding = copy.deepcopy(before)
    assert verify.coerce_numeric_fields(finding) is finding
    assert finding == after
    verify.coerce_numeric_fields(finding)
    assert finding == after
    if (
        isinstance(after, dict)
        and after.get("confidence") == 80
        and isinstance(before.get("confidence"), float)
    ):
        assert type(finding["confidence"]) is int


def test_coerce_nan():
    value = float("nan")
    finding = {"confidence": value}
    assert verify.coerce_numeric_fields(finding) is finding
    assert finding["confidence"] is value


@pytest.mark.parametrize(
    ("findings", "kept", "expected"),
    [
        pytest.param(
            [{"id": "copy"}],
            [],
            [{"id": "copy", "verified": False}],
            id="DELTA-equal-copy",
        ),
        pytest.param(
            [{"id": " a "}],
            [0],
            [{"id": " a ", "verified": True}],
            id="DELTA-padded-id",
        ),
        pytest.param(
            [None, {}, {"id": " "}, {"id": 1}], [], [], id="DELTA-unusable-id"
        ),
        pytest.param(
            [
                {
                    "id": "a",
                    "elimination_reason": "gone",
                    "confidence": 80,
                    "severity": "high",
                    "origin": "new",
                    "file": "hidden",
                }
            ],
            [],
            [
                {
                    "id": "a",
                    "verified": False,
                    "origin": "new",
                    "severity": "high",
                    "confidence": 80,
                    "elimination_reason": "gone",
                }
            ],
            id="DELTA-full-key-order",
        ),
        pytest.param(
            [{"id": "a", "confidence": 64.5}, {"id": "b", "confidence": -64.5}],
            [0, 1],
            [
                {"id": "a", "verified": True, "confidence": 65},
                {"id": "b", "verified": True, "confidence": -64},
            ],
            id="DELTA-half-up",
        ),
        pytest.param(
            [
                {"id": "a", "confidence": 9007199254740991},
                {"id": "b", "confidence": -9007199254740991},
                {"id": "c", "confidence": 9007199254740992},
                {"id": "d", "confidence": float("nan")},
                {"id": "e", "confidence": float("inf")},
                {"id": "f", "confidence": True},
            ],
            [],
            [
                {"id": "a", "verified": False, "confidence": 9007199254740991},
                {"id": "b", "verified": False, "confidence": -9007199254740991},
                {"id": "c", "verified": False},
                {"id": "d", "verified": False},
                {"id": "e", "verified": False},
                {"id": "f", "verified": False},
            ],
            id="DELTA-confidence-limits",
        ),
        pytest.param(
            [
                {"id": "a", "confidence": "80"},
                {"id": "b", "confidence": []},
                {"id": "c", "confidence": {}},
            ],
            [0, 1, 2],
            [
                {"id": "a", "verified": True},
                {"id": "b", "verified": True},
                {"id": "c", "verified": True},
            ],
            id="DELTA-quoted-container-confidence",
        ),
        pytest.param(
            [{"id": "negative", "confidence": -9007199254740992}],
            [0],
            [{"id": "negative", "verified": True}],
            id="DELTA-negative-unsafe-confidence",
        ),
    ],
)
def test_delta(findings, kept, expected, request):
    originals = copy.deepcopy(findings)
    verified = [originals[i] for i in kept]
    if request.node.callspec.id == "DELTA-equal-copy":
        verified = copy.deepcopy(originals)
    actual = verify.build_deltas(originals, verified)
    assert actual == expected
    assert [list(delta) for delta in actual] == [list(delta) for delta in expected]


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


def test_receipt_pre_coercion(invoke, tmp_path, verify_git):
    (tmp_path / "source").write_text("code\n", encoding="utf-8")
    token = (
        '{"findings":[{"id":"a","file":"source","line_start":"1","confidence":"80"}]}'
    )
    result = receipt(invoke, tmp_path, token)
    assert result.returncode == 0
    env = json.loads(result.stdout)
    assert env["receipt"] == {
        "sha": "abcd",
        "n_in": 1,
        "nonce": "cli",
        "deltas_checksum": "fnv1a32:0xc9ca61b0",
        "inline_checksum": "fnv1a32:0x17c759ec",
        "input_checksum": "fnv1a32:0x6754b4b8",
    }
    assert (
        (tmp_path / "slice.json").read_bytes()
        == b'{\n  "findings": [\n    {\n      "id": "a",\n      "file": "source",\n      "line_start": "1",\n      "confidence": "80"\n    }\n  ]\n}'
    )
    assert env["result"]["verified"] == [
        {
            "id": "a",
            "file": "source",
            "line_start": 1,
            "confidence": 80,
            "blame_metadata": {
                "classification": "new",
                "author": "First Author",
                "date": "2024-01-02",
                "original_severity": "",
            },
            "origin": "surfaced",
            "factual_verification": {
                "verified": True,
                "reason": "no extractable symbols \u2014 verification skipped",
                "code_at_lines": "code",
            },
            "diff_validation": {
                "in_diff": False,
                "reason": "lines 1-1 of 'source' not found in diff \u2014 "
                "tagged as surfaced (was: new)",
            },
        }
    ]
    assert env["result"]["eliminated"] == []
    assert env["result"]["deltas"] == [
        {"id": "a", "verified": True, "origin": "surfaced", "confidence": 80}
    ]
    assert env["result"]["stats"] == {
        "total": 1,
        "new": 0,
        "surfaced": 1,
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


@pytest.mark.parametrize(
    ("token", "written"),
    [
        pytest.param(
            '{"findings":[],"text":"caf%C3%A9","n":7.5}',
            b'{\n  "findings": [],\n  "text": "caf\\u00e9",\n  "n": 7.5\n}',
            id="RECEIPT-ASCII-input-fallback",
        ),
    ],
)
def test_receipt_fallback(token, written, invoke, tmp_path, verify_git):
    env = json.loads(receipt(invoke, tmp_path, token).stdout)
    assert env["status"] == "ok"
    assert list(env["receipt"]) == [
        "sha",
        "n_in",
        "nonce",
        "deltas_checksum",
        "inline_checksum",
    ]
    assert env["receipt"]["deltas_checksum"] == "fnv1a32:0x741638a5"
    assert (tmp_path / "slice.json").read_bytes() == written


def test_receipt_null_delta_proof(invoke, tmp_path, verify_git):
    env = json.loads(
        receipt(invoke, tmp_path, '{"findings":[{"id":"a","severity":2.5}]}').stdout
    )
    assert env["status"] == "ok"
    assert env["receipt"]["deltas_checksum"] is None
    assert env["result"]["deltas"] == [
        {"id": "a", "verified": True, "origin": "new", "severity": 2.5}
    ]


def test_receipt_surrogate_ascii(invoke, tmp_path, verify_git):
    result = receipt(
        invoke, tmp_path, '{"findings":[{"id":"%uDCFF","description":"%uD800"}]}'
    )
    assert result.returncode == 0
    assert result.stdout.isascii()
    env = json.loads(result.stdout)
    assert env["result"]["deltas"] == [
        {"id": "\udcff", "verified": True, "origin": "new"}
    ]
    assert env["result"]["verified"][0]["description"] == "\ud800"
    assert env["receipt"] == {
        "sha": "abcd",
        "n_in": 1,
        "nonce": "cli",
        "deltas_checksum": "fnv1a32:0x877e5b9a",
        "inline_checksum": "fnv1a32:0xc771c3d9",
        "input_checksum": "fnv1a32:0xefcb2f8b",
    }
    assert (
        (tmp_path / "slice.json").read_bytes()
        == b'{\n  "findings": [\n    {\n      "id": "\\udcff",\n      "description": "\\ud800"\n    }\n  ]\n}'
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
        assert (
            result.stderr
            == b"Diff source: git diff main...HEAD (three-dot), 0 bytes\nOSError: output denied\n"
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
    result = invoke("verify_findings", argv, tmp_path)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == f"verify_findings: {message}\n".encode()


def test_proof_canonical_spelling_injective():
    for cp in [*range(0x20, 0x300), 0x2028, 0xD800, 0xDFFF, 0x1F600, 0x10FFFF]:
        char = chr(cp)
        raw = char.encode("utf-8", "surrogatepass")
        candidates = {
            char,
            "".join(f"%{b:02X}" for b in raw),
            "".join(f"%{b:02x}" for b in raw),
            f"%u{cp:04X}",
            f"%u{cp:04x}",
        }
        if cp > 0xFFFF:
            candidates.add(
                f"%u{0xD800 + (cp - 0x10000 >> 10):04X}%u{0xDC00 + (cp - 0x10000 & 0x3FF):04X}"
            )
        accepted = []
        for token in candidates:
            try:
                value = verify.decode_inline_slice(
                    '{"findings":[],"s":"' + token + '"}'
                )["s"]
            except Exception as exc:  # noqa: BLE001 - rejections are the property probes
                assert str(exc).startswith("inline slice-input rejected:")
                continue
            normalized = value.encode("utf-16-le", "surrogatepass")
            if normalized == char.encode("utf-16-le", "surrogatepass"):
                accepted.append(token)
        assert len(accepted) <= 1, (hex(cp), accepted)


def test_proof_before_decode_write_coerce(invoke, tmp_path, verify_git, monkeypatch):
    events = []
    for name, label in [
        ["fnv1a32", "token"],
        ["decode_inline_slice", "decode"],
        ["checksum_or_none", "value"],
        ["write_atomic", "write"],
        ["coerce_numeric_fields", "coerce"],
    ]:
        original = getattr(verify, name)

        def spy(*args, _original=original, _label=label, **kwargs):
            events.append(_label)
            return _original(*args, **kwargs)

        monkeypatch.setattr(verify, name, spy)
    result = receipt(invoke, tmp_path, '{"findings":[{"id":"a"}]}')
    assert json.loads(result.stdout)["status"] == "ok"
    assert events == ["token", "decode", "value", "write", "coerce", "value"]


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
        ["git", "diff", "--end-of-options", f"{base}...HEAD"]
    ]


def test_list_line_start_failed_envelope(invoke, tmp_path, verify_git):
    (tmp_path / "source").write_text("code\n", encoding="utf-8")
    result = receipt(
        invoke, tmp_path, '{"findings":[{"file":"source","line_start":[1]}]}'
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "status": "failed",
        "exitCode": 1,
        "stderr": "'<' not supported between instances of 'list' and 'int'",
    }


def test_inline_low_low_pair():
    assert verify.decode_inline_slice('{"findings":[],"s":"%uDC00%uDFFF"}') == {
        "findings": [],
        "s": "\udc00\udfff",
    }


@pytest.mark.parametrize(
    "head_reply",
    [
        pytest.param(("", "", 1), id="RECEIPT-default-sha-and-nonce"),
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
        ["--input", "slice.json", "--input-inline", EMPTY, "--diff-file", "patch"],
        tmp_path,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["receipt"] == {
        "sha": "",
        "n_in": 0,
        "nonce": None,
        "deltas_checksum": "fnv1a32:0x741638a5",
        "inline_checksum": "fnv1a32:0x8c59538c",
        "input_checksum": "fnv1a32:0x2eb50aa0",
    }
    assert verify_git[0] == [
        (["git", "rev-parse", "--show-toplevel"], {}),
        (["git", "rev-parse", "--short", "HEAD"], {}),
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
        timeout=2,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"decoded\n"
