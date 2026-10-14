from __future__ import annotations

import http.client
import io
import json
import os
import socket
import stat
import urllib.error
from contextlib import redirect_stderr
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from workflows.test.tools import biome_check
from workflows.test.tools.biome_check import (
    PinError,
    UnsupportedPlatformError,
    build_command,
    load_pin,
    main,
    resolve_asset,
)

TEST_VERSION = "1.2.3"
ASSET = "biome-test"
DATA = b"verified binary"
DIGEST = "86fd6fb55a10988213329d914da3f5fbbc213ee143b46148ed21b60d9454e3dc"
URL = (
    "https://github.com/biomejs/biome/releases/download/@biomejs/biome@1.2.3/biome-test"
)


@pytest.fixture
def pin_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pin.json"
    path.write_text(
        json.dumps(
            {
                "version": TEST_VERSION,
                "sha256": {ASSET: DIGEST},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(biome_check, "PIN_FILE", path)
    return path


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Darwin", "arm64", "biome-darwin-arm64"),
        ("Darwin", "x86_64", "biome-darwin-x64"),
        ("Linux", "x86_64", "biome-linux-x64"),
        ("Linux", "amd64", "biome-linux-x64"),
        ("Linux", "aarch64", "biome-linux-arm64"),
        ("Linux", "arm64", "biome-linux-arm64"),
        ("Windows", "AMD64", "biome-win32-x64.exe"),
        ("Windows", "x86_64", "biome-win32-x64.exe"),
        ("Windows", "ARM64", "biome-win32-arm64.exe"),
        ("Windows", "aarch64", "biome-win32-arm64.exe"),
    ],
)
def test_resolve_asset(system: str, machine: str, expected: str) -> None:
    assert resolve_asset(system, machine) == expected


def test_unsupported_platform_names_available_assets() -> None:
    with pytest.raises(UnsupportedPlatformError) as raised:
        resolve_asset("Plan9", "sparc")
    for asset in biome_check.ASSET_NAMES:
        assert asset in str(raised.value)


@pytest.mark.parametrize(
    "value,message",
    [
        pytest.param([], "pin file must be an object", id="non-object"),
        pytest.param({"sha256": {"asset": "a" * 64}}, "version", id="missing-version"),
        pytest.param(
            {"version": "1.2", "sha256": {"asset": "a" * 64}},
            "version",
            id="incomplete-version",
        ),
        pytest.param(
            {"version": "1.2.3", "sha256": {"asset": "g" * 64}},
            "asset",
            id="non-hex-digest",
        ),
        pytest.param(
            {"version": "1.2.3", "sha256": {"asset": "a" * 63}},
            "asset",
            id="short-digest",
        ),
        pytest.param({"version": "1.2.3"}, "sha256", id="missing-checksums"),
        pytest.param(
            {"version": "1.2.3", "sha256": {}}, "sha256", id="empty-checksums"
        ),
    ],
)
def test_load_pin_rejects_invalid_shapes_and_values(
    tmp_path: Path, value: object, message: str
) -> None:
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(PinError, match=message):
        load_pin(path)


def test_build_command_puts_extra_args_after_the_workflow_path() -> None:
    assert build_command("/tmp/biome binary", ["--verbose"]) == [
        "/tmp/biome binary",
        "check",
        "--error-on-warnings",
        ".",
        "--verbose",
    ]


def test_checksum_mismatch_leaves_no_final_or_part_file(
    pin_file: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache with spaces"
    error = io.StringIO()
    with redirect_stderr(error):
        code = main(
            ["--asset", ASSET, "--cache-dir", str(cache)],
            environ={"HOME": str(tmp_path)},
            fetch=lambda _url: b"wrong bytes",
            run=lambda _argv, _cwd: 0,
        )
    final = cache / TEST_VERSION / ASSET
    assert code == 1
    assert "expected " + DIGEST in error.getvalue()
    assert "actual" in error.getvalue()
    assert not final.exists()
    assert not final.with_name("biome-test.part").exists()


@pytest.mark.skipif(os.name == "nt", reason="mode bits are POSIX-only")
@pytest.mark.parametrize(
    "initial,extra,downloads",
    [
        pytest.param(
            b"stale cached binary",
            [],
            [URL],
            id="corrupt-cache-refreshed",
        ),
        pytest.param(
            DATA,
            ["--verbose"],
            [],
            id="valid-cache-restores-mode-without-fetch",
        ),
        pytest.param(
            None,
            [],
            [URL],
            id="absent-cache-downloaded",
        ),
    ],
)
def test_cache_state(
    pin_file: Path,
    tmp_path: Path,
    initial: bytes | None,
    extra: list[str],
    downloads: list[str],
) -> None:
    cache = tmp_path / "cache with spaces"
    final = cache / TEST_VERSION / ASSET
    if initial is not None:
        final.parent.mkdir(parents=True)
        final.write_bytes(initial)
        final.chmod(0o644)
    fetched: list[str] = []
    calls: list[tuple[list[str], Path]] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return DATA

    def run(argv: list[str], cwd: Path) -> int:
        calls.append((argv, cwd))
        return 0

    assert (
        main(
            ["--asset", ASSET, "--cache-dir", str(cache), "--", *extra],
            environ={"HOME": str(tmp_path)},
            fetch=fetch,
            run=run,
        )
        == 0
    )
    assert fetched == downloads
    assert final.read_bytes() == DATA
    assert stat.S_IMODE(final.stat().st_mode) == 0o755
    assert not final.with_name("biome-test.part").exists()
    assert calls == [
        (
            [str(final), "check", "--error-on-warnings", ".", *extra],
            biome_check.WORKFLOWS_DIR,
        )
    ]


def test_biome_return_code_is_propagated(pin_file: Path, tmp_path: Path) -> None:
    assert (
        main(
            ["--asset", ASSET, "--cache-dir", str(tmp_path)],
            environ={"HOME": str(tmp_path)},
            fetch=lambda _url: DATA,
            run=lambda _argv, _cwd: 17,
        )
        == 17
    )


@pytest.mark.parametrize(
    "failure,failures,status,attempts,sleeps",
    [
        pytest.param(
            urllib.error.URLError("temporary failure"),
            4,
            0,
            5,
            [1, 2, 4, 8],
            id="four-failures-then-success",
        ),
        pytest.param(
            urllib.error.URLError("permanent failure"),
            5,
            1,
            5,
            [1, 2, 4, 8],
            id="five-failures-exhausted",
        ),
        pytest.param(
            urllib.error.HTTPError(
                "https://example.test", 503, "Unavailable", Message(), None
            ),
            1,
            0,
            2,
            [1],
            id="http-error-recovered",
        ),
        pytest.param(
            # Exercise the socket API spelling.
            socket.timeout("timed out"),  # noqa: UP041
            1,
            0,
            2,
            [1],
            id="socket-timeout-recovered",
        ),
        pytest.param(
            http.client.IncompleteRead(b"partial"),
            1,
            0,
            2,
            [1],
            id="partial-response-recovered",
        ),
    ],
)
def test_retry_outcome(
    pin_file: Path,
    tmp_path: Path,
    failure: Exception,
    failures: int,
    status: int,
    attempts: int,
    sleeps: list[int],
) -> None:
    calls = 0

    def fetch(_url: str) -> bytes:
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise failure
        return DATA

    with patch.object(biome_check.time, "sleep") as sleep:
        assert (
            main(
                ["--asset", ASSET, "--cache-dir", str(tmp_path / "cache")],
                environ={"HOME": str(tmp_path)},
                fetch=fetch,
                run=lambda _argv, _cwd: 0,
            )
            == status
        )
    assert calls == attempts
    assert [call.args[0] for call in sleep.call_args_list] == sleeps


def test_fetch_url_builds_the_pinned_request() -> None:
    with patch.object(biome_check.urllib.request, "urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = (
            b"downloaded over urllib"
        )
        assert (
            biome_check._fetch_url("https://example.test/biome")
            == b"downloaded over urllib"
        )
    urlopen.assert_called_once()
    request = urlopen.call_args.args[0]
    assert isinstance(request, biome_check.urllib.request.Request)
    assert request.full_url == "https://example.test/biome"
    assert request.get_header("User-agent") == biome_check.USER_AGENT
    assert urlopen.call_args.kwargs["timeout"] == 30


def test_main_wires_the_default_process_adapter(pin_file: Path, tmp_path: Path) -> None:
    final = tmp_path / TEST_VERSION / ASSET
    final.parent.mkdir(parents=True)
    final.write_bytes(DATA)

    def no_fetch(_url: str) -> bytes:
        raise AssertionError("pre-verified cache downloaded")

    with patch.object(
        biome_check.subprocess, "run", return_value=SimpleNamespace(returncode=17)
    ) as subprocess_run:
        code = main(
            ["--asset", ASSET, "--cache-dir", str(tmp_path)],
            environ={"HOME": str(tmp_path)},
            fetch=no_fetch,
        )
    assert code == 17
    subprocess_run.assert_called_once_with(
        [str(final), "check", "--error-on-warnings", "."], cwd=biome_check.WORKFLOWS_DIR
    )


def test_invalid_asset_override_is_usage_error(pin_file: Path, tmp_path: Path) -> None:
    assert (
        main(
            ["--asset", "missing", "--cache-dir", str(tmp_path)],
            environ={"HOME": str(tmp_path)},
            fetch=lambda _url: DATA,
            run=lambda _argv, _cwd: 0,
        )
        == 2
    )


@pytest.mark.parametrize(
    "flag,environment,relative,unused",
    [
        pytest.param(
            "flag",
            {
                "CODE_GAUNTLET_BIOME_CACHE": "configured",
                "XDG_CACHE_HOME": "xdg",
                "HOME": "home",
            },
            "flag",
            "configured",
            id="flag-over-environment",
        ),
        pytest.param(
            None,
            {
                "CODE_GAUNTLET_BIOME_CACHE": "configured",
                "XDG_CACHE_HOME": "xdg",
                "HOME": "home",
            },
            "configured",
            "xdg/code-gauntlet/biome",
            id="environment-over-xdg",
        ),
        pytest.param(
            None,
            {"XDG_CACHE_HOME": "xdg", "HOME": "home"},
            "xdg/code-gauntlet/biome",
            "home/.cache/code-gauntlet/biome",
            id="xdg-over-home",
        ),
        pytest.param(
            None,
            {"HOME": "home"},
            "home/.cache/code-gauntlet/biome",
            "home/code-gauntlet/biome",
            id="home-dot-cache-fallback",
        ),
    ],
)
def test_cache_location(
    pin_file: Path,
    tmp_path: Path,
    flag: str | None,
    environment: dict[str, str],
    relative: str,
    unused: str,
) -> None:
    env = {key: str(tmp_path / value) for key, value in environment.items()}
    args = ["--asset", ASSET]
    if flag is not None:
        args += ["--cache-dir", str(tmp_path / flag)]
    assert (
        main(args, fetch=lambda _url: DATA, run=lambda _argv, _cwd: 0, environ=env) == 0
    )
    assert (tmp_path / relative / TEST_VERSION / ASSET).is_file()
    assert not (tmp_path / unused / TEST_VERSION / ASSET).exists()
