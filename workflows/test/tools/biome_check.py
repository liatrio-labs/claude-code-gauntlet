#!/usr/bin/env python3
"""Run the local twin of CI's ``Run Workflow JS Lint`` gate.

CI runs this same file. It reads the version and per-asset SHA-256 values from
``workflows/biome-pin.json``, stores verified binaries in the user cache, and
downloads a missing or mismatched asset from the Biome release. ``biome check``
must run with cwd = <repo>/workflows because ``--config-path`` from the repo
root trips Biome's nested-root-configuration detection. The Windows and musl
assets are pinned for users but have no CI runner and are unexercised there.

The final download name is replaced atomically from ``<asset>.part``. On
Windows, replacing an in-use binary raises ``PermissionError``; close Biome
before retrying. The runner is Python 3.10-compatible and stdlib-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / "workflows"
PIN_FILE = WORKFLOWS_DIR / "biome-pin.json"
ASSET_NAMES = (
    "biome-darwin-arm64",
    "biome-darwin-x64",
    "biome-linux-arm64",
    "biome-linux-arm64-musl",
    "biome-linux-x64",
    "biome-linux-x64-musl",
    "biome-win32-arm64.exe",
    "biome-win32-x64.exe",
)
VERSION_PATTERN = r"^\d+\.\d+\.\d+$"
HEX_PATTERN = r"^[0-9a-f]{64}$"
RELEASE_URL = "https://github.com/biomejs/biome/releases/download/@biomejs/biome@{version}/{asset}"
USER_AGENT = "code-gauntlet-biome-check"
MAX_FETCH_ATTEMPTS = 5


class BiomeCheckError(Exception):
    """Base class for runner errors."""


class BiomeUsageError(ValueError, BiomeCheckError):
    """An unsupported platform, invalid pin, or invalid asset selection."""


class PinError(BiomeUsageError):
    """The pin file is missing or has an invalid shape or value."""


class UnsupportedPlatformError(BiomeUsageError):
    """The host platform has no default asset mapping."""


class DownloadError(BiomeCheckError):
    """The release asset could not be downloaded after all retries."""


class ChecksumError(BiomeCheckError):
    """The downloaded release asset did not match its pin."""


@dataclass(frozen=True)
class Pin:
    version: str
    sha256: dict[str, str]


Fetch = Callable[[str], bytes]
ProcessRunner = Callable[[list[str], Path], int]


def resolve_asset(system: str, machine: str) -> str:
    """Resolve a platform pair to a release asset, or raise a typed error."""
    mappings = {
        ("Darwin", "arm64"): "biome-darwin-arm64",
        ("Darwin", "x86_64"): "biome-darwin-x64",
        ("Linux", "x86_64"): "biome-linux-x64",
        ("Linux", "amd64"): "biome-linux-x64",
        ("Linux", "aarch64"): "biome-linux-arm64",
        ("Linux", "arm64"): "biome-linux-arm64",
        ("Windows", "AMD64"): "biome-win32-x64.exe",
        ("Windows", "x86_64"): "biome-win32-x64.exe",
        ("Windows", "ARM64"): "biome-win32-arm64.exe",
        ("Windows", "aarch64"): "biome-win32-arm64.exe",
    }
    try:
        return mappings[(system, machine)]
    except KeyError as exc:
        names = ", ".join(ASSET_NAMES)
        raise UnsupportedPlatformError(
            f"unsupported platform {system}/{machine}; pin file assets: {names}"
        ) from exc


def load_pin(path: str | os.PathLike[str]) -> Pin:
    """Read and validate a Biome pin file."""
    pin_path = Path(path)
    try:
        with pin_path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PinError(f"could not read pin file {pin_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise PinError("pin file must be an object")
    if "version" not in raw:
        raise PinError("pin file is missing key 'version'")
    version = raw["version"]
    if not isinstance(version, str) or not re.fullmatch(VERSION_PATTERN, version):
        raise PinError("pin key 'version' must match major.minor.patch")
    if "sha256" not in raw:
        raise PinError("pin file is missing key 'sha256'")
    checksums = raw["sha256"]
    if not isinstance(checksums, dict) or not checksums:
        raise PinError("pin key 'sha256' must be a non-empty object")

    validated: dict[str, str] = {}
    for asset, digest in checksums.items():
        if not isinstance(asset, str):
            raise PinError("pin key 'sha256' contains a non-string asset key")
        if not isinstance(digest, str) or not re.fullmatch(HEX_PATTERN, digest):
            raise PinError(f"pin key '{asset}' must be 64 lowercase hex characters")
        validated[asset] = digest
    return Pin(version=version, sha256=validated)


def build_command(binary: str | os.PathLike[str], extra: list[str]) -> list[str]:
    """Build the Biome invocation with caller arguments appended."""
    return [os.fspath(binary), "check", "--error-on-warnings", ".", *extra]


def _fetch_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return cast(bytes, response.read())


def _fetch_with_retries(fetch: Fetch, url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            return fetch(url)
        except Exception as exc:  # noqa: BLE001 - --retry-all-errors semantics
            last_error = exc
            if attempt + 1 < MAX_FETCH_ATTEMPTS:
                time.sleep(2**attempt)
    assert last_error is not None
    raise DownloadError(
        f"download failed after {MAX_FETCH_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _set_executable(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o755)


def _cache_root(cache_dir: str | None, environ: Mapping[str, str]) -> Path:
    configured = (
        cache_dir if cache_dir is not None else environ.get("CODE_GAUNTLET_BIOME_CACHE")
    )
    if configured is not None:
        return Path(configured).expanduser().resolve()
    base = environ.get("XDG_CACHE_HOME")
    if not base:
        home = environ.get("HOME") or str(Path.home())
        base = os.path.join(home, ".cache")
    return (Path(base).expanduser() / "code-gauntlet" / "biome").resolve()


def _ensure_binary(
    pin: Pin, asset: str, cache_root: Path, fetch: Fetch
) -> tuple[Path, bool]:
    expected = pin.sha256[asset]
    destination = cache_root / pin.version / asset
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if destination.is_file() and _sha256(destination) == expected:
            _set_executable(destination)
            return destination, True
    except OSError:
        # An unreadable cache probe is treated like a miss: fall through to a
        # fresh, verified download rather than trusting a file we could not hash.
        pass

    destination.unlink(missing_ok=True)
    part = destination.with_name(f"{asset}.part")
    try:
        data = _fetch_with_retries(
            fetch, RELEASE_URL.format(version=pin.version, asset=asset)
        )
        if not isinstance(data, bytes):
            raise DownloadError("fetcher returned non-bytes data")
        part.write_bytes(data)
        actual = _sha256(part)
        if actual != expected:
            raise ChecksumError(
                f"checksum mismatch for {asset}: expected {expected}, actual {actual}"
            )
        os.replace(part, destination)
        _set_executable(destination)
        return destination, False
    finally:
        part.unlink(missing_ok=True)


def _run_process(command: list[str], cwd: Path) -> int:
    return subprocess.run(command, cwd=cwd).returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", help="override the platform asset, for musl users")
    parser.add_argument("--cache-dir", help="override the Biome cache directory")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    fetch: Fetch | None = None,
    run: ProcessRunner | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Download if needed, then run Biome and return its exit code."""
    args = _parser().parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        pin = load_pin(PIN_FILE)
        asset = (
            args.asset
            if args.asset is not None
            else resolve_asset(platform.system(), platform.machine())
        )
        if asset not in pin.sha256:
            raise BiomeUsageError(f"asset '{asset}' is not present in pin key 'sha256'")
        cache_root = _cache_root(args.cache_dir, env)
        fetcher = _fetch_url if fetch is None else fetch
        binary, cached = _ensure_binary(pin, asset, cache_root, fetcher)
        if cached:
            print(f"Using cached Biome binary: {binary}", file=sys.stderr)
        else:
            print(f"Downloaded and verified Biome binary: {binary}", file=sys.stderr)
        extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
        command = build_command(binary, extra)
        runner = _run_process if run is None else run
        return runner(command, WORKFLOWS_DIR)
    except BiomeUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (BiomeCheckError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
