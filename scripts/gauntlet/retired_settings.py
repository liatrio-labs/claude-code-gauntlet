"""Recognize retired settings for a notice without parsing or applying configuration."""

from __future__ import annotations

import re

_MARKERS = re.compile(r"```|<!--")
_KEY = re.compile(
    r"(confidence_threshold|security_min_confidence|severity_threshold|ignore)\s*[:=]",
    re.I,
)
_HEADING = re.compile(r"#{1,6}[ \t]+Default Delivery[ \t]*", re.I)
_CSV = re.compile(r"[a-z_]+(?:,[a-z_]+)*")


def _branded(text: str, start: int, fence: bool) -> bool:
    cursor = start
    if fence and text[cursor : cursor + 4].lower() == "yaml":
        cursor += 4
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if fence and cursor < len(text) and text[cursor] == "#":
        cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    banner = text[cursor : cursor + 25].lower()
    return any(
        banner.startswith(name if fence else name + "-config")
        for name in ("code-gauntlet", "deep-review")
    )


def _comment_text(line: str, inside: bool) -> tuple[str, bool]:
    parts = []
    cursor = 0
    while cursor < len(line):
        marker = "-->" if inside else "<!--"
        end = line.find(marker, cursor)
        if end < 0:
            if not inside:
                parts.append(line[cursor:])
            break
        if not inside:
            parts.append(line[cursor:end])
        inside = not inside
        cursor = end + len(marker)
    return "".join(parts), inside


def has_retired_settings(text: str) -> bool:
    # Each marker examines only its following whitespace and a bounded banner, never a growing suffix.
    for marker in _MARKERS.finditer(text):
        if _branded(text, marker.end(), marker[0] == "```"):
            return True
    delivery = ignore_list = inside_comment = False
    # File reads normalize CRLF and CR; Unicode separators must remain prose.
    for line in text.split("\n"):
        stripped = line.lstrip(" \t")
        match = _KEY.match(stripped)
        if match:
            value = stripped[match.end() :].strip()
            key = match[1].lower()
            if key == "ignore":
                if value.startswith("["):
                    return True
                ignore_list = not value
            elif key == "severity_threshold":
                if value.lower().startswith(("critical", "high", "medium", "low")):
                    return True
            else:
                number = re.match(r"[0-9]{1,3}(?![0-9])", value)
                if number and int(number[0]) <= 100:
                    return True
        elif stripped.strip():
            if ignore_list and stripped.startswith("-"):
                return True
            ignore_list = False
        content, inside_comment = _comment_text(line, inside_comment)
        if _HEADING.fullmatch(content):
            delivery = True
        elif delivery and content.strip():
            if _CSV.fullmatch(content.strip()):
                return True
            delivery = False
    return False
