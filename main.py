"""Max — a self-correcting Discord coding agent.

Pipeline for `!max_fix <owner/repo> <path/to/file.py> <what to change>`:

  1. pull the file from GitHub
  2. recall lessons from `brain_memory.txt` in that repository
  3. search the live web for current docs / known fixes
  4. baseline-run the file in the free OnlineCompiler.io sandbox
  5. ask the LLM for a complete replacement file (raw code only)
  6. re-run the patch in the sandbox and stop if validation fails
  7. prepare a scoped pending GitHub action; test-repository publication needs
     authenticated Discord approval and explicit configuration

All blocking work (GitHub, HTTP sandbox, web search) runs in threads so the
Discord gateway connection never stalls.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands
from github import Github, GithubException, UnknownObjectException

from free_sandbox import clip, read_flanx_brain, run_code
from usage import format_usage, record_llm_usage, record_sandbox_run
from model_router import ModelError, ModelRouter
from prefs import get_pref
from task_engine import ActionNotStarted, Orchestrator, Status, Task
from approval_publish import PublishConfig, PublishError, PublishingCoordinator

import integrations  # pluggable modules: drop a file into integrations/ to add one

try:
    from ddgs import DDGS
except ImportError:  # legacy package: pip install duckduckgo-search
    from duckduckgo_search import DDGS

# --------------------------------------------------------------------------
# Configuration (Railway → Variables, or Freebuff Settings → Environment)
# --------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
TASK_DB = os.getenv("MAX_TASK_DB") or os.path.join(os.path.dirname(__file__), ".max_state", "tasks.sqlite3")
FLANX_DEFAULT_REPO = os.getenv("FLANX_DEFAULT_REPO")  # optional: default repo for chat mode
AI_RETRY_MODEL = os.getenv("AI_RETRY_MODEL")  # optional second model for the retry ladder
MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT = os.getenv("MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT", "").lower() in ("1", "true")

try:
    FIX_ATTEMPTS = max(1, min(3, int(os.getenv("FLANX_FIX_ATTEMPTS", "2"))))
except ValueError:
    FIX_ATTEMPTS = 2  # initial attempt + one retry by default (clamped 1..3)

WEB_RESULT_COUNT = 3
CONTEXT_CHAR_BUDGET = 6_000  # cap each injected context block so prompts stay cheap
LLM_MAX_ATTEMPTS = 2  # second attempt is a bilingual "English only" re-ask

# Chinese / Japanese / Korean ranges, incl. CJK punctuation and fullwidth forms.
CJK_RE = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\uff00-\uffef]")
CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}|"
    r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}"
)

LANGUAGE_CORRECTION = (
    "你的回复中包含了非英文内容（中文/日文/韩文）。请重新输出：完整的文件原始代码，"
    "只保留文件本身，不加任何说明；所有新增的注释和 docstring 一律用英文。\n"
    "Your reply contained non-English text. Reply again with the COMPLETE file as raw "
    "code only — no explanation before or after it — and every new comment/docstring "
    "written in English. Existing comments in the file must stay exactly as they are."
)

REQUIRED_VARS = ("DISCORD_TOKEN", "GITHUB_TOKEN")

intents = discord.Intents.default()
intents.message_content = True  # privileged intent: enable it in the Discord developer portal
bot = commands.Bot(command_prefix="!", intents=intents)
gh = Github(GITHUB_TOKEN or None)
model_router = ModelRouter()
publish_config = PublishConfig.from_env()
publisher = PublishingCoordinator(publish_config,
                                  lambda: Github(publish_config.token, timeout=15, retry=0))


class PublishApprovalView(discord.ui.View):
    """Discord identifies the interacting account; the server rechecks every decision."""

    def __init__(self, prepared):
        super().__init__(timeout=max(1, (prepared.expires_at - datetime.now(timezone.utc)).total_seconds()))
        self.prepared = prepared
        self.message = None

    async def _decision(self, interaction: discord.Interaction, approve: bool) -> None:
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if user_id not in publisher.config.approver_ids:
            await interaction.response.send_message("You are not authorized for this approval.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await publisher.decide(self.prepared.task_id, self.prepared.nonce,
                                        user_id, approve=approve)
        if result in ("rejected", "completed", "failed", "outcome_uncertain"):
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                if self.message is not None:
                    await self.message.edit(view=self)
                elif interaction.message is not None:
                    await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
        messages = {
            "rejected": "Approval rejected. The request cannot execute.",
            "completed": "Approved branch and pull request were created.",
            "failed": "Approved action stopped before publishing.",
            "outcome_uncertain": "GitHub outcome is uncertain. Reconcile it before any new request; no write will be retried automatically.",
        }
        await interaction.followup.send(messages.get(result, f"Approval stopped: {result}."), ephemeral=True)

    @discord.ui.button(label="Approve branch and PR", style=discord.ButtonStyle.success)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decision(interaction, True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decision(interaction, False)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

SYSTEM_PROMPT = """You are Max, an autonomous senior engineer running inside a Discord bot.
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
    """Live web lookup so Max can use current documentation and recent fixes."""
    try:
        print("🌐 Max is querying the web.")
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=WEB_RESULT_COUNT))
        if not results:
            return "No recent external web data returned."
        blocks = [
            f"Source {idx + 1}: {r.get('title', '')}\n{r.get('body', '')}"
            for idx, r in enumerate(results)
        ]
        return clip("\n\n".join(blocks), CONTEXT_CHAR_BUDGET)
    except Exception:
        return "Web search unavailable."


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
    model = model or model_router.selected_model("coding")
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
        reply = await model_router.generate(
            messages, task="coding", model=model, data_class=("source", "memory"),
            temperature=temperature,
        )
        if reply.prompt_tokens is not None or reply.completion_tokens is not None:
            record_llm_usage(reply.model, reply)
        content = reply.text
        patch = strip_code_fences(content)
        if not is_non_english(patch, original_code):
            return patch
        print(f"⚠️ Non-English output on attempt {attempt + 1}/{LLM_MAX_ATTEMPTS} — re-asking for English.")
        messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": LANGUAGE_CORRECTION},
        ]
    return patch  # last resort; the sandbox still gates any broken output


def _open_pull_request_after_approval(
    repo,
    file_path: str,
    content: str,
    goal: str,
    validation_report: str,
    branch: str,
    base_branch: str,
    base_sha: str,
    file_sha: str,
) -> str:
    """Internal write handler; callers must first pass the task-engine approval gate."""
    if not REPOSITORY_WRITES_ENABLED:
        raise RuntimeError("Repository publication is disabled")
    repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)

    for attempt in range(2):
        try:
            repo.update_file(
                path=file_path,
                message=f"🤖 Max fix: {goal[:60]}",
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
        title=f"🤖 Max automated patch: {file_path}",
        body=(
            f"**Goal:** {goal}\n\n"
            f"**Sandbox validation:**\n```\n"
            f"{clip(validation_report, 800)}\n```\n\n"
            "_Opened by Max — the patch was executed and exit-code checked in a free "
            "OnlineCompiler.io sandbox before this branch was pushed._"
        ),
        head=branch,
        base=base_branch,
    )
    return pull.html_url


# --------------------------------------------------------------------------
# Cloud task staging proposal (!update_repo)
# --------------------------------------------------------------------------
TASKS_DIR = "tasks"


def build_task_manifest(
    task_id: int,
    repo_name: str,
    file_path: str,
    instruction: str,
    base_branch: str,
    target_exists: bool,
    target_branch: str | None = None,
) -> str:
    """Render the ``tasks/update-job-<id>.md`` manifest an agent session picks up."""
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    exists_note = (
        "found on the base branch"
        if target_exists
        else "**not found** on the base branch — this task may intend to create it"
    )
    return (
        f"# Max Automated Update Manifest #{task_id}\n\n"
        f"- **Repository:** `{repo_name}`\n"
        f"- **Target file:** `{file_path}` ({exists_note})\n"
        f"- **Base branch:** `{base_branch}`\n"
        f"- **Staged on branch:** `{target_branch or f'max-evolution-{task_id}'}`\n"
        f"- **Created:** {created} (staged by Max on Railway via `!update_repo`)\n\n"
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
# Chat mode: bot mention or "max …" with no prefix
# --------------------------------------------------------------------------
CHAT_HELP = (
    "👋 **Max chat mode** — just talk to me, no prefix needed:\n"
    "• `max help me with src/app.js in owner/repo — <what to change>`\n"
    "• Mention this bot, then ask it to fix a file in owner/repo\n"
    "• `max search <query> for me`\n"
    "• `max what do you remember about owner/repo`\n"
    "• `max stats` — token usage, estimated cost and sandbox quota\n"
    "• `max set timezone Asia/Kolkata` · `max what time is it`\n"
    "• `max remember <anything>` · `max show notes`\n"
    "• `max integrations` — list the pluggable modules\n"
    "Prefix commands still work: `!max_fix`, `!update_repo`, `!max_brain`, `!max_stats`."
)

PERSONA_CARD = (
    "🤖 **I'm Max** — your coding companion and personal assistant.\n"
    "**Coding:** prepare and validate a patch; GitHub publication needs trusted approval.\n"
    "**Web:** `max search <query> for me`.\n"
    "**Memory:** a repo brain (`max what do you remember about owner/repo`) "
    "and your private notes (`max remember …`).\n"
    "**Daily stuff:** time in your timezone (`max set timezone Asia/Kolkata`), "
    "usage stats (`max stats`).\n"
    "**Extensions:** `max integrations` lists the pluggable modules — drop a new file "
    "into `integrations/` to add one.\n"
    "No prefix needed — just start with `max` or tag me."
)

FILE_EXT_PATTERN = (
    r"py|js|jsx|ts|tsx|java|go|rs|rb|php|css|html|scss|json|md|yml|yaml|"
    r"sh|c|cpp|h|cs|swift|kt|txt|toml|ini|cfg"
)
FILE_PATH_RE = re.compile(rf"\b[\w][\w./-]*\.(?:{FILE_EXT_PATTERN})\b", re.IGNORECASE)
REPO_RE = re.compile(r"\b([\w.-]{1,39})/([\w.-]{1,100})\b")
CHAT_NAME_RE = re.compile(r"^\s*(?:hey\s+|yo\s+|oi\s+)?(?:max|mex|flanx)\b[:,]?\s*(.*)$", re.IGNORECASE | re.DOTALL)
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
    r"(?:\s+(?:yo|hi|hey|there|sup|bro|man|dude|max|mex|flanx|guys))?\s*$"
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
    """'max help me with this' -> 'help me with this'; None when Max is not addressed."""
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
    # Only enter the code-repair workflow when the user gives a repair/update verb,
    # a repository, or a file path. Ordinary questions should receive an answer.
    if not (repo or file_path or FIX_VERB_RE.match(stripped)):
        return "general", {"prompt": stripped}
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
    print(f"🤖 Max online as {bot.user} (chat mode + pending repository actions)")
    await bot.change_presence(
        activity=discord.Game(name="just say 'max …' · !max_fix · !update_repo")
    )


@bot.event
async def on_message(message):
    """Chat mode: a bot mention or 'max …' with no prefix, alongside !commands."""
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

    intent, payload = chat_intent(remainder, get_pref(message.author.id, "repo", FLANX_DEFAULT_REPO))
    channel = message.channel
    try:
        if intent == "search":
            query = payload["query"]
            if not query:
                await channel.send(
                    "🔎 What should I look up? e.g. `max search python 3.14 changes for me`"
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
                f"{opener} I'm Max — I patch code on GitHub, search the web, keep time "
                "and take notes for you. Try `max help` for the full menu."
            )
        elif intent == "brain":
            repo = payload["repo"]
            if not repo:
                await channel.send(
                    "🧠 Which repository? e.g. `max what do you remember about owner/repo`"
                )
            else:
                memory = await asyncio.to_thread(read_flanx_brain, repo)
                await channel.send(f"🧠 **Brain memory for `{repo}`**\n```\n{clip(memory, 1800)}\n```")
        elif intent == "general":
            async with channel.typing():
                task = Task.new("General chat response", "general_chat",
                                permitted_tools={"model.reply"}, timeout_seconds=120)
                outcome = await chat_orchestrator.run(task, payload["prompt"])
            if outcome.task.status == Status.COMPLETED:
                await channel.send(clip(outcome.result, 1900))
            else:
                await channel.send("⚠️ Max could not complete that request. Please try again.")
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
                    "e.g. `max help me with src/app.js in owner/repo — <what to change>`"
                )
            else:
                async with channel.typing():
                    await fix_pipeline(channel, repo, file_path, goal, requester_id=message.author.id)
        else:
            await channel.send(CHAT_HELP)
    except Exception:
        await channel.send("⚠️ Max could not complete that request. No details were shared.")

    await bot.process_commands(message)


async def generate_assistant_reply(prompt: str, task: str = "general") -> str:
    """Answer Discord questions through the shared privacy-aware model router."""
    try:
        reply = await model_router.generate(
            [
                {
                    "role": "system",
                    "content": (
                        "You are Max, a friendly personal AI companion. Be useful, clear, "
                        "and honest about uncertainty. Do not claim to have taken actions "
                        "unless a tool actually performed them. Keep answers concise unless "
                        "the user asks for detail."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            task=task, data_class="private_repo", max_tokens=700,
        )
    except ModelError as exc:
        return str(exc)
    if reply.prompt_tokens is not None or reply.completion_tokens is not None:
        record_llm_usage(reply.model, reply)
    return reply.text


async def _general_chat_handler(task: Task, prompt: str) -> str:
    return await generate_assistant_reply(prompt, task="general")


chat_orchestrator = Orchestrator(store_path=TASK_DB)
chat_orchestrator.register(
    "general_chat", "model.reply", _general_chat_handler,
    valid_input=lambda prompt: isinstance(prompt, str) and bool(prompt.strip()),
    valid_result=lambda reply: isinstance(reply, str) and bool(reply.strip()),
)


async def _publish_fix_handler(task: Task, payload: dict) -> str:
    if not REPOSITORY_WRITES_ENABLED:
        raise ActionNotStarted("Legacy repository publication is disabled")
    if task.target != f'{payload["repo"]}:{payload["branch"]}:{payload["file"]}':
        raise ValueError("Write target does not match approved scope")
    repo = await asyncio.to_thread(gh.get_repo, payload["repo"])
    if repo.default_branch != payload["base_branch"]:
        raise ValueError("Base branch changed before publication")
    base = await asyncio.to_thread(repo.get_branch, payload["base_branch"])
    current = await asyncio.to_thread(repo.get_contents, payload["file"], ref=payload["base_branch"])
    if isinstance(current, list):
        raise ValueError("Target is a directory")
    await write_orchestrator.revalidate_approval(task, payload)
    return await asyncio.to_thread(
        _open_pull_request_after_approval, repo, payload["file"], payload["content"],
        payload["goal"], payload["validation"], payload["branch"],
        payload["base_branch"], base.commit.sha, current.sha,
    )


async def _stage_manifest_handler(task: Task, payload: dict) -> str:
    if not REPOSITORY_WRITES_ENABLED:
        raise ActionNotStarted("Legacy repository staging is disabled")
    if task.target != f'{payload["repo"]}:{payload["branch"]}:{payload["manifest_path"]}':
        raise ValueError("Write target does not match approved scope")
    repo = await asyncio.to_thread(gh.get_repo, payload["repo"])
    if repo.default_branch != payload["base_branch"]:
        raise ValueError("Base branch changed before staging")
    base = await asyncio.to_thread(repo.get_branch, payload["base_branch"])
    await write_orchestrator.revalidate_approval(task, payload)
    def stage() -> str:
        repo.create_git_ref(ref=f'refs/heads/{payload["branch"]}', sha=base.commit.sha)
        repo.create_file(
            path=payload["manifest_path"],
            message=f'Max: stage task manifest',
            content=payload["manifest"],
            branch=payload["branch"],
        )
        return github_file_url(payload["repo"], payload["branch"], payload["manifest_path"])
    return await asyncio.to_thread(stage)


def _valid_repository_action(payload: object, keys: set[str]) -> bool:
    if not (isinstance(payload, dict) and set(payload) == keys
            and all(isinstance(value, str) and bool(value.strip()) for value in payload.values())):
        return False
    repo = payload["repo"]
    path = payload["file"]
    branch = payload["branch"]
    if (re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None
            or not path or path.startswith("/") or "\\" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            or re.fullmatch(r"max-(?:fix|evolution)-[A-Za-z0-9-]+", branch) is None):
        return False
    if "manifest_path" in payload and re.fullmatch(r"tasks/update-job-[0-9]+\.md", payload["manifest_path"]) is None:
        return False
    return len(payload.get("content", "").encode("utf-8")) <= 60_000 and len(payload.get("manifest", "").encode("utf-8")) <= 16_000


write_orchestrator = Orchestrator(store_path=TASK_DB)
REPOSITORY_WRITES_ENABLED = False  # legacy handlers stay off; test PRs use PublishingCoordinator
write_orchestrator.register(
    "publish_fix", "repository.push", _publish_fix_handler,
    valid_input=lambda payload: _valid_repository_action(payload, {
        "repo", "base_branch", "branch", "file", "content", "goal", "validation", "requester_id"}),
    valid_result=lambda result: isinstance(result, str) and result.startswith("https://"),
)
write_orchestrator.register(
    "stage_manifest", "repository.stage", _stage_manifest_handler,
    valid_input=lambda payload: _valid_repository_action(payload, {
        "repo", "base_branch", "branch", "file", "manifest_path", "manifest", "requester_id"}),
    valid_result=lambda result: isinstance(result, str) and result.startswith("https://"),
)


async def _pending_repository_action(task_type: str, tool: str, target: str,
                                     payload: dict, requester_id: int) -> str:
    """Compatibility path for repository actions without an enabled publishing flow."""
    payload = {**payload, "requester_id": str(requester_id)}
    action = {"publish_fix": "Publish validated patch and pull request",
              "stage_manifest": "Stage task manifest on a new branch"}[task_type]
    task = Task.new(action, task_type,
                    permitted_tools={tool}, target=target, timeout_seconds=300)
    outcome = await write_orchestrator.run(task, payload)
    if outcome.task.status != Status.AWAITING_APPROVAL:
        return "⚠️ Could not prepare the repository action. No write was made."
    return (f"⏸️ Repository action pending approval (task `{task.task_id}`). "
            "This operation has no enabled publishing approval control, so no repository write was made.")


async def fix_pipeline(ctx, repo_name: str, file_path: str, diagnostic_goal: str,
                       *, requester_id: int) -> None:
    """Full repair cycle. `ctx` is a command ctx or any Discord messageable with .send()."""
    try:
        if not MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT:
            await ctx.send(
                "⚠️ Repository repair is paused: remote sandbox calls need an explicit "
                "repository-content privacy decision. Set MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT=true "
                "only after approving that data flow. No source code was sent or written."
            )
            return
        await ctx.send(f"📡 Max is pulling `{file_path}` from `{repo_name}`...")
        repo = await asyncio.to_thread(gh.get_repo, repo_name)
        default_branch = repo.default_branch

        def fetch_file():
            return repo.get_contents(file_path, ref=default_branch)

        file_obj = await asyncio.to_thread(fetch_file)
        if isinstance(file_obj, list):
            await ctx.send(f"⚠️ `{file_path}` is a directory — point Max at a single file.")
            return
        original_code = file_obj.decoded_content.decode("utf-8", errors="replace")
        if CREDENTIAL_RE.search(original_code) or CREDENTIAL_RE.search(diagnostic_goal):
            await ctx.send("⚠️ Repair stopped because the input may contain credentials. No model or sandbox call was made.")
            return

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
        if any(CREDENTIAL_RE.search(item) for item in (past_memories, live_web_intel, baseline_report)):
            await ctx.send("⚠️ Repair stopped because context may contain credentials. No model call or repository write was made.")
            return
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
        model, temperature = model_router.selected_model("coding"), 0.1
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
            except ModelError as exc:
                await ctx.send(f"Model request stopped: {exc} No repository write was made.")
                return
            except Exception:
                await ctx.send("⚠️ Model request failed. No repository write was made.")
                return
            if not raw_patch.strip():
                await ctx.send("⚠️ The model returned an empty patch — aborting before any push.")
                return
            if CREDENTIAL_RE.search(raw_patch):
                await ctx.send("⚠️ The proposed patch may contain credentials. No sandbox or repository write was made.")
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
            if CREDENTIAL_RE.search(retry_feedback):
                await ctx.send("⚠️ Sandbox feedback may contain credentials. No retry or repository write was made.")
                return
        else:
            exit_code = validation.exit_code if validation and validation.exit_code is not None else "n/a"
            detail = (validation.stderr or validation.error) if validation else "no run result"
            insight = (
                f"FAILED patch for `{file_path}` after {FIX_ATTEMPTS} attempt(s) "
                f"(goal: {diagnostic_goal[:60]}) — sandbox exit {exit_code}: {clip(detail, 140)}"
            )
            await ctx.send(
                f"⚠️ **Patch failed sandbox validation after {FIX_ATTEMPTS} attempt(s).** "
                "No repository write was made.\n"
                f"{validation.for_discord() if validation else 'no run result'}"
            )
            return

        branch = f"max-fix-{int(datetime.now(timezone.utc).timestamp())}-{requester_id}-{uuid.uuid4().hex[:8]}"
        if publisher.config.ready and repo_name == publisher.config.test_repo:
            try:
                prepared = await publisher.prepare(
                    repo=repo_name, base=default_branch, branch=branch, file=file_path,
                    original=original_code, content=raw_patch, goal=diagnostic_goal,
                    validation=validation.for_prompt("validation run"),
                    validation_passed=validation.passed, requester_id=requester_id,
                )
            except PublishError as exc:
                await ctx.send(f"Patch passed sandbox validation, but approval preparation stopped: {exc}. No write was made.")
                return
            view = PublishApprovalView(prepared)
            view.message = await ctx.send(prepared.summary, view=view,
                                          allowed_mentions=discord.AllowedMentions.none())
        else:
            pending = await _pending_repository_action(
                "publish_fix", "repository.push", f"{repo_name}:{branch}:{file_path}",
                {"repo": repo_name, "base_branch": default_branch, "branch": branch,
                 "file": file_path, "content": raw_patch, "goal": diagnostic_goal,
                 "validation": validation.for_prompt("validation run")}, requester_id,
            )
            await ctx.send("✅ Patch passed sandbox validation. " + pending)

    except GithubException as exc:
        await ctx.send(f"⚠️ GitHub request failed (HTTP {exc.status}). No repository write was made.")
    except Exception:
        await ctx.send("⚠️ Repair preparation failed. No repository write was made.")


@bot.command(name="max_fix", aliases=["mex_fix", "flanx_fix"])
async def flanx_fix(ctx, repo_name: str, file_path: str, *, diagnostic_goal: str):
    """Prepare a validated patch; GitHub publication waits for trusted approval."""
    async with ctx.typing():
        await fix_pipeline(ctx, repo_name, file_path, diagnostic_goal, requester_id=ctx.author.id)


@bot.command(name="update_repo")
async def update_repo(ctx, repo_name: str, file_path: str, *, instruction: str):
    """Prepare a scoped task manifest; GitHub staging waits for trusted approval."""
    async with ctx.typing():
        try:
            await ctx.send(f"📡 Checking `{repo_name}` before preparing a task manifest...")
            repo = await asyncio.to_thread(gh.get_repo, repo_name)
            base_branch = repo.default_branch
            task_id = int(ctx.message.created_at.timestamp())
            target_branch = f"max-evolution-{task_id}-{ctx.author.id}-{uuid.uuid4().hex[:8]}"
            manifest_path = f"{TASKS_DIR}/update-job-{task_id}.md"
            try:
                await asyncio.to_thread(repo.get_contents, file_path, ref=base_branch)
                target_exists = True
            except UnknownObjectException:
                target_exists = False
            manifest = build_task_manifest(
                task_id, repo_name, file_path, instruction, base_branch, target_exists,
                target_branch=target_branch,
            )
            pending = await _pending_repository_action(
                "stage_manifest", "repository.stage",
                f"{repo_name}:{target_branch}:{manifest_path}",
                {"repo": repo_name, "base_branch": base_branch, "branch": target_branch,
                 "file": file_path, "manifest_path": manifest_path, "manifest": manifest},
                ctx.author.id,
            )
            await ctx.send(pending)

        except GithubException as exc:
            await ctx.send(f"⚠️ GitHub request failed (HTTP {exc.status}). No repository write was made.")
        except Exception:
            await ctx.send("⚠️ Task staging preparation failed. No repository write was made.")


@bot.command(name="max_publish_status")
async def max_publish_status(ctx, task_id: str):
    """Authorized, read-only reconciliation of a possibly completed test PR."""
    if not publisher.config.ready or ctx.author.id not in publisher.config.approver_ids:
        await ctx.send("Publishing status is unavailable or you are not authorized.")
        return
    record = publisher.store.load(task_id) if re.fullmatch(r"[0-9a-f]{32}", task_id) else None
    if (record is None or record.get("requester_id") != str(ctx.author.id)
            or record.get("repo") != publisher.config.test_repo):
        await ctx.send("Publishing task is unavailable.")
        return
    state = record.get("state")
    if state in ("executing", "outcome_uncertain", "reconciliation_required"):
        state = await publisher.reconcile(task_id)
    await ctx.send(f"Publishing task `{task_id}`: `{state}`. "
                   "Reconciliation never retries a write. Partial results need a new reviewed request.")


@bot.command(name="max_brain", aliases=["mex_brain", "flanx_brain"])
async def flanx_brain(ctx, repo_name: str):
    """Show what Max remembers from past runs in a repository."""
    memory = await asyncio.to_thread(read_flanx_brain, repo_name)
    await ctx.send(f"🧠 **Brain memory for `{repo_name}`**\n```\n{clip(memory, 1800)}\n```")


@bot.command(name="max_stats", aliases=["mex_stats", "flanx_stats"])
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
