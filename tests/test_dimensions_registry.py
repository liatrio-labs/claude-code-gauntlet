import json
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class TestDimensionsRegistry(unittest.TestCase):
    def test_claude_md_dimension_list_matches_registry(self):
        node_src = (
            "import('./workflows/src/registry.js').then(m => "
            "console.log(JSON.stringify([...new Set(m.DIMENSIONS.map(d => d.dimension))].sort())))"
        )
        out = subprocess.run(["node", "--input-type=module", "-e", node_src],
                             cwd=REPO, capture_output=True, text=True, check=True)
        registry_dims = set(json.loads(out.stdout))
        claude_md = (REPO / "CLAUDE.md").read_text()
        # CLAUDE.md "Findings schema" lists the dimensions on the bullet line that
        # contains "short name from agent output", each as a `"name"` token. The
        # leading token is `- `dimension`` (with backticks), so splitting on the
        # bare string "dimension —" would not match — grab the whole line instead.
        line = next(l for l in claude_md.splitlines() if "short name from agent output" in l)
        listed = set(re.findall(r'`"(\w+)"`', line))
        self.assertEqual(registry_dims, listed,
                         f"registry {registry_dims} != CLAUDE.md {listed}")


if __name__ == "__main__":
    unittest.main()
