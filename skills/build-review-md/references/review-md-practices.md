# REVIEW.md Configuration Practices

Reference for the `build-review-md` skill. Contains builder-specific guidance on rule design,
rollout sequencing, ongoing maintenance, and example templates for common stacks.

Source: Research Artifact #19 — "Writing REVIEW.md files that actually improve code quality"

---

## Prescriptive vs Directional Rules

Rules fall into two categories that determine language and enforcement mode.

**Prescriptive rules** apply to security and correctness — violations are always wrong. Use
imperative language: MUST, NEVER, CRITICAL. The rule produces a binary pass/fail condition
the reviewer can evaluate without judgment. False-positive rates are low because the boundary
is unambiguous.

Examples:
```
- CRITICAL: Never commit secrets, API keys, or connection strings in source files.
  Use environment variables or secret managers.

- All async methods must accept CancellationToken as the last parameter. Omitting
  tokens prevents graceful shutdown under load.

- CRITICAL: Never use .Result or .Wait() on async calls. This deadlocks ASP.NET
  Core's thread pool. Use await instead.
```

**Directional rules** apply to design, style, and conventions — they guide toward better
outcomes but allow judgment. Use preference language: prefer, consider, flag when. The rule
acknowledges that context determines the right answer.

Examples:
```
- Prefer composition over inheritance. Deep hierarchies make behavior unpredictable —
  flag more than 2 levels unless there is an explicit abstraction reason.

- Extract component classes when Tailwind strings exceed ~8 utilities. Long class
  strings harm readability and make responsive behavior hard to audit visually.
```

**Decision rule:** If a violation is always wrong in every context, write a prescriptive rule.
If a violation is wrong in most contexts but has legitimate exceptions, write a directional rule
with an escape hatch.

---

## Rule Writing Standards

**Rationale is not optional.** A rule without rationale ("Never force push") can be ignored
under pressure. A rule with rationale ("Never force push — this rewrites shared history and is
unrecoverable for collaborators") gives the reviewer enough context to generalize correctly to
edge cases.

**Rule length sweet spot:**
- One sentence: acceptable only for truly unambiguous prescriptive rules
- Two sentences (rule + rationale): recommended for most rules
- Three sentences: allowed for contextual rules that need an escape hatch
- More than three sentences: split into multiple rules or reference a separate document

**Severity markers:** Use CRITICAL sparingly — at most 3–4 per file. If every rule is critical,
none are. Reserve for security and correctness violations where ignoring the rule causes
measurable harm.

**Verifiability test:** Before adding a rule, ask: can the reviewer determine pass/fail without
ambiguity? Rules that fail this test ("write clean code", "follow best practices") produce noise
because the reviewer cannot evaluate them.

**The 15–25 ceiling:** Frontier LLMs follow approximately 150–200 total instructions reliably.
The review system prompt consumes ~50 slots. With root + two subdirectory files, each file has
roughly 33–50 slots. Keep each file under 25 rules. Exceeding this degrades adherence across all
rules, not just the new ones.

---

## Default Thresholds for New Repositories

Start here. Loosen only after reviewing acceptance rate data.

| Setting | Day-one value | Rationale |
|---------|--------------|-----------|
| Confidence Threshold | 85 | Above default 70 — surfaces only high-conviction findings while calibrating |
| Severity Threshold | medium | Avoids the firehose of low-severity noise during week 1 |
| Model Tier | frontier | Reasoning-heavy review agents require frontier capability |

Add this comment block above the thresholds in the generated REVIEW.md:

```markdown
<!-- Week 1-2: confidence 85, severity medium. Lower after reviewing acceptance rates.
     Target: >50% of findings result in code changes. See review-md-practices.md. -->
```

---

## Phased Rollout Guidance

**Day one (weeks 1–2):** Start narrow.
- Root: 8–10 rules — security (3), architecture (2), git conventions (2), error handling (2)
- Backend subdirectory: 5–7 rules — async patterns (3), data access (2), DI lifetime (1)
- Frontend subdirectory: 5–7 rules — TypeScript strictness (2), framework hooks (2), data fetching (2)
- Confidence: 85, Severity: medium
- Skip patterns: full set from day one

**Week 4:** Expand only if acceptance rate supports it.
- Lower severity to **low** if medium-severity acceptance rate exceeds 60%
- Lower confidence to **70** (default)
- Add testing convention rules
- Add validation library rules (Zod for TypeScript, Pydantic for Python)

**Month 3+:** Add based on actual incidents.
- Performance-specific rules (N+1, unnecessary re-renders)
- Cross-file impact rules for API contract changes
- Backwards compatibility rules as APIs stabilize
- Ignore patterns for recurring false positives found in months 1–2

**Why phased matters:** Teams that deploy broad review on day one generate noise and lose
trust. Teams that start narrow and expand based on data build lasting adoption.

---

## Quarterly Audit Checklist

Run this audit when acceptance rate drops below 50%, or at minimum every quarter.

1. **Acceptance rate per rule:** Which rules have findings that developers consistently
   dismiss? Rules below 30% acceptance should be rewritten or removed.

2. **Coverage check:** Review the last 20 merged PRs. Did the review system miss any bugs
   that reached production? Missing bug categories indicate rule gaps.

3. **Staleness scan:** Does every rule still apply to the current tech stack? Flag rules
   referencing deprecated patterns, removed libraries, or obsoleted conventions.

4. **Contradiction audit:** Read all REVIEW.md files in sequence. Do accumulated rules
   conflict? Root says "prefer X" while subdirectory implies "avoid X"?

5. **Ignore pattern review:** Check each date-stamped ignore pattern. Is the suppressed
   framework pattern still in use? Remove stale ignores.

6. **Rule count check:** Count total rules across all files. If exceeding 50 total
   (root + all subdirectories), prune ruthlessly. Each rule beyond the ceiling dilutes all
   others.

7. **Severity calibration:** Are CRITICAL rules actually catching critical issues? Restrict
   CRITICAL to a maximum of 3–4 rules per file.

8. **Linter overlap:** Are any rules duplicating what ESLint, Roslyn analyzers, or other
   deterministic tools already catch? Remove these — deterministic tools are faster and more
   reliable for objective checks.

---

## Example REVIEW.md Templates

### TypeScript / React

Root `REVIEW.md`:
```markdown
<!-- Week 1-2: confidence 85, severity medium. Lower after reviewing acceptance rates. -->

## Confidence Threshold
85

## Severity Threshold
medium

## Model Tier
frontier

## Skip
**/node_modules/**
**/dist/**
**/.next/**
**/build/**
**/coverage/**
package-lock.json
pnpm-lock.yaml
yarn.lock
**/*.png
**/*.jpg
**/*.svg
**/*.woff2

## Rules

### Security
- CRITICAL: Never commit secrets, API keys, or tokens in source files. Use environment
  variables and a secrets manager. git history is permanent — rotation is not enough.
- CRITICAL: Validate all user-supplied input server-side. Client-side validation is UX,
  not a security boundary. Backend must enforce constraints independently.
- All API routes must enforce authentication before processing the request body.

### Architecture
- Dependencies flow from UI components → hooks → services → API layer. Components must
  not import directly from API modules — this couples rendering to transport.
- Changes to API contract types (request/response shapes) require corresponding consumer
  updates in the same PR. Flag any contract type modification without frontend/backend
  alignment.

### Error handling
- All API calls must handle error states explicitly. Silent failures (unhandled promise
  rejections, swallowed catch blocks) hide bugs until they surface in production.
- Log errors with correlation ID and operation context. Never log sensitive user data.

### Conventions
- Commit messages follow Conventional Commits format (feat|fix|docs|refactor|test|chore:
  description). This enables automated changelog generation.
```

Frontend subdirectory `src/REVIEW.md` (or `frontend/REVIEW.md`):
```markdown
## Model Tier
frontier

## Skip
**/__mocks__/**/*.json
**/generated/**
**/public/assets/**

## Rules

### TypeScript strictness
- CRITICAL: Never use the `any` type. Use `unknown` with runtime narrowing instead. `any`
  defeats TypeScript's type system and hides bugs that appear only in production.
- Ban @ts-ignore and @ts-expect-error without an accompanying comment explaining why and
  when the suppression can be removed.
- Use Zod `.safeParse()` at API boundaries instead of type assertions. Assertions create
  invisible contract drift between frontend expectations and backend responses.

### React patterns
- Never call hooks conditionally or inside loops. This violates React's Rules of Hooks
  and causes unpredictable state behavior across renders.
- Every useEffect with subscriptions, timers, or event listeners must return a cleanup
  function. Missing cleanup causes memory leaks that compound across navigation.
- Flag useEffect calls with no dependency array or incomplete dependencies. Missing
  dependencies create stale closure bugs that are difficult to reproduce.

### Data fetching
- Every mutation endpoint must specify invalidatesTags. Missing cache invalidation causes
  stale data that appears to users as "my changes didn't save."
- Define Zod schemas as the single source of truth and infer TypeScript types with
  z.infer<typeof Schema>. Dual interface + schema definitions drift apart silently.

## Ignore
simplification:"optional chaining" for Zod schema definitions
conventions:"file length" for RTK Query API slice definitions
```

---

### Python / Django

Root `REVIEW.md`:
```markdown
<!-- Week 1-2: confidence 85, severity medium. Lower after reviewing acceptance rates. -->

## Confidence Threshold
85

## Severity Threshold
medium

## Model Tier
frontier

## Skip
**/__pycache__/**
**/*.pyc
**/*.pyo
**/migrations/*.py
**/.venv/**
**/venv/**
**/dist/**
**/build/**
**/*.egg-info/**
**/.pytest_cache/**

## Rules

### Security
- CRITICAL: Never commit secrets, credentials, or API keys in source files. Use
  environment variables and a secrets manager (django-environ, python-decouple).
- CRITICAL: All user input must be validated before reaching business logic. Never
  trust request.data or request.GET directly — use serializers or form validators.
- Use Django's ORM for all database queries. Raw SQL with string interpolation is an
  injection vulnerability. If raw SQL is required, use parameterized queries exclusively.

### Architecture
- Business logic belongs in service modules or model methods, not in views. Fat views
  that contain query logic and business rules make testing difficult and reuse impossible.
- Django signals should be used sparingly. Hidden control flow through signals makes
  debugging and testing significantly harder — prefer explicit service calls.

### Error handling
- All API views must return consistent error response shapes. Never expose stack traces,
  model internals, or database error messages to API consumers.
- Log exceptions with request context (user ID, path, request ID). Avoid logging
  request bodies or response data that may contain sensitive information.

### Conventions
- Commit messages follow Conventional Commits format (feat|fix|docs|refactor|test|chore:
  description).
```

Backend subdirectory `backend/REVIEW.md`:
```markdown
## Model Tier
frontier

## Skip
**/fixtures/**/*.json
**/snapshots/**

## Rules

### Django ORM
- Watch for N+1 queries: any QuerySet iteration that triggers per-object database queries
  is a performance bug. Use select_related() for ForeignKey and prefetch_related() for
  ManyToMany. Flag any queryset access inside a loop.
- Use .only() or .defer() for read-heavy views that don't need all fields. Full object
  hydration for large querysets degrades response times under load.
- Never reuse Django ORM objects across requests. QuerySets are lazy — accessing them
  outside their request context causes stale data and transaction isolation bugs.

### Async correctness
- Never call synchronous ORM methods from async views without sync_to_async(). Blocking
  the async event loop degrades all concurrent requests, not just the current one.
- Never use time.sleep() in async code. Use asyncio.sleep() — blocking sleep ties up the
  event loop thread.

### Type hints
- All public functions and methods must have type hints on parameters and return values.
  Type hints enable static analysis and reduce the surface area for runtime type errors.
- Use Optional[T] (or T | None in Python 3.10+) for nullable parameters rather than
  defaulting to None without annotating the type.

### Testing
- Tests must follow Arrange/Act/Assert structure. Each test method verifies one behavior.
- Use pytest fixtures for shared setup. Django TestCase class methods (setUp/tearDown)
  are acceptable only when transaction isolation is required.

## Ignore
conventions:"file naming" for Django migration files
types:"missing return type" for Django view functions using class-based views
```

---

### Go

Root `REVIEW.md`:
```markdown
<!-- Week 1-2: confidence 85, severity medium. Lower after reviewing acceptance rates. -->

## Confidence Threshold
85

## Severity Threshold
medium

## Model Tier
frontier

## Skip
**/vendor/**
**/bin/**
**/*.pb.go
**/*_gen.go
**/*_mock.go
**/mocks/**

## Rules

### Security
- CRITICAL: Never commit secrets, credentials, or API keys in source files. Use
  environment variables and a secrets manager (Vault, AWS Secrets Manager).
- CRITICAL: Validate and sanitize all user-supplied input at the service boundary.
  Trusting caller-provided data in internal service functions enables privilege escalation.
- Use parameterized queries for all database operations. String-concatenated SQL is an
  injection vulnerability regardless of the ORM or driver.

### Error handling
- All errors must be wrapped with context before returning up the call stack. Bare
  `return err` loses the location and operation that caused the failure. Use
  `fmt.Errorf("operation failed: %w", err)` or errors.Wrap.
- Never silently discard errors with `_`. If an error is intentionally ignored, add a
  comment explaining why the failure is safe to ignore in this context.
- Sentinel errors must be compared with errors.Is(), not ==. Direct comparison breaks
  when errors are wrapped.

### Architecture
- Functions must accept context.Context as the first parameter. Never create
  context.Background() or context.TODO() inside handlers or service methods — contexts
  must flow from the entry point to enable cancellation and deadline propagation.
- Dependencies flow inward: handlers → services → repositories. Services must not import
  handler packages; repositories must not import service packages.

### Concurrency
- Every goroutine must have a defined exit condition. Goroutines that cannot be stopped
  are leaks — they accumulate over time and exhaust memory.
- Always close channels from the sender side. Sending to a closed channel panics; closing
  from the receiver is a race condition.

### Conventions
- Commit messages follow Conventional Commits format.
- Interface names use the -er suffix for single-method interfaces (Reader, Writer,
  Handler). Multi-method interfaces describe the abstraction, not the implementation.

## Ignore
conventions:"error message capitalization" for third-party library error wrapping
```

---

### Java / Spring

Root `REVIEW.md`:
```markdown
<!-- Week 1-2: confidence 85, severity medium. Lower after reviewing acceptance rates. -->

## Confidence Threshold
85

## Severity Threshold
medium

## Model Tier
frontier

## Skip
**/target/**
**/*.class
**/build/**
**/generated-sources/**
**/*.iml
**/.idea/**
**/bin/**

## Rules

### Security
- CRITICAL: Never commit secrets, passwords, or API keys in source files or
  application.properties. Use environment variables or Spring Vault integration.
- CRITICAL: All REST endpoints must enforce Spring Security authentication and
  authorization. Missing @PreAuthorize or SecurityConfig entries expose resources.
- Use Spring Data JPA repositories or parameterized JPQL for all queries. Native queries
  with string concatenation are SQL injection vulnerabilities.

### Architecture
- Spring components must follow single-responsibility. @Service classes that contain both
  business logic and data access should be split — repositories handle persistence,
  services handle orchestration.
- Dependencies flow: @RestController → @Service → @Repository. Controllers must not call
  repositories directly; repositories must not contain business logic.
- Avoid circular dependencies between Spring beans. Circular dependencies indicate a
  design problem — extract a third component to break the cycle.

### Error handling
- All @RestController exception handlers must return consistent error response shapes.
  Never expose stack traces, Hibernate internals, or database error details to API
  consumers.
- Use @ControllerAdvice for cross-cutting exception handling. Catching RuntimeException
  in individual controllers duplicates error handling logic and misses unmapped
  exceptions.

### Conventions
- Commit messages follow Conventional Commits format.
- REST endpoints follow noun-based URL conventions (/users, /orders/{id}) with HTTP
  method semantics. Verb-based URLs (/getUser, /createOrder) violate REST conventions.

## Ignore
conventions:"wildcard imports" for Spring Boot auto-configuration classes
```

Backend subdirectory (e.g., `src/main/java/REVIEW.md`):
```markdown
## Model Tier
frontier

## Skip
**/test/**/*.sql
**/resources/db/migration/**

## Rules

### JPA and Hibernate
- Watch for N+1 queries: any entity relationship accessed in a loop without eager loading
  is a performance bug. Use JOIN FETCH in JPQL or @EntityGraph for controlled eager
  loading. Flag any @ManyToOne or @OneToMany access inside an iteration.
- All read-only queries must use @Transactional(readOnly = true). This disables dirty
  checking and flush, reducing overhead for non-mutating operations.
- Prefer @Repository interfaces extending JpaRepository over custom EntityManager usage.
  Custom EntityManager queries bypass Spring Data's safety checks and transaction
  management.

### Spring bean lifecycle
- Never inject @RequestScope or @SessionScope beans into @Singleton beans directly.
  Use ObjectProvider<T> or javax.inject.Provider<T> for lifecycle-scoped dependencies.
- @Async methods must return CompletableFuture or void — returning direct values from
  @Async methods bypasses the async execution and runs synchronously.

### Testing
- Unit tests must mock all external dependencies using Mockito. Tests that hit a real
  database are integration tests — mark them with @SpringBootTest and run separately.
- Test names follow MethodName_Scenario_ExpectedResult convention. Descriptive names
  replace the need for test comments.
- Each test method verifies one behavior. Tests with multiple unrelated assertions make
  failure diagnosis difficult.

## Ignore
conventions:"method length" for Spring Data JPA specification classes
types:"unchecked cast" for Spring generic type erasure patterns
```

---

## Ignore Pattern Guidelines

Ignore patterns suppress known false positives. Format: `dimension:"pattern" for context`.

**Target the middle ground** — describe the pattern category, not the instance:
```
# Good: category-level suppression
conventions:"file naming" for EF Core migration files
types:"nullable reference" for test assertion helpers

# Too narrow: breaks when code moves
bugs:"null reference in UserService.GetCoach line 47"

# Too broad: suppresses genuine findings
bugs:"null reference"
```

**Date-stamp every ignore pattern:**
```markdown
## Ignore
# 2026-03-30: EF Core migrations are generated, naming conventions don't apply
conventions:"file naming" for migration files
# 2026-03-30: Test helpers intentionally use nullable without guards
types:"nullable reference" for test assertion helpers
```

**Soft cap:** 10–15 ignore patterns per file. Exceeding this signals either over-sensitive
rules that should be removed, or a systematic mismatch between rules and the codebase.
