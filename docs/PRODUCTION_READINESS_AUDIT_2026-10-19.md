# Max — production readiness audit and handoff

**Date:** 2026-10-19 (Asia/Kolkata)  
**Scope:** Repository `Flanxiyum001/Max`, default branch `4dd7a2cf4c7b5ccd0d34ae2f8e51fbeb89c07e2f` at the start of this audit; README, tests, CI and available read-only Railway metadata/log categories.  
**Disposition:** Draft PR #15 is a **review candidate only**. No production deployment, restart, secret change, live provider call or state migration was performed as part of this audit.

## Prioritized findings

| Severity | Confirmed evidence and impact | Remediation in PR #15 | Still limited |
|---|---|---|---|
| Critical | `main.py` and `integrations/repos.py` allowed Discord users who could address the bot to access GitHub-token-backed repository listing, inspection, PR creation and staging without a trusted operator check. | New `authz.py` fail-closed Discord user-ID allowlist for GitHub routes, prefix commands, and shared repository lessons. | Owner must configure `MAX_GITHUB_ALLOWED_USER_IDS` before these features will work following deployment. Reduce GitHub token scopes and repository access separately. |
| High | `main.py` treated a bare GitHub URL or `owner/repo` as a file-repair prompt and requested a file path, even for read-only review. | `repo_review.py` parses URLs/.git/trailing slashes, offers clarification and uses read-only sampled tree/file inspection for `review`/`analyze`. | Sampling is bounded, and the model's suggestions need human review. Private-source review is blocked by default. |
| High | `main.py` and `model_router.py` fell back to unavailable `qwen-2.5-coder-32b`, and `AI_BASE_URL` could point to an unverified public endpoint. | Explicit self-hosted `AI_BASE_URL` required, default model `auto`, stale-ID guard, safe status messages, bounded SDK timeout/retry. | Owner must confirm their **specific** FreeLLMAPI instance advertises and can actually serve `auto` or the selected model. |
| High | `free_sandbox.py`, `main.py`, `integrations/repos.py` could echo HTTP error bodies/exception strings, potentially containing secrets and private prompts. | Redacted/categorized error messages, strict response parsing, input-size limits, malformed-result rejection, local guarded exception handling. | Provider account/quota/authentication and live execution remain unverified. |
| High | Automatic repair outcome logging wrote `brain_memory.txt` to a repository's default branch outside PR review. | Disabled automatic brain writes, block direct `main`/`master` memory updates; write patches only to new PR branches; reject stale source SHA and concurrent edit conflicts. | GitHub may still retain a newly created empty feature branch if later PR creation fails. |
| High | Automated repair could retrieve **private repository source** and forward it to external AI and code execution providers without a separate approval for that disclosure. Private brain files could also be printed into Discord. | Private-source automatic repair, private tree enumeration, and private brain text display now fail closed before reading or transmitting content; regression tests assert external providers are never called. | An explicitly approved secure private-source workflow is required; no generic consent override was added. |
| High | A public repository source file can still accidentally contain hard-coded credentials that would be forwarded to AI or sandbox services. | Added a conservative preflight check blocking known token prefixes and likely literal credentials before outbound processing. | Heuristic detection is not comprehensive; use repository secret scanning, reviews and token rotation for exposed credentials. |
| High | Generated code previously counted as validated after only a standalone external sandbox run. | Python syntax parse for Python patches, explicit unchanged-output rejection, supported compiler ID selection, and honest **single-file smoke check** labels. | Full repository builds and integration tests must be run independently; no built-in isolated multi-file workspace. |
| Medium | `main.py` said “sandbox green” without an actual health probe. | Neutral status wording with unverified health stated explicitly. | No live health test was authorized. |
| Medium | `main.py` could mishandle empty/malformed gateway completions; long repository review and ordinary conversational content could exceed Discord message limits or break fenced formatting. | Guard result shape and split reviews under Discord limits, preserving Markdown fence parity. | Other pre-existing command formatting is not fully rewritten. |
| Medium | Malformed token-usage metadata could raise a conversion exception *after* a valid AI result, discarding the user-facing answer. | Broadened narrowly scoped usage-recording error handling and added an offline regression verifying a successful response is preserved. | Actual paid provider usage is not independently verified, and malformed counters are omitted rather than repaired. |
| Medium | `usage.py` claimed the number of free sandbox calls remaining from an internal counter. | Explicitly label only local attempts and optional configured allowance **estimates**. | Provider's documented pricing page and technical limits page disagree on the free monthly ceiling. Verify the owner's account through the provider dashboard / authenticated usage endpoint. |
| Medium | Public repo files could contain credentials or adversarial instructions and be forwarded to the model. | Bounded source-file sampling, excludes common secret paths, best-effort redaction, and explicit untrusted-content instructions. | Redaction and prompt-injection defenses are not complete. Require least-privilege credentials and independent review. |
| Medium | The service's state durability had previously been unverified. | Existing `storage_admin.py` and tests were inspected, no production data changes. | Railway now lists a `/data` volume, but no verified production backup, restored history, actual state counts, or controlled restart persistence was inspected during this audit. |
| Low | Missing Git ignore rules for SQLite, legacy lessons and dotenv files. | Added rules in `.gitignore` to reduce accidental commits of data/credentials. | Existing tracked sensitive files, if any, need a separate history review. |
| Low | `requirements.txt` uses minimum version constraints, not exact locks. | Left as-is to avoid untested dependency upgrades. | Reproducible dependency pinning and supply-chain scanning remain future work. |

## Architectural boundaries

The Discord gateway stays in one process, with asynchronous OpenAI requests and threaded synchronous GitHub/sandbox operations. `integrations/` discovers command handlers. `repo_review.py` reads GitHub metadata and small text samples, while `authz.py` protects GitHub operations. `learning.py` stores verified lessons in SQLite; `prefs.py` and `usage.py` store JSON state. `storage_admin.py` offers inventory and source-preserving backup/verification.

This architecture keeps existing Railway Hobby infrastructure and adds **no paid service, database or dependency**. Repository content and user instructions are not trust-equivalent; bot permissions stay separate from provider credentials.

## Testing and evidence

The feature-branch GitHub Actions workflow runs:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

The new `tests/test_production_readiness.py` covers private-source blocking and secret-bearing source preflight, a custom default-branch write guard, malformed usage data, long conversational replies, and the original Axiom GitHub URL, review vs repair, URL and path validation, real mocked GitHub file/tree retrieval, private repository guard, credential redaction, GitHub operator access, stale SHA branch writes, model routes and protected error reporting, OnlineCompiler HTTP behaviors, runtime identifiers, malformed provider responses, local quota wording, and Discord message splitting. Existing storage regression tests remain enabled.

As of commit `3622c6749bced9b5807421bc257f0a2cb1ecbfa2`, [GitHub Actions run 39166112195](https://github.com/Flanxiyum001/Max/actions/runs/39166112195) ran **122 unit tests: 122 passed, 0 failed**, and `python -m compileall -q .` passed. The final PR head must still be checked after documentation updates.

**Only GitHub Actions on the latest PR commit can establish the final test count and success.** No paid LLM call, authenticated sandbox execution, Discord live bot trial, or production restart was run.

## External protocol verification

- OnlineCompiler.io docs: https://onlinecompiler.io/docs — native synchronous `POST /api/run-code-sync/`, plain API key in `Authorization` header, `compiler`/`code`/`input` JSON; supports `python-3.14`, `typescript-deno`, `go-1.26`, `ruby-4.0`; **no Node.js native compiler**. The provider's pricing page and technical docs currently disagree on monthly free limits.
- FreeLLMAPI REST API docs: https://github.com/tashfeenahmed/freellmapi/blob/main/docs/en/api/01-rest-api.md — OpenAI-compatible `/v1/chat/completions`, model `auto` and `GET /v1/models`. This is vendor documentation, **not** evidence that the user's Railway instance accepts the current key or supports every catalog entry.

## Railway observations and required variable names

During this audit Railway described project `awake-truth`, service `Flanx`, environment `production`, **one replica**, volume `max-state` mounted at `/data`, and a successful deployment from `main` with commit `4dd7a2cf4c7b5ccd0d34ae2f8e51fbeb89c07e2f`. There were no staged changes in the environment. These observations are **read-only management metadata**, not a validated application test or data recovery check.

Variable names required to operate or configure this PR (never disclose values):

- `DISCORD_TOKEN`, `GITHUB_TOKEN`, `AI_API_KEY`, `AI_BASE_URL`, optional `AI_MODEL`.
- **New:** `MAX_GITHUB_ALLOWED_USER_IDS` — comma-separated trusted Discord numeric IDs, with no default access.
- `ONLINE_COMPILER_API_KEY`, optional `ONLINE_COMPILER_BASE_URL`, `SANDBOX_COMPILER`, `SANDBOX_MONTHLY_QUOTA`.
- `MAX_LESSONS_DB`, `MAX_LESSONS_FILE` (explicit legacy import only), `FLANX_PREFS_FILE`, `FLANX_USAGE_FILE`.
- Optional: `MAX_MODEL_CHAT`, `MAX_MODEL_GENERAL`, `MAX_MODEL_RESEARCH`, `MAX_MODEL_CODING`, `MAX_MODEL_REVIEW`, `AI_RETRY_MODEL`, `FLANX_FIX_ATTEMPTS`.

## Operator preflight and rollback

1. Keep the PR draft/unmerged and confirm its final successful CI head SHA.
2. Before approval, inspect the configured **names only** of Railway variables; review the current self-hosted `/v1/models` catalog **privately**. Never paste tokens in an issue, PR, Discord, or ChatGPT.
3. Configure trusted IDs for `MAX_GITHUB_ALLOWED_USER_IDS` only after approval, and limit `GITHUB_TOKEN` to intended repositories/permissions. Without the allowlist, GitHub actions will deliberately refuse requests.
4. Independently verify storage inventory, off-container backups, manifest hashes, actual `/data` paths and restoration history before changing persistence or restarting the service. The observed mount proves only that a volume is attached, not that older state is recovered.
5. In an explicitly approved test environment, verify ordinary chat, Axiom URL clarification, public read-only review, denied/allowed operator requests, test sandbox execution with harmless code, HTTP errors, and provider routing. Respect the existing free quotas.
6. If separately approved for deployment, capture the old commit and config **before** replacing it; inspect deploy logs and CI, use an appropriate rollback mechanism and preserve volume state. Reverting the code does **not** restore overwritten data and must never silently reset a database.

**Acceptance boundary:** This PR is a code hardening candidate. End-to-end reliability, authentication, live provider model routing, live sandbox operation, historical data recovery, and restart-tested production persistence remain unverified.
