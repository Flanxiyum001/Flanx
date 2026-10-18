# Project Max Implementation Plan

## Product goals

Project Max is a friendly, personalized Discord-first assistant that improves from verified experience while keeping the existing Railway deployment and operating costs under control.

## Non-negotiable constraints

- Keep `main` deployable and preserve the existing Railway production service during migration.
- Do not change Railway variables or deploy automatically during development.
- Reuse the existing OpenAI-compatible FreeLLMAPI gateway and unified API key.
- Keep the existing bot process and avoid additional paid services by default.
- Preserve existing commands and features until replacements are tested.
- Bound model calls, retries, tool calls, execution time and context size.
- Require explicit approval for consequential external actions.
- Never store secrets in memory, lessons, logs or repository documentation.

## Target architecture

Keep modules in the existing process initially; do not split them into separate Railway services without evidence that it is necessary.

- Discord gateway and command router
- Assistant orchestrator and friendly Max persona
- Task-aware model router
- Capability registry for GitHub, research and sandbox execution
- Personal memory and scoped project/repository knowledge
- Verified learning engine
- Permission and approval policy
- Usage metrics, structured logs and bounded execution

## Task-aware model routing

Use the existing unified FreeLLMAPI key and configured `AI_BASE_URL`. Do not create per-provider secrets in Max.

The task router should select a configured FreeLLMAPI model or routing profile based on task type:
- Casual conversation: fast profile
- General questions and planning: balanced profile
- Coding and debugging: coding-focused profile
- Review and validation analysis: reliable or strong reasoning profile

Profile names are configuration, not assumptions. Verify them against the live gateway before enabling them. Keep a safe default model/profile and a manual override. If a profile is unavailable or rate-limited, use bounded fallback behavior; do not retry indefinitely.

Record the requested task category, selected profile/model, token usage when available, latency, provider failures and objective task outcome. Do not log API keys or unnecessary private prompt content.

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

The current Flanx preferences and usage modules use local JSON files. The bot service has no attached Railway volume, so persistence across redeploys is not guaranteed. Audit available storage and backups before making long-term learning depend on local files. Prefer the least-cost durable option compatible with the actual deployment; do not add a paid database by default.

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
- The unified API key is reused; no key is hard-coded.
- Task routing is configurable and testable without network access.
- Unknown profiles and provider failures produce bounded, understandable errors.
- No new paid service or background polling loop is introduced.
- Existing commands remain unchanged.
- No Railway variables or production deployments are changed.
