"""Transaction and observation behavior through scoring and packet readers."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from bench.runner import ledger, observations, score
from bench.runner.epochs import DEFAULT_ROOT, digest, file_hash, load_epoch, read_json


def packet_path(packet):
    from pathlib import Path

    return Path(packet["ledger_row"]["packet_path"])


def hashes(path):
    return {
        p.relative_to(path).as_posix(): file_hash(p)
        for p in path.rglob("*")
        if p.is_file()
    }


def test_repeated_scoring_uses_private_workspaces_and_preserves_packets(
    score_case, tmp_path
):
    first = score_case.execute()
    path = packet_path(first)
    initial = hashes(path)
    legacy = (score_case.run / "scores.json").read_bytes()
    second = score_case.execute()
    third = score_case.execute(epoch_id="dfc6cb4-v1")
    assert (
        len(
            {
                first["score_ref"]["score_id"],
                second["score_ref"]["score_id"],
                third["score_ref"]["score_id"],
            }
        )
        == 3
    )
    assert len(set(score_case.calls)) == 3
    assert hashes(path) == initial
    assert (score_case.run / "scores.json").read_bytes() == legacy
    other = tmp_path / "other"
    shutil.copytree(
        score_case.run,
        other,
        ignore=shutil.ignore_patterns("scores", "legacy", "latest-score.json"),
    )
    replace(score_case, run=other).execute()
    assert len(set(score_case.calls)) == 4
    assert hashes(path) == initial
    loaded = observations.load_packet(path)
    assert loaded.score_ref.score_id == first["score_ref"]["score_id"]
    assert len(loaded.observations) == first["profiles"]["all"]["denominator"]
    assert first["ledger_row"]["validity_state"] == "historical_descriptive"
    assert first["ledger_row"]["judge_cost_usd"] is None


def test_duplicate_score_id_refuses_before_dispatch(score_case):
    first = score_case.execute(score_id="a" * 32)
    with pytest.raises(FileExistsError):
        score_case.execute(score_id="a" * 32)
    assert len(score_case.calls) == 1
    assert observations.load_packet(packet_path(first)).score_ref.score_id == "a" * 32


@pytest.mark.parametrize("failure", ["scorer", "adjudicator", "commit"])
def test_failed_execution_is_pending_and_never_ledgered(
    score_case, monkeypatch, failure
):
    def fail(*args, **kwargs):
        raise RuntimeError("interrupted")

    if failure == "scorer":
        monkeypatch.setattr(type(score_case), "scorer", fail)
    elif failure == "commit":
        monkeypatch.setattr(observations, "publish_packet", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        score_case.execute(
            **({"adjudicator": fail} if failure == "adjudicator" else {})
        )
    home = score_case.run / "scores/e616e84-v1"
    assert all(p.name.startswith(".pending-") for p in home.iterdir())
    assert not score_case.ledger.exists()
    assert not (score_case.run / "latest-score.json").exists()
    if failure == "commit":
        pending = next(home.iterdir())
        assert (pending / "complete.json").exists()
        with pytest.raises(ValueError, match="pending packet"):
            observations.load_packet(pending)


def test_packet_reader_rejects_corrupt_hashes(score_case):
    packet = score_case.execute()
    path = packet_path(packet)
    (path / "bugs.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        observations.load_packet(path)


def test_ledger_append_recovery_replays_complete_packet_without_judging(
    score_case, monkeypatch
):
    original = ledger.append_row

    def fail(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(ledger, "append_row", fail)
    with pytest.raises(OSError, match="ledger unavailable"):
        score_case.execute()
    path = next(
        p
        for p in (score_case.run / "scores/e616e84-v1").iterdir()
        if not p.name.startswith(".")
    )
    monkeypatch.setattr(ledger, "append_row", original)
    ledger.replay_packet(score_case.ledger, path)
    ledger.replay_packet(score_case.ledger, path)
    assert len(score_case.calls) == 1
    rows = [
        json.loads(line)
        for line in score_case.ledger.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1 and rows[0]["packet_sha256"] == file_hash(
        path / "complete.json"
    )


def test_missing_payload_is_artifact_error_and_empty_payload_is_full_misses(score_case):
    score_case.payload.unlink()
    with pytest.raises(ValueError, match="missing payload"):
        score_case.execute()
    assert not score_case.calls
    score_case.payload.write_text(
        json.dumps({"platform": "github", "payload": {"comments": []}}),
        encoding="utf-8",
    )
    packet = score_case.execute()
    assert packet["metrics"]["tp"] == 0
    assert packet["profiles"]["all"]["fn"] == len(
        [b for b in load_epoch(root=DEFAULT_ROOT).bugs if b.pr_url == score_case.url]
    )


def test_superseded_payload_does_not_mask_missing_current_payload(score_case):
    archived = score_case.payload.parent / "superseded/post-review-payload.json"
    archived.parent.mkdir()
    score_case.payload.rename(archived)
    with pytest.raises(ValueError, match="missing payload"):
        score_case.execute()


def test_countable_failure_has_full_zero_denominator_and_unresolved_pr_refuses(
    score_case,
):
    state = score_case.run / "state/a.json"
    state.write_text(
        json.dumps({"url": score_case.url, "status": "failed"}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="unresolved/invalid"):
        score_case.execute()
    assert not score_case.calls
    state.write_text(
        json.dumps(
            {
                "url": score_case.url,
                "status": "failed",
                "detail": {"countable_operational_failure": True},
            }
        ),
        encoding="utf-8",
    )
    packet = score_case.execute()
    loaded = observations.load_packet(packet_path(packet))
    assert loaded.observations and all(
        o.outcome == "operational_failure" for o in loaded.observations
    )
    assert packet["profiles"]["all"]["tp"] == 0
    assert packet["profiles"]["all"]["fn"] == len(loaded.observations)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_fn",
        "ambiguous_match",
        "absent_match",
        "error",
        "overlap",
        "bad_index",
        "no_evaluation",
    ],
)
def test_invalid_scorer_output_cannot_become_a_packet(
    score_case, monkeypatch, mutation
):
    original = type(score_case).scorer

    def corrupt(self, pin, key, model_dir):
        original(self, pin, key, model_dir)
        path = model_dir / "evaluations.json"
        ev = read_json(path)
        record = ev[self.url]["deep-review"]
        if mutation == "missing_fn":
            record["false_negatives"] = []
        elif mutation == "absent_match":
            record["true_positives"][0]["matched_candidate"] = "absent"
        elif mutation == "ambiguous_match":
            pass
        elif mutation == "error":
            record["errors"] = [{"error": "judge unavailable"}]
        elif mutation in ("overlap", "bad_index"):
            (model_dir / "dedup_groups.json").write_text(
                json.dumps(
                    {
                        self.url: {
                            "deep-review": [
                                [0, 1],
                                [1 if mutation == "overlap" else True],
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
        else:
            ev.clear()
        path.write_text(json.dumps(ev), encoding="utf-8")

    monkeypatch.setattr(type(score_case), "scorer", corrupt)
    if mutation == "ambiguous_match":
        payload = read_json(score_case.payload)
        payload["payload"]["comments"][1]["body"] = payload["payload"]["comments"][0][
            "body"
        ]
        score_case.payload.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        score_case.execute()
    assert not score_case.ledger.exists()


def test_dedup_siblings_and_multi_bug_match_do_not_become_noise(score_case):
    packet = score_case.execute()
    loaded = observations.load_packet(packet_path(packet))
    matched = [o for o in loaded.observations if o.outcome == "matched"]
    assert len(matched) == 2
    assert {m.kind for o in matched for m in o.matches} == {"direct", "dedup_sibling"}
    assert (
        len({m.candidate_id for o in matched for m in o.matches if m.kind == "direct"})
        == 1
    )
    assert packet["metrics"]["per_bucket"] == {
        "golden_matched": 2,
        "valid_extra": 0,
        "noise": 1,
    }
    assert packet["metrics"]["per_dimension"] == {}
    assert packet["manifest"]["provenance_status"] == "legacy_unknown"
    assert all(c.discovery_dimensions == ("unknown",) for c in loaded.candidates)
    assert len(loaded.adjudications) == 1
    context = read_json(
        packet_path(packet)
        / "contexts"
        / (loaded.adjudications[0].candidate_id + ".json")
    )
    assert (
        context["hunk"]
        and context["context_sha256"] == loaded.adjudications[0].context_sha256
    )


def test_repeated_unmatched_text_preserves_context_occurrences(score_case):
    payload = read_json(score_case.payload)
    payload["payload"]["comments"] += [
        {"body": "extra candidate", "path": "b.py", "line": 10}
    ]
    score_case.payload.write_text(json.dumps(payload), encoding="utf-8")
    diff = score_case.payload.parent / "diff.patch"
    diff.write_text(
        diff.read_text(encoding="utf-8")
        + "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -9,1 +9,2 @@\n context\n+distinct hunk\n",
        encoding="utf-8",
    )
    seen = []

    def adjudicate(text, hunk, context, pin, key):
        seen.append(hunk)
        return {"bucket": "valid_extra" if "distinct hunk" in hunk else "noise"}

    packet = score_case.execute(adjudicator=adjudicate)
    loaded = observations.load_packet(packet_path(packet))
    assert len(loaded.adjudications) == 2
    assert len({a.candidate_id for a in loaded.adjudications}) == 2
    assert len({a.context_sha256 for a in loaded.adjudications}) == 2
    assert len(seen) == 2 and seen[0] != seen[1]
    assert {a.bucket for a in loaded.adjudications} == {"valid_extra", "noise"}
    assert packet["metrics"]["per_bucket"] == {
        "golden_matched": 2,
        "valid_extra": 1,
        "noise": 1,
    }


def test_old_epoch_replays_recorded_baseline_exactly_offline(tmp_path, monkeypatch):
    fixture = read_json(Path(__file__).parent / "fixtures/baseline-replay.json")
    epoch = load_epoch("dfc6cb4-v1", root=DEFAULT_ROOT)
    run = tmp_path / fixture["run_id"]
    (run / "state").mkdir(parents=True)
    score._write_json(
        run / "run.json", {"run_id": run.name, "urls": list(fixture["prs"])}
    )
    verdicts = {}
    for i, (url, pr) in enumerate(fixture["prs"].items()):
        score._write_json(run / "state" / f"{i}.json", {"url": url, "status": "ok"})
        home = run / score.invoke.pr_dir_name({"url": url})
        score._write_json(
            home / "post-review-payload.json",
            {
                "platform": "github",
                "payload": {
                    "comments": [
                        {"body": c["text"], "path": c["path"], "line": c["line"]}
                        for c in pr["candidates"]
                    ]
                },
            },
        )
        for index, bucket in pr["adjudications"].items():
            text = pr["candidates"][int(index)]["text"]
            assert text not in verdicts
            verdicts[text] = bucket
    # A future active pin change cannot change the archived epoch's defaults.
    active_baselines = tmp_path / "active-baselines.json"
    score._write_json(
        active_baselines,
        {"judge_pin": "future-judge", "adjudicator_pin": "future-adjudicator"},
    )
    monkeypatch.setattr(score, "BASELINES_PATH", active_baselines)

    def recorded_scorer(pin, key, model_dir):
        assert pin == fixture["judge_pin"]
        evaluations, groups = {}, {}
        candidates = read_json(model_dir / "candidates.json")
        for url, pr in fixture["prs"].items():
            bugs = [b for b in epoch.bugs if b.pr_url == url]
            assert [c["text"] for c in candidates[url]["deep-review"]] == [
                c["text"] for c in pr["candidates"]
            ]
            evaluations[url] = {
                "deep-review": {
                    "tp": len(pr["tp"]),
                    "fn": len(pr["fn"]),
                    "fp": pr["fp"],
                    "true_positives": [
                        {
                            "golden_comment": bugs[p["golden"]].comment,
                            "matched_candidate": pr["candidates"][p["candidate"]][
                                "text"
                            ],
                            "confidence": p["confidence"],
                        }
                        for p in pr["tp"]
                    ],
                    "false_negatives": [
                        {"golden_comment": bugs[n].comment} for n in pr["fn"]
                    ],
                }
            }
            groups[url] = {"deep-review": pr["dedup_groups"]}
        score._write_json(model_dir / "evaluations.json", evaluations)
        score._write_json(model_dir / "dedup_groups.json", groups)

    def recorded_adjudicator(text, hunk, context, pin, key):
        assert pin == fixture["adjudicator_pin"]
        return {"bucket": verdicts.pop(text)}

    packet = score.score_run(
        run,
        epoch_id=epoch.epoch_id,
        env={"BENCH_JUDGE_API_KEY": "offline"},
        ledger_path=tmp_path / "ledger.jsonl",
        run_scorer=recorded_scorer,
        adjudicator=recorded_adjudicator,
    )
    assert not verdicts
    # Discovery dimensions supersede the historical severity alias; all other metrics reproduce.
    for name, expected in fixture["metrics"].items():
        if name != "per_dimension":
            assert packet["metrics"][name] == expected, name
    assert (
        packet["metrics"]["drift"]["severity_dist"]
        == fixture["metrics"]["per_dimension"]
    )
    loaded = observations.load_packet(packet_path(packet))
    assert len(loaded.adjudications) == 61
    assert any(
        m.kind == "dedup_sibling"
        and m.candidate_id in {a.candidate_id for a in loaded.adjudications}
        for bug in loaded.observations
        for m in bug.matches
    )


def test_score_normalizes_string_locations_before_context_capture(score_case):
    payload = read_json(score_case.payload)
    payload["payload"]["comments"][2]["line"] = "3"
    payload["payload"]["comments"].append(
        {"body": "invalid location", "path": "a.py", "line": "n/a"}
    )
    score._write_json(score_case.payload, payload)
    seen = {}

    def adjudicate(text, hunk, context, pin, key):
        seen[text] = hunk
        return {"bucket": "noise"}

    loaded = observations.load_packet(
        packet_path(score_case.execute(adjudicator=adjudicate))
    )
    assert seen["extra candidate"]
    assert seen["invalid location"] == ""
    assert [c.line for c in loaded.candidates][-2:] == [3, None]


@pytest.mark.parametrize("field", ["judge_pin", "adjudicator_pin"])
def test_epoch_refuses_explicit_pin_override_before_dispatch(
    score_case, tmp_path, field
):
    baseline = read_json(load_epoch(root=DEFAULT_ROOT).root / "baselines.json")
    baseline[field] = "different-pin"
    path = tmp_path / "baselines.json"
    score._write_json(path, baseline)
    with pytest.raises(ValueError, match=r"pin override.*epoch"):
        score_case.execute(baselines_path=path)
    assert not score_case.calls


def test_new_runs_never_acquire_a_legacy_archive(score_case):
    score_case.execute()
    score_case.execute(epoch_id="dfc6cb4-v1")
    assert not (score_case.run / "legacy").exists()


def test_scoring_archives_a_preexisting_legacy_score_before_rescore(score_case):
    legacy = b'{"scorer_sha":"dfc6cb4","metrics":{}}\n'
    (score_case.run / "scores.json").write_bytes(legacy)
    score_case.execute()
    assert (score_case.run / "legacy/dfc6cb4-v1/scores.json").read_bytes() == legacy


def test_legacy_archive_preserves_score_bytes_and_unscored_reviews(score_case):
    old = load_epoch("dfc6cb4-v1", root=DEFAULT_ROOT)
    legacy_bytes = b'{"historical": true}\n'
    (score_case.run / "scores.json").write_bytes(legacy_bytes)
    archive = observations.import_legacy(score_case.run, old)
    assert not archive.observations_available and archive.status == "partial"
    assert (
        score_case.run / "legacy/dfc6cb4-v1/scores.json"
    ).read_bytes() == legacy_bytes
    observations.import_legacy(score_case.run, old)
    packet = score_case.execute()
    assert (score_case.run / "scores.json").read_bytes() == legacy_bytes
    assert packet["score_ref"]["epoch_id"] == "e616e84-v1"


def test_unscored_legacy_archive_records_scores_absent(score_case):
    archive = observations.import_legacy(
        score_case.run, load_epoch("dfc6cb4-v1", root=DEFAULT_ROOT)
    )
    assert "scores_absent" in archive.unavailable_files
    assert any(
        name.endswith("post-review-payload.json") for name, _ in archive.archived_files
    )


def test_evaluator_id_tracks_scoring_code_and_legacy_identity_is_never_active():
    epoch = load_epoch(root=DEFAULT_ROOT)
    evaluator = score.evaluator_for(epoch, "judge", "adjudicator")
    from dataclasses import asdict

    wire = asdict(evaluator)
    eid = wire.pop("evaluator_id")
    assert eid == digest(wire)
    assert (
        ledger.score_identity({"run_id": "x", "scorer_sha": "dfc6cb4"}).epoch_id
        == "dfc6cb4-v1"
    )
    assert (
        ledger.score_identity({"run_id": "x", "scorer_sha": "unknown"}).status
        == "legacy_unknown"
    )


def test_explicit_attempt_selection_reads_frozen_payload_instead_of_current_pr(
    score_case,
):
    attempt = score_case.run / "attempts/cell/frozen"
    artifacts = attempt / "artifacts"
    artifacts.mkdir(parents=True)
    (attempt / "attempt.json").write_text(
        json.dumps(
            {
                "attempt_id": "frozen",
                "run_id": "run",
                "pr_url": score_case.url,
                "checkpoint_status": "ok",
            }
        ),
        encoding="utf-8",
    )
    shutil.copyfile(score_case.payload, artifacts / "post-review-payload.json")
    record = read_json(attempt / "attempt.json")
    record["source_artifact_hashes"] = {
        "artifacts/post-review-payload.json": file_hash(
            artifacts / "post-review-payload.json"
        )
    }
    (attempt / "attempt.json").write_text(json.dumps(record), encoding="utf-8")
    score_case.payload.write_text(
        json.dumps({"platform": "github", "payload": {"comments": []}}),
        encoding="utf-8",
    )
    packet = score_case.execute(attempt_selection={score_case.url: "frozen"})
    assert packet["metrics"]["total_candidates"] == 3
    assert packet["manifest"]["selected_attempts"] == {score_case.url: "frozen"}
    assert packet["ledger_row"]["validity_state"] == "historical_descriptive"
    with pytest.raises(ValueError, match="missing or ambiguous"):
        score_case.execute(attempt_selection={score_case.url: "absent"})


def test_same_number_prs_from_different_repositories_keep_distinct_identities(
    score_case,
):
    epoch = load_epoch(root=DEFAULT_ROOT)
    urls = sorted({b.pr_url for b in epoch.bugs if b.pr_url.endswith("/pull/1")})[:2]
    assert len(urls) == 2
    payload = score_case.payload.read_bytes()
    for index, url in enumerate(urls):
        (score_case.run / f"state/{'a' if index == 0 else 'b'}.json").write_text(
            json.dumps({"url": url, "status": "ok"}), encoding="utf-8"
        )
        pr = score_case.run / score.invoke.pr_dir_name({"url": url})
        pr.mkdir()
        (pr / "post-review-payload.json").write_bytes(payload)
    (score_case.run / "run.json").write_text(
        json.dumps({"run_id": "run", "urls": urls}), encoding="utf-8"
    )
    packet = score_case.execute()
    loaded = observations.load_packet(packet_path(packet))
    assert {c.pr_url for c in loaded.candidates} == set(urls)
    assert len({c.candidate_id for c in loaded.candidates}) == 6


@pytest.mark.parametrize(
    "mutation", ["observations", "profile", "raw_error", "evaluator", "context"]
)
def test_reader_rejects_semantic_corruption_even_with_rehashed_files(
    score_case, mutation
):
    packet = score_case.execute()
    path = packet_path(packet)
    if mutation == "observations":
        rows = [
            json.loads(line)
            for line in (path / "bugs.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        rows[0]["outcome"] = "missed"
        (path / "bugs.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )
    elif mutation == "raw_error":
        raw = read_json(path / "evaluations.json")
        raw[score_case.url]["deep-review"]["errors"] = [{"error": "failure"}]
        (path / "evaluations.json").write_text(json.dumps(raw), encoding="utf-8")
    elif mutation == "context":
        ctx_path = next((path / "contexts").glob("*.json"))
        ctx = read_json(ctx_path)
        ctx["hunk"] = "changed"
        ctx_path.write_text(json.dumps(ctx), encoding="utf-8")
    else:
        wire = read_json(path / "scores.json")
        if mutation == "profile":
            wire["profiles"]["core"]["tp"] += 1
        else:
            wire["manifest"]["evaluator"]["judge_pin"] = "changed"
            (path / "manifest.json").write_text(
                json.dumps(wire["manifest"]), encoding="utf-8"
            )
        (path / "scores.json").write_text(json.dumps(wire), encoding="utf-8")
    marker = read_json(path / "complete.json")
    marker["files"] = {name: file_hash(path / name) for name in marker["files"]}
    (path / "complete.json").write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(ValueError):
        observations.load_packet(path)
