"""Create a pinned, sequential measurement window from two local treatment sources."""

import argparse
import math
import os
import re
import shutil
import subprocess
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import get_args

from bench.runner import populations, validity
from bench.runner.compare import PromotionRule
from bench.runner.contracts import exclusive_json
from bench.runner.decode import decode
from bench.runner.epochs import DEFAULT_ROOT, digest, load_epoch, read_json


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True
    ).stdout


@contextmanager
def resolve_source(source, *, root=None):
    root = Path.cwd() if root is None else Path(root)
    kind, sep, value = source.partition(":")
    if not sep or not value or kind not in ("sha", "worktree"):
        raise ValueError("arm source requires sha:REV or worktree:PATH")
    if kind == "worktree":
        path = Path(value).resolve(strict=True)
        if (
            Path(git(path, "rev-parse", "--show-toplevel").decode().strip()).resolve()
            != path
        ):
            raise ValueError("source must be a Git worktree root")
        validity.require_clean_treatment(path)
        yield path
        return
    revision = (
        git(root, "rev-parse", "--verify", "--end-of-options", value + "^{commit}")
        .decode()
        .strip()
    )
    objects = Path(git(root, "rev-parse", "--git-path", "objects").decode().strip())
    objects = (root / objects).resolve()
    fmt = git(root, "rev-parse", "--show-object-format").decode().strip()
    with TemporaryDirectory(prefix="bench-treatment-") as tmp:
        path = Path(tmp)
        metadata = path / ".git"
        (metadata / "objects/info").mkdir(parents=True)
        (metadata / "refs").mkdir()
        (metadata / "HEAD").write_text(revision + "\n", encoding="ascii")
        (metadata / "objects/info/alternates").write_text(
            str(objects) + "\n", encoding="utf-8"
        )
        (metadata / "config").write_text(
            "[core]\nrepositoryformatversion = "
            + (
                "1\n[extensions]\nobjectformat = sha256\n" if fmt == "sha256" else "0\n"
            ),
            encoding="ascii",
        )
        git(path, "read-tree", revision)
        entries = git(root, "ls-tree", "-rz", revision, "--", *validity.PLUGIN_PATHS)
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            header, name = entry.split(b"\t", 1)
            mode, objtype, oid = header.split()
            if objtype != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError("plugin source requires regular files")
            target = path / name.decode("utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git(root, "cat-file", "blob", oid.decode("ascii")))
            target.chmod(int(mode, 8) & 0o777)
        yield path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--population", required=True, choices=get_args(populations.PopulationName)
    )
    parser.add_argument("--arm", action="append", required=True)
    parser.add_argument("--run-indices", required=True)
    parser.add_argument(
        "--auth-mode", required=True, choices=get_args(validity.AuthMode)
    )
    parser.add_argument("--duration-hours", required=True, type=float)
    parser.add_argument("--max-parallel-prs", type=int, choices=(1,), default=1)
    parser.add_argument("--child-model", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--timeout-mins", type=int, default=45)
    parser.add_argument("--opens-at", default=None)
    parser.add_argument("--window-id", default=None)
    parser.add_argument("--candidate-arm", default="candidate")
    parser.add_argument(
        "--contamination", type=Path, default=DEFAULT_ROOT / "contamination.json"
    )
    parser.add_argument("--wave")
    parser.add_argument("--rule", type=Path)
    parser.add_argument("--unseal")
    return parser.parse_args(argv)


def create_window(args):
    arms = [a.split("=", 1) for a in args.arm]
    indices = tuple(int(i) for i in args.run_indices.split(","))
    if (
        len(arms) != 2
        or any(len(a) != 2 or not a[0].strip() for a in arms)
        or len({a[0] for a in arms}) != 2
        or args.candidate_arm not in {a[0] for a in arms}
        or not indices
        or indices != tuple(sorted(set(indices)))
        or min(indices) < 0
        or any(
            not math.isfinite(n) or n <= 0
            for n in (args.duration_hours, args.timeout_mins)
        )
        or not re.fullmatch(r"claude-[A-Za-z0-9_.-]+(?:\[1m\])?", args.child_model)
        or bool(args.wave) != bool(args.rule)
    ):
        raise ValueError("invalid window plan, model, bounds or wave/rule pair")
    if args.population.startswith("gate-"):
        raise ValueError("use gate for runnable windows; gate selectors are reports")
    if args.out.exists():
        raise FileExistsError("window already exists")
    opens = validity.timestamp(args.opens_at or validity.utc_now())
    closes = opens + timedelta(hours=args.duration_hours)
    rule = decode(PromotionRule, read_json(args.rule)) if args.rule else None
    if rule and not rule.frozen:
        raise ValueError("window requires frozen rule")
    epoch = load_epoch()
    contamination = read_json(args.contamination)
    fingerprints = {}
    with ExitStack() as stack:
        sources = [
            (name, stack.enter_context(resolve_source(source))) for name, source in arms
        ]
        for name, root in sources:
            fingerprints[name] = validity.capture_fingerprint(
                cli_path=args.cli or shutil.which("claude"),
                child_model=args.child_model,
                auth_mode=args.auth_mode,
                epoch=epoch,
                judge_pin=epoch.judge_pin,
                adjudicator_pin=epoch.adjudicator_pin,
                env={**os.environ, "DISABLE_AUTOUPDATER": "1"},
                policy=validity.benchmark_policy(args.timeout_mins * 60, root=root),
                root=root,
            )
    fp = next(iter(fingerprints.values()))
    if any(f.controls != fp.controls for f in fingerprints.values()):
        raise ValueError("arm shared controls differ")
    population = populations.resolve_population(
        args.population,
        epoch,
        "core",
        contamination,
        unseal=args.unseal,
        treatment_sha=fingerprints[args.candidate_arm].treatment.treatment_git_sha,
    )
    window = validity.MeasurementWindow(
        2,
        args.window_id or uuid.uuid4().hex,
        opens.isoformat(),
        closes.isoformat(),
        fp.controls,
        population.pr_urls,
        indices,
        1,
        {name: f.treatment for name, f in fingerprints.items()},
        tuple(
            (name, i)
            for i in indices
            for name, _ in sorted(arms, key=lambda a: a[0] == args.candidate_arm)
        ),
        args.wave or "",
        digest(asdict(rule)) if rule else "",
        args.candidate_arm,
    )
    window = validity.window_from_wire(window.wire())
    exclusive_json(args.out, window.wire())
    return window


def main(argv=None):
    args = parse_args(argv)
    try:
        window = create_window(args)
    except (
        OSError,
        TypeError,
        ValueError,
        OverflowError,
        subprocess.SubprocessError,
    ) as exc:
        # Process stderr may contain operator configuration; diagnostics name only the failure.
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"window creation refused: {detail}")
        return 2
    print(f"{args.out}: {window.window_id}")
    print(", ".join(f"{arm}/{index}" for arm, index in window.planned_order))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
