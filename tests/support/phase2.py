import re
from pathlib import Path
from typing import AnyStr

SHA = "a1b2c3d4"
ARTIFACTS = {
    f"code-gauntlet-findings-{SHA}.json": b"findings bytes",
    f"code-gauntlet-report-{SHA}.md": b"report bytes",
    "code-gauntlet-checkpoint-all-ffffffff.json": b"different sha",
    f"other-report-{SHA}.md": b"no prefix",
}
TRUNCATED = {
    f"code-gauntlet-findings-{SHA}.json",
    f"code-gauntlet-report-{SHA}.md",
}


def normalize_frame_ids(body: AnyStr) -> AnyStr:
    if isinstance(body, bytes):
        return re.sub(rb' id="[0-9a-f]{32}"', b' id="ID"', body)
    return re.sub(r' id="[0-9a-f]{32}"', ' id="ID"', body)


def seed_artifacts(output_dir: Path) -> None:
    output_dir.mkdir()
    for name, contents in ARTIFACTS.items():
        (output_dir / name).write_bytes(contents)


def write_context_inputs(
    output_dir: Path, rules: bytes | None, diff: bytes | None, *, sha: str = SHA
) -> None:
    output_dir.mkdir()
    if rules is not None:
        (output_dir / f"code-gauntlet-project-rules-{sha}.md").write_bytes(rules)
    if diff is not None:
        (output_dir / f"code-gauntlet-diff-{sha}.patch").write_bytes(diff)
