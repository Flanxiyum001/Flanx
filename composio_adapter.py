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
            words = set(action.slug.split("_"))
            if action.risk == "read" and (not words.intersection(_READ_VERBS)
                                          or words.intersection(_MUTATION_VERBS)):
                raise ValueError("Read classification conflicts with action name")
            key = (action.account_id, action.slug)
            if key in self.actions:
                raise ValueError("Duplicate action policy")
            self.actions[key] = action

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CapabilityPolicy:
        env = os.environ if env is None else env
        path = env.get("MAX_COMPOSIO_POLICY_FILE", "")
        if not _flag(env.get("MAX_COMPOSIO_ENABLED")) or not path:
            return cls({"autonomy": "observe", "actions": []}, emergency_off=True)
        document = json.loads(Path(path).read_text(encoding="utf-8"))
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

    async def discover(self, account_id: str, slug: str) -> dict:
        action, tool = self.policy.decide(account_id, slug)
        client = self._sdk()
        try:
            accounts = await asyncio.wait_for(asyncio.to_thread(
                client.connected_accounts.list, user_ids=[action.user_id], statuses=["ACTIVE"]), self.timeout)
            entries = _data(accounts).get("items", [])
            if not any(_data(item).get("id") == account_id and
                       str(_data(item).get("status", "")).upper() == "ACTIVE" for item in entries):
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
        status = getattr(exc, "status_code", getattr(exc, "status", None))
        if status == 429:
            return IntegrationError("rate_limited")
        if status in (401, 403):
            return IntegrationError("connection_expired")
        return IntegrationError("service_error")

    async def execute(self, account_id: str, slug: str, arguments: dict) -> tuple[dict, Any]:
        metadata = await self.discover(account_id, slug)
        action = metadata["action"]
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
        return {"items": [{"id": "ca_test", "status": "ACTIVE"}]}

    def get_raw_composio_tool_by_slug(self, slug):
        return {"slug": slug, "version": "v1", "input_parameters": self.schema}

    def execute(self, slug, *, arguments, connected_account_id, user_id, version):
        self.calls.append((slug, arguments, connected_account_id, user_id, version))
        return self.result
