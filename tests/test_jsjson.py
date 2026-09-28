"""Bytes emitted by the shared JSON writer."""

import io
from contextlib import redirect_stdout

import pytest
from gauntlet.jsjson import dumps, write_result


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
    assert dumps({"café": [1, 2]}, compact=True).encode() == (b'{"caf\\u00e9":[1,2]}')


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_dumps_rejects_non_finite_numbers(value):
    with pytest.raises(ValueError, match="Out of range float"):
        dumps({"number": value})


def test_write_result_escapes_lone_surrogates_and_indents():
    output = io.StringIO()
    with redirect_stdout(output):
        write_result({"text": "é\ud800"})
    assert output.getvalue().encode("utf-8") == b'{\n  "text": "\xc3\xa9\\ud800"\n}\n'
