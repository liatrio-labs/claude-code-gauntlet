from __future__ import annotations

import base64
import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from tests.tools.probe_cases import (
    build_quick_action_case_list,
)
from tests.tools.probe_fixtures import (
    read_utf8,
    write_utf8,
)

QUICK_ACTION_SENTINEL = "GITLAB_QUICK_ACTION_VERDICTS_JSON:"


QUICK_ACTION_CONTAINER = "cdr-gitlab"


QUICK_ACTION_GITLAB_VERSION = "19.4.1"


@dataclass
class QuickActionRecordResult:
    document: dict[str, Any]
    changed_ids: list[str] = field(default_factory=list)
    check_mode: bool = False

    @property
    def exit_code(self) -> int:
        return int(self.check_mode and bool(self.changed_ids))


def _quick_action_runner_script(cases: Iterable[Mapping[str, str]]) -> str:
    payload = [
        {"id": case["id"], "group": case["group"], "text": case["text"]}
        for case in cases
    ]
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    source_path = Path(__file__).with_name("gitlab_quick_action_verdicts.rb")
    source = read_utf8(str(source_path))
    if source.count("__CASES_B64__") != 1:
        raise ValueError(
            "GitLab quick-action runner template has an invalid case marker"
        )
    return source.replace("__CASES_B64__", encoded, 1)


def parse_quick_action_runner_output(stdout: str) -> dict[str, Any]:
    lines = [
        line for line in stdout.splitlines() if line.startswith(QUICK_ACTION_SENTINEL)
    ]
    if len(lines) != 1:
        raise ValueError(
            "GitLab quick-action runner output must contain exactly one sentinel result"
        )
    try:
        result = json.loads(lines[0][len(QUICK_ACTION_SENTINEL) :])
    except json.JSONDecodeError as error:
        raise ValueError("GitLab quick-action runner returned invalid JSON") from error
    if not isinstance(result, dict):
        raise ValueError("GitLab quick-action runner result is not an object")
    return result


def run_gitlab_quick_action_verdicts(
    cases: Iterable[Mapping[str, str]],
    *,
    container: str = QUICK_ACTION_CONTAINER,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    if not container or any(character.isspace() for character in container):
        raise ValueError("GitLab container must be a non-empty single argument")
    case_list = list(cases)
    case_ids = [case["id"] for case in case_list]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("GitLab quick-action case ids must be unique")
    command = ["docker", "exec", "-i", container, "gitlab-rails", "runner", "-"]
    invoke = subprocess.run if runner is None else runner
    completed = invoke(
        command,
        input=_quick_action_runner_script(case_list),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        raise RuntimeError(
            f"GitLab quick-action runner exited {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    return parse_quick_action_runner_output(completed.stdout)


def _validate_quick_action_paragraphs(
    text: str, paragraphs: object, case_id: str
) -> list[dict[str, int]]:
    if not isinstance(paragraphs, list):
        raise ValueError(f"invalid paragraphs for GitLab case {case_id!r}")
    line_count = len(text.split("\n"))
    validated: list[dict[str, int]] = []
    for paragraph in paragraphs:
        if (
            not isinstance(paragraph, dict)
            or set(paragraph) != {"start_line", "end_line"}
            or type(paragraph.get("start_line")) is not int
            or type(paragraph.get("end_line")) is not int
        ):
            raise ValueError(f"invalid paragraph interval for GitLab case {case_id!r}")
        start_line = paragraph["start_line"]
        end_line = paragraph["end_line"]
        if not 0 <= start_line <= end_line < line_count:
            raise ValueError(
                f"out-of-bounds paragraph interval for GitLab case {case_id!r}"
            )
        validated.append({"start_line": start_line, "end_line": end_line})
    return validated


def _validated_quick_action_rows(
    source_cases: list[dict[str, str]], result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    version = result.get("gitlab_version")
    if version != QUICK_ACTION_GITLAB_VERSION:
        raise ValueError(
            f"expected GitLab {QUICK_ACTION_GITLAB_VERSION}, got {version!r}"
        )
    raw_rows = result.get("cases")
    if not isinstance(raw_rows, list):
        raise ValueError("GitLab quick-action runner has no cases list")
    if len(raw_rows) != len(source_cases):
        raise ValueError("GitLab quick-action runner returned a different case count")
    rows: list[dict[str, Any]] = []
    for source, raw in zip(source_cases, raw_rows, strict=True):
        if not isinstance(raw, dict):
            raise ValueError("GitLab quick-action verdict is not an object")
        if raw.get("id") != source["id"] or raw.get("group") != source["group"]:
            raise ValueError(
                "GitLab quick-action runner changed case identity or group"
            )
        digest = raw.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid SHA-256 for GitLab case {source['id']!r}")
        if not isinstance(raw.get("paragraphs"), list):
            raise ValueError(f"invalid paragraphs for GitLab case {source['id']!r}")
        if not isinstance(raw.get("commands"), list):
            raise ValueError(f"invalid commands for GitLab case {source['id']!r}")
        paragraphs = _validate_quick_action_paragraphs(
            source["text"], raw["paragraphs"], source["id"]
        )
        stored_equals_posted = raw.get("stored_equals_posted")
        if type(stored_equals_posted) is not bool:
            raise ValueError(
                f"invalid stored-body comparison for GitLab case {source['id']!r}"
            )
        normalized_posted = source["text"].replace("\r", "").rstrip()
        has_content = "content" in raw
        content = raw.get("content", normalized_posted)
        if not isinstance(content, str):
            raise ValueError(
                f"invalid Extractor content for GitLab case {source['id']!r}"
            )
        if stored_equals_posted != (content == normalized_posted):
            raise ValueError(
                f"inconsistent Extractor content for GitLab case {source['id']!r}"
            )
        if has_content == (content == normalized_posted):
            raise ValueError(
                f"Extractor content must be recorded only when changed for GitLab case {source['id']!r}"
            )
        row = {
            "id": source["id"],
            "group": source["group"],
            "sha256": digest,
            "paragraphs": paragraphs,
            "commands": raw["commands"],
            "stored_equals_posted": stored_equals_posted,
        }
        if has_content:
            row["content"] = content
        rows.append(row)
    return rows


def serialize_quick_action_fixture(document: Mapping[str, Any]) -> str:
    rows = [
        "    " + json.dumps(case, ensure_ascii=False, separators=(",", ":"))
        for case in document["cases"]
    ]
    return (
        "{\n"
        f'  "gitlab_version": {json.dumps(document["gitlab_version"])},\n'
        f'  "recorded": {json.dumps(document["recorded"])},\n'
        '  "cases": [\n' + ",\n".join(rows) + "\n  ]\n}\n"
    )


def _quick_action_changed_ids(old: object, new: Mapping[str, Any]) -> list[str]:
    if not isinstance(old, dict) or not isinstance(old.get("cases"), list):
        return ["fixture"]
    changed: list[str] = []
    if old.get("gitlab_version") != new["gitlab_version"]:
        changed.append("gitlab_version")
    old_rows = [row for row in old["cases"] if isinstance(row, dict)]
    if len(old_rows) != len(old["cases"]):
        return [*changed, "fixture"]
    old_by_id = {str(row.get("id")): row for row in old_rows}
    new_rows = new["cases"]
    new_ids = [str(row["id"]) for row in new_rows]
    old_ids = [str(row.get("id")) for row in old_rows]
    for row in new_rows:
        case_id = str(row["id"])
        if old_by_id.get(case_id) != row:
            changed.append(case_id)
    changed.extend(case_id for case_id in old_ids if case_id not in set(new_ids))
    if old_ids != new_ids and not changed:
        changed.append("case-order")
    return changed


def record_quick_action_fixture(
    path: str,
    outbound_cases: Iterable[Mapping[str, Any]],
    *,
    container: str = QUICK_ACTION_CONTAINER,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    clock: Callable[[], str] = lambda: date.today().isoformat(),
    read_text: Callable[[str], str] | None = None,
    write_text: Callable[[str, str], None] | None = None,
    check: bool = False,
) -> QuickActionRecordResult:
    read_fixture = read_utf8 if read_text is None else read_text
    write_fixture = write_utf8 if write_text is None else write_text
    cases = build_quick_action_case_list(outbound_cases)
    result = run_gitlab_quick_action_verdicts(cases, container=container, runner=runner)
    document = {
        "gitlab_version": QUICK_ACTION_GITLAB_VERSION,
        "recorded": clock(),
        "cases": _validated_quick_action_rows(cases, result),
    }
    if check:
        try:
            old = json.loads(read_fixture(path))
        except FileNotFoundError:
            changed_ids = ["fixture"]
        except json.JSONDecodeError:
            changed_ids = ["fixture"]
        else:
            changed_ids = _quick_action_changed_ids(old, document)
        return QuickActionRecordResult(document, changed_ids, True)
    write_fixture(path, serialize_quick_action_fixture(document))
    return QuickActionRecordResult(document)
