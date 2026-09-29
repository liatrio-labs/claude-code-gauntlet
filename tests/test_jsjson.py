"""JavaScript JSON parity at the shared serialization boundary."""

import io
import json
from contextlib import redirect_stdout

import pytest
from gauntlet import jsjson
from gauntlet.jsjson import (
    JsSerializationError,
    checksum_or_none,
    dumps,
    js_stringify_pretty,
    utf16_len,
    write_result,
)
from gauntlet.verify.decide import _input_checksum, deltas_checksum


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"9": 9, "2": 2, "name": "x"}, '{\n  "2": 2,\n  "9": 9,\n  "name": "x"\n}'),
        ({"astral": "\U0001f642"}, '{\n  "astral": "\U0001f642"\n}'),
        ([], "[]"),
    ],
)
def test_js_stringify_pretty(value, expected):
    assert js_stringify_pretty(value) == expected


def test_proof_omits_value_the_encoder_cannot_nest(monkeypatch):
    # Python 3.14 serializes a document past the recursion limit (measured on the
    # Windows 3.14 CI leg), so the encoder's RecursionError is injected, not reached.
    def too_deep(*_args, **_kwargs):
        raise RecursionError

    monkeypatch.setattr(jsjson.json, "dumps", too_deep)
    assert checksum_or_none([0]) is None
    assert _input_checksum is deltas_checksum is checksum_or_none


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"text": "café"}, b'{"text": "caf\xc3\xa9"}'),
        ({"text": "\ud800"}, b'{"text": "\\ud800"}'),
        ({"text": "\udc00"}, b'{"text": "\\udc00"}'),
        ({"text": "\ud83d\ude00"}, b'{"text": "\xf0\x9f\x98\x80"}'),
        ({"number": 9007199254740991}, b'{"number": 9007199254740991}'),
        ({"number": -9007199254740991}, b'{"number": -9007199254740991}'),
    ],
)
def test_dumps_utf8_bytes(value, expected):
    assert dumps(value, ascii=False).encode("utf-8") == expected


def test_dumps_compact_separators_and_ascii_escaping():
    assert dumps({"café": [1, 2]}, compact=True).encode() == b'{"caf\\u00e9":[1,2]}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_dumps_rejects_non_finite_numbers(value):
    with pytest.raises(ValueError, match="Out of range float"):
        dumps({"number": value})


def test_write_result_escapes_lone_surrogates_and_indents():
    output = io.StringIO()
    with redirect_stdout(output):
        write_result({"text": "é\ud800"})
    assert output.getvalue().encode("utf-8") == b'{\n  "text": "\xc3\xa9\\ud800"\n}\n'


@pytest.mark.parametrize(
    ("text", "expected"),
    [("abc", 3), ("café", 4), ("日本語", 3), ("😀", 2)],
    ids=["ascii", "accented", "cjk", "surrogate-pair"],
)
def test_utf16_len_counts_code_units_not_codepoints(text, expected):
    assert utf16_len(text) == expected


def test_pretty_stringifier_handles_deep_alternating_containers():
    # Above a recursive key-order copy's ceiling on Python 3.10 and 3.11 (about 497)
    # and below json.dumps's own (about 993).
    value = 0
    for depth in range(600):
        value = {"value": value} if depth % 2 == 0 else [value]

    serialized = js_stringify_pretty(value)

    assert json.loads(serialized) == value


def test_number_error_names_its_path():
    with pytest.raises(JsSerializationError) as caught:
        js_stringify_pretty({"phases": {"challenge": {"stats": {"rate": 0.5}}}})
    assert "$.phases.challenge.stats.rate" in str(caught.value)


def test_non_string_object_keys_are_refused():
    with pytest.raises(JsSerializationError, match="non-string object key"):
        js_stringify_pretty({"outer": {1: "value"}})


@pytest.mark.parametrize(
    "value",
    [
        *(
            {"confidence": number}
            for number in [1e-7, 0.000001, 90.5, -0.0, float("nan"), float("inf")]
        ),
        *([number] for number in [2**53, -(2**53), 10**30]),
        1.0,
        2**53,
        {"x": object()},
        object(),
        *(
            json.loads(document)
            for document in [
                "[1e-7]",
                "[0.000001]",
                "[90.0]",
                "[-0.0]",
                "[9007199254740993]",
                "[1000000000000000000000000000000]",
                '{"stats": {"rate": 0.5}}',
                "[1.5]",
                "[1e21]",
                "[9007199254740992]",
                "[NaN]",
                "[Infinity]",
            ]
        ),
    ],
)
def test_unreproducible_values_are_refused(value):
    with pytest.raises(JsSerializationError):
        js_stringify_pretty(value)
    assert checksum_or_none(value) is None
