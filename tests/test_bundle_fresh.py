import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.support.repository import workflow_header

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "workflows" / "pipeline.js"


@pytest.fixture(scope="module")
def bundle_text() -> str:
    return BUNDLE.read_text(encoding="utf-8")


def test_bundle_is_byte_identical_to_fresh_build() -> None:
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import { build } from './workflows/build.js'; process.stdout"
            ".write(build());",
        ],
        check=True,
        capture_output=True,
        cwd=REPO,
    )
    assert BUNDLE.read_bytes() == result.stdout, (
        "workflows/pipeline.js is stale - run `node workflows/build.js`"
    )


@pytest.mark.parametrize(
    "pattern", [r"^\s*import ", r"require\("], ids=["no-import", "no-require"]
)
def test_bundle_has_no_module_imports(bundle_text: str, pattern: str) -> None:
    assert not re.search(pattern, bundle_text, re.MULTILINE)


def test_plugin_version_matches_bundle_version(bundle_text: str) -> None:
    plugin: dict[str, object] = json.loads(
        (REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    version = plugin["version"]
    assert isinstance(version, str)
    assert f"const PIPELINE_VERSION = '{version}';" in bundle_text.splitlines()


def test_bundle_runtime_shape(bundle_text: str) -> None:
    lines = [line for line in bundle_text.splitlines() if line.strip()]
    assert lines, "bundle is empty"
    assert lines[0].startswith("export const meta"), "bundle must begin with meta"
    # The workflow runtime permits only the meta export and enters via a trailing
    # return.
    assert len([line for line in lines if re.match(r"^\s*export\b", line)]) == 1
    assert lines[-1].strip().startswith("return await run(")
    description = workflow_header(REPO)["description"]
    assert isinstance(description, str) and description.strip()


def test_bundle_body_compiles_as_async_function(bundle_text: str) -> None:
    # The host wraps the body in an async function; compilation catches top-level
    # declaration collisions without invoking runtime globals.
    body = "\n".join(
        line
        for line in bundle_text.splitlines()
        if not re.match(r"^\s*export\s+const\s+meta\b", line)
    )
    script = (
        "const body = require('node:fs').readFileSync(0, 'utf8');"
        "const AsyncFunction ="
        " Object.getPrototypeOf(async function () {}).constructor;"
        "new AsyncFunction(body);"
    )
    result = subprocess.run(
        ["node", "-e", script],
        input=body,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO,
    )
    assert result.returncode == 0, result.stderr.strip()
