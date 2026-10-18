# Max

Max is your self-correcting Discord coding companion. It pulls a file from GitHub, recalls past
lessons, searches the live web, proves the fix inside a **free** OnlineCompiler.io
sandbox, learns from the result, and opens a pull request — all from a Discord command.

```
!flanx_fix owner/repo src/app.py add retry logic around the HTTP call
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
   attached. Only after all attempts fail is the lesson written to the brain and no
   branch pushed.
7. On success, write the lesson to the brain, push a branch and open a PR.

All blocking work (PyGithub, sandbox HTTP, web search) runs in threads so the Discord
gateway never stalls.

## Project Max model routing

Max supports task-specific model/profile identifiers through the existing OpenAI-compatible
FreeLLMAPI gateway. The current `AI_MODEL` setting remains the compatibility fallback.
Configure task profiles only after verifying they exist in your FreeLLMAPI dashboard:

- `MAX_MODEL_CHAT` — casual conversation
- `MAX_MODEL_GENERAL` — general questions and planning
- `MAX_MODEL_RESEARCH` — research and synthesis
- `MAX_MODEL_CODING` — code generation and debugging
- `MAX_MODEL_REVIEW` — code review and verification

Each value may be a model ID or a gateway routing profile accepted by your FreeLLMAPI
instance (for example, a configured `auto:coding` profile). Unset values fall back to
`AI_MODEL`; no new API keys are required. Set `AI_BASE_URL` to the self-hosted gateway
URL and keep `AI_API_KEY` as a Railway secret. Do not put credentials in source control.

The routing module is network-independent and can be tested with:
`python -m unittest discover -s tests -v`.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Discord bot: commands, prompts, PR flow |
| `free_sandbox.py` | OnlineCompiler.io execution + `brain_memory.txt` read/write |
| `requirements.txt` | `discord.py`, `openai`, `PyGithub`, `requests`, `ddgs` |
| `knowledge.md` | Repo-level directive that steers Freebuff/Codebuff agents handling `tasks/*.md` |
| `usage.py` | Persistent LLM token/cost counters and monthly sandbox-quota tracking |
| `prefs.py` | Per-user companion preferences: timezone + personal notes (JSON) |
| `integrations/` | Pluggable integration modules (`clock`, `notes`, `_template`) with auto-discovery |

## GitHub repository workflows

Use the chat trigger by mentioning **Max** or starting a message with `max` (the old `flanx` trigger remains supported for compatibility):

- `max repos` — list the first page of repositories visible to `GITHUB_TOKEN`; `max repos 2` opens the next page. (`flanx repos` still works.)
- `max use owner/repo` — verify access and save a per-user default repository.
- `max current repo` — show your saved repository.
- `max inspect owner/repo` — read repository metadata, root entries, `AGENTS.md` (if present), and `README.md`; this does not execute code or change files.

Repository discovery is read-only. The token can only list repositories permitted by its GitHub scopes and organization policies. Per-user settings are stored in the existing local JSON preferences file and may not survive redeploys without persistent storage.

## Commands

- `!flanx_fix <owner/repo> <path/to/file.py> <what to change>` — full repair cycle
- `!update_repo <owner/repo> <path/to/file.py> <instruction>` — stage a task manifest on a
  fresh branch for an external agent session to execute
- `!flanx_brain <owner/repo>` — print what Max remembers for that repository
- `!flanx_stats` — LLM tokens used, estimated cost, and this month's sandbox quota

The legacy `!flanx_*` commands remain available; for plain chat use `max …` or mention `@Max` — see **Chat mode** below.

## Chat mode (no prefix needed)

Talk to Max in plain chat — it answers when you **mention it** (`@Max …`) or start a
message with the word `max` (case-insensitive; `hi max`, `hey max`, `hello max`, and `max,` work). The old `flanx` trigger remains a backward-compatible alias:

| You type | Max does |
| --- | --- |
| `max help me with src/app.js in owner/repo — add retry logic` | runs the full `!flanx_fix` repair pipeline |
| `@Max fix src/app.js in owner/repo: upgrade to argon2id` | same pipeline, via mention |
| `max search python 3.14 changes for me` | live web search, results posted back |
| `max what do you remember about owner/repo` | prints that repo's brain memory |
| `max stats` / `max how many tokens have you used` | usage: tokens, cost, sandbox quota |
| `hi max` / `max how are you` / `max who are you` | friendly companion replies (no fix-request nagging) |
| `max set timezone Asia/Kolkata` | saves **your** timezone (per Discord user, remembered) |
| `max what time is it` | current time in your saved timezone (or `flanx time in America/New_York`) |
| `max remember <anything>` / `max show notes` | personal notes, stored per user |
| `max help` | usage card |

Parsing pulls out the `owner/repo` and the file path automatically; set
`FLANX_DEFAULT_REPO=owner/repo` and you can omit the repo. **Casual chat never gets the
help card** — if a request is a fix but lacks details, Max asks for just that one
thing, in one friendly line. `!`-prefix commands keep working, and plain messages that
don't address Max are ignored.

### Companion behaviour

Max recognises greetings (`hi max`, `yo sup`, `good morning`), `how are you`, `who are you`,
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
   DESCRIPTION = "current weather"     # shown by `flanx integrations`
   USAGE = ["flanx weather in Pune"]   # example phrase
   def match(text: str) -> dict | None  # claim a message -> payload, else None
   async def handle(event, payload) -> str  # do the work, return the reply
   ```
3. Restart the bot. `flanx integrations` now lists it, and any chat message its
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

Instead of running heavy work on the Railway box, Max can **stage** it:

1. `!update_repo username/my-app src/auth.js "Upgrade encryption to argon2id"`
2. Max branches off the default branch (`flanx-evolution-<id>`), writes
   `tasks/update-job-<id>.md` containing your objective plus a definition-of-done
   checklist, commits it to that branch, and replies with a Discord embed linking the
   **branch** and the **manifest file** on GitHub.
3. An agent session (Freebuff Cloud with the repo connected, or Codebuff) reads the
   manifest together with `knowledge.md`, applies the change, validates it, and opens the
   PR per the checklist.

The bot also works in the other direction: `!flanx_fix` does the patching itself using
the free OnlineCompiler.io sandbox, so nothing depends on an external trigger.

> **Honest scope note:** what is verified is the Git staging (branch + manifest + GitHub
> links), the `knowledge.md` convention (documented for Codebuff, which Freebuff is built
> on), and Freebuff's "Connect a repository" entry point. What is **not** publicly
> documented is an automatic push→agent→PR webhook, so the embed deliberately does not
> promise one — it tells you where the task is staged and how to run it.

## Environment variables

Set these in Railway → Variables (or Freebuff Settings → Environment):

| Key | Required | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | yes | Bot token |
| `AI_API_KEY` | yes | Key for the OpenAI-compatible endpoint |
| `GITHUB_TOKEN` | yes | Fine-grained PAT with contents + pull-requests write on the target repo |
| `ONLINE_COMPILER_API_KEY` | yes | Free key from the OnlineCompiler.io dashboard (API Keys page) |
| `AI_BASE_URL` | no | default `https://freellmapi.co/v1` |
| `AI_MODEL` | no | default `qwen-2.5-coder-32b` |
| `SANDBOX_COMPILER` | no | default `python-3.14` |
| `ONLINE_COMPILER_BASE_URL` | no | default `https://api.onlinecompiler.io` |
| `FLANX_DEFAULT_REPO` | no | default `owner/repo` used by chat mode when a message omits it |
| `FLANX_FIX_ATTEMPTS` | no | retry-ladder depth, `1`–`3` (default `2` = first try + one retry) |
| `AI_RETRY_MODEL` | no | second model used for retries; unset = same model at temperature 0 |
| `AI_INPUT_PRICE_PER_M` | no | USD per 1M prompt tokens for the cost counter (default `0` = free pool) |
| `AI_OUTPUT_PRICE_PER_M` | no | USD per 1M completion tokens (default `0` = free pool) |
| `FLANX_USAGE_FILE` | no | counter file path (default `flanx_usage.json`) |
| `FLANX_PREFS_FILE` | no | companion preferences path (default `flanx_prefs.json`) |


## Verified lessons: SQLite storage, safe migration, and rollback

The `flanx lesson` and `flanx lessons` interfaces use SQLite only.
`MAX_LESSONS_DB` defaults to `max_lessons.sqlite3`. They **never
automatically fall back** to `MAX_LESSONS_FILE` (`max_lessons.json`), and
startup does not migrate or delete legacy JSON. A missing database is new
empty storage; an existing invalid/unreadable SQLite file causes an error.
The built-in `sqlite3` library requires no new dependency.

**Railway volume prerequisite:** First inventory, extract, and independently
verify recoverable copies of existing data *outside* the running ephemeral
container. Only then attach a real volume, restore the checked copies to that
mount, and configure `MAX_LESSONS_DB` to a file on it.
`/data/max_lessons.sqlite3` is only an example if the service really has
a Railway volume mounted at `/data`. An absolute path or `/data` prefix
does not prove persistence. Use one writer service against this local SQLite
database; do not share it through an unsuitable network volume.
Preferences remain JSON: configure `FLANX_PREFS_FILE` (for example
`/data/flanx_prefs.json` on a verified volume) separately. This change
does not modify `prefs.py`.

**Storage preflight (no production changes without approval):**

The proposed `storage_admin.py` utility inventories all four stores and
makes a validated offline backup without printing lesson text, notes, or usage
values:

```sh
python storage_admin.py --inventory
# Explicitly classify each missing store only after reviewing its inventory/history.
python storage_admin.py --backup-dir /safe/new-backup-dir \
  --allow-missing lessons_db \
  --allow-missing legacy_lessons_json \
  --allow-missing preferences \
  --allow-missing usage
python storage_admin.py --verify-backup /safe/new-backup-dir
```

Run these commands only in the environment that actually contains the real
state files; a local repository checkout or new container is **not** a
production backup. An inventory distinguishes missing paths (historical
existence unknown), valid empty files, valid files with records, corrupt files,
unreadable files, and inaccessible paths. A backup now refuses any missing
store unless it is explicitly classified with repeated `--allow-missing STORE`
flags. Use that only after reviewing whether the store was genuinely never
initialized or is not required for this recovery point; the decision is recorded
in the manifest and independently checked during verification. If a store is
known to have existed, supply `--expect-existing STORE` when inventorying or
backing up; missing expected stores block backup. A file not present at its
current path does not prove it never existed.

The SQLite snapshot uses the online backup API and includes committed WAL
data. The manifest records source paths, backup checksums, counts, and
integrity results; verification works on copies without reading originals.
Reject incomplete or all-missing backups and preserve partial results for
diagnosis. **Copy the complete verified backup directory and its manifest to
durable restricted storage outside Railway before any deployment or volume
operation.** A manifest is not a cryptographic signature: independently
preserve trusted hashes and restrict backup access.

To migrate safely, stop or quiesce all writes, capture a final consistent
snapshot, restore to a prepared volume, compare file hashes and record counts,
then change configured paths and deploy under explicit approval. Recheck
persistence after startup and a controlled restart. If failure occurs, keep
both old backups and the new volume untouched, and preserve current data
before any rollback. Never mount an empty volume or restart/redeploy merely
to gain filesystem access to current ephemeral state.

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
   cause a clear error. A rerun with matching destination records does not
   rewrite them. If a source record differs from an existing destination
   record (even when ID/signature match), migration refuses to overwrite
   potentially newer SQLite data. The historical `updated` report count
   denotes matched destination records, not SQL UPDATE executions.
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

`freellmapi.co` distributes a **self-hosted** OpenAI-compatible router (its documented
base URL is `http://localhost:3001/v1`, and requests go out using *your own* provider
keys). If you do not self-host it, point `AI_BASE_URL`/`AI_MODEL` at a provider with a
free OpenAI-compatible endpoint instead, for example:

| Provider | `AI_BASE_URL` | Notes |
| --- | --- | --- |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | documented free tier for Flash models |
| Groq | `https://api.groq.com/openai/v1` | free tier with rate limits |
| OpenRouter | `https://openrouter.ai/api/v1` | free router + `:free` models; `qwen/qwen-2.5-coder-32b-instruct` itself is paid |
| Self-hosted FreeLLMAPI | `http://localhost:3001/v1` | router is free, inference uses your provider keys |

## English-only output

FreeLLM routes to Chinese models (Qwen/DeepSeek) that occasionally answer in Chinese.
Max is English 24/7 through two layers:

1. **Prompt rule** — the system prompt demands English and forbids new non-English
   comments/docstrings, while existing file comments stay byte-for-byte untouched.
2. **Runtime guard** — `is_non_english()` scans every patch for CJK characters that are
   not already present in the source file; a hit triggers one bilingual "English only"
   re-ask before the patch is accepted. Anything that still slips through is caught by
   the sandbox gate before any branch is pushed.

Discord messages, PR titles/bodies and `brain_memory.txt` entries are written by the bot
itself and are always English.

## Usage counters, cost and the retry ladder

**Counters** (`!flanx_stats` or `flanx stats`) are maintained in `flanx_usage.json`
(git-ignored, written atomically, thread-safe):

- **LLM:** call count, prompt/completion/total tokens, per-model breakdown, and
  estimated USD cost. Cost is `$0` by design while you run on the free token pool —
  set `AI_INPUT_PRICE_PER_M` / `AI_OUTPUT_PRICE_PER_M` if you move to a paid endpoint
  (OpenRouter lists `qwen-2.5-coder-32b` at $0.66 / $1.00 per 1M tokens).
- **Sandbox:** runs used this calendar month against the free plan's 100,000 quota,
  with pass rate and remaining balance; counters reset on month rollover.
- The file lives on the host, so on Railway a redeploy resets it unless you point
  `FLANX_USAGE_FILE` at a persistent volume.

**Retry ladder:** when a generated patch fails sandbox validation, Max regenerates
at temperature 0 (and on `AI_RETRY_MODEL` when configured) with the exact failure log
injected into the prompt, up to `FLANX_FIX_ATTEMPTS` total attempts. Only the final
failure is written to the brain, so the memory log stays signal, not noise.

## The brain

`brain_memory.txt` in the target repository is Max's long-term memory. Every run
appends a timestamped line (failures and successes), it is trimmed to ~12KB so it always
fits in one prompt, and it is committed straight to the default branch so you can watch
the bot learn in the GitHub history.