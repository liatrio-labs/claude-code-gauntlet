import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "workflows" / "pipeline.js"
BUILD = REPO / "workflows" / "build.js"


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


if __name__ == "__main__":
    unittest.main()
