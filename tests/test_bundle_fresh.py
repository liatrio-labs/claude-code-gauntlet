import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "workflows" / "pipeline.js"
BUILD = REPO / "workflows" / "build.js"
PARSE_GATE = REPO / "workflows" / "test" / "tools" / "parse_gate.mjs"


class TestBundleFresh(unittest.TestCase):
    def test_bundle_is_byte_identical_to_fresh_build(self):
        before = BUNDLE.read_bytes()
        subprocess.run(["node", str(BUILD)], check=True, cwd=REPO)
        after = BUNDLE.read_bytes()
        self.assertEqual(before, after, "workflows/pipeline.js is stale — run `node workflows/build.js`")

    def test_bundle_has_no_module_imports(self):
        text = BUNDLE.read_text()
        for line in text.splitlines():
            self.assertFalse(line.strip().startswith("import "), f"bundle contains an import: {line!r}")
            self.assertNotIn("require(", line)

    def test_bundle_begins_with_meta_and_exposes_version(self):
        text = BUNDLE.read_text()
        first = next(line for line in text.splitlines() if line.strip())
        self.assertTrue(first.startswith("export const meta"), f"bundle must begin with meta, got {first!r}")
        self.assertIn("PIPELINE_VERSION", text)

    def test_bundle_meta_has_nonempty_description(self):
        text = BUNDLE.read_text()
        first = next(line for line in text.splitlines() if line.strip())
        match = re.search(r"description:\s*'([^']*)'", first)
        self.assertIsNotNone(match, f"meta literal must include a description field, got {first!r}")
        self.assertTrue(match.group(1).strip(), "meta.description must be a non-empty string")

    def test_bundle_has_no_export_besides_meta(self):
        # The workflow runtime rejects any `export` keyword other than the meta
        # literal (e.g. `export default` is a runtime SyntaxError), so exactly one
        # line may start with `export` — everything else must be stripped by build.js.
        text = BUNDLE.read_text()
        export_stmt_lines = [line for line in text.splitlines() if re.match(r"^\s*export\b", line)]
        self.assertEqual(
            len(export_stmt_lines), 1,
            f"bundle must contain exactly one `export` statement (the meta literal), found: {export_stmt_lines!r}",
        )
        self.assertTrue(export_stmt_lines[0].strip().startswith("export const meta"))

    def test_bundle_body_compiles_as_async_function(self):
        # PARSE GATE: the runtime wraps the meta-stripped bundle body in an async
        # function. A top-level identifier collision across concatenated modules
        # (two `const SEVERITY_ORDER`) is a compile-time SyntaxError there — the
        # exact defect that shipped in the committed bundle and crashed the live
        # smoke run. Compile it with the AsyncFunction constructor and assert it
        # does not throw. Fails against a colliding bundle, passes once single-owned.
        result = subprocess.run(
            ["node", str(PARSE_GATE), str(BUNDLE)],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"bundle body failed to compile as an async function: {result.stderr.strip()}",
        )

    def test_bundle_ends_with_top_level_run_invocation(self):
        # The runtime executes the bundle body as a wrapped async function with no
        # `export default` entry — the contract is a trailing top-level `return`.
        text = BUNDLE.read_text()
        stripped_lines = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(stripped_lines, "bundle is empty")
        last = stripped_lines[-1].strip()
        self.assertTrue(
            last.startswith("return await run("),
            f"bundle must end with a top-level `return await run(`, got {last!r}",
        )


if __name__ == "__main__":
    unittest.main()
