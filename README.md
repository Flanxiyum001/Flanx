# Flanx

A self-correcting Discord coding agent. It pulls a file from GitHub, recalls past
lessons, searches the live web, proves the fix inside a **free** OnlineCompiler.io
sandbox, learns from the result, and opens a pull request — all from a Discord command.

```
!flanx_fix owner/repo src/app.py add retry logic around the HTTP call
```

## Pipeline

1. Pull the target file from GitHub (default branch).
2. Read `brain_memory.txt` from that repo — Flanx's diary of past successes and failures.
3. Search the live web for current docs / known fixes (`ddgs`).
4. Baseline-run the current file in the free sandbox to capture a real error trace.
   (Steps 2–4 are independent, so Flanx runs them **concurrently** — the slowest one
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

Use the chat trigger (mention Max or prefix a message with `flanx`) for these actions:

- `flanx repos` — list the first page of repositories visible to `GITHUB_TOKEN`; `flanx repos 2` opens the next page.
- `flanx use owner/repo` — verify access and save a per-user default repository.
- `flanx current repo` — show your saved repository.
- `flanx inspect owner/repo` — read repository metadata, root entries, `AGENTS.md` (if present), and `README.md`; this does not execute code or change files.

Repository discovery is read-only. The token can only list repositories permitted by its GitHub scopes and organization policies. Per-user settings are stored in the existing local JSON preferences file and may not survive redeploys without persistent storage.

## Commands

- `!flanx_fix <owner/repo> <path/to/file.py> <what to change>` — full repair cycle
- `!update_repo <owner/repo> <path/to/file.py> <instruction>` — stage a task manifest on a
  fresh branch for an external agent session to execute
- `!flanx_brain <owner/repo>` — print what Flanx remembers for that repository
- `!flanx_stats` — LLM tokens used, estimated cost, and this month's sandbox quota

All of these also work as plain chat (`flanx …` / `@Flanx …`) — see **Chat mode** below.

## Chat mode (no prefix needed)

Talk to Flanx in plain chat — it answers when you **mention it** (`@Flanx …`) or start a
message with the word `flanx` (case-insensitive, `hey flanx`/`flanx,` also work):

| You type | Flanx does |
| --- | --- |
| `flanx help me with src/app.js in owner/repo — add retry logic` | runs the full `!flanx_fix` repair pipeline |
| `@Flanx fix src/app.js in owner/repo: upgrade to argon2id` | same pipeline, via mention |
| `flanx search python 3.14 changes for me` | live web search, results posted back |
| `flanx what do you remember about owner/repo` | prints that repo's brain memory |
| `flanx stats` / `flanx how many tokens have you used` | usage: tokens, cost, sandbox quota |
| `flanx yo sup` / `flanx how are you` / `flanx who are you` | friendly companion replies (no fix-request nagging) |
| `flanx set timezone Asia/Kolkata` | saves **your** timezone (per Discord user, remembered) |
| `flanx what time is it` | current time in your saved timezone (or `flanx time in America/New_York`) |
| `flanx remember <anything>` / `flanx show notes` | personal notes, stored per user |
| `flanx help` | usage card |

Parsing pulls out the `owner/repo` and the file path automatically; set
`FLANX_DEFAULT_REPO=owner/repo` and you can omit the repo. **Casual chat never gets the
help card** — if a request is a fix but lacks details, Flanx asks for just that one
thing, in one friendly line. `!`-prefix commands keep working, and plain messages that
don't address Flanx are ignored.

### Companion behaviour

Flanx recognises greetings (`yo sup`, `good morning`), `how are you`, `who are you`,
`thanks`, time/timezone requests and personal notes — all answered conversationally
instead of being treated as broken GitHub commands. Timezones and notes are stored per
Discord user in `flanx_prefs.json` (git-ignored; point `FLANX_PREFS_FILE` at a
persistent path to keep them across Railway redeploys). Notes are capped at 50 per
user so the file stays small.

## Adding your own integrations

Flanx has a **drop-in plugin system** — new capabilities need no changes to `main.py`:

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

Instead of running heavy work on the Railway box, Flanx can **stage** it:

1. `!update_repo username/my-app src/auth.js "Upgrade encryption to argon2id"`
2. Flanx branches off the default branch (`flanx-evolution-<id>`), writes
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
Flanx is English 24/7 through two layers:

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

**Retry ladder:** when a generated patch fails sandbox validation, Flanx regenerates
at temperature 0 (and on `AI_RETRY_MODEL` when configured) with the exact failure log
injected into the prompt, up to `FLANX_FIX_ATTEMPTS` total attempts. Only the final
failure is written to the brain, so the memory log stays signal, not noise.

## The brain

`brain_memory.txt` in the target repository is Flanx's long-term memory. Every run
appends a timestamped line (failures and successes), it is trimmed to ~12KB so it always
fits in one prompt, and it is committed straight to the default branch so you can watch
the bot learn in the GitHub history.