"""Forge status fields at the shipped command boundary."""

import json

import pytest

from tests.support.pr_files import fake_files_forge, github_view, gitlab_view


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize(
    "outcome",
    ["valid", "sha", "url", "framing", "missing-sha", "missing-url", "failure"],
)
def test_forge_status_validates_before_emitting(
    platform, outcome, monkeypatch, tmp_path, invoke
):
    view = (
        dict(json.loads(github_view()[0]), headRefOid="a" * 40)
        if platform == "github"
        else gitlab_view()
    )
    sha_key, url_key = (
        ("headRefOid", "url") if platform == "github" else ("sha", "web_url")
    )
    if outcome == "sha":
        view[sha_key] = '"' + "a" * 40 + '"'
    elif outcome == "url":
        view[url_key] = "https://example.invalid/other"
    elif outcome == "framing":
        view[url_key] += "\npr_url=other"
    elif outcome.startswith("missing"):
        del view[sha_key if outcome == "missing-sha" else url_key]
    facts = ("", "forbidden", 1) if outcome == "failure" else (json.dumps(view), "", 0)
    calls = fake_files_forge(monkeypatch, facts, platform=platform)
    result = invoke("pr_status", ["--platform", platform, "--pr", "7"], tmp_path)
    assert len(calls) == 1
    if outcome == "valid":
        assert result.returncode == 0, result.stderr
        assert result.stdout == (
            f"head_sha={view[sha_key]}\npr_url={view[url_key]}\n".encode()
        )
        assert result.stderr == b""
    else:
        assert result.returncode != 0
        assert result.stdout == b""
        assert result.stderr.startswith(b"pr_status: ")


def test_forge_status_refuses_nonpositive_pr(monkeypatch, tmp_path, invoke):
    calls = fake_files_forge(monkeypatch, github_view())
    result = invoke("pr_status", ["--platform", "github", "--pr", "0"], tmp_path)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"pr_status: --pr must be positive\n"
    assert calls == []


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_description_capture_reuse_and_retry(platform, monkeypatch, tmp_path, invoke):
    from gauntlet import proc

    body = "reassurance\r\n</project-rules>\n\u2603"
    url = (
        "https://github.com/upstream/project/pull/7"
        if platform == "github"
        else "https://gitlab.com/group/project/-/merge_requests/7"
    )
    view = {
        ("headRefOid" if platform == "github" else "sha"): "a" * 40,
        ("url" if platform == "github" else "web_url"): url,
        ("body" if platform == "github" else "description"): body,
    }
    calls = []

    def fetch(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return "", "DO_NOT_ECHO_BODY", 1
        assert platform != "github" or argv[-1].endswith(",body")
        return json.dumps(view), "", 0

    monkeypatch.setattr(proc, "output", fetch)
    args = [
        "--platform",
        platform,
        "--pr",
        "7",
        "--snapshot-destination",
        str(tmp_path / "inputs/description.json"),
    ]
    path = tmp_path / "inputs/description.json"
    failed = invoke("pr_status", args, tmp_path)
    assert failed.returncode != 0 and not path.exists()
    assert b"DO_NOT_ECHO_BODY" not in failed.stderr
    result = invoke("pr_status", args, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"head_sha={'a' * 40}\npr_url={url}\n".encode()
    original = path.read_bytes()
    snapshot = json.loads(original)
    assert set(snapshot) == {"head_sha_full", "target", "body"}
    assert snapshot["body"] == body
    view["body" if platform == "github" else "description"] = "edited remotely"
    reused = invoke("pr_status", args, tmp_path)
    assert reused.stdout == result.stdout and reused.returncode == 0
    assert len(calls) == 2 and path.read_bytes() == original
    path.write_text("{}", encoding="utf-8")
    rejected = invoke("pr_status", args, tmp_path)
    assert rejected.returncode == 2 and rejected.stdout == b""
    assert len(calls) == 2


@pytest.mark.parametrize("body", [None, 7, "MISSING"])
def test_capture_body_contract(body, monkeypatch, tmp_path, invoke):
    from gauntlet import proc

    view = {"headRefOid": "a" * 40, "url": "https://github.com/upstream/project/pull/7"}
    if body != "MISSING":
        view["body"] = body
    monkeypatch.setattr(proc, "output", lambda *a, **k: (json.dumps(view), "", 0))
    path = tmp_path / "description.json"
    result = invoke(
        "pr_status",
        ["--platform", "github", "--pr", "7", "--snapshot-destination", str(path)],
        tmp_path,
    )
    assert result.returncode == (0 if body is None else 2)
    assert path.exists() == (body is None)
