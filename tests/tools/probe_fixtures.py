from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast

from tests.tools.probe_github import (
    github_login,
    run_github_canary,
)
from tests.tools.probe_gitlab import (
    derive_handles,
    run_canary,
)
from tests.tools.probe_html import (
    _reference_originals,
    _skeleton,
    check_render,
    pair_sha256,
    structure,
)


@dataclass
class RecordResult:
    cases: list[dict[str, Any]]
    changed_ids: list[str] = field(default_factory=list)
    divergence_cleared: list[str] = field(default_factory=list)
    cleared_details: list[str] = field(default_factory=list)
    version_mismatch: tuple[str, str] | None = None
    check_mode: bool = False

    @property
    def exit_code(self) -> int:
        return int(
            self.check_mode
            and bool(
                self.changed_ids or self.divergence_cleared or self.version_mismatch
            )
        )


def input_text(case: Mapping[str, Any]) -> str:
    return f"{case['expected']}\n\nfooter line"


def input_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def update_cases(
    cases: list[dict[str, Any]],
    *,
    platform: str,
    render: Callable[[str], str],
    rendered: str,
    version: str | None = None,
    blob_prefix: str | None = None,
    check: bool = False,
) -> RecordResult:
    if platform not in {"github", "gitlab"}:
        raise ValueError(f"unknown render platform: {platform}")
    updated = copy.deepcopy(cases)
    changed: list[str] = []
    cleared: list[str] = []
    cleared_details: list[str] = []
    version_mismatch = None
    for case in updated:
        text = input_text(case)
        digest = input_sha256(text)
        expected_html = render(text)
        check_render(platform, expected_html)
        if platform == "github":
            probe: dict[str, Any] = {
                "renderer": "gh api markdown mode=gfm",
                "rendered": rendered,
                "input": "expected + blank line + footer line",
                "input_sha256": digest,
                "html": expected_html,
            }
            old = case.get("github_probe")
            if check and _probe_changed(
                old, probe, ("renderer", "input", "input_sha256", "html")
            ):
                changed.append(str(case["id"]))
            if not check:
                case["github_probe"] = probe
        else:
            if version is None or blob_prefix is None:
                raise ValueError("GitLab recording requires version and blob_prefix")
            old = case.get("gitlab_probe")
            old_divergence = old.get("divergence") if isinstance(old, dict) else None
            twin_html = render(text.replace("\uff20", "@"))
            probe = {
                "renderer": "gitlab api markdown gfm=true project",
                "version": version,
                "rendered": rendered,
                "input": "expected + blank line + footer line",
                "input_sha256": digest,
                "blob_prefix": blob_prefix,
                "twin_references": _reference_originals(twin_html),
                "html": expected_html,
                "divergence": old_divergence,
            }
            if check:
                compared = (
                    "renderer",
                    "input",
                    "input_sha256",
                    "blob_prefix",
                    "twin_references",
                    "html",
                )
                if _probe_changed(old, probe, compared):
                    changed.append(str(case["id"]))
                if isinstance(old, dict) and old.get("version") != version:
                    version_mismatch = (str(old.get("version", "missing")), version)
            else:
                _insert_after(case, "github_probe", "gitlab_probe", probe)
        github_probe = case.get("github_probe")
        gitlab_probe = case.get("gitlab_probe")
        divergence = (
            gitlab_probe.get("divergence") if isinstance(gitlab_probe, dict) else None
        )
        if divergence is not None:
            if isinstance(github_probe, dict) and isinstance(gitlab_probe, dict):
                gh_html = (
                    probe["html"]
                    if platform == "github"
                    else github_probe.get("html", "")
                )
                gl_html = (
                    probe["html"]
                    if platform == "gitlab"
                    else gitlab_probe.get("html", "")
                )
                prefix = (
                    blob_prefix
                    if platform == "gitlab"
                    else gitlab_probe.get("blob_prefix")
                )
                gh_structure = structure(gh_html, platform="github")
                gl_structure = structure(gl_html, platform="gitlab", blob_prefix=prefix)
                valid = (
                    isinstance(divergence, dict)
                    and isinstance(divergence.get("note"), str)
                    and type(divergence.get("issue")) is int
                    and divergence.get("pair_sha256")
                    == pair_sha256(gh_html, gl_html, blob_prefix=prefix)
                    and gh_structure != gl_structure
                    and _skeleton(gh_html, platform="github")
                    == _skeleton(gl_html, platform="gitlab", blob_prefix=prefix)
                )
            else:
                valid = False
            if not valid:
                if check:
                    cleared.append(str(case["id"]))
                elif isinstance(gitlab_probe, dict):
                    gitlab_probe["divergence"] = None
                    cleared.append(str(case["id"]))
                    if isinstance(divergence, dict):
                        cleared_details.append(
                            f"{case['id']}: issue {divergence.get('issue')}: {divergence.get('note')}"
                        )
    return RecordResult(
        updated, changed, cleared, cleared_details, version_mismatch, check
    )


def _probe_changed(
    old: object, new: Mapping[str, Any], fields: tuple[str, ...]
) -> bool:
    return not isinstance(old, dict) or any(
        old.get(field) != new.get(field) for field in fields
    )


def _insert_after(case: dict[str, Any], anchor: str, key: str, value: Any) -> None:
    reordered: dict[str, Any] = {}
    for current_key, current_value in case.items():
        if current_key != key:
            reordered[current_key] = current_value
        if current_key == anchor:
            reordered[key] = value
    if key not in reordered:
        reordered[key] = value
    case.clear()
    case.update(reordered)


def serialize_fixture(cases: Iterable[Mapping[str, Any]]) -> str:
    rows = [
        "    " + json.dumps(case, ensure_ascii=False, separators=(",", ":"))
        for case in cases
    ]
    return '{\n  "cases": [\n' + ",\n".join(rows) + "\n  ]\n}\n"


def read_utf8(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as stream:
        return stream.read()


def write_utf8(path: str, contents: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(contents)


def record_fixture(
    path: str,
    *,
    platform: str,
    render: Callable[[str], str],
    clock: Callable[[], str] = lambda: date.today().isoformat(),
    read_text: Callable[[str], str] = read_utf8,
    write_text: Callable[[str, str], None] = write_utf8,
    version: str | None = None,
    blob_prefix: str | None = None,
    check: bool = False,
    get_github_login: Callable[[], str] = github_login,
) -> RecordResult:
    document = _load_document(read_text(path))
    cases = document["cases"]
    if platform == "gitlab":
        run_canary(derive_handles(cases), render)
    elif platform == "github":
        run_github_canary(get_github_login(), render)
    result = update_cases(
        cases,
        platform=platform,
        render=render,
        rendered=clock(),
        version=version,
        blob_prefix=blob_prefix,
        check=check,
    )
    if not check:
        write_text(path, serialize_fixture(result.cases))
    return result


def _load_document(contents: str) -> dict[str, Any]:
    document = json.loads(contents)
    if (
        not isinstance(document, dict)
        or list(document) != ["cases"]
        or not isinstance(document["cases"], list)
        or not all(isinstance(case, dict) for case in document["cases"])
    ):
        raise ValueError("fixture must contain only a cases array")
    return document


def record_divergence(
    path: str,
    *,
    case_id: str,
    issue: int,
    note: str,
    read_text: Callable[[str], str] = read_utf8,
    write_text: Callable[[str, str], None] = write_utf8,
) -> None:
    document = _load_document(read_text(path))
    if issue < 1 or not note.strip():
        raise ValueError("divergence needs a positive issue and nonempty note")
    matches = [case for case in document["cases"] if case.get("id") == case_id]
    if len(matches) != 1:
        raise ValueError(f"expected one fixture row for {case_id}")
    case = matches[0]
    github = case["github_probe"]
    gitlab = case["gitlab_probe"]
    digest = input_sha256(input_text(case))
    if any(probe["input_sha256"] != digest for probe in (github, gitlab)):
        raise ValueError(f"stale probe input for {case_id}")
    gh_html, gl_html = github["html"], gitlab["html"]
    prefix = gitlab["blob_prefix"]
    if structure(gh_html, platform="github") == structure(
        gl_html, platform="gitlab", blob_prefix=prefix
    ):
        raise ValueError(f"equal structures for {case_id}")
    if _skeleton(gh_html, platform="github") != _skeleton(
        gl_html, platform="gitlab", blob_prefix=prefix
    ):
        raise ValueError(f"different element skeletons for {case_id}")
    gitlab["divergence"] = {
        "note": note,
        "issue": issue,
        "pair_sha256": pair_sha256(gh_html, gl_html, blob_prefix=prefix),
    }
    write_text(path, serialize_fixture(document["cases"]))


def _fixture_path() -> str:
    return str(
        Path(__file__).resolve().parents[1] / "fixtures" / "outbound_comment_cases.json"
    )


def _quick_action_fixture_path() -> str:
    return str(
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "gitlab_quick_action_verdicts_19_4_1.json"
    )


def _read_cases(path: str) -> list[dict[str, Any]]:
    document = _load_document(read_utf8(path))
    return cast(list[dict[str, Any]], document["cases"])
