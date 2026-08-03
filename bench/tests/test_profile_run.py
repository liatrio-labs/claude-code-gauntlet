"""
Tests for bench/profile_run.py

Builds a small synthetic on-disk fixture that mirrors the real
``~/.claude/projects/<project>/<sessionId>/{workflows,subagents}/...`` layout (record
JSON + per-agent transcript jsonl + orchestrator session jsonl) and exercises the
profiler against it end to end. Deliberately does NOT depend on any real transcript
data, which lives outside this repo.

Covers:
  - run discovery (explicit run_id, and default-to-most-recent-completed)
  - stage grouping (summarize / discover / verify-input-writer / verify-slice /
    validate-batch / challenge / report-writer / artifact-writer) + the merge/filter
    transform-gap placeholders
  - concurrency accounting (avg + max overlap) for a 2-wide fan-out stage
  - parallel-capacity accounting (slowest x slots vs used vs idle)
  - critical-path hop selection (slowest member of each fan-out stage)
  - model-generation vs tool-execution split from tool_use/tool_result deltas
  - output-byte accounting for a Write-shaped agent
  - orchestrator phase spans + Phase 2 Bash-call latency breakdown
  - reconciliation against the record's own headline totals
  - graceful "unavailable" degradation when a subagent transcript is missing
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import profile_run as pr


def iso(ms):
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )[:-4]
        + "Z"
    )


class SyntheticRunBuilder:
    """Writes a minimal but complete synthetic run under a TemporaryDirectory.

    Timeline (epoch ms), T0 = 1_800_000_000_000:
      T0 +    0  workflow start
      T0 +  100  summarize starts,   duration 1000  -> ends T0+1100
      T0 + 1200  discover-a starts,  duration 3000  -> ends T0+4200  (slowest)
      T0 + 1200  discover-b starts,  duration 1000  -> ends T0+2200
      T0 + 4300  verify-input-writer-0 starts, duration  500 -> ends T0+4800
      T0 + 4900  verify-slice-0 starts,        duration  400 -> ends T0+5300
      T0 + 5400  validate-batch-0 starts,      duration  600 -> ends T0+6000
      T0 + 6100  challenge-0 starts, duration  800 -> ends T0+6900 (slowest)
      T0 + 6100  challenge-1 starts, duration  300 -> ends T0+6400
      T0 + 7000  report-writer starts, duration  400 -> ends T0+7400
      T0 + 7500  artifact-writer starts, duration  700 -> ends T0+8200
      workflow durationMs = 8300 (a little slack after artifact-writer, like the real run)
    """

    T0: int = 1_800_000_000_000

    def __init__(self, root: Path, run_id="wf_test0001-1", task_id="ttest0001"):
        self.root = root
        self.run_id = run_id
        self.task_id = task_id
        self.project_dir = root / "-fake-project"
        self.session_id = f"sess-{run_id}"
        self.session_dir = self.project_dir / self.session_id
        self.workflows_dir = self.session_dir / "workflows"
        self.subagents_dir = self.session_dir / "subagents" / "workflows" / self.run_id
        self.workflows_dir.mkdir(parents=True)
        self.subagents_dir.mkdir(parents=True)

        self.agents_spec = [
            ("summarize", "code-gauntlet:change-summarizer", 100, 1000, "a-summarize"),
            (
                "code-gauntlet:bug-detector",
                "code-gauntlet:bug-detector",
                1200,
                3000,
                "a-discover-a",
            ),
            (
                "code-gauntlet:security-reviewer",
                "code-gauntlet:security-reviewer",
                1200,
                1000,
                "a-discover-b",
            ),
            (
                "verify-input-writer-0",
                "code-gauntlet:artifact-writer",
                4300,
                500,
                "a-viw",
            ),
            ("verify-slice-0", "code-gauntlet:executor", 4900, 400, "a-vslice"),
            ("validate-batch-0", "code-gauntlet:validator", 5400, 600, "a-vbatch"),
            ("challenge-0", "code-gauntlet:challenger", 6100, 800, "a-chal0"),
            ("challenge-1", "code-gauntlet:challenger", 6100, 300, "a-chal1"),
            ("report-writer", "code-gauntlet:report-writer", 7000, 400, "a-report"),
            (
                "artifact-writer",
                "code-gauntlet:artifact-writer",
                7500,
                700,
                "a-artifact",
            ),
        ]
        self.workflow_duration_ms = 8300

    def _write_json(self, path, obj):
        path.write_text(json.dumps(obj), encoding="utf-8")

    def _write_jsonl(self, path, rows):
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def build(self):
        workflow_progress = []
        total_tokens = 0
        total_tool_calls = 0
        for idx, (label, atype, start_off, dur, agent_id) in enumerate(
            self.agents_spec, 1
        ):
            started_at = self.T0 + start_off
            queued_at = started_at - 50  # fixed 50ms dispatch latency
            tokens = 100 * idx
            tool_calls = 2
            total_tokens += tokens
            total_tool_calls += tool_calls
            workflow_progress.append(
                {
                    "type": "workflow_agent",
                    "index": idx,
                    "label": label,
                    "agentId": agent_id,
                    "agentType": atype,
                    "model": "claude-sonnet-5",
                    "state": "done",
                    "startedAt": started_at,
                    "queuedAt": queued_at,
                    "attempt": 1,
                    "lastProgressAt": started_at + dur,
                    "tokens": tokens,
                    "toolCalls": tool_calls,
                    "durationMs": dur,
                }
            )
            self._write_agent_transcript(agent_id, started_at, dur, label)

        record = {
            "runId": self.run_id,
            "timestamp": iso(self.T0 + self.workflow_duration_ms),
            "taskId": self.task_id,
            "script": "// fake bundle //",
            "scriptPath": "/fake/workflows/pipeline.js",
            "args": {"argsVersion": 1},
            "result": {"ok": True},
            "agentCount": len(workflow_progress),
            "logs": [],
            "durationMs": self.workflow_duration_ms,
            "summary": None,
            "workflowName": "code-gauntlet",
            "status": "completed",
            "startTime": self.T0,
            "defaultModel": "claude-sonnet-5",
            "workflowProgress": workflow_progress,
            "totalTokens": total_tokens,
            "totalToolCalls": total_tool_calls,
        }
        self._write_json(self.workflows_dir / f"{self.run_id}.json", record)
        self._write_session_transcript()
        return record

    def _write_agent_transcript(self, agent_id, started_at, dur, label):
        """Two tool_use/tool_result pairs per agent; for artifact-writer add a Write call.

        Span == dur exactly: first event at started_at, last at started_at+dur.
        Tool time is a fixed, known fraction so generation time is exactly derivable.
        """
        rows = []
        prompt_ts = started_at
        rows.append(
            {
                "type": "user",
                "message": {"role": "user", "content": "go"},
                "timestamp": iso(prompt_ts),
            }
        )

        tool1_use_ts = started_at + int(dur * 0.2)
        tool1_result_ts = started_at + int(dur * 0.4)
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"{agent_id}-t1",
                            "name": "Read",
                            "input": {"file_path": "/x"},
                        }
                    ]
                },
                "timestamp": iso(tool1_use_ts),
            }
        )
        rows.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"{agent_id}-t1",
                            "content": "ok",
                        }
                    ]
                },
                "timestamp": iso(tool1_result_ts),
            }
        )

        if label in ("verify-input-writer-0", "artifact-writer"):
            write_use_ts = started_at + int(dur * 0.6)
            write_result_ts = started_at + int(dur * 0.7)
            write_content = "x" * 250  # known byte length
            rows.append(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"{agent_id}-write",
                                "name": "Write",
                                "input": {
                                    "file_path": f"/fake/{label}.json",
                                    "content": write_content,
                                },
                            }
                        ]
                    },
                    "timestamp": iso(write_use_ts),
                }
            )
            rows.append(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"{agent_id}-write",
                                "content": "wrote",
                            }
                        ]
                    },
                    "timestamp": iso(write_result_ts),
                }
            )

        tool2_use_ts = started_at + int(dur * 0.8)
        tool2_result_ts = started_at + int(dur * 0.9)
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"{agent_id}-t2",
                            "name": "StructuredOutput",
                            "input": {},
                        }
                    ]
                },
                "timestamp": iso(tool2_use_ts),
            }
        )
        rows.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"{agent_id}-t2",
                            "content": "done",
                        }
                    ]
                },
                "timestamp": iso(tool2_result_ts),
            }
        )
        # Final event exactly at started_at + dur to pin the transcript span to `dur`.
        rows.append(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "bye"}]},
                "timestamp": iso(started_at + dur),
            }
        )

        self._write_jsonl(self.subagents_dir / f"agent-{agent_id}.jsonl", rows)
        (self.subagents_dir / f"agent-{agent_id}.meta.json").write_text(
            json.dumps(
                {"agentType": label, "spawnDepth": 1, "model": "claude-sonnet-5"}
            ),
            encoding="utf-8",
        )

    def _write_session_transcript(self):
        """Orchestrator session: Phase1 -> AskUserQuestion -> human wait -> Phase2 Bash calls
        -> Workflow launch (matching this run's taskId) -> Phase3 wait -> resume event."""
        rows = []
        session_start = (
            self.T0 - 60_000
        )  # session started 60s before the workflow itself

        rows.append(
            {
                "type": "user",
                "message": {"role": "user", "content": "/code-gauntlet run"},
                "timestamp": iso(session_start),
            }
        )

        # First *tool* activity (a plain-text user message, like the slash-command
        # invocation above, carries no tool_use/tool_result and so is invisible to the
        # phase-boundary logic, which anchors on tool events only).
        preflight_read_ts = session_start + 2_000
        preflight_result_ts = session_start + 2_100
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "read-1",
                            "name": "Read",
                            "input": {"file_path": "/fake/SKILL.md"},
                        }
                    ]
                },
                "timestamp": iso(preflight_read_ts),
            }
        )
        rows.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "read-1",
                            "content": "skill body",
                        }
                    ]
                },
                "timestamp": iso(preflight_result_ts),
            }
        )

        ask_use_ts = session_start + 5_000
        ask_result_ts = session_start + 20_000  # 15s "human" wait
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "ask-1",
                            "name": "AskUserQuestion",
                            "input": {},
                        }
                    ]
                },
                "timestamp": iso(ask_use_ts),
            }
        )
        rows.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "ask-1",
                            "content": "answered",
                        }
                    ]
                },
                "timestamp": iso(ask_result_ts),
            }
        )

        # Two sequential Bash calls in Phase 2.
        prev_end = ask_result_ts
        bash_specs = [("checkout", 2_000, 500), ("write context", 1_000, 3_000)]
        for i, (desc, model_latency, shell_time) in enumerate(bash_specs, 1):
            use_ts = prev_end + model_latency
            result_ts = use_ts + shell_time
            rows.append(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"bash-{i}",
                                "name": "Bash",
                                "input": {"description": desc},
                            }
                        ]
                    },
                    "timestamp": iso(use_ts),
                }
            )
            rows.append(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"bash-{i}",
                                "content": "ok",
                            }
                        ]
                    },
                    "timestamp": iso(result_ts),
                }
            )
            prev_end = result_ts

        workflow_launch_ts = prev_end + 500
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "wf-1",
                            "name": "Workflow",
                            "input": {"scriptPath": "/fake/pipeline.js"},
                        }
                    ]
                },
                "timestamp": iso(workflow_launch_ts),
            }
        )
        rows.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "wf-1",
                            "content": f"Workflow launched in background. Task ID: {self.task_id}\nSummary: fake",
                        }
                    ]
                },
                "timestamp": iso(workflow_launch_ts + 100),
            }
        )

        # Resume event after the recorded workflow's own completion (start + durationMs).
        # Phase-boundary detection only looks at tool_use/tool_result content blocks (a
        # plain-text message, like the human-facing narration in a real transcript, is
        # invisible to it -- same reasoning as the Phase 1 anchor above), so the resume
        # marker itself must be a tool call.
        workflow_end_ms = self.T0 + self.workflow_duration_ms
        resume_ts = workflow_end_ms + 2_000
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "read-2",
                            "name": "Read",
                            "input": {"file_path": "/fake/report.md"},
                        }
                    ]
                },
                "timestamp": iso(resume_ts),
            }
        )
        rows.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "read-2",
                            "content": "report body",
                        }
                    ]
                },
                "timestamp": iso(resume_ts + 50),
            }
        )

        session_end_ts = resume_ts + 10_000
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "bash-post",
                            "name": "Bash",
                            "input": {"description": "post review"},
                        }
                    ]
                },
                "timestamp": iso(session_end_ts),
            }
        )
        rows.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "bash-post",
                            "content": "posted",
                        }
                    ]
                },
                "timestamp": iso(session_end_ts + 50),
            }
        )

        self._write_jsonl(self.project_dir / f"{self.session_id}.jsonl", rows)


class ProfileRunTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.builder = SyntheticRunBuilder(self.root)
        self.record = self.builder.build()

    def tearDown(self):
        self._tmp.cleanup()

    # -- discovery -----------------------------------------------------------

    def test_find_run_record_explicit_id(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        self.assertEqual(record["runId"], self.builder.run_id)
        self.assertEqual(session_dir, self.builder.session_dir)

    def test_find_run_record_defaults_to_most_recent_completed(self):
        # Add a second, earlier-timestamped run; the default pick must still be ours.
        older = SyntheticRunBuilder(
            self.root, run_id="wf_older0001-1", task_id="told0001"
        )
        older.T0 = self.builder.T0 - 1_000_000
        older.build()
        record, record_path, _ = pr.find_run_record(self.root, None)
        self.assertEqual(record["runId"], self.builder.run_id)

    def test_missing_run_id_raises(self):
        with self.assertRaises(FileNotFoundError):
            pr.find_run_record(self.root, "wf_does-not-exist-1")

    # -- stage grouping / concurrency -----------------------------------------

    def test_stage_grouping_covers_all_agents_and_transforms(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        stage_names = [s["stage"] for s in profile["stage_profile"]]
        for expected in (
            "summarize",
            "discover",
            "merge (transform, no agent)",
            "verify-input-writer",
            "verify-slice",
            "validate-batch",
            "filter (transform, no agent)",
            "challenge",
            "report-writer",
            "artifact-writer",
        ):
            self.assertIn(expected, stage_names)

    def test_discover_stage_concurrency(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        discover = next(s for s in profile["stage_profile"] if s["stage"] == "discover")
        self.assertEqual(discover["agent_count"], 2)
        # discover-a: [1200,4200), discover-b: [1200,2200) -> full overlap for 1000ms, so max concurrency 2.
        self.assertEqual(discover["max_concurrency"], 2)
        # span = 4200-1200 = 3000ms; busy = 3000+1000=4000ms -> avg concurrency 4000/3000.
        self.assertAlmostEqual(discover["avg_concurrency"], 4000 / 3000, places=6)
        self.assertAlmostEqual(discover["span_wall_s"], 3.0, places=6)

    def test_merge_transform_gap_span(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        merge = next(
            s
            for s in profile["stage_profile"]
            if s["stage"] == "merge (transform, no agent)"
        )
        # discover ends at T0+4200 (offset 4.2s), verify-input-writer starts at T0+4300 (offset 4.3s).
        self.assertAlmostEqual(merge["span_start_offset_s"], 4.2, places=6)
        self.assertAlmostEqual(merge["span_end_offset_s"], 4.3, places=6)
        self.assertAlmostEqual(merge["span_wall_s"], 0.1, places=6)

    # -- capacity accounting ---------------------------------------------------

    def test_capacity_accounting_discover(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        cap = next(
            c for c in profile["capacity_accounting"] if c["stage"] == "discover"
        )
        self.assertEqual(cap["slots"], 2)
        self.assertEqual(cap["slowest_agent_ms"], 3000)
        self.assertAlmostEqual(
            cap["capacity_agent_seconds"], 6.0, places=6
        )  # 3000ms * 2 slots
        self.assertAlmostEqual(cap["agent_seconds_used"], 4.0, places=6)  # 3000+1000 ms
        self.assertAlmostEqual(cap["idle_agent_seconds"], 2.0, places=6)
        self.assertAlmostEqual(cap["idle_pct"], (2.0 / 6.0) * 100, places=6)

    # -- critical path ---------------------------------------------------------

    def test_critical_path_picks_slowest_member_per_stage(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        hops = {h["stage"]: h for h in profile["critical_path"]["hops"]}
        self.assertEqual(
            hops["discover"]["critical_agent_label"], "code-gauntlet:bug-detector"
        )
        self.assertEqual(hops["challenge"]["critical_agent_label"], "challenge-0")
        # Every agent-bearing stage should appear as a hop.
        self.assertEqual(
            set(hops.keys()),
            {
                "summarize",
                "discover",
                "verify-input-writer",
                "verify-slice",
                "validate-batch",
                "challenge",
                "report-writer",
                "artifact-writer",
            },
        )

    # -- generation vs tool time -------------------------------------------------

    def test_generation_vs_tool_time_split(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        summarize_agent = next(
            a for a in profile["agents"] if a["label"] == "summarize"
        )
        t = summarize_agent["transcript"]
        # dur=1000ms; tool1 at [0.2,0.4]*dur = 200ms; tool2 at [0.8,0.9]*dur = 100ms -> tool_time=300ms.
        self.assertAlmostEqual(t["tool_time_ms"], 300, delta=1)
        self.assertAlmostEqual(t["span_ms"], 1000, delta=1)
        self.assertAlmostEqual(t["generation_time_ms"], 700, delta=1)
        self.assertEqual(t["matched_tool_calls"], 2)
        self.assertEqual(t["unmatched_tool_use"], 0)

    # -- output-byte accounting ---------------------------------------------------

    def test_output_byte_accounting_for_write_agents(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        artifact_writer = next(
            a for a in profile["agents"] if a["label"] == "artifact-writer"
        )
        writes = artifact_writer["transcript"]["writes"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["content_bytes"], 250)
        self.assertEqual(writes[0]["file_path"], "/fake/artifact-writer.json")

    # -- orchestrator phases -----------------------------------------------------

    def test_orchestrator_phase_spans(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        op = profile["orchestrator_phases"]
        # Phase 1 is measured from the first tool_use/tool_result event onward (a plain-text
        # message carries no tool activity and is invisible to this heuristic) through the
        # AskUserQuestion gate: (session_start+5000) - (session_start+2000) = 3.0s.
        self.assertAlmostEqual(op["phase1_preflight_s"], 3.0, places=3)
        self.assertAlmostEqual(op["human_wait_after_phase1_s"], 15.0, places=3)
        self.assertEqual(op["phase2_bash_call_count"], 2)
        self.assertAlmostEqual(
            op["phase2_total_model_latency_s"], 3.0, places=3
        )  # 2s + 1s
        self.assertAlmostEqual(
            op["phase2_total_shell_time_s"], 3.5, places=3
        )  # 0.5s + 3.0s
        self.assertAlmostEqual(op["phase3_dispatch_to_resume_latency_s"], 2.0, places=3)

    # -- reconciliation ------------------------------------------------------------

    def test_reconciliation_matches_record_totals(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        r = profile["reconciliation"]
        self.assertTrue(r["agent_count"]["match"])
        self.assertTrue(r["total_tokens"]["match"])
        self.assertTrue(r["total_tool_calls"]["match"])

    # -- graceful degradation ---------------------------------------------------------

    def test_missing_subagent_dir_marks_unavailable_not_crash(self):
        import shutil

        shutil.rmtree(self.builder.subagents_dir)
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        self.assertTrue(any(pr.UNAVAILABLE in n for n in profile["notes"]))
        for a in profile["agents"]:
            self.assertIsNone(a["transcript"])
        # Markdown rendering must still succeed (no crash on missing transcript data).
        md = pr.render_markdown(profile)
        self.assertIn("UNAVAILABLE", md)

    def test_missing_duration_ms_reports_unavailable_not_zero(self):
        # If the workflow record is missing durationMs, the header line must show the
        # UNAVAILABLE marker, never a fabricated "0.0s" (regression for a guessed-value bug).
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        profile["duration_ms"] = None
        md = pr.render_markdown(profile)
        duration_line = next(
            line for line in md.splitlines() if line.startswith("- start:")
        )
        self.assertIn(f"duration: {pr.UNAVAILABLE}", duration_line)
        self.assertNotIn("0.0s", duration_line)

    def test_render_markdown_smoke(self):
        record, record_path, session_dir = pr.find_run_record(
            self.root, self.builder.run_id
        )
        profile = pr.build_profile(
            record, record_path, session_dir, self.builder.run_id, self.root
        )
        md = pr.render_markdown(profile)
        self.assertIn("# Workflow profile", md)
        self.assertIn("## Critical path", md)
        self.assertIn("## Orchestrator phase spans", md)

    # -- CLI end-to-end ------------------------------------------------------------

    def test_main_writes_json_and_markdown(self):
        out_json = self.root / "out.json"
        out_md = self.root / "out.md"
        rc = pr.main(
            [
                self.builder.run_id,
                "--projects-dir",
                str(self.root),
                "--out-json",
                str(out_json),
                "--out-md",
                str(out_md),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out_json.exists())
        self.assertTrue(out_md.exists())
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(data["run_id"], self.builder.run_id)


if __name__ == "__main__":
    unittest.main()
