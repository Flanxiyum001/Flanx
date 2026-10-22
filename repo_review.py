"""Read-only bounded GitHub repository discovery and review evidence.

Retrieved files are untrusted content, never instructions to the assistant.
No repository writes occur here.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

REPO_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?",
    re.IGNORECASE,
)
REPO_ID_RE = re.compile(
    r"(?<![\w./-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?![\w./-])"
)
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.-]+")
FILE_EXTENSIONS = {
    "py", "js", "jsx", "ts", "tsx", "html", "css", "json", "md", "yml",
    "yaml", "toml", "txt", "rs", "go", "sh", "java",
}
MAX_TREE_ENTRIES = 300
MAX_FILES = 14
MAX_CONTENT_CHARS = 18000
MAX_ONE_FILE_CHARS = 2600

_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^(\s*[\w-]*(?:api_key|apikey|token|password|secret|private_key|"
    r"access_key|authorization)[\w-]*\s*[:=]\s*)"
    r"(\x22[^\x22\n]*\x22|'[^'\n]*'|[^\s#\n]+)"
)
_SECRET_TOKEN = re.compile(r"(?i)\b(?:ghp_|gho_|github_pat_|sk-)[A-Za-z0-9_-]{12,}\b")


def _redact_review_excerpt(content: str) -> str:
    """Minimize obvious credential leakage into model review context."""
    lines = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", content)
    return _SECRET_TOKEN.sub("[REDACTED]", lines)

# Conservative smoke-test guard: only flag likely literal credentials,
# not ordinary safe lookups such as TOKEN = os.getenv("TOKEN").
_LITERAL_SECRET = re.compile(
    r"(?im)^\s*[A-Za-z_]*(?:api_key|token|password|secret|private_key)"
    r"[A-Za-z_]*\s*[:=]\s*['\"][^'\"\n]{16,}['\"]"
)


def contains_potential_secret(content: str) -> bool:
    """Block sending likely embedded credentials to external execution/LLM."""
    return bool(_SECRET_TOKEN.search(content or "") or _LITERAL_SECRET.search(content or ""))




def normalize_repository(identifier: str) -> str:
    """Normalize a GitHub repository URL or owner/name; reject other hosts."""
    value = (identifier or "").strip()
    if value.lower().startswith(("https://", "http://")):
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"}
                or (url.hostname or "").lower() not in {"github.com", "www.github.com"}
                or url.username or url.password or url.port
                or url.query or url.fragment):
            raise ValueError("Only GitHub repository URLs are supported")
        parts = [part for part in url.path.split("/") if part]
        if len(parts) != 2:
            raise ValueError("Expected a GitHub repository URL with owner and name")
        owner, name = parts
    else:
        parts = value.split("/")
        if len(parts) != 2:
            raise ValueError("Expected owner/repo or a GitHub repository URL")
        owner, name = parts
    if name.endswith(".git"):
        name = name[:-4]
    if (not 1 <= len(owner) <= 39 or not 1 <= len(name) <= 100
            or owner in {".", ".."} or name in {".", ".."}
            or not IDENTIFIER_RE.fullmatch(owner)
            or not IDENTIFIER_RE.fullmatch(name)):
        raise ValueError("Invalid GitHub repository identifier")
    return f"{owner}/{name}"


def extract_repository(message: str) -> str | None:
    """Prefer a full GitHub URL; never confuse it with a source file path."""
    text = message or ""
    url = REPO_URL_RE.search(text)
    if url:
        return normalize_repository(url.group().rstrip("/"))
    for match in REPO_ID_RE.finditer(text):
        candidate = match.group(1)
        owner, name = candidate.split("/", 1)
        if owner.lower() in {"github.com", "www.github.com", "raw.githubusercontent.com"}:
            continue
        if "." in name and name.rsplit(".", 1)[-1].lower() in FILE_EXTENSIONS:
            continue
        try:
            return normalize_repository(candidate)
        except ValueError:
            continue
    return None


def validate_target_path(path: str) -> str:
    """A single repository-relative, non-special file path for a requested edit."""
    value = (path or "").strip()
    if (not value or len(value) > 260 or "\\" in value or "\x00" in value
            or value.startswith(("/", "~")) or "://" in value):
        raise ValueError("Provide a repository-relative file path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or part.startswith(".git") for part in parts):
        raise ValueError("Path traversal and Git metadata paths are not allowed")
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise ValueError("Invalid characters in target file path")
    return value


def _candidate_files(paths: list[str]) -> list[str]:
    """Select a bounded representative sample with docs, configuration, code, tests."""
    priorities = {
        "readme.md": 0, "pyproject.toml": 1, "requirements.txt": 1,
        "package.json": 1, "cargo.toml": 1, "go.mod": 1,
        "dockerfile": 2, ".github/workflows/tests.yml": 2,
    }
    def order(path: str):
        lower = path.lower()
        if lower in priorities:
            priority = priorities[lower]
        elif lower.startswith(("tests/", "test/", "__tests__/")):
            priority = 3
        elif lower.startswith(("src/", "app/", "lib/")):
            priority = 4
        elif lower.endswith((".py", ".js", ".ts", ".rs", ".go")):
            priority = 5
        else:
            priority = 10
        return priority, path.count("/"), path
    def allowed(path: str) -> bool:
        lower = path.lower()
        parts = lower.split("/")
        if any(part in {".env", ".git", ".ssh", "secrets", "credentials"}
               or part.startswith(".env.") for part in parts):
            return False
        if lower in priorities:
            return True
        if lower.startswith(("tests/", "test/", "__tests__/", "src/", "app/", "lib/")):
            return lower.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".md"))
        return lower.endswith((".py", ".js", ".ts", ".go", ".rs")) and "/" not in lower

    return sorted((path for path in paths if allowed(path)), key=order)[:MAX_FILES]


def inspect_repository(client, identifier: str) -> dict:
    """Fetch real metadata, recursive tree, and selected text files (no writes)."""
    full_name = normalize_repository(identifier)
    repository = client.get_repo(full_name)
    branch = repository.default_branch
    if bool(repository.private):
        # Private tree paths can reveal internal architecture. Do not even
        # fetch them into the Discord process without a separate approved flow.
        return {
            "name": repository.full_name, "branch": branch, "private": True,
            "files": [], "failed": [], "truncated": False,
            "context": (
                f"Private GitHub repository {repository.full_name} on {branch}; "
                "metadata access only. No tree or source contents retrieved."
            ),
        }
    tree = repository.get_git_tree(branch, recursive=True)
    entries = list(tree.tree)
    file_paths = [
        node.path for node in entries
        if node.type == "blob" and node.path
    ]
    visible = file_paths[:MAX_TREE_ENTRIES]
    summary = [
        "REPOSITORY METADATA (retrieved from GitHub):",
        f"name: {repository.full_name}",
        f"default branch: {branch}",
        f"private: {bool(repository.private)}",
        f"description: {(repository.description or '')[:300]!r}",
        f"GitHub reported {len(file_paths)} tree file entries",
        "TREE PATHS (sample; not file contents):",
        *visible[:100],
    ]
    inspected, failed = [], []
    budget = MAX_CONTENT_CHARS
    for path in _candidate_files(file_paths):
        if budget < 200:
            break
        try:
            item = repository.get_contents(path, ref=branch)
            if isinstance(item, list):
                continue
            raw = item.decoded_content
            if len(raw) > 40000:
                failed.append(f"{path} (too large to inspect)")
                continue
            body = raw.decode("utf-8")
        except (UnicodeError, ValueError):
            failed.append(f"{path} (not UTF-8 text)")
            continue
        except Exception:
            failed.append(f"{path} (retrieval failed)")
            continue
        length = min(MAX_ONE_FILE_CHARS, budget)
        snippet = _redact_review_excerpt(body[:length])
        summary.append(
            f"\n--- UNTRUSTED FILE CONTENT: {path} "
            f"({len(body)} chars; {'partial' if len(body) > length else 'complete'}) ---\n{snippet}"
        )
        inspected.append(path)
        budget -= len(snippet)
    if not file_paths:
        raise ValueError("GitHub returned no files to inspect; cannot claim a review")
    summary.append("\nINSPECTED PATHS: " + ", ".join(inspected))
    summary.append("NOT RETRIEVED: " + ", ".join(failed))
    summary.append(
        "This is a bounded sample, not a complete security audit; do not "
        "claim uninspected code or tests were executed."
    )
    return {
        "name": repository.full_name, "branch": branch, "private": False,
        "files": inspected, "failed": failed,
        "truncated": bool(getattr(tree, "truncated", False)) or len(file_paths) > MAX_TREE_ENTRIES
        or len(file_paths) > len(inspected),
        "context": "\n".join(summary)[:MAX_CONTENT_CHARS + 2000],
    }
