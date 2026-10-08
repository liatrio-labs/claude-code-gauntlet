"""Wire proofs at the inline and receipt boundaries."""

import copy
import json
import sys
from pathlib import Path

import pytest
from gauntlet import fs, jsjson
from gauntlet.verify import wire as verify

VECTORS = Path(__file__).parent / "fixtures/parity/slice_inline"
EMPTY = '{"findings":[],"base_branch":"main"}'
EMPTY_BYTES = b'{\n  "status": "ok",\n  "receipt": {\n    "sha": "abcd",\n    "n_in": 0,\n    "nonce": "cli",\n    "deltas_checksum": "fnv1a32:0x741638a5",\n    "inline_checksum": "fnv1a32:0x8c59538c",\n    "input_checksum": "fnv1a32:0x2eb50aa0"\n  },\n  "result": {\n    "deltas": [],\n    "verified": [],\n    "eliminated": [],\n    "stats": {\n      "total": 0,\n      "new": 0,\n      "surfaced": 0,\n      "eliminated": 0\n    }\n  }\n}'


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
    json.loads(
        '["astral","control_chars","empty_findings","lone_surrogates","nested_cross_file_refs","non_ascii_keys","percent_forms","safe_punctuation","surrogate_pair","three_findings"]'
    ),
    ids=lambda case: f"INLINE-{case}",
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
        pytest.param(*row[1:], id=row[0])
        for row in json.loads(r"""[
        ["REJECT-raw-nested", "{\"findings\":[{\"evidence\":\"`\"}]}", "raw U+0060 at $.findings[0].evidence"],
        ["REJECT-invalid-percent", "{\"findings\":[{\"evidence\":\"%5c\"}]}", "invalid percent escape at $.findings[0].evidence"],
        ["REJECT-nonhex", "{\"findings\":[{\"evidence\":\"%GG\"}]}", "invalid percent escape at $.findings[0].evidence"],
        ["REJECT-truncated", "{\"findings\":[{\"evidence\":\"%4\"}]}", "invalid percent escape at $.findings[0].evidence"],
        ["REJECT-json-unicode", "{\"findings\":[],\"s\":\"\\u0041\"}", "JSON escape sequences are not canonical at $ (offending U+005C)"],
        ["REJECT-infinity", "{\"findings\":[],\"n\":Infinity}", "invalid JSON at $ (non-finite JSON constant Infinity)"],
        ["REJECT-negative-infinity", "{\"findings\":[],\"n\":-Infinity}", "invalid JSON at $ (non-finite JSON constant -Infinity)"],
        ["REJECT-unsafe-key", "{\"findings\":[{\"`\":\"ok\"}]}", "raw U+0060 at $.findings[0].<key>"],
        ["REJECT-encoded-safe-key", "{\"findings\":[{\"%69d\":\"ok\"}]}", "non-canonical percent escape %69 at $.findings[0].<key> (byte is SAFE ASCII)"],
        ["REJECT-encoded-safe-value", "{\"findings\":[{\"evidence\":\"%41\"}]}", "non-canonical percent escape %41 at $.findings[0].evidence (byte is SAFE ASCII)"],
        ["REJECT-invalid-utf8", "{\"findings\":[{\"evidence\":\"%FF\"}]}", "invalid UTF-8 at $.findings[0].evidence (offending bytes FF, 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte)"],
        ["REJECT-non-surrogate-u", "{\"findings\":[{\"evidence\":\"%u0041\"}]}", "invalid %u escape U+0041 at $.findings[0].evidence (only surrogates are allowed)"],
        ["REJECT-adjacent-pair-at-end", "{\"findings\":[{\"evidence\":\"%uD83D%uDE00\"}]}", "non-canonical surrogate pair %uD83D%uDE00 at $.findings[0].evidence (an astral character is spelled as its UTF-8 bytes)"],
        ["REJECT-paired-u-key", "{\"findings\":[{\"%uD83D%uDE00\":\"ok\"}]}", "non-canonical surrogate pair %uD83D%uDE00 at $.findings[0].<key> (an astral character is spelled as its UTF-8 bytes)"],
        ["REJECT-duplicate-key", "{\"findings\":[],\"findings\":[]}", "invalid JSON at $ (duplicate object key 'findings')"],
        ["REJECT-nonfinite", "{\"findings\":[],\"n\":NaN}", "invalid JSON at $ (non-finite JSON constant NaN)"],
        ["REJECT-json-escape", "{\"findings\":[],\"s\":\"\\n\"}", "JSON escape sequences are not canonical at $ (offending U+005C)"],
        ["REJECT-nonobject", "[]", "root must be an object at $"],
        ["REJECT-missing-findings", "{}", "missing required 'findings' array at $"],
        ["REJECT-nonarray", "{\"findings\":{}}", "'findings' must be an array at $"]
    ]""")
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
    pairs = json.loads(
        '[["%uD800%uDBFF","\\ud800\\udbff"],["%uDFFF%uD800","\\udfff\\ud800"],["tail%uD800","tail\\ud800"],["%uD800x%uDFFF","\\ud800x\\udfff"],["%uD800%C3%A9","\\ud800\\u00e9"],["%C3%A9%uDCFF","\\u00e9\\udcff"],["%F0%9F%98%80","\\ud83d\\ude00"],["%F4%8F%BF%BF","\\udbff\\udfff"]]'
    )
    for token, value in pairs:
        assert verify.decode_inline_slice('{"findings":[],"s":"' + token + '"}') == {
            "findings": [],
            "s": value,
        }


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(*row[1:], id=row[0])
        for row in json.loads(r"""[
        ["COERCE-five-fields", {"line_start": "10", "line_end": "11", "line": "12", "end_line": "13", "confidence": "80", "unknown": "9"}, {"line_start": 10, "line_end": 11, "line": 12, "end_line": 13, "confidence": 80, "unknown": "9"}],
        ["COERCE-signed-whitespace", {"confidence": "\u0085+80\u001c", "line_start": " -2 "}, {"confidence": 80, "line_start": -2}],
        ["COERCE-unicode-digits", {"confidence": "\u0668\u0660"}, {"confidence": 80}],
        ["COERCE-positive-half", {"confidence": 64.5, "line_start": 1.49}, {"confidence": 65, "line_start": 1}],
        ["COERCE-negative-half", {"confidence": -64.5, "line_start": -1.51}, {"confidence": -64, "line_start": -2}],
        ["COERCE-integral-float", {"confidence": 80.0}, {"confidence": 80}],
        ["COERCE-junk", {"confidence": true, "line_start": null, "line_end": "bad", "line": "1.5"}, {"confidence": true, "line_start": null, "line_end": "bad", "line": "1.5"}],
        ["COERCE-nondict", ["untouched"], ["untouched"]],
        ["COERCE-nonfinite", {"confidence": Infinity, "line": -Infinity}, {"confidence": Infinity, "line": -Infinity}],
        ["COERCE-unsafe-int", {"confidence": 9007199254740992}, {"confidence": 9007199254740992}]
    ]""")
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
        pytest.param(*row[1:], id=row[0])
        for row in json.loads(r"""[
        ["DELTA-dispatch-order", [{"id": "b", "origin": "new"}, {"id": "a", "elimination_reason": "gone"}], [0], [{"id": "b", "verified": true, "origin": "new"}, {"id": "a", "verified": false, "elimination_reason": "gone"}]],
        ["DELTA-same-id", [{"id": "same"}, {"id": "same"}], [1], [{"id": "same", "verified": false}, {"id": "same", "verified": true}]],
        ["DELTA-equal-copy", [{"id": "copy"}], [], [{"id": "copy", "verified": false}]],
        ["DELTA-unusable-id", [null, {}, {"id": " "}, {"id": 1}], [], []],
        ["DELTA-padded-id", [{"id": " a "}], [0], [{"id": " a ", "verified": true}]],
        ["DELTA-null-unknown", [{"id": "a", "origin": null, "confidence": null, "severity": null, "unknown": 7}], [0], [{"id": "a", "verified": true}]],
        ["DELTA-full-key-order", [{"id": "a", "elimination_reason": "gone", "confidence": 80, "severity": "high", "origin": "new", "file": "hidden"}], [], [{"id": "a", "verified": false, "origin": "new", "severity": "high", "confidence": 80, "elimination_reason": "gone"}]],
        ["DELTA-half-up", [{"id": "a", "confidence": 64.5}, {"id": "b", "confidence": -64.5}], [0, 1], [{"id": "a", "verified": true, "confidence": 65}, {"id": "b", "verified": true, "confidence": -64}]],
        ["DELTA-confidence-limits", [{"id": "a", "confidence": 9007199254740991}, {"id": "b", "confidence": -9007199254740991}, {"id": "c", "confidence": 9007199254740992}, {"id": "d", "confidence": NaN}, {"id": "e", "confidence": Infinity}, {"id": "f", "confidence": true}], [], [{"id": "a", "verified": false, "confidence": 9007199254740991}, {"id": "b", "verified": false, "confidence": -9007199254740991}, {"id": "c", "verified": false}, {"id": "d", "verified": false}, {"id": "e", "verified": false}, {"id": "f", "verified": false}]],
        ["DELTA-unspellable-severity", [{"id": "a", "severity": 2.5}], [0], [{"id": "a", "verified": true, "severity": 2.5}]],
        ["DELTA-quoted-container-confidence", [{"id": "a", "confidence": "80"}, {"id": "b", "confidence": []}, {"id": "c", "confidence": {}}], [0, 1, 2], [{"id": "a", "verified": true}, {"id": "b", "verified": true}, {"id": "c", "verified": true}]]
    ]""")
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
    if request.node.callspec.id == "DELTA-unspellable-severity":
        assert verify.checksum_or_none(actual) is None


@pytest.mark.parametrize(
    "destination",
    ["stdout", "file"],
    ids=["RECEIPT-stdout-exact-bytes", "RECEIPT-file-no-final-LF"],
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
    assert env["receipt"] == json.loads(
        '{"sha":"abcd","n_in":1,"nonce":"cli","deltas_checksum":"fnv1a32:0xc9ca61b0","inline_checksum":"fnv1a32:0x17c759ec","input_checksum":"fnv1a32:0x6754b4b8"}'
    )
    assert (
        (tmp_path / "slice.json").read_bytes()
        == b'{\n  "findings": [\n    {\n      "id": "a",\n      "file": "source",\n      "line_start": "1",\n      "confidence": "80"\n    }\n  ]\n}'
    )
    assert env["result"]["verified"] == json.loads(
        '[{"id":"a","file":"source","line_start":1,"confidence":80,"blame_metadata":{"classification":"new","author":"First Author","date":"2024-01-02","original_severity":""},"origin":"surfaced","factual_verification":{"verified":true,"reason":"no extractable symbols \\u2014 verification skipped","code_at_lines":"code"},"diff_validation":{"in_diff":false,"reason":"lines 1-1 of \'source\' not found in diff \\u2014 tagged as surfaced (was: new)"}}]'
    )
    assert env["result"]["eliminated"] == []
    assert env["result"]["deltas"] == json.loads(
        '[{"id":"a","verified":true,"origin":"surfaced","confidence":80}]'
    )
    assert env["result"]["stats"] == json.loads(
        '{"total":1,"new":0,"surfaced":1,"eliminated":0}'
    )


def test_receipt_token_value_spelling(invoke, tmp_path, verify_git):
    for spelling, token_proof in json.loads(
        '[["0","fnv1a32:0xd176d439"],["-0","fnv1a32:0x8e8176c6"]]'
    ):
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
        ('{"findings":[],"n":7.5}', b'{\n  "findings": [],\n  "n": 7.5\n}'),
        (
            '{"findings":[],"text":"caf%C3%A9","n":7.5}',
            b'{\n  "findings": [],\n  "text": "caf\\u00e9",\n  "n": 7.5\n}',
        ),
    ],
    ids=["RECEIPT-omitted-input-proof", "RECEIPT-ASCII-input-fallback"],
)
def test_receipt_fallback(token, written, invoke, tmp_path, verify_git):
    env = json.loads(receipt(invoke, tmp_path, token).stdout)
    assert env["status"] == "ok"
    assert list(env["receipt"]) == json.loads(
        '["sha","n_in","nonce","deltas_checksum","inline_checksum"]'
    )
    assert env["receipt"]["deltas_checksum"] == "fnv1a32:0x741638a5"
    assert (tmp_path / "slice.json").read_bytes() == written


def test_receipt_null_delta_proof(invoke, tmp_path, verify_git):
    env = json.loads(
        receipt(invoke, tmp_path, '{"findings":[{"id":"a","severity":2.5}]}').stdout
    )
    assert env["status"] == "ok"
    assert env["receipt"]["deltas_checksum"] is None
    assert env["result"]["deltas"] == json.loads(
        '[{"id":"a","verified":true,"origin":"new","severity":2.5}]'
    )


def test_receipt_surrogate_ascii(invoke, tmp_path, verify_git):
    result = receipt(
        invoke, tmp_path, '{"findings":[{"id":"%uDCFF","description":"%uD800"}]}'
    )
    assert result.returncode == 0
    assert result.stdout.isascii()
    env = json.loads(result.stdout)
    assert env["result"]["deltas"] == json.loads(
        '[{"id":"\\udcff","verified":true,"origin":"new"}]'
    )
    assert env["result"]["verified"][0]["description"] == "\ud800"
    assert env["receipt"] == json.loads(
        '{"sha":"abcd","n_in":1,"nonce":"cli","deltas_checksum":"fnv1a32:0x877e5b9a","inline_checksum":"fnv1a32:0xc771c3d9","input_checksum":"fnv1a32:0xefcb2f8b"}'
    )
    assert (
        (tmp_path / "slice.json").read_bytes()
        == b'{\n  "findings": [\n    {\n      "id": "\\udcff",\n      "description": "\\ud800"\n    }\n  ]\n}'
    )
    assert b'"id": "\\udcff"' in result.stdout
    assert b'"description": "\\ud800"' in result.stdout


@pytest.mark.parametrize(
    "failure",
    ["write", "verification", "output", "stdout"],
    ids=json.loads(
        '["RECEIPT-atomic-write-failure","RECEIPT-verification-exception","RECEIPT-output-write-failure","RECEIPT-stdout-write-failure"]'
    ),
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
        (["--input-inline", EMPTY], "--input-inline requires --input for receipt mode"),
        (["--input", "slice.json"], "--input requires --input-inline for receipt mode"),
        ([], "--input and --input-inline are required for receipt mode"),
        (["slice.json"], "unrecognized arguments: slice.json"),
    ],
    ids=json.loads(
        '["ARGV-inline-without-input","ARGV-input-without-inline","ARGV-neither-input","ARGV-positional-rejected"]'
    ),
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
                f"%u{55296 + (cp - 65536 >> 10):04X}%u{56320 + (cp - 65536 & 1023):04X}"
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
    for name, label in json.loads(
        '[["fnv1a32","token"],["decode_inline_slice","decode"],["checksum_or_none","value"],["write_atomic","write"],["coerce_numeric_fields","coerce"]]'
    ):
        original = getattr(verify, name)

        def spy(*args, _original=original, _label=label, **kwargs):
            events.append(_label)
            return _original(*args, **kwargs)

        monkeypatch.setattr(verify, name, spy)
    result = receipt(invoke, tmp_path, '{"findings":[{"id":"a"}]}')
    assert json.loads(result.stdout)["status"] == "ok"
    assert events == ["token", "decode", "value", "write", "coerce", "value"]


@pytest.mark.parametrize(
    "head,sha,commands",
    json.loads(
        '[[[],"c0ffee",[["git","rev-parse","--show-toplevel"],["git","rev-parse","--short","HEAD"]]],[["--head-sha","pinned"],"pinned",[["git","rev-parse","--show-toplevel"]]]]'
    ),
)
def test_head_sha(head, sha, commands, invoke, tmp_path, verify_git):
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
            *head,
        ],
        tmp_path,
    )
    assert json.loads(result.stdout)["receipt"]["sha"] == sha
    assert verify_git[0] == [(argv, {}) for argv in commands]


@pytest.mark.parametrize(
    "token,flags,base",
    json.loads(
        '[["{\\"findings\\":[],\\"base_branch\\":\\"document\\"}",["--base-branch","flag"],"document"],["{\\"findings\\":[]}",[],"main"]]'
    ),
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
