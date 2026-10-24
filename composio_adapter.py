"""Optional, fail-closed Composio direct-tools adapter.

Only configured account/tool pairs can be exposed. No model receives the SDK
client, API key, or a general-purpose execution handle.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


class IntegrationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _data(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    return vars(value) if hasattr(value, "__dict__") else {}


def _flag(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes"}


_READ_VERBS = {"GET", "LIST", "SEARCH", "FETCH", "READ", "RETRIEVE"}
_MUTATION_VERBS = {"DELETE", "REMOVE", "SEND", "POST", "PUBLISH", "CREATE", "UPDATE",
                   "EDIT", "PATCH", "PUT", "MERGE", "DEPLOY", "PAY", "PURCHASE",
                   "REVOKE", "GRANT", "TRANSFER", "INVITE", "ADD"}
_TOOLKIT = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{3,120}\Z")
_GITHUB_ACCOUNT_READS = frozenset({
    "GITHUB_WHO_AM_I", "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER",
    "GITHUB_LIST_ACCESSIBLE_REPOSITORIES",
    "GITHUB_LIST_REPOSITORIES_ACCESSIBLE_TO_THE_APP_INSTALLATION",
})


def _user_id(requester_id: int | str) -> str:
    """A Discord ID is the only source of Composio connection ownership."""
    value = str(requester_id)
    if not value.isdecimal() or int(value) <= 0:
        raise IntegrationError("invalid_requester")
    return f"max-discord-{int(value)}"


def _toolkit_name(value: str) -> str:
    name = (value or "").strip().lower().replace(" ", "")
    if not _TOOLKIT.fullmatch(name):
        raise IntegrationError("invalid_toolkit")
    return name


def _safe_scopes(value: Any) -> list[str]:
    """Expose only scope identifiers, never provider credential fields."""
    candidate = _data(value)
    for field in ("scopes", "requested_scopes"):
        scopes = candidate.get(field)
        if isinstance(scopes, list):
            return [scope for scope in scopes[:30] if isinstance(scope, str)
                    and len(scope) <= 180 and re.fullmatch(r"[A-Za-z0-9:/. _-]+", scope)]
    for field in ("auth_config", "auth_config_details"):
        if candidate.get(field) is not None:
            result = _safe_scopes(candidate[field])
            if result:
                return result
    return []


def _github_connection_only_policy(auth_config_id: str) -> dict:
    """Explicit Railway env fallback: allow a GitHub Connect Link, zero tool calls.

    No Composio SDK or user-supplied action policy is inferred from the ID.
    """
    if (not isinstance(auth_config_id, str)
            or not _IDENTIFIER.fullmatch(auth_config_id)
            or auth_config_id.startswith("replace-with-")):
        raise ValueError("Invalid GitHub auth-config ID for connection-only mode")
    return {
        "autonomy": "observe",
        "connections": {"github": auth_config_id},
        "github_repository_allowlist": [],
        "actions": [],
    }


@dataclass(frozen=True)
class Action:
    slug: str
    toolkit: str
    account_id: str
    user_id: str
    version: str
    risk: str
    category: str
    max_calls: int


class CapabilityPolicy:
    """Explicit account-scoped allowlist; config is trusted operator input only."""

    def __init__(self, document: Mapping[str, Any], *, emergency_off: bool = False):
        if not isinstance(document, dict) or document.get("autonomy") not in {
            "observe", "assist", "execute_approved", "trusted_autonomy"
        }:
            raise ValueError("Invalid autonomy policy")
        self.autonomy = document["autonomy"]
        self.emergency_off = emergency_off
        self.disabled_integrations = set(document.get("disabled_integrations", []))
        self.disabled_actions = set(document.get("disabled_actions", []))
        configured = document.get("connections", {})
        if not isinstance(configured, dict) or any(
            not isinstance(name, str) or not _TOOLKIT.fullmatch(name)
            or not isinstance(auth_id, str) or not _IDENTIFIER.fullmatch(auth_id)
            for name, auth_id in configured.items()
        ):
            raise ValueError("Invalid connection policy")
        self.connections = dict(configured)
        repositories = document.get("github_repository_allowlist", [])
        if (not isinstance(repositories, list) or any(
            not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}", name)
            for name in repositories
        )):
            raise ValueError("Invalid GitHub repository allowlist")
        self.github_repository_allowlist = frozenset(repositories)
        self.actions: dict[tuple[str, str], Action] = {}
        for entry in document.get("actions", []):
            if not isinstance(entry, dict) or set(entry) != {
                "slug", "toolkit", "account_id", "user_id", "version", "risk", "category", "max_calls"
            }:
                raise ValueError("Invalid action policy entry")
            if (not all(isinstance(entry[key], str) and entry[key] and len(entry[key]) <= 120
                        for key in ("slug", "toolkit", "account_id", "user_id", "version", "risk", "category"))
                    or not re.fullmatch(r"[A-Z][A-Z0-9_]+", entry["slug"])
                    or not re.fullmatch(r"[a-z][a-z0-9_]+", entry["toolkit"])
                    or not entry["slug"].startswith(entry["toolkit"].upper() + "_")
                    or entry["risk"] not in {"read", "write", "high"}
                    or type(entry["max_calls"]) is not int or not 1 <= entry["max_calls"] <= 100):
                raise ValueError("Invalid action policy entry")
            action = Action(**entry)
            # Phase 7 GitHub integration is read-only regardless of autonomy mode.
            if action.toolkit == "github" and action.risk != "read":
                raise ValueError("GitHub write actions are not enabled")
            words = set(action.slug.split("_"))
            if action.risk == "read" and (not (words.intersection(_READ_VERBS)
                                               or action.slug == "GITHUB_WHO_AM_I")
                                          or words.intersection(_MUTATION_VERBS)):
                raise ValueError("Read classification conflicts with action name")
            key = (action.account_id, action.slug)
            if key in self.actions:
                raise ValueError("Duplicate action policy")
            self.actions[key] = action

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CapabilityPolicy:
        env = os.environ if env is None else env
        path = (env.get("MAX_COMPOSIO_POLICY_FILE") or "").strip()
        if not _flag(env.get("MAX_COMPOSIO_ENABLED")):
            return cls({"autonomy": "observe", "actions": []}, emergency_off=True)
        auth_id = (env.get("MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID") or "").strip()
        if not path:
            if not auth_id:
                return cls({"autonomy": "observe", "actions": []}, emergency_off=True)
            document = _github_connection_only_policy(auth_id)
        else:
            try:
                document = json.loads(Path(path).read_text(encoding="utf-8"))
            except FileNotFoundError:
                # Deliberately configured env ID is the ONLY fileless fallback.
                # Malformed/unreadable existing policy files never fall back.
                if not auth_id:
                    raise
                document = _github_connection_only_policy(auth_id)
        return cls(document, emergency_off=_flag(env.get("MAX_EXTERNAL_ACTIONS_DISABLED")))

    def decide(self, account_id: str, slug: str) -> tuple[Action, str]:
        action = self.actions.get((account_id, slug))
        if (self.emergency_off or action is None or action.toolkit in self.disabled_integrations
                or slug in self.disabled_actions):
            raise IntegrationError("permission_denied")
        if self.autonomy == "observe" and action.risk != "read":
            raise IntegrationError("permission_denied")
        if self.autonomy == "assist" and action.risk != "read":
            raise IntegrationError("permission_denied")
        if action.risk == "read":
            return action, "composio.read"
        return action, "composio.write"

    def check_repository_scope(self, action: Action, arguments: dict) -> None:
        """GitHub actions outside the account inventory need an exact repo grant."""
        if action.toolkit != "github" or action.slug in _GITHUB_ACCOUNT_READS:
            return
        if not isinstance(arguments, dict):
            raise IntegrationError("permission_denied")
        if isinstance(arguments.get("owner"), str) and isinstance(arguments.get("repo"), str):
            name = f"{arguments['owner']}/{arguments['repo']}"
        else:
            name = arguments.get("full_name") or arguments.get("repository") or arguments.get("repo")
        if not isinstance(name, str) or name not in self.github_repository_allowlist:
            raise IntegrationError("permission_denied")


def _validate(value: Any, schema: dict, depth: int = 0) -> bool:
    """Conservative JSON Schema subset. Unknown validation features fail closed."""
    if depth > 8 or not isinstance(schema, dict):
        return False
    allowed = {"type", "title", "description", "default", "examples", "enum",
               "properties", "required", "additionalProperties", "items",
               "minLength", "maxLength", "pattern", "minItems", "maxItems",
               "minimum", "maximum"}
    if set(schema) - allowed:
        return False
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict) or len(value) > 64:
            return False
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if (not isinstance(props, dict) or not isinstance(required, list)
                or schema.get("additionalProperties", False) not in (False, None)):
            return False
        if not set(required).issubset(value):
            return False
        return all(key in props and _validate(item, props[key], depth + 1)
                   for key, item in value.items())
    if kind == "array":
        return (isinstance(value, list) and schema.get("minItems", 0) <= len(value)
                and len(value) <= min(schema.get("maxItems", 100), 100)
                and all(_validate(item, schema.get("items", {}), depth + 1) for item in value))
    expected = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}
    if kind not in expected or not isinstance(value, expected[kind]) or (kind in {"integer", "number"} and isinstance(value, bool)):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > min(schema.get("maxLength", 10_000), 10_000):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
    if kind in {"integer", "number"} and (value < schema.get("minimum", value)
                                           or value > schema.get("maximum", value)):
        return False
    return True


def composio_setup_issues(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return safe setup blockers without revealing secrets, IDs, or policy content.

    This checks local configuration only, not live Composio authentication.
    Never use the result to authorize actions or bypass CapabilityPolicy.
    """
    env = os.environ if environ is None else environ
    issues: list[str] = []
    if not _flag(env.get("MAX_COMPOSIO_ENABLED")):
        issues.append("MAX_COMPOSIO_ENABLED is off; enable it only after the private policy is ready.")
    if not str(env.get("COMPOSIO_API_KEY") or "").strip():
        issues.append("COMPOSIO_API_KEY is missing; set it privately in the service environment.")
    path = (env.get("MAX_COMPOSIO_POLICY_FILE") or "").strip()
    auth_id = (env.get("MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID") or "").strip()

    def check_connection_only() -> None:
        if not auth_id:
            issues.append(
                "No private Composio policy file is available. For GitHub "
                "connection-only mode, configure MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID "
                "with the real dashboard auth-config ID."
            )
            return
        try:
            _github_connection_only_policy(auth_id)
        except ValueError:
            issues.append(
                "MAX_COMPOSIO_GITHUB_AUTH_CONFIG_ID is invalid or a placeholder; "
                "use the actual GitHub auth-config ID from the Composio dashboard."
            )

    if not path:
        check_connection_only()
    else:
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            check_connection_only()
        except (OSError, UnicodeError, json.JSONDecodeError):
            issues.append("The configured private policy file is unreadable or invalid JSON.")
        else:
            try:
                policy = CapabilityPolicy(document)
            except (ValueError, TypeError):
                issues.append("The private Composio policy failed schema validation.")
            else:
                configured_id = policy.connections.get("github")
                if not configured_id:
                    issues.append("The private Composio policy lacks a GitHub auth config in connections.")
                elif configured_id.startswith("replace-with-"):
                    issues.append("The GitHub auth-config ID is still a placeholder; use the real dashboard ID.")
    if _flag(env.get("MAX_EXTERNAL_ACTIONS_DISABLED")):
        issues.append("MAX_EXTERNAL_ACTIONS_DISABLED is on; this emergency switch also disables connections.")
    return tuple(issues)


class ComposioAdapter:
    def __init__(self, policy: CapabilityPolicy, *, client: Any = None,
                 api_key: str = "", timeout: float = 20):
        self.policy = policy
        self._client = client
        self._api_key = api_key
        self.timeout = max(1, min(float(timeout), 60))
        self._calls: dict[tuple[str, str], int] = {}
        self._slots = asyncio.Semaphore(2)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ComposioAdapter:
        env = os.environ if env is None else env
        policy = CapabilityPolicy.from_env(env)
        if not policy.emergency_off and not env.get("COMPOSIO_API_KEY"):
            raise IntegrationError("configuration")
        return cls(policy, api_key=env.get("COMPOSIO_API_KEY", ""))

    def _sdk(self):
        if self.policy.emergency_off:
            raise IntegrationError("disabled")
        if self._client is None:
            if not self._api_key:
                raise IntegrationError("configuration")
            try:
                from composio import Composio
            except ImportError:
                raise IntegrationError("sdk_missing") from None
            self._client = Composio(api_key=self._api_key)
        return self._client

    async def _call(self, method, *args, **kwargs):
        try:
            return await asyncio.wait_for(asyncio.to_thread(method, *args, **kwargs), self.timeout)
        except asyncio.TimeoutError:
            raise IntegrationError("timeout") from None
        except Exception as exc:
            raise self._classify(exc) from None

    def _connection_policy(self, toolkit: str) -> tuple[str, str]:
        name = _toolkit_name(toolkit)
        if name in self.policy.disabled_integrations:
            raise IntegrationError("toolkit_disabled")
        auth_id = self.policy.connections.get(name)
        if not auth_id:
            raise IntegrationError("auth_config_missing")
        return name, auth_id

    def _account_summary(self, account: Any, user_id: str) -> dict:
        entry = _data(account)
        toolkit = _data(entry.get("toolkit")).get("slug")
        account_id = entry.get("id")
        if (entry.get("user_id") != user_id or not isinstance(toolkit, str)
                or not _TOOLKIT.fullmatch(toolkit) or not isinstance(account_id, str)
                or not _IDENTIFIER.fullmatch(account_id)):
            raise IntegrationError("ownership_mismatch")
        categories = set()
        for action in self.policy.actions.values():
            if action.account_id == account_id and action.toolkit == toolkit:
                try:
                    self.policy.decide(account_id, action.slug)
                except IntegrationError:
                    continue
                categories.add(action.category)
        return {"id": account_id, "toolkit": toolkit,
                "status": str(entry.get("status") or "UNKNOWN").upper(),
                "scopes": _safe_scopes(entry), "action_categories": sorted(categories),
                "actions_enabled": bool(categories)}

    async def list_connections(self, requester_id: int | str) -> list[dict]:
        user_id = _user_id(requester_id)
        accounts = await self._call(self._sdk().connected_accounts.list, user_ids=[user_id])
        items = _data(accounts).get("items", [])
        if not isinstance(items, list):
            raise IntegrationError("invalid_response")
        return [self._account_summary(item, user_id) for item in items[:50]]

    async def initiate_connection(self, toolkit: str, requester_id: int | str) -> dict:
        """Return a private Connect Link; callers must deliver it only by DM."""
        name, auth_id = self._connection_policy(toolkit)
        user_id = _user_id(requester_id)
        client = self._sdk()
        configured = _data(await self._call(client.auth_configs.get, auth_id))
        actual_toolkit = _data(configured.get("toolkit")).get("slug")
        if actual_toolkit != name:
            raise IntegrationError("auth_config_mismatch")
        for account in await self.list_connections(requester_id):
            if account["toolkit"] == name and account["status"] == "ACTIVE":
                raise IntegrationError("already_connected")
            if account["toolkit"] == name and account["status"] in {"INITIALIZING", "INITIATED"}:
                raise IntegrationError("connection_pending")
        request = _data(await self._call(client.connected_accounts.link, user_id, auth_id))
        link = request.get("redirect_url")
        account_id = request.get("id")
        try:
            parsed = urlsplit(link)
            valid_link = parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
        except (TypeError, ValueError):
            valid_link = False
        if not valid_link or not isinstance(account_id, str) or not _IDENTIFIER.fullmatch(account_id):
            raise IntegrationError("invalid_response")
        return {"id": account_id, "toolkit": name, "url": link,
                "scopes": _safe_scopes(configured), "status": "INITIATED"}

    async def connection_status(self, account_id: str, requester_id: int | str) -> dict:
        user_id = _user_id(requester_id)
        if not isinstance(account_id, str) or not _IDENTIFIER.fullmatch(account_id):
            raise IntegrationError("invalid_account")
        account = await self._call(self._sdk().connected_accounts.get, account_id)
        summary = self._account_summary(account, user_id)
        auth_id = self.policy.connections.get(summary["toolkit"])
        if not auth_id:
            raise IntegrationError("toolkit_unconfigured")
        configured = _data(await self._call(self._sdk().auth_configs.get, auth_id))
        if _data(configured.get("toolkit")).get("slug") != summary["toolkit"]:
            raise IntegrationError("auth_config_mismatch")
        summary["scopes"] = _safe_scopes(configured) or summary["scopes"]
        return summary

    async def disconnect_connection(self, account_id: str, requester_id: int | str) -> dict:
        summary = await self.connection_status(account_id, requester_id)
        await self._call(self._sdk().connected_accounts.delete, account_id)
        return {**summary, "status": "DELETED"}

    async def discover(self, account_id: str, slug: str) -> dict:
        action, tool = self.policy.decide(account_id, slug)
        client = self._sdk()
        try:
            accounts = await asyncio.wait_for(asyncio.to_thread(
                client.connected_accounts.list, user_ids=[action.user_id], statuses=["ACTIVE"]), self.timeout)
            entries = _data(accounts).get("items", [])
            if not any(_data(item).get("id") == account_id
                       and _data(item).get("user_id") == action.user_id
                       and str(_data(item).get("status", "")).upper() == "ACTIVE"
                       for item in entries):
                raise IntegrationError("connection_expired")
            raw = await asyncio.wait_for(asyncio.to_thread(
                client.tools.get_raw_composio_tool_by_slug, slug), self.timeout)
            data = _data(raw)
            schema = data.get("input_parameters") or data.get("parameters")
            version = data.get("version") or data.get("toolkit_version")
            if (data.get("slug") != slug or not isinstance(schema, dict)
                    or schema.get("type") != "object" or version != action.version):
                raise IntegrationError("schema_mismatch")
            return {"action": action, "tool": tool, "schema": schema,
                    "description": str(data.get("description") or "")[:300]}
        except IntegrationError:
            raise
        except asyncio.TimeoutError:
            raise IntegrationError("timeout") from None
        except Exception as exc:
            raise self._classify(exc) from None

    @staticmethod
    def _classify(exc: Exception) -> IntegrationError:
        if "MultipleConnectedAccounts" in type(exc).__name__:
            return IntegrationError("already_connected")
        if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
            return IntegrationError("timeout")
        status = getattr(exc, "status_code", getattr(exc, "status", None))
        if status == 429:
            return IntegrationError("rate_limited")
        if status == 401:
            return IntegrationError("authentication")
        if status == 403:
            return IntegrationError("permission_denied")
        if status == 404:
            return IntegrationError("not_found")
        if status == 409:
            return IntegrationError("conflict")
        return IntegrationError("service_error")

    async def execute(self, account_id: str, slug: str, arguments: dict) -> tuple[dict, Any]:
        metadata = await self.discover(account_id, slug)
        action = metadata["action"]
        self.policy.check_repository_scope(action, arguments)
        if (not isinstance(arguments, dict) or len(json.dumps(arguments, allow_nan=False)) > 32_000
                or not _validate(arguments, metadata["schema"])):
            raise IntegrationError("invalid_arguments")
        key = (account_id, slug)
        async with self._slots:
            # Recheck the kill switch immediately before dispatch. A write is
            # counted before calling the SDK and is never retried here.
            self.policy.decide(account_id, slug)
            if self._calls.get(key, 0) >= action.max_calls:
                raise IntegrationError("limit_reached")
            self._calls[key] = self._calls.get(key, 0) + 1
            try:
                result = await asyncio.wait_for(asyncio.to_thread(
                    self._sdk().tools.execute, slug, arguments=arguments,
                    connected_account_id=account_id, user_id=action.user_id,
                    version=action.version), self.timeout)
            except asyncio.TimeoutError:
                raise IntegrationError("outcome_uncertain" if action.risk != "read" else "timeout") from None
            except Exception as exc:
                error = self._classify(exc)
                if action.risk != "read" and error.code == "service_error":
                    raise IntegrationError("outcome_uncertain") from None
                raise error from None
            data = _data(result)
            if data.get("successful") is False or data.get("error"):
                raise IntegrationError("service_error")
            return metadata, result


class MockComposioClient:
    """Small deterministic SDK surface for offline tests."""
    def __init__(self, schema: dict, result: Any = None):
        self.schema = schema
        self.result = {"successful": True, "data": {"ok": True}} if result is None else result
        self.calls: list[tuple] = []
        self.connected_accounts = self
        self.tools = self

    def list(self, *, user_ids, statuses):
        return {"items": [{"id": "ca_test", "user_id": user_ids[0], "status": "ACTIVE"}]}

    def get_raw_composio_tool_by_slug(self, slug):
        return {"slug": slug, "version": "v1", "input_parameters": self.schema}

    def execute(self, slug, *, arguments, connected_account_id, user_id, version):
        self.calls.append((slug, arguments, connected_account_id, user_id, version))
        return self.result
