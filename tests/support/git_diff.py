"""A real history and user configuration hostile to machine-readable patches."""

import os
import subprocess


def git(directory, *args):
    return subprocess.run(
        ["git", *args],
        cwd=directory,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def patch_repo(directory, monkeypatch, config="external"):
    for key, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    git(directory, "init", "-q")
    git(directory, "config", "commit.gpgsign", "false")
    git(directory, "config", "core.hooksPath", os.devnull)
    source = directory / "file"
    source.write_text("old\n", encoding="utf-8", newline="\n")
    git(directory, "add", "file")
    git(directory, "commit", "-qm", "base")
    base = git(directory, "rev-parse", "HEAD")
    source.write_text("new\n", encoding="utf-8", newline="\n")
    git(directory, "add", "file")
    git(directory, "commit", "-qm", "target")
    target = git(directory, "rev-parse", "HEAD")
    source.write_text("local\n", encoding="utf-8", newline="\n")
    git(directory, "config", "color.ui", "always")
    git(directory, "config", "diff.mnemonicPrefix", "true")
    if config == "external":
        git(directory, "config", "diff.external", "git --version")
    elif config == "noprefix":
        git(directory, "config", "diff.noprefix", "true")
    return base, target


def assert_standard_patch(patch):
    assert patch.startswith(b"diff --git a/file b/file\n")
    assert b"\x1b" not in patch
    assert b"--- a/file\n+++ b/file\n@@ " in patch
