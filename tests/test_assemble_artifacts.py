"""
Tests for scripts/assemble_artifacts.py.

The script is the disk-side half of the code-gauntlet persistence redesign
(issue #38, D3): the artifact-writer agent persists only the UNIQUE content
(findings.json, report.md, the persist plan), and this script DERIVES the two
artifacts that are pure projections of findings.json — the post-review delivery
set and the resume checkpoint — while emitting a content-proof receipt.

Contract under test:
  * exactly one line of JSON on stdout, diagnostics on stderr — on EVERY path,
    including an unexpected internal error (no traceback, no empty stdout);
  * structural failures (missing file, unparseable JSON, missing/duplicate id,
    an unproven or altered plan, a number this runtime cannot spell the way
    JSON.stringify does) are HARD failures: exit non-zero, ok:false, nothing
    written, and NOTHING left truncated at a planned path;
  * a checksum mismatch on one of the `expect` PRIMARIES is NOT a hard failure —
    the on-disk bytes are the source of truth, so derivation proceeds and the
    entry is stamped content_proof:"mismatch" so the caller can surface it loudly.
    A PLAN checksum mismatch is the opposite call: the plan is the instruction
    set, not data, so it is never executed;
  * the fnv1a32-over-UTF-16-code-units checksum AND the pretty printer are both
    byte-identical to the JS implementations that run inside the workflow sandbox.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from scripts.assemble_artifacts import (  # noqa: E402
    JsSerializationError,
    assemble,
    fnv1a32,
    js_stringify_pretty,
    normalize_content,
    plan_checksum,
    utf16_len,
    write_text_atomic,
)

SCRIPT = os.path.join(REPO_ROOT, "scripts", "assemble_artifacts.py")

# The JS twin, verbatim from the design spec (D3.1). It must live in the sandbox
# with no TextEncoder/Buffer, so it walks UTF-16 code units via charCodeAt.
JS_CHECKSUM = (
    "let s = process.argv[1];"
    "let h = 0x811c9dc5;"
    "for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }"
    "process.stdout.write('fnv1a32:0x' + h.toString(16).padStart(8, '0') + ' ' + s.length);"
)

# JSON.stringify(x, null, 2) for a list of JSON *documents* (passed as text so a
# lone surrogate survives the argv hop, which raw UTF-8 could not). Returns the
# pretty strings as a JSON array — stdout stays well-formed because a well-formed
# JSON.stringify escapes any lone surrogate rather than emitting it raw.
JS_STRINGIFY = (
    "const docs = JSON.parse(process.argv[1]);"
    "process.stdout.write(JSON.stringify(docs.map((d) => JSON.stringify(JSON.parse(d), null, 2))));"
)

# The plan self-proof, computed the way workflows/src/stages.js persistPlan computes
# it: delete the LAST key (`planChecksum`), pretty-print, fnv1a32. `delete` preserves
# the order of the remaining keys, exactly as Python's dict comprehension does.
JS_PLAN_CHECKSUM = (
    "const plan = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));"
    "delete plan.planChecksum;"
    "const s = JSON.stringify(plan, null, 2);"
    "let h = 0x811c9dc5;"
    "for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }"
    "process.stdout.write('fnv1a32:0x' + h.toString(16).padStart(8, '0'));"
)


def node_or_skip(case):
    if shutil.which("node") is None:
        case.skipTest("node not available")


def js_stringify_many(docs_as_json_text):
    """Run node's JSON.stringify(JSON.parse(text), null, 2) over each document."""
    proc = subprocess.run(
        ["node", "-e", JS_STRINGIFY, json.dumps(docs_as_json_text)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


def finding(fid, **over):
    """A canonical persisted finding: canonical schema + the v2 aliases the
    artifact-writer boundary adds (line/end_line/body)."""
    f = {
        "id": fid,
        "file": "src/%s.js" % fid,
        "line_start": 10,
        "line_end": 12,
        "title": "finding %s" % fid,
        "description": "a real problem in %s" % fid,
        "severity": "high",
        "confidence": 90,
        "dimension": "bug",
        "origin": "new",
        "cross_file_refs": [],
    }
    f.update(over)
    f["line"] = f["line_start"]
    f["end_line"] = f["line_end"]
    f["body"] = f["description"]
    return f


def js_pretty(obj):
    """Byte-equivalent of JSON.stringify(obj, null, 2)."""
    return json.dumps(obj, indent=2, ensure_ascii=False)


class _Workspace:
    """A temp output dir with findings.json + report.md already on disk."""

    def __init__(self, findings=None, report="# report\n\nbody", findings_json=None):
        self.findings = (
            findings if findings is not None else [finding("F1"), finding("F2")]
        )
        self.report = report
        # Override for fixtures whose on-disk bytes are not plain js_pretty output —
        # a lone surrogate, for instance, is ESCAPED on disk (JSON.stringify is
        # well-formed) and could not be written raw at all.
        self.findings_json_override = findings_json

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="assemble-")
        self.findings_path = os.path.join(
            self.dir, "code-gauntlet-findings-abc1234.json"
        )
        self.report_path = os.path.join(self.dir, "code-gauntlet-report-abc1234.md")
        self.post_path = os.path.join(
            self.dir, "code-gauntlet-post-review-abc1234.json"
        )
        self.checkpoint_path = os.path.join(
            self.dir, "code-gauntlet-checkpoint-all-abc1234.json"
        )
        self.plan_path = os.path.join(
            self.dir, "code-gauntlet-persist-plan-abc1234.json"
        )
        self.findings_json = (
            self.findings_json_override
            if self.findings_json_override is not None
            else js_pretty(self.findings)
        )
        self.write(self.findings_path, self.findings_json)
        self.write(self.report_path, self.report)
        return self

    def __exit__(self, exc_type, exc, tb):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def read(self, path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def plan(self, **over):
        ids = [f["id"] for f in self.findings if isinstance(f, dict) and "id" in f]
        plan = {
            "planVersion": 2,
            "expect": [
                {
                    "path": self.findings_path,
                    "chars": utf16_len(self.findings_json),
                    "checksum": fnv1a32(self.findings_json),
                },
                {
                    "path": self.report_path,
                    "chars": utf16_len(normalize_content(self.report)),
                    "checksum": fnv1a32(normalize_content(self.report)),
                },
            ],
            "postReview": {
                "path": self.post_path,
                "source": self.findings_path,
                "ids": list(ids),
                "wrapper": None,
            },
            "checkpoint": {
                "path": self.checkpoint_path,
                "source": self.findings_path,
                "challengeFindingIds": list(ids),
                "stripAliasFields": ["line", "end_line", "body"],
                "skeleton": {
                    "phases": {
                        "challenge": {
                            "findings": [],
                            "unverified": [],
                            "eliminated": [],
                            "gaps": [],
                            "stats": {},
                            "generated_at": "2026-07-27T00:00:00Z",
                        }
                    },
                    "completed": ["challenge"],
                    "phaseReached": "report",
                    "counts": {"challenge": len(ids)},
                },
            },
        }
        plan.update(over)
        return plan

    def write_plan(self, plan, seal=True):
        """Persist the plan the way the pipeline does: the self-proof is computed
        LAST, over the plan without it. `seal=False` writes it unproven."""
        out = dict(plan)
        out.pop("planChecksum", None)
        if seal:
            out["planChecksum"] = plan_checksum(out)
        self.write(self.plan_path, js_pretty(out))
        return self.plan_path

    def tamper_plan(self, mutate):
        """Seal a plan, then alter it WITHOUT re-sealing — a writer that elided or
        reordered entries while transcribing."""
        sealed = json.loads(self.read(self.write_plan(self.plan())))
        mutate(sealed)
        self.write(self.plan_path, js_pretty(sealed))
        return self.plan_path


def run_script(plan_path):
    """Invoke the real CLI (a single invocation of plain word tokens)."""
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--plan", plan_path],
        capture_output=True,
        text=True,
    )
    return proc


class TestChecksum(unittest.TestCase):
    def test_known_vector(self):
        # fnv1a32 of the empty string is the offset basis.
        self.assertEqual(fnv1a32(""), "fnv1a32:0x811c9dc5")

    def test_output_is_always_eight_hex_digits(self):
        for s in ["", "a", "abc", "x" * 100]:
            self.assertRegex(fnv1a32(s), r"^fnv1a32:0x[0-9a-f]{8}$")

    def test_utf16_len_counts_code_units_not_codepoints(self):
        self.assertEqual(utf16_len("abc"), 3)
        self.assertEqual(utf16_len("café"), 4)
        self.assertEqual(utf16_len("日本語"), 3)
        self.assertEqual(utf16_len("😀"), 2)  # surrogate pair

    def test_normalize_strips_bom_and_one_trailing_newline(self):
        self.assertEqual(normalize_content("﻿abc"), "abc")
        self.assertEqual(normalize_content("abc\n"), "abc")
        self.assertEqual(normalize_content("abc\r\n"), "abc")
        # At most ONE trailing newline is tolerated.
        self.assertEqual(normalize_content("abc\n\n"), "abc\n")
        self.assertEqual(normalize_content("abc"), "abc")


class TestCrossRuntimeChecksumParity(unittest.TestCase):
    """The JS implementation runs in the sandbox; Python runs on disk. They must
    agree exactly — surrogate pairs (emoji, astral plane) are the trap."""

    STRINGS = [
        "",
        "a",
        "hello world",
        '{"id":"F1","line_start":10}',
        "café — naïve",
        "日本語のテキストです",
        "中文字符测试",
        "😀",
        "😀🎉🚀",
        "mixed 😀 café 日本語 tail",
        "𝕏 astral plane 𝔸𝔹ℂ",
        "line1\nline2\ttab\r\n",
        "𠜎𠜱𠝹",  # CJK extension B (astral)
    ]

    def test_js_and_python_agree(self):
        if shutil.which("node") is None:
            self.skipTest("node not available")
        for s in self.STRINGS:
            proc = subprocess.run(
                ["node", "-e", JS_CHECKSUM, s], capture_output=True, text=True
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            js_checksum, js_chars = proc.stdout.strip().split(" ")
            self.assertEqual(js_checksum, fnv1a32(s), "checksum mismatch for %r" % s)
            self.assertEqual(
                int(js_chars), utf16_len(s), "char count mismatch for %r" % s
            )


class TestEscapeHardenedPrimaryIsAcceptedUnchanged(unittest.TestCase):
    """The cross-runtime half of hardenEscapeRuns (see workflows/src/stages.js).

    The JS side respells every escaped backslash in findings.json as \\u005c so the
    artifact-writer never has to transcribe a run of backslashes — the failure that
    cost run wf_adc1a803-912 (2026-07-30) every artifact. That fix ships with NO
    Python change, and this is the guard on that claim: the hardened bytes must be
    read, checksummed and derived from exactly like any other findings.json.

    Deliberately shells out to node for the hardened string rather than
    reimplementing the transform here — a Python twin of it could drift, and the
    thing under test is precisely that the two runtimes agree on the bytes.
    """

    PROSE = 'gradeInputProof produces \\"the executor\'s receipt carried no input_checksum\\" here'

    def _hardened(self, findings):
        js = (
            "const { persistPrimaries } = await import(process.argv[1]);"
            "process.stdout.write(persistPrimaries({ findings: JSON.parse(process.argv[2]) }).findingsJson);"
        )
        proc = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                js,
                os.path.join(REPO_ROOT, "workflows", "src", "stages.js"),
                json.dumps(findings),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node not available")

    def test_hardened_findings_json_carries_no_backslash_run(self):
        findings = [dict(finding("F1"), description=self.PROSE)]
        hardened = self._hardened(findings)
        self.assertNotIn(
            "\\\\", hardened, "a run of two backslashes is what the writer collapses"
        )
        self.assertIn(
            "\\u005c",
            hardened,
            "precondition: the fixture actually exercises the transform",
        )
        self.assertEqual(json.loads(hardened)[0]["description"], self.PROSE)

    def test_assembler_accepts_it_and_derives_the_same_documents(self):
        findings = [dict(finding("F1"), description=self.PROSE)]
        hardened = self._hardened(findings)
        with _Workspace(findings=json.loads(hardened), findings_json=hardened) as ws:
            proc = run_script(ws.write_plan(ws.plan()))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            receipt = json.loads(proc.stdout)
            self.assertTrue(receipt["ok"], receipt)
            self.assertEqual(receipt["errors"], [])
            # The content proof is over the HARDENED bytes on both sides.
            proofs = {e["path"]: e["content_proof"] for e in receipt["verified"]}
            self.assertEqual(proofs[ws.findings_path], "match")
            # The derived documents are spelled the ORDINARY way (Python re-serializes
            # the parsed value), and carry the finding text byte for byte.
            post = json.loads(ws.read(ws.post_path))
            self.assertEqual(post[0]["description"], self.PROSE)
            self.assertNotIn(
                "\\u005c",
                ws.read(ws.post_path),
                "hardening is a WIRE spelling, not a data change",
            )
            checkpoint = json.loads(ws.read(ws.checkpoint_path))
            self.assertEqual(
                checkpoint["phases"]["challenge"]["findings"][0]["description"],
                self.PROSE,
            )

    def test_the_unhardened_spelling_of_the_same_document_still_works(self):
        """Hardening must be OPTIONAL to the reader: an artifact written the old way
        (or by the legacy by-value path) is still a valid input."""
        findings = [dict(finding("F1"), description=self.PROSE)]
        with _Workspace(findings=findings) as ws:
            self.assertIn(
                "\\\\",
                ws.findings_json,
                "precondition: this fixture is the UNhardened spelling",
            )
            proc = run_script(ws.write_plan(ws.plan()))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            receipt = json.loads(proc.stdout)
            self.assertTrue(receipt["ok"], receipt)
            post = json.loads(ws.read(ws.post_path))
            self.assertEqual(post[0]["description"], self.PROSE)


class TestRoundTripDerivation(unittest.TestCase):
    def test_derives_post_review_and_checkpoint(self):
        with _Workspace() as ws:
            path = ws.write_plan(ws.plan())
            proc = run_script(path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            receipt = json.loads(proc.stdout)
            self.assertTrue(receipt["ok"], receipt)
            self.assertEqual(receipt["errors"], [])
            self.assertEqual(receipt["planVersion"], 2)

            post = json.loads(ws.read(ws.post_path))
            self.assertEqual(post, ws.findings)

            cp = json.loads(ws.read(ws.checkpoint_path))
            self.assertEqual(len(cp["phases"]["challenge"]["findings"]), 2)
            self.assertEqual(cp["completed"], ["challenge"])
            self.assertEqual(cp["phaseReached"], "report")

    def test_receipt_lists_verified_and_written_paths(self):
        with _Workspace() as ws:
            receipt = json.loads(run_script(ws.write_plan(ws.plan())).stdout)
            verified = {e["path"] for e in receipt["verified"]}
            written = {e["path"] for e in receipt["written"]}
            self.assertEqual(verified, {ws.findings_path, ws.report_path})
            self.assertEqual(written, {ws.post_path, ws.checkpoint_path})
            for e in receipt["verified"]:
                self.assertEqual(e["content_proof"], "match")
            for e in receipt["written"]:
                self.assertIsInstance(e["chars"], int)
                self.assertRegex(e["checksum"], r"^fnv1a32:0x[0-9a-f]{8}$")

    def test_receipt_is_exactly_one_stdout_line(self):
        with _Workspace() as ws:
            proc = run_script(ws.write_plan(ws.plan()))
            self.assertEqual(len(proc.stdout.strip().split("\n")), 1)
            json.loads(proc.stdout)  # the whole of stdout is that one object

    def test_derived_json_is_js_pretty_byte_identical(self):
        with _Workspace() as ws:
            run_script(ws.write_plan(ws.plan()))
            self.assertEqual(ws.read(ws.post_path), js_pretty(ws.findings))


class TestProjection(unittest.TestCase):
    def test_id_order_drives_output_order(self):
        with _Workspace(findings=[finding("A"), finding("B"), finding("C")]) as ws:
            plan = ws.plan()
            plan["postReview"]["ids"] = ["C", "A"]
            receipt = json.loads(run_script(ws.write_plan(plan)).stdout)
            self.assertTrue(receipt["ok"], receipt)
            post = json.loads(ws.read(ws.post_path))
            self.assertEqual([f["id"] for f in post], ["C", "A"])

    def test_wrapper_emits_the_post_review_envelope(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["postReview"]["wrapper"] = {
                "owner": "o",
                "repo": "r",
                "pr_number": 7,
                "sha": "deadbeef",
                "review_body": "",
            }
            receipt = json.loads(run_script(ws.write_plan(plan)).stdout)
            self.assertTrue(receipt["ok"], receipt)
            post = json.loads(ws.read(ws.post_path))
            self.assertEqual(
                list(post.keys()),
                ["owner", "repo", "pr_number", "sha", "review_body", "findings"],
            )
            self.assertEqual(post["findings"], ws.findings)

    def test_null_wrapper_emits_a_bare_array(self):
        with _Workspace() as ws:
            run_script(ws.write_plan(ws.plan()))
            self.assertIsInstance(json.loads(ws.read(ws.post_path)), list)

    def test_checkpoint_strips_the_v2_alias_fields(self):
        with _Workspace() as ws:
            run_script(ws.write_plan(ws.plan()))
            cp = json.loads(ws.read(ws.checkpoint_path))
            for f in cp["phases"]["challenge"]["findings"]:
                self.assertNotIn("line", f)
                self.assertNotIn("end_line", f)
                self.assertNotIn("body", f)
                self.assertIn("line_start", f)
                self.assertIn("description", f)

    def test_checkpoint_findings_keep_their_position_in_the_challenge_object(self):
        with _Workspace() as ws:
            run_script(ws.write_plan(ws.plan()))
            cp = json.loads(ws.read(ws.checkpoint_path))
            self.assertEqual(
                list(cp["phases"]["challenge"].keys()),
                [
                    "findings",
                    "unverified",
                    "eliminated",
                    "gaps",
                    "stats",
                    "generated_at",
                ],
            )

    def test_empty_id_lists_derive_empty_artifacts(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["postReview"]["ids"] = []
            plan["checkpoint"]["challengeFindingIds"] = []
            receipt = json.loads(run_script(ws.write_plan(plan)).stdout)
            self.assertTrue(receipt["ok"], receipt)
            self.assertEqual(json.loads(ws.read(ws.post_path)), [])
            cp = json.loads(ws.read(ws.checkpoint_path))
            self.assertEqual(cp["phases"]["challenge"]["findings"], [])


class TestStructuralHardFailures(unittest.TestCase):
    """Exit non-zero, ok:false, NOTHING written. This is the class that caught
    the #25 incident (tool-call markup appended after the JSON document)."""

    def assert_hard_failure(self, ws, plan, needle):
        proc = run_script(ws.write_plan(plan))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        receipt = json.loads(proc.stdout)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["written"], [])
        self.assertTrue(
            any(needle in e for e in receipt["errors"]),
            "expected %r in %r" % (needle, receipt["errors"]),
        )
        self.assertFalse(os.path.exists(ws.post_path))
        self.assertFalse(os.path.exists(ws.checkpoint_path))

    def test_missing_expected_file(self):
        with _Workspace() as ws:
            os.unlink(ws.report_path)
            self.assert_hard_failure(ws, ws.plan(), "not found")

    def test_unparseable_json_source(self):
        with _Workspace() as ws:
            ws.write(ws.findings_path, ws.findings_json + "\n<tool_use>oops</tool_use>")
            self.assert_hard_failure(ws, ws.plan(), "not valid JSON")

    def test_requested_id_absent_from_source(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["postReview"]["ids"] = ["F1", "GHOST"]
            self.assert_hard_failure(ws, plan, "GHOST")

    def test_requested_challenge_id_absent_from_source(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["checkpoint"]["challengeFindingIds"] = ["F1", "PHANTOM"]
            self.assert_hard_failure(ws, plan, "PHANTOM")

    def test_duplicate_ids_in_source(self):
        with _Workspace(findings=[finding("F1"), finding("F1")]) as ws:
            self.assert_hard_failure(ws, ws.plan(), "duplicate")

    def test_source_entry_without_an_id(self):
        bad = finding("F2")
        del bad["id"]
        with _Workspace(findings=[finding("F1"), bad]) as ws:
            self.assert_hard_failure(ws, ws.plan(), "id")

    def test_source_is_not_an_array(self):
        with _Workspace() as ws:
            ws.write(ws.findings_path, js_pretty({"findings": []}))
            self.assert_hard_failure(ws, ws.plan(), "array")

    def test_unsupported_plan_version(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["planVersion"] = 3
            self.assert_hard_failure(ws, plan, "planVersion")

    def test_challenge_ids_with_no_challenge_phase_in_the_skeleton(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["checkpoint"]["skeleton"] = {"phases": {}, "completed": []}
            self.assert_hard_failure(ws, plan, "challenge")

    def test_missing_plan_file(self):
        proc = run_script("/nonexistent/plan.json")
        self.assertNotEqual(proc.returncode, 0)
        receipt = json.loads(proc.stdout)
        self.assertFalse(receipt["ok"])
        self.assertTrue(any("not found" in e for e in receipt["errors"]))

    def test_unparseable_plan_file(self):
        with _Workspace() as ws:
            ws.write(ws.plan_path, "{not json")
            proc = run_script(ws.plan_path)
            self.assertNotEqual(proc.returncode, 0)
            receipt = json.loads(proc.stdout)
            self.assertFalse(receipt["ok"])
            self.assertEqual(receipt["written"], [])


class TestChecksumMismatchIsNotFatal(unittest.TestCase):
    """The source of truth is what is actually on disk. Refusing to derive here
    would invent a new way to lose a run — the opposite of never-fabricate."""

    def test_mismatch_still_derives_and_is_stamped(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["expect"][0]["checksum"] = "fnv1a32:0xdeadbeef"
            plan["expect"][0]["chars"] = 999999
            proc = run_script(ws.write_plan(plan))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            receipt = json.loads(proc.stdout)
            self.assertTrue(receipt["ok"])
            entry = [e for e in receipt["verified"] if e["path"] == ws.findings_path][0]
            self.assertEqual(entry["content_proof"], "mismatch")
            self.assertEqual(entry["expected_checksum"], "fnv1a32:0xdeadbeef")
            self.assertEqual(entry["expected_chars"], 999999)
            self.assertEqual(entry["chars"], utf16_len(ws.findings_json))
            self.assertEqual(entry["checksum"], fnv1a32(ws.findings_json))
            # Derived anyway, from the on-disk truth.
            self.assertTrue(os.path.exists(ws.post_path))
            self.assertEqual(json.loads(ws.read(ws.post_path)), ws.findings)

    def test_report_mismatch_is_reported_per_entry(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["expect"][1]["checksum"] = "fnv1a32:0x00000000"
            receipt = json.loads(run_script(ws.write_plan(plan)).stdout)
            proofs = {e["path"]: e["content_proof"] for e in receipt["verified"]}
            self.assertEqual(proofs[ws.findings_path], "match")
            self.assertEqual(proofs[ws.report_path], "mismatch")


class TestWriteToolNormalizationTolerance(unittest.TestCase):
    """The Write tool may normalise a trailing newline or add a BOM; a false
    degrade must not cost a run its artifacts."""

    def test_trailing_newline_added_by_the_writer_still_matches(self):
        with _Workspace() as ws:
            ws.write(ws.findings_path, ws.findings_json + "\n")
            receipt = json.loads(run_script(ws.write_plan(ws.plan())).stdout)
            entry = [e for e in receipt["verified"] if e["path"] == ws.findings_path][0]
            self.assertEqual(entry["content_proof"], "match")

    def test_crlf_trailing_newline_still_matches(self):
        with _Workspace() as ws:
            ws.write(ws.findings_path, ws.findings_json + "\r\n")
            receipt = json.loads(run_script(ws.write_plan(ws.plan())).stdout)
            entry = [e for e in receipt["verified"] if e["path"] == ws.findings_path][0]
            self.assertEqual(entry["content_proof"], "match")

    def test_bom_prefix_still_matches(self):
        with _Workspace() as ws:
            ws.write(ws.findings_path, "﻿" + ws.findings_json)
            receipt = json.loads(run_script(ws.write_plan(ws.plan())).stdout)
            entry = [e for e in receipt["verified"] if e["path"] == ws.findings_path][0]
            self.assertEqual(entry["content_proof"], "match")

    def test_two_trailing_newlines_is_a_real_mismatch(self):
        with _Workspace() as ws:
            ws.write(ws.findings_path, ws.findings_json + "\n\n")
            receipt = json.loads(run_script(ws.write_plan(ws.plan())).stdout)
            entry = [e for e in receipt["verified"] if e["path"] == ws.findings_path][0]
            self.assertEqual(entry["content_proof"], "mismatch")


class TestNonAsciiContent(unittest.TestCase):
    """Findings carry prose; prose carries emoji and CJK. The derived artifacts
    must round-trip them and the checksums must still agree."""

    def test_astral_and_cjk_content_round_trips(self):
        findings = [
            finding("F1", description="日本語の説明 😀 with an astral 𝕏"),
            finding("F2", title="中文标题 🎉"),
        ]
        with _Workspace(findings=findings) as ws:
            receipt = json.loads(run_script(ws.write_plan(ws.plan())).stdout)
            self.assertTrue(receipt["ok"], receipt)
            entry = [e for e in receipt["verified"] if e["path"] == ws.findings_path][0]
            self.assertEqual(entry["content_proof"], "match")
            post = json.loads(ws.read(ws.post_path))
            self.assertEqual(post[0]["description"], "日本語の説明 😀 with an astral 𝕏")
            self.assertEqual(post[1]["title"], "中文标题 🎉")

    def test_non_ascii_is_not_escaped_in_the_derived_json(self):
        with _Workspace(findings=[finding("F1", title="日本語 😀")]) as ws:
            run_script(ws.write_plan(ws.plan()))
            self.assertIn("日本語 😀", ws.read(ws.post_path))
            self.assertNotIn("\\u65e5", ws.read(ws.post_path))


class TestPlanSelfProof(unittest.TestCase):
    """issue #38 L1-2. The plan is transcribed to disk by the artifact-writer just
    like the two primaries, but it is the INSTRUCTION SET, not data: postReview.ids
    alone decides which findings reach the delivered artifact. A writer that elides
    entries used to produce a silently smaller delivered set with an ok:true receipt
    and no gap. So the plan proves itself, and an unproven plan is not executed."""

    def test_receipt_echoes_the_recomputed_plan_checksum(self):
        with _Workspace() as ws:
            path = ws.write_plan(ws.plan())
            declared = json.loads(ws.read(path))["planChecksum"]
            receipt = json.loads(run_script(path).stdout)
            self.assertTrue(receipt["ok"], receipt)
            self.assertEqual(receipt["planChecksum"], declared)
            self.assertRegex(receipt["planChecksum"], r"^fnv1a32:0x[0-9a-f]{8}$")

    def assert_refuses_to_execute(self, ws, path):
        proc = run_script(path)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        receipt = json.loads(proc.stdout)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["written"], [])
        self.assertTrue(
            any("plan checksum" in e or "planChecksum" in e for e in receipt["errors"]),
            receipt["errors"],
        )
        # An untrustworthy instruction set must not be PARTLY executed either.
        self.assertFalse(os.path.exists(ws.post_path))
        self.assertFalse(os.path.exists(ws.checkpoint_path))

    def test_an_elided_delivery_id_is_refused(self):
        # The exact issue-38 hard-line violation: two ids become one, the delivered
        # set silently shrinks. Without the proof this ran happily to ok:true.
        with _Workspace(findings=[finding("A"), finding("B"), finding("C")]) as ws:

            def drop(plan):
                plan["postReview"]["ids"] = ["A", "B"]

            self.assert_refuses_to_execute(ws, ws.tamper_plan(drop))

    def test_a_reordered_delivery_id_list_is_refused(self):
        with _Workspace(findings=[finding("A"), finding("B"), finding("C")]) as ws:

            def swap(plan):
                plan["postReview"]["ids"] = ["C", "B", "A"]

            self.assert_refuses_to_execute(ws, ws.tamper_plan(swap))

    def test_an_elided_challenge_id_is_refused(self):
        with _Workspace(findings=[finding("A"), finding("B")]) as ws:

            def drop(plan):
                plan["checkpoint"]["challengeFindingIds"] = ["A"]

            self.assert_refuses_to_execute(ws, ws.tamper_plan(drop))

    def test_an_altered_skeleton_or_path_is_refused(self):
        with _Workspace() as ws:

            def repoint(plan):
                plan["postReview"]["path"] = plan["postReview"]["path"] + ".other"

            self.assert_refuses_to_execute(ws, ws.tamper_plan(repoint))
        with _Workspace() as ws:

            def restamp(plan):
                plan["checkpoint"]["skeleton"]["phaseReached"] = "challenge"

            self.assert_refuses_to_execute(ws, ws.tamper_plan(restamp))

    def test_a_plan_with_no_checksum_is_refused(self):
        with _Workspace() as ws:
            self.assert_refuses_to_execute(ws, ws.write_plan(ws.plan(), seal=False))

    def test_a_non_string_checksum_is_refused(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["planChecksum"] = 0
            ws.write(ws.plan_path, js_pretty(plan))
            self.assert_refuses_to_execute(ws, ws.plan_path)

    def test_the_proof_survives_the_write_tool_normalisations(self):
        # The plan file goes through the same Write tool as the primaries, so the
        # same BOM / trailing-newline tolerance must apply — otherwise the proof
        # becomes a new way to lose a run.
        for suffix, prefix in [("\n", ""), ("\r\n", ""), ("", "﻿")]:
            with _Workspace() as ws:
                path = ws.write_plan(ws.plan())
                ws.write(path, prefix + ws.read(path) + suffix)
                receipt = json.loads(run_script(path).stdout)
                self.assertTrue(receipt["ok"], (prefix, suffix, receipt))

    def test_plan_checksum_is_computed_over_the_plan_minus_the_field(self):
        with _Workspace() as ws:
            sealed = json.loads(ws.read(ws.write_plan(ws.plan())))
            self.assertEqual(list(sealed.keys())[-1], "planChecksum")
            body = dict((k, v) for (k, v) in sealed.items() if k != "planChecksum")
            self.assertEqual(sealed["planChecksum"], fnv1a32(js_stringify_pretty(body)))


class TestPlanChecksumCrossRuntime(unittest.TestCase):
    """The construction has to be unambiguous in BOTH runtimes: the workflow sandbox
    computes it with JSON.stringify + charCodeAt, this script recomputes it with
    json.dumps + utf-16-le. If they ever disagree the plan can never be executed."""

    def test_node_and_python_agree_on_the_plan_checksum(self):
        node_or_skip(self)
        cases = [
            [finding("F1"), finding("F2")],
            [
                finding("A", description="日本語 😀 astral 𝕏"),
                finding("B", title="中文 🎉"),
            ],
            [],
        ]
        for findings in cases:
            with _Workspace(findings=findings) as ws:
                path = ws.write_plan(ws.plan())
                proc = subprocess.run(
                    ["node", "-e", JS_PLAN_CHECKSUM, path],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    proc.stdout.strip(), json.loads(ws.read(path))["planChecksum"]
                )


class TestLoneSurrogatesAreEscapedNotFatal(unittest.TestCase):
    """issue #38 L1-1. A lone UTF-16 surrogate anywhere in findings.json used to
    raise UnicodeEncodeError — a ValueError, so `except (IOError, OSError)` missed
    it — printing a traceback with EMPTY stdout (breaking the one-line-receipt
    contract) and leaving a truncated ZERO-BYTE file at the post-review path
    (breaking the hard-failure-writes-nothing contract).

    The fix is to spell it the way a well-formed JSON.stringify does, so it is not
    a failure at all."""

    def workspace(self):
        # A lone surrogate reaches findings.json as a JSON escape — it is not
        # UTF-8-encodable any other way, and JSON.stringify has been well-formed
        # since ES2019. This fixture is pure ASCII apart from that one escape, so
        # `ensure_ascii=True` reproduces JS's output for it exactly (the corpus in
        # TestCrossRuntimeStringifyParity proves the general case against node).
        findings = [
            finding("F1", description=json.loads('"a lone \\ud800 surrogate"')),
            finding("F2"),
        ]
        return _Workspace(
            findings=findings, findings_json=json.dumps(findings, indent=2)
        )

    def test_a_lone_surrogate_derives_successfully(self):
        with self.workspace() as ws:
            proc = run_script(ws.write_plan(ws.plan()))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            receipt = json.loads(proc.stdout)
            self.assertTrue(receipt["ok"], receipt)
            self.assertEqual(len(proc.stdout.strip().split("\n")), 1)

    def test_the_derived_file_is_complete_and_escapes_the_surrogate(self):
        with self.workspace() as ws:
            run_script(ws.write_plan(ws.plan()))
            text = ws.read(ws.post_path)
            self.assertGreater(len(text), 0, "a zero-byte artifact is the old bug")
            self.assertIn("\\ud800", text)
            self.assertEqual([f["id"] for f in json.loads(text)], ["F1", "F2"])

    def test_the_derived_file_is_byte_identical_to_JSON_stringify(self):
        node_or_skip(self)
        with self.workspace() as ws:
            run_script(ws.write_plan(ws.plan()))
            expected = js_stringify_many([ws.findings_json])[0]
            self.assertEqual(ws.read(ws.post_path), expected)


class TestNoTruncatedArtifactAtAPlannedPath(unittest.TestCase):
    """issue #38 L1-1, layer 3. Opening the destination for writing truncates it
    BEFORE the encode, so any failure leaves an empty file that later stages read
    as a real artifact. Every derived document is now written to a sibling temp
    file and os.replace()d into place."""

    def test_a_failing_write_leaves_the_destination_untouched(self):
        directory = tempfile.mkdtemp(prefix="atomic-")
        try:
            dest = os.path.join(directory, "artifact.json")
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write("PREVIOUS CONTENT")
            # A lone surrogate is not encodable as UTF-8: the exact failure that
            # used to truncate the destination to zero bytes.
            with self.assertRaises(UnicodeEncodeError):
                write_text_atomic(dest, "\ud800")
            with open(dest, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "PREVIOUS CONTENT")
            self.assertEqual(
                os.listdir(directory), ["artifact.json"], "no temp residue"
            )
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_a_successful_write_is_exact_and_leaves_no_temp_files(self):
        directory = tempfile.mkdtemp(prefix="atomic-")
        try:
            dest = os.path.join(directory, "artifact.json")
            write_text_atomic(dest, '{"a": 1}')
            with open(dest, "r", encoding="utf-8", newline="") as fh:
                self.assertEqual(fh.read(), '{"a": 1}')
            self.assertEqual(os.listdir(directory), ["artifact.json"])
            # The temp file's 0600 must not ride along onto the artifact.
            umask = os.umask(0)
            os.umask(umask)
            self.assertEqual(os.stat(dest).st_mode & 0o777, 0o666 & ~umask)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_a_structural_failure_never_creates_the_derived_files(self):
        with _Workspace() as ws:
            plan = ws.plan()
            plan["postReview"]["ids"] = ["F1", "GHOST"]
            proc = run_script(ws.write_plan(plan))
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(os.path.exists(ws.post_path))
            self.assertFalse(os.path.exists(ws.checkpoint_path))


class TestAnyFailureStillReturnsAReceipt(unittest.TestCase):
    """issue #38 L1-1, layer 2. `except (IOError, OSError)` missed every
    ValueError-shaped failure. The one-line-receipt contract holds on EVERY path:
    an empty stdout is indistinguishable from a dead executor."""

    def assert_honest_failure(self, proc, needle):
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(proc.stdout.strip().split("\n")), 1, proc.stdout)
        receipt = json.loads(proc.stdout)
        self.assertFalse(receipt["ok"], receipt)
        self.assertTrue(
            any(needle in e for e in receipt["errors"]),
            "expected %r in %r" % (needle, receipt["errors"]),
        )
        self.assertNotIn("Traceback", proc.stderr)
        return receipt

    def test_non_utf8_bytes_in_the_source(self):
        with _Workspace() as ws:
            with open(ws.findings_path, "wb") as fh:
                fh.write(b'[{"id": "F1", "t": "\xff\xfe"}]')
            self.assert_honest_failure(
                run_script(ws.write_plan(ws.plan())), "unreadable"
            )

    def test_non_utf8_bytes_in_the_plan(self):
        with _Workspace() as ws:
            with open(ws.plan_path, "wb") as fh:
                fh.write(b'{"planVersion": 2, "x": "\xff\xfe"}')
            self.assert_honest_failure(run_script(ws.plan_path), "unreadable")

    def test_a_derived_path_that_cannot_be_written(self):
        with _Workspace() as ws:
            os.mkdir(ws.post_path)  # a directory where a file belongs
            self.assert_honest_failure(
                run_script(ws.write_plan(ws.plan())), "could not write"
            )

    def test_the_receipt_survives_an_unexpected_internal_error(self):
        # assemble() is the last-resort guard: whatever goes wrong inside, the
        # caller gets a receipt rather than an exception.
        receipt = assemble(None)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["written"], [])
        self.assertTrue(receipt["errors"])
        json.dumps(receipt)  # serializable, so main() can still print one line


class TestNumberSpellingPrecondition(unittest.TestCase):
    """issue #38 L1-3. JS Number#toString and Python repr(float) disagree below
    1e-6, on integral floats, and on -0/NaN. The script does NOT port
    Number#toString (a port whose own bugs would be invisible is worse than a
    precondition) — it refuses any number it cannot round-trip. In the pipeline the
    JS-side persistDerivable applies the same rule first and falls back to the
    legacy by-value writer, so this precondition costs a run nothing."""

    def test_integers_are_accepted(self):
        for value in [0, -1, 90, 2**53 - 1, -(2**53 - 1)]:
            self.assertEqual(js_stringify_pretty(value), json.dumps(value))

    def test_non_integer_numbers_are_refused(self):
        for value in [1e-7, 0.000001, 90.5, -0.0, float("nan"), float("inf")]:
            with self.assertRaises(JsSerializationError):
                js_stringify_pretty({"confidence": value})

    def test_integers_outside_the_js_safe_range_are_refused(self):
        for value in [2**53, -(2**53), 10**30]:
            with self.assertRaises(JsSerializationError):
                js_stringify_pretty([value])

    def test_the_error_names_the_path(self):
        with self.assertRaises(JsSerializationError) as caught:
            js_stringify_pretty({"phases": {"challenge": {"stats": {"rate": 0.5}}}})
        self.assertIn("$.phases.challenge.stats.rate", str(caught.exception))

    def test_a_float_in_the_source_is_a_structural_failure(self):
        with _Workspace(findings=[finding("F1", confidence=0.9)]) as ws:
            proc = run_script(ws.write_plan(ws.plan()))
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            receipt = json.loads(proc.stdout)
            self.assertFalse(receipt["ok"])
            self.assertTrue(
                any("non-integer number" in e for e in receipt["errors"]),
                receipt["errors"],
            )
            self.assertFalse(os.path.exists(ws.post_path))

    def test_a_bare_NaN_in_the_source_is_a_structural_failure(self):
        # json.loads ACCEPTS bare NaN/Infinity by default; JSON.parse rejects them
        # and JSON.stringify would spell them `null`. Either way they must not reach
        # a derived artifact.
        with _Workspace() as ws:
            ws.write(ws.findings_path, '[{"id": "F1", "confidence": NaN}]')
            proc = run_script(ws.write_plan(ws.plan()))
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertFalse(json.loads(proc.stdout)["ok"])
            self.assertFalse(os.path.exists(ws.post_path))


class TestCrossRuntimeStringifyParity(unittest.TestCase):
    """js_stringify_pretty must be byte-identical to JSON.stringify(obj, null, 2).
    The corpus is the trap list: lone surrogates (L1-1), astral pairs, U+2028/9,
    control characters, empty containers, and the numeric edges (L1-3)."""

    # Documents as JSON TEXT so escapes survive the argv hop into node unchanged.
    AGREE = [
        "[]",
        "{}",
        '[{}, [], "", null, true, false]',
        '{"a": {"b": {"c": []}}}',
        '"plain string"',
        '["\\ud800"]',  # lone high surrogate
        '["\\udfff"]',  # lone low surrogate
        '["pre\\ud800post"]',  # lone surrogate mid-string
        '{"\\ud800": "in a KEY"}',
        '["\\ud83d\\ude00"]',  # a well-formed astral pair
        '["\\ud83d\\ude00\\ud800"]',  # pair immediately followed by a lone one
        '["\\u2028\\u2029"]',  # line/paragraph separators: NOT escaped by JS
        '["\\u0000\\u0001\\u001f"]',  # control characters
        '["\\b\\f\\n\\r\\t"]',
        '["quote \\" backslash \\\\ slash /"]',
        '["café — naïve", "日本語", "𝕏 astral", "\\u007f"]',
        "[0, -0, 1, -1, 9007199254740991, -9007199254740991]",
        '{"line_start": 10, "line_end": 12, "confidence": 90}',
        '[{"id": "F1", "d": "多行\\ntext\\twith escapes"}]',
    ]

    # Documents whose naive json.dumps spelling PROVABLY differs from JSON.stringify.
    DIVERGENT = [
        "[1e-7]",  # 1e-7   vs 1e-07
        "[0.000001]",  # 0.000001 vs 1e-06
        "[90.0]",  # 90     vs 90.0
        "[-0.0]",  # 0      vs -0.0
        "[9007199254740993]",  # 2**53+1: JS parses it lossily, so the values differ
        "[1000000000000000000000000000000]",  # JS spells this 1e+30
        '{"stats": {"rate": 0.5}}',  # spells the same, but nested — proves path reporting
    ]

    # Everything this runtime REFUSES. A superset of DIVERGENT: the rule is blanket
    # "integers only" because deciding per-value which float happens to agree (1.5
    # does; 1e-7 does not) needs exactly the Number#toString port the precondition
    # exists to avoid. NaN/Infinity are here too — json.loads accepts them bare,
    # JSON.parse rejects them, JSON.stringify spells them `null`.
    REFUSE = DIVERGENT + [
        "[1.5]",
        "[1e21]",
        "[9007199254740992]",
        "[NaN]",
        "[Infinity]",
    ]

    def test_python_matches_node_over_the_trap_corpus(self):
        node_or_skip(self)
        expected = js_stringify_many(self.AGREE)
        for text, want in zip(self.AGREE, expected):
            got = js_stringify_pretty(json.loads(text))
            self.assertEqual(got, want, "divergence for %s" % text)

    def test_the_agreed_output_is_always_utf8_encodable(self):
        # The L1-1 crash: a raw lone surrogate in the output cannot be encoded.
        for text in self.AGREE:
            js_stringify_pretty(json.loads(text)).encode("utf-8")

    def test_refused_documents_raise_rather_than_diverge(self):
        for text in self.REFUSE:
            with self.assertRaises(JsSerializationError, msg=text):
                js_stringify_pretty(json.loads(text))

    def test_the_refused_numbers_really_would_have_diverged(self):
        # Pins the JUSTIFICATION, not just the behaviour: if a future Python or node
        # made these agree, this test fails and the precondition can be relaxed.
        node_or_skip(self)
        provable = [t for t in self.DIVERGENT if t != '{"stats": {"rate": 0.5}}']
        expected = js_stringify_many(provable)
        for text, want in zip(provable, expected):
            naive = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            self.assertNotEqual(naive, want, "%s no longer diverges" % text)


class TestDerivedDocumentsAgreeWithTheJsSerialization(unittest.TestCase):
    """issue #38 F1-persist-1/F4-4. The plan's `derive` block carries chars+checksum
    for the two documents THIS script writes, computed on the JS side from
    writerPayload(). The workflow compares them to `written[]` and treats a
    difference as a STRUCTURAL failure (unlike a primary mismatch, there is no
    on-disk truth to fall back to) — so the two runtimes have to agree byte for byte
    over the DERIVED documents, not only over the primaries this file already pins."""

    def js_expectation(self, document):
        """chars + checksum over node's own JSON.stringify(document, null, 2)."""
        text = js_stringify_many([json.dumps(document)])[0]
        return utf16_len(text), fnv1a32(text)

    def test_written_entries_match_what_node_would_have_computed(self):
        node_or_skip(self)
        findings = [finding("F1", description="日本語 😀 𝕏 prose"), finding("F2")]
        with _Workspace(findings=findings) as ws:
            receipt = json.loads(run_script(ws.write_plan(ws.plan())).stdout)
            self.assertTrue(receipt["ok"], receipt)
            written = {e["path"]: e for e in receipt["written"]}

            # post-review: the bare projected array (wrapper: null).
            chars, checksum = self.js_expectation(ws.findings)
            self.assertEqual(written[ws.post_path]["chars"], chars)
            self.assertEqual(written[ws.post_path]["checksum"], checksum)

            # checkpoint: the skeleton with the alias-stripped findings in place.
            skeleton = ws.plan()["checkpoint"]["skeleton"]
            skeleton["phases"]["challenge"]["findings"] = [
                dict(
                    (k, v)
                    for (k, v) in f.items()
                    if k not in ("line", "end_line", "body")
                )
                for f in ws.findings
            ]
            chars, checksum = self.js_expectation(skeleton)
            self.assertEqual(written[ws.checkpoint_path]["chars"], chars)
            self.assertEqual(written[ws.checkpoint_path]["checksum"], checksum)

    def test_the_written_numbers_describe_the_bytes_actually_on_disk(self):
        # The receipt reports the serialized text; write_text_atomic writes it
        # verbatim, so re-reading the file must reproduce the same proof.
        with _Workspace() as ws:
            receipt = json.loads(run_script(ws.write_plan(ws.plan())).stdout)
            for entry in receipt["written"]:
                on_disk = ws.read(entry["path"])
                self.assertEqual(utf16_len(on_disk), entry["chars"])
                self.assertEqual(fnv1a32(on_disk), entry["checksum"])


class TestCheckpointSkeletonGuardMirrorsTheJsOne(unittest.TestCase):
    """issue #38 F1-persist-3. The JS side (persistPlan) empties
    `phases.challenge.findings` into the skeleton ONLY when it held an ARRAY:
    `challenge && Array.isArray(challenge.findings)`. A looser predicate here
    FABRICATES a findings array the pipeline never had — a derived document the
    two runtimes disagree about, which nothing downstream can cross-check."""

    def derive(self, ws, skeleton, challenge_ids=None):
        plan = ws.plan()
        plan["checkpoint"]["skeleton"] = skeleton
        if challenge_ids is not None:
            plan["checkpoint"]["challengeFindingIds"] = challenge_ids
        proc = run_script(ws.write_plan(plan))
        return proc, json.loads(proc.stdout)

    def test_a_challenge_object_with_no_findings_key_is_left_alone(self):
        # The pipeline held a challenge phase that carried no findings array, so the
        # derived checkpoint must carry none either.
        with _Workspace() as ws:
            proc, receipt = self.derive(
                ws,
                {"phases": {"challenge": {"stats": {}}}, "completed": []},
                challenge_ids=[],
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertTrue(receipt["ok"], receipt)
            cp = json.loads(ws.read(ws.checkpoint_path))
            self.assertNotIn(
                "findings",
                cp["phases"]["challenge"],
                "a findings array the pipeline never had must not be fabricated",
            )

    def test_a_non_array_findings_value_is_preserved_verbatim(self):
        with _Workspace() as ws:
            proc, receipt = self.derive(
                ws,
                {"phases": {"challenge": {"findings": "truncated"}}, "completed": []},
                challenge_ids=[],
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertTrue(receipt["ok"], receipt)
            cp = json.loads(ws.read(ws.checkpoint_path))
            self.assertEqual(cp["phases"]["challenge"]["findings"], "truncated")

    def test_challenge_ids_with_a_non_array_findings_slot_is_a_hard_failure(self):
        # There is nowhere honest to put them, so refuse rather than overwrite.
        with _Workspace() as ws:
            plan = ws.plan()
            plan["checkpoint"]["skeleton"] = {
                "phases": {"challenge": {"findings": None}},
                "completed": [],
            }
            self.assert_hard_failure_for(ws, plan, "challenge")

    def test_the_array_case_is_unchanged(self):
        with _Workspace() as ws:
            run_script(ws.write_plan(ws.plan()))
            cp = json.loads(ws.read(ws.checkpoint_path))
            self.assertEqual(len(cp["phases"]["challenge"]["findings"]), 2)

    def assert_hard_failure_for(self, ws, plan, needle):
        proc = run_script(ws.write_plan(plan))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        receipt = json.loads(proc.stdout)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["written"], [])
        self.assertTrue(
            any(needle in e for e in receipt["errors"]),
            "expected %r in %r" % (needle, receipt["errors"]),
        )
        self.assertFalse(os.path.exists(ws.checkpoint_path))


class TestStdoutIsNeverEmpty(unittest.TestCase):
    """issue #38 F1-persist-2. `assemble()` guarantees a receipt DICT on every
    path, but a dict is not yet a LINE. The unsupported-planVersion branch copies
    the plan's own `planVersion` into the receipt and returns BEFORE the
    number-spelling precondition ever runs — and `json.loads` accepts a bare `NaN`,
    which `allow_nan=False` then refuses to spell. That raised out of main() as a
    traceback with EMPTY stdout: indistinguishable to the executor from a dead
    script, which silently costs the run its artifacts at the very last hop."""

    def test_a_NaN_planVersion_still_yields_exactly_one_receipt_line(self):
        with _Workspace() as ws:
            # A bare NaN is legal to json.loads and reaches the receipt verbatim.
            ws.write(ws.plan_path, '{"planVersion": NaN}')
            proc = run_script(ws.plan_path)

            self.assertNotEqual(proc.returncode, 0)
            self.assertNotEqual(proc.stdout.strip(), "", "stdout must NEVER be empty")
            self.assertEqual(len(proc.stdout.strip().split("\n")), 1)
            receipt = json.loads(proc.stdout)
            self.assertFalse(receipt["ok"])
            self.assertTrue(receipt["errors"])
            self.assertNotIn("Traceback", proc.stderr)

    def test_an_Infinity_planVersion_is_handled_the_same_way(self):
        with _Workspace() as ws:
            ws.write(ws.plan_path, '{"planVersion": Infinity}')
            proc = run_script(ws.plan_path)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(json.loads(proc.stdout)["ok"])

    def test_the_minimal_line_is_well_formed_and_self_describing(self):
        from scripts.assemble_artifacts import _minimal_receipt_line

        receipt = json.loads(_minimal_receipt_line(ValueError("out of range float")))
        self.assertEqual(receipt["ok"], False)
        self.assertEqual(receipt["written"], [])
        self.assertEqual(receipt["verified"], [])
        self.assertTrue(any("out of range float" in e for e in receipt["errors"]))
        self.assertEqual(
            sorted(receipt.keys()),
            ["errors", "ok", "planChecksum", "planVersion", "verified", "written"],
            "the fallback keeps the receipt's own shape so the caller needs no new branch",
        )

    def test_the_minimal_line_survives_an_exception_it_cannot_render(self):
        from scripts.assemble_artifacts import _minimal_receipt_line

        class Hostile(Exception):
            def __str__(self):
                raise RuntimeError("even str() fails here")

        line = _minimal_receipt_line(Hostile())
        self.assertNotEqual(line.strip(), "")
        self.assertFalse(json.loads(line)["ok"])


class TestAssembleAsALibrary(unittest.TestCase):
    """assemble() is the unit seam; main() only prints its receipt."""

    def test_returns_the_receipt_dict(self):
        with _Workspace() as ws:
            receipt = assemble(ws.write_plan(ws.plan()))
            self.assertTrue(receipt["ok"])
            self.assertEqual(
                sorted(receipt.keys()),
                ["errors", "ok", "planChecksum", "planVersion", "verified", "written"],
            )


if __name__ == "__main__":
    unittest.main()
