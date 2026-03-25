# Research

Research artifacts that informed the design of claude-deep-review. Each document covers a specific aspect of AI code review architecture, validated against production systems and published research.

## Artifacts

| # | Document | Summary |
|---|----------|---------|
| 01 | [AI Code Review: State of the Art](artifacts/01-ai-code-review-state-of-the-art.md) | Comprehensive synthesis of production AI code review systems (Anthropic, CodeRabbit, Ellipsis, Qodo, Greptile). Establishes that concern-parallel agents with verification outperform file-parallel or single-pass approaches. Documents Anthropic's dimension-based architecture and the five synthesis patterns across production systems. |
| 02 | [False Positive Reduction Techniques](artifacts/02-false-positive-reduction-techniques.md) | How production systems fight false positives. Key finding: deterministic grounding (verifying findings against actual code) beats LLM-on-LLM verification since frontier models share correlated errors ~60% of the time. Covers Anthropic's confidence scoring, CodeRabbit's evidence-linked hallucination filter, and Ellipsis's 4-stage pipeline. |
| 03 | [Optimal Agent Count](artifacts/03-optimal-agent-count.md) | Research showing quality plateaus at 4-6 agents. DeepMind found unstructured parallel systems amplify errors up to 17.2x. Recommends coarse-grained specialization (one security agent, not separate injection/auth/crypto agents) and heterogeneous agent design to delay saturation. |
| 04 | [Context Window Engineering](artifacts/04-context-window-engineering.md) | How systems handle large PRs. Key finding: context length alone degrades reasoning by 13-85% even when information is retrievable. Documents PR-Agent's asymmetric context windows, CodeRabbit's recursive summarization, and cubic's context-pulling pattern (51% fewer false positives). |
| 05 | [Prompt Injection Vulnerabilities](artifacts/05-prompt-injection-vulnerabilities.md) | Documents that every major AI code review tool has been exploited via prompt injection. Catalogs 17+ real-world attack vectors. Recommends 5-layer defense: preprocessing, prompt architecture (trust boundary delimiters), architectural separation, output validation, and operational controls. |
| 06 | [LSP Integration Patterns](artifacts/06-lsp-integration-patterns.md) | How LLM coding tools integrate Language Server Protocol. LSP provides 900x faster symbol resolution (50ms vs 30-60s) and eliminates false matches from comments/strings. Documents Claude Code's native LSP support, the Serena MCP server, and LSPRAG's 213% improvement in adjacent tasks. |
| 07 | [Evaluation Benchmarks and Metrics](artifacts/07-evaluation-benchmarks-and-metrics.md) | How to evaluate AI code review quality. Identifies the architectural blind spot (cross-file bugs) as largely unsolved. Documents that AI-generated code produces 75% more logic errors. Covers CRQBench, Augment Code benchmark, and the precision-recall tradeoff (no tool exceeds ~55% recall). |
| 08 | [Handling Pre-existing Technical Debt](artifacts/08-handling-pre-existing-technical-debt.md) | How production systems avoid flooding PRs with tech debt findings. SonarQube's deterministic baseline matching is the gold standard. AI-native tools rely on diff-scoping and confidence filtering. Documents the consistency-vs-correctness tradeoff (no tool handles "new code following existing bad patterns" automatically). |
| 09 | [Hierarchical Config File Design](artifacts/09-hierarchical-config-file-design.md) | Design patterns for directory-scoped configuration (REVIEW.md). Industry convergence on "nearest-only with explicit extends" after ESLint's painful cascading lessons. Recommends centralized root config with optional subdirectory overrides. Documents CLAUDE.md's additive merge model and CodeRabbit's path_instructions pattern. |
| 10 | [Agent-Skill Architecture Patterns](artifacts/10-agent-skill-architecture-patterns.md) | Whether agents should delegate to skills for methodology. Conclusion: don't — Anthropic's own plugins use self-contained agents. System prompt instructions outperform mid-execution loaded instructions by 30%+ (Lost in the Middle paper). Debugging is 3-5x harder with split behavior. Subagent-skill interaction is buggy. |
| 11 | [Incremental Review Patterns](artifacts/11-incremental-review-patterns.md) | How production tools handle re-reviews after new commits. Only CodeRabbit implements genuine incremental review (state persisted in hidden PR comments). Most tools re-analyze the full diff. Academic techniques (Infer's report diffing, SonarQube's new code period) offer a blueprint LLM tools haven't adopted. Comment lifecycle management and notification fatigue drive 60-80% noise rates. |
| 12 | [Model Routing for Code Review](artifacts/12-model-routing-for-code-review.md) | Evidence for assigning different model tiers to different review subtasks. Anthropic's own plugin uses all-Sonnet, not Opus for bugs. SWE-bench Verified gap between Opus and Sonnet compressed to 1.2 points. DeepMind: "spend on workers, not the manager." Different models have complementary vulnerability-class detection profiles — the strongest case for multi-model security review. More intuition than rigorous evidence. |
| 13 | [Cost and Token Economics](artifacts/13-cost-and-token-economics.md) | Multi-agent review costs $0.05-$25 per PR (500x range). Token duplication rates of 53-86% across frameworks. Quality plateaus at 4 agents. Prompt caching delivers 60-90% savings. Self-hosted optimized systems achieve $0.50-$1.50/PR vs Anthropic's $15-$25 managed service. Model costs falling 10-50x/year. The Faros AI Productivity Paradox: AI review may deliver higher organizational ROI than AI coding assistants. |
| 14 | [Inter-Agent Debate and Challenge Rounds](artifacts/14-inter-agent-debate-and-challenge-rounds.md) | Majority voting, not debate, drives performance gains (martingale proof). Sycophancy corrupts verification in 18/20 configurations. Production systems favor pipelines over debate. Disagreement itself is the signal — route contradictions to blind challenge, testable claims to deterministic verification, ambiguous cases to human escalation. Challenge agents should see only the finding and code, never the original reasoning. |
| 15 | [Developer Experience of Review Output](artifacts/15-developer-experience-of-review-output.md) | Engagement decays in ~10 days without tuning. Optimal volume: 5-6 comments per PR. Committable code suggestions see 60-70% implementation rates vs 36-43% for prose. Trust in AI accuracy at 29% (Stack Overflow 2025). Adoption threshold is 75-80% precision. Batch findings into single review events. Silence is a feature — post nothing on 29% of reviews. |
| 16 | [Reliable API Payload Patterns](artifacts/16-reliable-api-payload-patterns.md) | Why shell-constructed JSON fails for AI agents (double-escaping trap). Python `json.dumps()` to temp file → `gh api --input` is the most reliable pattern. Covers GitHub batched PR reviews, GitLab MR discussions with position data, universal Python helper, `jq --arg` as shell-native alternative. `-f`/`-F` flags cannot construct `comments` arrays. |

## How these informed the design

Key design decisions and which research artifacts support them:

| Design Decision | Artifacts | Rationale |
|----------------|-----------|-----------|
| 5+2 agents (5 always-on, 2 conditional) | #03 | Quality plateaus at 4-6; unstructured parallel amplifies errors 17.2x |
| Concern-parallel decomposition | #01 | Anthropic, Ellipsis, Qodo converge on concern-parallel, not file-parallel |
| Deterministic verification before LLM judgment | #02 | LLM-on-LLM shares correlated errors ~60%; grounding in tool output is essential |
| Context-pulling (agents investigate via tools) | #04 | cubic achieved 51% fewer false positives switching from push to pull |
| Security confidence threshold 70 (vs 80 for others) | #02 | Anthropic's judge filtered 7/8 security findings at 80, including real ones |
| Overconfidence calibration warnings | #02 | Xiong et al. (ICLR 2024): LLMs cluster confidence in 80-100 range |
| Full-codebase investigation mandate | #01 | Cat Wu (Anthropic): "agents take the entire codebase into account" |
| Prompt injection trust boundary delimiters | #05 | Every major tool exploited; 5-layer defense recommended |
| LSP-first navigation instructions | #06 | 900x faster; eliminates false matches from comments/strings |
| AI-generated code risk elevation | #07 | CodeRabbit: 75% more logic errors in AI-authored code |
| 500-line large-PR threshold | #07 | Review effectiveness drops sharply above 400 lines |
| Git blame new/surfaced classification | #08 | SonarQube's baseline matching adapted for AI-native tools |
| REVIEW.md hierarchy mirroring CLAUDE.md | #09 | Nearest-only with explicit extends; settings override, rules accumulate |
| Suggest-not-modify for config changes | #09 | Google SRE: tools should suggest through reviewable mechanisms |
| Self-contained agent definitions (not skill delegation) | #10 | Anthropic's own plugins are self-contained; system prompt instructions outperform by 30%+ |
| Max findings cap | #08 | Qodo defaults to 3 findings; configurable cap prevents noise in high-debt repos |
| GitHub + GitLab VCS abstraction | #01 | Production tools support multiple platforms; auto-detection from git remote |
| Incremental review with report diffing | #11 | Infer's introduced/fixed/preexisting classification; CodeRabbit's hidden PR comment state persistence |
| Model routing: Sonnet default, Opus for security | #12 | SWE-bench gap compressed to 1.2 points; Anthropic's own plugin is all-Sonnet; complementary vulnerability-class profiles justify multi-model security |
| Prompt caching for cost optimization | #13 | 70-80% of input tokens cacheable; 60-90% savings; self-hosted at $0.50-$1.50 vs $15-$25 managed |
| Challenge round: blind challenge on contradictions only | #14 | Martingale proof: debate doesn't improve correctness; sycophancy in 18/20 configs; challenge agents must not see original reasoning |
| Disagreement as difficulty signal | #14 | Ensemble disagreement correlates with finding importance; route to appropriate resolution mechanism rather than forcing consensus |
| Max 5-6 comments per review | #15 | Engagement decays in ~10 days; adoption threshold 75-80% precision; silence is a feature |
| Committable code suggestions in findings | #15 | 60-70% implementation rate vs 36-43% for prose-only; Graphite Agent: 67% of suggestions implemented |
| Batch findings into single review event | #15 | Per-comment notifications cause auto-dismissal; one review event = one notification |
| Rich FIX task metadata (cw-plan compatible) | #13, #15 | Self-contained work orders with structured requirements, proof artifacts, and toolchain detection enable autonomous execution via cw-execute |
| Light review mode for trivial PRs | #13 | 31% of small PRs receive no findings; 2-agent mode cuts cost ~60%; quality plateaus at 4 agents |
| Soft default cap of 8 findings | #15 | Engagement decays in ~10 days; 5-6 comments optimal; 75-80% precision is the adoption threshold; default cap prevents noise without requiring REVIEW.md setup |
| Python `json.dumps` for API payloads | #16 | Shell-constructed JSON fails due to double-escaping trap (JSON + bash metacharacters). Python serialization to temp file eliminates all escaping layers; `gh api --input` / `glab api --input` never see raw JSON in shell context |

## Adding new research

When adding new research artifacts:
1. Number sequentially (next: `17-`)
2. Use lowercase-kebab-case for filenames
3. Place in the `artifacts/` directory
4. Update this index with a summary row and any new design decision mappings
