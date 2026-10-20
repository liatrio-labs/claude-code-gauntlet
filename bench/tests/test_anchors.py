"""Tests for the anchor candidates file and bench/runner/anchors.py.

Offline and keyless: the vendored anchor data is validated as committed; the
``spot_check`` scorer invocation and ``rejudge_anchors`` machinery run entirely
through injected fakes (no ``uv`` subprocess, no network, no keys). The real
plumbing spot-check that makes API spend lives outside the test suite.
"""

import json
import unittest
from pathlib import Path

from bench.runner import anchors, score
from bench.runner.anchors import (
    ANCHOR_TOOLS,
    rejudge_anchors,
    spot_check,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ANCHORS_FILE = REPO_ROOT / "bench" / "golden" / "anchors" / "candidates.json"
SUBSETS_FILE = REPO_ROOT / "bench" / "golden" / "subsets.json"


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------ Step 1: the committed data


class AnchorCandidatesFileTests(unittest.TestCase):
    """Validate bench/golden/anchors/candidates.json as committed (offline)."""

    @classmethod
    def setUpClass(cls):
        cls.anchors = json.loads(ANCHORS_FILE.read_text(encoding="utf-8"))
        subs = json.loads(SUBSETS_FILE.read_text(encoding="utf-8"))
        cls.subsets = subs
        cls.subset_urls = set(subs["gate"]) | set(subs["holdout"]) | set(subs["smoke"])

    def test_file_loads_and_is_nonempty(self):
        self.assertIsInstance(self.anchors, dict)
        self.assertTrue(self.anchors)

    def test_only_three_anchor_tools_present(self):
        seen = set()
        for tools in self.anchors.values():
            seen.update(tools)
        self.assertEqual(seen, set(ANCHOR_TOOLS))

    def test_only_subset_urls_present(self):
        extra = set(self.anchors) - self.subset_urls
        self.assertEqual(extra, set(), "non-subset URLs leaked into the anchor file")

    def test_every_gate_url_present_with_every_anchor_tool(self):
        # Upstream committed candidates for all three anchors on every gate PR;
        # the filtered file must preserve that (recall signal per gate PR).
        for url in self.subsets["gate"]:
            self.assertIn(url, self.anchors, f"gate URL missing: {url}")
            for tool in ANCHOR_TOOLS:
                self.assertIn(
                    tool,
                    self.anchors[url],
                    f"gate URL {url} missing anchor tool {tool}",
                )

    def test_candidate_entries_have_scorer_shape(self):
        # step3's get_candidates reads c["text"]; provenance is source="extracted".
        for tools in self.anchors.values():
            for cands in tools.values():
                self.assertIsInstance(cands, list)
                for c in cands:
                    self.assertIn("text", c)
                    self.assertIsInstance(c["text"], str)
                    self.assertIn("source", c)

    def test_no_duplicate_candidate_text_within_a_tool(self):
        # bucket_join requires unambiguous candidate texts per (url, tool).
        for url, tools in self.anchors.items():
            for tool, cands in tools.items():
                texts = [c["text"] for c in cands]
                self.assertEqual(
                    len(texts),
                    len(set(texts)),
                    f"duplicate candidate text in {url} / {tool}",
                )


# ---------------------------------------------- Step 2: spot_check (mocked)


# ------------------------------------------- Step 2: rejudge_anchors (mocked)


class AdjudicatorContextTests(unittest.TestCase):
    """Anchor comments (null path/line) get the whole capped PR diff as context."""

    def test_capped_diff_truncates_with_marker(self):
        short = "x" * 100
        self.assertEqual(anchors._capped_diff(short), short)
        long = "y" * (anchors._MAX_DIFF_CHARS + 500)
        out = anchors._capped_diff(long)
        self.assertTrue(out.startswith("y" * anchors._MAX_DIFF_CHARS))
        self.assertIn("truncated", out)

    def test_null_path_line_comment_gets_full_diff(self):
        buckets = {"u": {"golden_matched": [], "adjudicator": ["c-extra"]}}
        candidates = {
            "u": {"claude": [{"text": "c-extra", "path": None, "line": None}]}
        }
        diffs = {"u": "diff --git a/f b/f\n@@ -1 +1 @@\n+code"}
        seen = []

        def adj(text, hunk, ctx, pin, key):
            seen.append({"hunk": hunk, "ctx": ctx})
            return {"bucket": "valid_extra", "failed_check": None, "reason": "t"}

        anchors._adjudicate_anchor_bucket(
            buckets, candidates, "claude", "pin", "k", adj, diffs
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["hunk"], diffs["u"])  # whole diff, not a slice
        self.assertEqual(seen[0]["ctx"], "")

    def test_located_candidate_gets_sliced_hunk(self):
        buckets = {"u": {"golden_matched": [], "adjudicator": ["c"]}}
        candidates = {"u": {"claude": [{"text": "c", "path": "f.py", "line": 2}]}}
        diff = (
            "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
            "@@ -1,3 +1,3 @@\n line1\n+line2\n line3\n"
        )
        seen = []

        def adj(text, hunk, ctx, pin, key):
            seen.append(hunk)
            return {"bucket": "noise", "failed_check": 1, "reason": "t"}

        anchors._adjudicate_anchor_bucket(
            buckets, candidates, "claude", "pin", "k", adj, {"u": diff}
        )
        self.assertTrue(seen[0].startswith("@@ "))  # sliced hunk, not whole diff

    def test_duplicate_text_candidates_get_distinct_contexts(self):
        # Two same-body comments at different located path/line must each be
        # adjudicated with their OWN sliced hunk, not collapsed to one context by
        # a text-keyed dict. Mirrors score._adjudicate_bucket's record iteration.
        buckets = {"u": {"golden_matched": [], "adjudicator": ["dup", "dup"]}}
        candidates = {
            "u": {
                "claude": [
                    {"text": "dup", "path": "a.py", "line": 2},
                    {"text": "dup", "path": "b.py", "line": 2},
                ]
            }
        }
        diff = (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1,3 +1,3 @@\n a1\n+a2\n a3\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
            "@@ -1,3 +1,3 @@\n b1\n+b2\n b3\n"
        )
        seen = []

        def adj(text, hunk, ctx, pin, key):
            seen.append(hunk)
            return {"bucket": "noise", "failed_check": 1, "reason": "t"}

        anchors._adjudicate_anchor_bucket(
            buckets, candidates, "claude", "pin", "k", adj, {"u": diff}
        )
        self.assertEqual(len(seen), 2)  # one adjudicator call per candidate record
        self.assertEqual(len(set(seen)), 2, "each duplicate got its own hunk")
        self.assertIn("a2", seen[0])  # a.py record -> a.py hunk
        self.assertIn("b2", seen[1])  # b.py record -> b.py hunk


if __name__ == "__main__":
    unittest.main()


def test_rejudge_cache_is_epoch_corpus_context_and_evaluator_scoped(
    score_case, tmp_path, monkeypatch
):
    from dataclasses import replace

    import pytest

    from bench.runner.epochs import DEFAULT_ROOT, load_epoch, read_json

    epoch = load_epoch(root=DEFAULT_ROOT)
    url = score_case.url
    anchor_file = tmp_path / "anchors.json"
    anchor_file.write_text(
        json.dumps(
            {
                url: {
                    "claude": [
                        {"text": "candidate", "path": None, "line": None},
                        {"text": "extra", "path": None, "line": None},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    subsets_file = tmp_path / "subsets.json"
    subsets_file.write_text(json.dumps({"gate": [url]}), encoding="utf-8")
    calls = []

    def fake(pin, key, base_url, dedup_path, env=None):
        model = Path(dedup_path).parent
        calls.append(model)
        golden = read_json(model.parent / "benchmark_data.json")[url]["golden_comments"]
        ev = {
            "true_positives": [
                {
                    "golden_comment": golden[0]["comment"],
                    "matched_candidate": "candidate",
                    "confidence": 1.0,
                }
            ],
            "false_negatives": [{"golden_comment": g["comment"]} for g in golden[1:]],
            "tp": 1,
            "fp": 1,
            "fn": len(golden) - 1,
            "errors": [],
        }
        write_json(model / "evaluations.json", {url: {"claude": ev}})
        write_json(model / "dedup_groups.json", {})

    def call(epoch_id=epoch.epoch_id, diffs=None, pin="claude-opus-4-5-20251101"):
        return rejudge_anchors(
            pin,
            tools=["claude"],
            epoch_id=epoch_id,
            anchors_path=anchor_file,
            subsets_path=subsets_file,
            cache_dir=tmp_path / "cache",
            run_scorer=fake,
            diffs=diffs,
            adjudicator=lambda *args: {"bucket": "noise"},
        )

    first = call()
    assert not first["cache_hit"] and call()["cache_hit"]
    primary = first["per_tool"]["claude"]
    assert primary["profile"] == "core" and primary["fp"] == 1
    bugs = [b for b in epoch.bugs if b.pr_url == url]
    core = [b for b in bugs if b.category not in ("style", "speculative")]
    assert primary["recall"] == int(bugs[0] in core) / len(core)
    context = read_json(
        next((Path(first["work_dir"]) / "contexts/claude").glob("*.json"))
    )
    assert context["context_status"] == "missing" and context["hunk"] == ""

    assert not call(diffs={url: "changed context"})["cache_hit"]
    assert not call("dfc6cb4-v1")["cache_hit"]
    original = anchors.load_epoch
    with monkeypatch.context() as m:
        m.setattr(
            anchors,
            "load_epoch",
            lambda *args, **kwargs: replace(
                original(*args, **kwargs), golden_sha256="changed"
            ),
        )
        assert not call()["cache_hit"]
    with monkeypatch.context() as m:
        m.setattr(
            anchors,
            "load_epoch",
            lambda *args, **kwargs: replace(
                original(*args, **kwargs), scorer_sha256="changed"
            ),
        )
        assert not call()["cache_hit"]
    with monkeypatch.context() as m:
        m.setattr(anchors, "_MAX_DIFF_CHARS", 10)
        assert not call()["cache_hit"]
    assert len(set(calls)) == 6
    # A changed evaluator cannot reuse an earlier cache even with unchanged corpus.
    evaluator_for = score.evaluator_for
    with monkeypatch.context() as m:
        m.setattr(
            score,
            "evaluator_for",
            lambda *args: replace(
                evaluator_for(*args), adjudicator_prompt_sha256="changed"
            ),
        )
        assert not call()["cache_hit"]
    assert len(calls) == 7
    assert not call(pin="claude-opus-4-5-20251102")["cache_hit"]
    assert len(calls) == 8
    with pytest.raises(ValueError, match="unknown subset"):
        rejudge_anchors(
            "j", epoch_id=epoch.epoch_id, subset="unknown", subsets_path=subsets_file
        )


def test_spot_check_uses_named_epoch_and_private_results(score_case, tmp_path):
    import pytest

    from bench.runner.epochs import DEFAULT_ROOT, load_epoch

    url = score_case.url
    anchor_file = tmp_path / "anchors.json"
    write_json(
        anchor_file, {url: {"claude": [{"text": "c", "path": None, "line": None}]}}
    )
    upstream = tmp_path / "upstream.json"
    ev = {url: {"claude": {"tp": 1, "fp": 0, "fn": 1}}}
    write_json(upstream, ev)
    calls = []

    def fake(model, key, base_url, dedup_path, env=None):
        calls.append(Path(dedup_path).parent)
        write_json(Path(dedup_path).parent / "evaluations.json", ev)

    kwargs = dict(
        epoch_id="dfc6cb4-v1",
        anchors_path=anchor_file,
        upstream_eval_path=upstream,
        results_dir=tmp_path / "work",
        diff_out=tmp_path / "diff.json",
        run_scorer=fake,
        api_key="test",
    )
    result = spot_check(url, **kwargs)
    assert result["pass"] and result["n_golden"] == sum(
        b.pr_url == url for b in load_epoch("dfc6cb4-v1", root=DEFAULT_ROOT).bugs
    )
    assert result["spend_estimate"] == {
        "judge_calls": result["n_golden"],
        "dedup_calls": 0,
    }
    assert Path(result["diff_path"]).is_file()
    write_json(upstream, {url: {"claude": {"tp": 2, "fp": 0, "fn": 1}}})
    assert spot_check(url, **kwargs)["pass"]
    assert len(set(calls)) == 2
    with pytest.raises(ValueError, match="not in anchor"):
        spot_check("unknown", **kwargs)
    write_json(upstream, {url: {"claude": {"tp": 10, "fp": 0, "fn": 1}}})
    assert not spot_check(url, **kwargs)["pass"]
