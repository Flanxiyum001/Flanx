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
5. Ask the LLM for a complete replacement file (raw code, no markdown fences).
6. Re-run the patched file in the sandbox — **if it fails, the failure is written to
   the brain and no branch is pushed.**
7. On success, write the lesson to the brain, push a branch and open a PR.

All blocking work (PyGithub, sandbox HTTP, web search) runs in threads so the Discord
gateway never stalls.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Discord bot: commands, prompts, PR flow |
| `free_sandbox.py` | OnlineCompiler.io execution + `brain_memory.txt` read/write |
| `requirements.txt` | `discord.py`, `openai`, `PyGithub`, `requests`, `ddgs` |
| `knowledge.md` | Repo-level directive that steers Freebuff/Codebuff agents handling `tasks/*.md` |

## Commands

- `!flanx_fix <owner/repo> <path/to/file.py> <what to change>` — full repair cycle
- `!update_repo <owner/repo> <path/to/file.py> <instruction>` — stage a task manifest on a
  fresh branch for an external agent session to execute
- `!flanx_brain <owner/repo>` — print what Flanx remembers for that repository

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

## The brain

`brain_memory.txt` in the target repository is Flanx's long-term memory. Every run
appends a timestamped line (failures and successes), it is trimmed to ~12KB so it always
fits in one prompt, and it is committed straight to the default branch so you can watch
the bot learn in the GitHub history.