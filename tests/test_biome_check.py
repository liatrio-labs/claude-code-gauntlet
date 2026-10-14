from __future__ import annotations

import io
import json
import os
import stat
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from workflows.test.tools import biome_check
from workflows.test.tools.biome_check import (
    PinError,
    UnsupportedPlatformError,
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


def test_resolve_asset() -> None:
    assert resolve_asset("Darwin", "arm64") == "biome-darwin-arm64"


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
    ],
)
def test_cache_state(
    pin_file: Path,
    tmp_path: Path,
    initial: bytes,
    extra: list[str],
    downloads: list[str],
) -> None:
    cache = tmp_path / "cache with spaces"
    final = cache / TEST_VERSION / ASSET
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


@pytest.mark.parametrize(
    "failures,status",
    [
        pytest.param(4, 0, id="four-failures-then-success"),
        pytest.param(5, 1, id="five-failures-exhausted"),
    ],
)
def test_retry_outcome(
    pin_file: Path,
    tmp_path: Path,
    failures: int,
    status: int,
) -> None:
    calls = 0
    failure = urllib.error.URLError("temporary failure")

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
    assert calls == 5
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4, 8]


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


def test_cache_location(pin_file: Path, tmp_path: Path) -> None:
    env = {"CODE_GAUNTLET_BIOME_CACHE": str(tmp_path / "configured")}
    args = ["--asset", ASSET, "--cache-dir", str(tmp_path / "flag")]
    assert (
        main(args, fetch=lambda _url: DATA, run=lambda _argv, _cwd: 0, environ=env) == 0
    )
    assert (tmp_path / "flag" / TEST_VERSION / ASSET).is_file()
    assert not (tmp_path / "configured" / TEST_VERSION / ASSET).exists()
