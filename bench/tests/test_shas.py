"""Offline integrity tests for pinned per-PR SHAs (Task 6).

stdlib only, no network. Validates shas.json against subsets.json: every subset
PR has a complete, well-formed entry, and fork flags are URL-consistent.
"""
import json
import re
import unittest
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent.parent / "golden"
FORK_ORG = "ai-code-review-evaluation"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _load(name):
    with open(GOLDEN / name, encoding="utf-8") as fh:
        return json.load(fh)


class TestShas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shas = _load("shas.json")
        cls.subsets = _load("subsets.json")
        cls.subset_urls = (
            set(cls.subsets["gate"])
            | set(cls.subsets["holdout"])
            | set(cls.subsets["smoke"])
            | set(cls.subsets["review_md_fixtures"])
        )

    def test_every_subset_url_has_complete_entry(self):
        for url in sorted(self.subset_urls):
            self.assertIn(url, self.shas, f"no shas.json entry for {url}")
            entry = self.shas[url]
            for field in ("owner", "repo", "pr_number", "head_sha", "base_sha", "base_ref", "fork"):
                self.assertIn(field, entry, f"{url} missing {field}")
            for field in ("head_sha", "base_sha", "base_ref"):
                self.assertNotEqual(entry[field], "missing", f"{url} has missing {field}")

    def test_subset_shas_are_40_hex(self):
        for url in sorted(self.subset_urls):
            entry = self.shas[url]
            self.assertRegex(entry["head_sha"], SHA_RE, f"{url} bad head_sha")
            self.assertRegex(entry["base_sha"], SHA_RE, f"{url} bad base_sha")

    def test_fork_flag_consistent_with_url_org(self):
        for url, entry in self.shas.items():
            self.assertEqual(
                entry["fork"],
                entry["owner"] == FORK_ORG,
                f"{url} fork flag inconsistent with owner {entry['owner']}",
            )

    def test_owner_repo_pr_match_url(self):
        for url, entry in self.shas.items():
            expected = f"https://github.com/{entry['owner']}/{entry['repo']}/pull/{entry['pr_number']}"
            self.assertEqual(url, expected, f"{url} owner/repo/pr fields disagree with key")


if __name__ == "__main__":
    unittest.main()
