---
name: build-review-md
description: |
  Use this skill when the user wants to create or set up a REVIEW.md configuration file for their repository. Trigger for ANY of these: (1) user says "create REVIEW.md", "set up REVIEW.md", or "configure review rules", (2) code-gauntlet Phase 2c detects no REVIEW.md and suggests creating one, (3) user wants to customize what the code-gauntlet skill focuses on or ignores, (4) user asks "how do I configure the reviewer" or "how do I set review rules". Do NOT trigger for: reviewing code (use code-gauntlet), explaining what REVIEW.md does in the abstract, or editing an already-complete REVIEW.md the user is satisfied with. This skill NEVER loads into the main code-gauntlet context — it is a standalone configuration wizard.
---

# REVIEW.md Builder

Guided wizard that creates a REVIEW.md configuration tailored to the repo's actual languages, frameworks, and priorities. Produces a root REVIEW.md and subdirectory configs only when the multi-stack test justifies them.

**This skill produces configuration files, not code review.** Do not run review agents or analyze code for bugs. Stay in configuration mode until exit.

---

## Step 1: Detect

Scan the repository to understand what you are configuring. Do this silently — do not ask the user to describe their stack.

**Languages** — scan file extensions:

- `.ts`, `.tsx` → TypeScript/React
- `.js`, `.jsx` → JavaScript
- `.py` → Python
- `.go` → Go
- `.rs` → Rust
- `.cs` → C#/.NET
- `.java` → Java
- `.rb` → Ruby
- `.php` → PHP

**Frameworks and runtimes** — check for manifest files:

- `package.json` → Node/JS/TS ecosystem; read `dependencies` and `devDependencies` for React, Next.js, Vue, Express, NestJS, RTK Query, Zod, Vitest, Jest
- `requirements.txt` / `pyproject.toml` / `setup.py` → Python; check for Django, FastAPI, SQLAlchemy, pytest
- `Cargo.toml` → Rust; check for tokio, axum, sqlx
- `go.mod` → Go; check for gin, echo, gorm
- `pom.xml` / `build.gradle` → Java/Kotlin; check for Spring Boot, Hibernate
- `*.csproj` / `*.sln` → .NET; check for ASP.NET Core, EF Core, xUnit, NUnit
- `Gemfile` → Ruby on Rails

**Multi-stack / mono-repo detection** — a repo is multi-stack when it contains multiple package manifest files in different subdirectories (e.g., `frontend/package.json` AND `backend/*.csproj`). Record the subdirectory paths.

**Existing REVIEW.md** — check whether a root REVIEW.md already exists. If it does, announce what you found and ask:

```
AskUserQuestion({
  "question": "A REVIEW.md already exists at the repo root. What would you like to do?",
  "options": [
    "Start fresh — replace it with a new one",
    "Extend it — add rules to what's already there",
    "Cancel — keep what I have"
  ]
})
```

If "Cancel", exit immediately with the message: "Your existing REVIEW.md is unchanged."

---

## Step 2: Ask priorities

Announce your detection findings in one sentence (e.g., "I found a TypeScript/React frontend and a .NET backend in separate directories."), then ask:

```
AskUserQuestion({
  "question": "What matters most for this review configuration?",
  "options": [
    "Security & Correctness — catch secrets, auth gaps, bugs, async errors, type misuse",
    "Code Quality & Performance — complexity, duplication, naming, N+1 queries, resource leaks",
    "Test Coverage & Conventions — flag missing tests, enforce commit messages, PR size, naming patterns",
    "All of the above"
  ]
})
```

Record the selected priorities. Security and Correctness always produce **prescriptive rules** ("MUST", "NEVER", "CRITICAL") because violations are always wrong. Code quality, performance, test coverage, and conventions produce **directional rules** ("prefer", "consider", "flag when") that allow judgment.

---

## Step 3: Generate root REVIEW.md

Generate a root REVIEW.md with 8–10 rules drawn from the detected stack and selected priorities. Rules must be specific and verifiable — see guidance below.

**Rule writing standards** (derived from research on LLM instruction-following):

- Prescriptive rules for security and correctness: state the requirement, then state why in one sentence. Example: "CRITICAL: Never commit secrets or API keys in source files. Use environment variables or secret managers — exposure in git history is permanent."
- Directional rules for quality and conventions: state the preference, then the escape hatch or rationale. Example: "Prefer composition over inheritance. Deep hierarchies make behavior unpredictable — flag more than 2 levels unless there is an explicit abstraction reason."
- One-liners are acceptable only when the rule is truly unambiguous. Two-sentence rules with rationale outperform one-liners for anything contextual.
- CRITICAL label is reserved for security and correctness violations that are always wrong. Use it for at most 3–4 rules per file — overuse destroys emphasis.
- Do not write rules that duplicate what linters catch deterministically (formatting, indentation, import sorting).

**Start with conservative thresholds:**

```markdown
## Confidence Threshold
85

## Severity Threshold
medium

## Model Tier
frontier
```

The research consensus: start at confidence 85 and severity medium for the first 2–4 weeks. Lower thresholds after reviewing acceptance rates. Document this in a comment above the thresholds block.

**Rule sections** — organize by selected priorities:

- Security (always first, if selected)
- Correctness / language-specific bugs
- Architecture and design (if code quality selected)
- Performance (if selected)
- Tests (if selected)
- Conventions (always last, if selected)

**Skip patterns** — always include a full skip block with generated/binary/lock files appropriate to the detected stack. Standard set:

```markdown
## Skip
**/bin/**
**/obj/**
**/dist/**
**/node_modules/**
**/.next/**
**/Generated/**
package-lock.json
pnpm-lock.yaml
yarn.lock
**/*.g.cs
**/Migrations/*.Designer.cs
**/.vs/**
**/*.suo
**/*.user
**/*.png
**/*.jpg
**/*.woff2
**/*.woff
```

Adjust for detected stack (e.g., add `**/target/**` for Rust/Java, `**/__pycache__/**` for Python, `**/vendor/**` for Go).

**Output** — write the file to `REVIEW.md` at the repo root. Announce: "Root REVIEW.md written with [N] rules."

---

## Step 4: Generate subdirectory REVIEW.md(s)

Apply the multi-stack decision test: for each technology-specific rule you want to add, ask: **"Would this rule generate false positives when applied to the other stack?"**

If yes → the rule belongs in a subdirectory REVIEW.md.
If no → the rule belongs in the root REVIEW.md.

**Only create subdirectory configs when the answer is "yes" for at least 3 rules.** A single technology-specific rule does not justify a new file — add it to root with a path qualifier in the rule text: "For files in `backend/`: prefer event-sourced state transitions over direct entity mutation."

**When creating subdirectory configs:**

- Include `## Model Tier` and technology-specific `## Skip` patterns only
- Do not repeat rules that are already in root
- Do not contradict root rules — extend them. If root says "prefer immutable types," the subdirectory rule should add the escape hatch, not contradict
- Subdirectory configs should have 5–8 rules each; stop at the second directory level

**Common subdirectory rules by stack:**

*TypeScript/React frontend:*

- TypeScript strict mode enforcement (no `any`, no `@ts-ignore` without comment)
- React hooks rules (no conditional hooks, useEffect cleanup, dependency arrays)
- RTK Query cache invalidation (invalidatesTags on mutations)
- Zod schema validation at API boundaries (.safeParse() not type assertions)

*.NET/C# backend:*

- Async/await patterns (no .Result/.Wait(), async void except event handlers, CancellationToken)
- EF Core patterns (N+1 detection, AsNoTracking for reads, scoped DbContext)
- DI lifetime correctness (no scoped in singleton)

*Python:*

- Type hints required on public functions
- SQLAlchemy session management (no session reuse across requests)
- Async correctness (no blocking calls in async functions)

*Go:*

- Error wrapping with context (errors.Wrap, not bare return err)
- Context propagation (first argument, no context.Background() in handlers)
- Goroutine leak prevention (always close channels, cancel contexts)

**Output** — write each subdirectory REVIEW.md. Announce each file written and the rule count.

---

## Step 5: Exit

After all files are written, output a brief summary:

```
REVIEW.md created. Start your review — the config will be picked up automatically.

Files written:
  REVIEW.md — [N] rules ([threshold] confidence, medium severity)
  [subdir]/REVIEW.md — [N] rules  (if applicable)

Thresholds are conservative for week 1. After 2–4 weeks, review your acceptance rate:
- If >60% of findings result in fixes, lower severity threshold to "low"
- If false positives accumulate, raise confidence threshold to 90
- Run `code-gauntlet` on any open PR to see results immediately
```

Do not offer to run a review or explain the review process. The user can trigger code-gauntlet separately.

---

## Critical Rules

1. **No code review.** This skill configures — it does not analyze code for bugs, security issues, or quality problems.
2. **Prescriptive for non-negotiables, directional for preferences.** Never write a CRITICAL rule for a style preference, never write a directional rule for a security requirement.
3. **15–25 rules total across all files.** Exceeding this degrades LLM instruction-following. Prefer 8–10 in root, 5–8 per subdirectory.
4. **Do not create subdirectory configs without the decision test.** Technology-specific rules that pass the false-positive test belong in root with a path qualifier.
5. **Conservative defaults.** Always start at confidence 85, severity medium. The research is unambiguous: teams that start broad generate noise and lose trust.
