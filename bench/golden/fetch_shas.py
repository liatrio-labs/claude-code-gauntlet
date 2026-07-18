#!/usr/bin/env python3
"""Fetch and pin per-PR head/base SHAs for the 50 golden PRs (Task 6).

Reads golden urls from ``bench/golden/benchmark_data.min.json``, resolves each
via ``gh api repos/{owner}/{repo}/pulls/{n}``, and writes
``bench/golden/shas.json`` keyed by golden url. Fork-org PRs
(owner == ``ai-code-review-evaluation``) are only resolvable while those forks
exist; any unfetchable PR is written with ``"missing"`` markers for owner
triage, listed at the end, and the script exits 1.

``base_sha`` is pinned to the TRUE branch point — ``merge-base(head, base_ref)``
— not to the ``pulls`` API's ``base.sha``. That ``base.sha`` is the base branch's
tip *at fetch time*: a moving target that advances every time the base branch
gets a new commit, so a diff taken against it silently grows to include unrelated
downstream changes. The drift guard the runner applies verifies
``base_sha == merge-base(head, base_ref)`` (the stable common ancestor the PR was
actually branched from and diffed against), so pinning the tip makes every later
run fail that check — four early pins had to be hand-repaired for exactly this
reason. To pin the stable value we call the compare API
(``repos/{owner}/{repo}/compare/{base_ref}...{head_sha}``) after resolving
``head.sha``/``base.ref`` and record its ``merge_base_commit.sha`` as
``base_sha``. The moving tip is kept as ``base_sha_api`` for provenance.

stdlib + ``gh`` CLI via subprocess (no ``shell=True``).

Output shape (per url):
    {"owner", "repo", "pr_number", "head_sha", "base_sha", "base_sha_api",
     "base_ref", "fork"}
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


def _gh_api_json(endpoint, jq, run=None):
    """Return parsed ``gh api`` JSON for ``endpoint`` (via ``jq``); RuntimeError on final failure.

    Retries transient failures with exponential backoff (2s, then 4s). ``run`` is
    injectable (defaults to ``subprocess.run``) so tests drive the fetcher with no
    ``gh`` and no network.
    """
    runner = run or subprocess.run
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            proc = runner(
                ["gh", "api", endpoint, "--jq", jq],
                capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return json.loads(proc.stdout)
            last_err = (proc.stderr or proc.stdout).strip()[:200] or f"exit {proc.returncode}"
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            last_err = str(exc)
        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)  # 2s, then 4s
    raise RuntimeError(last_err or "unknown error")


def fetch_one(owner, repo, pr_number, run=None):
    """Return (head_sha, base_sha, base_ref, base_sha_api); RuntimeError on final failure.

    ``base_sha`` is ``merge-base(head, base_ref)`` from the compare API — the stable
    branch point the drift guard verifies — while ``base_sha_api`` is the ``pulls``
    ``base.sha`` (base-branch tip at fetch time), kept only for provenance.
    """
    pull = _gh_api_json(
        f"repos/{owner}/{repo}/pulls/{pr_number}",
        "{head_sha: .head.sha, base_sha: .base.sha, base_ref: .base.ref}",
        run=run,
    )
    head, base_tip, ref = pull.get("head_sha"), pull.get("base_sha"), pull.get("base_ref")
    if not (head and base_tip and ref):
        raise RuntimeError(f"incomplete pulls response: {json.dumps(pull)[:200]}")

    compare = _gh_api_json(
        f"repos/{owner}/{repo}/compare/{ref}...{head}",
        "{merge_base_sha: .merge_base_commit.sha}",
        run=run,
    )
    merge_base = compare.get("merge_base_sha")
    if not merge_base:
        raise RuntimeError(f"incomplete compare response: {json.dumps(compare)[:200]}")
    return head, merge_base, ref, base_tip


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
            head, base, ref, base_api = fetch_one(owner, repo, pr_number)
            entry["head_sha"] = head
            entry["base_sha"] = base
            entry["base_sha_api"] = base_api
            entry["base_ref"] = ref
        except RuntimeError as exc:
            entry["head_sha"] = "missing"
            entry["base_sha"] = "missing"
            entry["base_sha_api"] = "missing"
            entry["base_ref"] = "missing"
            missing.append((url, str(exc)))
            print(f"UNFETCHABLE {url}: {exc}", file=sys.stderr)
        out[url] = entry

    with open(GOLDEN / "shas.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")  # single trailing newline: pre-commit's end-of-file-fixer convention

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
