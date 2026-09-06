"""Tests for the contributor-local Biome runner."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import socket
import stat
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workflows.test.tools import biome_check
from workflows.test.tools.biome_check import (
    PinError,
    UnsupportedPlatformError,
    build_command,
    load_pin,
    main,
    resolve_asset,
)

ASSET_ROWS = (
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
)


def _pin_data(version: str, asset: str, digest: str) -> dict[str, object]:
    return {"version": version, "sha256": {asset: digest}}


TEST_VERSION = "1.2.3"


class TestBiomeCheck(unittest.TestCase):
    def test_resolve_asset_table_and_unsupported_pair(self) -> None:
        for system, machine, expected in ASSET_ROWS:
            with self.subTest(system=system, machine=machine):
                self.assertEqual(resolve_asset(system, machine), expected)
        with self.assertRaises(UnsupportedPlatformError) as raised:
            resolve_asset("Plan9", "sparc")
        for asset in biome_check.ASSET_NAMES:
            self.assertIn(asset, str(raised.exception))

    def test_load_pin_rejects_invalid_shapes_and_values(self) -> None:
        cases: tuple[tuple[object, str], ...] = (
            ([], "pin file must be an object"),
            ({"sha256": {"asset": "a" * 64}}, "version"),
            (_pin_data("1.2", "asset", "a" * 64), "version"),
            (_pin_data(TEST_VERSION, "asset", "g" * 64), "asset"),
            (_pin_data(TEST_VERSION, "asset", "a" * 63), "asset"),
            ({"version": TEST_VERSION}, "sha256"),
            ({"version": TEST_VERSION, "sha256": {}}, "sha256"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pin.json"
            for value, message in cases:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaisesRegex(PinError, message):
                        load_pin(path)

    def test_build_command_puts_extra_args_after_the_workflow_path(self) -> None:
        self.assertEqual(
            build_command("/tmp/biome binary", ["--verbose"]),
            ["/tmp/biome binary", "check", "--error-on-warnings", ".", "--verbose"],
        )

    def test_checksum_mismatch_leaves_no_final_or_part_file(self) -> None:
        expected_bytes = b"the expected binary"
        asset = "biome-test"
        pin = _pin_data(TEST_VERSION, asset, hashlib.sha256(expected_bytes).hexdigest())
        checksums = pin["sha256"]
        assert isinstance(checksums, dict)
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            cache = Path(directory) / "cache with spaces"
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            error = io.StringIO()
            with (
                redirect_stderr(error),
                patch.object(biome_check, "PIN_FILE", pin_path),
            ):
                code = main(
                    ["--asset", asset, "--cache-dir", str(cache)],
                    fetch=lambda _url: b"wrong bytes",
                    run=lambda _argv, _cwd: 0,
                )
            final = cache.resolve() / TEST_VERSION / asset
            self.assertEqual(code, 1)
            self.assertIn(f"expected {checksums[asset]}", error.getvalue())
            self.assertIn("actual", error.getvalue())
            self.assertFalse(final.exists())
            self.assertFalse(final.with_name(f"{asset}.part").exists())

    @unittest.skipUnless(os.name != "nt", "mode bits are POSIX-only")
    def test_mismatched_cache_is_refreshed_and_installed_executable(self) -> None:
        asset = "biome-test"
        old_data = b"stale cached binary"
        expected_data = b"refreshed binary"
        pin = _pin_data(TEST_VERSION, asset, hashlib.sha256(expected_data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            cache = Path(directory) / "cache"
            final = cache.resolve() / TEST_VERSION / asset
            final.parent.mkdir(parents=True)
            final.write_bytes(old_data)
            final.chmod(0o644)
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            fetched: list[str] = []

            def fetch(url: str) -> bytes:
                fetched.append(url)
                return expected_data

            with patch.object(biome_check, "PIN_FILE", pin_path):
                code = main(
                    ["--asset", asset, "--cache-dir", str(cache)],
                    fetch=fetch,
                    run=lambda _argv, _cwd: 0,
                )

            self.assertEqual(code, 0)
            self.assertEqual(len(fetched), 1)
            self.assertEqual(final.read_bytes(), expected_data)
            self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o755)
            self.assertFalse(final.with_name(f"{asset}.part").exists())

    @unittest.skipUnless(os.name != "nt", "mode bits are POSIX-only")
    def test_cache_hit_skips_fetch_restores_mode_and_runs_expected_command(
        self,
    ) -> None:
        asset = "biome-test"
        data = b"cached binary"
        pin = _pin_data(TEST_VERSION, asset, hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            cache = Path(directory) / "cache with spaces"
            final = cache.resolve() / TEST_VERSION / asset
            final.parent.mkdir(parents=True)
            final.write_bytes(data)
            final.chmod(0o644)
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            calls: list[tuple[list[str], Path]] = []

            def run(argv: list[str], cwd: Path) -> int:
                calls.append((argv, cwd))
                return 0

            with patch.object(biome_check, "PIN_FILE", pin_path):
                code = main(
                    ["--asset", asset, "--cache-dir", str(cache), "--", "--verbose"],
                    fetch=lambda _url: (_ for _ in ()).throw(
                        AssertionError("cache hit downloaded")
                    ),
                    run=run,
                )
            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o755)
            self.assertEqual(
                calls,
                [
                    (
                        [
                            str(final),
                            "check",
                            "--error-on-warnings",
                            ".",
                            "--verbose",
                        ],
                        biome_check.WORKFLOWS_DIR,
                    )
                ],
            )

    @unittest.skipUnless(os.name != "nt", "mode bits are POSIX-only")
    def test_download_verifies_and_installs_executable(self) -> None:
        asset = "biome-test"
        data = b"downloaded binary"
        pin = _pin_data(TEST_VERSION, asset, hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            cache = Path(directory) / "cache"
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            calls: list[tuple[list[str], Path]] = []

            def record_run(argv: list[str], cwd: Path) -> int:
                calls.append((argv, cwd))
                return 0

            with patch.object(biome_check, "PIN_FILE", pin_path):
                code = main(
                    ["--asset", asset, "--cache-dir", str(cache)],
                    fetch=lambda url: data,
                    run=record_run,
                )
            final = cache.resolve() / TEST_VERSION / asset
            self.assertEqual(code, 0)
            self.assertEqual(final.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o755)
            self.assertEqual(
                calls[0],
                (
                    [str(final), "check", "--error-on-warnings", "."],
                    biome_check.WORKFLOWS_DIR,
                ),
            )

    def test_biome_return_code_is_propagated(self) -> None:
        asset = "biome-test"
        data = b"return code binary"
        pin = _pin_data(TEST_VERSION, asset, hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            with patch.object(biome_check, "PIN_FILE", pin_path):
                self.assertEqual(
                    main(
                        ["--asset", asset, "--cache-dir", directory],
                        fetch=lambda _url: data,
                        run=lambda _argv, _cwd: 17,
                    ),
                    17,
                )

    def test_retry_four_failures_then_success_and_five_failures(self) -> None:
        asset = "biome-test"
        data = b"retry binary"
        pin = _pin_data(TEST_VERSION, asset, hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            successful_calls = 0

            def flaky_fetch(_url: str) -> bytes:
                nonlocal successful_calls
                successful_calls += 1
                if successful_calls < 5:
                    raise urllib.error.URLError("temporary failure")
                return data

            with (
                patch.object(biome_check, "PIN_FILE", pin_path),
                patch.object(biome_check.time, "sleep") as sleep,
            ):
                code = main(
                    ["--asset", asset, "--cache-dir", str(Path(directory) / "one")],
                    fetch=flaky_fetch,
                    run=lambda _argv, _cwd: 0,
                )
            self.assertEqual(code, 0)
            self.assertEqual(successful_calls, 5)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8]
            )

            failed_calls = 0

            def failing_fetch(_url: str) -> bytes:
                nonlocal failed_calls
                failed_calls += 1
                raise urllib.error.URLError("permanent failure")

            with (
                patch.object(biome_check, "PIN_FILE", pin_path),
                patch.object(biome_check.time, "sleep"),
            ):
                code = main(
                    ["--asset", asset, "--cache-dir", str(Path(directory) / "two")],
                    fetch=failing_fetch,
                    run=lambda _argv, _cwd: 0,
                )
            self.assertEqual(code, 1)
            self.assertEqual(failed_calls, 5)

    def test_retry_recovers_from_non_url_errors(self) -> None:
        failures = (
            socket.timeout("timed out"),  # noqa: UP041 - named regression case
            urllib.error.HTTPError(
                "https://example.test/biome", 503, "unavailable", Message(), None
            ),
            http.client.IncompleteRead(b"partial"),
        )
        asset = "biome-test"
        data = b"retry-all-errors binary"
        pin = _pin_data(TEST_VERSION, asset, hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            for index, failure in enumerate(failures):
                calls = 0

                def flaky_fetch(_url: str, failure: Exception = failure) -> bytes:
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise failure
                    return data

                with (
                    self.subTest(error=type(failure).__name__),
                    patch.object(biome_check, "PIN_FILE", pin_path),
                    patch.object(biome_check.time, "sleep") as sleep,
                ):
                    code = main(
                        [
                            "--asset",
                            asset,
                            "--cache-dir",
                            str(Path(directory) / f"cache-{index}"),
                        ],
                        fetch=flaky_fetch,
                        run=lambda _argv, _cwd: 0,
                    )

                self.assertEqual(code, 0)
                self.assertEqual(calls, 2)
                sleep.assert_called_once_with(1)

    def test_fetch_url_builds_the_pinned_request(self) -> None:
        url = "https://example.test/biome"
        data = b"downloaded over urllib"
        with patch.object(biome_check.urllib.request, "urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = data

            result = biome_check._fetch_url(url)

        self.assertEqual(result, data)
        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertIsInstance(request, biome_check.urllib.request.Request)
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.get_header("User-agent"), biome_check.USER_AGENT)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)

    def test_main_wires_the_default_process_adapter(self) -> None:
        asset = "biome-test"
        data = b"pre-verified binary"
        pin = _pin_data(TEST_VERSION, asset, hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            cache = Path(directory) / "cache"
            final = cache.resolve() / TEST_VERSION / asset
            final.parent.mkdir(parents=True)
            final.write_bytes(data)
            pin_path.write_text(json.dumps(pin), encoding="utf-8")

            with (
                patch.object(biome_check, "PIN_FILE", pin_path),
                patch.object(
                    biome_check.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=17),
                ) as subprocess_run,
            ):
                code = main(
                    ["--asset", asset, "--cache-dir", str(cache)],
                    fetch=lambda _url: (_ for _ in ()).throw(
                        AssertionError("pre-verified cache downloaded")
                    ),
                )

            self.assertEqual(code, 17)
            subprocess_run.assert_called_once_with(
                [str(final), "check", "--error-on-warnings", "."],
                cwd=biome_check.WORKFLOWS_DIR,
            )

    def test_invalid_asset_override_is_usage_error(self) -> None:
        data = b"asset error"
        pin = _pin_data(TEST_VERSION, "biome-test", hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            with patch.object(biome_check, "PIN_FILE", pin_path):
                self.assertEqual(
                    main(
                        ["--asset", "missing", "--cache-dir", directory],
                        fetch=lambda _url: data,
                        run=lambda _argv, _cwd: 0,
                    ),
                    2,
                )

    def test_cache_environment_precedes_xdg_default(self) -> None:
        asset = "biome-test"
        data = b"environment binary"
        pin = _pin_data(TEST_VERSION, asset, hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            pin_path = Path(directory) / "pin.json"
            pin_path.write_text(json.dumps(pin), encoding="utf-8")
            cache = Path(directory) / "configured-cache"
            xdg = Path(directory) / "xdg-cache"
            with patch.object(biome_check, "PIN_FILE", pin_path):
                code = main(
                    ["--asset", asset],
                    fetch=lambda _url: data,
                    run=lambda _argv, _cwd: 0,
                    environ={
                        "CODE_GAUNTLET_BIOME_CACHE": str(cache),
                        "XDG_CACHE_HOME": str(xdg),
                    },
                )
            self.assertEqual(code, 0)
            self.assertTrue((cache / TEST_VERSION / asset).is_file())
            self.assertFalse((xdg / "code-gauntlet" / "biome").exists())


if __name__ == "__main__":
    unittest.main()
