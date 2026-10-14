"""SessionStart payload bytes and the style hook's banner policy."""

from gauntlet import style_hook


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


def test_missing_carrier(tmp_path, monkeypatch, invoke):
    monkeypatch.setattr(style_hook, "CARRIER", str(tmp_path / "absent.md"))
    result = invoke("emit_style_context", [], tmp_path)
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
