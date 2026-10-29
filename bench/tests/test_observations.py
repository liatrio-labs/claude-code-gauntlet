"""Scoring refuses corrupt verdicts and publishes only complete evidence."""

import json
from pathlib import Path

import pytest

from bench.runner import ledger, observations, score
from bench.runner.contracts import artifact_allowed
from bench.runner.epochs import file_hash, read_json


@pytest.mark.parametrize(
    "relative",
    [
        "Pairing/receipt.json",
        "nested/Auth.json",
        ".CODEX/settings.json",
        ".ENV",
        "nested/.Env.local",
        "Credentials.json",
        "BACKUPS/receipt.GPG",
    ],
)
def test_artifact_guard_and_context_capture_deny_secret_names_in_any_case(
    tmp_path, relative
):
    assert not artifact_allowed(Path(relative))
    info = {"pr_dir": str(tmp_path)}
    assert score._head_file_lines(info, relative) == []
    assert artifact_allowed(Path("src/environment.py"))


def packet_path(packet):
    return Path(packet["ledger_row"]["packet_path"])


def test_score_packet_buckets_costs_and_replay_are_derived_once(score_case):
    packet = score_case.execute()
    path = packet_path(packet)
    loaded = observations.load_packet(path)
    stored = read_json(path / "scores.json")
    assert (
        "metrics" not in stored
        and "profiles" not in stored
        and not (path / "manifest.json").exists()
    )
    assert stored["costs"] == {
        "dedup": None,
        "judge": None,
        "adjudicator": None,
        "attribution": None,
    }
    assert len(loaded.observations) == packet["profiles"]["all"]["denominator"]
    assert packet["metrics"]["per_bucket"] == {
        "golden_matched": 2,
        "valid_extra": 0,
        "noise": 1,
    }
    assert {m.kind for b in loaded.observations for m in b.matches} == {
        "direct",
        "dedup_sibling",
    }
    assert len(loaded.adjudications) == 1
    ledger.replay_packet(score_case.ledger, path)
    assert len(score_case.ledger.read_text(encoding="utf-8").splitlines()) == 1
    previous = file_hash(path / "scores.json")
    other = score_case.execute()
    assert (
        other["score_ref"] != packet["score_ref"]
        and file_hash(path / "scores.json") == previous
    )
    with pytest.raises(FileExistsError):
        score_case.execute(score_id=packet["score_ref"]["score_id"])
    # Historical packets have no surface receipt; canonical candidate bytes stay unchanged.
    (path / "surface-views.json").unlink()
    stored = read_json(path / "scores.json")
    for c in stored["candidates"]:
        assert "surface" not in c and "issue_id" not in c
    complete = read_json(path / "complete.json")
    del complete["files"]["surface-views.json"]
    complete["files"]["scores.json"] = file_hash(path / "scores.json")
    score._write_json(path / "complete.json", complete)
    legacy = observations.load_packet(path).projection()
    assert legacy["metrics"] == packet["metrics"]
    assert legacy["surface_views"]["all"] is None


@pytest.mark.parametrize(
    "mutation", ["coverage", "match", "error", "dedup", "candidate", "no_evaluation"]
)
def test_invalid_scorer_output_cannot_become_a_packet(
    score_case, monkeypatch, mutation
):
    original = type(score_case).scorer

    def corrupt(self, pin, key, model):
        original(self, pin, key, model)
        ev = read_json(model / "evaluations.json")
        record = ev[self.url]["deep-review"]
        if mutation == "coverage":
            record["false_negatives"] = []
        elif mutation == "match":
            record["true_positives"][0]["matched_candidate"] = "absent"
        elif mutation == "error":
            record["errors"] = ["unavailable"]
        elif mutation == "dedup":
            (model / "dedup_groups.json").write_text(
                json.dumps({self.url: {"deep-review": [[0, 1], [1]]}}), encoding="utf-8"
            )
        elif mutation == "candidate":
            (model / "candidates.json").write_text("{}", encoding="utf-8")
        else:
            ev.clear()
        score._write_json(model / "evaluations.json", ev)

    monkeypatch.setattr(type(score_case), "scorer", corrupt)
    with pytest.raises(ValueError):
        score_case.execute()
    assert not score_case.ledger.exists()


@pytest.mark.parametrize(
    "field",
    [
        "outcome",
        "matches",
        "adjudication",
        "context",
        "source",
        "hash",
        "evaluator",
        "ledger_state",
        "ledger_profile",
        "ledger_schema",
        "ledger_missing",
        "surface_hash",
        "surface_join",
        "surface_identity",
        "surface_reference",
    ],
)
def test_reader_rechecks_joins_beyond_byte_hashes(score_case, field, tmp_path):
    if field.startswith("surface_"):
        from bench.tests.test_score import prepare_surface_case

        prepare_surface_case(score_case, tmp_path)
    path = packet_path(score_case.execute(surface_views=field.startswith("surface_")))
    if field.startswith("surface_"):
        target = (
            "surface-views.json"
            if field == "surface_reference"
            else "surface-all/view.json"
        )
        surface = read_json(path / target)
        if field == "surface_reference":
            surface["all_surface"] = "scores.json"
        elif field == "surface_join":
            surface["observations"][0]["outcome"] = "missed"
        elif field == "surface_identity":
            surface["candidates"][-1]["issue_id"] = "forged"
        else:
            surface["costs"]["judge"] = 1
        score._write_json(path / target, surface)
        if field != "surface_hash":
            complete = read_json(path / "complete.json")
            complete["files"][target] = file_hash(path / target)
            score._write_json(path / "complete.json", complete)
        with pytest.raises(ValueError):
            observations.load_packet(path)
        return
    data = read_json(path / "scores.json")
    if field == "evaluator":
        data["manifest"]["evaluator"]["judge_pin"] = "forged"
    elif field == "outcome":
        data["observations"][0]["outcome"] = "missed"
    elif field == "matches":
        data["observations"][0]["matches"] = []
    elif field == "adjudication":
        data["adjudications"] = []
    elif field == "context":
        data["contexts"][0]["hunk"] = "forged bytes"
    elif field == "source":
        data["candidates"][0]["source_trace_ids"] = ["made-up"]
    elif field == "ledger_state":
        data["metadata"]["validity_state"] = "unknown"
    elif field == "ledger_profile":
        data["metadata"]["profile"] = "unknown"
    elif field == "ledger_schema":
        data["metadata"]["schema_version"] = 1
    elif field == "ledger_missing":
        del data["metadata"]["denominator"]
    score._write_json(path / "scores.json", data)
    if field != "hash":
        complete = read_json(path / "complete.json")
        complete["files"]["scores.json"] = file_hash(path / "scores.json")
        score._write_json(path / "complete.json", complete)
    with pytest.raises(ValueError):
        observations.load_packet(path)


@pytest.mark.parametrize("failure", ["scorer", "adjudicator", "publish"])
def test_interrupted_score_is_pending_and_not_ledgered(
    score_case, monkeypatch, failure
):
    def fail(*args, **kwargs):
        raise RuntimeError("interrupted")

    if failure == "publish":
        monkeypatch.setattr(observations, "publish_packet", fail)
    elif failure == "scorer":
        monkeypatch.setattr(type(score_case), "scorer", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        score_case.execute(
            **({"adjudicator": fail} if failure == "adjudicator" else {})
        )
    assert not score_case.ledger.exists()
    assert all(
        p.name.startswith(".pending-")
        for p in (score_case.run / "scores/e616e84-v1").iterdir()
    )
