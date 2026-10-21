"""Exact allowances, generation coverage and sealed diagnostics."""

from dataclasses import asdict, replace
from pathlib import Path

import pytest

from bench.runner import leaks as l
from bench.runner.epochs import canonical, digest, load_epoch, read_json


@pytest.fixture
def lexical(monkeypatch):
    epoch = load_epoch()
    policy = read_json(l.ROOT / "bench/golden/leak_allowlist.json")
    derived = l.derive_identifiers(
        epoch,
        holdout_pr_urls=frozenset(epoch.subsets["holdout"]),
        holdout_salt=policy["holdout_salt"],
    )
    monkeypatch.setattr(l, "generated_surfaces", lambda *args: ())
    monkeypatch.setattr(l, "_shared_context_surfaces", lambda *args: ())
    return epoch, derived, {**policy, "entries": [], "passages": []}


def test_repository_scan_and_no_deleted_allowance_policy():
    report = l.scan_repository()
    assert report.passed and not report.stale_allowances
    assert len(report.hits) == 13
    policy = read_json(l.ROOT / "bench/golden/leak_allowlist.json")
    assert policy["entries"] and all(
        e["expires_wave"] == "2a" for e in policy["entries"]
    )
    assert not (l.ROOT / "bench/golden/leak_allowlist_ceiling.json").exists()


def test_exact_counts_stale_entries_and_wave_deadline(lexical):
    epoch, derived, policy = lexical
    token = derived.identifiers[0].token
    surface = l.PromptSurface("surface", "agents/a.md", "agents/a.md", "static", token)
    report = l.scan(derived, [surface], policy)
    assert not report.passed and len(report.unallowed_hits) > 0
    policy["entries"] = [
        {
            **{
                k: asdict(h)[k]
                for k in (
                    "identifier_id",
                    "surface_id",
                    "source_path",
                    "excerpt_sha256",
                )
            },
            "expected_count": 1,
            "expires_wave": "2a",
        }
        for h in report.hits
    ]
    assert l.scan(derived, [surface], policy).passed
    for changed in (
        replace(surface, text=""),
        replace(surface, text=token + "\n" + token),
        replace(surface, text="prefix " + token),
        replace(surface, source_path="agents/b.md"),
    ):
        assert not l.scan(derived, [changed], policy).passed
    expired = {**policy, "current_wave": "2a"}
    with pytest.raises(ValueError, match="empty"):
        l.scan(derived, [surface], expired)
    assert l.scan(derived, [], {**expired, "entries": []}).passed


def test_generic_predicate_is_exact_and_does_not_exempt_project_members(lexical):
    _, derived, policy = lexical
    for token in (
        "JavaScript",
        "NullPointerException",
        "if/else",
        "time.sleep",
        "AssertionError",
    ):
        assert l.ordinary_vocabulary(token)
    for token in ("project.JavaScript", "customAuthContext", "project.time.sleep"):
        assert not l.ordinary_vocabulary(token)
    assert not any(l.ordinary_vocabulary(i.token) for i in derived.identifiers)


def test_sealed_hits_redact_the_entire_line_and_serialization(lexical, tmp_path):
    epoch, derived, policy = lexical
    # Synthetic sealed wording proves diagnostics without using a holdout answer.
    symbol = "SealedSyntheticSymbol"
    hidden = l.HoldoutLeakIdentifier(
        digest([derived.holdout_salt, "identifier", symbol]),
        "identifier",
    )
    derived = replace(derived, holdout_identifiers=(hidden,))
    surface = l.PromptSurface("surface", "a.md", "a.md", "static", symbol)
    report = l.scan(derived, [surface], policy)
    assert (
        not report.passed and report.hits[0].holdout and report.hits[0].excerpt is None
    )
    assert symbol.encode() not in canonical(asdict(report))
    path = tmp_path / "output.json"
    path.write_text(symbol, encoding="utf-8")
    with pytest.raises(ValueError, match="sealed token") as error:
        l.assert_no_holdout_tokens(derived, epoch, [path], root=tmp_path)
    assert symbol not in str(error.value)


def test_generated_prompts_and_passages_cannot_disappear(lexical, monkeypatch):
    _, derived, policy = lexical
    surface = l.PromptSurface(
        "generated/legacy/a", "p.js", "p.js", "legacy", "a scenario"
    )
    monkeypatch.setattr(l, "generated_surfaces", lambda *args: (surface,))
    assert not l.scan(derived, [], policy).passed
    assert not l.scan(derived, [replace(surface, text="changed")], policy).passed
    policy["passages"] = [
        dict(
            surface_id=surface.surface_id,
            source_path=surface.source_path,
            excerpt_sha256=digest([derived.holdout_salt, surface.text]),
            line_count=1,
            expected_count=1,
            expires_wave="2a",
        )
    ]
    assert l.scan(derived, [surface], policy).passed
    assert not l.scan(derived, [replace(surface, text="")], policy).passed


def test_surface_collection_is_confined_and_refuses_credentials(tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills/a.md").write_text("ordinary agent text", encoding="utf-8")
    (tmp_path / "skills/auth.json").write_text("synthetic denied", encoding="utf-8")
    (tmp_path / "skills/escape.md").symlink_to(Path(__file__))
    monkeypatch.setattr(l, "_shared_context_surfaces", lambda *args: ())
    monkeypatch.setattr(l, "_workflow_literal_surfaces", lambda *args: ())
    surfaces = l.collect_surfaces(tmp_path, ())
    assert [s.surface_id for s in surfaces] == ["skills/a.md"]
