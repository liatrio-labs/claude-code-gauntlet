# FIX Task Metadata Template

When creating FIX tasks from review findings, each task is a self-contained work order compatible with cw-execute's 11-phase protocol. Tasks are created in three sub-steps.

## Step 2.5: Detect project toolchain (once, before creating any tasks)

Detect the project's test/lint/build commands by scanning for config files at the repo root:

| Config file | Test command | Lint command | Build command |
|---|---|---|---|
| `package.json` | `npm test` | `npm run lint` | `npm run build` |
| `Cargo.toml` | `cargo test` | `cargo clippy` | `cargo build` |
| `go.mod` | `go test ./...` | `golangci-lint run` | `go build ./...` |
| `pyproject.toml` | `pytest` | `ruff check .` | — |
| `Makefile` | `make test` | `make lint` | `make build` |
| `.csproj` / `.sln` | `dotnet test` | `dotnet format --verify-no-changes` | `dotnet build` |

Check `package.json` scripts for custom names (e.g., `"test:unit"`, `"lint:fix"`). If no config files are found, use generic placeholders and note "toolchain not detected — update commands manually."

Store the detected commands for reuse across all FIX tasks in this session.

## Step 3a: Detect patterns_to_follow

For each finding's file, identify 1-2 other files in the same directory that are NOT being fixed. These serve as style reference for the implementing agent. Prefer files with similar names or purpose.

## Step 3b: TaskCreate with structured description

```
TaskCreate(
  subject: "FIX: [finding.title]",
  description: "## Issue\n[finding.description]\n\n## Location\n`[finding.file]:[finding.line_start]-[finding.line_end]`\n\n## Evidence\n[finding.evidence]\n\n## Suggested Fix\n[finding.suggestion]\n\n## Category\n[finding.severity] | [finding.dimension]"
)
```

## Step 3c: TaskUpdate with implementation metadata

```
TaskUpdate(
  taskId: "<id from TaskCreate>",
  metadata: {
    "task_type": "review-fix",
    "task_id": "FIX-[finding.id]",
    "category": "[finding.dimension]",
    "severity": "[finding.severity]",
    "role": "implementer",
    "complexity": "<critical/high → 'standard', medium/low → 'trivial'>",
    "model": "<trivial → 'haiku', standard → 'sonnet'>",
    "scope": {
      "files_to_create": [],
      "files_to_modify": ["[finding.file]"],
      "patterns_to_follow": ["<1-2 nearby files from Step 3a>"]
    },
    "requirements": [
      {
        "id": "R-[finding.id].1",
        "text": "[finding.description]",
        "testable": true
      }
    ],
    "proof_artifacts": [
      {
        "type": "test",
        "command": "<detected test command from Step 2.5>",
        "expected": "All pass"
      },
      {
        "type": "file",
        "path": "[finding.file]",
        "contains": "<key pattern from the suggested fix>"
      }
    ],
    "verification": {
      "pre": ["<detected lint command>", "<detected build command>"],
      "post": ["<detected test command>"]
    },
    "commit": {
      "template": "fix([scope-from-file-path]): [finding.title]"
    },
    "review_context": {
      "finding_id": "[finding.id]",
      "dimension": "[finding.dimension]",
      "confidence": <finding.confidence>,
      "evidence": "[finding.evidence]",
      "cross_file_refs": ["[finding.cross_file_refs]"],
      "blame_classification": "<new or surfaced from the Verify stage classification>"
    }
  }
)
```

## How cw-execute consumes this metadata

- `scope` enables Phase 3 (CONTEXT) to load patterns and identify files
- `requirements` drives Phase 4 (IMPLEMENT) — each requirement becomes one implementation unit
- `proof_artifacts` drives Phase 6 (PROOF) — test commands are pre-detected
- `verification` drives Phase 2 (BASELINE), Phase 5 (VERIFY-LOCAL), and Phase 9 (VERIFY-FULL)
- `commit.template` drives Phase 8 (COMMIT) — conventional commit format
- `complexity` and `model` enable cw-dispatch to route to the appropriate model tier
- `review_context` preserves traceability back to the review finding
