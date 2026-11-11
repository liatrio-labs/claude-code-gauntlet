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


def fact_card_payload(content: bytes | str) -> bytes:
    raw = content.encode() if isinstance(content, str) else content
    match = re.search(
        rb'kind="fact-cards"[^>]*>\n(.*?)\n</untrusted-code-content', raw, re.S
    )
    assert match is not None, "prepared context has no fact-card frame"
    return match[1]


def discovery_plan_payload(receipt, output_dir):
    import hashlib
    import json

    assert "discoveryPlan" not in receipt
    ref = receipt["discoveryPlanRef"]
    assert set(ref) == {"path", "sha256"}
    assert Path(ref["path"]).name == ref["path"]
    data = (Path(output_dir) / ref["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == ref["sha256"]
    return json.loads(data)["plan"]
