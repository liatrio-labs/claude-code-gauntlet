"""Leaf score identities and confined artifact IO shared across layers."""

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from bench.runner.epochs import canonical


@dataclass(frozen=True, slots=True)
class ScoreRef:
    run_id: str
    epoch_id: str
    score_id: str


def artifact_allowed(path: Path) -> bool:
    return not any(
        part in ("pairing", "auth.json", ".codex")
        or part.startswith(".env")
        or "credential" in part.lower()
        for part in path.parts
    ) and not ("backups" in path.parts and path.suffix == ".gpg")


def exclusive_json(path: Path, value: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def copy_exclusive(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)


def _pr_number(pr):
    number = next(
        (pr[k] for k in ("pr_number", "number", "n") if pr.get(k) is not None),
        _parse_url(pr.get("url", ""))[2],
    )
    if number is None:
        raise KeyError("pr dict has no pr_number/number and no parseable url")
    return number


def _parse_url(url):
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url or "")
    return (m[1], m[2], int(m[3])) if m else (None, None, None)


def _owner(pr):
    return pr.get("owner") or _parse_url(pr.get("url", ""))[0]


def _repo(pr):
    return pr.get("repo") or _parse_url(pr.get("url", ""))[1]


def _slug(part):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(part or "")).strip("-")


def pr_dir_name(pr):
    return f"pr-{_slug(_owner(pr))}-{_slug(_repo(pr))}-{_pr_number(pr)}"


def _load_dotenv_key(path, key):
    value = None
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            name, separator, raw = line.strip().partition("=")
            if separator and not name.startswith("#") and name.strip() == key:
                value = raw.strip().strip("'\"") or None
    except OSError:
        return None
    return value


def parse_result_envelope(text):
    text = (text or "").strip()
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, end = decoder.raw_decode(text[match.start() :])
        except ValueError:
            continue
        if obj.get("type") == "result" or (match.start() == 0 and end == len(text)):
            return obj
    return None


def _is_superseded(path):
    return "superseded" in Path(path).parts
