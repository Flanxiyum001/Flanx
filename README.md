# Max

A Discord coding assistant. Repository repair prepares and validates a patch.
Publishing a branch and pull request requires an authenticated Discord approval
and explicit configuration for one test repository. Publishing is off by default.

```
!max_fix owner/repo src/app.py add retry logic around the HTTP call
```

## Pipeline

1. Pull the target file from GitHub (default branch).
2. Read `brain_memory.txt` from that repo — Max's diary of past successes and failures.
3. Search the live web for current docs / known fixes (`ddgs`).
4. Baseline-run the current file in the free sandbox to capture a real error trace.
   (Steps 2–4 are independent, so Max runs them **concurrently** — the slowest one
   sets the pace instead of the sum of all three.)
5. Ask the LLM for a complete replacement file (raw code, no markdown fences).
6. Re-run the patched file in the sandbox. On a failure the **retry ladder** kicks in:
   one more attempt at temperature 0 (on `AI_RETRY_MODEL` if set) with the failure log
   attached. A failed patch makes no repository write.
7. On success, prepare a scoped approval request. A configured test repository
   can receive the patch on a new branch and pull request after authenticated
   approval. Memory updates remain disabled.

Repository repair is paused by default before source reaches the remote sandbox.
Set `MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT=true` only after deciding that this
repository's source, memory, and diagnostic context may enter that workflow.

All blocking work (PyGithub, sandbox HTTP, web search) runs in threads so the Discord
gateway never stalls.

## Max model routing

Max defaults to local Ollama with `qwen3:8b`. Task and role models, the Ollama endpoint, request limits, and optional FreeLLMAPI routing are configurable. Cloud use for source, memory, and Discord chat requires an explicit privacy setting. See [Local inference and privacy](docs/LOCAL_INFERENCE.md) for settings and a Windows smoke test.

## Task engine (Phase 2)

Ordinary chat requests now pass through `task_engine.py`: a normalized task is
created, checked against the capability allowlist, run with a deadline, and
returned as a structured outcome. The registered `general_chat` handler calls
the existing `generate_assistant_reply()` function, so `model_router.py` and
its configuration precedence remain in use. Task metadata is stored in SQLite
without raw prompts, source, results, or credentials. Interrupted tasks require
reconciliation after restart and are never automatically replayed. General chat
uses one attempt and a 120-second deadline. See
[Execution and recovery](docs/EXECUTION_AND_RECOVERY.md).

`task_engine.py` defines the task state transitions and a central capability
allowlist. Read operations and sandbox execution are distinct from writes,
deployments, purchases, bookings, external messages, and deletion. A registered
handler for a consequential action cannot run until a trusted caller supplies
an explicit approval matching the task, capability, target, and payload.
The test-repository approval flow uses Discord buttons and the same task
boundary. See [Approval and publishing](docs/APPROVAL_AND_PUBLISHING.md).

## Specialist workflow (Phase 3)

`specialists.py` registers planner, coder, reviewer, and tester roles through the
task engine. The planner returns at most eight flat steps. The coder proposes
changes to at most eight selected text files, which are applied only to a
temporary copy. The reviewer inspects the actual unified diff and reports
structured findings. The tester runs the named `compile` check in that copy
with time and output limits. Executable `unittest` checks are skipped until a
stronger isolation backend exists. At most two correction cycles are
allowed. Planner and coder adapters must be supplied by a trusted caller;
there is no automatic model call or Discord trigger for this workflow.

The temporary copy is file isolation, not an operating-system sandbox. Tests
are executable code and must come from a trusted repository. The workflow
returns a bounded diff and report without applying it to the primary tree.

`!max_fix` can present a Discord approval for one configured test repository.
`!update_repo` still creates a pending, scoped action without a publishing path.
Approval requests expire after five minutes and are not durable across restarts.
The old production write handlers remain disabled.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Discord bot: commands, prompts, PR flow |
| `free_sandbox.py` | OnlineCompiler.io execution + `brain_memory.txt` read/write |
| `task_engine.py` | In-process task state, capability checks, deadlines, retries and approvals |
| `task_store.py` | SQLite task metadata and restart recovery |
| `approval_publish.py` | Test-repository approval binding, publishing, and reconciliation |
| `specialists.py` | Scoped planner, coder, reviewer, tester workflow and temporary workspace |
| `requirements.txt` | `discord.py`, `aiohttp`, `openai`, `PyGithub`, `requests`, `ddgs` |
| `knowledge.md` | Repo-level directive that steers Freebuff/Codebuff agents handling `tasks/*.md` |
| `usage.py` | Persistent LLM token/cost counters and monthly sandbox-quota tracking |
| `prefs.py` | Per-user companion preferences: timezone + personal notes (JSON) |
| `integrations/` | Pluggable integration modules (`clock`, `notes`, `_template`) with auto-discovery |

## GitHub repository workflows

Mention the bot account or prefix a message with `max` for these actions:

- `max repos` — list the first page of repositories visible to `GITHUB_TOKEN`; `max repos 2` opens the next page.
- `max use owner/repo` — verify access and save a per-user default repository.
- `max current repo` — show your saved repository.
- `max inspect owner/repo` — read repository metadata, root entries, `AGENTS.md` (if present), and `README.md`; this does not execute code or change files.

Repository discovery is read-only. The token can only list repositories permitted by its GitHub scopes and organization policies. Per-user settings are stored in the existing local JSON preferences file and may not survive redeploys without persistent storage.

## Commands

- `!max_fix <owner/repo> <path/to/file.py> <what to change>` — validate a patch and request approval for a configured test repository (requires repository privacy opt-in)
- `!update_repo <owner/repo> <path/to/file.py> <instruction>` — prepare a pending task manifest action
- `!max_publish_status <task-id>` — authorized, read-only reconciliation of a test publishing operation
- `!max_brain <owner/repo>` — print what Max remembers for that repository
- `!max_stats` — LLM tokens used, estimated cost, and this month's sandbox quota

All of these also work as plain chat (`max …` or a bot mention) — see **Chat mode** below.

For existing users, the `mex` and `flanx` chat triggers and their matching
`!mex_*` and `!flanx_*` command spellings remain accepted. They are compatibility
inputs; the bot identifies itself as Max in replies and new task output.

## Chat mode (no prefix needed)

Talk to Max in plain chat — it answers when you **mention the bot account** or start a
message with `max` (case-insensitive; `hey max` and `max,` also work):

| You type | Max does |
| --- | --- |
| `max help me with src/app.js in owner/repo — add retry logic` | runs the full `!max_fix` repair pipeline |
| Mention the bot, then say `fix src/app.js in owner/repo: upgrade to argon2id` | same pipeline, via mention |
| `max search python 3.14 changes for me` | live web search, results posted back |
| `max what do you remember about owner/repo` | prints that repo's brain memory |
| `max stats` / `max how many tokens have you used` | usage: tokens, cost, sandbox quota |
| `max yo sup` / `max how are you` / `max who are you` | friendly companion replies (no fix-request nagging) |
| `max set timezone Asia/Kolkata` | saves **your** timezone (per Discord user, remembered) |
| `max what time is it` | current time in your saved timezone (or `max time in America/New_York`) |
| `max remember <anything>` / `max show notes` | personal notes, stored per user |
| `max help` | usage card |

Parsing pulls out the `owner/repo` and the file path automatically; set
`FLANX_DEFAULT_REPO=owner/repo` and you can omit the repo. **Casual chat never gets the
help card** — if a request is a fix but lacks details, Max asks for just that one
thing, in one friendly line. `!`-prefix commands keep working, and plain messages that
don't address Max are ignored.

### Companion behaviour

Max recognises greetings (`yo sup`, `good morning`), `how are you`, `who are you`,
`thanks`, time/timezone requests and personal notes — all answered conversationally
instead of being treated as broken GitHub commands. Timezones and notes are stored per
Discord user in `flanx_prefs.json` (git-ignored; point `FLANX_PREFS_FILE` at a
persistent path to keep them across Railway redeploys). Notes are capped at 50 per
user so the file stays small.

## Adding your own integrations

Max has a **drop-in plugin system** — new capabilities need no changes to `main.py`:

1. Copy `integrations/_template.py` to `integrations/<name>.py` (any name **without** a
   leading underscore; underscore-prefixed files are ignored so the template stays
dormant).
2. Fill in the contract:
   ```python
   NAME = "weather"                    # slug used by the router
   DESCRIPTION = "current weather"     # shown by `max integrations`
   USAGE = ["max weather in Pune"]   # example phrase
   def match(text: str) -> dict | None  # claim a message -> payload, else None
   async def handle(event, payload) -> str  # do the work, return the reply
   ```
3. Restart the bot. `max integrations` now lists it, and any chat message its
   `match()` claims is routed to `handle()` — search/stats/fix intents keep priority
   over your claims only where they are checked first, so keep `match()` anchored and
   specific.

Rules the registry enforces: modules missing `NAME`/`match`/`handle` are skipped with a
console warning instead of crashing the bot, blocking I/O inside `handle()` belongs in
`asyncio.to_thread(...)`, and the returned string is posted verbatim (keep it English,
under 2000 chars). Shipped examples: **`clock`** (timezone + time) and **`notes`**
(personal notes) — both were originally hard-wired in `main.py` and now live as
modules, proving the contract.

## Cloud staging with `!update_repo`

`!update_repo` prepares a manifest proposal:

1. `!update_repo username/my-app src/auth.js "Upgrade encryption to argon2id"`
2. Max reads the target repository and builds `tasks/update-job-<id>.md` with
   the objective and a definition-of-done checklist.
3. The task engine returns a pending approval request bound to the repository,
   branch, manifest path, and exact payload. No branch or manifest is created.

## Environment variables

Set these in Railway → Variables (or Freebuff Settings → Environment):

| Key | Required | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | yes | Bot token |
| `AI_API_KEY` | cloud only | Key for the optional OpenAI-compatible endpoint |
| `GITHUB_TOKEN` | yes | Fine-grained PAT with contents + pull-requests write on the target repo |
| `ONLINE_COMPILER_API_KEY` | yes | Free key from the OnlineCompiler.io dashboard (API Keys page) |
| `AI_BASE_URL` | no | default `https://freellmapi.co/v1` |
| `AI_MODEL` | no | legacy model ID override; local default is `qwen3:8b` |
| `SANDBOX_COMPILER` | no | default `python-3.14` |
| `ONLINE_COMPILER_BASE_URL` | no | default `https://api.onlinecompiler.io` |
| `FLANX_DEFAULT_REPO` | no | default `owner/repo` used by chat mode when a message omits it |
| `FLANX_FIX_ATTEMPTS` | no | retry-ladder depth, `1`–`3` (default `2` = first try + one retry) |
| `AI_RETRY_MODEL` | no | second model used for retries; unset = same model at temperature 0 |
| `MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT` | no | default false; explicit opt-in for the remote repository repair workflow |
| `MAX_INFERENCE_PROVIDER` | no | `local` (default) or `cloud` |
| `MAX_INFERENCE_POLICY` | no | `local_only` (default) or `cloud_allowed` |
| `MAX_ALLOW_CLOUD_PRIVATE_CONTEXT` | no | default false; additional opt-in for cloud inference with chat, source, or memory |
| `MAX_TASK_DB` | no | local SQLite task metadata path (default `.max_state/tasks.sqlite3`) |
| `MAX_CHECK_TIMEOUT_SECONDS` | no | local compile-check deadline (default 10 seconds, maximum 30) |
| `MAX_CHECK_OUTPUT_BYTES` | no | captured check output (default 4000, maximum 16000) |
| `AI_INPUT_PRICE_PER_M` | no | USD per 1M prompt tokens for the cost counter (default `0` = free pool) |
| `AI_OUTPUT_PRICE_PER_M` | no | USD per 1M completion tokens (default `0` = free pool) |
| `FLANX_USAGE_FILE` | no | counter file path (default `flanx_usage.json`) |
| `FLANX_PREFS_FILE` | no | companion preferences path (default `flanx_prefs.json`) |

The `FLANX_*` and `MAX_*` keys and existing storage filenames remain unchanged so
current installations can use their settings and data without migration.

## Verified lessons: SQLite storage, safe migration, and rollback

The `max lesson` and `max lessons` interfaces use SQLite only.
`MAX_LESSONS_DB` defaults to `max_lessons.sqlite3`. They **never
automatically fall back** to `MAX_LESSONS_FILE` (`max_lessons.json`), and
startup does not migrate or delete legacy JSON. A missing database is new
empty storage; an existing invalid/unreadable SQLite file causes an error.
The built-in `sqlite3` library requires no new dependency.

**Railway volume prerequisite:** Provision and attach a *real* persistent
volume to the service before cutover, verify the mount path and writable
filesystem, and configure `MAX_LESSONS_DB` to a file on the mount.
`/data/max_lessons.sqlite3` is only an example if the service really has
a Railway volume mounted at `/data`. An absolute path or `/data` prefix
does not prove persistence. Use one writer service against this local SQLite
database; do not share it through an unsuitable network volume.
Preferences remain JSON: configure `FLANX_PREFS_FILE` (for example
`/data/flanx_prefs.json` on a verified volume) separately. This change
does not modify `prefs.py`.

**Before cutover (bot stopped, no concurrent lesson writes):**

1. Identify the original JSON file, set `MAX_LESSONS_FILE` to that file,
   set `MAX_LESSONS_DB` to the destination on the verified Railway mount,
   and confirm both values. Preserve independent backups of any existing
   SQLite destination (including SQLite sidecar files if present).
2. Back up the original JSON: `cp -p "$MAX_LESSONS_FILE" "$MAX_LESSONS_FILE.backup"`.
   Verify the backup exists and is readable. Never modify the original to
   resolve validation errors; work on a *copy*.
3. Execute `python learning.py --migrate-json "$MAX_LESSONS_FILE"`.
   Migration validates *all* source records and ID/signature collisions
   before making import writes. It runs as a single SQLite transaction
   without retention pruning. It reports `source_records`,
   `distinct_signatures`, `inserted`, `updated`,
   `skipped_identical_source_duplicates`, and `conflicting_records`.
   Identical repeated records count as explicit skips; divergent duplicates
   cause a clear error. Import rejects ID or signature collisions with
   *different* destination identities rather than overwriting them.
4. The runtime keeps at most **500** lessons. Migration refuses to run if
   the source has more than 500 distinct signatures or if existing destination
   records plus new imports would exceed 500. **Nothing is pruned during
   migration.** Preserve the original backup, separately archive excess
   lessons, then prepare and review a smaller *copy* of the source or use a
   verified empty destination. A blocked import is not a successful migration.
5. Inspect the migration counts. To verify the actual target:
   `python -c "import os,sqlite3; c=sqlite3.connect(os.environ['MAX_LESSONS_DB']); print(c.execute('SELECT COUNT(*) FROM lessons').fetchone()[0]); c.close()"`.
   Compare the count with the distinct imported records and any existing
   destination records; allow only documented identical source duplicates.
   Check representative `owner/repo` scopes and evidence content, and
   ensure the original JSON backup is untouched. Only then start the bot
   using `MAX_LESSONS_DB` on the actual persistent volume.

**Export and rollback (stop writes first):** Export the complete current
SQLite state to a **new** legacy-format JSON file, read-only against SQLite:

```sh
python learning.py --export-json /safe-backup/max-lessons-from-sqlite.json
```

The command refuses an existing destination and never overwrites the
SQLite database. Back up the destination first and specify
`--overwrite-export` **only if** replacing that file is intentional.
Verify the exported `lessons` count and representative records. To roll
back to the older JSON-only code, stop the bot, back up SQLite, and
**explicitly place the verified latest export at the exact
`MAX_LESSONS_FILE` path** used by the older revision (after separately
backing up the prior JSON there). Then revert the application revision
and its storage configuration. Restoring older code and an old JSON
backup alone **will lose lessons added since cutover**. Do not restart
older code until the export and file placement are verified.

SQLite serializes writers using `BEGIN IMMEDIATE` and a 30-second busy
timeout. Connection errors, malformed databases, failed writes, and
migration conflicts surface as errors rather than empty history. This
provides transaction safety, not verified Railway durability, backups,
or compatibility with multi-replica deployments.

## Setup

1. `pip install -r requirements.txt`
2. Add the four required variables above.
3. In the Discord developer portal enable the **Message Content Intent**
   (Settings → Bot → Privileged Gateway Intents) — `main.py` requests it.
4. `python main.py` (Railway: start command `python main.py`).

## Notes on the sandbox API

The OnlineCompiler.io docs describe the **native** REST API, which differs from the
commonly circulated snippet:

- Base URL is `https://api.onlinecompiler.io`, endpoint `POST /api/run-code-sync/`.
- Auth is the raw key in `Authorization: <key>` — **no `Bearer` prefix**.
- The body is `{"compiler": "python-3.14", "code": "...", "input": "..."}` (a single
  `compiler` id, not separate `language`/`version`).
- The response returns `output` / `error` / `exit_code` / `time`
  (`free_sandbox.py` also accepts the Piston-compatible `run.stdout` shape).

Free plan (verified on their pricing page): 100,000 runs/month, 30 submissions/minute,
2 concurrent executions, 30s execution timeout, 100KB code+stdin, 64KB output per stream,
**no outbound network inside the sandbox**, no credit card.

Docs: <https://onlinecompiler.io/docs> · Pricing: <https://onlinecompiler.io/pricing>

### LLM endpoint

Local Ollama is the default. See [Local inference and privacy](docs/LOCAL_INFERENCE.md) for local settings and explicit cloud routing.

## English-only output

Some configured models may occasionally answer in Chinese.
Max is English 24/7 through two layers:

1. **Prompt rule** — the system prompt demands English and forbids new non-English
   comments/docstrings, while existing file comments stay byte-for-byte untouched.
2. **Runtime guard** — `is_non_english()` scans every patch for CJK characters that are
   not already present in the source file; a hit triggers one bilingual "English only"
   re-ask before the patch is accepted. Anything that still slips through is caught by
   the sandbox gate before any repository action is prepared.

Discord messages, PR titles/bodies and `brain_memory.txt` entries are written by the bot
itself and are always English.

## Usage counters, cost and the retry ladder

**Counters** (`!max_stats` or `max stats`) are maintained in `flanx_usage.json`
(git-ignored, written atomically, thread-safe):

- **LLM:** call count, prompt/completion/total tokens, per-model breakdown, and
  estimated USD cost. Set `AI_INPUT_PRICE_PER_M` and `AI_OUTPUT_PRICE_PER_M`
  for an explicitly configured cloud provider; local inference records tokens
  when Ollama reports them.
- **Sandbox:** runs used this calendar month against the free plan's 100,000 quota,
  with pass rate and remaining balance; counters reset on month rollover.
- The file lives on the host, so on Railway a redeploy resets it unless you point
  `FLANX_USAGE_FILE` at a persistent volume.

**Retry ladder:** when a generated patch fails sandbox validation, Max regenerates
at temperature 0 (and on `AI_RETRY_MODEL` when configured) with the failure log,
up to `FLANX_FIX_ATTEMPTS` total attempts. This phase does not write failures to
the remote brain.

## The brain

`brain_memory.txt` in the target repository is legacy long-term memory. Max can
read it for repair context after the repository privacy opt-in. Automatic remote
memory updates are disabled until they can pass the same scoped approval gate.
