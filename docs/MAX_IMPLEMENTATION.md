# Max Implementation Plan

## Product goals

Max is a friendly, personalized Discord-first assistant that improves from verified experience while keeping the existing Railway deployment and operating costs under control.

## Non-negotiable constraints

- Keep `main` deployable and preserve the existing Railway production service during migration.
- Do not change Railway variables or deploy automatically during development.
- Keep the existing FreeLLMAPI gateway optional and preserve its AI_BASE_URL, AI_API_KEY, and AI_MODEL compatibility.
- Keep the existing bot process and avoid additional paid services by default.
- Preserve existing commands and features until replacements are tested.
- Bound model calls, retries, tool calls, execution time and context size.
- Require explicit approval for consequential external actions.
- Never store secrets in memory, lessons, logs or repository documentation.

## Target architecture

Keep modules in the existing process initially; do not split them into separate Railway services without evidence that it is necessary.

- Discord gateway and command router
- Assistant orchestrator with one Max identity
- Task-aware model router
- Capability registry for GitHub, research and sandbox execution
- Personal memory and scoped project/repository knowledge
- Verified learning engine
- Permission and approval policy
- Usage metrics, structured logs and bounded execution

Phase 3 adds bounded planner, coder, reviewer, and tester roles using temporary
file copies and task-engine capability checks. The repair and task-staging
commands prepare scoped pending actions through the task engine. They do not
write to GitHub because a trusted Discord approval interface does not exist yet.
Repository repair requires explicit opt-in before sending source or memory
to remote sandbox services. Cloud model inference requires a separate privacy opt-in. Local file copies do not provide operating-
system isolation for executable tests.

## Task-aware model routing

Phase 4 extends the existing model router with local Ollama inference. The
initial text model is `qwen3:8b`; `qwen3-vl:8b` is reserved for explicit vision
selection. The router owns provider selection, privacy checks, concurrency,
timeouts, and response normalization. Local inference needs no cloud key.
FreeLLMAPI remains available only when the cloud provider and policy are
explicitly enabled. Private repository content, memory, and chat also require
the separate cloud private-context opt-in. Provider failures do not cause
silent cross-provider fallback.

See [Local inference and privacy](LOCAL_INFERENCE.md) for model IDs, environment
variables, resource limits, and the Windows smoke test.

## Phase 5 execution and recovery

The tester uses a fixed compile command in a subprocess with a minimal
environment, bounded output, a deadline, and direct-child termination on
cancellation or timeout. Executable repository unit tests remain disabled
without a stronger isolation backend; a temporary directory is not a
filesystem security boundary.

The bot's task orchestrators persist sanitized metadata in a shared local
SQLite file. Recovery marks unfinished tasks interrupted, treats uncertain
external actions as requiring reconciliation, and never restores an approval
or automatically replays an external action. This does not change the
existing disabled repository write handlers. See
[Execution and recovery](EXECUTION_AND_RECOVERY.md) for exact limits and the
opt-in Windows Ollama smoke test.

## Phase 6 authenticated test publishing

An opt-in Discord button flow binds a validated `!max_fix` proposal to the
requesting Discord ID, a configured test repository, exact branches, file,
payload digest, and expiry. The first enabled operation creates a branch and
pull request only. Branch-only task staging, remote-memory writes, merges, and
deployments remain disabled. Uncertain GitHub writes require read-only
reconciliation and never replay automatically. See
[Approval and publishing](APPROVAL_AND_PUBLISHING.md) for configuration and
recovery details.

## Verified learning system

- Record failure evidence from tests, tool errors, user corrections and CI results.
- Separate suspected causes from verified root causes.
- Turn a failure into a reusable lesson only when evidence supports the diagnosis.
- Store lesson scope, failure category, root cause, prevention rule, evidence, confidence and timestamps.
- Retrieve relevant lessons by repository/project and task category.
- Keep the root `AGENTS.md` concise; keep detailed lessons in a separate store and summarize only stable verified rules into agent instructions.
- Deduplicate lessons, track whether they help, and allow correction or deletion.
- Do not let untrusted repository content rewrite trusted global instructions.
- Do not allow the agent to rewrite its own production code without branch isolation, tests and review.

## GitHub development workflow

1. Present an interactive, paginated repository selector covering accessible personal and organization repositories.
2. Verify the runtime GitHub credential has required repository and organization permissions.
3. Inspect repository metadata, tree, instructions, dependencies, tests and deployment configuration.
4. Explain the plan and ask clarifying questions when requirements are ambiguous.
5. Create a task branch and implement a scoped patch.
6. Run repository-appropriate tests, lint, type checks and builds.
7. Inspect CI results and deployment-provider logs when authorized integrations exist.
8. Diagnose code failures separately from outages, quota exhaustion, secrets/configuration errors and provider failures.
9. Run a bounded repair loop and an independent review for meaningful changes.
10. Report changed files, checks, remaining risks and deployment status accurately.

Merging, protected-branch writes, production deployments, destructive actions and other consequential external changes require explicit approval.

## Bounded specialist roles

Implement specialists as task-scoped roles within the existing process, not as permanent services:
- Repository Scout: architecture and relevant code discovery
- Coding Agent: patch implementation on a branch
- Reviewer/Test Agent: independent diff review and validation
- CI/Deployment Investigator: inspect GitHub Actions and explicitly configured deployment providers

Invoke only roles relevant to the task. Apply a global task budget, per-role limits, timeouts and cancellation.

## Persistence and Railway safety

The current Max preferences and usage modules use local JSON files. The bot service has no attached Railway volume, so persistence across redeploys is not guaranteed. Audit available storage and backups before making long-term learning depend on local files. Prefer the least-cost durable option compatible with the actual deployment; do not add a paid database by default.

The FreeLLMAPI service has its own persistent volume and must remain separate from the bot's lifecycle. Do not modify its configuration as part of the initial Max migration.

## Rollout phases

1. Audit repository, production config, persistence and baseline command behavior.
2. Add backward-compatible configuration and task-aware model routing with unit tests.
3. Introduce Max identity and orchestrator boundaries without removing legacy commands.
4. Add scoped personal/project memory and verified learning.
5. Add repository picker and branch-based development workflow.
6. Add bounded specialist roles, CI diagnosis and approval enforcement.
7. Test using a separate Discord test bot and test repository; release only after regression checks.

## Acceptance criteria for the first implementation increment

- Existing configuration continues to work.
- Cloud inference reuses the existing API key; local inference needs no key.
- Task routing is configurable and testable without network access.
- Unknown profiles and provider failures produce bounded, understandable errors.
- No new paid service or background polling loop is introduced.
- Existing commands remain unchanged.
- No Railway variables or production deployments are changed.
