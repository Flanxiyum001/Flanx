"""Flanx — a self-correcting Discord coding agent.

Pipeline for `!flanx_fix <owner/repo> <path/to/file.py> <what to change>`:

  1. pull the file from GitHub
  2. recall lessons from `brain_memory.txt` in that repository
  3. search the live web for current docs / known fixes
  4. baseline-run the file in the free OnlineCompiler.io sandbox
  5. ask the LLM for a complete replacement file (raw code only)
  6. re-run the patch in the sandbox — abort if it fails, and log the
     failure to the brain so Flanx does not repeat it
  7. log the success, push a branch and open a pull request

All blocking work (GitHub, HTTP sandbox, web search) runs in threads so the
Discord gateway connection never stalls.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timezone

import discord
from discord.ext import commands
from github import Github, GithubException, UnknownObjectException
from openai import AsyncOpenAI

from free_sandbox import clip, read_flanx_brain, run_code, update_flanx_brain
from usage import format_usage, record_llm_usage, record_sandbox_run
from model_router import route_model

import integrations  # pluggable modules: drop a file into integrations/ to add one

try:
    from ddgs import DDGS
except ImportError:  # legacy package: pip install duckduckgo-search
    from duckduckgo_search import DDGS

# --------------------------------------------------------------------------
# Configuration (Railway → Variables, or Freebuff Settings → Environment)
# --------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://freellmapi.co/v1")
AI_MODEL = os.getenv("AI_MODEL", "qwen-2.5-coder-32b")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
FLANX_DEFAULT_REPO = os.getenv("FLANX_DEFAULT_REPO")  # optional: default repo for chat mode
AI_RETRY_MODEL = os.getenv("AI_RETRY_MODEL")  # optional second model for the retry ladder

try:
    FIX_ATTEMPTS = max(1, min(3, int(os.getenv("FLANX_FIX_ATTEMPTS", "2"))))
except ValueError:
    FIX_ATTEMPTS = 2  # initial attempt + one retry by default (clamped 1..3)

WEB_RESULT_COUNT = 3
CONTEXT_CHAR_BUDGET = 6_000  # cap each injected context block so prompts stay cheap
LLM_MAX_ATTEMPTS = 2  # second attempt is a bilingual "English only" re-ask

# Chinese / Japanese / Korean ranges, incl. CJK punctuation and fullwidth forms.
CJK_RE = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\uff00-\uffef]")

LANGUAGE_CORRECTION = (
    "你的回复中包含了非英文内容（中文/日文/韩文）。请重新输出：完整的文件原始代码，"
    "只保留文件本身，不加任何说明；所有新增的注释和 docstring 一律用英文。\n"
    "Your reply contained non-English text. Reply again with the COMPLETE file as raw "
    "code only — no explanation before or after it — and every new comment/docstring "
    "written in English. Existing comments in the file must stay exactly as they are."
)

REQUIRED_VARS = ("DISCORD_TOKEN", "AI_API_KEY", "GITHUB_TOKEN")

intents = discord.Intents.default()
intents.message_content = True  # privileged intent: enable it in the Discord developer portal
bot = commands.Bot(command_prefix="!", intents=intents)
gh = Github(GITHUB_TOKEN or None)
ai_client = AsyncOpenAI(api_key=AI_API_KEY or "unset-AI_API_KEY", base_url=AI_BASE_URL)

SYSTEM_PROMPT = """You are Flanx, an autonomous senior engineer running inside a Discord bot.
Rewrite the file below so it satisfies the user's goal, using every piece of context provided.

Rules:
- Output the COMPLETE updated file as raw text — nothing before it, nothing after it.
- Never wrap the output in ``` fences and never add commentary.
- ALWAYS respond in English (US). Never write Chinese, Japanese or Korean characters in
  anything you produce, even if a model, a prompt or quoted code drifts into another
  language. Any new comment or docstring you write must be English.
- Keep the file's existing comments/docstrings byte-for-byte; do not translate them.
- Make surgical changes; do not rewrite parts that do not need to change.
- Never repeat a mistake already recorded in the memory log.
- The sandbox has NO internet access: code must not need network at import/run time.

--- MEMORY LOG (past successes and failures) ---
{memory}

--- LIVE WEB RESEARCH ---
{web_intel}

--- SANDBOX BASELINE RUN OF THE CURRENT FILE ---
{baseline}

--- FILE UNDER REPAIR: {file_path} ({branch_default}) ---
{original_code}
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def web_search(query: str) -> str:
    """Live web lookup so Flanx can use current documentation and recent fixes."""
    try:
        print(f"🌐 Flanx is querying the web: {query!r}")
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=WEB_RESULT_COUNT))
        if not results:
            return "No recent external web data returned."
        blocks = [
            f"Source {idx + 1}: {r.get('title', '')}\n{r.get('body', '')}"
            for idx, r in enumerate(results)
        ]
        return clip("\n\n".join(blocks), CONTEXT_CHAR_BUDGET)
    except Exception as exc:
        return f"Web search unavailable: {clip(str(exc), 300)}"


def is_non_english(output: str, original_code: str) -> bool:
    """True when the patch contains CJK text that was not already in the source file.

    CJK characters that already exist in the original file are allowed (existing
    Chinese comments stay untouched); anything new means the model drifted out of
    English and should be re-asked.
    """
    allowed = set(original_code)
    return any(ch not in allowed for ch in CJK_RE.findall(output or ""))


def strip_code_fences(text: str) -> str:
    """Remove markdown fences if the model wrapped its reply anyway."""
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]  # drop the opening fence (``` or ```python)
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")


async def generate_patch(
    original_code: str,
    file_path: str,
    branch_default: str,
    memory: str,
    web_intel: str,
    baseline: str,
    goal: str,
    *,
    model: "str | None" = None,
    temperature: float = 0.1,
    retry_feedback: str = "",
) -> str:
    model = model or AI_MODEL
    system_prompt = SYSTEM_PROMPT.format(
        memory=clip(memory, CONTEXT_CHAR_BUDGET),
        web_intel=web_intel,
        baseline=baseline,
        file_path=file_path,
        branch_default=branch_default,
        original_code=clip(original_code, 24_000),
    )
    if retry_feedback:
        system_prompt += (
            "\n--- WHY THE PREVIOUS ATTEMPT WAS REJECTED BY THE SANDBOX ---\n"
            f"{retry_feedback}\n"
            "Fix that specific failure this time.\n"
        )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": goal},
    ]
    patch = ""
    for attempt in range(LLM_MAX_ATTEMPTS):
        completion = await ai_client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=messages,
        )
        served_model = getattr(completion, "model", None) or model
        if getattr(completion, "usage", None) is not None:
            record_llm_usage(served_model, completion.usage)
        content = completion.choices[0].message.content or ""
        patch = strip_code_fences(content)
        if not is_non_english(patch, original_code):
            return patch
        print(f"⚠️ Non-English output on attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} — re-asking for English.")
        messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": LANGUAGE_CORRECTION},
        ]
    return patch  # last resort; the sandbox still gates any broken output


def open_pull_request(
    repo,
    file_path: str,
    content: str,
    goal: str,
    validation_report: str,
) -> str:
    """Create a branch from the default branch, write the patch, open a PR."""
    branch = f"flanx-fix-{int(datetime.now(timezone.utc).timestamp())}"
    base_branch = repo.default_branch
    base = repo.get_branch(base_branch)
    repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base.commit.sha)

    # Read the current blob SHA on the fresh branch (raises loudly if the path is
    # not a file, instead of silently passing a commit SHA to update_file).
    current = repo.get_contents(file_path, ref=branch)
    if isinstance(current, list):
        raise ValueError(f"`{file_path}` is a directory on `{branch}` — Flanx needs a single file.")
    file_sha = current.sha

    for attempt in range(2):
        try:
            repo.update_file(
                path=file_path,
                message=f"🤖 Flanx fix: {goal[:60]}",
                content=content if content.endswith("\n") else content + "\n",
                sha=file_sha,
                branch=branch,
            )
            break
        except GithubException as exc:
            if exc.status == 409 and attempt == 0:
                current = repo.get_contents(file_path, ref=branch)
                if isinstance(current, list):
                    raise
                file_sha = current.sha
            else:
                raise

    pull = repo.create_pull(
        title=f"🤖 Flanx automated patch: {file_path}",
        body=(
            f"**Goal:** {goal}\n\n"
            f"**Sandbox validation:**\n```\n"
            f"{clip(validation_report, 800)}\n```\n\n"
            "_Opened by Flanx — the patch was executed and exit-code checked in a free "
            "OnlineCompiler.io sandbox before this branch was pushed._"
        ),
        head=branch,
        base=base_branch,
    )
    return pull.html_url


# --------------------------------------------------------------------------
# Cloud task staging (!update_repo)
# --------------------------------------------------------------------------
TASKS_DIR = "tasks"


def build_task_manifest(
    task_id: int,
    repo_name: str,
    file_path: str,
    instruction: str,
    base_branch: str,
    target_exists: bool,
) -> str:
    """Render the ``tasks/update-job-<id>.md`` manifest an agent session picks up."""
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    exists_note = (
        "found on the base branch"
        if target_exists
        else "**not found** on the base branch — this task may intend to create it"
    )
    return (
        f"# Flanx Automated Update Manifest #{task_id}\n\n"
        f"- **Repository:** `{repo_name}`\n"
        f"- **Target file:** `{file_path}` ({exists_note})\n"
        f"- **Base branch:** `{base_branch}`\n"
        f"- **Staged on branch:** `flanx-evolution-{task_id}`\n"
        f"- **Created:** {created} (staged by Flanx on Railway via `!update_repo`)\n\n"
        f"## Engineering objective\n\n{instruction}\n\n"
        f"## Definition of done\n\n"
        f"- [ ] Keep the change confined to the target file unless the objective needs more.\n"
        f"- [ ] Run the project's build and tests in the workspace sandbox before committing; "
        f"never stage changes that fail validation.\n"
        f"- [ ] Follow the file's existing style and language for comments and docstrings.\n"
        f"- [ ] Open a Pull Request from this branch that references manifest #{task_id}, "
        f"with the validation log in the description.\n"
        f"- [ ] Do not push directly to the default branch.\n"
    )


def github_branch_url(repo_name: str, branch: str) -> str:
    return f"https://github.com/{repo_name}/tree/{branch}"


def github_file_url(repo_name: str, branch: str, path: str) -> str:
    return f"https://github.com/{repo_name}/blob/{branch}/{path}"


# --------------------------------------------------------------------------
# Chat mode: "@Flanx …" or "flanx …" with no prefix
# --------------------------------------------------------------------------
CHAT_HELP = (
    "👋 **Flanx chat mode** — just talk to me, no prefix needed:\n"
    "• `flanx help me with src/app.js in owner/repo — <what to change>`\n"
    "• `@Flanx fix src/app.js in owner/repo: <what to change>`\n"
    "• `flanx search <query> for me`\n"
    "• `flanx what do you remember about owner/repo`\n"
    "• `flanx stats` — token usage, estimated cost and sandbox quota\n"
    "• `flanx set timezone Asia/Kolkata` · `flanx what time is it`\n"
    "• `flanx remember <anything>` · `flanx show notes`\n"
    "• `flanx integrations` — list the pluggable modules\n"
    "Prefix commands still work: `!flanx_fix`, `!update_repo`, `!flanx_brain`, `!flanx_stats`."
)

PERSONA_CARD = (
    "🤖 **I'm Flanx** — your coding companion and personal assistant.\n"
    "**Coding:** pull a file, patch it with AI, prove it in a free cloud sandbox, open a PR.\n"
    "**Web:** `flanx search <query> for me`.\n"
    "**Memory:** a repo brain (`flanx what do you remember about owner/repo`) "
    "and your private notes (`flanx remember …`).\n"
    "**Daily stuff:** time in your timezone (`flanx set timezone Asia/Kolkata`), "
    "usage stats (`flanx stats`).\n"
    "**Extensions:** `flanx integrations` lists the pluggable modules — drop a new file "
    "into `integrations/` to add one.\n"
    "No prefix needed — just start with `flanx` or tag me."
)

FILE_EXT_PATTERN = (
    r"py|js|jsx|ts|tsx|java|go|rs|rb|php|css|html|scss|json|md|yml|yaml|"
    r"sh|c|cpp|h|cs|swift|kt|txt|toml|ini|cfg"
)
FILE_PATH_RE = re.compile(rf"\b[\w][\w./-]*\.(?:{FILE_EXT_PATTERN})\b", re.IGNORECASE)
REPO_RE = re.compile(r"\b([\w.-]{1,39})/([\w.-]{1,100})\b")
CHAT_NAME_RE = re.compile(r"^\s*(?:hey\s+|yo\s+|oi\s+)?flanx\b[:,]?\s*(.*)$", re.IGNORECASE | re.DOTALL)
SEARCH_VERB_RE = re.compile(r"\b(?:search|look\s*up|google)\b", re.IGNORECASE)
MEMORY_RE = re.compile(r"\b(?:brain|memory|remember|lessons|learned)\b", re.IGNORECASE)
STATS_RE = re.compile(
    r"^\s*(?:stats|usage|cost|quota|tokens?)\b"
    r"|(?:how\s+many\s+tokens|token\s+count|what(?:'s|\s+is)\s+my\s+(?:usage|cost))",
    re.IGNORECASE,
)
EXTENSIONS_RE = re.compile(
    r"^\s*(?:what\s+)?(?:integrations?|plugins?|extensions?|modules?)"
    r"(?:\s+(?:do\s+you\s+have|are\s+installed|list))?\s*$",
    re.IGNORECASE,
)
WHO_RE = re.compile(r"^\s*(?:who\s+are\s+you|what\s+are\s+you|introduce\s+yourself|your\s+name)\b", re.IGNORECASE)
HOWAREYOU_RE = re.compile(
    r"^\s*(?:how(?:'s|\s+is)?\s+(?:it\s+going|are\s+you|you(?:'re|\s+are)?\s+doing)|you\s+(?:ok|there|good))\b.*$",
    re.IGNORECASE,
)
THANKS_RE = re.compile(r"^\s*(?:thanks|thank\s+you|thx|ty|cheers)\b.*$", re.IGNORECASE)
GREETING_RE = re.compile(
    r"^\s*(?:yo|hi|hey|hello|sup|howdy|hii+)\b"
    r"(?:\s+(?:yo|hi|hey|there|sup|bro|man|dude|flanx|guys))?\s*$"
    r"|^good\s+(?:morning|afternoon|evening)\b.*$",
    re.IGNORECASE,
)
HELP_RE = re.compile(r"^\s*(?:help|what\s+can\s+you\s+do|commands)\s*$", re.IGNORECASE)
FIX_VERB_RE = re.compile(
    r"^\s*(?:can\s+you\s+|pls\s+|please\s+)?"
    r"(?:help\s+me(?:\s+(?:with|to))?\s+|fix|repair|patch|update|upgrade|improve|work\s+on)\b[:\s]*",
    re.IGNORECASE,
)


def _strip_name_trigger(text: str) -> "str | None":
    """'flanx help me with this' -> 'help me with this'; None when Flanx is not addressed."""
    match = CHAT_NAME_RE.match((text or "").strip())
    return match.group(1).strip() if match else None


def _extract_repo(text: str) -> "str | None":
    """First owner/repo token that is not a file path like src/app.py."""
    for owner, name in REPO_RE.findall(text):
        if re.fullmatch(FILE_EXT_PATTERN, name.rsplit(".", 1)[-1], re.IGNORECASE):
            continue  # a file path, not a repository
        return f"{owner}/{name}"
    return None


def _extract_file(text: str) -> "str | None":
    match = FILE_PATH_RE.search(text or "")
    return match.group(0) if match else None


def _clean_search_query(text: str) -> str:
    query = re.sub(
        r"^\s*(?:can\s+you\s+|could\s+you\s+|pls\s+|please\s+)?", "", text, flags=re.IGNORECASE
    )
    query = SEARCH_VERB_RE.sub("", query, count=1)
    query = re.sub(r"^\s*(?:the\s+)?(?:web|internet|online|google)?\s*(?:for\s+)?", "", query, flags=re.IGNORECASE)
    query = re.sub(r"\s*(?:for\s+me|please|pls)\s*$", "", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+", " ", query).strip(" ?!.,")
    return "" if query.lower() in {"me", "it", "for me", "please", "pls"} else query


def _clean_goal(text: str, repo: "str | None", file_path: "str | None") -> str:
    goal = text
    if repo:
        goal = goal.replace(repo, " ")
    if file_path:
        goal = goal.replace(file_path, " ")
    goal = FIX_VERB_RE.sub("", goal, count=1)
    goal = re.sub(r"\s+", " ", goal).strip(" ,:;|-—")
    # remove filler left behind when the file/repo tokens were stripped:
    # doubled prepositions ("in in") from two removals, then one leading one
    goal = re.sub(r"\b(in|on|for|at|to|from|of)\s+\1\b", "", goal, flags=re.IGNORECASE)
    goal = re.sub(r"\s+", " ", goal)
    return re.sub(r"^(?:in|on|for)\s+(?=\S)", "", goal).strip(" ,:;|-—")


def chat_intent(text: str, default_repo: "str | None" = None) -> "tuple[str, dict]":
    """Classify a chat request: greeting/time/notes/who … or search/brain/help/fix."""
    stripped = (text or "").strip().strip("?.!")
    if not stripped:
        return "help", {}
    extension = integrations.match(stripped)
    if extension:
        return "integration", {"name": extension[0], "payload": extension[1]}
    if SEARCH_VERB_RE.search(stripped):
        return "search", {"query": _clean_search_query(stripped)}
    if STATS_RE.search(stripped):
        return "stats", {}
    if EXTENSIONS_RE.match(stripped):
        return "integrations", {}
    if WHO_RE.match(stripped):
        return "who", {}
    if HOWAREYOU_RE.match(stripped):
        return "howareyou", {}
    if THANKS_RE.match(stripped):
        return "thanks", {}
    if GREETING_RE.match(stripped):
        return "greeting", {"text": stripped}
    if MEMORY_RE.search(stripped):
        return "brain", {"repo": _extract_repo(stripped) or default_repo}
    if HELP_RE.match(stripped):
        return "help", {}
    repo = _extract_repo(stripped)
    file_path = _extract_file(stripped)
    return "fix", {
        "repo": repo or default_repo,
        "file": file_path,
        "goal": _clean_goal(stripped, repo, file_path),
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"🤖 Flanx online as {bot.user} (chat mode + free sandbox + cloud task staging)")
    await bot.change_presence(
        activity=discord.Game(name="just say 'flanx …' · !flanx_fix · !update_repo")
    )


@bot.event
async def on_message(message):
    """Chat mode: '@Flanx …' or 'flanx …' with no prefix, alongside the !commands."""
    if message.author.bot:
        return

    text = (message.content or "").strip()
    if bot.user and any(user.id == bot.user.id for user in message.mentions):
        remainder = re.sub(rf"<@!?{bot.user.id}>", "", text).strip()
    else:
        remainder = _strip_name_trigger(text)

    if remainder is None:
        await bot.process_commands(message)
        return

    intent, payload = chat_intent(remainder, FLANX_DEFAULT_REPO)
    channel = message.channel
    try:
        if intent == "search":
            query = payload["query"]
            if not query:
                await channel.send(
                    "🔎 What should I look up? e.g. `flanx search python 3.14 changes for me`"
                )
            else:
                async with channel.typing():
                    results = await asyncio.to_thread(web_search, query)
                await channel.send(
                    f"🌐 **Search:** `{clip(query, 120)}`\n```\n{clip(results, 1800)}\n```"
                )
        elif intent == "stats":
            await channel.send(await asyncio.to_thread(format_usage))
        elif intent == "integration":
            reply = await integrations.dispatch(
                payload["name"],
                payload["payload"],
                integrations.ChatEvent(
                    channel=channel, author_id=message.author.id, raw_text=remainder
                ),
            )
            if reply:
                await channel.send(reply)
        elif intent == "integrations":
            lines = [
                "🧩 **Installed integrations** — drop a new module into `integrations/` "
                "to add another (no core changes needed):"
            ]
            for name, description, usages in integrations.catalog():
                example = f" — e.g. `{usages[0]}`" if usages else ""
                lines.append(f"• **{name}**: {description}{example}")
            await channel.send("\n".join(lines))
        elif intent == "who":
            await channel.send(PERSONA_CARD)
        elif intent == "howareyou":
            await channel.send(
                "Running smooth ✅ — sandbox green, counters ticking, brain intact. "
                "What are we building today?"
            )
        elif intent == "thanks":
            await channel.send("Anytime 🙌 Just say the word.")
        elif intent == "greeting":
            text = payload.get("text", "").lower()
            if "morning" in text:
                opener = "Good morning ☀️"
            elif "afternoon" in text:
                opener = "Good afternoon 🌤️"
            elif "evening" in text:
                opener = "Good evening 🌙"
            else:
                opener = "Yo! 👋"
            await channel.send(
                f"{opener} I'm Flanx — I patch code on GitHub, search the web, keep time "
                "and take notes for you. Try `flanx help` for the full menu."
            )
        elif intent == "brain":
            repo = payload["repo"]
            if not repo:
                await channel.send(
                    "🧠 Which repository? e.g. `flanx what do you remember about owner/repo`"
                )
            else:
                memory = await asyncio.to_thread(read_flanx_brain, repo)
                await channel.send(f"🧠 **Brain memory for `{repo}`**\n```\n{clip(memory, 1800)}\n```")
        elif intent == "fix":
            repo, file_path, goal = payload["repo"], payload["file"], payload["goal"]
            missing = []
            if not repo:
                missing.append("the repo (`owner/repo`)")
            if not file_path:
                missing.append("the file path")
            if not goal:
                missing.append("what you'd like changed")
            if missing:
                await channel.send(
                    "🙂 Happy to help with that — I just need " + " and ".join(missing) + ".\n"
                    "e.g. `flanx help me with src/app.js in owner/repo — <what to change>`"
                )
            else:
                async with channel.typing():
                    await fix_pipeline(channel, repo, file_path, goal)
        else:
            await channel.send(CHAT_HELP)
    except Exception as exc:
        await channel.send(f"⚠️ **Matrix exception:** {clip(str(exc), 300)}")

    await bot.process_commands(message)


async def fix_pipeline(ctx, repo_name: str, file_path: str, diagnostic_goal: str) -> None:
    """Full repair cycle. `ctx` is a command ctx or any Discord messageable with .send()."""
    try:
        await ctx.send(f"📡 *Jarvis sequence initiated.* Pulling `{file_path}` from `{repo_name}`...")
        repo = await asyncio.to_thread(gh.get_repo, repo_name)
        default_branch = repo.default_branch

        def fetch_file():
            return repo.get_contents(file_path, ref=default_branch)

        file_obj = await asyncio.to_thread(fetch_file)
        if isinstance(file_obj, list):
            await ctx.send(f"⚠️ `{file_path}` is a directory — point Flanx at a single file.")
            return
        original_code = file_obj.decoded_content.decode("utf-8", errors="replace")

        # 1-3. Memory recall, web research and the sandbox baseline are independent
        # network calls — run them concurrently so the slowest one sets the pace
        # instead of the sum of all three.
        await ctx.send(
            "🌐 Pulling memory + live web docs + free cloud sandbox baseline in parallel..."
        )
        past_memories, live_web_intel, baseline = await asyncio.gather(
            asyncio.to_thread(read_flanx_brain, repo_name),
            asyncio.to_thread(web_search, diagnostic_goal),
            asyncio.to_thread(run_code, original_code),
        )
        baseline_report = baseline.for_prompt("baseline run")
        record_sandbox_run(baseline)
        if not baseline.ok:
            await ctx.send(f"⚠️ **Sandbox unreachable — aborting.** {baseline.error}")
            return

        # 4-5. Generate -> validate, with a retry ladder: a failed sandbox check
        # triggers another attempt at temperature 0 (on AI_RETRY_MODEL if set),
        # carrying the failure log forward so the model can fix the actual problem.
        validation = None
        raw_patch = ""
        retry_feedback = ""
        model, temperature = route_model("coding", fallback=AI_MODEL), 0.1
        for attempt in range(1, FIX_ATTEMPTS + 1):
            if attempt == 1:
                await ctx.send("🧠 Composing the patch from memory + web intel + sandbox trace...")
            else:
                model = AI_RETRY_MODEL or model
                temperature = 0.0
                await ctx.send(
                    f"🔁 Retry {attempt}/{FIX_ATTEMPTS}: regenerating at temperature 0"
                    + (f" on `{model}`" if AI_RETRY_MODEL else "")
                    + " with the failure log attached..."
                )
            try:
                raw_patch = await generate_patch(
                    original_code, file_path, default_branch,
                    past_memories, live_web_intel, baseline_report, diagnostic_goal,
                    model=model, temperature=temperature, retry_feedback=retry_feedback,
                )
            except Exception as exc:
                await ctx.send(
                    f"⚠️ **LLM layer error** (`{model}` @ `{AI_BASE_URL}`): "
                    f"`{clip(str(exc), 300)}`"
                )
                return
            if not raw_patch.strip():
                await ctx.send("⚠️ The model returned an empty patch — aborting before any push.")
                return

            await ctx.send(
                f"🔄 Validating the patch in the free sandbox (attempt {attempt}/{FIX_ATTEMPTS})..."
            )
            validation = await asyncio.to_thread(run_code, raw_patch)
            record_sandbox_run(validation)
            if validation.passed:
                break
            retry_feedback = (
                f"Your previous patch for `{file_path}` failed sandbox validation:\n"
                f"{validation.for_prompt('failed attempt')}"
            )
        else:
            exit_code = validation.exit_code if validation and validation.exit_code is not None else "n/a"
            detail = (validation.stderr or validation.error) if validation else "no run result"
            insight = (
                f"FAILED patch for `{file_path}` after {FIX_ATTEMPTS} attempt(s) "
                f"(goal: {diagnostic_goal[:60]}) — sandbox exit {exit_code}: {clip(detail, 140)}"
            )
            await asyncio.to_thread(update_flanx_brain, repo_name, insight)
            await ctx.send(
                f"⚠️ **Patch failed sandbox validation after {FIX_ATTEMPTS} attempt(s) — lesson "
                f"logged to the brain so it is not repeated.**\n"
                f"{validation.for_discord() if validation else 'no run result'}"
            )
            return

        # 6. Success: record the lesson, then branch + PR
        insight = (
            f"Fixed `{file_path}` (goal: {diagnostic_goal[:70]}) — "
            "passed sandbox validation with exit code 0."
        )
        await asyncio.to_thread(update_flanx_brain, repo_name, insight)

        await ctx.send("🚀 Validation clean! Creating the branch and opening the pull request...")
        pr_url = await asyncio.to_thread(
            open_pull_request, repo, file_path, raw_patch, diagnostic_goal,
            validation.for_prompt("validation run"),
        )

        await ctx.send(
            "✅ **Mission complete.** The patch ran cleanly in the sandbox and the brain has "
            f"been updated.\nReview the PR: {pr_url}"
        )

    except GithubException as exc:
        await ctx.send(f"⚠️ **GitHub error** `{exc.status}`: {clip(str(exc.data), 300)}")
    except Exception as exc:
        await ctx.send(f"⚠️ **Matrix exception:** {clip(str(exc), 300)}")


@bot.command(name="flanx_fix")
async def flanx_fix(ctx, repo_name: str, file_path: str, *, diagnostic_goal: str):
    """Pull a file from GitHub, patch it with AI, prove it in the free sandbox, open a PR."""
    async with ctx.typing():
        await fix_pipeline(ctx, repo_name, file_path, diagnostic_goal)


@bot.command(name="update_repo")
async def update_repo(ctx, repo_name: str, file_path: str, *, instruction: str):
    """Stage a task manifest on a fresh branch for Freebuff/Codebuff agents to execute."""
    async with ctx.typing():
        try:
            await ctx.send(f"📡 *Opening repository grid.* Staging a task manifest for `{repo_name}`...")
            repo = await asyncio.to_thread(gh.get_repo, repo_name)
            base_branch = repo.default_branch
            task_id = int(ctx.message.created_at.timestamp())
            target_branch = f"flanx-evolution-{task_id}"
            manifest_path = f"{TASKS_DIR}/update-job-{task_id}.md"

            def stage() -> bool:
                """Branch from the default HEAD, then drop the manifest onto it."""
                base = repo.get_branch(base_branch)
                repo.create_git_ref(ref=f"refs/heads/{target_branch}", sha=base.commit.sha)
                try:
                    repo.get_contents(file_path, ref=base_branch)
                    exists = True
                except UnknownObjectException:
                    exists = False
                manifest = build_task_manifest(
                    task_id, repo_name, file_path, instruction, base_branch, exists
                )
                repo.create_file(
                    path=manifest_path,
                    message=f"🤖 Flanx: stage task #{task_id} manifest",
                    content=manifest,
                    branch=target_branch,
                )
                return exists

            target_exists = await asyncio.to_thread(stage)

            embed = discord.Embed(
                title="🗂️ Task manifest staged",
                description=(
                    f"Manifest `#{task_id}` is committed to a new branch. Open it in Freebuff "
                    "Cloud (with this repository connected there) to run agents on it, or run "
                    f"`!flanx_fix {repo_name} {file_path} ...` to patch it right here."
                ),
                color=discord.Color.blurple(),
                url=github_branch_url(repo_name, target_branch),
            )
            embed.add_field(name="Repository", value=f"`{repo_name}`", inline=True)
            embed.add_field(name="Base branch", value=f"`{base_branch}`", inline=True)
            embed.add_field(
                name="Feature branch",
                value=f"[flanx-evolution-{task_id}]({github_branch_url(repo_name, target_branch)})",
                inline=True,
            )
            embed.add_field(
                name="Task manifest",
                value=f"[`{manifest_path}`]({github_file_url(repo_name, target_branch, manifest_path)})",
                inline=True,
            )
            embed.add_field(
                name="Target file",
                value=(
                    f"`{file_path}` found on `{base_branch}`"
                    if target_exists
                    else f"`{file_path}` not found on `{base_branch}` (task may create it)"
                ),
                inline=True,
            )
            await ctx.send(embed=embed)

        except GithubException as exc:
            await ctx.send(f"⚠️ **GitHub error** `{exc.status}`: {clip(str(exc.data), 300)}")
        except Exception as exc:
            await ctx.send(f"⚠️ **Automation matrix interrupted:** {clip(str(exc), 300)}")


@bot.command(name="flanx_brain")
async def flanx_brain(ctx, repo_name: str):
    """Show what Flanx remembers from past runs in a repository."""
    memory = await asyncio.to_thread(read_flanx_brain, repo_name)
    await ctx.send(f"🧠 **Brain memory for `{repo_name}`**\n```\n{clip(memory, 1800)}\n```")


@bot.command(name="flanx_stats")
async def flanx_stats(ctx):
    """Show LLM token usage, estimated cost and sandbox quota consumption."""
    await ctx.send(await asyncio.to_thread(format_usage))


if __name__ == "__main__":
    missing = [name for name in REQUIRED_VARS if not os.getenv(name)]
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        print("   Set them in Railway → Variables (or Freebuff Settings → Environment).")
        sys.exit(1)
    if not os.getenv("ONLINE_COMPILER_API_KEY"):
        print(
            "⚠️  ONLINE_COMPILER_API_KEY is unset — sandbox validation will refuse "
            "to run until you add it."
        )
    bot.run(DISCORD_TOKEN)