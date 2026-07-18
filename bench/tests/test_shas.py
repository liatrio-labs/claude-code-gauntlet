"""Offline integrity tests for pinned per-PR SHAs (Task 6).

stdlib only, no network. Validates shas.json against subsets.json: every subset
PR has a complete, well-formed entry, and fork flags are URL-consistent. Also
drives ``fetch_shas.fetch_one`` through an injected ``gh`` runner (no network) to
prove ``base_sha`` is pinned to the compare-API merge-base and the moving tip is
kept as ``base_sha_api``.
"""
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bench.golden import fetch_shas

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

    def test_every_entry_has_base_sha_api_and_40hex_base(self):
        # The fetcher pins base_sha to the compare-API merge-base and records the
        # pulls base.sha tip as base_sha_api on every entry. For a successfully
        # fetched entry both are 40-hex; a "missing" entry carries the marker in
        # all three sha fields together.
        for url, entry in self.shas.items():
            self.assertIn("base_sha_api", entry, f"{url} missing base_sha_api")
            if entry["base_sha"] == "missing":
                self.assertEqual(entry["base_sha_api"], "missing", f"{url} half-missing")
                continue
            self.assertRegex(entry["base_sha"], SHA_RE, f"{url} bad base_sha")
            self.assertRegex(entry["base_sha_api"], SHA_RE, f"{url} bad base_sha_api")

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


class TestFetchOne(unittest.TestCase):
    """Drive fetch_one through an injected gh runner (no network, no gh)."""

    HEAD = "a" * 40
    TIP = "b" * 40
    MERGE_BASE = "c" * 40

    def _runner(self, calls):
        def run(argv, **kwargs):
            calls.append(argv)
            endpoint = argv[2]
            if "/pulls/" in endpoint:
                out = json.dumps(
                    {"head_sha": self.HEAD, "base_sha": self.TIP, "base_ref": "main"}
                )
            elif "/compare/" in endpoint:
                out = json.dumps({"merge_base_sha": self.MERGE_BASE})
            else:
                raise AssertionError(f"unexpected endpoint {endpoint}")
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        return run

    def test_records_merge_base_as_base_sha_and_tip_as_api(self):
        calls = []
        head, base_sha, ref, base_sha_api = fetch_shas.fetch_one(
            "o", "r", 1, run=self._runner(calls)
        )
        self.assertEqual(head, self.HEAD)
        self.assertEqual(base_sha, self.MERGE_BASE)     # merge-base is the pinned base
        self.assertEqual(base_sha_api, self.TIP)        # moving tip kept for provenance
        self.assertEqual(ref, "main")

    def test_compare_uses_base_ref_and_head(self):
        calls = []
        fetch_shas.fetch_one("o", "r", 1, run=self._runner(calls))
        compare = [c for c in calls if "/compare/" in c[2]]
        self.assertEqual(len(compare), 1, "exactly one compare call")
        self.assertIn(f"main...{self.HEAD}", compare[0][2])

    def test_compare_failure_raises(self):
        def run(argv, **kwargs):
            endpoint = argv[2]
            if "/pulls/" in endpoint:
                out = json.dumps(
                    {"head_sha": self.HEAD, "base_sha": self.TIP, "base_ref": "main"}
                )
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="compare boom")
        with mock.patch.object(fetch_shas.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError):
                fetch_shas.fetch_one("o", "r", 1, run=run)


if __name__ == "__main__":
    unittest.main()
