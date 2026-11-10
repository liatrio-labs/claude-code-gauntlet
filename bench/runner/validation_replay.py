"""Frozen Validate inputs, Workflow dispatch, and score attribution for the bench."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, TypedDict

from bench.runner import invoke
from bench.runner.contracts import artifact_allowed
from bench.runner.mirrors import _mirror_dirname

DRIVER = Path(__file__).with_name("validate_replay.mjs")


class ReplayPayloadWire(TypedDict):
    args: dict[str, Any]
    findings: list[dict[str, Any]]
    limits: dict[str, Any]
    resolvedReview: dict[str, Any]
    recordedAudit: dict[str, Any]
    recordedFilter: list[dict[str, Any]]
    verificationDispatches: list[str]


class ReplayManifestWire(TypedDict):
    source_run: str
    score_id: str
    treatment_tree: str
    treatment_hashes: dict[str, str]
    child_model: str
    child_effort: str
    child_auth: Literal["api", "subscription"]
    replays: int
    dry_run: bool


class DispatchWire(TypedDict):
    prompt: str
    options: dict[str, Any]
    result: Any


@dataclass(frozen=True, slots=True)
class SourcePR:
    url: str
    identity: dict[str, Any]
    payload: ReplayPayloadWire
    artifacts: tuple[tuple[str, Path], ...]
    bugs: tuple[tuple[str, tuple[str, ...]], ...]
    hashes: dict[str, str]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def node(operation, request):
    with TemporaryDirectory(prefix="validate-replay-") as temporary:
        src, dest = Path(temporary) / "input.json", Path(temporary) / "output.json"
        write_json(src, request)
        subprocess.run(
            ["node", str(DRIVER), operation, str(src), str(dest)], check=True
        )
        return read_json(dest)


def confined(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or not artifact_allowed(path):
        raise ValueError(f"invalid source artifact path: {relative}")
    return path


def load_sources(run, score_id, pr_urls=()):
    packets = list((run / "scores").glob(f"*/{score_id}"))
    if len(packets) != 1:
        raise ValueError("score id must identify one score packet")
    packet = packets[0]
    if not (packet / "complete.json").is_file():
        raise ValueError("source score is incomplete")
    metadata = read_json(packet / "sources/run.json")
    bindings = read_json(packet / "stage_trace_bindings.json")
    observations = [
        json.loads(line) for line in (packet / "stages.jsonl").read_text().splitlines()
    ]
    if pr_urls and set(pr_urls) - bindings.keys():
        raise ValueError("requested PR is absent from the source score")
    sources = []
    for url, binding in sorted(bindings.items()):
        if pr_urls and url not in pr_urls:
            continue
        attempt = binding["attempt_id"]
        manifest = read_json(packet / "attempts" / (attempt + ".json"))
        if manifest["pr_url"] != url:
            raise ValueError("source attempt identity mismatch")
        roots = list((run / "attempts").glob(f"*/{attempt}"))
        archived = confined(packet, str(Path(binding["trace_path"]).parent.parent))
        root = roots[0] if len(roots) == 1 else archived
        hashes = manifest["source_artifact_hashes"]
        artifacts = {}
        for relative, expected in hashes.items():
            path = confined(root, relative)
            if path.is_file():
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError(f"source artifact hash mismatch: {relative}")
                artifacts[relative] = path
        records = [
            read_json(path)
            for name, path in artifacts.items()
            if "/workflows/wf_" in name
        ]
        records = [
            row
            for row in records
            if row.get("result", {}).get("ok") and row.get("args", {}).get("nonce")
        ]
        if len(records) != 1:
            raise ValueError(f"expected one completed source workflow: {url}")
        record = records[0]
        trace_path = confined(packet, binding["trace_path"])
        if (
            hashlib.sha256(trace_path.read_bytes()).hexdigest()
            != binding["trace_sha256"]
        ):
            raise ValueError("score trace hash mismatch")
        trace = read_json(trace_path)
        checkpoints = [
            p
            for name, p in artifacts.items()
            if "/code-gauntlet-checkpoint-all-" in name
        ]
        if len(checkpoints) != 1:
            raise ValueError("missing source checkpoint")
        outputs = {}
        inputs = []
        for name, path in artifacts.items():
            if "phase4-input-" in name:
                inputs.extend(read_json(path)["findings"])
            match = re.search(
                r"phase4-output-.*\.(slice\d+(?:\.r[12](?:\.\d+)?)?)\.json$", name
            )
            if match:
                outputs[match[1]] = read_json(path)
        payload = node(
            "reconstruct",
            dict(
                record=record,
                trace=trace,
                checkpoint=read_json(checkpoints[0]),
                verifyOutputs=outputs,
                verifyInputs=inputs,
            ),
        )
        by_version = {
            row["version_id"]: row["finding"]["id"] for row in trace["versions"]
        }
        rows = [
            row
            for row in observations
            if row["pr_url"] == url and row["stage"] == "verify"
        ]
        expected_bugs = {row["bug_id"] for row in observations if row["pr_url"] == url}
        if {row["bug_id"] for row in rows} != expected_bugs:
            raise ValueError("missing verify-stage known bug attribution")
        verify_versions = set(
            next(
                row["finding_version_ids"]
                for row in trace["snapshots"]
                if row["stage"] == "verify"
            )
        )
        bugs = []
        for row in rows:
            if (
                row["attempt_id"] != attempt
                or row["score_ref"]["score_id"] != score_id
                or row["coverage"] != "complete"
                or not set(row["matched_version_ids"]).issubset(verify_versions)
            ):
                raise ValueError("incomplete or mismatched source attribution")
            bugs.append(
                (
                    row["bug_id"],
                    tuple(by_version[v] for v in row["matched_version_ids"]),
                )
            )
        if len({bug for bug, _ in bugs}) != len(bugs):
            raise ValueError("duplicate known bug attribution")
        identity = metadata["pr_identities"][url]
        if not identity["head_sha"].startswith(payload["args"]["headShaShort"]):
            raise ValueError("source head identity mismatch")
        sources.append(
            SourcePR(
                url, identity, payload, tuple(artifacts.items()), tuple(bugs), hashes
            )
        )
    if not sources:
        raise ValueError("source score contains no bound PRs")
    return sources


def relocate(source, dest, tree):
    args = source.payload["args"]
    output = dest / "context"
    output.mkdir()
    replacements = {
        args["outputDir"]: str(output),
        args["repoRoot"]: str(dest / "worktree"),
        args["pluginRoot"]: str(tree),
    }

    def rewrite(text):
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    prefixes = (
        "code-gauntlet-context-",
        "code-gauntlet-context-map-",
        "code-gauntlet-diff-",
        "code-gauntlet-files-",
        "code-gauntlet-project-rules-",
        "code-gauntlet-inputs-",
    )
    for relative, path in source.artifacts:
        local = Path(relative).relative_to("artifacts")
        if not local.parts[0].startswith(prefixes):
            continue
        target = output / local
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".patch":
            target.write_bytes(path.read_bytes())
        else:
            target.write_text(
                rewrite(path.read_text(encoding="utf-8")), encoding="utf-8"
            )
    for name in (
        f"code-gauntlet-context-{args['headShaShort']}.md",
        f"code-gauntlet-diff-{args['headShaShort']}.patch",
        f"code-gauntlet-files-{args['headShaShort']}.json",
    ):
        if not (output / name).is_file():
            raise ValueError(f"missing source context artifact {name}")
    payload = json.loads(rewrite(json.dumps(source.payload)))
    context = (output / f"code-gauntlet-context-{args['headShaShort']}.md").read_text()
    # Match the collector's newline and decoded-code-point receipt after relocation.
    payload["args"].update(
        contextLines=context.count("\n"),
        contextChars=len(context),
        contextMaxLineChars=max(map(len, context.split("\n"))) + 1,
    )
    sidecar = output / f"code-gauntlet-context-map-{args['headShaShort']}.json"
    if sidecar.is_file():
        mapping = read_json(sidecar)
        mapping["context_sha256"] = hashlib.sha256(
            (output / f"code-gauntlet-context-{args['headShaShort']}.md").read_bytes()
        ).hexdigest()
        write_json(sidecar, mapping)
    return payload


def checkout_commands(source, mirrors, dest):
    meta = source.identity
    url = f"https://github.com/{meta['owner']}/{meta['repo']}.git"
    mirror = mirrors / _mirror_dirname(url)
    if not mirror.is_dir():
        raise ValueError(f"missing local mirror: {mirror}")
    # A local shared clone keeps worktree administration out of the cached mirror.
    local = dest / "repository.git"
    return [
        ["git", "clone", "--bare", "--shared", str(mirror), str(local)],
        [
            "git",
            "-C",
            str(local),
            "worktree",
            "add",
            "--detach",
            str(dest / "worktree"),
            meta["head_sha"],
        ],
    ]


def child_command(script, tree, model, effort):
    prompt = (
        f"Call Workflow with scriptPath={json.dumps(str(script))} and args={{}}. "
        "Wait for completion. Do not read, rewrite or transcribe the script or its results. "
        "Return only a short completion status."
    )
    return [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        # The child only launches one Workflow whose validators read and search, so the
        # tools that write, execute or reach the network stay off even with prompts skipped.
        "--disallowedTools",
        "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch",
        "--plugin-dir",
        str(tree),
        "--model",
        model,
        "--effort",
        effort,
    ]


def workflow_result(directory, script):
    records = []
    for path in (directory / "workflows").glob("wf_*.json"):
        record = read_json(path)
        recorded, _, _ = invoke.workflow_record_fields(record)
        if recorded and Path(recorded).resolve() == script.resolve():
            records.append(record)
    if len(records) != 1 or records[0].get("status") != "completed":
        raise ValueError("missing, ambiguous or failed replay Workflow record")
    if records[0].get("script") != script.read_text(encoding="utf-8"):
        raise ValueError("replay Workflow script bytes differ")
    result = records[0].get("result")
    if not isinstance(result, dict) or result.get("kind") != "bench-validate-replay":
        raise ValueError("invalid replay Workflow return")
    return result


def launch(command, source, dest, auth, timeout):
    pr = {
        "owner": source.identity["owner"],
        "repo": source.identity["repo"],
        "pr_number": source.identity["pr_number"],
    }
    env = invoke.build_env(pr, dest, os.environ, child_auth=auth)
    invoke._seed_trust(Path(env["CLAUDE_CONFIG_DIR"]), dest / "worktree")
    home = Path(env["CLAUDE_CONFIG_DIR"]).parent
    if auth == "subscription" and invoke.api_key_helper_files(home):
        raise ValueError("isolated Claude settings contain an apiKeyHelper")
    baseline = invoke.snapshot_workflow_records(home)
    with (dest / "raw.json").open("w") as output:
        process = subprocess.Popen(
            command,
            cwd=dest / "worktree",
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        finally:
            invoke.collect_workflow_records(home, dest, baseline)
    if code:
        raise ValueError(f"replay child exited {code}; see {dest / 'raw.json'}")


def summarize(source, result):
    findings = result["findings"]
    passed = {f["id"] for f in findings if f["passes_filter"]}
    attributed = {fid for _, ids in source.bugs for fid in ids}
    bugs = {bug: bool(passed.intersection(ids)) for bug, ids in source.bugs}
    for finding in findings:
        finding["matched_bug_ids"] = [
            bug for bug, ids in source.bugs if finding["id"] in ids
        ]
    result["summary"] = {
        "known_bugs": len(bugs),
        "known_bugs_passing": sum(bugs.values()),
        "bug_outcomes": bugs,
        "unmatched_passing": len(passed - attributed),
        "findings": len(findings),
        "passing": len(passed),
        "filtered_out": len(findings) - len(passed),
    }
    return result


def spreads(results):
    groups = {}
    for url, result in results:
        for finding in result["findings"]:
            groups.setdefault((url, finding["id"]), []).append(finding)
    output = []
    for (url, fid), rows in sorted(groups.items()):
        confidences = [f.get("validator_confidence") for f in rows]
        scores = [c for c in confidences if c is not None]
        floor = [f["passes_floor"] for f in rows]
        passing = [f["passes_filter"] for f in rows]
        output.append(
            dict(
                pr_url=url,
                finding_id=fid,
                replays=len(rows),
                confidences=confidences,
                confidence_min=min(scores, default=None),
                confidence_max=max(scores, default=None),
                confidence_spread=max(scores) - min(scores) if scores else None,
                floor_pass_count=sum(floor),
                filter_pass_count=sum(passing),
                floor_flips=sum(a != b for a, b in pairwise(floor)),
                filter_flips=sum(a != b for a, b in pairwise(passing)),
                crosses_floor=bool(any(floor) and not all(floor)),
            )
        )
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--score-id", required=True)
    parser.add_argument("--treatment-tree", type=Path, required=True)
    parser.add_argument("--mirrors-dir", type=Path, required=True)
    parser.add_argument("--replays", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--child-auth", choices=("subscription", "api"), required=True)
    parser.add_argument("--child-model", default="sonnet")
    parser.add_argument(
        "--child-effort", choices=("low", "medium", "high", "max"), default="low"
    )
    parser.add_argument("--timeout-s", type=int, default=1200)
    parser.add_argument(
        "--pr",
        action="append",
        default=[],
        help="Limit to this scored PR URL (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.replays < 1 or args.timeout_s < 1:
        parser.error("replays and timeout must be positive")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.score_id):
        parser.error("invalid score id")
    tree, out = args.treatment_tree.resolve(), args.out.resolve()
    sources = load_sources(args.source_run.resolve(), args.score_id, args.pr)
    out.mkdir(parents=True, exist_ok=False)
    treatment_files = [
        tree / "agents/validator.md",
        tree / "workflows/build.js",
        *sorted((tree / "workflows/src").glob("*.js")),
    ]
    treatment_hashes = {
        str(p.relative_to(tree)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in treatment_files
    }
    manifest: ReplayManifestWire = {
        "source_run": str(args.source_run.resolve()),
        "score_id": args.score_id,
        "treatment_tree": str(tree),
        "treatment_hashes": treatment_hashes,
        "child_model": args.child_model,
        "child_effort": args.child_effort,
        "child_auth": args.child_auth,
        "replays": args.replays,
        "dry_run": args.dry_run,
    }
    write_json(out / "replay.json", manifest)
    results = []
    for replay in range(1, args.replays + 1):
        for source in sources:
            slug = f"{source.identity['owner']}-{source.identity['repo']}-{source.identity['pr_number']}"
            dest = out / f"replay-{replay:03}" / slug
            dest.mkdir(parents=True)
            payload = relocate(source, dest, tree)
            write_json(
                dest / "source.json",
                {
                    "pr_url": source.url,
                    "identity": source.identity,
                    "artifact_hashes": source.hashes,
                    "bugs": dict(source.bugs),
                },
            )
            commands = checkout_commands(source, args.mirrors_dir.resolve(), dest)
            dispatches: list[DispatchWire] = []
            round_number = 0
            while True:
                prepared = node(
                    "prepare",
                    {"tree": str(tree), "payload": payload, "previous": dispatches},
                )
                payload["input"] = prepared["input"]
                write_json(dest / "input.json", payload)
                if not prepared["calls"]:
                    break
                round_number += 1
                script = dest / f"validate-round-{round_number}.js"
                script.write_text(prepared["script"], encoding="utf-8")
                command = child_command(
                    script, tree, args.child_model, args.child_effort
                )
                write_json(
                    dest / f"commands-round-{round_number}.json",
                    {
                        "checkout": commands if round_number == 1 else [],
                        "cwd": str(dest / "worktree"),
                        "child": command,
                        "child_auth": args.child_auth,
                        "timeout_s": args.timeout_s,
                    },
                )
                if args.dry_run:
                    break
                print(
                    f"replay {replay}/{args.replays}, round {round_number}: {source.url}",
                    flush=True,
                )
                if round_number == 1:
                    for checkout in commands:
                        subprocess.run(
                            checkout, check=True, capture_output=True, text=True
                        )
                launch(command, source, dest, args.child_auth, args.timeout_s)
                receipt = workflow_result(dest, script)
                dispatches.extend(receipt["dispatches"])
                write_json(dest / "dispatches.json", dispatches)
            if args.dry_run:
                continue
            result = node(
                "apply",
                {"tree": str(tree), "payload": payload, "dispatches": dispatches},
            )
            result = summarize(source, result)
            result["replay"] = replay
            write_json(dest / "outcomes.json", result)
            results.append((source.url, result))
            if any(
                hashlib.sha256((tree / name).read_bytes()).hexdigest() != value
                for name, value in treatment_hashes.items()
            ):
                raise ValueError("treatment changed during replay")
    if not args.dry_run:
        write_json(out / "spread.json", spreads(results))
        write_json(
            out / "summary.json",
            {
                "per_pr": [
                    dict(replay=result["replay"], pr_url=url, **result["summary"])
                    for url, result in results
                ],
                "per_replay": [
                    dict(
                        replay=index,
                        **{
                            key: sum(
                                result["summary"][key]
                                for _, result in results
                                if result["replay"] == index
                            )
                            for key in (
                                "known_bugs",
                                "known_bugs_passing",
                                "unmatched_passing",
                                "findings",
                                "passing",
                                "filtered_out",
                            )
                        },
                    )
                    for index in range(1, args.replays + 1)
                ],
            },
        )
    print(str(out))
    return 0
