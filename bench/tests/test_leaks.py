"""Exact allowances, passage removal and sealed diagnostics."""

from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pytest

from bench.runner import leaks as l
from bench.runner.epochs import (
    GoldenEpoch,
    build_bug_index,
    canonical,
    digest,
    load_epoch,
    read_json,
)
from bench.tests.conftest import refuses


@pytest.fixture
def controlled():
    comments = (
        "CrossProjectSymbol SameOwnerSymbol PublicSymbol WrappedSealedSymbolTail path/SealedRoute/file",
        "SameOwnerSymbol",
        "CrossProjectSymbol SealedSymbol SealedRoute",
    )
    data = {
        url: {
            "pr_title": "Synthetic PR",
            "original_url": None,
            "source_repo": "synthetic",
            "reviews": [],
            "golden_comments": [dict(comment=text, severity="High", category="bug")],
        }
        for url, text in zip(("pr-a", "pr-b", "pr-c"), comments, strict=True)
    }
    projects = {"pr-a": "one", "pr-b": "one", "pr-c": "two"}
    epoch = GoldenEpoch(
        epoch_id="synthetic",
        upstream_sha="",
        scorer_tree="",
        judge_pin="",
        adjudicator_pin="",
        golden_sha256=digest([data, projects]),
        supported_profiles=("core",),
        primary_profile="core",
        bugs=build_bug_index(data),
        scorer_input=data,
        project_by_pr=projects,
        subsets={"holdout": ("pr-c",)},
        shas={},
    )
    policy = dict(
        schema_version=3,
        epoch_id=epoch.epoch_id,
        golden_sha256=epoch.golden_sha256,
        holdout_salt="ab" * 32,
        current_wave="0a",
        entries=[],
        passages=[],
    )
    derived = l.derive_identifiers(
        epoch,
        holdout_pr_urls=frozenset(epoch.subsets["holdout"]),
        holdout_salt=policy["holdout_salt"],
    )
    return epoch, derived, policy


@pytest.mark.parametrize(
    "token, mode, membership",
    [
        ("CrossProjectSymbol", "identifier", "dropped"),
        ("SameOwnerSymbol", "identifier", "plain"),
        ("PublicSymbol", "identifier", "plain"),
        ("SealedSymbol", "identifier", "sealed"),
        ("SealedRoute", "identifier", "sealed"),
        ("WrappedSealedSymbolTail", "identifier", "sealed"),
        ("path/SealedRoute/file", "literal_path", "sealed"),
    ],
)
def test_derive_identifier_membership(controlled, token, mode, membership):
    _, derived, _ = controlled
    assert (
        l.LeakIdentifier(digest([mode, token]), token, mode) in derived.identifiers
    ) == (membership == "plain")
    assert (
        l.HoldoutLeakIdentifier(digest([derived.holdout_salt, mode, token]), mode)
        in derived.holdout_identifiers
    ) == (membership == "sealed")


@pytest.mark.parametrize(
    "field, value, error",
    [
        *(
            ("holdout_salt", value, "salt")
            for value in ("", "a" * 63, "a" * 65, "AB" * 32, "zz" * 32)
        ),
        ("holdout_pr_urls", frozenset(), "membership"),
        ("holdout_pr_urls", frozenset(("pr-c", "unknown")), "membership"),
        ("project_by_pr", {"pr-b": "one", "pr-c": "two"}, "project membership"),
    ],
)
def test_derive_refuses_invalid_inputs(controlled, field, value, error):
    epoch, _, policy = controlled
    kwargs = dict(
        holdout_pr_urls=frozenset(epoch.subsets["holdout"]),
        holdout_salt=policy["holdout_salt"],
    )
    if field == "project_by_pr":
        epoch = replace(epoch, project_by_pr=value)
    else:
        kwargs[field] = value
    refuses(l.derive_identifiers, epoch, **kwargs, match=error)


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
    return (
        epoch,
        derived,
        {**policy, "current_wave": "0a", "entries": [], "passages": []},
    )


def test_repository_scan_and_no_deleted_allowance_policy(monkeypatch):
    calls, producer = [], l.generated_surfaces

    def generated(root):
        calls.append(root)
        return producer(root)

    monkeypatch.setattr(l, "generated_surfaces", generated)
    report = l.scan_repository()
    assert report.passed and not report.stale_allowances
    assert not report.hits and not report.unallowed_hits and calls == [l.ROOT]
    policy = read_json(l.ROOT / "bench/golden/leak_allowlist.json")
    assert policy["current_wave"] == "2a"
    assert policy["entries"] == []
    assert all(p["expected_count"] == 0 for p in policy["passages"])
    assert all("expires_wave" not in e for e in policy["entries"] + policy["passages"])
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
                for k in [
                    "identifier_id",
                    "surface_id",
                    "source_path",
                    "excerpt_sha256",
                ]
            },
            "expected_count": 1,
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
    refuses(l.scan, derived, [surface], expired, match="empty")
    assert l.scan(derived, [], {**expired, "entries": []}).passed


def test_generic_predicate_is_exact_and_does_not_exempt_project_members(lexical):
    _, derived, policy = lexical
    for token in [
        "JavaScript",
        "NullPointerException",
        "if/else",
        "time.sleep",
        "AssertionError",
    ]:
        assert l.ordinary_vocabulary(token)
    for token in ["project.JavaScript", "customAuthContext", "project.time.sleep"]:
        assert not l.ordinary_vocabulary(token)
    assert not any(l.ordinary_vocabulary(i.token) for i in derived.identifiers)


def test_sealed_hits_redact_the_entire_line_and_serialization(controlled, tmp_path):
    _, derived, policy = controlled
    sealed_line, plain_line = "SealedSymbol PublicSymbol", "PublicSymbol"
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents/a.md").write_text(
        sealed_line + "\n" + plain_line, encoding="utf-8"
    )
    surfaces = l.collect_surfaces(tmp_path, ())
    salted = digest([derived.holdout_salt, sealed_line])
    unsalted = sha256(plain_line.encode()).hexdigest()
    policy["entries"] = [
        dict(
            identifier_id=digest(
                [derived.holdout_salt, "identifier", token]
                if sealed
                else ["identifier", token]
            ),
            surface_id="agents/a.md",
            source_path="agents/a.md",
            excerpt_sha256=fingerprint,
            expected_count=1,
        )
        for token, sealed, fingerprint in (
            ("SealedSymbol", True, salted),
            ("PublicSymbol", False, salted),
            ("PublicSymbol", False, unsalted),
        )
    ]
    report = l.scan(derived, surfaces, policy)
    assert not report.passed and not report.stale_allowances
    assert [
        (h.holdout, h.allowed, h.excerpt, h.excerpt_sha256) for h in report.hits
    ] == [
        (True, False, None, salted),
        (False, True, None, salted),
        (False, True, plain_line, unsalted),
    ]
    assert report.unallowed_hits == (report.hits[0],)
    assert b"SealedSymbol" not in canonical(asdict(report))
    path = tmp_path / "output.json"
    path.write_text(sealed_line, encoding="utf-8")
    with pytest.raises(ValueError, match="sealed token") as error:
        l.assert_no_holdout_tokens(derived, [path], root=tmp_path)
    assert "SealedSymbol" not in str(error.value)


@pytest.mark.parametrize(
    "section, field, value, error",
    [
        (None, "schema_version", 2, "epoch/hash"),
        (None, "epoch_id", "wrong", "epoch/hash"),
        (None, "golden_sha256", "0" * 64, "epoch/hash"),
        (None, "holdout_salt", "cd" * 32, "epoch/hash"),
        (None, "current_wave", "unknown", "wave"),
        ("entries", "duplicate", None, "exact allowance"),
        *(
            ("entries", "expected_count", value, "exact allowance")
            for value in (None, "1", 1.5, True, 0, -1)
        ),
        *(
            ("passages", "line_count", value, "passage allowance")
            for value in (None, "1", 1.5, True, 0, -1)
        ),
        *(
            ("passages", "expected_count", value, "passage allowance")
            for value in (None, "1", 1.5, True, -1)
        ),
    ],
)
def test_scan_refuses_invalid_policy(controlled, section, field, value, error):
    _, derived, policy = controlled
    surface = l.PromptSurface("surface", "a.md", "a.md", "static", "PublicSymbol")
    policy["entries"] = [
        dict(
            identifier_id=digest(["identifier", surface.text]),
            surface_id=surface.surface_id,
            source_path=surface.source_path,
            excerpt_sha256=sha256(surface.text.encode()).hexdigest(),
            expected_count=1,
        )
    ]
    policy["passages"] = [
        dict(
            surface_id=surface.surface_id,
            source_path=surface.source_path,
            excerpt_sha256=digest([derived.holdout_salt, surface.text]),
            line_count=1,
            expected_count=1,
        )
    ]
    assert l.scan(derived, [surface], policy).passed
    if field == "duplicate":
        policy["entries"].append(dict(policy["entries"][0]))
    elif section is None:
        policy[field] = value
    else:
        policy[section][0][field] = value
    refuses(l.scan, derived, [surface], policy, match=error)


@pytest.mark.parametrize("wave", ["2a", "2b", "3"])
def test_passages_must_be_removed_after_wave_2a(lexical, wave):
    _, derived, policy = lexical
    surface = l.PromptSurface(
        "generated/legacy/a", "p.js", "p.js", "legacy", "a scenario"
    )
    policy["passages"] = [
        dict(
            surface_id=surface.surface_id,
            source_path=surface.source_path,
            excerpt_sha256=digest([derived.holdout_salt, surface.text]),
            line_count=1,
            expected_count=1,
        )
    ]
    assert l.scan(derived, [surface], policy).passed
    assert not l.scan(derived, [replace(surface, text="")], policy).passed
    policy["current_wave"] = wave
    refuses(l.scan, derived, [surface], policy, match="zero")
    policy["passages"][0]["expected_count"] = 0
    assert not l.scan(derived, [surface], policy).passed
    assert l.scan(derived, [replace(surface, text="")], policy).passed


def test_surface_collection_excludes_symlink_escapes(tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills/a.md").write_text("ordinary agent text", encoding="utf-8")
    (tmp_path / "skills/escape.md").symlink_to(Path(__file__))
    monkeypatch.setattr(l, "_shared_context_surfaces", lambda *args: ())
    monkeypatch.setattr(l, "_workflow_literal_surfaces", lambda *args: ())
    surfaces = l.collect_surfaces(tmp_path, ())
    assert [s.surface_id for s in surfaces] == ["skills/a.md"]
