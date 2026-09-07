"""Tests for the shared JSON result writer."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.script_io import write_result


class TestWriteResult(unittest.TestCase):
    def test_stdout_escapes_lone_surrogates_and_stays_parseable(self):
        output = io.StringIO()
        with redirect_stdout(output):
            write_result(None, {"value": "bad\ud800"})
        self.assertIn(r"\ud800", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["value"], "bad\ud800")

    def test_file_escapes_lone_surrogates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            write_result(str(path), {"value": "bad\udfff"})
            self.assertIn(r"\udfff", path.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(path.read_text())["value"], "bad\udfff")


if __name__ == "__main__":
    unittest.main()
