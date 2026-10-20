"""Tests for bench/runner/score.py.

No network, no keys, no ``uv`` subprocess: the models API, the vendored scorer,
and the adjudicator are all injected or monkeypatched. Covers judge-pin
resolution + idempotency, the pin-mismatch/null refusals, historical metric arithmetic and legacy
artifact handling. Packet-boundary join and context cases live in test_observations.
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from bench.runner import (
    ledger,
    score,
)
from bench.runner.score import (
    compute_metrics,
    resolve_judge_pin,
    score_run,
)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def ev_result(tp, fp, fn, matched):
    """A minimal step3 per-(url,tool) evaluation result."""
    return {
        "skipped": False,
        "true_positives": [{"matched_candidate": t} for t in matched],
        "false_positives": [],
        "false_negatives": [],
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "errors_count": 0,
    }


# --------------------------------------------------------------- judge pin


class ResolveJudgePinTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.baselines = Path(self._tmp.name) / "baselines.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_idempotent_returns_existing_without_api_call(self):
        write_json(self.baselines, {"judge_pin": "claude-opus-4-8-20260101"})

        def boom(*a, **k):
            raise AssertionError("models API must not be called when pin exists")

        with mock.patch.object(score, "_http_get_json", boom):
            pin = resolve_judge_pin(env={}, baselines_path=self.baselines)
        self.assertEqual(pin, "claude-opus-4-8-20260101")

    def test_force_reresolves_and_writes_newest_dated_snapshot(self):
        write_json(self.baselines, {"judge_pin": "claude-opus-4-8-20250101"})
        models = {
            "data": [
                {"id": "claude-opus-4-8-20260101"},
                {"id": "claude-opus-4-8-20260315"},  # newest
                {"id": "claude-opus-4-8"},  # alias, no date -> ignored
                {"id": "claude-opus-4-8[1m]"},  # variant, no date -> ignored
                {"id": "claude-sonnet-5-20260101"},  # wrong family -> ignored
            ]
        }
        with mock.patch.object(score, "_http_get_json", lambda url, headers: models):
            pin = resolve_judge_pin(
                env={"ANTHROPIC_API_KEY": "k"},
                force=True,
                baselines_path=self.baselines,
            )
        self.assertEqual(pin, "claude-opus-4-8-20260315")
        # Persisted back into baselines.json.
        self.assertEqual(
            json.loads(self.baselines.read_text(encoding="utf-8"))["judge_pin"], pin
        )

    def test_no_key_raises(self):
        write_json(self.baselines, {"judge_pin": None})
        # env has no key and BENCH_DIR points at a keyless tmp dir, so the bench/.env
        # fallback finds nothing either -> genuinely keyless, no network call.
        with (
            mock.patch.object(score, "BENCH_DIR", Path(self._tmp.name)),
            self.assertRaises(RuntimeError),
        ):
            resolve_judge_pin(env={}, baselines_path=self.baselines)

    def test_no_dated_snapshot_raises(self):
        write_json(self.baselines, {"judge_pin": None})
        models = {"data": [{"id": "claude-opus-4-8"}, {"id": "claude-opus-4-8[1m]"}]}
        with (
            mock.patch.object(score, "_http_get_json", lambda url, headers: models),
            self.assertRaises(RuntimeError),
        ):
            resolve_judge_pin(
                env={"ANTHROPIC_API_KEY": "k"}, baselines_path=self.baselines
            )

    def test_pick_snapshot_helper_selects_newest(self):
        self.assertEqual(
            score._pick_opus_snapshot(
                {
                    "data": [
                        {"id": "claude-opus-4-8-20260101"},
                        {"id": "claude-opus-4-8-20260901"},
                    ]
                }
            ),
            "claude-opus-4-8-20260901",
        )
        self.assertIsNone(score._pick_opus_snapshot({"data": []}))

    def test_pick_prefers_dated_4_8_over_older_opus(self):
        # A dated 4.8 wins even when an older-family dated Opus is also present.
        self.assertEqual(
            score._pick_opus_snapshot(
                {
                    "data": [
                        {"id": "claude-opus-4-5-20251101"},
                        {"id": "claude-opus-4-8-20260101"},
                        {"id": "claude-opus-4-1-20250805"},
                    ]
                }
            ),
            "claude-opus-4-8-20260101",
        )

    def test_pick_falls_back_to_newest_dated_opus_when_no_4_8(self):
        # No dated 4.8 (the real-world case): fall back to the newest dated Opus, which
        # is the settled claude-opus-4-5-20251101 that baselines.json already ships.
        self.assertEqual(
            score._pick_opus_snapshot(
                {
                    "data": [
                        {"id": "claude-opus-4-1-20250805"},
                        {"id": "claude-opus-4-5-20251101"},
                        {"id": "claude-opus-4-8"},  # alias, undated -> ignored
                        {"id": "claude-opus-4-8[1m]"},  # variant, undated -> ignored
                    ]
                }
            ),
            "claude-opus-4-5-20251101",
        )

    def test_pick_none_when_no_dated_opus(self):
        self.assertIsNone(
            score._pick_opus_snapshot(
                {
                    "data": [
                        {"id": "claude-opus-4-8"},
                        {"id": "claude-sonnet-5-20260101"},
                    ]
                }
            )
        )

    def test_fallback_resolves_and_pins_newest_dated_opus(self):
        # End-to-end resolve with no dated 4.8 available: writes the 4.5 fallback.
        write_json(self.baselines, {"judge_pin": None})
        models = {
            "data": [
                {"id": "claude-opus-4-8"},
                {"id": "claude-opus-4-5-20251101"},
                {"id": "claude-opus-4-1-20250805"},
            ]
        }
        with mock.patch.object(score, "_http_get_json", lambda url, headers: models):
            pin = resolve_judge_pin(
                env={"ANTHROPIC_API_KEY": "k"}, baselines_path=self.baselines
            )
        self.assertEqual(pin, "claude-opus-4-5-20251101")
        self.assertEqual(
            json.loads(self.baselines.read_text(encoding="utf-8"))["judge_pin"], pin
        )


# ---------------------------------------------------------------- metrics


class ComputeMetricsTests(unittest.TestCase):
    def test_two_tp_one_valid_extra_one_noise(self):
        # 4 candidates: m1,m2 matched (2 TP), plus 2 extras (fp=2). One golden
        # unmatched (fn=1). Adjudicator splits the extras 1 valid_extra / 1 noise.
        candidates = {
            "u": {
                "deep-review": [
                    {"text": "[HIGH] m1"},
                    {"text": "[MEDIUM] m2"},
                    {"text": "[LOW] e1"},
                    {"text": "[HIGH] e2"},
                ]
            }
        }
        evaluations = {
            "u": {"deep-review": ev_result(2, 2, 1, ["[HIGH] m1", "[MEDIUM] m2"])}
        }
        buckets = {
            "u": {
                "golden_matched": ["[HIGH] m1", "[MEDIUM] m2"],
                "adjudicator": ["[LOW] e1", "[HIGH] e2"],
            }
        }
        adjudications = [
            {"bucket": "valid_extra", "candidate": "[LOW] e1"},
            {"bucket": "noise", "candidate": "[HIGH] e2"},
        ]
        m = compute_metrics(evaluations, candidates, buckets, adjudications)

        self.assertAlmostEqual(m["golden_recall"], 2 / 3)
        self.assertAlmostEqual(m["precision_strict"], 0.5)
        self.assertAlmostEqual(m["valid_extra_rate"], 0.25)
        self.assertAlmostEqual(m["noise_rate"], 0.25)
        self.assertAlmostEqual(m["f1_strict"], 2 * 0.5 * (2 / 3) / (0.5 + 2 / 3))
        self.assertEqual(
            m["per_bucket"], {"golden_matched": 2, "valid_extra": 1, "noise": 1}
        )
        self.assertEqual(m["total_candidates"], 4)
        self.assertEqual(m["n_prs"], 1)
        # Buckets partition the tool's total candidates.
        self.assertEqual(
            m["per_bucket"]["golden_matched"]
            + m["per_bucket"]["valid_extra"]
            + m["per_bucket"]["noise"],
            m["total_candidates"],
        )

    def test_severity_dist_and_desc_len(self):
        candidates = {
            "u": {
                "deep-review": [
                    {"text": "[HIGH] aaaa"},
                    {"text": "[HIGH] bbbb"},
                    {"text": "no tag"},
                ]
            }
        }
        evaluations = {"u": {"deep-review": ev_result(0, 3, 0, [])}}
        buckets = {
            "u": {
                "golden_matched": [],
                "adjudicator": [c["text"] for c in candidates["u"]["deep-review"]],
            }
        }
        adjudications = [
            {"bucket": "noise", "candidate": c["text"]}
            for c in candidates["u"]["deep-review"]
        ]
        m = compute_metrics(evaluations, candidates, buckets, adjudications)
        self.assertEqual(m["drift"]["severity_dist"], {"HIGH": 2, "UNKNOWN": 1})
        self.assertAlmostEqual(m["drift"]["desc_len_mean"], (11 + 11 + 6) / 3)


# ----------------------------------------------------------- ledger invocation


def test_ledger_invocation_labels_naive_and_skill():
    metrics = {
        "n_prs": 1,
        "golden_recall": 0.0,
        "valid_extra_rate": 0.0,
        "noise_rate": 0.0,
        "precision_strict": 0.0,
        "f1_strict": 0.0,
        "per_bucket": {},
        "per_dimension": {},
        "drift": {},
    }
    costs = {"tokens_total": 0, "cost_usd": 0.0, "per_model": {}}
    # Explicit manifest invocation wins.
    row = score._build_ledger_row(
        "/tmp/r",
        metrics,
        costs,
        {
            "run_id": "r",
            "tier": "smoke",
            "git_sha": "abc",
            "invocation": "naive:single-pass max-turns=40",
            "anchor": "naive",
        },
        "pin",
        "pin",
        "sha",
    )
    assert row["envelope"]["invocation"] == "naive:single-pass max-turns=40"
    # Legacy naive manifest without invocation falls back by anchor.
    row = score._build_ledger_row(
        "/tmp/r",
        metrics,
        costs,
        {"run_id": "r", "tier": "smoke", "git_sha": "abc", "anchor": "naive"},
        "pin",
        "pin",
        "sha",
    )
    assert row["envelope"]["invocation"] == "naive:single-pass"
    assert row["tool"] == "naive-anchor"
    # Skill run keeps the headless label.
    row = score._build_ledger_row(
        "/tmp/r",
        metrics,
        costs,
        {"run_id": "r", "tier": "smoke", "git_sha": "abc", "anchor": None},
        "pin",
        "pin",
        "sha",
    )
    assert row["envelope"]["invocation"] == "headless:/code-gauntlet"


# --------------------------------------------------------------- score_run


class ScoreRunRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_null_pin_refuses(self):
        baselines = self.tmp / "baselines.json"
        write_json(baselines, {"judge_pin": None})
        with self.assertRaises(RuntimeError) as ctx:
            score_run(self.run_dir, env={}, baselines_path=baselines)
        self.assertIn("judge_pin", str(ctx.exception))

    def test_martian_model_mismatch_refuses(self):
        baselines = self.tmp / "baselines.json"
        write_json(baselines, {"judge_pin": "claude-opus-4-8-20260101"})
        with self.assertRaises(RuntimeError) as ctx:
            score_run(
                self.run_dir,
                env={"MARTIAN_MODEL": "claude-opus-4-8-20259999"},
                baselines_path=baselines,
            )
        self.assertIn("MARTIAN_MODEL", str(ctx.exception))


# ----------------------------------------------------- scorer-stage failure surface


# ------------------------------------------------------------- _read_run_costs


class ReadRunCostsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_raw_naive_envelope(self):
        # A naive-anchor PR dir carries only raw-naive.json (never raw.json).
        pr = self.tmp / "run" / "pr-9"
        pr.mkdir(parents=True)
        write_json(
            pr / "raw-naive.json",
            {
                "type": "result",
                "total_cost_usd": 0.7,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
        costs = score._read_run_costs(self.tmp / "run")
        self.assertAlmostEqual(costs["cost_usd"], 0.7)
        self.assertEqual(costs["tokens_total"], 15)

    def test_delegates_to_canonical_envelope_parser(self):
        # score.py must not re-implement the tolerant envelope parser; it delegates to
        # the shared invoke.parse_result_envelope. Spy-wrap it and confirm _read_run_costs
        # routes through it.
        pr = self.tmp / "run" / "pr-3"
        pr.mkdir(parents=True)
        write_json(
            pr / "raw.json",
            {
                "type": "result",
                "total_cost_usd": 0.5,
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )
        real = score.invoke.parse_result_envelope
        calls = []

        def spy(text):
            calls.append(text)
            return real(text)

        with mock.patch.object(score.invoke, "parse_result_envelope", spy):
            costs = score._read_run_costs(self.tmp / "run")
        self.assertTrue(calls)
        self.assertAlmostEqual(costs["cost_usd"], 0.5)


# ------------------------------------------------------------- _assemble_candidates


class AssembleCandidatesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_nested_payload(self):
        # The flat post-review-payload.json is absent; a nested one under output/
        # must still be discovered (mirrors invoke._find_payload's rglob fallback).
        run_dir = self.tmp / "run"
        url = "https://github.com/o/r/pull/12"
        nested = run_dir / "pr-12" / "output"
        nested.mkdir(parents=True)
        write_json(
            nested / "post-review-payload.json",
            {
                "platform": "github",
                "payload": {"comments": [{"body": "c1", "path": "f.py", "line": 3}]},
                "skipped": [],
            },
        )
        candidates, per_pr = score._assemble_candidates(run_dir, [(url, "ok")])
        self.assertIn(url, candidates)
        self.assertEqual(len(candidates[url]["deep-review"]), 1)
        self.assertEqual(per_pr[url]["candidates"][0]["text"], "c1")


class ResolvePrDirTests(unittest.TestCase):
    """FIX 1: pr-dir resolution across the new pr-{owner}-{repo}-{n} name, the legacy
    pr-{n} name, and an authoritative checkpoint payload_path hint."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / "run"
        self.run_dir.mkdir(parents=True)
        # A URL whose pull number (1) collides across repos in the real golden set.
        self.url = "https://github.com/ai-code-review-evaluation/sentry-greptile/pull/1"
        self.new_dir = self.run_dir / "pr-ai-code-review-evaluation-sentry-greptile-1"
        self.legacy = self.run_dir / "pr-1"

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, pr_dir):
        pr_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            pr_dir / "post-review-payload.json",
            {
                "platform": "github",
                "payload": {"comments": [{"body": "c1", "path": "f.py", "line": 3}]},
                "skipped": [],
            },
        )

    def test_resolves_new_owner_repo_key(self):
        self._payload(self.new_dir)
        _c, per_pr = score._assemble_candidates(self.run_dir, [(self.url, "ok", None)])
        self.assertEqual(per_pr[self.url]["pr_dir"], str(self.new_dir))

    def test_legacy_pr_n_rescorable_despite_stale_output_hint(self):
        # Reproduces the committed baseline subset-20260718-031746-27875ca: the payload
        # lives in the legacy pr-{n} dir, but the recorded detail payload_path points at
        # the now-empty shared output dir. Resolution must skip the dead hint and fall
        # through to pr-{n}, keeping that run re-scorable.
        self._payload(self.legacy)
        stale_hint = str(
            self.run_dir / "output" / "post-review-payload.json"
        )  # never written
        cands, per_pr = score._assemble_candidates(
            self.run_dir, [(self.url, "ok", stale_hint)]
        )
        self.assertIn(self.url, cands)
        self.assertEqual(per_pr[self.url]["pr_dir"], str(self.legacy))

    def test_payload_hint_parent_is_authoritative(self):
        self._payload(self.new_dir)
        hint = str(self.new_dir / "post-review-payload.json")
        _c, per_pr = score._assemble_candidates(self.run_dir, [(self.url, "ok", hint)])
        self.assertEqual(per_pr[self.url]["pr_dir"], str(self.new_dir))

    def test_new_name_wins_over_legacy_when_both_exist(self):
        self._payload(self.new_dir)
        self._payload(self.legacy)
        _c, per_pr = score._assemble_candidates(self.run_dir, [(self.url, "ok", None)])
        self.assertEqual(per_pr[self.url]["pr_dir"], str(self.new_dir))


class ToolLabelTests(unittest.TestCase):
    """The ledger row's tool label is derived from the run manifest's anchor field."""

    def test_naive_and_default_labels(self):
        self.assertEqual(score._tool_label({"anchor": "naive"}), "naive-anchor")
        self.assertEqual(score._tool_label({"anchor": None}), "deep-review-v2")
        self.assertEqual(score._tool_label({}), "deep-review-v2")

    def test_explicit_tool_overrides_anchor(self):
        self.assertEqual(
            score._tool_label({"tool": "deep-review-v3", "anchor": "naive"}),
            "deep-review-v3",
        )

    def test_label_read_from_run_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name)

        write_json(run_dir / "run.json", {"run_id": "smoke-x", "anchor": "naive"})
        self.assertEqual(
            score._tool_label(score._read_run_manifest(run_dir)), "naive-anchor"
        )

        write_json(run_dir / "run.json", {"run_id": "smoke-x"})  # no anchor
        self.assertEqual(
            score._tool_label(score._read_run_manifest(run_dir)), "deep-review-v2"
        )


class AuthModeTests(unittest.TestCase):
    """The ledger row records which credential served the review children.

    ``cost_usd``/``tokens_total`` are the envelope's own figures in every mode --
    a subscription run's cost is documented as not relevant for billing, but the
    ledger's job is to LABEL it, not to rewrite it. Zeroing or scaling the figure
    here would silently destroy the only record of what the run consumed.
    """

    METRICS: ClassVar[dict] = {
        "n_prs": 1,
        "golden_recall": 0.0,
        "valid_extra_rate": 0.0,
        "noise_rate": 0.0,
        "precision_strict": 0.0,
        "f1_strict": 0.0,
        "per_bucket": {},
        "per_dimension": {},
        "drift": {},
    }
    COSTS: ClassVar[dict] = {
        "tokens_total": 4321,
        "cost_usd": 27.0,
        "per_model": {},
    }

    def _row(self, manifest):
        return score._build_ledger_row(
            "/tmp/r", self.METRICS, self.COSTS, manifest, "pin", "pin", "sha"
        )

    def test_reads_the_shared_manifest_chain(self):
        # The manifest -> mode chain itself is pinned in test_ledger.py; what matters here
        # is that the row writer reads THAT one, not a copy of it, so a resumed run and
        # its ledger row can never disagree about which credential was spent.
        self.assertIs(score.manifest_auth_mode, ledger.manifest_auth_mode)

    def test_row_carries_manifest_mode(self):
        row = self._row({"run_id": "r", "tier": "smoke", "child_auth": "subscription"})
        self.assertEqual(row["auth_mode"], "subscription")

    def test_row_carries_fingerprint_mode(self):
        row = self._row(
            {
                "run_id": "r",
                "tier": "smoke",
                "env_fingerprint": {"child_auth": "subscription"},
            }
        )
        self.assertEqual(row["auth_mode"], "subscription")

    def test_row_defaults_to_api(self):
        row = self._row({"run_id": "r", "tier": "smoke"})
        self.assertEqual(row["auth_mode"], "api")

    def test_cost_and_tokens_are_recorded_verbatim_in_both_modes(self):
        api = self._row({"run_id": "r", "tier": "smoke", "child_auth": "api"})
        sub = self._row({"run_id": "r", "tier": "smoke", "child_auth": "subscription"})
        self.assertEqual(sub["cost_usd"], self.COSTS["cost_usd"])
        self.assertEqual(sub["tokens_total"], self.COSTS["tokens_total"])
        self.assertEqual(sub["cost_usd"], api["cost_usd"])
        self.assertEqual(sub["tokens_total"], api["tokens_total"])

    def test_mode_read_from_run_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name)

        write_json(
            run_dir / "run.json", {"run_id": "smoke-x", "child_auth": "subscription"}
        )
        row = self._row(score._read_run_manifest(run_dir))
        self.assertEqual(row["auth_mode"], "subscription")

        write_json(run_dir / "run.json", {"run_id": "smoke-x"})  # pre-flag manifest
        row = self._row(score._read_run_manifest(run_dir))
        self.assertEqual(row["auth_mode"], "api")


if __name__ == "__main__":
    unittest.main()
