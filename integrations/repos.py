"""GitHub repository discovery, selection, and lightweight reconnaissance."""
from __future__ import annotations
import asyncio, os, re
from github import Github, GithubException
from prefs import get_pref, set_pref
NAME = "repos"
DESCRIPTION = "list accessible GitHub repos, choose a default, and inspect project instructions"
USAGE = ["flanx repos", "flanx use owner/repo", "flanx current repo", "flanx inspect owner/repo"]
LIST_RE = re.compile(r"^\s*repos(?:\s+(\d{1,3}))?\s*$", re.I)
USE_RE = re.compile(r"^\s*use\s+([\w.-]+/[\w.-]+)\s*$", re.I)
INSPECT_RE = re.compile(r"^\s*inspect\s+([\w.-]+/[\w.-]+)\s*$", re.I)
CURRENT_RE = re.compile(r"^\s*(?:current repo|my repo|selected repo)\s*$", re.I)
PER_PAGE = 10
MAX_PAGE = 100

def match(text: str) -> dict | None:
    text = (text or "").strip()
    found = LIST_RE.match(text)
    if found: return {"action": "list", "page": max(1, min(int(found.group(1) or "1"), MAX_PAGE))}
    found = USE_RE.match(text)
    if found: return {"action": "use", "repository": found.group(1)}
    found = INSPECT_RE.match(text)
    if found: return {"action": "inspect", "repository": found.group(1)}
    if CURRENT_RE.match(text): return {"action": "current"}
    return None

def _github():
    token = os.getenv("GITHUB_TOKEN")
    if not token: raise RuntimeError("GITHUB_TOKEN is not configured")
    return Github(token, per_page=PER_PAGE)

def _list_repos(page: int) -> tuple[list[dict], bool]:
    user = _github().get_user()
    # Pull only the requested page; no unbounded repository enumeration.
    start = (page - 1) * PER_PAGE
    repos = user.get_repos(sort="updated", direction="desc", affiliation="owner,collaborator,organization_member")
    rows = []
    for repo in repos.get_page(page - 1):
        rows.append({"full_name": repo.full_name, "private": repo.private, "updated": repo.updated_at.strftime("%Y-%m-%d") if repo.updated_at else "unknown", "description": (repo.description or "").replace("\n", " ")[:100]})
    return rows, len(rows) == PER_PAGE

def _inspect(repository: str) -> str:
    repo = _github().get_repo(repository)
    root = repo.get_contents("")
    names = sorted(item.name + ("/" if item.type == "dir" else "") for item in root)
    summary = [f"**Repository:** `{repo.full_name}`", f"**Default branch:** `{repo.default_branch}`", f"**Visibility:** {'private' if repo.private else 'public'}", f"**Languages:** {', '.join(f'{name} {amount} bytes' for name, amount in list(repo.get_languages().items())[:8]) or 'not detected'}", "**Root entries:** " + ", ".join(names[:30])]
    for path, label, limit in [("AGENTS.md", "Repository instructions", 1800), ("README.md", "README overview", 2200)]:
        try:
            item = repo.get_contents(path, ref=repo.default_branch)
            if item.encoding == "base64":
                import base64
                body = base64.b64decode(item.content).decode("utf-8", errors="replace")
            else: body = item.decoded_content.decode("utf-8", errors="replace")
            summary.append(f"**{label}:**\n{body[:limit]}")
        except GithubException as exc:
            if exc.status != 404: raise
    summary.append("_Reconnaissance only: no files changed and no code was executed._")
    return "\n\n".join(summary)[:7000]

async def handle(event, payload: dict) -> str:
    action = payload["action"]
    if action == "current":
        selected = get_pref(event.author_id, "repo")
        return f"Your selected repository is `{selected}`." if selected else "No repository selected yet. Use `flanx repos`, then `flanx use owner/repo`."
    if action == "use":
        try:
            repo = await asyncio.to_thread(_github().get_repo, payload["repository"])
        except Exception as exc:
            return f"Couldn't verify that repository with the configured GitHub token: {str(exc)[:220]}"
        set_pref(event.author_id, "repo", repo.full_name)
        return f"Selected `{repo.full_name}` as your personal default repository. Say `flanx current repo` to check it, or `flanx inspect {repo.full_name}` to inspect it."
    if action == "inspect":
        try: return await asyncio.to_thread(_inspect, payload["repository"])
        except Exception as exc: return f"Repository inspection failed: {str(exc)[:300]}"
    try:
        rows, has_next = await asyncio.to_thread(_list_repos, payload["page"])
    except Exception as exc:
        return f"Couldn't list repositories: {str(exc)[:300]}"
    if not rows: return f"No repositories found on page {payload['page']}. Try `flanx repos 1` or check GitHub token permissions."
    lines = [f"**Accessible GitHub repositories — page {payload['page']}**"]
    for row in rows:
        privacy = "private" if row["private"] else "public"
        description = f" — {row['description']}" if row["description"] else ""
        lines.append(f"• `{row['full_name']}` ({privacy}, updated {row['updated']}){description}")
    lines.append(f"Use `flanx use owner/repo` to select one. " + (f"Next page: `flanx repos {payload['page'] + 1}`." if has_next else "End of results."))
    return "\n".join(lines)[:1850]
