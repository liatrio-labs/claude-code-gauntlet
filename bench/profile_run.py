#!/usr/bin/env python3
"""bench/profile_run.py — repeatable profiler for a recorded code-gauntlet workflow run.

Given a workflow run id (``wf_<hex>-<n>``), reconstructs a timing/cost profile of that
run from three on-disk sources under ``~/.claude/projects/<project>/``:

  1. the workflow record JSON (``workflows/wf_<runId>.json``) — ``workflowProgress[]``
     per-agent summary rows (label, agentType, model, timings, tokens, toolCalls);
  2. the per-agent subagent transcripts (``subagents/workflows/<runId>/agent-<id>.jsonl``)
     — per-message timestamps and ``tool_use``/``tool_result`` pairs, which is how
     model-generation time vs tool-execution time gets split;
  3. the orchestrator session transcript (``<projectDir>/<sessionId>.jsonl``, the
     sibling of the ``<sessionId>/`` folder that holds the two directories above) —
     Phase 1/2/3-wait/8 spans and Phase 2's sequential Bash-call latency.

It emits BOTH a machine-readable JSON profile and a human markdown report covering:
  - per-stage wall-clock span / share / agent count / concurrency
  - a per-agent table (label, agentType, model, attempt, durationMs, tokens, toolCalls,
    dispatch latency, start/end offsets)
  - parallel-capacity accounting for each fan-out stage (slowest-agent x slots vs used)
  - the critical path through the stage graph (compute vs dispatch vs orchestration-gap)
  - model-generation vs tool-execution time, per agent and in aggregate
  - output-byte accounting for Write-shaped agents (artifact-writer, verify-input-writer)
  - orchestrator phase spans + Phase 2 Bash-call latency breakdown

Stdlib-only (repo CLAUDE.md). Never guesses when data is missing — prints an explicit
"unavailable" marker (``UNAVAILABLE`` sentinel / ``notes`` list) instead.

Usage::

    python3 bench/profile_run.py [RUN_ID] [--projects-dir PATH]
                                  [--out-json PATH] [--out-md PATH]

With no RUN_ID, the most recently *completed* ``code-gauntlet`` workflow record under
``--projects-dir`` (default ``~/.claude/projects``) is used.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UNAVAILABLE = "UNAVAILABLE"

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Discovery agentTypes that make up the "discover" stage (CLAUDE.md's 7-agent list,
# minus change-summarizer/artifact-writer/executor/validator/challenger/report-writer
# which have their own stages).
DISCOVER_AGENT_TYPES = {
    "code-gauntlet:bug-detector",
    "code-gauntlet:security-reviewer",
    "code-gauntlet:cross-file-impact",
    "code-gauntlet:test-analyzer",
    "code-gauntlet:conventions-and-intent",
    "code-gauntlet:type-design-analyzer",
    "code-gauntlet:code-simplifier",
}

# Ordered stage-grouping rules: (stage_name, predicate(label, agentType)). Order matters
# for the pipeline-order stage list; first match wins.
def _stage_rules():
    return [
        ("summarize", lambda label, _atype: label == "summarize"),
        ("discover", lambda _label, atype: atype in DISCOVER_AGENT_TYPES),
        ("verify-input-writer", lambda label, _atype: label.startswith("verify-input-writer-")),
        ("verify-slice", lambda label, _atype: label.startswith("verify-slice-")),
        ("validate-batch", lambda label, _atype: label.startswith("validate-batch-")),
        ("challenge", lambda label, _atype: label.startswith("challenge-")),
        ("report-writer", lambda label, _atype: label == "report-writer"),
        ("artifact-writer", lambda label, _atype: label == "artifact-writer"),
    ]


# Non-agent transform phases that sit between agent stages in the pipeline (workflows/src/stages.js
# runPhase('merge', ...) / runPhase('filter', ...)) — no agent dispatch, so they can only be
# observed as the orchestration gap between the previous stage's last completion and the next
# stage's first dispatch.
TRANSFORM_STAGE_AFTER = {
    "discover": "merge",
    "validate-batch": "filter",
}

STAGE_ORDER = [name for name, _ in _stage_rules()]


# --------------------------------------------------------------------------- helpers

def iso_to_ms(ts):
    """Parse an ISO8601 'Z' timestamp (as found in transcript jsonl files) to epoch ms."""
    if ts is None:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp() * 1000.0


def ms_to_iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def read_jsonl(path):
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# --------------------------------------------------------------------------- discovery

def find_run_record(projects_dir: Path, run_id: str | None):
    """Locate the workflow record JSON. Returns (record_dict, record_path, session_dir).

    session_dir is the ``<project>/<sessionId>`` folder that contains both
    ``workflows/`` and ``subagents/workflows/<runId>/``.
    """
    # Layout: <projects_dir>/<project-dir>/<sessionId>/workflows/wf_<runId>.json
    candidates = list(projects_dir.glob("*/*/workflows/wf_*.json"))
    if not candidates:
        raise FileNotFoundError(f"no workflow records found under {projects_dir}/*/*/workflows/")

    if run_id:
        matches = [p for p in candidates if p.stem == run_id or p.name == f"{run_id}.json"]
        if not matches:
            raise FileNotFoundError(f"no workflow record named {run_id!r} under {projects_dir}")
        record_path = matches[0]
    else:
        # Default: most recently completed code-gauntlet run, by the record's own
        # 'timestamp' field (falls back to file mtime if a record is unparseable).
        def sort_key(p):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if d.get("workflowName") == "code-gauntlet" and d.get("status") == "completed":
                    ts = d.get("timestamp")
                    if ts:
                        return (1, iso_to_ms(ts))
            except (json.JSONDecodeError, OSError, ValueError):
                pass
            return (0, p.stat().st_mtime * 1000.0)

        record_path = max(candidates, key=sort_key)

    record = json.loads(record_path.read_text(encoding="utf-8"))
    session_dir = record_path.parent.parent  # .../<sessionId>/workflows/x.json -> <sessionId>/
    return record, record_path, session_dir


def find_session_transcript(session_dir: Path):
    """The orchestrator session transcript sits one level up, named <sessionId>.jsonl."""
    session_id = session_dir.name
    path = session_dir.parent / f"{session_id}.jsonl"
    return path if path.exists() else None


def find_subagent_dir(session_dir: Path, run_id: str):
    d = session_dir / "subagents" / "workflows" / run_id
    return d if d.is_dir() else None


# --------------------------------------------------------------------------- per-agent transcript analysis

def analyze_agent_transcript(path: Path):
    """Return per-agent generation/tool split + output-byte accounting from one .jsonl file.

    tool_use -> tool_result deltas (matched by tool_use_id) are tool-execution time;
    everything else within the transcript's own first/last timestamp span is generation
    time (per issue-38 requirement 6's own definition of the split).
    """
    rows = read_jsonl(path)
    result = {
        "transcript_path": str(path),
        "message_count": len(rows),
        "first_ts": None,
        "last_ts": None,
        "span_ms": None,
        "tool_time_ms": 0.0,
        "generation_time_ms": None,
        "matched_tool_calls": 0,
        "unmatched_tool_use": 0,
        "unmatched_tool_result": 0,
        "tool_calls_detail": [],  # {name, tool_use_id, start_ms, end_ms, duration_ms}
        "writes": [],  # {file_path, content_bytes, duration_ms} for Write tool calls
    }
    if not rows:
        result["note"] = UNAVAILABLE + ": empty or missing transcript"
        return result

    tool_uses = {}  # id -> {name, ts_ms, input}
    tool_result_ts = {}  # id -> ts_ms
    all_ts = []

    for row in rows:
        ts = row.get("timestamp")
        ts_ms = iso_to_ms(ts) if ts else None
        if ts_ms is not None:
            all_ts.append(ts_ms)
        msg = row.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype == "tool_use" and ts_ms is not None:
                tool_uses[c.get("id")] = {"name": c.get("name"), "ts_ms": ts_ms, "input": c.get("input") or {}}
            elif ctype == "tool_result" and ts_ms is not None:
                tid = c.get("tool_use_id")
                if tid is not None:
                    tool_result_ts[tid] = ts_ms

    if all_ts:
        result["first_ts"] = ms_to_iso(min(all_ts))
        result["last_ts"] = ms_to_iso(max(all_ts))
        result["span_ms"] = max(all_ts) - min(all_ts)

    tool_time_ms = 0.0
    matched = 0
    for tid, tu in tool_uses.items():
        tr_ms = tool_result_ts.get(tid)
        if tr_ms is None:
            result["unmatched_tool_use"] += 1
            continue
        dur = tr_ms - tu["ts_ms"]
        if dur < 0:
            continue  # out-of-order clock artifact; do not count negative tool time
        tool_time_ms += dur
        matched += 1
        detail = {
            "name": tu["name"],
            "tool_use_id": tid,
            "start_ms": tu["ts_ms"],
            "end_ms": tr_ms,
            "duration_ms": dur,
        }
        result["tool_calls_detail"].append(detail)
        if tu["name"] == "Write":
            inp = tu["input"] or {}
            content_str = inp.get("content")
            byte_len = len(content_str.encode("utf-8")) if isinstance(content_str, str) else None
            result["writes"].append(
                {
                    "file_path": inp.get("file_path"),
                    "content_bytes": byte_len,
                    "duration_ms": dur,
                }
            )

    result["unmatched_tool_result"] = len([tid for tid in tool_result_ts if tid not in tool_uses])
    result["tool_time_ms"] = tool_time_ms
    result["matched_tool_calls"] = matched
    if result["span_ms"] is not None:
        result["generation_time_ms"] = max(result["span_ms"] - tool_time_ms, 0.0)
    result["tool_calls_detail"].sort(key=lambda d: d["start_ms"])
    return result


# --------------------------------------------------------------------------- stage grouping

def classify_stage(label, agent_type):
    for name, pred in _stage_rules():
        if pred(label, agent_type):
            return name
    return "other"


def build_stage_profile(agents, workflow_start_ms, workflow_duration_ms):
    by_stage = {}
    for a in agents:
        stage = a["stage"]
        by_stage.setdefault(stage, []).append(a)

    stages_out = []
    for name in STAGE_ORDER:
        members = by_stage.get(name, [])
        if not members:
            stages_out.append(
                {
                    "stage": name,
                    "agent_count": 0,
                    "note": UNAVAILABLE + ": no agents dispatched under this label in this run",
                }
            )
            continue
        starts = [m["started_at"] for m in members if m["started_at"] is not None]
        ends = [
            m["started_at"] + m["duration_ms"]
            for m in members
            if m["started_at"] is not None and m["duration_ms"] is not None
        ]
        if not starts or not ends:
            stages_out.append(
                {"stage": name, "agent_count": len(members), "note": UNAVAILABLE + ": missing timing fields"}
            )
            continue
        span_start, span_end = min(starts), max(ends)
        span_ms = span_end - span_start
        busy_ms = sum(m["duration_ms"] for m in members if m["duration_ms"] is not None)
        avg_concurrency = (busy_ms / span_ms) if span_ms > 0 else None
        max_concurrency = _max_overlap(
            [(m["started_at"], m["started_at"] + m["duration_ms"]) for m in members if m["started_at"] is not None and m["duration_ms"] is not None]
        )
        stages_out.append(
            {
                "stage": name,
                "agent_count": len(members),
                "span_start_offset_s": (span_start - workflow_start_ms) / 1000.0,
                "span_end_offset_s": (span_end - workflow_start_ms) / 1000.0,
                "span_wall_s": span_ms / 1000.0,
                "share_of_workflow_wall": (span_ms / workflow_duration_ms) if workflow_duration_ms else None,
                "agent_seconds_used": busy_ms / 1000.0,
                "avg_concurrency": avg_concurrency,
                "max_concurrency": max_concurrency,
            }
        )
        transform_after = TRANSFORM_STAGE_AFTER.get(name)
        if transform_after:
            # Purely observational: the gap between this stage's last completion and
            # the *next* agent-bearing stage's first dispatch includes the named
            # transform phase (a pure-JS runPhase() call, no agent — see
            # workflows/src/stages.js) plus any orchestration overhead. We cannot
            # separate the two without instrumenting the sandbox itself.
            stages_out.append(
                {
                    "stage": f"{transform_after} (transform, no agent)",
                    "agent_count": 0,
                    "note": (
                        "gap-derived only: wall time between end of '" + name + "' and start of the "
                        "next agent stage; includes this pure-JS transform phase plus any "
                        "orchestration overhead, not separable from the recorded data"
                    ),
                }
            )
    return stages_out


def _max_overlap(intervals):
    if not intervals:
        return None
    events = []
    for s, e in intervals:
        events.append((s, 1))
        events.append((e, -1))
    events.sort(key=lambda x: (x[0], -x[1]))  # starts before ends at same timestamp
    cur = 0
    best = 0
    for _, delta in events:
        cur += delta
        best = max(best, cur)
    return best


def _fill_transform_gaps(stages_out):
    """Resolve the gap-derived transform stages' actual span now that all stage spans exist."""
    stage_span = {}
    for s in stages_out:
        if "span_start_offset_s" in s:
            stage_span[s["stage"]] = (s["span_start_offset_s"], s["span_end_offset_s"])

    order = [s["stage"] for s in stages_out]
    for idx, s in enumerate(stages_out):
        if "(transform, no agent)" not in s["stage"]:
            continue
        prev_stage = order[idx - 1] if idx > 0 else None
        next_stage = order[idx + 1] if idx + 1 < len(order) else None
        prev_end = stage_span.get(prev_stage, (None, None))[1]
        next_start = stage_span.get(next_stage, (None, None))[0]
        if prev_end is not None and next_start is not None:
            s["span_start_offset_s"] = prev_end
            s["span_end_offset_s"] = next_start
            s["span_wall_s"] = max(next_start - prev_end, 0.0)


# --------------------------------------------------------------------------- parallel-capacity accounting

def build_capacity_accounting(agents):
    by_stage = {}
    for a in agents:
        by_stage.setdefault(a["stage"], []).append(a)

    out = []
    for name in STAGE_ORDER:
        members = [m for m in by_stage.get(name, []) if m["duration_ms"] is not None]
        if not members:
            continue
        slots = len(members)
        slowest_ms = max(m["duration_ms"] for m in members)
        capacity_agent_s = (slowest_ms * slots) / 1000.0
        used_agent_s = sum(m["duration_ms"] for m in members) / 1000.0
        idle_agent_s = max(capacity_agent_s - used_agent_s, 0.0)
        idle_pct = (idle_agent_s / capacity_agent_s * 100.0) if capacity_agent_s > 0 else None
        out.append(
            {
                "stage": name,
                "slots": slots,
                "slowest_agent_ms": slowest_ms,
                "capacity_agent_seconds": capacity_agent_s,
                "agent_seconds_used": used_agent_s,
                "idle_agent_seconds": idle_agent_s,
                "idle_pct": idle_pct,
            }
        )
    return out


# --------------------------------------------------------------------------- critical path

def build_critical_path(agents, workflow_start_ms, workflow_end_ms):
    """Longest dependency chain through the (mostly linear) stage graph.

    Within a fan-out stage the critical member is the one that finishes last
    (its completion gates the next stage's dispatch). merge/filter transform
    phases are inserted as zero-agent "gap" hops between discover->verify and
    validate-batch->challenge.
    """
    by_stage = {}
    for a in agents:
        by_stage.setdefault(a["stage"], []).append(a)

    hops = []
    prev_end = workflow_start_ms
    for name in STAGE_ORDER:
        members = [m for m in by_stage.get(name, []) if m["started_at"] is not None and m["duration_ms"] is not None]
        if not members:
            continue
        critical = max(members, key=lambda m: m["started_at"] + m["duration_ms"])
        queued = critical.get("queued_at")
        started = critical["started_at"]
        ended = started + critical["duration_ms"]
        gap_before = max((queued if queued is not None else started) - prev_end, 0.0)
        dispatch_latency = max(started - queued, 0.0) if queued is not None else None
        hops.append(
            {
                "stage": name,
                "critical_agent_label": critical["label"],
                "gap_before_ms": gap_before,
                "dispatch_latency_ms": dispatch_latency,
                "compute_ms": critical["duration_ms"],
                "start_offset_s": (started - workflow_start_ms) / 1000.0,
                "end_offset_s": (ended - workflow_start_ms) / 1000.0,
            }
        )
        prev_end = ended

    total_gap_ms = sum(h["gap_before_ms"] for h in hops)
    total_dispatch_ms = sum(h["dispatch_latency_ms"] for h in hops if h["dispatch_latency_ms"] is not None)
    total_compute_ms = sum(h["compute_ms"] for h in hops)
    critical_path_span_ms = (hops[-1]["end_offset_s"] * 1000.0) if hops else None
    workflow_wall_ms = (workflow_end_ms - workflow_start_ms) if workflow_end_ms else None

    return {
        "hops": hops,
        "total_gap_ms": total_gap_ms,
        "total_dispatch_latency_ms": total_dispatch_ms,
        "total_compute_ms": total_compute_ms,
        "critical_path_end_offset_s": (critical_path_span_ms / 1000.0) if critical_path_span_ms is not None else None,
        "workflow_wall_s": (workflow_wall_ms / 1000.0) if workflow_wall_ms is not None else None,
    }


# --------------------------------------------------------------------------- orchestrator session phases

def analyze_orchestrator_phases(session_path: Path | None, workflow_start_ms, workflow_end_ms, run_task_id):
    if session_path is None or not session_path.exists():
        return {"note": UNAVAILABLE + ": orchestrator session transcript not found"}

    rows = read_jsonl(session_path)
    events = []  # {ts_ms, kind: 'tool_use'|'tool_result', name, id/tool_use_id, text}
    for row in rows:
        ts = row.get("timestamp")
        ts_ms = iso_to_ms(ts) if ts else None
        msg = row.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype == "tool_use" and ts_ms is not None:
                events.append({"ts_ms": ts_ms, "kind": "tool_use", "name": c.get("name"), "id": c.get("id"), "input": c.get("input") or {}})
            elif ctype == "tool_result" and ts_ms is not None:
                cont = c.get("content")
                text = cont if isinstance(cont, str) else json.dumps(cont) if cont is not None else ""
                events.append({"ts_ms": ts_ms, "kind": "tool_result", "tool_use_id": c.get("tool_use_id"), "text": text})

    events.sort(key=lambda e: e["ts_ms"])
    if not events:
        return {"note": UNAVAILABLE + ": no tool_use/tool_result events in session transcript"}

    session_start_ms = events[0]["ts_ms"]
    session_end_ms = events[-1]["ts_ms"]

    result: dict[str, Any] = {"session_path": str(session_path)}

    # Phase 1 ends at the first AskUserQuestion tool_use (the REVIEW.md/delivery gate).
    ask_events = [e for e in events if e["kind"] == "tool_use" and e["name"] == "AskUserQuestion"]
    phase1_end_ms = ask_events[0]["ts_ms"] if ask_events else None
    if phase1_end_ms is not None:
        result["phase1_preflight_s"] = (phase1_end_ms - session_start_ms) / 1000.0
    else:
        result["phase1_preflight_s"] = UNAVAILABLE + ": no AskUserQuestion tool_use found"

    # Human-wait 1: from that AskUserQuestion's tool_use to its matching tool_result.
    human_wait1_s = UNAVAILABLE + ": could not match AskUserQuestion tool_result"
    phase2_start_ms = None
    if ask_events:
        first_ask = ask_events[0]
        tr = next((e for e in events if e["kind"] == "tool_result" and e.get("tool_use_id") == first_ask["id"]), None)
        if tr:
            human_wait1_s = (tr["ts_ms"] - first_ask["ts_ms"]) / 1000.0
            phase2_start_ms = tr["ts_ms"]
    result["human_wait_after_phase1_s"] = human_wait1_s

    # Phase 2 ends at the Workflow tool_use whose tool_result reports the Task ID that
    # matches this run's own taskId (there may be more than one Workflow launch in the
    # transcript, e.g. a relaunch/resume; only the one that produced *this* run counts).
    workflow_launches = [e for e in events if e["kind"] == "tool_use" and e["name"] == "Workflow"]
    matched_launch = None
    for launch in workflow_launches:
        tr = next((e for e in events if e["kind"] == "tool_result" and e.get("tool_use_id") == launch["id"]), None)
        if tr and run_task_id and (f"Task ID: {run_task_id}" in tr["text"]):
            matched_launch = (launch, tr)
            break
    if len(workflow_launches) > 1:
        result["note_multiple_workflow_launches"] = (
            f"{len(workflow_launches)} Workflow tool_use calls found in this session; "
            "only the one matching this run's taskId was used for phase boundaries "
            "(see project_v3_workflow_backgrounding-style relaunch quirks)"
        )

    if matched_launch and phase2_start_ms is not None:
        launch_ts = matched_launch[0]["ts_ms"]
        result["phase2_checkout_and_context_s"] = (launch_ts - phase2_start_ms) / 1000.0

        # Phase 2 Bash-call accounting: sequential Bash tool_use/tool_result pairs between
        # phase2_start_ms and launch_ts.
        bash_calls = []
        prev_result_ts = phase2_start_ms
        for e in events:
            if e["kind"] != "tool_use" or e["name"] != "Bash" or not (phase2_start_ms <= e["ts_ms"] <= launch_ts):
                continue
            tr = next((r for r in events if r["kind"] == "tool_result" and r.get("tool_use_id") == e.get("id")), None)
            model_latency_ms = max(e["ts_ms"] - prev_result_ts, 0.0)
            shell_ms = (tr["ts_ms"] - e["ts_ms"]) if tr else None
            bash_calls.append(
                {
                    "description": (e.get("input") or {}).get("description"),
                    "model_latency_ms": model_latency_ms,
                    "shell_time_ms": shell_ms,
                }
            )
            if tr:
                prev_result_ts = tr["ts_ms"]
        result["phase2_bash_call_count"] = len(bash_calls)
        result["phase2_bash_calls"] = bash_calls
        result["phase2_total_model_latency_s"] = sum(b["model_latency_ms"] for b in bash_calls) / 1000.0
        result["phase2_total_shell_time_s"] = (
            sum(b["shell_time_ms"] for b in bash_calls if b["shell_time_ms"] is not None) / 1000.0
        )
    else:
        result["phase2_checkout_and_context_s"] = UNAVAILABLE + ": could not identify this run's Workflow launch in the session transcript"

    # Phase 3 wait: from workflow_start_ms (the run's own recorded start) to the first
    # orchestrator event timestamped at/after workflow_end_ms (the resume-on-completion).
    if workflow_start_ms is not None and workflow_end_ms is not None:
        resume_idx = bisect.bisect_left([e["ts_ms"] for e in events], workflow_end_ms)
        if resume_idx < len(events):
            resume_ts = events[resume_idx]["ts_ms"]
            result["phase3_wait_s"] = (resume_ts - workflow_start_ms) / 1000.0
            result["phase3_dispatch_to_resume_latency_s"] = (resume_ts - workflow_end_ms) / 1000.0
            result["phase8_delivery_s"] = (session_end_ms - resume_ts) / 1000.0
        else:
            result["phase3_wait_s"] = UNAVAILABLE + ": no session event at/after workflow completion (orchestrator resume not captured)"
            result["phase8_delivery_s"] = UNAVAILABLE
    else:
        result["phase3_wait_s"] = UNAVAILABLE + ": workflow start/end unknown"

    return result


# --------------------------------------------------------------------------- top-level assembly

def build_profile(record, record_path, session_dir, run_id, projects_dir):
    workflow_start_ms = record.get("startTime")
    duration_ms = record.get("durationMs")
    workflow_end_ms = (workflow_start_ms + duration_ms) if (workflow_start_ms is not None and duration_ms is not None) else None

    subagent_dir = find_subagent_dir(session_dir, run_id)
    session_path = find_session_transcript(session_dir)

    agents = []
    notes = []
    if subagent_dir is None:
        notes.append(f"{UNAVAILABLE}: subagent transcript dir not found for {run_id} under {session_dir}")

    for wp in record.get("workflowProgress", []):
        agent_id = wp.get("agentId")
        transcript_analysis = None
        if subagent_dir is not None and agent_id:
            tpath = subagent_dir / f"agent-{agent_id}.jsonl"
            transcript_analysis = analyze_agent_transcript(tpath)

        stage = classify_stage(wp.get("label", ""), wp.get("agentType", ""))
        queued_at = wp.get("queuedAt")
        started_at = wp.get("startedAt")
        duration = wp.get("durationMs")
        agents.append(
            {
                "index": wp.get("index"),
                "label": wp.get("label"),
                "agent_type": wp.get("agentType"),
                "model": wp.get("model"),
                "state": wp.get("state"),
                "attempt": wp.get("attempt"),
                "agent_id": agent_id,
                "stage": stage,
                "queued_at": queued_at,
                "started_at": started_at,
                "duration_ms": duration,
                "tokens": wp.get("tokens"),
                "tool_calls": wp.get("toolCalls"),
                "dispatch_latency_ms": (started_at - queued_at) if (queued_at is not None and started_at is not None) else None,
                "start_offset_s": ((started_at - workflow_start_ms) / 1000.0) if (started_at is not None and workflow_start_ms is not None) else None,
                "end_offset_s": (
                    ((started_at + duration) - workflow_start_ms) / 1000.0
                    if (started_at is not None and duration is not None and workflow_start_ms is not None)
                    else None
                ),
                "transcript": transcript_analysis,
            }
        )

    stage_profile = build_stage_profile(agents, workflow_start_ms, duration_ms)
    _fill_transform_gaps(stage_profile)
    capacity = build_capacity_accounting(agents)
    critical_path = build_critical_path(agents, workflow_start_ms, workflow_end_ms)

    # Aggregate model-generation vs tool-execution time.
    total_gen_ms = 0.0
    total_tool_ms = 0.0
    gen_tool_available = 0
    for a in agents:
        t = a["transcript"]
        if t and t.get("generation_time_ms") is not None:
            total_gen_ms += t["generation_time_ms"]
            total_tool_ms += t["tool_time_ms"]
            gen_tool_available += 1

    orchestrator_phases = analyze_orchestrator_phases(session_path, workflow_start_ms, workflow_end_ms, record.get("taskId"))

    # Reconciliation against the record's own headline totals.
    sum_tokens = sum(a["tokens"] for a in agents if a["tokens"] is not None)
    sum_tool_calls = sum(a["tool_calls"] for a in agents if a["tool_calls"] is not None)
    reconciliation = {
        "agent_count": {"recorded": record.get("agentCount"), "observed": len(agents), "match": record.get("agentCount") == len(agents)},
        "total_tokens": {"recorded": record.get("totalTokens"), "observed": sum_tokens, "match": record.get("totalTokens") == sum_tokens},
        "total_tool_calls": {"recorded": record.get("totalToolCalls"), "observed": sum_tool_calls, "match": record.get("totalToolCalls") == sum_tool_calls},
        "duration_ms": {"recorded": record.get("durationMs")},
    }

    return {
        "run_id": run_id,
        "record_path": str(record_path),
        "session_dir": str(session_dir),
        "subagent_dir": str(subagent_dir) if subagent_dir else None,
        "workflow_name": record.get("workflowName"),
        "status": record.get("status"),
        "task_id": record.get("taskId"),
        "start_time_ms": workflow_start_ms,
        "start_time_iso": ms_to_iso(workflow_start_ms) if workflow_start_ms else None,
        "duration_ms": duration_ms,
        "default_model": record.get("defaultModel"),
        "recorded_agent_count": record.get("agentCount"),
        "recorded_total_tokens": record.get("totalTokens"),
        "recorded_total_tool_calls": record.get("totalToolCalls"),
        "notes": notes,
        "stage_profile": stage_profile,
        "agents": agents,
        "capacity_accounting": capacity,
        "critical_path": critical_path,
        "generation_vs_tool_time": {
            "total_generation_ms": total_gen_ms,
            "total_tool_ms": total_tool_ms,
            "agents_with_transcript_data": gen_tool_available,
            "agents_total": len(agents),
        },
        "orchestrator_phases": orchestrator_phases,
        "reconciliation": reconciliation,
    }


# --------------------------------------------------------------------------- markdown rendering

def _fmt(v, unit="", digits=1):
    if isinstance(v, str):
        return v
    if v is None:
        return UNAVAILABLE
    if unit == "s":
        return f"{v:.{digits}f}s"
    if unit == "%":
        return f"{v:.{digits}f}%"
    if unit == "x":
        return f"{v:.2f}x"
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def render_markdown(profile):
    lines = []
    p = profile
    lines.append(f"# Workflow profile — {p['run_id']}")
    lines.append("")
    lines.append(f"- workflow: `{p['workflow_name']}` | status: `{p['status']}` | task: `{p['task_id']}`")
    lines.append(f"- record: `{p['record_path']}`")
    lines.append(f"- subagent transcripts: `{p['subagent_dir']}`")
    lines.append(f"- session transcript: `{p['orchestrator_phases'].get('session_path', UNAVAILABLE)}`")
    duration_s = (p["duration_ms"] / 1000.0) if p["duration_ms"] is not None else None
    lines.append(f"- start: {p['start_time_iso']} | duration: {_fmt(duration_s, 's')} ({p['duration_ms']} ms)")
    lines.append(f"- default model: `{p['default_model']}`")
    for n in p["notes"]:
        lines.append(f"- NOTE: {n}")
    lines.append("")

    # Reconciliation
    r = p["reconciliation"]
    lines.append("## Reconciliation against record headline totals")
    lines.append("")
    lines.append("| metric | recorded | observed | match |")
    lines.append("|---|---|---|---|")
    for key, label in (("agent_count", "agentCount"), ("total_tokens", "totalTokens"), ("total_tool_calls", "totalToolCalls")):
        row = r[key]
        lines.append(f"| {label} | {row['recorded']} | {row['observed']} | {'OK' if row['match'] else 'MISMATCH'} |")
    lines.append(f"| durationMs | {r['duration_ms']['recorded']} | (defines workflow wall clock; not independently re-derived) | n/a |")
    lines.append("")

    # Stage profile
    lines.append("## Stage profile")
    lines.append("")
    lines.append("| stage | agents | span (s) | start offset (s) | end offset (s) | share of wall | avg concurrency | max concurrency |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in p["stage_profile"]:
        if "note" in s and "span_wall_s" not in s:
            lines.append(f"| {s['stage']} | {s['agent_count']} | {s['note']} | | | | | |")
            continue
        lines.append(
            "| {stage} | {n} | {span} | {sstart} | {send} | {share} | {avgc} | {maxc} |".format(
                stage=s["stage"],
                n=s["agent_count"],
                span=_fmt(s.get("span_wall_s"), "s"),
                sstart=_fmt(s.get("span_start_offset_s"), "s"),
                send=_fmt(s.get("span_end_offset_s"), "s"),
                share=_fmt((s.get("share_of_workflow_wall") or 0) * 100 if s.get("share_of_workflow_wall") is not None else None, "%"),
                avgc=_fmt(s.get("avg_concurrency"), "x") if s.get("avg_concurrency") is not None else UNAVAILABLE,
                maxc=s.get("max_concurrency") if s.get("max_concurrency") is not None else UNAVAILABLE,
            )
        )
        if s.get("note"):
            lines.append(f"|   | | _{s['note']}_ | | | | | |")
    lines.append("")

    # Per-agent table
    lines.append("## Per-agent table")
    lines.append("")
    lines.append("| label | agentType | model | attempt | duration (ms) | tokens | toolCalls | dispatch latency (ms) | start offset (s) | end offset (s) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for a in p["agents"]:
        lines.append(
            "| {label} | {atype} | {model} | {attempt} | {dur} | {tok} | {tc} | {dl} | {so} | {eo} |".format(
                label=a["label"],
                atype=a["agent_type"],
                model=a["model"],
                attempt=a["attempt"],
                dur=a["duration_ms"],
                tok=a["tokens"],
                tc=a["tool_calls"],
                dl=_fmt(a["dispatch_latency_ms"]) if a["dispatch_latency_ms"] is not None else UNAVAILABLE,
                so=_fmt(a["start_offset_s"], "s") if a["start_offset_s"] is not None else UNAVAILABLE,
                eo=_fmt(a["end_offset_s"], "s") if a["end_offset_s"] is not None else UNAVAILABLE,
            )
        )
    lines.append("")

    # Parallel-capacity accounting
    lines.append("## Parallel-capacity accounting (per stage)")
    lines.append("")
    lines.append("capacity = slowest-agent duration x slots (agents dispatched); idle = capacity - used.")
    lines.append("")
    lines.append("| stage | slots | slowest agent (ms) | capacity (agent-s) | used (agent-s) | idle (agent-s) | idle % |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in p["capacity_accounting"]:
        lines.append(
            "| {stage} | {slots} | {slowest} | {cap} | {used} | {idle} | {idlep} |".format(
                stage=c["stage"],
                slots=c["slots"],
                slowest=c["slowest_agent_ms"],
                cap=_fmt(c["capacity_agent_seconds"]),
                used=_fmt(c["agent_seconds_used"]),
                idle=_fmt(c["idle_agent_seconds"]),
                idlep=_fmt(c["idle_pct"], "%") if c["idle_pct"] is not None else UNAVAILABLE,
            )
        )
    lines.append("")

    # Critical path
    cp = p["critical_path"]
    lines.append("## Critical path")
    lines.append("")
    lines.append(
        f"Total: compute={_fmt((cp['total_compute_ms'] or 0)/1000.0,'s')}, "
        f"dispatch-latency={_fmt((cp['total_dispatch_latency_ms'] or 0)/1000.0,'s')}, "
        f"orchestration-gap(merge/filter/queueing)={_fmt((cp['total_gap_ms'] or 0)/1000.0,'s')}, "
        f"critical-path end offset={_fmt(cp.get('critical_path_end_offset_s'),'s')} "
        f"vs workflow wall={_fmt(cp.get('workflow_wall_s'),'s')}"
    )
    lines.append("")
    lines.append("| hop (stage) | critical agent | gap before (ms) | dispatch latency (ms) | compute (ms) | start offset (s) | end offset (s) |")
    lines.append("|---|---|---|---|---|---|---|")
    for h in cp["hops"]:
        lines.append(
            "| {stage} | {label} | {gap} | {dl} | {compute} | {so} | {eo} |".format(
                stage=h["stage"],
                label=h["critical_agent_label"],
                gap=_fmt(h["gap_before_ms"]),
                dl=_fmt(h["dispatch_latency_ms"]) if h["dispatch_latency_ms"] is not None else UNAVAILABLE,
                compute=_fmt(h["compute_ms"]),
                so=_fmt(h["start_offset_s"], "s"),
                eo=_fmt(h["end_offset_s"], "s"),
            )
        )
    lines.append("")

    # Model-generation vs tool-execution time
    gt = p["generation_vs_tool_time"]
    lines.append("## Model-generation vs tool-execution time")
    lines.append("")
    lines.append(
        f"Aggregate (from {gt['agents_with_transcript_data']}/{gt['agents_total']} agents with transcript data): "
        f"generation={_fmt(gt['total_generation_ms']/1000.0,'s')}, tool-exec={_fmt(gt['total_tool_ms']/1000.0,'s')}"
    )
    lines.append("")
    lines.append("| label | span (s) | generation (s) | tool-exec (s) | matched tool calls | unmatched tool_use | unmatched tool_result |")
    lines.append("|---|---|---|---|---|---|---|")
    for a in p["agents"]:
        t = a["transcript"]
        if not t or t.get("span_ms") is None:
            lines.append(f"| {a['label']} | {UNAVAILABLE} | {UNAVAILABLE} | {UNAVAILABLE} | | | |")
            continue
        lines.append(
            "| {label} | {span} | {gen} | {tool} | {matched} | {unmatched_use} | {unmatched_result} |".format(
                label=a["label"],
                span=_fmt(t["span_ms"] / 1000.0, "s"),
                gen=_fmt(t["generation_time_ms"] / 1000.0, "s"),
                tool=_fmt(t["tool_time_ms"] / 1000.0, "s"),
                matched=t["matched_tool_calls"],
                unmatched_use=t["unmatched_tool_use"],
                unmatched_result=t["unmatched_tool_result"],
            )
        )
    lines.append("")

    # Output-byte accounting for write-shaped agents
    lines.append("## Output-byte accounting (Write-shaped agents: artifact-writer, verify-input-writer)")
    lines.append("")
    write_rows = []
    for a in p["agents"]:
        t = a["transcript"]
        if not t or not t.get("writes"):
            continue
        for w in t["writes"]:
            write_rows.append((a["label"], w))
    if not write_rows:
        lines.append(f"_{UNAVAILABLE}: no Write tool calls found on artifact-writer/verify-input-writer transcripts_")
    else:
        lines.append("| label | file | content bytes | seconds spent |")
        lines.append("|---|---|---|---|")
        for label, w in write_rows:
            lines.append(
                "| {label} | {f} | {b} | {s} |".format(
                    label=label,
                    f=w["file_path"],
                    b=w["content_bytes"] if w["content_bytes"] is not None else UNAVAILABLE,
                    s=_fmt(w["duration_ms"] / 1000.0, "s"),
                )
            )
    lines.append("")

    # Orchestrator phases
    op = p["orchestrator_phases"]
    lines.append("## Orchestrator phase spans (session transcript)")
    lines.append("")
    if "note" in op and len(op) == 1:
        lines.append(f"_{op['note']}_")
    else:
        for key, label in (
            ("phase1_preflight_s", "Phase 1 (preflight)"),
            ("human_wait_after_phase1_s", "  human wait (REVIEW.md/delivery question)"),
            ("phase2_checkout_and_context_s", "Phase 2 (checkout + context prep)"),
            ("phase3_wait_s", "Phase 3 (wait for pipeline workflow)"),
            ("phase3_dispatch_to_resume_latency_s", "  dispatch-to-resume latency after pipeline completion"),
            ("phase8_delivery_s", "Phase 8 (delivery)"),
        ):
            val = op.get(key)
            if isinstance(val, (int, float)):
                lines.append(f"- {label}: {_fmt(val, 's')}")
            else:
                lines.append(f"- {label}: {val if val is not None else UNAVAILABLE}")
        if op.get("note_multiple_workflow_launches"):
            lines.append(f"- NOTE: {op['note_multiple_workflow_launches']}")
        lines.append("")
        lines.append("### Phase 2 — sequential Bash calls (model latency vs shell time)")
        lines.append("")
        calls = op.get("phase2_bash_calls")
        if calls:
            lines.append(f"count: {op.get('phase2_bash_call_count')}, total model-latency: {_fmt(op.get('phase2_total_model_latency_s'), 's')}, total shell-time: {_fmt(op.get('phase2_total_shell_time_s'), 's')}")
            lines.append("")
            lines.append("| # | description | model latency (ms) | shell time (ms) |")
            lines.append("|---|---|---|---|")
            for i, c in enumerate(calls, 1):
                lines.append(
                    "| {i} | {desc} | {ml} | {st} |".format(
                        i=i,
                        desc=c["description"] or "",
                        ml=_fmt(c["model_latency_ms"]),
                        st=_fmt(c["shell_time_ms"]) if c["shell_time_ms"] is not None else UNAVAILABLE,
                    )
                )
        else:
            lines.append(f"_{UNAVAILABLE}: Phase 2 Bash calls not derivable_")
    lines.append("")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", nargs="?", default=None, help="workflow run id, e.g. wf_cef39739-577 (default: most recent completed code-gauntlet run)")
    parser.add_argument("--projects-dir", default=str(DEFAULT_PROJECTS_DIR), help="root to search for */workflows/wf_*.json (default: ~/.claude/projects)")
    parser.add_argument("--out-json", default=None, help="path to write machine JSON profile (default: stdout only if --out-md also omitted)")
    parser.add_argument("--out-md", default=None, help="path to write markdown report")
    args = parser.parse_args(argv)

    projects_dir = Path(args.projects_dir).expanduser()
    record, record_path, session_dir = find_run_record(projects_dir, args.run_id)
    run_id = record_path.stem

    profile = build_profile(record, record_path, session_dir, run_id, projects_dir)
    md = render_markdown(profile)

    if args.out_json:
        Path(args.out_json).write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")
    if not args.out_json and not args.out_md:
        print(json.dumps(profile, indent=2))
        print(md)
    else:
        print(f"run_id={run_id}")
        if args.out_json:
            print(f"wrote JSON: {args.out_json}")
        if args.out_md:
            print(f"wrote markdown: {args.out_md}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
