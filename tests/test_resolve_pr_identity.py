"""Tests for the stdlib PR/MR identity producer."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "resolve_pr_identity.py"
SHA = "0123456789abcdef0123456789abcdef01234567"


def run_identity(platform, url, sha=SHA, title=None):
    command = [
        sys.executable,
        str(SCRIPT),
        "--platform",
        platform,
        "--url",
        url,
        "--sha",
        sha,
    ]
    if title is not None:
        command.extend(["--title", title])
    return subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=10)


class TestResolvePrIdentity(unittest.TestCase):
    def test_github_and_enterprise_urls(self):
        # Mutation: delete the github PATH_PATTERNS row; both hand-typed outputs turn red.
        github = run_identity(
            "github",
            "https://github.com/OpenAI/codex/pull/278",
            title="A caf\N{LATIN SMALL LETTER E WITH ACUTE} fix",
        )
        self.assertEqual(github.returncode, 0, github.stderr)
        self.assertEqual(
            json.loads(github.stdout),
            {
                "owner": "OpenAI",
                "repo": "codex",
                "pr_number": 278,
                "sha_full": SHA,
                "platform": "github",
                "web_origin": "https://github.com",
                "title": "A caf\N{LATIN SMALL LETTER E WITH ACUTE} fix",
            },
        )
        self.assertIn(r"\u00e9", github.stdout)

        enterprise = run_identity(
            "github", "https://Git.Example.COM:8443/acme/widget/pull/7/?ignored=yes"
        )
        self.assertEqual(enterprise.returncode, 0, enterprise.stderr)
        self.assertEqual(
            json.loads(enterprise.stdout),
            {
                "owner": "acme",
                "repo": "widget",
                "pr_number": 7,
                "sha_full": SHA,
                "platform": "github",
                "web_origin": "https://git.example.com:8443",
            },
        )

    def test_gitlab_and_nested_self_hosted_urls(self):
        # Mutation: delete the gitlab PATH_PATTERNS row; nested owners stop resolving.
        public = run_identity(
            "gitlab", "https://gitlab.com/acme/widget/-/merge_requests/281"
        )
        self.assertEqual(public.returncode, 0, public.stderr)
        self.assertEqual(json.loads(public.stdout)["owner"], "acme")
        self.assertEqual(json.loads(public.stdout)["pr_number"], 281)

        nested = run_identity(
            "gitlab",
            "http://GitLab.Example:8080/group/sub/widget/-/merge_requests/9/?view=parallel",
        )
        self.assertEqual(nested.returncode, 0, nested.stderr)
        self.assertEqual(
            json.loads(nested.stdout),
            {
                "owner": "group/sub",
                "repo": "widget",
                "pr_number": 9,
                "sha_full": SHA,
                "platform": "gitlab",
                "web_origin": "http://gitlab.example:8080",
            },
        )

    def test_wrong_platform_bad_sha_and_blank_title_are_handled(self):
        # Mutation: delete SHA_FULL_RE or use an unanchored path match; these refusals turn green.
        wrong = run_identity("github", "https://gitlab.com/a/r/-/merge_requests/3")
        self.assertEqual(wrong.returncode, 2)
        self.assertRegex(wrong.stderr, r"^PR IDENTITY ERROR: .+\n$")
        for sha in ("a" * 39, "A" * 40):
            bad_sha = run_identity("github", "https://github.com/a/r/pull/3", sha=sha)
            self.assertEqual(bad_sha.returncode, 2)
            self.assertEqual(
                bad_sha.stderr,
                "PR IDENTITY ERROR: sha must be a 40-character lowercase hex commit id\n",
            )
        blank = run_identity("github", "https://github.com/a/r/pull/3/", title="   ")
        self.assertEqual(blank.returncode, 0, blank.stderr)
        self.assertNotIn("title", json.loads(blank.stdout))


if __name__ == "__main__":
    unittest.main()
