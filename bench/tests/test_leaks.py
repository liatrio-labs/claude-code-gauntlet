"""Exercise the lexical guard at its collection, dispatch, policy and CLI boundaries."""

import json
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from bench.runner import leaks
from bench.runner.epochs import DEFAULT_ROOT, canonical, digest, load_epoch, read_json


@pytest.fixture(scope="module")
def corpus():
    epoch = load_epoch(root=DEFAULT_ROOT)
    ceiling = leaks.load_ceiling()
    derived = leaks.derive_identifiers(
        epoch,
        holdout_pr_urls=frozenset(read_json(DEFAULT_ROOT / "subsets.json")["holdout"]),
        holdout_salt=ceiling["holdout_salt"],
    )
    return epoch, derived, ceiling


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    result = subprocess.run(
        ["node", "workflows/test/tools/emit_agent_prompts.mjs"],
        cwd=leaks.ROOT,
        capture_output=True,
        check=True,
    )
    path = tmp_path_factory.mktemp("prompts") / "prompts.json"
    path.write_bytes(result.stdout)
    return leaks.load_generated(path)


def empty_allowlist(derived):
    return {
        "schema_version": 2,
        "epoch_id": derived.epoch_id,
        "entries": [],
        "passages": [],
    }


def ceiling_for(derived, allowlist):
    return {
        "schema_version": 3,
        "epoch_id": derived.epoch_id,
        "golden_sha256": derived.golden_sha256,
        "holdout_salt": derived.holdout_salt,
        "entries": [dict(row) for row in allowlist["entries"]],
        "passages": [
            dict(row, identifier_id="passage:" + row["passage_id"])
            for row in allowlist["passages"]
        ],
    }


def toy_identifiers(corpus, comments, sealed=frozenset()):
    epoch, original, _ = corpus
    bugs = tuple(
        replace(epoch.bugs[0], bug_id=f"g:{i}", pr_url=url, comment=text)
        for i, (url, text) in enumerate(comments)
    )
    return leaks.derive_identifiers(
        replace(epoch, bugs=bugs, golden_sha256=digest(comments)),
        holdout_pr_urls=sealed,
        holdout_salt=original.holdout_salt,
    )


def surface(text, name="agents/new-agent.md"):
    return leaks.PromptSurface(name, name, name, "static", text)


def allowance(hit, count=1):
    return {
        "identifier_id": hit.identifier_id,
        "surface_id": hit.surface_id,
        "source_path": hit.source_path,
        "excerpt_sha256": hit.excerpt_sha256,
        "expected_count": count,
        "reason": "holdout_delete_only"
        if hit.holdout
        else "explicit generic collision",
        "disposition": "delete" if hit.holdout else "generic_identifier",
        "expires_wave": "2a",
    }


def public_token(derived):
    return next(
        i.token
        for i in derived.identifiers
        if i.match_mode == "identifier" and "." not in i.token
    )


def ensure_redacted(tokens, data):
    if any(token.encode() in data for token in tokens):
        pytest.fail("sealed text escaped redaction", pytrace=False)


def test_agent_read_text_has_no_unallowed_golden_identifiers(corpus, generated):
    _, derived, _ = corpus
    allowlist = read_json(DEFAULT_ROOT / "leak_allowlist.json")
    report = leaks.scan(
        derived, leaks.collect_surfaces(leaks.ROOT, generated), allowlist
    )
    if not report.passed:
        pytest.fail(
            "unallowed/stale leak allowance or missing generated route; use the redacted CLI report",
            pytrace=False,
        )
    assert not any(h.holdout for h in report.hits)
    for prefix in ("generated", "generated-bundle"):
        selected = [s for s in generated if s.surface_id.startswith(prefix + "/")]
        assert {s.scenario for s in selected} == {
            "legacy",
            "derived",
            "bucketed",
            "fallback",
        }
        prompts = {
            s.surface_id.removesuffix("/prompt")
            for s in selected
            if s.surface_id.endswith("/prompt")
        }
        schemas = {
            s.surface_id.removesuffix("/schema")
            for s in selected
            if s.surface_id.endswith("/schema")
        }
        assert prompts == schemas
        assert len(prompts) > 0


@pytest.mark.parametrize(
    "path",
    [
        "agents/new-agent.md",
        "skills/new/SKILL.md",
        "skills/new/references/orphan.md",
        "skills/new/templates/new.md",
    ],
)
def test_new_cue_in_every_agent_markdown_surface_fails(
    tmp_path, corpus, generated, path
):
    _, derived, _ = corpus
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text("new cue: " + public_token(derived), encoding="utf-8")
    report = leaks.scan(
        derived,
        (*generated, *leaks.collect_surfaces(tmp_path, ())),
        read_json(DEFAULT_ROOT / "leak_allowlist.json"),
    )
    assert any(h.source_path == path and not h.allowed for h in report.unallowed_hits)


def test_maintainer_files_metadata_and_code_are_not_agent_read(tmp_path, corpus):
    _, derived, _ = corpus
    for name in (
        "AGENTS.md",
        "agents/AGENTS.md",
        "agents/nested/helper.md",
        "workflows/AGENTS.md",
        "workflows/src/new.js",
        "workflows/pipeline.js",
        "scripts/new.py",
        ".claude-plugin/plugin.json",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(public_token(derived), encoding="utf-8")
    assert leaks.collect_surfaces(tmp_path, ()) == ()


def copy_workflow(root):
    for directory in (
        "workflows/src",
        "workflows/test/helpers",
        "workflows/test/tools",
    ):
        target = root / directory
        target.mkdir(parents=True)
        for path in (leaks.ROOT / directory).glob("*.*js"):
            shutil.copyfile(path, target / path.name)
    shutil.copyfile(
        leaks.ROOT / "workflows/pipeline.js", root / "workflows/pipeline.js"
    )


@pytest.mark.parametrize("implementation", ["source", "bundle"])
@pytest.mark.parametrize("kind", ["prompt", "schema"])
def test_assembled_dispatch_prompt_and_schema_are_scanned(
    tmp_path, corpus, implementation, kind
):
    _, derived, _ = corpus
    token = public_token(derived)
    copy_workflow(tmp_path)
    path = tmp_path / (
        "workflows/src/registry.js"
        if implementation == "source"
        else "workflows/pipeline.js"
    )
    fragment = json.dumps(token[:3]) + " + " + json.dumps(token[3:])
    text = path.read_text(encoding="utf-8")
    if kind == "prompt":
        marker = "const TYPO_NAMING_SWEEP_PROMPT_EXTRA = "
        text = text.replace(marker, marker + fragment + " + ' ' + ")
    else:
        text = text.replace(
            "id: 'string', file:",
            "id: { type: 'string', description: " + fragment + " }, file:",
        )
    path.write_text(text, encoding="utf-8")
    assert leaks.collect_surfaces(tmp_path, ()) == ()
    result = subprocess.run(
        ["node", "workflows/test/tools/emit_agent_prompts.mjs"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    output = tmp_path / "prompts.json"
    output.write_bytes(result.stdout)
    report = leaks.scan(
        derived,
        leaks.collect_surfaces(tmp_path, leaks.load_generated(output)),
        read_json(DEFAULT_ROOT / "leak_allowlist.json"),
    )
    prefix = "generated/" if implementation == "source" else "generated-bundle/"
    assert any(
        h.identifier_id == digest(["identifier", token])
        and h.surface_id.startswith(prefix)
        and h.surface_id.endswith("/" + kind)
        for h in report.unallowed_hits
    )


def test_shared_context_scans_emitted_template_and_keeps_stable_id(tmp_path, corpus):
    _, derived, _ = corpus
    for path in (leaks.ROOT / "scripts").rglob("*.py"):
        if not leaks._safe(leaks.ROOT, path):
            continue
        target = tmp_path / path.relative_to(leaks.ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    path = tmp_path / "scripts/gauntlet/shared_context.py"
    original = leaks.collect_surfaces(tmp_path, ())
    text = path.read_text(encoding="utf-8").replace(
        'b"\\n## Risk classification',
        'b"\\n' + public_token(derived) + " ## Risk classification",
    )
    path.write_text("# Unrelated source line.\n" + text, encoding="utf-8")
    changed = leaks.collect_surfaces(tmp_path, ())
    assert [s.surface_id for s in original] == [s.surface_id for s in changed]
    report = leaks.scan(derived, changed, empty_allowlist(derived))
    assert any(
        h.source_path == "scripts/gauntlet/shared_context.py"
        for h in report.unallowed_hits
    )


def test_runtime_names_and_symbols_shared_by_different_prs_are_excluded(corpus):
    names = leaks.runtime_identifiers()
    assert {
        "indexOf",
        "TypeError",
        "Object.prototype.hasOwnProperty",
        "__import__",
        "pathlib",
    } <= names
    derived = toy_identifiers(corpus, [("same-pr", " ".join(sorted(names)))])
    assert not {i.token for i in derived.identifiers} & names
    derived = toy_identifiers(
        corpus,
        [
            ("pr-a", "RepeatedSynthetic SamePrSynthetic"),
            ("pr-a", "SamePrSynthetic"),
            ("pr-b", "RepeatedSynthetic"),
        ],
        frozenset({"pr-b"}),
    )
    assert {i.token for i in derived.identifiers} == {"SamePrSynthetic"}
    assert len(derived.identifiers[0].bug_ids) == 2
    assert derived.holdout_identifiers == ()


def test_boundaries_case_subchains_paths_and_generic_words(corpus):
    derived = toy_identifiers(
        corpus,
        [
            (
                "public",
                "sample_symbol AlphaWidget.valueChain src/sample_file.py SSRF prose ordinary",
            )
        ],
    )
    allowed = empty_allowlist(derived)
    ceiling = ceiling_for(derived, allowed)
    tokens = {i.token for i in derived.identifiers}
    assert {
        "sample_symbol",
        "AlphaWidget.valueChain",
        "valueChain",
        "AlphaWidget",
        "src/sample_file.py",
    } <= tokens
    assert not {"ordinary", "SSRF"} & tokens
    text = "presample_symbolSuffix SAMPLE_SYMBOL x.AlphaWidget.valueChain.tail src/sample_file.py"
    report = leaks._scan_with_ceiling(
        derived, (surface(text),), allowed, ceiling=ceiling
    )
    found = Counter(h.identifier_id for h in report.hits)
    assert digest(["identifier", "sample_symbol"]) not in found
    assert found[digest(["identifier", "valueChain"])] == 1
    assert found[digest(["literal_path", "src/sample_file.py"])] == 1
    assert not leaks._scan_with_ceiling(
        derived, (surface("alphawidget.valuechain"),), allowed, ceiling=ceiling
    ).hits


def test_exact_allowance_counts_excerpts_and_source_paths(corpus):
    derived = toy_identifiers(corpus, [("public", "UniqueSyntheticCue")])
    cue = surface("UniqueSyntheticCue")
    allowed = empty_allowlist(derived)
    hit = leaks._scan_with_ceiling(
        derived, (cue,), allowed, ceiling=ceiling_for(derived, allowed)
    ).hits[0]
    allowed["entries"] = [allowance(hit)]
    ceiling = ceiling_for(derived, allowed)
    assert all(
        h.allowed
        for h in leaks._scan_with_ceiling(
            derived, (cue,), allowed, ceiling=ceiling
        ).hits
    )
    for changed in (
        replace(cue, text=cue.text + " " + cue.text),
        replace(cue, text="changed " + cue.text),
        replace(cue, source_path="agents/other.md"),
    ):
        assert leaks._scan_with_ceiling(
            derived, (changed,), allowed, ceiling=ceiling
        ).unallowed_hits
    assert leaks._scan_with_ceiling(
        derived, (), allowed, ceiling=ceiling
    ).stale_allowances


def test_ceiling_accepts_lower_counts_and_removal_but_rejects_restoration(corpus):
    derived = toy_identifiers(corpus, [("public", "UniqueSyntheticCue")])
    cue = surface("UniqueSyntheticCue\nUniqueSyntheticCue")
    empty = empty_allowlist(derived)
    hit = leaks._scan_with_ceiling(
        derived, (cue,), empty, ceiling=ceiling_for(derived, empty)
    ).hits[0]
    original = empty_allowlist(derived)
    original["entries"] = [allowance(hit, 2)]
    ceiling = ceiling_for(derived, original)
    lower = json.loads(json.dumps(original))
    lower["entries"][0]["expected_count"] = 1
    assert all(
        h.allowed
        for h in leaks._scan_with_ceiling(
            derived, (replace(cue, text="UniqueSyntheticCue"),), lower, ceiling=ceiling
        ).hits
    )
    leaks._check_ceiling(derived, (), empty, ceiling, [lower])
    for previous in (lower, empty):
        with pytest.raises(ValueError, match="restored or widened"):
            leaks._check_ceiling(derived, (cue,), original, ceiling, [previous])
    for key, value in (
        ("expected_count", 3),
        ("reason", "new reason"),
        ("source_path", "agents/other.md"),
        ("disposition", "delete"),
        ("identifier_id", "0" * 64),
        ("excerpt_sha256", "1" * 64),
    ):
        changed = json.loads(json.dumps(original))
        changed["entries"][0][key] = value
        with pytest.raises(ValueError, match="widened"):
            leaks._scan_with_ceiling(derived, (cue,), changed, ceiling=ceiling)


def test_epoch_and_golden_changes_require_reissued_pinned_ceiling(
    corpus, generated, monkeypatch
):
    _, derived, ceiling = corpus
    allowed = empty_allowlist(derived)
    for changed in (
        replace(derived, epoch_id="next-epoch"),
        replace(derived, golden_sha256="0" * 64),
    ):
        matching = dict(allowed, epoch_id=changed.epoch_id)
        with pytest.raises(ValueError, match="re-issue"):
            leaks.scan(changed, generated, matching)
        reissued = dict(
            ceiling, epoch_id=changed.epoch_id, golden_sha256=changed.golden_sha256
        )
        leaks._scan_with_ceiling(
            changed, generated, matching, ceiling=dict(reissued, passages=[])
        )
    monkeypatch.setattr(leaks, "ALLOWANCE_CEILING_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="re-issue"):
        leaks.load_ceiling()


def test_committed_history_preserves_retirement_across_ceiling_repin(
    corpus, monkeypatch
):
    _, derived, ceiling = corpus
    removed = empty_allowlist(derived)
    restored = read_json(DEFAULT_ROOT / "leak_allowlist.json")
    older_ceiling = dict(ceiling, entries=[])
    snapshots = {
        "restore": (restored, ceiling),
        "remove": (removed, older_ceiling),
        "issued": (restored, ceiling),
        "migration": (restored, dict(ceiling, schema_version=2)),
    }
    paths = [
        "bench/golden/leak_allowlist.json",
        "bench/golden/leak_allowlist_ceiling.json",
    ]

    def git(args, **kwargs):
        if args[1] == "log":
            return subprocess.CompletedProcess(
                args, 0, "restore\nremove\nissued\nmigration\n"
            )
        revision, path = args[2].split(":", 1)
        return subprocess.CompletedProcess(
            args, 0, json.dumps(snapshots[revision][paths.index(path)])
        )

    monkeypatch.setattr(leaks.subprocess, "run", git)
    history = leaks._committed_allowlists(leaks.ROOT, ceiling)
    assert len(history) == 3
    with pytest.raises(ValueError, match="restored"):
        leaks._check_ceiling(derived, (), restored, ceiling, history)
    snapshots["migration"] = (restored, ceiling)
    with pytest.raises(ValueError, match="incomplete allowance history"):
        leaks._committed_allowlists(leaks.ROOT, ceiling)


def test_known_passages_require_dispositions_until_deleted(corpus, generated):
    _, derived, _ = corpus
    surfaces = leaks.collect_surfaces(leaks.ROOT, generated)
    original = read_json(DEFAULT_ROOT / "leak_allowlist.json")
    for index in range(len(original["passages"])):
        removed = json.loads(json.dumps(original))
        passage = removed["passages"].pop(index)
        with pytest.raises(ValueError, match="known passage"):
            leaks.scan(derived, surfaces, removed)
        remaining = tuple(s for s in surfaces if s.surface_id != passage["surface_id"])
        leaks.scan(derived, remaining, removed)


def test_missing_generated_prompt_schema_bundle_and_context_fail(corpus, generated):
    _, derived, _ = corpus
    surfaces = leaks.collect_surfaces(leaks.ROOT, generated)
    for removed in (
        next(s for s in surfaces if s.surface_id.endswith("/schema")),
        next(
            s
            for s in surfaces
            if s.surface_id.startswith("generated-bundle/")
            and s.surface_id.endswith("/prompt")
        ),
        next(s for s in surfaces if s.scenario == "shared-context"),
    ):
        report = leaks._scan_with_ceiling(
            derived,
            tuple(s for s in surfaces if s != removed),
            empty_allowlist(derived),
            ceiling=ceiling_for(derived, empty_allowlist(derived)),
        )
        assert removed.surface_id in report.generation_coverage
    with pytest.raises(ValueError, match="producer"):
        leaks.collect_surfaces(
            leaks.ROOT,
            (replace(generated[0], producer_path="workflows/src/not-present.js"),),
        )
    with pytest.raises(ValueError, match="duplicate"):
        leaks.collect_surfaces(leaks.ROOT, (generated[0], generated[0]))


def test_deny_list_and_symlink_confinement_never_read_denied_files(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    blocked = [
        ".env",
        "bench/.env",
        "auth.json",
        "pairing/private.md",
        "backups/data.gpg",
        "skills/x/.env.md",
        "skills/x/auth.json",
        "skills/pairing/x.md",
        "skills/x/credentials.json",
    ]
    for relative in blocked:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("forbidden synthetic fixture", encoding="utf-8")
    safe = root / "agents/clean.md"
    safe.parent.mkdir()
    safe.write_text("ordinary prose", encoding="utf-8")
    outside = tmp_path / "external.md"
    outside.write_text("outside synthetic fixture", encoding="utf-8")
    (safe.parent / "outside.md").symlink_to(outside)
    (safe.parent / "secret.md").symlink_to(root / ".env")
    (safe.parent / "alias.md").symlink_to(safe)
    read = Path.read_text

    def guarded(path, *args, **kwargs):
        if path.resolve() in {outside, *(root / p for p in blocked)}:
            pytest.fail("collector read a denied path", pytrace=False)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    assert {s.surface_id for s in leaks.collect_surfaces(root, ())} == {
        "agents/clean.md",
        "agents/alias.md",
    }


def test_committed_mechanism_outputs_withhold_sealed_tokens(corpus):
    epoch, derived, _ = corpus
    leaks.assert_no_holdout_tokens(derived, epoch, leaks.mechanism_outputs(leaks.ROOT))


def test_output_inventory_only_reads_mechanism_files_and_explicit_targets(
    tmp_path, monkeypatch, corpus
):
    epoch, derived, _ = corpus
    names = [
        "bench/golden/leak_allowlist.json",
        "bench/capture.json",
        "workflows/src/ordinary.js",
    ]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic output", encoding="utf-8")

    def git(args, **kwargs):
        assert args == ["git", "ls-files", "-z", "--", "**/leak_*"]
        return subprocess.CompletedProcess(args, 0, names[0].encode() + b"\0")

    monkeypatch.setattr(leaks.subprocess, "run", git)
    files = leaks.mechanism_outputs(tmp_path, [Path(names[1])])
    assert set(files) == {tmp_path / name for name in names[:2]}
    leaks.assert_no_holdout_tokens(derived, epoch, files, root=tmp_path)
    outside = tmp_path.parent / "outside.json"
    (tmp_path / "escape.json").symlink_to(outside)
    for target in (
        outside,
        tmp_path / "escape.json",
        tmp_path / "../outside.json",
        tmp_path / ".env",
    ):
        with pytest.raises(ValueError, match="unconfined"):
            leaks.mechanism_outputs(tmp_path, [target])
        with pytest.raises(ValueError, match="unconfined"):
            leaks.assert_no_holdout_tokens(derived, epoch, [target], root=tmp_path)


def test_holdout_output_check_uses_spans_and_redacts_location(corpus, tmp_path):
    epoch, _, _ = corpus
    sealed_chain = "SyntheticSealed.memberCase"
    derived = toy_identifiers(corpus, [("sealed", sealed_chain)], frozenset({"sealed"}))
    path = tmp_path / "receipt.json"
    path.write_text("SyntheticSealedSuffix memberCaseSuffix", encoding="utf-8")
    leaks.assert_no_holdout_tokens(derived, epoch, [path], root=tmp_path)
    path.write_text(sealed_chain, encoding="utf-8")
    with pytest.raises(ValueError) as captured:
        leaks.assert_no_holdout_tokens(derived, epoch, [path], root=tmp_path)
    ensure_redacted(
        (sealed_chain, "SyntheticSealed", "memberCase"), str(captured.value).encode()
    )
    assert "receipt.json:1" in str(captured.value)


def test_sealed_subchains_and_overlapping_public_excerpts_are_redacted(corpus):
    sealed_chain = "SyntheticSealed.memberCase"
    derived = toy_identifiers(
        corpus,
        [("sealed", sealed_chain), ("public", "SyntheticWidget")],
        frozenset({"sealed"}),
    )
    empty = empty_allowlist(derived)
    ceiling = ceiling_for(derived, empty)
    for text in (
        "outer." + sealed_chain + ".tailCase SyntheticWidget",
        "memberCase SyntheticWidget",
        "SyntheticSealed SyntheticWidget",
    ):
        report = leaks._scan_with_ceiling(
            derived, (surface(text),), empty, ceiling=ceiling
        )
        assert any(h.holdout and h.excerpt is None for h in report.hits)
        assert all(h.excerpt is None for h in report.hits)
        ensure_redacted(
            (sealed_chain, "memberCase", "SyntheticSealed"), canonical(asdict(report))
        )
    assert not leaks._scan_with_ceiling(
        derived, (surface(sealed_chain.lower()),), empty, ceiling=ceiling
    ).hits
    report = leaks._scan_with_ceiling(
        derived, (surface(sealed_chain),), empty, ceiling=ceiling
    )
    allowed = dict(empty, entries=[allowance(h) for h in report.hits])
    allowed["entries"][0]["disposition"] = "generic_identifier"
    with pytest.raises(ValueError, match="delete-only"):
        leaks._scan_with_ceiling(
            derived,
            (surface(sealed_chain),),
            allowed,
            ceiling=ceiling_for(derived, allowed),
        )


def test_derivation_is_in_memory_deterministic_and_order_independent(corpus):
    epoch, derived, _ = corpus
    reversed_epoch = replace(epoch, bugs=tuple(reversed(epoch.bugs)))
    assert leaks.identifiers_wire(derived) == leaks.identifiers_wire(
        leaks.derive_identifiers(
            reversed_epoch,
            holdout_pr_urls=frozenset(
                read_json(DEFAULT_ROOT / "subsets.json")["holdout"]
            ),
            holdout_salt=derived.holdout_salt,
        )
    )
    assert not (DEFAULT_ROOT / "leak_identifiers.json").exists()


def test_cli_rederives_scans_and_confines_explicit_output(
    corpus, generated, tmp_path, capsys
):
    prompt_path = tmp_path / "prompts.json"
    prompt_path.write_bytes(
        canonical(
            [
                {
                    k: getattr(s, k)
                    for k in ("surface_id", "producer_path", "scenario", "text")
                }
                for s in generated
            ]
        )
    )
    args = [
        "--root",
        str(leaks.ROOT),
        "--generated-prompts",
        str(prompt_path),
        "--allowlist",
        "bench/golden/leak_allowlist.json",
    ]
    with tempfile.NamedTemporaryFile(
        dir=leaks.ROOT / "bench", suffix=".json"
    ) as output:
        assert leaks.main([*args, "--out", output.name]) == 0
        assert read_json(Path(output.name))["passed"]
    assert "LEAK_GUARD: PASS" in capsys.readouterr().out
    outside = tmp_path / "report.json"
    assert leaks.main([*args, "--out", str(outside)]) == 2
    assert not outside.exists()
