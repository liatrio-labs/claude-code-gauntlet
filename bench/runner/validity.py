"""Pinned measurement controls, arm declarations and immutable two-strike attempts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner.decode import decode  # noqa: E402
from bench.runner.epochs import (  # noqa: E402
    DEFAULT_ROOT,
    confined,
    digest,
    file_hash,
    load_epoch,
    read_json,
)
from bench.runner.observations import (  # noqa: E402
    ScoreRef,
    artifact_allowed,
    copy_exclusive,
    exclusive_json,
)

AuthMode = Literal["api", "subscription"]
CheckpointStatus = Literal["pending", "ok", "timeout", "invalid", "drifted", "failed"]
ValidityState = Literal["valid", "invalid", "pending", "operational_failure"]
EFFORT_KEYS = (
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SUBAGENT_EFFORT",
    "MAX_THINKING_TOKENS",
)
MODEL_OVERRIDE_KEYS = (
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_SUBAGENT_EFFORT_LEVEL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
)
LIMITATIONS = (
    "Installed CLI auto-update and effective effort behavior require live verification.",
)


def refuse_ambient_model_overrides(env):
    if any(k in env for k in (*MODEL_OVERRIDE_KEYS, *EFFORT_KEYS)) or any(
        env.get(k) and env[k].lower() not in ("0", "false")
        for k in (
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
        )
    ):
        raise ValueError(
            "measurement refuses ambient model/effort/provider/auth overrides"
        )


@dataclass(frozen=True, slots=True)
class SharedControls:
    cli_version: str
    cli_realpath: str
    cli_sha256: str
    judge_pin: str
    adjudicator_pin: str
    golden_epoch_id: str
    golden_sha256: str
    scorer_tree: str
    auth_mode: AuthMode
    auto_update_disabled: Literal[True] = True


@dataclass(frozen=True, slots=True)
class Treatment:
    treatment_git_sha: str
    resolved_models: tuple[tuple[str, str], ...]
    allowed_auxiliary_model_ids: tuple[str, ...]
    effort_env: tuple[tuple[str, str | None], ...]
    agent_efforts: tuple[tuple[str, str | None], ...]
    bench_policy_sha256: str


@dataclass(frozen=True, slots=True)
class RunFingerprint:
    cli_version: str
    cli_realpath: str
    cli_sha256: str
    resolved_models: tuple[tuple[str, str], ...]
    allowed_auxiliary_model_ids: tuple[str, ...]
    effort_env: tuple[tuple[str, str | None], ...]
    agent_efforts: tuple[tuple[str, str | None], ...]
    judge_pin: str
    adjudicator_pin: str
    golden_epoch_id: str
    golden_sha256: str
    scorer_tree: str
    auth_mode: AuthMode
    bench_policy_sha256: str
    treatment_git_sha: str

    def wire(self):
        value = asdict(self)
        for key in ("resolved_models", "effort_env", "agent_efforts"):
            value[key] = dict(value[key])
        value["allowed_auxiliary_model_ids"] = list(self.allowed_auxiliary_model_ids)
        return value

    def controls(self):
        return SharedControls(
            **{
                k: getattr(self, k)
                for k in SharedControls.__dataclass_fields__
                if k != "auto_update_disabled"
            }
        )

    def treatment(self):
        return Treatment(
            **{k: getattr(self, k) for k in Treatment.__dataclass_fields__}
        )


def fingerprint_from_wire(value):
    value = dict(value)
    for key in ("resolved_models", "effort_env", "agent_efforts"):
        value[key] = sorted(value[key].items())
    fp = decode(RunFingerprint, value)
    if not all(
        (
            fp.cli_version,
            fp.cli_realpath,
            fp.cli_sha256,
            fp.judge_pin,
            fp.adjudicator_pin,
            fp.golden_epoch_id,
            fp.golden_sha256,
            fp.scorer_tree,
            fp.treatment_git_sha,
        )
    ):
        raise ValueError("incomplete measurement pins")
    if not fp.resolved_models or any(
        not re.fullmatch(r"claude-[A-Za-z0-9_.-]+(?:\[1m\])?", m)
        for m in (*dict(fp.resolved_models).values(), *fp.allowed_auxiliary_model_ids)
    ):
        raise ValueError("unresolved model fingerprint")
    if (
        set(dict(fp.effort_env)) != {*EFFORT_KEYS, "DISABLE_AUTOUPDATER"}
        or dict(fp.effort_env)["DISABLE_AUTOUPDATER"] != "1"
    ):
        raise ValueError("auto-update must be disabled; incomplete effort fingerprint")
    return fp


@dataclass(frozen=True, slots=True)
class MeasurementWindow:
    schema_version: Literal[1]
    window_id: str
    opens_at: str
    closes_at: str
    expected: SharedControls
    pr_urls: tuple[str, ...]
    planned_run_indices: tuple[int, ...]
    max_parallel_prs: Literal[1]
    arms: dict[str, Treatment]

    def wire(self):
        return asdict(self)


def timestamp(value):
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if result.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return result.astimezone(timezone.utc)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def window_from_wire(value):
    window = decode(MeasurementWindow, value)
    if (
        not window.window_id
        or timestamp(window.opens_at) >= timestamp(window.closes_at)
        or not window.pr_urls
        or len(set(window.pr_urls)) != len(window.pr_urls)
        or not window.planned_run_indices
        or tuple(sorted(set(window.planned_run_indices))) != window.planned_run_indices
        or min(window.planned_run_indices) < 0
        or len(window.arms) != 2
    ):
        raise ValueError("invalid measurement window")
    return window


def load_window(path):
    return window_from_wire(read_json(Path(path)))


@dataclass(frozen=True, slots=True)
class Validity:
    state: ValidityState
    reasons: tuple[str, ...] = ()
    window_id: str | None = None
    observed: RunFingerprint | None = None
    retry_of: str | None = None
    confirmed_reset_at: str | None = None
    retryable: bool = False
    limitations: tuple[str, ...] = LIMITATIONS
    evidence_refs: tuple[str, ...] = ()

    def wire(self):
        return {
            **asdict(self),
            "observed": self.observed.wire() if self.observed else None,
        }


def check_window(window, observed, *, arm, now):
    reasons = []
    if observed.controls() != window.expected:
        reasons.append("window_mismatch")
    if observed.treatment() != window.arms.get(arm):
        reasons.append("treatment_mismatch")
    if not timestamp(window.opens_at) <= timestamp(now) <= timestamp(window.closes_at):
        reasons.append("window_closed")
    return Validity(
        "invalid" if reasons else "valid",
        tuple(reasons),
        window.window_id,
        observed,
        retryable=bool(reasons),
    )


def cli_probe(path):
    return subprocess.run(
        [str(path), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def model_probe(root, policy):
    script = "import {resolvedDispatchModels} from './workflows/src/registry.js'; console.log(JSON.stringify(resolvedDispatchModels(JSON.parse(process.argv[1]))));"
    return json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", script, json.dumps(policy)],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
    )


def capture_fingerprint(
    *,
    cli_path,
    child_model,
    auth_mode,
    epoch,
    judge_pin,
    adjudicator_pin,
    env,
    policy,
    root=ROOT,
    auxiliary_models=(),
    cli_probe=cli_probe,
    model_probe=model_probe,
):
    refuse_ambient_model_overrides(env)
    path = Path(cli_path).resolve(strict=True)
    models = {**model_probe(root, policy), "orchestrator": child_model}
    efforts = tuple(
        (
            p.stem,
            next(
                (
                    line.split(":", 1)[1].strip()
                    for line in p.read_text(encoding="utf-8")
                    .split("---", 2)[1]
                    .splitlines()
                    if line.startswith("effort:")
                ),
                None,
            ),
        )
        for p in sorted((Path(root) / "agents").glob("*.md"))
        if p.read_text(encoding="utf-8").startswith("---")
    )
    fp = RunFingerprint(
        cli_probe(path),
        str(path),
        file_hash(path),
        tuple(sorted(models.items())),
        tuple(sorted(auxiliary_models)),
        tuple(
            sorted(
                {
                    **{k: env.get(k) for k in EFFORT_KEYS},
                    "DISABLE_AUTOUPDATER": "1",
                }.items()
            )
        ),
        efforts,
        judge_pin,
        adjudicator_pin,
        epoch.epoch_id,
        epoch.golden_sha256,
        epoch.scorer_tree,
        auth_mode,
        digest(policy),
        treatment_sha(root),
    )
    return fingerprint_from_wire(fp.wire())


def recheck_cli(fingerprint, *, probe=cli_probe):
    path = Path(fingerprint.cli_realpath)
    if (
        file_hash(path) != fingerprint.cli_sha256
        or probe(path) != fingerprint.cli_version
    ):
        raise ValueError("cli_mismatch: pinned CLI changed before child dispatch")


def same_pr_set(actual, expected):
    return len(actual) == len(expected) and set(actual) == set(expected)


def classify_attempt(result, *, auth_mode):
    return (
        Validity("valid")
        if result.status == "ok"
        else Validity("invalid", (result.reason or result.status,), retryable=True)
    )


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    schema_version: Literal[1]
    attempt_id: str
    run_id: str
    pr_url: str
    started_at: str
    completed_at: str
    checkpoint_status: CheckpointStatus
    checkpoint_reason: str | None
    validity: Validity
    source_artifact_hashes: tuple[tuple[str, str], ...]
    sequence: int
    requested_models: tuple[tuple[str, str], ...]
    observed_model_ids: tuple[str, ...]

    def wire(self):
        return {
            **asdict(self),
            "validity": self.validity.wire(),
            "source_artifact_hashes": dict(self.source_artifact_hashes),
            "requested_models": dict(self.requested_models),
        }


def attempt_from_wire(value):
    value = dict(value)
    value["validity"] = dict(value["validity"])
    if value["validity"]["observed"]:
        value["validity"]["observed"] = asdict(
            fingerprint_from_wire(value["validity"]["observed"])
        )
    for key in ("source_artifact_hashes", "requested_models"):
        value[key] = sorted(value[key].items())
    result = decode(AttemptRecord, value)
    if result.sequence < 1 or timestamp(result.started_at) > timestamp(
        result.completed_at
    ):
        raise ValueError("invalid immutable attempt")
    return result


def check_retry(original, *, now, reset_confirmed_at=None):
    if timestamp(now) < timestamp(original.completed_at):
        raise ValueError("retry must follow the first completed attempt")
    if original.validity.state != "invalid" or not original.validity.retryable:
        raise ValueError("attempt cannot retry")
    if (
        original.validity.observed
        and original.validity.observed.auth_mode == "subscription"
        and (
            not reset_confirmed_at
            or not timestamp(original.completed_at)
            < timestamp(reset_confirmed_at)
            <= timestamp(now)
        )
    ):
        raise ValueError(
            "reset_not_confirmed: record reset time after failure and before retry"
        )


def confirmed_failure(attempt):
    return attempt.validity.state == "operational_failure" and bool(
        attempt.validity.retry_of
    )


def failure_confirmation_ref(attempt):
    return attempt.validity.retry_of


def load_attempt(path):
    path = Path(path)
    result = attempt_from_wire(read_json(path))
    if result.attempt_id != path.parent.name:
        raise ValueError("attempt identity mismatch")
    for name, expected in result.source_artifact_hashes:
        if (
            not artifact_allowed(Path(name))
            or file_hash(confined(path.parent, name)) != expected
        ):
            raise ValueError("attempt artifact hash mismatch")
    if result.validity.state == "valid":
        from bench.runner.check import workflow_stage_outcomes

        failures, _ = workflow_stage_outcomes(path.parent / "artifacts")
        if failures:
            raise ValueError("invalid selected attempt: " + "; ".join(failures))
    if result.validity.state == "operational_failure":
        originals = list(
            path.parents[3].parent.glob(
                f"*/attempts/*/{result.validity.retry_of}/attempt.json"
            )
        )
        if len(originals) != 1 or originals[0] == path:
            raise ValueError("missing independent first attempt")
        original = load_attempt(originals[0])
        if (
            original.pr_url != result.pr_url
            or original.validity.window_id != result.validity.window_id
            or original.validity.observed != result.validity.observed
            or (
                original.run_id == result.run_id
                and original.sequence >= result.sequence
            )
            or result.checkpoint_status == "ok"
        ):
            raise ValueError("invalid operational failure link")
        check_retry(
            original,
            now=result.started_at,
            reset_confirmed_at=result.validity.confirmed_reset_at,
        )
    return result


selected_attempt = load_attempt


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    exclusive_json(pending, value)
    os.replace(pending, path)


def snapshot_attempt(
    run_dir,
    pr_dir,
    *,
    run_id,
    pr_url,
    result,
    validity,
    started_at,
    requested_models=(),
    observed_model_ids=(),
):
    run_dir, pr_dir = Path(run_dir), Path(pr_dir)
    parent = run_dir / "attempts" / hashlib.sha256(pr_url.encode()).hexdigest()[:20]
    sequence = len(list(parent.glob("*/attempt.json"))) + 1
    aid = f"a{sequence:04d}-{uuid.uuid4().hex}"
    target, hashes = parent / aid, {}
    if not pr_dir.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("attempt PR directory escapes run")
    pr_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(
        pr_dir / "attempt-result.json",
        {
            "status": result.status,
            "reason": result.reason,
            "cost_usd": result.cost_usd,
            "per_model": result.per_model,
            "requested_models": dict(requested_models),
            "observed_model_ids": list(observed_model_ids),
        },
    )
    for source in sorted(pr_dir.rglob("*")):
        relative = source.relative_to(pr_dir)
        if (
            not source.is_file()
            or source.is_symlink()
            or not artifact_allowed(relative)
            or any(p in ("superseded", "worktree") for p in relative.parts)
        ):
            continue
        if not source.resolve().is_relative_to(pr_dir.resolve()):
            raise ValueError("attempt artifact escapes PR")
        name = "artifacts/" + relative.as_posix()
        copy_exclusive(source, target / name)
        hashes[name] = file_hash(target / name)
    record = AttemptRecord(
        1,
        aid,
        run_id,
        pr_url,
        started_at,
        utc_now(),
        result.status,
        result.reason,
        validity,
        tuple(sorted(hashes.items())),
        sequence,
        tuple(sorted(dict(requested_models).items())),
        tuple(sorted(observed_model_ids)),
    )
    exclusive_json(target / "attempt.json", record.wire())
    return record


@dataclass(frozen=True, slots=True)
class CellSelection:
    arm: str
    run_index: int
    pr_url: str
    attempt_id: str
    score_ref: ScoreRef | None
    failure_confirmation_ref: str | None = None
    attempt: AttemptRecord | None = None
    observations: tuple[MatrixObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class MatrixObservation:
    arm: str
    run_index: int
    pr_url: str
    bug_id: str
    detected: bool
    run_id: str
    attempt_id: str
    delivered_dimensions: tuple[str, ...] = ()
    discovery_dimensions: tuple[str, ...] = ()
    stage_outcomes: tuple[tuple[str, bool | None], ...] = ()
    provenance_available: bool = False


@dataclass(frozen=True, slots=True)
class CompleteMatrix:
    selections: tuple[CellSelection, ...]
    observations: tuple[MatrixObservation, ...]
    arms: tuple[str, str]
    run_indices_by_arm: tuple[tuple[str, tuple[int, ...]], ...]
    stage_observations: tuple = ()


def validate_matrix(selections, window, population, *, epoch, expected_run_indices):
    selections = tuple(selections)
    arms = tuple(sorted({s.arm for s in selections}))
    if (
        len(arms) != 2
        or tuple(window.planned_run_indices[: len(expected_run_indices)])
        != expected_run_indices
    ):
        raise ValueError("invalid arms or planned run prefix")
    expected = {
        (a, r, p) for a in arms for r in expected_run_indices for p in window.pr_urls
    }
    actual = [(s.arm, s.run_index, s.pr_url) for s in selections]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("incomplete or duplicate PR/run matrix")
    run_ids = {}
    expected_bugs = {
        p: {b.bug_id for b in epoch.bugs if b.pr_url == p} for p in window.pr_urls
    }
    all_observations = []
    for s in selections:
        attempt = s.attempt
        if (
            not attempt
            or not s.score_ref
            or s.attempt_id != attempt.attempt_id
            or s.pr_url != attempt.pr_url
        ):
            raise ValueError("missing selected attempt or score")
        v = attempt.validity
        if (
            v.state not in ("valid", "operational_failure")
            or v.window_id != window.window_id
            or v.observed is None
            or check_window(window, v.observed, arm=s.arm, now=attempt.started_at).state
            != "valid"
        ):
            raise ValueError("invalid selected outcome or measurement identity")
        if v.state == "operational_failure" and not s.failure_confirmation_ref:
            raise ValueError("unconfirmed operational failure")
        rows = s.observations
        if (
            len(rows) != len(expected_bugs[s.pr_url])
            or {r.bug_id for r in rows} != expected_bugs[s.pr_url]
            or any(
                (r.arm, r.run_index, r.pr_url, r.run_id, r.attempt_id)
                != (s.arm, s.run_index, s.pr_url, attempt.run_id, attempt.attempt_id)
                or type(r.detected) is not bool
                for r in rows
            )
            or (v.state == "operational_failure" and any(r.detected for r in rows))
        ):
            raise ValueError(
                "incomplete/duplicate/non-Boolean bug matrix or nonzero operational failure"
            )
        all_observations.extend(rows)
        key = (s.arm, s.run_index)
        if (
            run_ids.setdefault(key, attempt.run_id) != attempt.run_id
            or s.score_ref.run_id != attempt.run_id
        ):
            raise ValueError("run identity mismatch")
    if len(set(run_ids.values())) != len(run_ids):
        raise ValueError("rescoring does not create independent review runs")
    return CompleteMatrix(
        selections,
        tuple(all_observations),
        arms,
        tuple((a, expected_run_indices) for a in arms),
    )


def benchmark_policy(timeout_s, *, resolve_limits=True):
    from bench.runner.invoke import BENCH_PINS

    limits = None
    if resolve_limits:
        limits = json.loads(
            subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    "import {LIMIT_DEFAULTS} from './workflows/src/registry.js'; console.log(JSON.stringify(LIMIT_DEFAULTS));",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            ).stdout
        )
    return {
        "provider": "firstParty",
        "subagentModel": None,
        "pins": dict(BENCH_PINS),
        "timeout_s": timeout_s,
        "stage_limits": limits,
        "capacity_policy_sha256": file_hash(ROOT / "workflows/src/capacity.js"),
    }


PLUGIN_PATHS = (".claude-plugin", "agents", "skills", "workflows", "scripts")


def require_clean_treatment(root=ROOT):
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    if status.stdout.strip():
        raise ValueError("input_drift: measurement requires clean tracked files")


def treatment_sha(root=ROOT):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def agent_read_hash(root=ROOT):
    # The index records names, modes and blob ids for the entire loaded plugin tree.
    entries = subprocess.run(
        ["git", "ls-files", "--stage", "-z", "--", *PLUGIN_PATHS],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    if not entries or entries == [b""]:
        raise ValueError("missing plugin index tree")
    return digest(sorted(row.decode("utf-8") for row in entries if row))


def run_provenance(
    *,
    child_model,
    child_auth,
    timeout_s,
    epoch_id=None,
    window=None,
    arm=None,
    root=ROOT,
):
    epoch = load_epoch(epoch_id, root=DEFAULT_ROOT)
    policy = benchmark_policy(timeout_s, resolve_limits=window is not None)
    cli = window.expected.cli_realpath if window else shutil.which("claude")
    try:
        fp = capture_fingerprint(
            cli_path=cli,
            child_model=child_model,
            auth_mode=child_auth,
            epoch=epoch,
            judge_pin=epoch.judge_pin,
            adjudicator_pin=epoch.adjudicator_pin,
            env=os.environ,
            policy=policy,
            root=root,
            auxiliary_models=window.arms[arm].allowed_auxiliary_model_ids
            if window
            else (),
        )
        if window and check_window(window, fp, arm=arm, now=utc_now()).state != "valid":
            raise ValueError(
                "window preflight: shared controls or arm treatment mismatch"
            )
        return fp.wire() if window else {**fp.wire(), "status": "non_record"}
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        if window:
            raise
        return {
            "status": "non_record",
            "golden_epoch_id": epoch.epoch_id,
            "golden_sha256": epoch.golden_sha256,
            "auth_mode": child_auth,
            "requested_models": {"orchestrator": child_model},
        }


def collect_attempt(
    run_dir, pr_dir, *, url, result, started_at, manifest, prior=None, reset_at=None
):
    from bench.runner import check, invoke

    provenance = manifest.get("provenance") or {}
    fp = (
        fingerprint_from_wire(provenance)
        if provenance and provenance.get("status") != "non_record"
        else None
    )
    envelope = (
        invoke.parse_result_envelope(
            Path(result.raw_json_path).read_text(encoding="utf-8")
        )
        if result.raw_json_path and Path(result.raw_json_path).is_file()
        else {}
    )
    observed = tuple(sorted((envelope or {}).get("modelUsage", {})))
    errors = []
    stage_failures, disclosures = (
        check.workflow_stage_outcomes(pr_dir)
        if manifest.get("anchor") != "naive"
        else ((), ())
    )
    if stage_failures:
        errors.append("pipeline_degraded")
    if result.status == "ok":
        errors.extend(
            check.check_attempt(Path(pr_dir), naive=manifest.get("anchor") == "naive")
            if fp
            else ()
        )
        if not result.payload_path or not Path(result.payload_path).is_file():
            errors.append("artifact_missing")
        if fp:
            allowed = {
                *dict(fp.resolved_models).values(),
                *fp.allowed_auxiliary_model_ids,
            }
            if not observed or set(observed) - allowed:
                errors.append("model_mismatch")
            if manifest.get("anchor") != "naive":
                maps = [
                    r["result"].get("resolvedPolicy", {}).get("resolvedModels")
                    for p in (Path(pr_dir) / "workflows").glob("*.json")
                    for r in [read_json(p)]
                    if isinstance(r.get("result"), dict)
                    and invoke.record_identifies_repo_bundle(r, ROOT)
                ]
                expected = {k: m for k, m in fp.resolved_models if k != "orchestrator"}
                if not maps or any(m != expected for m in maps):
                    errors.append("model_mismatch")
    if fp and manifest.get("measurement_window"):
        errors.extend(
            check_window(
                load_window(Path(manifest["measurement_window"])),
                fp,
                arm=manifest["arm"],
                now=utc_now(),
            ).reasons
        )
        try:
            recheck_cli(fp)
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append("cli_mismatch")
    if errors:
        result = replace(result, status="invalid", reason=",".join(sorted(set(errors))))
    value = replace(
        classify_attempt(result, auth_mode=manifest.get("child_auth", "api")),
        observed=fp,
        window_id=manifest.get("window_id"),
        retry_of=prior.attempt_id if prior else None,
        confirmed_reset_at=reset_at,
        evidence_refs=stage_failures,
        limitations=(*LIMITATIONS, *disclosures),
    )
    if (
        prior
        and value.state == "invalid"
        and prior.pr_url == url
        and prior.validity.window_id == value.window_id
        and prior.validity.observed == fp
        and fp is not None
    ):
        try:
            check_retry(prior, now=started_at, reset_confirmed_at=reset_at)
        except ValueError:
            pass
        else:
            value = replace(value, state="operational_failure", retryable=False)
    return snapshot_attempt(
        run_dir,
        pr_dir,
        run_id=manifest.get("run_id", Path(run_dir).name),
        pr_url=url,
        result=result,
        validity=value,
        started_at=started_at,
        requested_models=manifest.get("requested_models", {}).items(),
        observed_model_ids=observed,
    )


def measurement_context(manifest):
    window = load_window(Path(manifest["measurement_window"]))
    fp = fingerprint_from_wire(manifest["provenance"])
    checked = check_window(window, fp, arm=manifest["arm"], now=manifest["started"])
    if (
        checked.state != "valid"
        or manifest["window_id"] != window.window_id
        or manifest["window_sha256"] != file_hash(Path(manifest["measurement_window"]))
        or not same_pr_set(manifest["pr_urls"], window.pr_urls)
    ):
        raise ValueError("invalid measurement context")
    return fp
