"""Prior-review detection, receipt degradation, and marker containment."""

import contextlib
import io
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, NoReturn, TypedDict, cast
from unittest.mock import patch

import gauntlet.marker as review_marker
import gauntlet.prior_review as detect_prior_review
import pytest
from gauntlet import proc
from gauntlet.forge import JsonFetch, Platform, ReviewTarget
from gauntlet.prior_review import PriorDelivery

from tests.support.forge import FakeForge, FakeForgeFactory, FakeGitLab, ForgeCall

FULL_SHA = "a" * 40
HEAD_SHA = "b" * 40
SHORT_SHA = FULL_SHA[:8]
# Repeated hex characters avoid false credential matches in the secret scanner.
FINDING_KEY = "c" * 16
OTHER_FINDING_KEY = "d" * 16


class _GitOptions(TypedDict, total=False):
    resolvable: bool
    full_sha: str
    head_sha: str
    commit_count: int
    ancestor: bool


def _run_main(argv: list[str]) -> tuple[str, int]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = detect_prior_review.CLI.invoke(argv)
    return stdout.getvalue(), code


def _result(out: str = "", err: str = "", rc: int = 0) -> proc.CompletedProcess[str]:
    return proc.CompletedProcess([], rc, stdout=out, stderr=err)


def _legacy_group_body(
    primary_key: str, corroborator_titles: Sequence[str], sha: str = FULL_SHA
) -> str:
    sections = "\n\n".join(
        "**Corroborating finding — bug-detector (correctness, confidence 70):**\n"
        + title
        for title in corroborator_titles
    )
    return sections + "\n" + review_marker.build_finding_marker(sha, primary_key)


@pytest.mark.parametrize("forge_factory", [detect_prior_review], indirect=True)
@pytest.mark.parametrize(
    "platform, bodies, error, expected_sha",
    [
        pytest.param(
            "github",
            [review_marker.build_footer(3, FULL_SHA)],
            None,
            FULL_SHA,
            id="github-review",
        ),
        pytest.param(
            "gitlab",
            [review_marker.build_marker(FULL_SHA, 1)],
            None,
            FULL_SHA,
            id="gitlab-note",
        ),
        pytest.param(
            "github",
            [],
            "github reviews: fetch failed (exit 1): gh: fetch failed",
            None,
            id="github-failure",
        ),
        pytest.param(
            "github",
            [
                review_marker.build_marker("c" * 40, 1),
                review_marker.build_marker("d" * 40, 2),
            ],
            None,
            "d" * 40,
            id="newest-entry",
        ),
    ],
)
def test_network_scan(
    platform: Platform,
    bodies: list[str],
    error: str | None,
    expected_sha: str | None,
    forge_factory: FakeForgeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp_key = "submitted_at" if platform == "github" else "created_at"
    payload = [
        {"id": index, "body": body, timestamp_key: f"2026-0{index + 1}-01T00:00:00Z"}
        for index, body in enumerate(bodies)
    ]
    fake = forge_factory.configure(
        FakeForge(platform, entries=[JsonFetch(payload, error)])
    )
    monkeypatch.setattr(proc, "run", _fake_git_run())
    out, code = _run_main(
        ["--platform", platform, "--owner", "o", "--repo", "r", "--number", "5"]
    )
    result: dict[str, object] = json.loads(out)
    source = "review" if platform == "github" else "note"
    assert code == 0
    assert result["previously_reviewed"] is (expected_sha is not None)
    assert result["last_reviewed_sha"] == expected_sha
    assert result["signal"] == ("marker" if expected_sha else None)
    assert result["source"] == (source if expected_sha else None)
    assert result["scanned"] == {source: len(bodies)}
    assert result["errors"] == ([error] if error else [])
    assert result["incremental_safe"] is (expected_sha is not None)
    assert forge_factory.calls == [platform]
    assert fake.calls == [ForgeCall("review_entries", ReviewTarget("o", "r", "5"))]


@pytest.mark.parametrize(
    "notes, error, expected",
    [
        pytest.param(
            [],
            "gitlab notes: fetch failed (exit 1): boom",
            PriorDelivery(
                False,
                frozenset(),
                frozenset(),
                "gitlab notes: fetch failed (exit 1): boom",
            ),
            id="fetch-error",
        ),
        pytest.param(
            [
                {"body": review_marker.build_footer(2, FULL_SHA)},
                {"body": review_marker.build_finding_marker(FULL_SHA, "c" * 16)},
                {"body": review_marker.build_finding_marker(FULL_SHA, "d" * 16)},
            ],
            None,
            PriorDelivery(True, frozenset({"c" * 16, "d" * 16}), frozenset(), None),
            id="summary-and-keys",
        ),
        pytest.param(
            [
                {
                    "id": "8f4a",
                    "individual_note": False,
                    "notes": [
                        {"body": review_marker.build_finding_marker(FULL_SHA, "c" * 16)}
                    ],
                }
            ],
            None,
            PriorDelivery(False, frozenset(), frozenset(), None),
            id="nested-discussions",
        ),
    ],
)
def test_prior_state(
    notes: list[dict[str, object]],
    error: str | None,
    expected: PriorDelivery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed: list[str] = []
    real = review_marker.find_finding_markers

    def parse(body: str) -> list[review_marker.FindingMarkerWire]:
        parsed.append(body)
        return real(body)

    monkeypatch.setattr(detect_prior_review, "find_finding_markers", parse)
    fake = FakeGitLab(entries=[JsonFetch(notes, error)])
    assert (
        detect_prior_review.gitlab_prior_delivery_state(
            "o", "r", 5, FULL_SHA, forge=fake
        )
        == expected
    )
    assert fake.calls == [ForgeCall("review_entries", ReviewTarget("o", "r", 5))]
    assert parsed == (
        []
        if error
        else [
            note["body"]
            for note in notes
            if isinstance(note, dict) and isinstance(note.get("body"), str)
        ]
    )


@pytest.mark.parametrize("forge_factory", [detect_prior_review], indirect=True)
@pytest.mark.parametrize(
    "outcome",
    [
        "missing-git",
        "timeout",
        "nonutf8-slug",
    ],
)
def test_detector_origin_receipt(
    outcome: str,
    forge_factory: FakeForgeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_run = proc.run
    fake = forge_factory.configure(FakeForge())
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        errors: Literal["strict", "replace"] = "strict",
    ) -> proc.CompletedProcess[str]:
        kwargs: dict[str, object] = {"cwd": cwd, "timeout": timeout, "errors": errors}
        if command[1:4] != ["remote", "get-url", "origin"]:
            return _fake_git_run()(command, **kwargs)
        calls.append((command, kwargs))
        if outcome == "missing-git":
            raise FileNotFoundError("missing git")
        if outcome == "timeout":
            raise proc.TimeoutExpired(command, timeout or 0.0)
        assert outcome == "nonutf8-slug"
        child = (
            r"import sys; sys.stdout.buffer.write("
            r"b'https://github.com/o/r\xff.git\n')"
        )
        return real_run(
            [sys.executable, "-c", child],
            cwd=cwd,
            timeout=timeout,
            errors=errors,
        )

    monkeypatch.setattr(proc, "run", run)
    out, code = _run_main(["--platform", "github", "--number", "5"])
    result: dict[str, object] = json.loads(out)
    assert code == 0
    assert result["previously_reviewed"] is False
    assert forge_factory.calls == ["github"]
    assert calls == [
        (
            ["git", "remote", "get-url", "origin"],
            {"cwd": None, "timeout": 10, "errors": "replace"},
        )
    ]
    if outcome == "nonutf8-slug":
        assert result["errors"] == []
        assert result["scanned"] == {"review": 0}
        assert fake.calls == [
            ForgeCall("review_entries", ReviewTarget("o", "r\ufffd", "5"))
        ]
    else:
        receipt_errors = cast("list[str]", result["errors"])
        assert receipt_errors[0].startswith("could not determine owner/repo:")
        assert fake.calls == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            proc.TimeoutExpired(["git"], 3),
            ("", "timed out after 3s", -1),
        ),
        (OSError("denied"), ("", "denied", -1)),
    ],
)
def test_run_maps_process_failures_to_exact_sentinels(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: tuple[str, str, int],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise failure

    monkeypatch.setattr(proc, "output", fail)
    assert detect_prior_review.run(["git", "status"], timeout=3) == expected


def _fake_git_run(
    resolvable: bool = True,
    full_sha: str = FULL_SHA,
    head_sha: str = HEAD_SHA,
    commit_count: int = 3,
    ancestor: bool = True,
) -> Callable[..., proc.CompletedProcess[str]]:
    def run(
        cmd: Sequence[str], *_args: object, **_kwargs: object
    ) -> proc.CompletedProcess[str]:
        if "cat-file" in cmd:
            return _result(
                rc=0 if resolvable else 128,
                err="" if resolvable else f"fatal: Not a valid object name {cmd[-1]}",
            )
        if "merge-base" in cmd:
            return _result(rc=0 if ancestor else 1)
        if "rev-parse" in cmd and cmd[-1] == "HEAD":
            return _result(out=head_sha + "\n")
        if "rev-parse" in cmd:
            # Preserve full revisions so an explicit head override stays observable.
            rev = cmd[-1]
            if len(rev) == 40:
                return _result(out=rev + "\n")
            if resolvable:
                return _result(out=full_sha + "\n")
            return _result(
                err=f"fatal: ambiguous argument '{rev}': unknown revision or "
                "path not in the working tree.",
                rc=128,
            )
        if "rev-list" in cmd:
            return _result(out=f"{commit_count}\n")
        raise AssertionError(f"Unexpected Git call: {cmd}")

    return run


@pytest.mark.parametrize(
    "collector, timestamp_key, source",
    [
        (detect_prior_review.collect_entries_github, "submitted_at", "review"),
        (detect_prior_review.collect_entries_gitlab, "created_at", "note"),
        (detect_prior_review.collect_entries_file, "timestamp", "bodies_file"),
    ],
)
def test_collect_entries(collector, timestamp_key, source):
    timestamp = "2026-01-01T00:00:00Z"
    payload = [{"id": 1, "body": "review body", timestamp_key: timestamp}, {"id": 2}]
    assert collector(payload) == [
        {"id": 1, "body": "review body", "timestamp": timestamp, "source": source}
    ]


def _finding_note(keys: tuple[str, ...], sha: str = FULL_SHA) -> dict[str, str]:
    return {
        "body": "Detail.\n\n"
        + "\n".join(review_marker.build_finding_marker(sha, key) for key in keys)
    }


# Exact SHA equality prevents another revision from suppressing delivery.
@pytest.mark.parametrize(
    "entries, sha, expected",
    [
        pytest.param(
            [
                {
                    "body": _legacy_group_body("c" * 16, ["Corroborator A"])
                    + "\n"
                    + review_marker.build_finding_marker(FULL_SHA, "c" * 16)
                }
            ],
            FULL_SHA,
            PriorDelivery(False, frozenset({"c" * 16}), frozenset(), None),
            id="duplicate-markers-count-as-members",
        ),
        pytest.param(
            [{"body": review_marker.build_footer(1, FULL_SHA), "timestamp": None}],
            FULL_SHA[:7],
            PriorDelivery(False, frozenset(), frozenset(), None),
            id="long-marker-short-sha",
        ),
        pytest.param(
            [_finding_note((FINDING_KEY,))],
            FULL_SHA[:7],
            PriorDelivery(False, frozenset(), frozenset(), None),
            id="finding-key-sha-prefix",
        ),
        pytest.param(
            [{"body": _legacy_group_body(FINDING_KEY, ["Corroborator A"])}],
            FULL_SHA,
            PriorDelivery(
                False,
                frozenset({"cccccccccccccccc"}),
                frozenset({"cccccccccccccccc"}),
                None,
            ),
            id="undermarked-group",
        ),
        pytest.param(
            [
                {
                    "body": _legacy_group_body(FINDING_KEY, ["Corroborator A"])
                    + "\n"
                    + review_marker.build_finding_marker(FULL_SHA, OTHER_FINDING_KEY)
                }
            ],
            FULL_SHA,
            PriorDelivery(
                False,
                frozenset({"cccccccccccccccc", "dddddddddddddddd"}),
                frozenset(),
                None,
            ),
            id="fully-marked-group",
        ),
        pytest.param(
            [{"body": _finding_note((FINDING_KEY,))["body"]}],
            FULL_SHA,
            PriorDelivery(False, frozenset({"cccccccccccccccc"}), frozenset(), None),
            id="individual-primary",
        ),
    ],
)
def test_prior_delivery(
    entries: list[object], sha: str, expected: PriorDelivery
) -> None:
    assert detect_prior_review.prior_delivery_from_entries(entries, sha) == expected


def _bodies_file(tmp_path: Path, entries: Sequence[object]) -> Path:
    path = tmp_path / "bodies.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _base_argv(bodies_path: Path, platform: str = "github", **extra: str) -> list[str]:
    argv = [
        "--platform",
        platform,
        "--owner",
        "o",
        "--repo",
        "r",
        "--number",
        "5",
        "--bodies-file",
        str(bodies_path),
    ]
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", value]
    return argv


@pytest.mark.parametrize(
    "marker_sha, git_kwargs, expected",
    [
        pytest.param(
            FULL_SHA,
            {"resolvable": True},
            {
                "previously_reviewed": True,
                "signal": "marker",
                "source": "review",
                "last_reviewed_sha": FULL_SHA,
                "sha_resolvable": True,
                "sha_is_ancestor": True,
                "head_sha": HEAD_SHA,
                "head_advanced": True,
                "new_commit_count": 3,
                "incremental_safe": True,
                "errors": [],
            },
            id="found-and-advanced",
        ),
        pytest.param(
            HEAD_SHA,
            {
                "resolvable": True,
                "full_sha": HEAD_SHA,
                "head_sha": HEAD_SHA,
                "commit_count": 0,
            },
            {
                "previously_reviewed": True,
                "head_advanced": False,
                "incremental_safe": False,
            },
            id="found-and-not-advanced",
        ),
        pytest.param(
            FULL_SHA,
            {"resolvable": False},
            {
                "previously_reviewed": True,
                "sha_resolvable": False,
                "incremental_safe": False,
            },
            id="found-but-sha-unresolvable",
        ),
        pytest.param(
            FULL_SHA,
            {"resolvable": True, "ancestor": False, "commit_count": 0},
            {
                "previously_reviewed": True,
                "sha_resolvable": True,
                "sha_is_ancestor": False,
                "head_advanced": False,
                "incremental_safe": False,
            },
            id="found-but-not-ancestor",
        ),
        pytest.param(
            None,
            {},
            {
                "previously_reviewed": False,
                "signal": None,
                "source": None,
                "marker": None,
                "last_reviewed_sha": None,
                "incremental_safe": False,
            },
            id="not-found",
        ),
    ],
)
def test_cli_bodies_file_receipt_outcomes(
    tmp_path: Path,
    marker_sha: str | None,
    git_kwargs: _GitOptions,
    expected: dict[str, object],
) -> None:
    body = (
        review_marker.build_marker(marker_sha, 4)
        if marker_sha
        else "just a plain unrelated comment"
    )
    entries = [{"body": body, "source": "review"}]
    path = _bodies_file(tmp_path, entries)
    with patch.object(proc, "run", side_effect=_fake_git_run(**git_kwargs)):
        out, code = _run_main(_base_argv(path))

    assert code == 0
    result: dict[str, object] = json.loads(out.strip())
    assert set(result) == RECEIPT_KEYS
    assert {key: result[key] for key in expected} == expected
    assert result["last_reviewed_sha_short"] == (marker_sha[:8] if marker_sha else None)
    assert result["legacy"] is False
    if marker_sha:
        assert result["marker"] == {
            "version": "3.0",
            "findings_count": 4,
            "sha": marker_sha,
            "_token": "code-gauntlet-findings",
            "_legacy": False,
        }


def test_cli_bodies_file_head_sha_override_is_used(tmp_path: Path) -> None:
    override_head = "d" * 40
    path = _bodies_file(
        tmp_path,
        [{"body": review_marker.build_marker(FULL_SHA, 1), "source": "review"}],
    )
    with patch.object(proc, "run", side_effect=_fake_git_run(resolvable=True)):
        out, code = _run_main(_base_argv(path, head_sha=override_head))
    assert code == 0
    result: dict[str, object] = json.loads(out.strip())
    assert result["head_sha"] == override_head


MARKER_BASE = {
    "version": "3.0",
    "findings_count": 2,
    "sha": FULL_SHA,
    "_token": "code-gauntlet-findings",
    "_legacy": False,
}


@pytest.mark.parametrize(
    "marker, expected, forbidden",
    [
        pytest.param(MARKER_BASE, MARKER_BASE, (), id="normal"),
        pytest.param(
            {
                "version": "3.0",
                "sha": FULL_SHA,
                "attacker_field": "SECRET_PAYLOAD_VALUE_SHOULD_NOT_APPEAR",
            },
            {"version": "3.0", "sha": FULL_SHA, "unknown_keys": ["attacker_field"]},
            ("SECRET_PAYLOAD_VALUE_SHOULD_NOT_APPEAR",),
            id="unknown-values-refused",
        ),
        pytest.param(
            {**MARKER_BASE, "version": "INJECT " * 9000},
            {**MARKER_BASE, "version": ("INJECT " * 74)[:512] + "...[truncated]"},
            (),
            id="clipped-string",
        ),
        pytest.param(
            {
                **MARKER_BASE,
                "version": "\x00" * 512,
                "findings_count": "\x00" * 512,
                **{f"u{index:02d}" + "x" * 61: True for index in range(32)},
            },
            {
                **MARKER_BASE,
                "version": "\x00" * 64 + "...[truncated]",
                "findings_count": None,
                "truncated": True,
            },
            (),
            id="whole-payload",
        ),
        pytest.param(None, None, (), id="footer-without-marker"),
        pytest.param(
            {"version": "3.0", "sha": FULL_SHA, "X" * 3000: 1, "Y" * 3000: 2},
            {
                "version": "3.0",
                "sha": FULL_SHA,
                "unknown_keys": ["X" * 64 + "...", "Y" * 64 + "..."],
            },
            (),
            id="long-names",
        ),
        pytest.param(
            {"version": "3.0", "findings_count": int("9" * 4200), "sha": FULL_SHA},
            {
                "version": "3.0",
                "findings_count": "[number truncated, 4200 digits]",
                "sha": FULL_SHA,
            },
            (),
            id="big-number",
        ),
    ],
)
def test_sanitize_marker(
    marker: object, expected: dict[str, object] | None, forbidden: tuple[str, ...]
) -> None:
    actual = detect_prior_review.sanitize_marker(marker)
    assert actual == expected
    encoded = json.dumps(actual)
    assert len(encoded) <= 4096
    assert all(value not in encoded for value in forbidden)


@pytest.mark.parametrize(
    "reviewed_sha, head_sha, default_head_failure, expected_head, expected_errors",
    [
        pytest.param(
            FULL_SHA, "deadbeef", False, "deadbeef", ("head-sha",), id="explicit-head"
        ),
        pytest.param(
            None,
            None,
            True,
            "unknown",
            ("could not resolve the head commit",),
            id="default-head",
        ),
        pytest.param(
            FULL_SHA,
            HEAD_SHA,
            False,
            HEAD_SHA,
            ("is not present in this clone", SHORT_SHA),
            id="missing-reviewed",
        ),
    ],
)
def test_git_fact_errors(
    reviewed_sha: str | None,
    head_sha: str | None,
    default_head_failure: bool,
    expected_head: str,
    expected_errors: tuple[str, ...],
) -> None:
    inner = _fake_git_run(resolvable=False)

    def run(
        cmd: Sequence[str], *_args: object, **_kwargs: object
    ) -> proc.CompletedProcess[str]:
        if default_head_failure:
            if cmd[:2] == ["git", "rev-parse"]:
                return _result(err="fatal: not a git repository", rc=128)
            return _result()
        return inner(cmd, *_args, **_kwargs)

    errors: list[str] = []
    with patch.object(proc, "run", side_effect=run):
        facts = detect_prior_review.resolve_git_facts(reviewed_sha, head_sha, errors)
    assert facts.head_sha == expected_head
    assert not facts.sha_resolvable
    assert all(any(text in error for error in errors) for text in expected_errors)


RECEIPT_KEYS = {
    "previously_reviewed",
    "signal",
    "source",
    "legacy",
    "last_reviewed_sha",
    "last_reviewed_sha_short",
    "sha_resolvable",
    "sha_is_ancestor",
    "head_sha",
    "head_advanced",
    "new_commit_count",
    "incremental_safe",
    "marker",
    "scanned",
    "errors",
}


def test_cli_legacy_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = "<!-- deep-review-findings: " + json.dumps({"sha": SHORT_SHA}) + " -->"
    path = _bodies_file(tmp_path, [{"body": body, "source": "note"}])
    monkeypatch.setattr(proc, "run", _fake_git_run())
    out, code = _run_main(_base_argv(path))
    result: dict[str, object] = json.loads(out)
    assert code == 0
    assert set(result) == RECEIPT_KEYS
    assert result["legacy"] is True
    assert result["source"] == "note"
    assert result["last_reviewed_sha"] == FULL_SHA
    assert result["last_reviewed_sha_short"] == SHORT_SHA
    assert result["marker"] == {
        "sha": SHORT_SHA,
        "_token": "deep-review-findings",
        "_legacy": True,
    }


def test_build_result_default() -> None:
    result = detect_prior_review.build_result(None, None)
    assert set(result) == RECEIPT_KEYS
    assert result["head_sha"] is None
    assert result["previously_reviewed"] is False
    assert result["incremental_safe"] is False
