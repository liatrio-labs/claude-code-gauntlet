"""Normalize temporary paths in captured CLI text."""

import os
import re
from os import PathLike


def normalize_path_text(
    text: str, directory: str | PathLike[str], replacement: str
) -> str:
    """Replace a directory and its following separator across path encodings."""
    root = os.fspath(directory).rstrip("/\\")
    path_forms = {
        root,
        root.replace("\\", "/"),
        root.replace("/", os.sep),
    }
    for path_form in sorted(path_forms, key=len, reverse=True):
        if not path_form:
            continue
        for slash_count in (4, 2, 1):
            spelling = path_form.replace("\\", "\\" * slash_count)
            separators = {"/", "\\" * slash_count}
            suffix = "|".join(
                re.escape(separator)
                for separator in sorted(separators, key=len, reverse=True)
            )
            pattern = re.escape(spelling) + (
                rf"(?:(?P<separator>{suffix})|(?=$|[\s'\"),\]}}:]))"
            )
            text = re.sub(
                pattern,
                lambda match: replacement + ("/" if match.group("separator") else ""),
                text,
            )
    return text
