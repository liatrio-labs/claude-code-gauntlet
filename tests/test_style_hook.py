"""SessionStart payload bytes and the style hook's banner policy."""

import json

import pytest
from gauntlet import style_hook


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "<!-- GENERATED -->\n\n# Style\n", "# Style\n", id="banner-and-blank"
        ),
        pytest.param(
            "<!-- GENERATED -->\n# Style\n", "# Style\n", id="banner-no-blank"
        ),
        pytest.param("# Style\n", "# Style\n", id="no-banner"),
    ],
)
def test_strip_banner(text, expected):
    assert style_hook.strip_banner(text) == expected


def test_payload(tmp_path, monkeypatch, invoke):
    carrier = tmp_path / "carrier.md"
    carrier.write_text(
        "<!-- GENERATED -->\n\n# Session output style\n\n- Caf\u00e9.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(style_hook, "CARRIER", str(carrier))
    result = invoke(
        "emit_style_context", ["--help", "--unknown", "--", "value"], tmp_path
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout == (
        b'{"hookSpecificOutput": {"hookEventName": "SessionStart", '
        b'"additionalContext": "# Session output style\\n\\n- Caf\\u00e9.\\n"}}\n'
    )
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "# Session output style\n\n- Caf\u00e9.\n",
        }
    }


def test_missing_carrier(tmp_path, monkeypatch, invoke):
    monkeypatch.setattr(style_hook, "CARRIER", str(tmp_path / "absent.md"))
    result = invoke("emit_style_context", [], tmp_path)
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""


def test_unreadable_carrier(tmp_path, monkeypatch, invoke):
    carrier = tmp_path / "carrier.md"
    carrier.write_bytes(b"\xff")
    monkeypatch.setattr(style_hook, "CARRIER", str(carrier))
    result = invoke("emit_style_context", [], tmp_path)
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"decode" in result.stderr
