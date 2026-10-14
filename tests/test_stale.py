import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import Invocation
from tests.support.phase2 import ARTIFACTS, SHA, TRUNCATED, seed_artifacts

DEFERRED = (
    "DEFERRED: previously reviewed at the current SHA -- truncation withheld "
    "until the Skip/Review-again answer is known (a Skip must preserve these files)"
)


@pytest.mark.parametrize(
    "detector, unconditional, deferred",
    [
        pytest.param({}, False, False, id="no-prior-review"),
        pytest.param(
            {
                "previously_reviewed": True,
                "sha_resolvable": False,
                "last_reviewed_sha": SHA,
                "head_sha": SHA,
            },
            False,
            False,
            id="unresolved-same-sha",
        ),
        pytest.param(
            {
                "previously_reviewed": True,
                "sha_resolvable": True,
                "last_reviewed_sha": "f" * 40,
                "head_sha": SHA,
            },
            False,
            False,
            id="different-resolved-sha",
        ),
        pytest.param(
            {
                "previously_reviewed": True,
                "sha_resolvable": True,
                "last_reviewed_sha": SHA,
                "head_sha": SHA,
            },
            False,
            True,
            id="same-resolved-sha-deferred",
        ),
        pytest.param(
            {
                "previously_reviewed": True,
                "sha_resolvable": True,
                "last_reviewed_sha": SHA,
                "head_sha": SHA,
            },
            True,
            False,
            id="unconditional-overrides-deferral",
        ),
    ],
)
def test_truncation_decision(
    detector: dict[str, object],
    unconditional: bool,
    deferred: bool,
    tmp_path: Path,
    invoke: Callable[..., Invocation],
) -> None:
    output_dir = tmp_path / "o'brien out[1]"
    seed_artifacts(output_dir)
    args = ["--output-dir", str(output_dir), "--head-sha", SHA]
    if unconditional:
        args.append("--unconditional")
    result = invoke("stale_truncate", args, tmp_path, json.dumps(detector).encode())
    assert result.returncode == 0, result.stderr
    expected = f"{DEFERRED}\n" if deferred else "truncated 2 file(s)\n"
    assert result.stdout == expected.encode()
    assert result.stderr == b""
    for name, contents in ARTIFACTS.items():
        expected_bytes = b"" if not deferred and name in TRUNCATED else contents
        assert (output_dir / name).read_bytes() == expected_bytes, name


@pytest.mark.parametrize(
    "sha, stdin, make_dir",
    [
        pytest.param("not-a-sha", b"{}", True, id="bad-sha"),
        pytest.param(SHA, b"{}", False, id="missing-directory"),
        pytest.param(SHA, b"[]", True, id="non-object-json"),
        pytest.param(SHA, b"{", True, id="malformed-json"),
    ],
)
def test_truncation_refusal(
    sha: str,
    stdin: bytes,
    make_dir: bool,
    tmp_path: Path,
    invoke: Callable[..., Invocation],
) -> None:
    output_dir = tmp_path / "o'brien out[1]"
    if make_dir:
        seed_artifacts(output_dir)
    result = invoke(
        "stale_truncate",
        ["--output-dir", str(output_dir), "--head-sha", sha],
        tmp_path,
        stdin,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert len(result.stderr.splitlines()) == 1
    assert result.stderr.startswith(b"stale_truncate: ")
    if sha != SHA:
        assert result.stderr == (
            b"stale_truncate: --head-sha must be 4 to 40 lowercase "
            b"hexadecimal characters\n"
        )
    if make_dir:
        for name, contents in ARTIFACTS.items():
            assert (output_dir / name).read_bytes() == contents, name
    else:
        assert not output_dir.exists()


def test_unwritable_match_exits_two(
    tmp_path: Path, invoke: Callable[..., Invocation]
) -> None:
    output_dir = tmp_path / "o'brien out[1]"
    output_dir.mkdir()
    (output_dir / f"code-gauntlet-dir-{SHA}.d").mkdir()
    result = invoke(
        "stale_truncate",
        ["--output-dir", str(output_dir), "--head-sha", SHA, "--unconditional"],
        tmp_path,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert len(result.stderr.splitlines()) == 1, result.stderr
    assert result.stderr.startswith(
        f"stale_truncate: cannot truncate code-gauntlet-dir-{SHA}.d (".encode()
    )
    assert b"; 0 of 1 matching file(s) truncated before it" in result.stderr
