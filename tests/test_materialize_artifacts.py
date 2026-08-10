"""
Tests for scripts/materialize_artifacts.py.

The script is the disk-side half of the RETURN persist channel: the pipeline
hands its primaries back inside the workflow's own return value — which the
HARNESS serializes to ``tasks/<task-id>.output``, with no model retyping it —
and this script writes them and derives the projections from what landed.

Every input here is REAL pipeline output. ``workflows/test/tools/emit_task_output.mjs``
runs the wired pipeline (``runWith``) on the return channel and writes the
Workflow tool's own envelope shape, so a stage that stopped carrying a field, or
a writeArtifacts that stopped carrying the primaries home, fails here rather
than passing against a hand-authored payload.

Contract under test:
  * the three primaries land BYTE-EXACT, including the escape runs, astral-plane
    characters and long prose that the artifact-writer measurably loses;
  * the two projections are derived from them, so post-review.json exists and PR
    comments are postable;
  * exactly one line of JSON on stdout on EVERY path, including no-source-found
    and an unexpected internal error;
  * a structural failure writes NOTHING and exits 1; a content-proof failure
    still names what landed, because the artifacts are deliverable and the
    divergence is a gap to disclose, not a run to throw away;
  * an entry may not write outside --output-dir;
  * a nonce, when given, must match — one review's findings must never be
    delivered under another's name.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from scripts.assemble_artifacts import plan_checksum  # noqa: E402
from scripts.materialize_artifacts import main, materialize  # noqa: E402

SCRIPT = os.path.join(REPO_ROOT, "scripts", "materialize_artifacts.py")
RECORDER = os.path.join(REPO_ROOT, "workflows", "test", "tools", "emit_task_output.mjs")
NONCE = "nonce-materialize-test"


def record_task_output(tmp, nonce=NONCE):
    """Run the wired pipeline on the return channel; return (task_path, out_dir)."""
    out_dir = os.path.join(tmp, ".code-gauntlet")
    os.makedirs(out_dir, exist_ok=True)
    task_path = os.path.join(tmp, "tasks", "task-abc123.output")
    os.makedirs(os.path.dirname(task_path), exist_ok=True)
    proc = subprocess.run(
        ["node", RECORDER, task_path, out_dir, nonce],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"recorder failed: {proc.stderr}")
    return task_path, out_dir


def payload_of(task_path):
    with open(task_path, encoding="utf-8") as fh:
        return json.load(fh)["result"]["persistReturn"]


def rewrite_payload(task_path, mutate):
    """Apply *mutate* to the envelope's persistReturn and write it back."""
    with open(task_path, encoding="utf-8") as fh:
        envelope = json.load(fh)
    mutate(envelope["result"]["persistReturn"])
    with open(task_path, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh, indent=2)


def run_cli(args, environ=None):
    """Run the script as the skill runs it. Returns (exit_code, receipt, stdout)."""
    env = dict(os.environ)
    env.update(environ or {})
    proc = subprocess.run(
        [sys.executable, SCRIPT, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line, got {proc.stdout!r}"
    return proc.returncode, json.loads(lines[0]), proc.stdout


class MaterializeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.task, self.out_dir = record_task_output(self.tmp)

    def artifact(self, name):
        return os.path.join(
            self.out_dir,
            f"code-gauntlet-{name}-abc1234.{'md' if name == 'report' else 'json'}",
        )

    def read(self, path):
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()


class TestHappyPath(MaterializeTestCase):
    def test_every_primary_lands_byte_exact(self):
        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 0, receipt)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["gaps"], [])
        for entry in payload_of(self.task)["entries"]:
            self.assertEqual(
                self.read(entry["path"]),
                entry["text"],
                f"{entry['path']} is not byte-identical to what the workflow returned",
            )

    def test_the_bytes_a_transcriber_loses_survive(self):
        # The measured failure modes, asserted on the content rather than on the
        # mechanism: a literal backslash ahead of a quote (18 of 18 collapsed on the
        # run that lost every artifact), astral-plane characters, and prose long
        # enough that summarizing it would pass every schema check.
        run_cli(["--output-dir", self.out_dir, "--task", self.task])
        findings = json.loads(self.read(self.artifact("findings")))
        description = findings[0]["description"]
        self.assertIn('\\"receipt\\"', description)
        self.assertIn("C:\\tmp\\out", description)
        self.assertIn("😀", description)
        self.assertIn("\U0001d54f", description)
        self.assertGreater(len(description), 1000, "long prose was not shortened")
        self.assertEqual(findings[0]["body"], description, "the v2 alias too")

    def test_the_projections_are_derived_so_pr_comments_are_postable(self):
        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 0)
        plan = json.loads(payload_of(self.task)["entries"][2]["text"])
        post_review = json.loads(self.read(self.artifact("post-review")))
        checkpoint = json.loads(self.read(self.artifact("checkpoint-all")))
        # The delivery set is the pipeline's own ranked, capped selection — asserted
        # against the plan's id list rather than a hand-written order, which is the
        # thing the derivation must reproduce and must not re-rank.
        self.assertEqual([f["id"] for f in post_review], plan["postReview"]["ids"])
        self.assertEqual(sorted(f["id"] for f in post_review), ["F1", "F2"])
        self.assertEqual(
            [f["id"] for f in checkpoint["phases"]["challenge"]["findings"]],
            plan["checkpoint"]["challengeFindingIds"],
        )
        self.assertEqual(
            sorted(e["path"] for e in receipt["assemble"]["written"]),
            sorted([self.artifact("post-review"), self.artifact("checkpoint-all")]),
        )

    def test_the_content_proof_is_the_assemblers_own_and_it_matched(self):
        _code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        proofs = {
            e["path"]: e["content_proof"] for e in receipt["assemble"]["verified"]
        }
        self.assertEqual(
            proofs,
            {self.artifact("findings"): "match", self.artifact("report"): "match"},
        )

    def test_running_it_twice_is_idempotent(self):
        run_cli(["--output-dir", self.out_dir, "--task", self.task])
        first = {
            e["path"]: self.read(e["path"]) for e in payload_of(self.task)["entries"]
        }
        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 0, receipt)
        for path, text in first.items():
            self.assertEqual(self.read(path), text)

    def test_materialize_happy_path_returns_a_success_receipt(self):
        # Direct materialize() on a pristine recorder task takes the success path.
        receipt = materialize(self.task, None, self.out_dir, environ={})
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["channel"], "return")


# What the last-resort guard returns, whole: main()'s stdout line is this dict
# serialized, so a field left unasserted is a stdout line that can change silently.
UNEXPECTED_RECEIPT = {
    "ok": False,
    "channel": "return",
    "source": None,
    "scanned": 0,
    "materialized": [],
    "assemble": None,
    "gaps": [],
    "errors": ["materializer failed unexpectedly: RuntimeError: injected"],
}


class TestUnexpectedFailure(unittest.TestCase):
    """The guard at both ends: materialize() builds the receipt for every caller,
    main() prints it."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)

    def raising_source(self):
        # Raise inside the first call materialize() makes, before any fixture work;
        # run_cli cannot inject across a process boundary without touching scripts/.
        return patch(
            "scripts.materialize_artifacts.select_source",
            side_effect=RuntimeError("injected"),
        )

    def test_materialize_converts_an_unexpected_exception_into_a_receipt(self):
        # materialize() promises a receipt to EVERY caller, not just to main(), so
        # the guard is asserted where it lives. Without this, moving the guard back
        # into main() would leave the docstring's promise untested and false again.
        with self.raising_source():
            receipt = materialize("/nonexistent", None, self.out_dir, environ={})
        self.assertEqual(receipt, UNEXPECTED_RECEIPT)

    def test_main_unexpected_exception_still_prints_one_line_receipt(self):
        buf = io.StringIO()
        with self.raising_source(), patch("sys.stdout", buf):
            code = main(
                ["--output-dir", self.out_dir, "--task", "/nonexistent"],
            )
        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, buf.getvalue())
        self.assertEqual(json.loads(lines[0]), UNEXPECTED_RECEIPT)
        self.assertEqual(code, 1)


class TestResolution(MaterializeTestCase):
    def test_the_nonce_alone_finds_the_run_when_no_task_id_is_in_hand(self):
        # The Workflow tool returns inline on a fast run, so there may be no Task ID to
        # thread — but the harness writes the file either way.
        code, receipt, _ = run_cli(
            ["--output-dir", self.out_dir, "--nonce", NONCE],
            {"CODE_GAUNTLET_TASKS_DIR": os.path.dirname(self.task)},
        )
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["source"], self.task)

    def test_a_foreign_nonce_is_refused_and_nothing_is_written(self):
        code, receipt, _ = run_cli(
            [
                "--output-dir",
                self.out_dir,
                "--task",
                self.task,
                "--nonce",
                "some-other-run",
            ],
        )
        self.assertEqual(code, 1)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["materialized"], [])
        self.assertFalse(os.path.exists(self.artifact("findings")))

    def test_no_source_at_all_still_prints_one_line(self):
        code, receipt, _ = run_cli(
            ["--output-dir", self.out_dir, "--nonce", "nothing-here"],
            {"CODE_GAUNTLET_TASKS_DIR": os.path.join(self.tmp, "empty")},
        )
        self.assertEqual(code, 1)
        self.assertIsNone(receipt["source"])
        self.assertIn("no task output file", receipt["errors"][0])

    def test_neither_target_is_a_usage_error_with_empty_stdout(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--output-dir", self.out_dir],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")


class TestFailureModes(MaterializeTestCase):
    def test_a_truncated_task_output_writes_nothing(self):
        # Truncation on this platform is documented as SILENT, so it must be the
        # detection that is loud: a truncated document does not parse, no terminal
        # object is found, and nothing is written at a planned path.
        with open(self.task, encoding="utf-8") as fh:
            text = fh.read()
        with open(self.task, "w", encoding="utf-8") as fh:
            fh.write(text[: len(text) // 2])
        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 1)
        self.assertEqual(receipt["materialized"], [])
        self.assertFalse(os.path.exists(self.artifact("findings")))
        self.assertFalse(os.path.exists(self.artifact("post-review")))

    def test_a_primary_that_disagrees_with_its_proof_is_disclosed_not_hidden(self):
        # The channel losing bytes is exactly what the proof exists to catch. The
        # artifacts still land (they are self-consistent and deliverable), the run
        # reports not-ok, and the gap names the path.
        def shorten(payload):
            payload["entries"][0]["text"] = json.dumps([{"id": "F1"}], indent=2)

        rewrite_payload(self.task, shorten)

        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 1)
        self.assertFalse(receipt["ok"])
        self.assertTrue(
            os.path.exists(self.artifact("findings")), "what landed is named"
        )
        self.assertEqual(len(receipt["materialized"]), 3)
        self.assertTrue(
            any(
                "artifact-content-proof" in gap and "findings" in gap
                for gap in receipt["gaps"]
            ),
            receipt["gaps"],
        )

    def test_a_derived_document_that_diverges_from_the_pipeline_is_disclosed(self):
        # The serializer canary: the plan carries the pipeline's OWN chars/checksum for
        # each projection, so a Python-vs-JS divergence surfaces instead of shipping a
        # payload the pipeline never produced. Re-proving the plan after the edit is
        # what makes this a derived-content failure rather than a plan-checksum one.
        def tamper(payload):
            plan = json.loads(payload["entries"][2]["text"])
            plan["derive"][0]["checksum"] = "fnv1a32:0xdeadbeef"
            del plan["planChecksum"]
            plan["planChecksum"] = plan_checksum(plan)
            payload["entries"][2]["text"] = json.dumps(plan, indent=2)

        rewrite_payload(self.task, tamper)

        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 1)
        self.assertTrue(
            any(
                "does not match the pipeline's own derivation" in gap
                for gap in receipt["gaps"]
            ),
            receipt["gaps"],
        )

    def test_an_entry_may_not_write_outside_the_output_directory(self):
        escapee = os.path.join(self.tmp, "elsewhere.json")

        def redirect(payload):
            payload["entries"][0]["path"] = escapee

        rewrite_payload(self.task, redirect)

        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(escapee))
        self.assertEqual(
            receipt["materialized"], [], "a refused payload writes nothing at all"
        )
        self.assertIn("outside the output directory", receipt["errors"][0])

    def test_a_payload_naming_no_plan_writes_nothing(self):
        def drop(payload):
            payload["planPath"] = f"{self.out_dir}/not-an-entry.json"

        rewrite_payload(self.task, drop)

        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 1)
        self.assertEqual(receipt["materialized"], [])
        self.assertFalse(os.path.exists(self.artifact("findings")))


class TestOtherChannelsUntouched(MaterializeTestCase):
    def test_a_return_without_persistReturn_is_not_a_source(self):
        with open(self.task, encoding="utf-8") as fh:
            envelope = json.load(fh)
        del envelope["result"]["persistReturn"]
        with open(self.task, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, indent=2)

        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 1)
        self.assertIsNone(receipt["source"])
        self.assertFalse(os.path.exists(self.artifact("findings")))

    def test_a_foreign_channel_name_is_skipped_rather_than_guessed_at(self):
        rewrite_payload(
            self.task, lambda p: p.__setitem__("channel", "some-future-channel")
        )
        code, receipt, _ = run_cli(["--output-dir", self.out_dir, "--task", self.task])
        self.assertEqual(code, 1)
        self.assertIsNone(receipt["source"])


if __name__ == "__main__":
    unittest.main()
