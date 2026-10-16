"""Prior-review detection, receipt degradation, and marker containment."""

import contextlib
import io
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, TypedDict, cast
from unittest.mock import patch

import gauntlet.marker as review_marker
import gauntlet.prior_review as detect_prior_review
import pytest
from gauntlet import proc
from gauntlet.forge import JsonFetch, Platform, Poster, ReviewTarget
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
@pytest.mark.parametrize("pr_url", [None, "enterprise"])
def test_network_scan(
    pr_url,
    platform: Platform,
    bodies: list[str],
    error: str | None,
    expected_sha: str | None,
    forge_factory: FakeForgeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp_key = "submitted_at" if platform == "github" else "created_at"
    payload = [
        {
            "id": index,
            "body": body,
            timestamp_key: f"2026-0{index + 1}-01T00:00:00Z",
            "user": {"login": "poster"},
            "author": {"username": "poster"},
        }
        for index, body in enumerate(bodies)
    ]
    fake = forge_factory.configure(
        FakeForge(platform, entries=[JsonFetch(payload, error)])
    )
    monkeypatch.setattr(proc, "run", _fake_git_run())
    target_flags = ["--owner", "o", "--repo", "r", "--number", "5"]
    owner = "o"
    if pr_url:
        owner = "group/sub" if platform == "gitlab" else "o"
        surface = "-/merge_requests" if platform == "gitlab" else "pull"
        target_flags = [
            "--pr-url",
            f"https://Git.Example.COM:8443/{owner}/r/{surface}/5",
        ]
    out, code = _run_main(["--platform", platform, *target_flags])
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
    assert forge_factory.calls == [(platform, "git.example.com" if pr_url else None)]
    assert fake.calls == [
        ForgeCall("review_entries", ReviewTarget(owner, "r", 5 if pr_url else "5")),
        ForgeCall("poster"),
    ]


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
    notes = [{**note, "author": {"username": "poster"}} for note in notes]
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
    assert fake.calls == [
        ForgeCall("review_entries", ReviewTarget("o", "r", 5)),
        ForgeCall("poster"),
    ]
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
    assert forge_factory.calls == [("github", None)]
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
            ForgeCall("review_entries", ReviewTarget("o", "r\ufffd", "5")),
            ForgeCall("poster"),
        ]
    else:
        receipt_errors = cast("list[str]", result["errors"])
        assert receipt_errors[0].startswith("could not determine owner/repo:")
        assert fake.calls == []


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
                "head_sha": "e" * 40,
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
            "footer",
            {},
            {"previously_reviewed": True, "signal": "footer", "marker": None},
            id="legacy-prose-footer",
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
    footer = marker_sha == "footer"
    if footer:
        marker_sha = FULL_SHA
    body = (
        review_marker.build_prose_footer(marker_sha)
        if footer
        else review_marker.build_marker(marker_sha, 4)
        if marker_sha
        else "just a plain unrelated comment"
    )
    entries = [{"body": body, "source": "review"}]
    path = _bodies_file(tmp_path, entries)
    with patch.object(proc, "run", side_effect=_fake_git_run(**git_kwargs)):
        extra = (
            {"head_sha": str(expected["head_sha"])} if "head_sha" in expected else {}
        )
        out, code = _run_main(_base_argv(path, **extra))

    assert code == 0
    result: dict[str, object] = json.loads(out.strip())
    assert set(result) == RECEIPT_KEYS
    assert {key: result[key] for key in expected} == expected
    assert result["last_reviewed_sha_short"] == (marker_sha[:8] if marker_sha else None)
    assert result["legacy"] is False
    if marker_sha and not footer:
        assert result["marker"] == {
            "version": "3.0",
            "findings_count": 4,
            "sha": marker_sha,
            "_token": "code-gauntlet-findings",
            "_legacy": False,
        }


@pytest.mark.parametrize("overflow", [False, True])
def test_cli_bounds_hostile_marker_echo(
    overflow: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = {
        "sha": FULL_SHA,
        "version": "\x00" * 512 if overflow else "INJECT " * 9000,
        "findings_count": "\x00" * 512 if overflow else int("9" * 4200),
        "X" * 3000: "SECRET_PAYLOAD_VALUE_SHOULD_NOT_APPEAR",
        **{f"u{index:02d}" + "x" * 61: True for index in range(32)},
    }
    body = f"<!-- code-gauntlet-findings: {json.dumps(marker)} -->"
    path = _bodies_file(tmp_path, [{"body": body}])
    monkeypatch.setattr(proc, "run", _fake_git_run())
    out, code = _run_main(_base_argv(path))
    result: dict[str, object] = json.loads(out)
    actual = result["marker"]
    assert code == 0 and isinstance(actual, dict)
    assert actual["sha"] == FULL_SHA
    assert actual["_token"] == "code-gauntlet-findings"
    assert actual["_legacy"] is False
    if overflow:
        assert actual["version"] == "\x00" * 64 + "...[truncated]"
        assert actual["findings_count"] is None
        assert actual["truncated"] is True
    else:
        assert actual["version"] == ("INJECT " * 74)[:512] + "...[truncated]"
        assert actual["findings_count"] == "[number truncated, 4200 digits]"
        assert actual["unknown_keys"][0] == "X" * 64 + "..."
        assert len(actual["unknown_keys"]) == 32
    encoded = json.dumps(actual)
    assert len(encoded) <= 4096
    assert "SECRET_PAYLOAD_VALUE_SHOULD_NOT_APPEAR" not in encoded


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
    "ignored_foreign",
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


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize(
    "case", ["own", "foreign", "missing", "null", "system", "case", "identity-failure"]
)
def test_network_identity_boundary(
    platform: Platform, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    author: object = {"login": "poster", "username": "poster", "type": "Bot"}
    if case == "foreign":
        author = {"login": "other[bot]", "username": "other", "type": "Bot"}
    elif case == "null":
        author = None
    elif case == "case":
        author = {"login": "POSTER", "username": "POSTER"}
    entry: dict[str, object] = {
        "body": review_marker.build_marker(FULL_SHA, 1),
        "system": case == "system",
    }
    if case != "missing":
        entry["user" if platform == "github" else "author"] = author
    failure = "identity denied; set CODE_GAUNTLET_POSTER"
    fake = FakeForge(
        platform,
        entries=[JsonFetch([entry], None)],
        posters=[
            Poster(None, failure)
            if case == "identity-failure"
            else Poster("poster", None)
        ],
    )
    monkeypatch.setattr(detect_prior_review, "make_forge", lambda _, host=None: fake)
    monkeypatch.setattr(proc, "run", _fake_git_run())
    out, code = _run_main(
        ["--platform", platform, "--owner", "o", "--repo", "r", "--number", "5"]
    )
    result: dict[str, object] = json.loads(out)
    accepted = case == "own" or (platform == "github" and case in {"case", "system"})
    assert code == 0
    assert result["previously_reviewed"] is accepted
    assert result["incremental_safe"] is accepted
    assert result["ignored_foreign"] == (0 if accepted else 1)
    assert result["scanned"] == {
        "review" if platform == "github" else "note": 1 if accepted else 0
    }
    assert result["errors"] == ([failure] if case == "identity-failure" else [])


def test_gitlab_dedup_shares_identity_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    own_key, foreign_key = FINDING_KEY, OTHER_FINDING_KEY
    notes = [
        {
            "body": review_marker.build_marker(FULL_SHA, 1),
            "author": {"username": "foreign"},
        },
        {**_finding_note((foreign_key,)), "author": {"username": "foreign"}},
        {**_finding_note((own_key,)), "author": {"username": "poster"}},
    ]
    state = detect_prior_review.gitlab_prior_delivery_state(
        "o", "r", 5, FULL_SHA, forge=FakeGitLab(entries=[JsonFetch(notes, None)])
    )
    assert state == PriorDelivery(False, frozenset({own_key}), frozenset(), None)
    failure = "identity denied; set CODE_GAUNTLET_POSTER"
    state = detect_prior_review.gitlab_prior_delivery_state(
        "o",
        "r",
        5,
        FULL_SHA,
        forge=FakeGitLab(
            entries=[JsonFetch(notes, None)], posters=[Poster(None, failure)]
        ),
    )
    assert state == PriorDelivery(False, frozenset(), frozenset(), failure)


@pytest.mark.parametrize("timeout", [False, True])
def test_offline_detector_without_git_degrades_with_receipt(
    timeout: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _bodies_file(
        tmp_path, [None, {}, {"body": review_marker.build_footer(0, FULL_SHA)}]
    )

    def unavailable(
        command: Sequence[str], **_kwargs: object
    ) -> proc.CompletedProcess[str]:
        if timeout:
            raise proc.TimeoutExpired(command, 10)
        raise FileNotFoundError("missing git")

    monkeypatch.setattr(proc, "run", unavailable)
    out, code = _run_main(_base_argv(path))
    result: dict[str, object] = json.loads(out)
    assert code == 0
    assert result["previously_reviewed"] is True
    assert result["head_sha"] == "unknown"
    assert result["incremental_safe"] is False
    assert result["sha_resolvable"] is False
    assert result["ignored_foreign"] == 0
    assert result["scanned"] == {"bodies_file": 1}
    errors = cast("list[str]", result["errors"])
    assert "could not resolve the head commit" in errors[0]
    assert ("timed out after 10s" if timeout else "missing git") in errors[1]


@pytest.mark.parametrize("forge_factory", [detect_prior_review], indirect=True)
@pytest.mark.parametrize(
    "flags, error, fetched",
    [
        (
            ["--pr-url", "invalid"],
            "--pr-url: URL must use http(s) and include a host",
            False,
        ),
        *[
            (
                ["--pr-url", "https://git.example.com/o/r/pull/5", flag, value],
                "usage: --pr-url cannot be combined with --owner, --repo or --number",
                False,
            )
            for flag, value in [("--owner", "o"), ("--repo", "r"), ("--number", "5")]
        ],
        (
            ["--pr-url", ""],
            "usage: --number is required unless --bodies-file is given",
            False,
        ),
        (["--pr-url", "", "--number", "5"], None, True),
    ],
)
def test_pr_url_degradation(flags, error, fetched, forge_factory, monkeypatch):
    fake = forge_factory.configure(FakeForge())
    monkeypatch.setattr(proc, "run", _fake_git_run())
    monkeypatch.setattr(detect_prior_review, "remote_slug", lambda: ("o", "r"))
    out, code = _run_main(["--platform", "github", *flags])
    result = json.loads(out)
    assert code == 0
    assert result["errors"] == ([error] if error else [])
    assert forge_factory.calls == ([] if error and flags[1] else [("github", None)])
    assert fake.calls == (
        [ForgeCall("review_entries", ReviewTarget("o", "r", "5")), ForgeCall("poster")]
        if fetched
        else []
    )
