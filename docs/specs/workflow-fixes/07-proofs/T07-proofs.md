# T07 Proof Artifacts

## Task
T07: Specify exact gh pr diff commands in Phase 2 (BF-07)

Requirement: In Phase 2's diff-gathering steps, explicitly specify the exact commands:
- `gh pr diff {number} --name-only` for file list
- `gh pr diff {number}` for full diff
- Prevent orchestrator from hallucinating `--stat` flag

## Files Modified
- `skills/deep-review/references/phase2-triage.md` (section 2c)

## Change Summary

Added explicit command specifications to section 2c "Identify Review Target":

### Before
```
1. **PR/MR mode** — user provides a number/URL. Use `gh pr view`/`glab mr view` + diff commands. Get full SHA: `git rev-parse HEAD`
```

### After
```
1. **PR/MR mode** — user provides a number/URL. Use `gh pr view`/`glab mr view` + diff commands. Get full SHA: `git rev-parse HEAD`
   - **GitHub (PR):** Gather the file list with `gh pr diff {number} --name-only`. Gather the full diff with `gh pr diff {number}`.
   - **GitLab (MR):** Gather the file list with `glab mr diff {number} --name-only`. Gather the full diff with `glab mr diff {number}`.
```

## Proof Artifacts

### T07-01-content-verification.txt
- **Type:** file
- **Status:** PASS
- **Result:** Verified that exact commands are now explicitly specified for both GitHub and GitLab, with no --stat flag present.

## Validation

- ✓ GitHub PR commands explicitly specified (--name-only for file list, default for full diff)
- ✓ GitLab MR commands explicitly specified (--name-only for file list, default for full diff)
- ✓ No --stat flag in any specification
- ✓ Prevents orchestrator hallucination by making exact syntax clear
- ✓ Follows existing phase2-triage.md formatting and style
