"""Cross-repository knowledge graph: persistent learning across all repositories.

Stores patterns, fixes, and architectural decisions that Max learns from each
repair session, enabling cross-repo knowledge transfer like:
"In repo X, we solved this bug with pattern Y - want to apply it here?"
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


KNOWLEDGE_DB = os.getenv("MAX_KNOWLEDGE_DB") or os.path.join(
    os.path.dirname(__file__), ".max_state", "knowledge.sqlite3"
)

_lock = threading.Lock()

# Pattern categories for knowledge storage
CATEGORIES = {
    "bug_fix": "Recurring bug patterns and their fixes",
    "security_pattern": "Security vulnerabilities and mitigations",
    "performance": "Performance optimization patterns",
    "architecture": "Architectural decisions and patterns",
    "testing": "Test strategies and patterns",
    "dependency": "Dependency issues and resolutions",
    "configuration": "Configuration patterns and gotchas",
    "style": "Code style and convention preferences",
}


@dataclass
class KnowledgeEntry:
    id: str
    category: str
    pattern_signature: str  # Normalized pattern for matching
    problem_description: str
    solution_code: str
    solution_explanation: str
    repo_name: str
    file_path: str
    language: str
    confidence: float  # 0.0 - 1.0
    usage_count: int
    created_at: str
    updated_at: str
    tags: list[str]


def _init_db() -> None:
    Path(KNOWLEDGE_DB).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(KNOWLEDGE_DB)) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                pattern_signature TEXT NOT NULL,
                problem_description TEXT NOT NULL,
                solution_code TEXT NOT NULL,
                solution_explanation TEXT NOT NULL,
                repo_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                language TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                usage_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                UNIQUE(pattern_signature, repo_name, file_path)
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_knowledge_category
            ON knowledge(category)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_knowledge_signature
            ON knowledge(pattern_signature)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_knowledge_repo
            ON knowledge(repo_name)
        """)


def _normalize_signature(text: str) -> str:
    """Create a normalized signature for pattern matching."""
    # Remove whitespace, comments, variable names - keep structure
    text = re.sub(r'#.*$', '', text, flags=re.MULTILINE)  # Python comments
    text = re.sub(r'//.*$', '', text, flags=re.MULTILINE)  # JS/TS comments
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)  # Block comments
    text = re.sub(r'\s+', ' ', text)  # Normalize whitespace
    text = re.sub(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', 'IDENT', text)  # Normalize identifiers
    text = re.sub(r'\b\d+\b', 'NUM', text)  # Normalize numbers
    text = re.sub(r'["\'].*?["\']', 'STR', text)  # Normalize strings
    return text.strip().lower()[:200]


def _extract_language(file_path: str) -> str:
    ext = file_path.rsplit('.', 1)[-1].lower() if '.' in file_path else ''
    return {
        'py': 'python', 'js': 'javascript', 'ts': 'typescript',
        'tsx': 'typescript', 'jsx': 'javascript', 'go': 'go',
        'rs': 'rust', 'rb': 'ruby', 'java': 'java', 'cpp': 'cpp',
        'c': 'c', 'cs': 'csharp', 'php': 'php', 'sh': 'bash',
    }.get(ext, 'unknown')


def learn_from_repair(
    repo_name: str,
    file_path: str,
    original_code: str,
    fixed_code: str,
    goal: str,
    validation_report: str,
    category: str = "bug_fix",
) -> str | None:
    """Extract and store a knowledge entry from a successful repair."""
    _init_db()
    
    if category not in CATEGORIES:
        category = "bug_fix"
    
    language = _extract_language(file_path)
    signature = _normalize_signature(original_code + " -> " + fixed_code)
    
    # Generate explanation using the fix context
    explanation = (
        f"Problem: {goal[:200]}\n"
        f"Validation: {validation_report[:300]}\n"
        f"Pattern: {signature[:100]}"
    )
    
    # Determine tags from goal and code
    tags = []
    goal_lower = goal.lower()
    if any(kw in goal_lower for kw in ['auth', 'login', 'jwt', 'token', 'password', 'security']):
        tags.append('security')
    if any(kw in goal_lower for kw in ['performance', 'slow', 'optimize', 'cache', 'memory']):
        tags.append('performance')
    if any(kw in goal_lower for kw in ['test', 'coverage', 'mock', 'assert']):
        tags.append('testing')
    if any(kw in goal_lower for kw in ['async', 'await', 'promise', 'concurrent', 'thread']):
        tags.append('async')
    if any(kw in goal_lower for kw in ['database', 'sql', 'query', 'migration', 'orm']):
        tags.append('database')
    if any(kw in goal_lower for kw in ['api', 'endpoint', 'route', 'http', 'rest', 'graphql']):
        tags.append('api')
    
    now = datetime.now(timezone.utc).isoformat()
    entry_id = f"kg_{abs(hash(signature + repo_name + file_path)) % 10**12:012x}"
    
    with _lock:
        with closing(sqlite3.connect(KNOWLEDGE_DB)) as db, db:
            try:
                db.execute("""
                    INSERT OR REPLACE INTO knowledge
                    (id, category, pattern_signature, problem_description, solution_code,
                     solution_explanation, repo_name, file_path, language, confidence,
                     usage_count, created_at, updated_at, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    entry_id, category, signature, goal[:500], fixed_code[:10000],
                    explanation[:1000], repo_name, file_path, language, 0.7,
                    0, now, now, json.dumps(tags)
                ))
                return entry_id
            except sqlite3.Error:
                return None


def query_knowledge(
    problem_description: str,
    language: str | None = None,
    category: str | None = None,
    repo_name: str | None = None,
    limit: int = 5,
) -> list[KnowledgeEntry]:
    """Find relevant knowledge entries for a given problem."""
    _init_db()
    
    query_sig = _normalize_signature(problem_description)
    
    where_clauses = ["1=1"]
    params = []
    
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    if language:
        where_clauses.append("language = ?")
        params.append(language)
    if repo_name:
        where_clauses.append("repo_name = ?")
        params.append(repo_name)
    
    # Also search by pattern similarity (simple text matching on signature)
    where_clauses.append("(pattern_signature LIKE ? OR problem_description LIKE ?)")
    params.extend([f"%{query_sig[:50]}%", f"%{problem_description[:50]}%"])
    
    sql = f"""
        SELECT * FROM knowledge
        WHERE {' AND '.join(where_clauses)}
        ORDER BY confidence DESC, usage_count DESC, updated_at DESC
        LIMIT ?
    """
    params.append(limit)
    
    results = []
    with closing(sqlite3.connect(KNOWLEDGE_DB)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute(sql, params).fetchall():
            results.append(KnowledgeEntry(
                id=row['id'],
                category=row['category'],
                pattern_signature=row['pattern_signature'],
                problem_description=row['problem_description'],
                solution_code=row['solution_code'],
                solution_explanation=row['solution_explanation'],
                repo_name=row['repo_name'],
                file_path=row['file_path'],
                language=row['language'],
                confidence=row['confidence'],
                usage_count=row['usage_count'],
                created_at=row['created_at'],
                updated_at=row['updated_at'],
                tags=json.loads(row['tags'] or '[]'),
            ))
    return results


def record_knowledge_usage(entry_id: str, success: bool = True) -> None:
    """Update usage statistics for a knowledge entry."""
    _init_db()
    with _lock:
        with closing(sqlite3.connect(KNOWLEDGE_DB)) as db, db:
            if success:
                db.execute("""
                    UPDATE knowledge
                    SET usage_count = usage_count + 1,
                        confidence = MIN(1.0, confidence + 0.05),
                        updated_at = ?
                    WHERE id = ?
                """, (datetime.now(timezone.utc).isoformat(), entry_id))
            else:
                db.execute("""
                    UPDATE knowledge
                    SET confidence = MAX(0.1, confidence - 0.1),
                        updated_at = ?
                    WHERE id = ?
                """, (datetime.now(timezone.utc).isoformat(), entry_id))


def get_knowledge_stats() -> dict[str, Any]:
    """Get statistics about the knowledge graph."""
    _init_db()
    with closing(sqlite3.connect(KNOWLEDGE_DB)) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT COUNT(*) as c FROM knowledge").fetchone()['c']
        by_category = {}
        for row in db.execute("SELECT category, COUNT(*) as c FROM knowledge GROUP BY category").fetchall():
            by_category[row['category']] = row['c']
        by_language = {}
        for row in db.execute("SELECT language, COUNT(*) as c FROM knowledge GROUP BY language").fetchall():
            by_language[row['language']] = row['c']
        by_repo = {}
        for row in db.execute("SELECT repo_name, COUNT(*) as c FROM knowledge GROUP BY repo_name ORDER BY c DESC LIMIT 10").fetchall():
            by_repo[row['repo_name']] = row['c']
    return {
        "total_entries": total,
        "by_category": by_category,
        "by_language": by_language,
        "top_repositories": by_repo,
    }


def format_knowledge_for_context(entries: list[KnowledgeEntry], max_chars: int = 3000) -> str:
    """Format knowledge entries for inclusion in LLM prompts."""
    if not entries:
        return "No relevant cross-repository knowledge found."
    
    lines = ["--- CROSS-REPOSITORY KNOWLEDGE (learned from past repairs) ---"]
    total = 0
    for entry in entries:
        block = (
            f"\n📚 [{entry.category.upper()}] {entry.repo_name}/{entry.file_path} ({entry.language})\n"
            f"Problem: {entry.problem_description[:200]}\n"
            f"Solution: {entry.solution_explanation[:300]}\n"
            f"Code pattern: {entry.solution_code[:400]}\n"
            f"Confidence: {entry.confidence:.2f} | Used: {entry.usage_count}x | Tags: {', '.join(entry.tags) or 'none'}\n"
        )
        if total + len(block) > max_chars:
            lines.append(f"\n... and {len(entries) - len(lines) + 1} more entries")
            break
        lines.append(block)
        total += len(block)
    
    return "\n".join(lines)