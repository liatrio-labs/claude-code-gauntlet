"""Input builders for artifact and command characterization."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from gauntlet.artifacts import plan_checksum
from gauntlet.jsjson import fnv1a32, normalize_content, utf16_len

REPO_ROOT = str(Path(__file__).resolve().parents[2])


def finding(fid, **over):
    """A canonical persisted finding: canonical schema + the v2 aliases the
    artifact-writer boundary adds (line/end_line/body)."""
    f = {
        "id": fid,
        "file": f"src/{fid}.js",
        "line_start": 10,
        "line_end": 12,
        "title": f"finding {fid}",
        "description": f"a real problem in {fid}",
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
        with open(path, encoding="utf-8", newline="") as fh:
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


SUCCESS_RETURN = {
    "ok": True,
    "phaseReached": "report",
    "stats": {"discovered": 9, "merged": 9, "verified": True},
    "artifactPaths": {
        "findings": "/out/code-gauntlet-findings-da09bc08.json",
        "report": "/out/code-gauntlet-report-da09bc08.md",
    },
    "resolvedPolicy": {"subagentModel": None},
    "checkpoints": {"completed": ["summarize", "report"]},
    "gaps": [],
}


def envelope(result):
    """Wrap *result* in the Workflow tool's real output-file envelope.

    Key set and nesting copied from .../tasks/w3eeyrqqm.output: the tool writes
    {summary, agentCount, logs, result, workflowProgress, totalTokens,
    totalToolCalls} with the script's return value nested at `result`.
    """
    return {
        "summary": "code-gauntlet v3 pipeline: phases 3-8 orchestration",
        "agentCount": 19,
        "logs": [],
        "result": result,
        "workflowProgress": [
            {
                "type": "workflow_agent",
                "index": 1,
                "label": "summarize",
                "state": "done",
                "lastToolSummary": "ok",
            },
        ],
        "totalTokens": 669337,
        "totalToolCalls": 118,
    }


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
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"recorder failed: {proc.stderr}")
    return task_path, out_dir
