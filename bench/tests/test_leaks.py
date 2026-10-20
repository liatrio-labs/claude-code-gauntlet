"""Caller-boundary leak diagnostics; sealed fixtures never appear in assertion output."""

import ast
import json
import shutil
import subprocess
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from bench.runner.epochs import DEFAULT_ROOT, canonical, digest, load_epoch, read_json
from bench.runner.leaks import (
    ROOT,
    PromptSurface,
    _spans,
    assert_no_holdout_tokens,
    collect_surfaces,
    derive_identifiers,
    identifiers_wire,
    load_generated,
    main,
    scan,
    validate_identifiers,
)


@pytest.fixture(scope="module")
def corpus():
    epoch = load_epoch(root=DEFAULT_ROOT)
    stored = read_json(DEFAULT_ROOT / "leak_identifiers.json")
    derived = derive_identifiers(
        epoch,
        holdout_pr_urls=frozenset(read_json(DEFAULT_ROOT / "subsets.json")["holdout"]),
        holdout_salt=stored["holdout_salt"],
    )
    return epoch, derived, stored


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    result = subprocess.run(
        ["node", "workflows/test/tools/emit_agent_prompts.mjs"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    path = tmp_path_factory.mktemp("prompts") / "prompts.json"
    path.write_bytes(result.stdout)
    return load_generated(path)


def empty_allowlist(derived):
    return {
        "schema_version": 2,
        "epoch_id": derived.epoch_id,
        "entries": [],
        "passages": [],
    }


def simple_epoch(epoch, comments):
    bugs = tuple(
        replace(epoch.bugs[0], bug_id=f"g:{i}", pr_url=url, comment=text)
        for i, (url, text) in enumerate(comments)
    )
    return replace(epoch, bugs=bugs, golden_sha256=digest(comments))


def public_token(derived):
    return next(
        i.token for i in derived.identifiers if i.token == "organization_context.member"
    )


def surface(text, name="agents/new-agent.md"):
    return PromptSurface(name, name, name, "static", text)


def allowance(derived, hit, count=1):
    return {
        "identifier_id": hit.identifier_id,
        "surface_id": hit.surface_id,
        "excerpt_sha256": hit.excerpt_sha256,
        "expected_count": count,
        "reason": "holdout_delete_only"
        if hit.holdout
        else "explicit generic collision",
        "disposition": "delete" if hit.holdout else "generic_identifier",
        "expires_wave": "2a",
    }


def test_agent_read_text_has_no_unallowed_golden_identifiers(corpus, generated):
    _, derived, stored = corpus
    validate_identifiers(stored, derived)
    allowlist = read_json(DEFAULT_ROOT / "leak_allowlist.json")
    rows = allowlist["entries"] + allowlist["passages"]
    assert len({digest(row) for row in rows}) == len(rows)
    report = scan(derived, collect_surfaces(ROOT, generated), allowlist)
    if not report.passed:
        pytest.fail(
            "unallowed/stale leak allowance or missing generated route; use the redacted CLI report",
            pytrace=False,
        )
    assert {s.scenario for s in generated} == {
        "legacy",
        "derived",
        "bucketed",
        "fallback",
    }
    assert len(report.hits) > 0


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "agents/new-agent.md",
        "skills/new-skill/references/orphan.md",
        "skills/new-skill/templates/new.md",
        ".claude-plugin/plugin.json",
        "workflows/AGENTS.md",
        "workflows/src/new.js",
        "workflows/pipeline.js",
        "scripts/new.py",
        "scripts/gauntlet/new.py",
    ],
)
def test_new_cue_in_every_agent_read_surface_fails(tmp_path, corpus, generated, path):
    _, derived, _ = corpus
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    token = public_token(derived)
    text = f"prompt = {token!r}" if path.endswith(".py") else f"new cue: {token}"
    target.write_text(text, encoding="utf-8")
    surfaces = collect_surfaces(tmp_path, ())
    report = scan(derived, (*generated, *surfaces), empty_allowlist(derived))
    assert not report.passed
    assert any(h.source_path == path and not h.allowed for h in report.unallowed_hits)


def test_generated_prompt_assembled_across_workflow_fragments_is_scanned(
    tmp_path, corpus
):
    _, derived, _ = corpus
    token = public_token(derived)
    for directory in ("workflows/src", "workflows/test/helpers"):
        destination = tmp_path / directory
        destination.mkdir(parents=True)
        for path in (ROOT / directory).glob("*.js"):
            shutil.copyfile(path, destination / path.name)
    helper = "workflows/test/tools/emit_agent_prompts.mjs"
    (tmp_path / helper).parent.mkdir(parents=True)
    shutil.copyfile(ROOT / helper, tmp_path / helper)
    registry = tmp_path / "workflows/src/registry.js"
    midpoint = token.index(".")
    declaration = "const TYPO_NAMING_SWEEP_PROMPT_EXTRA = "
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            declaration,
            declaration
            + json.dumps(token[:midpoint])
            + " + "
            + json.dumps(token[midpoint:])
            + " + ' ' + ",
        ),
        encoding="utf-8",
    )
    allowlist = read_json(DEFAULT_ROOT / "leak_allowlist.json")
    token_id = digest(["identifier", token])
    static = collect_surfaces(tmp_path, ())
    assert not any(
        h.identifier_id == token_id for h in scan(derived, static, allowlist).hits
    )
    result = subprocess.run(
        ["node", helper], cwd=tmp_path, capture_output=True, check=True
    )
    emitted = tmp_path / "prompts.json"
    emitted.write_bytes(result.stdout)
    report = scan(
        derived, collect_surfaces(tmp_path, load_generated(emitted)), allowlist
    )
    assert not report.passed
    assert any(
        h.surface_id.startswith("generated/") and h.identifier_id == token_id
        for h in report.unallowed_hits
    )


def test_exact_allowance_rejects_added_occurrence_changed_excerpt_and_staleness(
    corpus, generated
):
    epoch, original, _ = corpus
    token = public_token(original)
    derived = derive_identifiers(
        simple_epoch(epoch, [("public", token)]),
        holdout_pr_urls=frozenset(),
        holdout_salt=original.holdout_salt,
    )
    cue = surface(token)
    base = scan(derived, (*generated, cue), empty_allowlist(derived))
    hits = [h for h in base.hits if h.surface_id == cue.surface_id]
    allowed = empty_allowlist(derived)
    allowed["entries"] = [allowance(derived, h) for h in hits]
    assert all(h.allowed for h in scan(derived, (cue,), allowed).hits)
    assert scan(
        derived, (replace(cue, text=cue.text + " " + cue.text),), allowed
    ).unallowed_hits
    assert scan(
        derived, (replace(cue, text="changed " + cue.text),), allowed
    ).unallowed_hits
    assert scan(derived, (), allowed).stale_allowances
    assert scan(derived, (cue,), empty_allowlist(derived)).unallowed_hits
    assert not scan(derived, (), empty_allowlist(derived)).stale_allowances
    allowed["epoch_id"] = "different"
    with pytest.raises(ValueError, match="epoch mismatch"):
        scan(derived, (cue,), allowed)


def test_boundaries_case_terminal_symbols_paths_and_generic_words(corpus):
    epoch, _, _ = corpus
    toy = simple_epoch(
        epoch,
        [
            (
                "public",
                "`sample_symbol` `AlphaWidget.valueChain` `src/sample_file.py` SSRF prose ordinary",
            )
        ],
    )
    derived = derive_identifiers(
        toy, holdout_pr_urls=frozenset(), holdout_salt="1" * 64
    )
    tokens = {i.token for i in derived.identifiers}
    assert {
        "sample_symbol",
        "AlphaWidget.valueChain",
        "valueChain",
        "AlphaWidget",
        "src/sample_file.py",
    } <= tokens
    assert "ordinary" not in tokens and "SSRF" not in tokens
    text = "presample_symbolSuffix SAMPLE_SYMBOL x.AlphaWidget.valueChain.tail src/sample_file.py"
    report = scan(derived, (surface(text),), empty_allowlist(derived))
    found = Counter(h.identifier_id for h in report.hits)
    assert digest(["identifier", "sample_symbol"]) not in found
    assert found[digest(["identifier", "valueChain"])] == 1
    assert found[digest(["literal_path", "src/sample_file.py"])] == 1
    assert not scan(
        derived, (surface("alphawidget.valuechain"),), empty_allowlist(derived)
    ).hits


def test_missing_generated_path_and_wrong_producer_fail(corpus, generated, tmp_path):
    _, derived, _ = corpus
    report = scan(derived, generated[1:], empty_allowlist(derived))
    assert generated[0].surface_id in report.generation_coverage
    wrong = replace(generated[0], producer_path="workflows/src/not-present.js")
    with pytest.raises(ValueError, match="producer"):
        collect_surfaces(ROOT, (wrong,))
    with pytest.raises(ValueError, match="duplicate"):
        collect_surfaces(ROOT, (generated[0], generated[0]))


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
        "scripts/credentials.json",
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
    surfaces = collect_surfaces(root, ())
    assert {s.surface_id for s in surfaces} == {"agents/clean.md", "agents/alias.md"}


def test_python_fstring_pieces_with_shared_source_positions_remain_distinct(
    tmp_path, corpus, monkeypatch
):
    epoch, original, _ = corpus
    source = 'prompt = f"FirstSynthetic {value} LastSynthetic"'
    tree = ast.parse(source)
    joined = next(node for node in ast.walk(tree) if isinstance(node, ast.JoinedStr))
    for node in joined.values:
        if isinstance(node, ast.Constant):
            node.lineno, node.col_offset = joined.lineno, joined.col_offset
    path = tmp_path / "scripts/prompt.py"
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr("bench.runner.leaks.ast.parse", lambda text: tree)
        surfaces = collect_surfaces(tmp_path, ())
    assert len(surfaces) == len({s.surface_id for s in surfaces}) == 2
    derived = derive_identifiers(
        simple_epoch(epoch, [("public", "FirstSynthetic LastSynthetic")]),
        holdout_pr_urls=frozenset(),
        holdout_salt=original.holdout_salt,
    )
    assert len(scan(derived, surfaces, empty_allowlist(derived)).unallowed_hits) == 2


def sealed_tokens(epoch, derived):
    ids = {i.identifier_id for i in derived.holdout_identifiers}
    return tuple(
        {
            s.token
            for b in epoch.bugs
            for s in _spans(b.comment)
            if digest([derived.holdout_salt, s.mode, s.token]) in ids
        }
    )


def ensure_redacted(tokens, data):
    if any(token.encode() in data for token in tokens):
        pytest.fail("sealed text escaped redaction", pytrace=False)


def test_all_committed_leak_files_and_changed_outputs_withhold_sealed_tokens(corpus):
    epoch, derived, _ = corpus
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.split(b"\0")
    changed = subprocess.run(
        ["git", "diff", "--name-only", "-z", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    added = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    names = {
        p.decode()
        for p in tracked
        if p and any(word in p.lower() for word in (b"leak", b"receipt", b"reports/"))
    }
    names.update(p.decode() for p in changed + added if p)
    names.update(
        {
            "bench/golden/leak_identifiers.json",
            "bench/golden/leak_allowlist.json",
            "bench/runner/leaks.py",
            "bench/tests/test_leaks.py",
            "workflows/test/tools/emit_agent_prompts.mjs",
        }
    )
    from bench.runner.leaks import _denied, _safe

    files = [
        ROOT / name
        for name in names
        if not _denied(Path(name))
        and _safe(ROOT, ROOT / name)
        and (ROOT / name).is_file()
        and not (name.startswith("bench/golden/") and "leak_" not in name)
    ]
    report = ROOT.parent / "reports/stage4.md"
    if report.is_file():
        files.append(report)
    try:
        assert_no_holdout_tokens(derived, epoch, files)
    except ValueError as error:
        pytest.fail(str(error), pytrace=False)


def test_plaintext_sealed_insertion_fails_with_only_hash_and_location(corpus, tmp_path):
    epoch, derived, _ = corpus
    tokens = sealed_tokens(epoch, derived)
    path = tmp_path / "leak-receipt.json"
    path.write_text(tokens[0], encoding="utf-8")
    try:
        assert_no_holdout_tokens(derived, epoch, [path])
    except ValueError as error:
        ensure_redacted(tokens, str(error).encode())
        if str(path) not in str(error) or ":1" not in str(error):
            pytest.fail("sealed diagnostic lost location", pytrace=False)
    else:
        pytest.fail("plaintext sealed insertion was accepted", pytrace=False)


def test_sealed_derivation_redacts_shared_chains_overlaps_and_errors(corpus):
    epoch, original, _ = corpus
    tokens = sealed_tokens(epoch, original)
    token = next(t for t in tokens if "." not in t and "/" not in t)
    toy = simple_epoch(
        epoch, [("sealed", token), ("public", token + " `SyntheticWidget`")]
    )
    derived = derive_identifiers(
        toy, holdout_pr_urls=frozenset({"sealed"}), holdout_salt=original.holdout_salt
    )
    ensure_redacted((token,), canonical(identifiers_wire(derived)))
    if (
        not derived.holdout_identifiers
        or len(derived.holdout_identifiers[0].bug_ids) != 2
    ):
        pytest.fail("shared sealed token lost source ids", pytrace=False)
    cue = surface(f"wrapper.{token}.tail SyntheticWidget")
    report = scan(derived, (cue,), empty_allowlist(derived))
    ensure_redacted((token,), canonical(asdict(report)))
    if not report.hits or any(h.excerpt is not None for h in report.hits):
        pytest.fail("overlapping public excerpt was not suppressed", pytrace=False)
    allowed = empty_allowlist(derived)
    allowed["entries"] = [allowance(derived, h) for h in report.hits]
    scan(derived, (cue,), allowed)
    entry = next(
        row for row in allowed["entries"] if row["reason"] == "holdout_delete_only"
    )
    entry["disposition"] = "generic_identifier"
    try:
        scan(derived, (cue,), allowed)
    except ValueError as error:
        ensure_redacted((token,), str(error).encode())
    else:
        pytest.fail("sealed rewrite allowance was accepted", pytrace=False)


def test_corpus_omissions_staleness_order_and_source_ids(corpus):
    epoch, derived, stored = corpus
    assert identifiers_wire(derived) == identifiers_wire(
        derive_identifiers(
            replace(epoch, bugs=tuple(reversed(epoch.bugs))),
            holdout_pr_urls=frozenset(
                read_json(DEFAULT_ROOT / "subsets.json")["holdout"]
            ),
            holdout_salt=derived.holdout_salt,
        )
    )
    for key in ("identifiers", "holdout_identifiers"):
        changed = json.loads(json.dumps(stored))
        changed[key].pop()
        with pytest.raises(ValueError, match="altered identifier corpus"):
            validate_identifiers(changed, derived)
    changed = json.loads(json.dumps(stored))
    changed["holdout_identifiers"][0]["bug_ids"] = []
    with pytest.raises(ValueError, match="altered identifier corpus"):
        validate_identifiers(changed, derived)


def test_cli_rederives_and_scans_without_emitting_sealed_text(
    corpus, generated, tmp_path, capsys
):
    epoch, derived, _ = corpus
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
    out = tmp_path / "report.json"
    assert (
        main(
            [
                "--root",
                str(ROOT),
                "--generated-prompts",
                str(prompt_path),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    ensure_redacted(
        sealed_tokens(epoch, derived),
        out.read_bytes() + capsys.readouterr().out.encode(),
    )
    assert main(["--root", str(ROOT)]) == 2
    derived_path = tmp_path / "identifiers.json"
    assert main(["--root", str(ROOT), "--derive", "--out", str(derived_path)]) == 0
    assert read_json(derived_path) == identifiers_wire(derived)
    assert (
        main(
            [
                "--root",
                str(ROOT),
                "--derive",
                "--holdout-salt",
                "0" * 64,
                "--out",
                str(derived_path),
            ]
        )
        == 2
    )


def test_initial_allowlist_can_only_shrink_and_known_passages_need_dispositions(
    corpus, generated
):
    _, derived, _ = corpus
    surfaces = collect_surfaces(ROOT, generated)
    original = read_json(DEFAULT_ROOT / "leak_allowlist.json")
    widened = json.loads(json.dumps(original))
    widened["entries"][0]["expected_count"] += 1
    with pytest.raises(ValueError, match="widened"):
        scan(derived, surfaces, widened)
    widened = json.loads(json.dumps(original))
    widened["entries"][0]["surface_id"] = "agents/future.md"
    with pytest.raises(ValueError, match="widened"):
        scan(derived, surfaces, widened)
    removed = json.loads(json.dumps(original))
    removed["entries"].pop()
    assert not scan(derived, surfaces, removed).passed
    for index in range(len(original["passages"])):
        removed = json.loads(json.dumps(original))
        passage = removed["passages"].pop(index)
        with pytest.raises(ValueError, match="known passage"):
            scan(derived, surfaces, removed)
        # Deleting the passage permits removing its disposition at the same time.
        remaining = tuple(s for s in surfaces if s.surface_id != passage["surface_id"])
        scan(derived, remaining, removed)


def test_sealed_dotted_terminal_case_and_shared_subchains(corpus):
    epoch, original, _ = corpus
    hidden = "SyntheticSealed.memberCase"
    toy = simple_epoch(
        epoch, [("sealed", hidden), ("public", "prefix." + hidden + ".tailCase")]
    )
    derived = derive_identifiers(
        toy, holdout_pr_urls=frozenset({"sealed"}), holdout_salt=original.holdout_salt
    )
    for text in ("outer." + hidden + ".tailCase", "memberCase", "SyntheticSealed"):
        report = scan(derived, (surface(text),), empty_allowlist(derived))
        assert any(h.holdout and h.excerpt is None for h in report.hits)
    assert not scan(derived, (surface(hidden.lower()),), empty_allowlist(derived)).hits
    ensure_redacted(
        (hidden, "memberCase", "SyntheticSealed"), canonical(identifiers_wire(derived))
    )
