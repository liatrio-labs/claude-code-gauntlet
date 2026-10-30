"""Repository classification facts bound to the reviewed source capture."""

from __future__ import annotations

import os
import re

from gauntlet import proc
from gauntlet.diff import AnnotatedDiff
from gauntlet.registry import EXCLUSION_CATEGORIES, ScopeFacts
from gauntlet.source_capture import SourceAdapter

ATTRIBUTES = ("linguist-generated", "linguist-vendored", "linguist-documentation")
GENERATED_MARKER = re.compile(
    r"@generated\b|\b(?:automatically generated|auto-generated|generated (?:file|code)|do not edit)\b",
    re.IGNORECASE,
)


def repository_scope(
    source: SourceAdapter, indexed: AnnotatedDiff | None, files: list[str]
) -> dict[str, ScopeFacts]:
    capture = source.source_capture
    if capture is None or indexed is None:
        return {}
    paths = sorted(set(files) | set(indexed.files))
    attributes: dict[str, dict[str, bool | None]] = {}
    try:
        result = proc.run_bytes(
            [
                "git",
                "-c",
                f"core.attributesFile={os.devnull}",
                "check-attr",
                f"--source={capture.manifest['head']}",
                "-z",
                *ATTRIBUTES,
                "--",
                *paths,
            ],
            cwd=str(source.root),
            timeout=10,
        )
        if result.returncode == 0:
            tokens = result.stdout.decode("utf-8", "surrogateescape").split("\0")
            for i in range(0, len(tokens) - 3, 3):
                file, attribute, value = tokens[i : i + 3]
                attributes.setdefault(file, {})[attribute] = {
                    "set": True,
                    "true": True,
                    "unset": False,
                    "false": False,
                }.get(value)
    except (OSError, UnicodeError, proc.TimeoutExpired):
        pass
    documentation = [
        attributes.get(file, {}).get("linguist-documentation") for file in indexed.files
    ]
    states = set(documentation)
    all_docs = False if False in states else True if states == {True} else None
    basenames = next(
        row["basenames"] for row in EXCLUSION_CATEGORIES if "basenames" in row
    )
    facts: dict[str, ScopeFacts] = {}
    for file in files:
        lines = source.read(file)
        attrs = attributes.get(file, {})
        lockfile = file.rsplit("/", 1)[-1] in basenames
        facts[file] = {
            "file": file,
            "head": capture.manifest["head"],
            "snapshot_id": capture.receipt["manifest_sha256"],
            "linguist_generated": attrs.get("linguist-generated"),
            "linguist_vendored": attrs.get("linguist-vendored"),
            "linguist_documentation": attrs.get("linguist-documentation"),
            "generated_marker": None
            if lines is None
            else any(GENERATED_MARKER.search(line) for line in lines[:10]),
            "all_changed_files_documentation": all_docs,
            "lockfile_basename": lockfile,
            "mechanical_change": None if lines is None else lockfile,
        }
    return facts
