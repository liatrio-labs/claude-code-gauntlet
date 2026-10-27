"""Saved-input replay exercises evidence, proofs and actual delivery boundaries."""

import difflib
import json
from pathlib import Path

import pytest

from bench.runner import location_replay as replay


def _json(path, value):
    path.write_bytes(replay.encoded(value))


def _case(tmp_path, git_repo, *, scenario="normal"):
    root, git = git_repo
    head = [f"source_line_{i}" for i in range(1, 51)]
    head[6:8] = ["    chosen_value = compute_value()", "    return chosen_value"]
    head[9] = head[13] = "    repeated_value = repeated_compute()"
    (root / "source.txt").write_text("\n".join(head) + "\n", encoding="utf-8")
    git("add", "source.txt")
    git("commit", "-qm", "source fixture")
    sha = git("rev-parse", "HEAD")
    run = tmp_path / "run"
    parent = run / "pr-offline-saved-1"
    parent.mkdir(parents=True)
    _json(
        run / "run.json",
        {
            "pr_identities": {
                "saved": {
                    "owner": "offline",
                    "repo": "saved",
                    "pr_number": 1,
                    "head_sha": sha,
                }
            }
        },
    )
    old = list(head)
    old[6:8] = ["    deleted_value = old_value()"]
    raw = (
        "diff --git a/source.txt b/source.txt\n"
        + "".join(
            difflib.unified_diff(
                [line + "\n" for line in old],
                [line + "\n" for line in head],
                fromfile="a/source.txt",
                tofile="b/source.txt",
                n=100,
            )
        )
    ).encode()
    diff_path = parent / f"code-gauntlet-diff-{sha[:8]}.patch"
    diff_path.write_bytes(raw)
    context = (
        b"rules\n" * 6
        + b'<untrusted-code-content id="saved">\n'
        + raw
        + b'</untrusted-code-content id="saved">\n'
    )
    context_path = parent / f"code-gauntlet-context-{sha[:8]}.md"
    context_path.write_bytes(context)
    # Physical rows are counted directly, independently of the production walker.
    claimed = next(
        i
        for i, line in enumerate(context.decode().split("\n"), 1)
        if line == "+" + head[6]
    )
    repeated = next(
        i
        for i, line in enumerate(context.decode().split("\n"), 1)
        if line == " " + head[9]
    )
    assert 8 < claimed < len(head)
    original = [
        {
            "id": "positive",
            "line_start": claimed,
            "line_end": claimed + 1,
            "evidence": "The value fails: `chosen_value = compute_value()`",
        },
        {
            "id": "historical",
            "line_start": 7,
            "line_end": 8,
            "evidence": "Exact code:\n```text\n    chosen_value = compute_value()\n    return chosen_value\n```\n",
        },
        {
            "id": "missing",
            "line_start": 99999,
            "line_end": 99999,
            "evidence": "A retained observation without quoted source.",
        },
        {
            "id": "fragment",
            "line_start": repeated,
            "line_end": repeated,
            "evidence": "The expression `repeated_value = repeated_` fails.",
            "suggested_fix_code": "    replacement_value()",
        },
        {
            "id": "span",
            "line_start": claimed,
            "line_end": claimed + 3,
            "evidence": "The assignment `chosen_value = compute_value()` fails.",
            "suggested_fix_code": "    replacement_value()",
        },
        {
            "id": "bare",
            "line_start": 7,
            "line_end": 7,
            "evidence": "The failure occurs here: chosen_value = compute_value(); after the call.",
        },
        {
            "id": "whitespace",
            "line_start": 7,
            "line_end": 7,
            "evidence": "Exact code `chosen_value  = compute_value()` differs internally.",
        },
    ]
    if scenario == "wrong_move":
        original[1]["line_start"], original[1]["line_end"] = 20, 21
    if scenario == "missed_positive":
        original[0]["evidence"] = "The source was not quoted."
    if scenario == "ellipsis_positive":
        original[0]["evidence"] = "The expression `chosen_value = ...` fails."
    if scenario == "flattened_positive":
        original[0]["evidence"] = (
            "The expression `chosen_value = compute_value() return chosen_value` fails."
        )
    if scenario == "paraphrase_positive":
        original[0]["evidence"] = "The expression `chosen_value = other_call()` fails."
    if scenario == "blocked_positive":
        original[0]["line_start"] = 1
        original[0]["line_end"] = 1
    if scenario == "historical_unconfirmed":
        original[1]["evidence"] = "The source was not quoted."
    findings = [
        dict(
            file="source.txt",
            severity="high",
            confidence=90,
            title=f["id"],
            dimension="bug",
            description="Retained observation.",
            **f,
        )
        for f in original
    ]
    artifact = parent / f"code-gauntlet-phase4-input-{sha[:8]}.slice0.json"
    output_path = parent / f"code-gauntlet-phase4-output-{sha[:8]}.slice0.json"
    _json(artifact, {"findings": findings})
    _json(
        output_path,
        {"status": "ok", "result": {"verified": findings, "eliminated": []}},
    )
    _json(
        parent / "code-gauntlet-measurement-trace.json",
        {"versions": [{"finding": f} for f in findings]},
    )
    _json(parent / f"code-gauntlet-post-review-{sha[:8]}.json", {"findings": findings})
    map_dir = tmp_path / "map"
    map_dir.mkdir()
    rows = [
        dict(
            artifact=str(artifact),
            artifact_index=i,
            id=f["id"],
            file=f["file"],
            line_start=f["line_start"],
            line_end=f["line_end"],
            agent="reviewer",
            evidence=f["evidence"],
            source_mirror=str(root / ".git"),
            source_sha=sha,
            context_path=str(context_path),
            output_path=str(output_path),
            category="new-supported"
            if i == 1
            else "context-supported"
            if i == 0
            else "new-plausible",
            context_in_range=i == 0,
            outcome="verified",
        )
        for i, f in enumerate(findings)
    ]
    replay.write_jsonl(map_dir / "finding-evidence.jsonl", rows)
    replay.write_jsonl(
        map_dir / "run-inventory.jsonl", [{"path": str(run), "inputs": 1, "outputs": 1}]
    )
    blob = (root / "source.txt").read_bytes()

    def site(index, start):
        return dict(
            artifact=str(artifact),
            artifact_index=index,
            file="source.txt",
            dimension="bug",
            source_sha=sha,
            expected_start=start,
            head_text=blob.decode(),
            head_sha256=replay.digest(blob),
        )

    oracle = tmp_path / "sites.json"
    _json(
        oracle,
        dict(
            version=1,
            measured_sha256=replay.digest(
                (map_dir / "finding-evidence.jsonl").read_bytes()
            ),
            previously_correct=[site(1, findings[1]["line_start"])],
            hand_checked=[site(0, 7)],
            canary=dict(artifact=str(artifact), artifact_index=0),
            fence=dict(artifact=str(artifact), artifact_index=0),
        ),
    )
    return map_dir, oracle, rows


def _freeze(map_dir, oracle, path):
    assert (
        replay.main(
            [
                "--map-dir",
                str(map_dir),
                "--expectations",
                str(oracle),
                "--freeze-quotes",
                str(path),
            ]
        )
        == 0
    )


def _run(map_dir, oracle, frozen, output):
    return replay.main(
        [
            "--map-dir",
            str(map_dir),
            "--expectations",
            str(oracle),
            "--quote-spans",
            str(frozen),
            "--output-dir",
            str(output),
        ]
    )


def test_replay_counts_eliminated_records(tmp_path, git_repo, monkeypatch):
    map_dir, oracle, _rows = _case(tmp_path, git_repo)
    frozen, output = tmp_path / "quotes.jsonl", tmp_path / "result"
    _freeze(map_dir, oracle, frozen)
    original = replay.replay_wire

    def eliminate(*args, **kwargs):
        findings = original(*args, **kwargs)
        return (
            findings
            if kwargs.get("bypass")
            else [f for f in findings if f["id"] != "span"]
        )

    monkeypatch.setattr(replay, "replay_wire", eliminate)
    _run(map_dir, oracle, frozen, output)
    summary = json.loads((output / "summary.json").read_bytes())
    assert summary["range_eliminations"] == 1


def test_replay_keeps_denominator_and_evidence_adapter(tmp_path, git_repo, monkeypatch):
    map_dir, oracle, rows = _case(tmp_path, git_repo)
    frozen = tmp_path / "quotes.jsonl"
    with monkeypatch.context() as guard:
        guard.setattr(
            replay.location,
            "resolve_location",
            lambda *_args: pytest.fail("freeze resolved source"),
        )
        guard.setattr(
            replay, "annotate_diff", lambda *_args: pytest.fail("freeze walked a patch")
        )
        guard.setattr(
            replay, "read_source", lambda *_args: pytest.fail("freeze read source")
        )
        _freeze(map_dir, oracle, frozen)
    second = tmp_path / "quotes-again.jsonl"
    _freeze(map_dir, oracle, second)
    assert second.read_bytes() == frozen.read_bytes()
    first_out, second_out = tmp_path / "first", tmp_path / "second"
    # Synthetic fixtures cannot satisfy the CLI's fixed real-input denominator.
    assert _run(map_dir, oracle, frozen, first_out) == 1
    assert _run(map_dir, oracle, frozen, second_out) == 1
    for name in ("records.jsonl", "summary.json"):
        assert (first_out / name).read_bytes() == (second_out / name).read_bytes()
    records = [
        json.loads(line)
        for line in (first_out / "records.jsonl").read_bytes().splitlines()
    ]
    assert len(records) == len(rows)
    by_id = {r["id"]: r for r in records}
    assert by_id["positive"]["new_start"] == 7
    assert by_id["fragment"]["rule_b_only"] and by_id["fragment"]["rule_b_agrees"]
    assert by_id["fragment"]["new_start"] == 10 and by_id["fragment"]["inline"]
    assert not by_id["fragment"]["fence"] and not by_id["span"]["fence"]
    assert by_id["missing"]["status"] == by_id["whitespace"]["status"] == "unconfirmed"
    assert not any(by_id["missing"][k] for k in ("inline", "fence", "task"))
    assert by_id["bare"]["extraction_kind"] == "code_like"
    violations = (first_out / "violations.jsonl").read_text(encoding="utf-8")
    assert json.loads(violations) == {"violation": "fixed_denominator_mismatch"}
    controls = json.loads((first_out / "mutation.json").read_bytes())[
        "whole_resolver_bypass"
    ]
    assert controls[0]["broken"] and controls[0]["bypassed"] != 7


@pytest.mark.parametrize(
    ("evidence", "expected", "kind"),
    [
        ("Why `alpha_beta()` then `gamma_delta()`.", "gamma_delta()", "backtick"),
        ("`first_value()` or `other_value()`", "first_value()", "backtick"),
        (
            'Prefix: object.call("a  b"); after prose.',
            'object.call("a  b");',
            "code_like",
        ),
        (
            "Prefix:\n  first_value = call_value()\n  other_value = call_value()\nWords follow.",
            "  first_value = call_value()\n  other_value = call_value()",
            "code_like",
        ),
        ("```lang\n  exact_value()\n```\n", "  exact_value()\n", "fenced"),
        ("~~~~lang\nfirst_value()\n~~~\n~~~~~\n", "first_value()\n~~~\n", "fenced"),
        ("``first`value()``", "first`value()", "backtick"),
        ("`abcdefghijk`", None, "none"),
        ("`abcdefghijkl`", "abcdefghijkl", "backtick"),
        ("`" + "x" * 401 + "`", None, "none"),
        ("```\naaaa\nbbbb\ncccc\ndddd\neeee\n```\n", None, "none"),
        ("unclosed `object.call()", None, "none"),
        ("```lang\nobject.call()\n", None, "none"),
        ("Only words without a source quotation.", None, "none"),
    ],
)
def test_frozen_extraction_is_an_unchanged_evidence_slice(evidence, expected, kind):
    candidate = replay.extract_quote(evidence)
    assert (
        evidence[candidate.start : candidate.end] if candidate else None
    ) == expected
    assert (candidate.kind if candidate else "none") == kind


@pytest.mark.parametrize(
    "scenario",
    [
        "missed_positive",
        "ellipsis_positive",
        "flattened_positive",
        "paraphrase_positive",
        "wrong_inline",
        "unsafe_fence",
        "unsafe_patch",
        "context_disagreement",
        "failed_mutation",
        "historical_unconfirmed",
    ],
)
def test_replay_fails_missed_positives_or_unsafe_payloads(
    tmp_path, git_repo, monkeypatch, scenario
):
    map_dir, oracle, rows = _case(tmp_path, git_repo, scenario=scenario)
    frozen, output = tmp_path / "quotes.jsonl", tmp_path / "result"
    _freeze(map_dir, oracle, frozen)
    if scenario in ("wrong_inline", "unsafe_fence"):
        original = replay.post.DeliverySession.post

        def corrupt(self, request, *, forge):
            result = original(self, request, forge=forge)
            comment = request.payload["comments"][0]
            if scenario == "wrong_inline":
                comment["line"] += 1
            else:
                comment["body"] += "\n```suggestion\nwrong replacement\n```\n"
            return result

        monkeypatch.setattr(replay.post.DeliverySession, "post", corrupt)
    elif scenario == "unsafe_patch":
        original = replay.patches._execute

        def corrupt_patch(args):
            receipt, code = original(args)
            receipt["kept"] += 1
            return receipt, code

        monkeypatch.setattr(replay.patches, "_execute", corrupt_patch)
    elif scenario == "context_disagreement":
        monkeypatch.setattr(
            replay, "saved_head_row", lambda *_args: "different head row"
        )
    elif scenario == "failed_mutation":
        original = replay.replay_wire

        def no_mutation(*args, **kwargs):
            return original(*args, **{**kwargs, "bypass": False})

        monkeypatch.setattr(replay, "replay_wire", no_mutation)
    assert _run(map_dir, oracle, frozen, output) == 1
    violations = [
        json.loads(line)["violation"]
        for line in (output / "violations.jsonl").read_bytes().splitlines()
    ]
    if scenario == "historical_unconfirmed":
        assert violations == ["fixed_denominator_mismatch"]
        summary = json.loads((output / "summary.json").read_bytes())
        assert summary["previously_correct_became_unconfirmed"] == {"none": 1}
    else:
        expected = {
            "missed_positive": "positive_not_recovered",
            "ellipsis_positive": "positive_not_recovered",
            "flattened_positive": "positive_not_recovered",
            "paraphrase_positive": "positive_not_recovered",
            "wrong_inline": "unsafe_inline_payload",
            "unsafe_fence": "unsafe_rendered_fence",
            "unsafe_patch": "unsafe_patch_receipt",
            "context_disagreement": "rule_b_context_disagreement",
            "failed_mutation": "resolver_bypass_not_detected",
        }
        assert expected[scenario] in violations
        if scenario.endswith("positive"):
            summary = json.loads((output / "summary.json").read_bytes())
            (miss,) = summary["missed_positives"]
            diagnosis = miss["diagnosis"]
            assert (
                diagnosis["classification"]
                == {
                    "missed_positive": "no_anchor",
                    "ellipsis_positive": "ellipsis",
                    "flattened_positive": "flattened_multiline",
                    "paraphrase_positive": "paraphrase_or_other_mismatch",
                }[scenario]
            )
            assert not diagnosis["anchor_matches_any_source"]
            assert not diagnosis["anchor_matches_expected_source"]
            assert diagnosis["matching_starts"] == []
            assert json.dumps(miss, sort_keys=True) in (output / "report.md").read_text(
                encoding="utf-8"
            )


@pytest.mark.parametrize("scenario", ["wrong_move", "blocked_positive"])
def test_replay_recovers_unique_anchors_and_describes_historical_distance(
    tmp_path, git_repo, scenario
):
    map_dir, oracle, rows = _case(tmp_path, git_repo, scenario=scenario)
    frozen, output = tmp_path / "quotes.jsonl", tmp_path / "result"
    _freeze(map_dir, oracle, frozen)
    assert _run(map_dir, oracle, frozen, output) == 1
    assert json.loads((output / "violations.jsonl").read_bytes()) == {
        "violation": "fixed_denominator_mismatch"
    }
    summary = json.loads((output / "summary.json").read_bytes())
    assert summary["known_recoveries"] == 1
    assert summary["previously_correct_moves_by_distance"] == (
        {"13": 1} if scenario == "wrong_move" else {}
    )


@pytest.mark.parametrize("bypass", [False, True])
def test_live_replay_uses_real_anchors_and_checks_every_placed_span(
    tmp_path, git_repo, monkeypatch, bypass
):
    map_dir, oracle, rows = _case(tmp_path, git_repo)
    artifact = Path(rows[0]["artifact"])
    directory = artifact.parent
    run = directory.parent
    sha = rows[0]["source_sha"]
    diff = (directory / f"code-gauntlet-diff-{sha[:8]}.patch").read_bytes()
    context = Path(rows[0]["context_path"]).read_bytes()
    _, entries = replay.reconstruct_map(diff, context)
    _json(
        directory / f"code-gauntlet-context-map-{sha[:8]}.json",
        {
            "version": 1,
            "sha": sha[:8],
            "status": "available",
            "context_sha256": replay.digest(context),
            "diff_sha256": replay.digest(diff),
            "entries": {str(k): v for k, v in entries.items()},
        },
    )
    findings = json.loads(artifact.read_bytes())["findings"]
    anchors = [
        "chosen_value = compute_value()\nreturn chosen_value",
        "chosen_value = compute_value()\nreturn chosen_value",
        None,
        "repeated_value = repeated_compute()",
        "chosen_value = compute_value()",
        "chosen_value = compute_value()",
        "chosen_value  = compute_value()",
    ]
    for finding, anchor in zip(findings, anchors, strict=True):
        finding["evidence"] = "This prose has no source quote."
        if anchor is not None:
            finding["anchor"] = anchor
    for index in (0, 3, 4):
        findings[index]["line_start"] = 1
    _json(artifact, {"findings": findings})
    root, _ = git_repo
    monkeypatch.setattr(replay, "_mirror_dirname", lambda _url: ".git")
    monkeypatch.setattr(
        replay, "extract_quote", lambda *_args: pytest.fail("live replay used adapter")
    )
    if bypass:
        original = replay.location.resolve_location

        def corrupt(finding, *args):
            if finding["id"] == "positive":
                return replay.location.Resolution(1, 2, "relocated", "unique")
            return original(finding, *args)

        monkeypatch.setattr(replay.location, "resolve_location", corrupt)
    output = tmp_path / "live"
    assert replay.main(
        [
            "--live-run",
            str(run),
            "--mirrors-dir",
            str(root),
            "--output-dir",
            str(output),
        ]
    ) == int(bypass)
    summary = json.loads((output / "live-replay.json").read_bytes())
    assert summary["records"] == 7 and summary["artifacts"] == 1
    assert summary["statuses"] == {"confirmed": 2, "relocated": 2, "unconfirmed": 3}
    assert summary["anchor_mismatches"] == int(bypass)
    assert summary["anchored_records"] == 6
    by_id = {r["id"]: r for r in summary["findings"]}
    positive = by_id["positive"]
    assert (positive["new_start"], positive["new_end"]) == (
        (1, 2) if bypass else (7, 8)
    )
    assert positive["anchor_in_resolved_span"] is (not bypass)
    assert positive["source_sha"] == sha
    assert by_id["missing"]["anchor_in_resolved_span"] is None
    assert by_id["fragment"]["reason"] == "ambiguous"
    assert all(
        r["anchor_in_resolved_span"] is not None
        for r in summary["findings"]
        if r["status"] != "unconfirmed"
    )
    assert "Contains anchor" in (output / "live-replay.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "damage",
    [
        "hash",
        "oracle",
        "duplicate",
        "missing",
        "offset",
        "source",
        "identity",
        "unsafe",
    ],
)
def test_replay_rejects_changed_or_missing_inputs(tmp_path, git_repo, damage):
    map_dir, oracle, rows = _case(tmp_path, git_repo)
    frozen = tmp_path / "quotes.jsonl"
    _freeze(map_dir, oracle, frozen)
    if damage == "hash":
        Path(rows[0]["artifact"]).write_bytes(b'{"findings": []}')
    elif damage == "oracle":
        oracle.write_bytes(oracle.read_bytes() + b"\n")
    elif damage in ("duplicate", "offset"):
        documents = [json.loads(line) for line in frozen.read_bytes().splitlines()]
        if damage == "duplicate":
            documents.append(documents[-1])
        else:
            documents[1]["offsets"] = [0, 14]
        replay.write_jsonl(frozen, documents)
    elif damage == "missing":
        Path(rows[0]["output_path"]).unlink()
    elif damage == "source":
        changed = json.loads(oracle.read_bytes())
        changed["hand_checked"][0]["head_text"] = "changed"
        _json(oracle, changed)
    elif damage == "identity":
        rows[0]["source_sha"] = "0" * 40
        replay.write_jsonl(map_dir / "finding-evidence.jsonl", rows)
    else:
        rows[0]["file"] = "../outside"
        replay.write_jsonl(map_dir / "finding-evidence.jsonl", rows)
    assert _run(map_dir, oracle, frozen, tmp_path / "result") == 2
