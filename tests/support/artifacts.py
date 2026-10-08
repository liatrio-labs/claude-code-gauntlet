"""Input builders for artifact and command characterization."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from gauntlet.artifacts import plan_checksum
from gauntlet.jsjson import fnv1a32, normalize_content, utf16_len

REPO_ROOT = str(Path(__file__).resolve().parents[2])


def finding(fid):
    """Canonical persisted input with the writer's v2 aliases."""
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
    f["line"] = f["line_start"]
    f["end_line"] = f["line_end"]
    f["body"] = f["description"]
    return f


def js_pretty(obj):
    """Byte-equivalent of JSON.stringify(obj, null, 2)."""
    return json.dumps(obj, indent=2, ensure_ascii=False)


class _Workspace:
    """A temp output dir with findings.json + report.md already on disk."""

    def __init__(self):
        self.findings = [finding("F1"), finding("F2")]
        self.report = "# report\n\nbody"

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
        self.findings_json = js_pretty(self.findings)
        self.write(self.findings_path, self.findings_json)
        self.write(self.report_path, self.report)
        return self

    def write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def plan(self):
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
        return plan

    def write_plan(self, plan):
        """The self-proof covers the plan without its checksum field."""
        out = dict(plan)
        out.pop("planChecksum", None)
        out["planChecksum"] = plan_checksum(out)
        self.write(self.plan_path, js_pretty(out))
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
    """Wrap the return with the Workflow tool's captured output-file key set."""
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


def task_file(root, name="task", text="", mtime=10):
    """Place input at the nested layout task discovery searches."""
    path = Path(root) / "slug" / "session" / "tasks" / f"{name}.output"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def returned_task(path, payload):
    """Write a return-channel input without going through the subject."""
    Path(path).write_text(
        json.dumps({"result": {"ok": True, "stats": {}, "persistReturn": payload}}),
        encoding="utf-8",
    )
    return str(path)


def artifact_plan(source="findings.json", ids=None):
    """A small relative-path instruction set, suitable for literal wire proofs."""
    ids = ["A"] if ids is None else ids
    return {
        "planVersion": 2,
        "expect": [],
        "postReview": {
            "path": "post.json",
            "source": source,
            "ids": ids,
            "wrapper": None,
        },
        "checkpoint": {
            "path": "checkpoint.json",
            "source": source,
            "challengeFindingIds": ids,
            "stripAliasFields": ["line", "body"],
            "skeleton": {
                "phases": {"challenge": {"before": 1, "findings": [], "after": 2}}
            },
        },
    }


def seal_plan(path, plan):
    """Produce a valid input proof; tests supply their own expected receipt values."""
    sealed = dict(plan)
    sealed["planChecksum"] = plan_checksum(plan)
    Path(path).write_text(json.dumps(sealed), encoding="utf-8")
    return sealed
