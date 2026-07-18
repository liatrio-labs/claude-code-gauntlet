#!/usr/bin/env python3
"""Fetch and pin per-PR head/base SHAs for the 50 golden PRs (Task 6).

Reads golden urls from ``bench/golden/benchmark_data.min.json``, resolves each
via ``gh api repos/{owner}/{repo}/pulls/{n}``, and writes
``bench/golden/shas.json`` keyed by golden url. Fork-org PRs
(owner == ``ai-code-review-evaluation``) are only resolvable while those forks
exist; any unfetchable PR is written with ``"missing"`` markers for owner
triage, listed at the end, and the script exits 1.

stdlib + ``gh`` CLI via subprocess (no ``shell=True``).

Output shape (per url):
    {"owner", "repo", "pr_number", "head_sha", "base_sha", "base_ref", "fork"}
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent
FORK_ORG = "ai-code-review-evaluation"
URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)$")
MAX_RETRIES = 3
GH_TIMEOUT_S = 30


def parse_url(url):
    m = URL_RE.match(url)
    if not m:
        raise ValueError(f"unparseable golden url: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def fetch_one(owner, repo, pr_number):
    """Return (head_sha, base_sha, base_ref); raise RuntimeError on final failure."""
    endpoint = f"repos/{owner}/{repo}/pulls/{pr_number}"
    jq = "{head_sha: .head.sha, base_sha: .base.sha, base_ref: .base.ref}"
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            proc = subprocess.run(
                ["gh", "api", endpoint, "--jq", jq],
                capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                head, base, ref = data.get("head_sha"), data.get("base_sha"), data.get("base_ref")
                if head and base and ref:
                    return head, base, ref
                last_err = f"incomplete response: {proc.stdout.strip()[:200]}"
            else:
                last_err = (proc.stderr or proc.stdout).strip()[:200] or f"exit {proc.returncode}"
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            last_err = str(exc)
        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)  # 2s, then 4s
    raise RuntimeError(last_err or "unknown error")


def main():
    golden_urls = sorted(json.load(open(GOLDEN / "benchmark_data.min.json")).keys())
    out = {}
    missing = []
    for url in golden_urls:
        owner, repo, pr_number = parse_url(url)
        entry = {
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "fork": owner == FORK_ORG,
        }
        try:
            head, base, ref = fetch_one(owner, repo, pr_number)
            entry["head_sha"] = head
            entry["base_sha"] = base
            entry["base_ref"] = ref
        except RuntimeError as exc:
            entry["head_sha"] = "missing"
            entry["base_sha"] = "missing"
            entry["base_ref"] = "missing"
            missing.append((url, str(exc)))
            print(f"UNFETCHABLE {url}: {exc}", file=sys.stderr)
        out[url] = entry

    with open(GOLDEN / "shas.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.write("\n")

    fetched = len(out) - len(missing)
    print(f"fetched {fetched}/{len(out)} PR SHAs; wrote {GOLDEN / 'shas.json'}")
    if missing:
        print(f"\n{len(missing)} unfetchable PR(s):", file=sys.stderr)
        for url, err in missing:
            print(f"  {url}  ({err})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
