"""Bare-mirror + worktree lifecycle with a SHA input-drift guard.

Each golden PR is reviewed from a `git worktree` checked out of a cached bare
mirror of its repo. Before the worktree is created, the pinned head/base SHAs
(from `golden/shas.json`) are re-verified against the mirror's live pull ref so a
force-push or rebase upstream can never be scored silently — it is flagged as
input drift instead.

Stdlib-only (repo CLAUDE.md). Every git call goes through ``subprocess.run`` with
an argument list — never ``shell=True``.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

__all__ = ["DriftError", "ensure_mirror", "make_worktree", "remove_worktree"]


class DriftError(Exception):
    """A mirror's live pull ref no longer matches the pinned head/base SHAs."""


def _git(args, check=True):
    """Run ``git <args>`` capturing output. Raises CalledProcessError if check."""
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["git", *args], result.stdout, result.stderr
        )
    return result


def _mirror_dirname(clone_url):
    """A deterministic, filesystem-safe directory name for a clone URL.

    A readable slug (the repo tail) plus a short hash of the full URL, so two
    URLs that share a tail never collide onto the same cached mirror.
    """
    normalized = clone_url.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    tail = normalized.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"[^0-9A-Za-z._-]+", "-", tail).strip("-") or "repo"
    digest = hashlib.sha256(clone_url.encode("utf-8")).hexdigest()[:12]
    return f"{tail}-{digest}.git"


def ensure_mirror(clone_url, mirrors_dir, refresh=False):
    """Return the path to a cached bare mirror of ``clone_url``.

    Created with ``git clone --mirror`` on first use (this downloads the repo).
    If the mirror already exists the network is not touched unless
    ``refresh=True``, which runs ``git remote update`` to pull new refs.
    """
    clone_url = str(clone_url)
    mirror = Path(mirrors_dir) / _mirror_dirname(clone_url)
    if mirror.exists():
        if refresh:
            _git(["-C", str(mirror), "remote", "update"])
        return mirror
    Path(mirrors_dir).mkdir(parents=True, exist_ok=True)
    _git(["clone", "--mirror", clone_url, str(mirror)])
    return mirror


def make_worktree(mirror, head_sha, base_sha, base_ref, dest, pr_number):
    """Add a detached worktree at ``head_sha``, guarding against input drift.

    Verifies ``refs/pull/{pr_number}/head == head_sha`` and
    ``merge-base(head_sha, base_ref) == base_sha`` against the mirror; either
    mismatch raises :class:`DriftError` naming the PR and no worktree is created.
    """
    mirror = Path(mirror)
    dest = Path(dest)
    pull_ref = f"refs/pull/{pr_number}/head"

    actual_head = _git(["-C", str(mirror), "rev-parse", pull_ref]).stdout.strip()
    if actual_head != head_sha:
        raise DriftError(
            f"PR #{pr_number}: {pull_ref} is now {actual_head}, expected pinned "
            f"head {head_sha} -- input drift, refusing to score."
        )

    actual_base = _git(
        ["-C", str(mirror), "merge-base", head_sha, base_ref]
    ).stdout.strip()
    if actual_base != base_sha:
        raise DriftError(
            f"PR #{pr_number}: merge-base({head_sha}, {base_ref}) is now "
            f"{actual_base}, expected pinned base {base_sha} -- input drift, "
            f"refusing to score."
        )

    if dest.exists():
        # Leftover from an unclean stop (SIGKILL/OOM before the removal ran):
        # `git worktree add` would fail on the existing path, turning every
        # --resume/--retry-failed into a mirror_error. Self-heal: force-remove a
        # registered worktree, rmtree an unregistered leftover, prune stale admin.
        _git(
            ["-C", str(mirror), "worktree", "remove", "--force", str(dest)],
            check=False,
        )
        if dest.exists():
            shutil.rmtree(dest)
        _git(["-C", str(mirror), "worktree", "prune"], check=False)

    _git(["-C", str(mirror), "worktree", "add", "--detach", str(dest), head_sha])
    return dest


def remove_worktree(mirror, dest):
    """Remove a worktree with ``--force``; tolerant of an already-absent one."""
    mirror = Path(mirror)
    dest = Path(dest)
    result = _git(
        ["-C", str(mirror), "worktree", "remove", "--force", str(dest)], check=False
    )
    if result.returncode == 0:
        return
    # Already gone (e.g. a second remove, or a manually deleted dir): prune the
    # stale admin entry and swallow the error only when the path is truly absent.
    _git(["-C", str(mirror), "worktree", "prune"], check=False)
    if dest.exists():
        raise subprocess.CalledProcessError(
            result.returncode,
            ["git", "-C", str(mirror), "worktree", "remove", "--force", str(dest)],
            result.stdout,
            result.stderr,
        )
