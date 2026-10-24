"""Proactive & Predictive Assistance Engine for Max.

Anticipates user needs based on context:
- File being edited → suggests related improvements
- Repository patterns → predicts likely issues
- User history → personalizes suggestions
- "Jarvis, I'm working on X" → pre-loads relevant context
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable

from knowledge_graph import query_knowledge, get_knowledge_stats, format_knowledge_for_context
from prefs import get_pref, list_notes


PROACTIVE_DB = os.getenv("MAX_PROACTIVE_DB") or os.path.join(
    os.path.dirname(__file__), ".max_state", "proactive.sqlite3"
)

# Context patterns that trigger proactive suggestions
TRIGGER_PATTERNS = {
    "auth_file": {
        "patterns": [r"auth", r"login", r"jwt", r"token", r"password", r"session", r"oauth", r"permission"],
        "suggestions": [
            "Add rate limiting to auth endpoints",
            "Implement refresh token rotation",
            "Add security headers (CSP, HSTS)",
            "Audit for timing attacks on password comparison",
            "Add audit logging for auth events",
        ],
        "knowledge_categories": ["security_pattern", "bug_fix"],
    },
    "payment_file": {
        "patterns": [r"payment", r"stripe", r"billing", r"subscription", r"checkout", r"webhook"],
        "suggestions": [
            "Verify idempotency keys for webhook handling",
            "Add proper error handling for failed payments",
            "Implement retry logic with exponential backoff",
            "Add audit trail for all payment events",
            "Validate webhook signatures",
        ],
        "knowledge_categories": ["security_pattern", "bug_fix", "architecture"],
    },
    "database_file": {
        "patterns": [r"model", r"schema", r"migration", r"query", r"orm", r"repository", r"dao"],
        "suggestions": [
            "Add database indexes for frequently queried columns",
            "Implement connection pooling",
            "Add query timeout protection",
            "Consider read replicas for scaling reads",
            "Add migration rollback procedures",
        ],
        "knowledge_categories": ["performance", "architecture", "bug_fix"],
    },
    "api_file": {
        "patterns": [r"api", r"endpoint", r"route", r"controller", r"handler", r"rest", r"graphql"],
        "suggestions": [
            "Add input validation and sanitization",
            "Implement rate limiting per endpoint",
            "Add request/response logging",
            "Consider API versioning strategy",
            "Add OpenAPI/Swagger documentation",
        ],
        "knowledge_categories": ["architecture", "security_pattern", "bug_fix"],
    },
    "test_file": {
        "patterns": [r"test", r"spec", r"mock", r"fixture", r"assert", r"coverage"],
        "suggestions": [
            "Add edge case tests for error paths",
            "Implement property-based testing for complex logic",
            "Add integration tests for critical flows",
            "Consider contract testing for APIs",
            "Add performance benchmarks",
        ],
        "knowledge_categories": ["testing", "architecture"],
    },
    "config_file": {
        "patterns": [r"config", r"settings", r"\.env", r"yaml", r"toml", r"json"],
        "suggestions": [
            "Validate config at startup with schema",
            "Separate secrets from config (use env vars)",
            "Add config hot-reload capability",
            "Document all configuration options",
            "Add environment-specific configs",
        ],
        "knowledge_categories": ["configuration", "security_pattern"],
    },
    "async_file": {
        "patterns": [r"async", r"await", r"promise", r"future", r"concurrent", r"thread", r"pool"],
        "suggestions": [
            "Add proper error handling for async operations",
            "Implement timeout and cancellation",
            "Consider using semaphores for resource limiting",
            "Add structured logging with correlation IDs",
            "Avoid blocking calls in async functions",
        ],
        "knowledge_categories": ["performance", "bug_fix", "architecture"],
    },
}


@dataclass
class UserContext:
    user_id: int
    current_repo: str | None = None
    current_file: str | None = None
    current_goal: str | None = None
    recent_files: list[str] = field(default_factory=list)
    recent_goals: list[str] = field(default_factory=list)
    active_context: str = "general"  # coding, debugging, reviewing, planning
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    preferences: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProactiveSuggestion:
    trigger: str
    title: str
    description: str
    action_type: str  # "suggest", "preload", "warn", "auto_fix"
    priority: int  # 1=high, 2=medium, 3=low
    context_data: dict[str, Any] = field(default_factory=dict)
    knowledge_entries: list = field(default_factory=list)


class ProactiveEngine:
    """Main engine for proactive assistance."""
    
    def __init__(self):
        self.user_contexts: dict[int, UserContext] = {}
        self._init_db()
    
    def _init_db(self) -> None:
        Path(PROACTIVE_DB).parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(PROACTIVE_DB)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS user_context (
                    user_id INTEGER PRIMARY KEY,
                    context_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS suggestion_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    suggestion TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    accepted BOOLEAN,
                    created_at TEXT NOT NULL
                )
            """)
    
    def update_context(self, user_id: int, **kwargs) -> UserContext:
        """Update user's current context."""
        ctx = self.user_contexts.get(user_id, UserContext(user_id=user_id))
        for key, value in kwargs.items():
            if hasattr(ctx, key):
                setattr(ctx, key, value)
        ctx.last_activity = datetime.now(timezone.utc)
        self.user_contexts[user_id] = ctx
        self._persist_context(ctx)
        return ctx
    
    def _persist_context(self, ctx: UserContext) -> None:
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(PROACTIVE_DB)) as db, db:
            db.execute("""
                INSERT OR REPLACE INTO user_context (user_id, context_json, updated_at)
                VALUES (?, ?, ?)
            """, (ctx.user_id, json.dumps({
                "current_repo": ctx.current_repo,
                "current_file": ctx.current_file,
                "current_goal": ctx.current_goal,
                "recent_files": ctx.recent_files[-20:],
                "recent_goals": ctx.recent_goals[-20:],
                "active_context": ctx.active_context,
                "preferences": ctx.preferences,
            }), ctx.last_activity.isoformat()))
    
    def get_context(self, user_id: int) -> UserContext:
        """Get or load user context."""
        if user_id in self.user_contexts:
            return self.user_contexts[user_id]
        
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(PROACTIVE_DB)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT context_json FROM user_context WHERE user_id = ?", (user_id,)
            ).fetchone()
        
        if row:
            data = json.loads(row['context_json'])
            ctx = UserContext(
                user_id=user_id,
                current_repo=data.get('current_repo'),
                current_file=data.get('current_file'),
                current_goal=data.get('current_goal'),
                recent_files=data.get('recent_files', []),
                recent_goals=data.get('recent_goals', []),
                active_context=data.get('active_context', 'general'),
                preferences=data.get('preferences', {}),
            )
        else:
            ctx = UserContext(user_id=user_id)
        
        self.user_contexts[user_id] = ctx
        return ctx
    
    def analyze_file_context(self, file_path: str, code: str) -> list[ProactiveSuggestion]:
        """Analyze a file and generate proactive suggestions."""
        suggestions = []
        file_lower = file_path.lower()
        code_lower = code.lower()
        
        for trigger_name, trigger_config in TRIGGER_PATTERNS.items():
            # Check if file matches trigger patterns
            matches = any(
                re.search(pattern, file_lower) or re.search(pattern, code_lower)
                for pattern in trigger_config["patterns"]
            )
            
            if matches:
                # Query knowledge graph for relevant patterns
                knowledge_entries = []
                for cat in trigger_config["knowledge_categories"]:
                    entries = query_knowledge(
                        problem_description=f"Working on {trigger_name} in {file_path}",
                        category=cat,
                        limit=3,
                    )
                    knowledge_entries.extend(entries)
                
                for i, suggestion_text in enumerate(trigger_config["suggestions"]):
                    suggestions.append(ProactiveSuggestion(
                        trigger=trigger_name,
                        title=f"💡 {suggestion_text}",
                        description=f"Based on {trigger_name} patterns in {file_path}",
                        action_type="suggest",
                        priority=2 if i < 2 else 3,
                        context_data={"file": file_path, "trigger": trigger_name},
                        knowledge_entries=knowledge_entries[:2],
                    ))
        
        return suggestions
    
    def analyze_goal_context(self, goal: str, repo: str | None = None) -> list[ProactiveSuggestion]:
        """Analyze a user's stated goal and generate proactive suggestions."""
        suggestions = []
        goal_lower = goal.lower()
        
        # Detect intent patterns
        if any(kw in goal_lower for kw in ["refactor", "clean up", "improve", "optimize"]):
            suggestions.append(ProactiveSuggestion(
                trigger="refactor_intent",
                title="🔧 Refactoring detected",
                description="Consider: extract methods, reduce complexity, add tests first",
                action_type="suggest",
                priority=1,
                context_data={"goal": goal, "type": "refactor"},
            ))
        
        if any(kw in goal_lower for kw in ["debug", "fix", "error", "bug", "crash", "fail"]):
            suggestions.append(ProactiveSuggestion(
                trigger="debug_intent",
                title="🐛 Debugging detected",
                description="I'll pull related error patterns from knowledge graph and check sandbox baseline",
                action_type="preload",
                priority=1,
                context_data={"goal": goal, "type": "debug"},
            ))
        
        if any(kw in goal_lower for kw in ["security", "secure", "vulnerability", "audit"]):
            suggestions.append(ProactiveSuggestion(
                trigger="security_intent",
                title="🔒 Security focus detected",
                description="I'll check for common vulnerabilities: injection, auth bypass, data exposure",
                action_type="preload",
                priority=1,
                context_data={"goal": goal, "type": "security"},
            ))
        
        if repo:
            # Query cross-repo knowledge for this goal
            entries = query_knowledge(
                problem_description=goal,
                repo_name=repo,
                limit=3,
            )
            if entries:
                suggestions.append(ProactiveSuggestion(
                    trigger="cross_repo_knowledge",
                    title="🧠 Cross-repo knowledge available",
                    description=f"Found {len(entries)} relevant patterns from past repairs",
                    action_type="preload",
                    priority=2,
                    context_data={"goal": goal, "repo": repo},
                    knowledge_entries=entries,
                ))
        
        return suggestions
    
    def generate_proactive_message(self, user_id: int, file_path: str | None = None, 
                                    code: str | None = None, goal: str | None = None) -> str | None:
        """Generate a proactive assistance message for the user."""
        ctx = self.get_context(user_id)
        all_suggestions = []
        
        if file_path and code:
            all_suggestions.extend(self.analyze_file_context(file_path, code))
            ctx.current_file = file_path
            if file_path not in ctx.recent_files:
                ctx.recent_files.insert(0, file_path)
        
        if goal:
            all_suggestions.extend(self.analyze_goal_context(goal, ctx.current_repo))
            ctx.current_goal = goal
            if goal not in ctx.recent_goals:
                ctx.recent_goals.insert(0, goal)
        
        if not all_suggestions:
            return None
        
        # Sort by priority and deduplicate
        seen = set()
        unique = []
        for s in sorted(all_suggestions, key=lambda x: x.priority):
            key = (s.trigger, s.title)
            if key not in seen:
                seen.add(key)
                unique.append(s)
        
        # Take top 3
        top_suggestions = unique[:3]
        
        # Build message
        lines = ["🤖 **Proactive Assistance** — I noticed you're working on this:"]
        
        for s in top_suggestions:
            lines.append(f"\n**{s.title}**")
            lines.append(f"   {s.description}")
            if s.knowledge_entries:
                lines.append(f"   📚 Related knowledge: {len(s.knowledge_entries)} patterns from past repairs")
        
        lines.append("\n*Say 'max apply <number>' to act on a suggestion, or 'max ignore' to dismiss.*")
        
        self._persist_context(ctx)
        return "\n".join(lines)
    
    def record_suggestion_feedback(self, user_id: int, trigger: str, suggestion: str, 
                                    action_type: str, accepted: bool) -> None:
        """Record user feedback on suggestions for learning."""
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(PROACTIVE_DB)) as db, db:
            db.execute("""
                INSERT INTO suggestion_history (user_id, trigger, suggestion, action_type, accepted, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, trigger, suggestion, action_type, accepted, datetime.now(timezone.utc).isoformat()))
    
    def get_personalized_insights(self, user_id: int) -> str:
        """Generate personalized insights based on user history."""
        ctx = self.get_context(user_id)
        
        # Get user's notes for context
        notes = list_notes(user_id)
        
        # Get knowledge stats
        stats = get_knowledge_stats()
        
        lines = ["📊 **Your Personalized Insights**"]
        
        if ctx.current_repo:
            lines.append(f"\n**Current Repository:** {ctx.current_repo}")
        
        if ctx.recent_files:
            lines.append(f"\n**Recent Files:** {', '.join(ctx.recent_files[:5])}")
        
        if ctx.recent_goals:
            lines.append(f"\n**Recent Goals:** {', '.join(ctx.recent_goals[:3])}")
        
        if notes:
            lines.append(f"\n**Your Notes ({len(notes)}):** {notes[-1][:100]}")
        
        if stats['total_entries'] > 0:
            lines.append(f"\n**Knowledge Graph:** {stats['total_entries']} patterns learned across {len(stats['top_repositories'])} repositories")
            if stats['top_repositories']:
                top_repo = list(stats['top_repositories'].keys())[0]
                lines.append(f"   Most active: {top_repo} ({stats['top_repositories'][top_repo]} patterns)")
        
        # Suggest next actions based on patterns
        if ctx.recent_goals:
            recent = ctx.recent_goals[0].lower()
            if "test" in recent:
                lines.append("\n💡 *Since you've been working on tests, want me to check coverage or add edge cases?*")
            elif "debug" in recent or "fix" in recent:
                lines.append("\n💡 *You've been debugging — want me to set up a watch on related files?*")
        
        return "\n".join(lines)


# Global instance
proactive_engine = ProactiveEngine()


def get_proactive_engine() -> ProactiveEngine:
    return proactive_engine


async def generate_proactive_response(user_id: int, file_path: str | None = None,
                                       code: str | None = None, goal: str | None = None) -> str | None:
    """Async wrapper for generating proactive responses."""
    return proactive_engine.generate_proactive_message(user_id, file_path, code, goal)