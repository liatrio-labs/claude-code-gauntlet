"""Scoring boundary controls and recorded charge semantics."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bench.runner import observations, score
from bench.runner.contracts import pr_dir_name
from bench.runner.epochs import read_json


def prepare_surface_case(score_case, tmp_path):
    from bench.tests.test_provenance import payload_for, surface_review

    path, review, diff = surface_review(tmp_path, grouped=False)
    pr = score_case.payload.parent
    (pr / "diff.patch").write_text(diff, encoding="utf-8")
    score._write_json(pr / "code-gauntlet-post-review-abc1234.json", review)
    (pr / "code-gauntlet-measurement-abc1234.json").write_bytes(path.read_bytes())
    score._write_json(
        score_case.payload,
        payload_for(
            review["findings"],
            diff,
            "github",
            review_body=review["review_body"],
            sha=review["sha"],
        ),
    )
    return review


def test_surface_views_preserve_candidate_order_across_prs(score_case, tmp_path):
    prepare_surface_case(score_case, tmp_path)
    epoch = score.load_epoch()
    second_url = next(b.pr_url for b in epoch.bugs if b.pr_url != score_case.url)
    second_pr = score_case.run / pr_dir_name({"url": second_url})
    second_pr.mkdir()
    second_tmp = tmp_path / "second"
    second_tmp.mkdir()
    prepare_surface_case(
        replace(
            score_case, url=second_url, payload=second_pr / "post-review-payload.json"
        ),
        second_tmp,
    )
    score._write_json(
        score_case.run / "state/second.json", {"url": second_url, "status": "ok"}
    )
    score._write_json(
        score_case.run / "run.json",
        {"run_id": "run", "urls": [score_case.url, second_url]},
    )
    result = score_case.execute(surface_views=True)
    loaded = observations.load_packet(Path(result["ledger_row"]["packet_path"]))
    assert loaded.surface_view is not None
    assert [(c.pr_url, c.surface) for c in loaded.surface_view.candidates] == [
        (url, surface)
        for url in sorted((score_case.url, second_url))
        for surface in ("inline", "body", "body")
    ]
    assert result["surface_views"]["all"]["metrics"]["n_prs"] == 2


@pytest.mark.parametrize(
    "kind", ["absolute", "traversal", "destination_symlink", "surface_symlink"]
)
def test_surface_source_copy_rejects_unsafe_paths(tmp_path, kind):
    epoch = score.load_epoch()
    url = epoch.bugs[0].pr_url
    run = tmp_path / "run"
    packet = run / "packet"
    pr = packet / "sources/pr"
    head = pr / "worktree/source.py"
    head.parent.mkdir(parents=True)
    head.write_text("source context", encoding="utf-8")
    (head.parent / "nested").mkdir()
    path = str(head) if kind == "absolute" else "nested/../source.py"
    message = "surface candidate path must be relative without traversal"
    if kind in ("destination_symlink", "surface_symlink"):
        path = "source.py"
        outside = tmp_path / "outside"
        outside.mkdir()
        surface = packet / "surface-all"
        if kind == "surface_symlink":
            surface.symlink_to(outside, target_is_directory=True)
        else:
            surface.mkdir()
            (surface / "sources").symlink_to(outside, target_is_directory=True)
        message = "surface destination escapes packet root"
    mapped = observations.candidate_records(
        {
            url: {
                score.TOOL: [
                    dict(text="body", path=path, surface="body", issue_id="issue")
                ]
            }
        },
        {url: "attempt"},
    )
    request = observations.ScoreRequest(
        score.ScoreRef("run", epoch.epoch_id, "score"),
        epoch,
        packet,
        (url,),
        ((url, "attempt"),),
        (),
        score.TOOL,
        observations.Evaluator(
            epoch.judge_pin, epoch.adjudicator_pin, epoch.scorer_tree, "fixed", 0.0
        ),
    )
    with pytest.raises(ValueError, match=message):
        score.score_surface_view(
            request,
            mapped,
            (),
            {url: {"pr_dir": str(pr)}},
            (),
            None,
            {},
            lambda *args: pytest.fail("unsafe source reached scoring"),
            lambda *args: pytest.fail("unsafe source reached adjudication"),
            run,
        )
    assert head.read_text(encoding="utf-8") == "source context"
    if kind in ("destination_symlink", "surface_symlink"):
        assert list(outside.iterdir()) == []
    else:
        assert not (packet / "surface-all").exists()


def test_surface_failure_without_message_publishes_readable_receipt(
    score_case, tmp_path, monkeypatch
):
    prepare_surface_case(score_case, tmp_path)
    original = score.provenance.trace_delivery

    def fail_body(*args, **kwargs):
        if kwargs.get("include_body"):
            raise ValueError()
        return original(*args, **kwargs)

    monkeypatch.setattr(score.provenance, "trace_delivery", fail_body)
    result = score_case.execute(surface_views=True)
    loaded = observations.load_packet(Path(result["ledger_row"]["packet_path"]))
    assert loaded.surface_view is None
    assert read_json(loaded.root / "surface-views.json")["unavailable_reason"]


@pytest.mark.parametrize("body_evidence", ["available", "missing", "tampered"])
def test_scoring_preserves_inline_and_all_surface_receipts(
    score_case, tmp_path, monkeypatch, body_evidence
):
    from bench.runner.epochs import load_epoch
    from bench.tests.test_provenance import payload_for

    review = prepare_surface_case(score_case, tmp_path)
    if body_evidence == "missing":
        del review["review_body"]
    elif body_evidence == "tampered":
        review["review_body"] += " changed"
    score._write_json(
        score_case.payload.parent / "code-gauntlet-post-review-abc1234.json", review
    )
    epoch = load_epoch()
    empty_url = next(b.pr_url for b in epoch.bugs if b.pr_url != score_case.url)
    pr = score_case.run / pr_dir_name({"url": empty_url})
    pr.mkdir()
    score._write_json(
        score_case.run / "state/empty.json", {"url": empty_url, "status": "ok"}
    )
    score._write_json(
        score_case.run / "run.json",
        {"run_id": "run", "urls": [score_case.url, empty_url]},
    )
    trace = read_json(
        score_case.payload.parent / "code-gauntlet-measurement-abc1234.json"
    )
    trace["versions"] = []
    for snapshot in trace["snapshots"]:
        snapshot.update(finding_version_ids=[], decisions=[])
    score._write_json(pr / "code-gauntlet-measurement-empty.json", trace)
    empty_review = dict(findings=[], review_body="No issues.", sha="b" * 40)
    score._write_json(pr / "code-gauntlet-post-review-empty.json", empty_review)
    (pr / "diff.patch").write_text("", encoding="utf-8")
    score._write_json(
        pr / "post-review-payload.json",
        payload_for(
            [],
            "",
            "github",
            review_body=empty_review["review_body"],
            sha=empty_review["sha"],
        ),
    )
    original = type(score_case).scorer

    def scorer(self, pin, key, model):
        self.hits["*"] = (0, 2) if "surface-all" in model.parts else (0, 1)
        original(self, pin, key, model)
        if "surface-all" in model.parts:
            evaluations = read_json(model / "evaluations.json")
            body = read_json(model / "candidates.json")[self.url]["deep-review"][-1][
                "text"
            ]
            for tp in evaluations[self.url]["deep-review"]["true_positives"]:
                tp["matched_candidate"] = body
            evaluations[self.url]["deep-review"]["fp"] = 2
            score._write_json(model / "evaluations.json", evaluations)

    monkeypatch.setattr(type(score_case), "scorer", scorer)
    adjudicator_calls = []

    def adjudicate(text, hunk, context, pin, key):
        adjudicator_calls.append((text, hunk, context))
        return {
            "bucket": "noise",
            "usage": [
                {
                    "model": pin,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
            ],
        }

    result = score_case.execute(adjudicator=adjudicate, surface_views=True)
    loaded = observations.load_packet(Path(result["ledger_row"]["packet_path"]))
    assert result["profiles"]["all"]["tp"] == 2
    assert result["metrics"]["n_prs"] == 2
    assert len(loaded.candidates) == 1 and all(
        c.surface == "inline" for c in loaded.candidates
    )
    assert "surface_views" not in result["ledger_row"]
    assert read_json(loaded.root / "scores.json")["costs"].keys() == {
        "dedup",
        "judge",
        "adjudicator",
        "attribution",
    }
    if body_evidence != "available":
        assert result["surface_views"]["all"] is None
        assert len(score_case.calls) == 1
        return
    assert len(score_case.calls) == 2
    view = loaded.surface_view
    assert [c.surface for c in view.candidates] == ["inline", "body", "body"]
    assert result["surface_views"]["all"]["profiles"]["all"]["tp"] == 3
    assert result["surface_views"]["all"]["profiles"]["all"]["fp"] == 1
    assert result["surface_views"]["all"]["metrics"]["n_prs"] == 2
    assert len({b.bug_id for b in view.observations if b.outcome == "matched"}) == 3
    repeated = (*view.candidates, replace(view.candidates[0], surface="body"))
    assert observations.surface_candidates(iter(repeated)) == view.candidates
    assert len(view.contexts) == len(view.adjudications) == len(adjudicator_calls) == 1
    assert view.costs["adjudicator"] == pytest.approx(0.00075)
    assert loaded.costs["adjudicator"] == 0
    assert view.costs.keys() == {"dedup", "judge", "adjudicator"}
    assert loaded.projection() == result


@pytest.mark.parametrize("failure", ["scorer", "adjudicator", "receipt", "process"])
def test_failed_surface_pass_still_publishes_canonical_score(
    score_case, tmp_path, monkeypatch, failure
):
    prepare_surface_case(score_case, tmp_path)
    original = type(score_case).scorer

    def scorer(self, pin, key, model):
        original(self, pin, key, model)
        score._write_json(model / "judge-usage.json", [])
        if "surface-all" in model.parts and failure == "scorer":
            raise RuntimeError("surface judge unavailable")
        if "surface-all" in model.parts and failure == "receipt":
            score._write_json(model / "candidates.json", {})
        if "surface-all" in model.parts and failure == "process":
            raise score.subprocess.CalledProcessError(1, ["surface-scorer"])

    def adjudicate(*args):
        if failure == "adjudicator":
            raise ValueError("surface adjudicator unavailable")
        return {"bucket": "noise"}

    monkeypatch.setattr(type(score_case), "scorer", scorer)
    result = score_case.execute(surface_views=True, adjudicator=adjudicate)
    path = Path(result["ledger_row"]["packet_path"])
    loaded = observations.load_packet(path)
    assert result["profiles"]["all"]["tp"] == 2
    assert result["ledger_row"]["judge_cost_usd"] == 0
    assert loaded.surface_view is None
    assert result["surface_views"]["all"] is None
    receipt = read_json(path / "surface-views.json")
    assert receipt["all_surface"] is None
    if failure == "receipt":
        assert receipt["unavailable_reason"] == (
            "all-surface scoring failed: surface candidate or evaluator join mismatch"
        )
    elif failure == "process":
        assert receipt["unavailable_reason"] == (
            "all-surface scoring failed: "
            + str(score.subprocess.CalledProcessError(1, ["surface-scorer"]))
        )
    else:
        component = "judge" if failure == "scorer" else failure
        assert receipt["unavailable_reason"] == (
            f"all-surface scoring failed: surface {component} unavailable"
        )
    assert len(score_case.calls) == 2
    assert list((path / "surface-all").rglob("judge-usage.json"))
    assert read_json(score_case.run / "latest-score.json") == result["score_ref"]
    assert len(score_case.ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_surface_opt_in_preserves_canonical_packet_bytes(
    score_case, tmp_path, monkeypatch
):
    from bench.runner import score
    from bench.runner.epochs import canonical, digest

    prepare_surface_case(score_case, tmp_path)
    head = score_case.payload.parent / "worktree/body-1.py"
    head.parent.mkdir()
    head.write_text("body source context\n" * 1000, encoding="utf-8")
    monkeypatch.setattr(score, "_now", lambda: "2026-01-01T00:00:00Z")
    monkeypatch.setattr(score, "_git_head", lambda: "a" * 40)
    epoch = score.load_epoch()
    evaluator = observations.Evaluator(
        epoch.judge_pin, epoch.adjudicator_pin, epoch.scorer_tree, "fixed", 0.0
    )
    monkeypatch.setattr(score, "evaluator_for", lambda *args: evaluator)
    score_id = "b" * 32
    baseline = score_case.execute(score_id=score_id)
    path = Path(baseline["ledger_row"]["packet_path"])
    canonical_bytes = (path / "scores.json").read_bytes()
    manifest_bytes = canonical(read_json(path / "scores.json")["manifest"])
    assert len(score_case.calls) == 1
    assert baseline["surface_views"]["all"] is None
    assert not (path / "surface-all").exists()
    stored = read_json(path / "scores.json")
    stored["metadata"]["packet_path"] = "PACKET"
    # Fixed evaluator/time and a normalized packet path make the wire bytes reproducible.
    assert (
        canonical_bytes.replace(str(path).encode(), b"PACKET")
        == canonical(stored) + b"\n"
    )
    assert (
        digest(stored)
        == "6363379781409eaf53669842ff40d2e8d79a1835e8e13fad561d20d818340643"
    )
    assert (
        digest(stored["manifest"])
        == "9a7149fffc5a0b34cba3a33887a4fcc1dd77352f2f9e9e0e2dbc29134bad895e"
    )
    path.rename(tmp_path / "canonical-packet")

    result = replace(score_case, ledger=tmp_path / "surface-ledger.jsonl").execute(
        score_id=score_id, surface_views=True
    )
    assert (path / "scores.json").read_bytes() == canonical_bytes
    assert canonical(read_json(path / "scores.json")["manifest"]) == manifest_bytes
    assert result["metrics"] == baseline["metrics"]
    assert len(score_case.calls) == 3
    view = observations.load_packet(path).surface_view
    relative = head.relative_to(score_case.run)
    assert relative.as_posix() in view.source_artifact_hashes
    assert (path / "surface-all/sources" / relative).read_bytes() == head.read_bytes()
    assert not (path / "sources" / relative).exists()
    assert relative.as_posix() not in stored["manifest"]["source_artifact_hashes"]


@pytest.mark.parametrize("kind", ["duplicate", "missing_payload", "unresolved", "pin"])
def test_score_refuses_incomplete_or_inconsistent_inputs(score_case, monkeypatch, kind):
    options = {}
    if kind == "duplicate":
        (score_case.run / "run.json").write_text(
            json.dumps({"urls": [score_case.url] * 2}), encoding="utf-8"
        )
    elif kind == "missing_payload":
        score_case.payload.unlink()
    elif kind == "unresolved":
        (score_case.run / "state/a.json").write_text(
            json.dumps({"url": score_case.url, "status": "failed"}), encoding="utf-8"
        )
    else:
        options["env"] = {
            "BENCH_JUDGE_API_KEY": "synthetic",
            "MARTIAN_MODEL": "different",
        }
    with pytest.raises((ValueError, RuntimeError)):
        score_case.execute(**options)
    assert not score_case.calls


def test_a_run_copied_to_another_checkout_keeps_its_recorded_payload(tmp_path):
    run = tmp_path / "copy" / "runs" / "mini-x"
    (run / "state").mkdir(parents=True)
    (run / "pr-a").mkdir()
    (run / "pr-a" / "post-review-payload.json").write_text("{}", encoding="utf-8")
    recorded = "/elsewhere/bench/workspace/runs/mini-x/pr-a/post-review-payload.json"
    (run / "state" / "a.json").write_text(
        '{"url": "https://github.com/o/r/pull/1", "status": "ok", '
        f'"detail": {{"payload_path": "{recorded}"}}}}',
        encoding="utf-8",
    )
    from bench.runner.score import _resolve_pr_dir, _run_pr_records

    ((url, _, hint),) = _run_pr_records(run)
    assert _resolve_pr_dir(run, url, hint) == run / "pr-a"


def test_scoring_resolves_recorded_current_legacy_and_nested_payloads_in_order(
    score_case,
):
    current = score_case.payload.parent
    number = pr_dir_name({"url": score_case.url}).rsplit("-", 1)[1]
    legacy = score_case.run / ("pr-" + number)
    recorded = score_case.run / "recorded"
    score_case.hits["*"] = ()

    def payload(path, text):
        score._write_json(
            path,
            {"platform": "github", "payload": {"comments": [dict(body=text)]}},
        )

    def resolved_text(hint=None):
        score._write_json(
            score_case.run / "state/a.json",
            dict(url=score_case.url, status="ok", detail={"payload_path": hint}),
        )
        result = score_case.execute()
        packet = observations.load_packet(Path(result["ledger_row"]["packet_path"]))
        return [c.text for c in packet.candidates]

    payload(current / "post-review-payload.json", "current")
    payload(legacy / "post-review-payload.json", "legacy")
    payload(recorded / "post-review-payload.json", "recorded")
    assert resolved_text() == ["current"]
    assert resolved_text(str(recorded / "post-review-payload.json")) == ["recorded"]
    stale = str(score_case.run / "missing/post-review-payload.json")
    assert resolved_text(stale) == ["current"]
    current.rename(score_case.run / "archived-current")
    assert resolved_text(stale) == ["legacy"]
    (legacy / "post-review-payload.json").unlink()
    payload(legacy / "0/superseded/post-review-payload.json", "superseded")
    payload(legacy / "1/nested/post-review-payload.json", "nested")
    assert resolved_text(stale) == ["nested"]


def test_same_text_comments_receive_their_own_context_and_independent_verdicts(
    score_case,
):
    score_case.hits["*"] = ()
    comments = [dict(body="same claim", path=p, line=2) for p in ("a.py", "b.py")]
    score._write_json(
        score_case.payload, dict(platform="github", payload={"comments": comments})
    )
    pr = score_case.payload.parent
    (pr / "diff.patch").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n context-a\n+only-a\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
        "@@ -1,2 +1,2 @@\n context-b\n+only-b\n",
        encoding="utf-8",
    )
    (pr / "worktree").mkdir()
    for name in ("a", "b"):
        (pr / "worktree" / (name + ".py")).write_text(
            f"context-{name}\nonly-{name}\n", encoding="utf-8"
        )
    calls = []

    def adjudicate(text, hunk, context, pin, key):
        calls.append((text, hunk, context))
        return {"bucket": "valid_extra" if "+only-b" in hunk else "noise"}

    result = score_case.execute(adjudicator=adjudicate)
    packet = observations.load_packet(Path(result["ledger_row"]["packet_path"]))
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == "same claim"
    assert "+only-a" in calls[0][1] and "only-b" not in calls[0][1]
    assert "+only-b" in calls[1][1] and "only-a" not in calls[1][1]
    assert "only-a" in calls[0][2] and "only-b" in calls[1][2]
    paths = {c.candidate_id: c.path for c in packet.candidates}
    assert {paths[a.candidate_id]: a.bucket for a in packet.adjudications} == {
        "a.py": "noise",
        "b.py": "valid_extra",
    }
    assert len({c.context_sha256 for c in packet.contexts}) == 2
    assert read_json(packet.root / "scores.json")["costs"]["adjudicator"] is None
