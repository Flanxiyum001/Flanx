"""Read-only GitHub visibility report through configured Composio actions."""

from __future__ import annotations

import re
from typing import Any

from composio_adapter import ComposioAdapter, IntegrationError, _user_id, _validate
from external_actions import ExternalActionService


IDENTITY_ACTION = "GITHUB_WHO_AM_I"
REPOSITORY_ACTIONS = (
    "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER",
    "GITHUB_LIST_ACCESSIBLE_REPOSITORIES",
    "GITHUB_LIST_REPOSITORIES_ACCESSIBLE_TO_THE_APP_INSTALLATION",
)
_REPO_NAME = re.compile(r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z")
_LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}\Z")
_PERMISSIONS = ("admin", "maintain", "push", "triage", "pull")


def _payload(value: Any) -> Any:
    """Unwrap only documented result envelopes; never synthesize missing data."""
    if isinstance(value, dict) and value.get("successful") is False:
        raise IntegrationError("service_error")
    for _ in range(3):
        if not isinstance(value, dict):
            break
        if "data" in value and isinstance(value["data"], (dict, list)):
            value = value["data"]
        elif "result" in value and isinstance(value["result"], (dict, list)):
            value = value["result"]
        else:
            break
    return value


def _identity(value: Any) -> str:
    data = _payload(value)
    if isinstance(data, dict) and isinstance(data.get("user"), dict):
        data = data["user"]
    login = data.get("login") if isinstance(data, dict) else None
    if not isinstance(login, str) or not _LOGIN.fullmatch(login):
        raise IntegrationError("invalid_response")
    return login


def _repositories(value: Any) -> list[dict]:
    data = _payload(value)
    if isinstance(data, dict):
        data = next((data[key] for key in ("repositories", "items", "repos")
                     if isinstance(data.get(key), list)), None)
    if not isinstance(data, list):
        raise IntegrationError("invalid_response")
    result = []
    for item in data:
        if not isinstance(item, dict):
            raise IntegrationError("invalid_response")
        name = item.get("full_name")
        if not isinstance(name, str) or not _REPO_NAME.fullmatch(name):
            raise IntegrationError("invalid_response")
        repo = {"name": name}
        if type(item.get("private")) is bool:
            repo["private"] = item["private"]
        permissions = item.get("permissions")
        if isinstance(permissions, dict):
            known = {key: permissions[key] for key in _PERMISSIONS
                     if type(permissions.get(key)) is bool}
            if known:
                repo["permissions"] = known
        result.append(repo)
    return result


async def github_visibility(adapter: ComposioAdapter, service: ExternalActionService,
                            requester_id: int | str) -> dict:
    """List actual API-visible repositories for the requester's active connection."""
    if adapter.policy.emergency_off or "github" in adapter.policy.disabled_integrations:
        raise IntegrationError("disabled")
    if "github" not in adapter.policy.connections:
        raise IntegrationError("auth_config_missing")
    user_id = _user_id(requester_id)
    accounts = [item for item in await adapter.list_connections(requester_id)
                if item["toolkit"] == "github" and item["status"] == "ACTIVE"]
    if not accounts:
        raise IntegrationError("github_connection_missing")
    if len(accounts) != 1:
        raise IntegrationError("github_connection_ambiguous")
    account = accounts[0]
    account_id = account["id"]
    actions = adapter.policy.actions
    identity = actions.get((account_id, IDENTITY_ACTION))
    repository = next((actions[(account_id, slug)] for slug in REPOSITORY_ACTIONS
                       if (account_id, slug) in actions), None)
    if identity is None or repository is None:
        raise IntegrationError("github_read_action_missing")
    for action in (identity, repository):
        if action.user_id != user_id or action.toolkit != "github" or action.risk != "read":
            raise IntegrationError("permission_denied")
        adapter.policy.decide(account_id, action.slug)

    async def read(action, arguments):
        outcome = await service.start(account_id, action.slug, arguments,
                                      requester_id=str(requester_id))
        if outcome.status != "completed":
            raise IntegrationError("service_error" if outcome.error_code in (None, "handler_error")
                                   else outcome.error_code)
        if not isinstance(outcome.result, dict) or "untrusted_external_data" not in outcome.result:
            raise IntegrationError("invalid_response")
        return outcome.result["untrusted_external_data"]

    identity_schema = (await adapter.discover(account_id, identity.slug))["schema"]
    if not _validate({}, identity_schema):
        raise IntegrationError("invalid_arguments")
    login = _identity(await read(identity, {}))
    repo_schema = (await adapter.discover(account_id, repository.slug))["schema"]
    props = repo_schema.get("properties", {})
    page_field = props.get("page") if isinstance(props, dict) else None
    size_field = props.get("per_page") if isinstance(props, dict) else None
    paged = isinstance(page_field, dict) and page_field.get("type") == "integer"
    size = isinstance(size_field, dict) and size_field.get("type") == "integer"
    maximum = size_field.get("maximum") if size else None
    page_size = min(100, maximum) if type(maximum) is int and maximum > 0 else (100 if size else 30)
    limit = min(repository.max_calls, 5) if paged else 1
    seen: dict[str, dict] = {}
    truncated = False
    for page in range(1, limit + 1):
        arguments = {"page": page} if paged else {}
        if size:
            arguments["per_page"] = page_size
        if not _validate(arguments, repo_schema):
            raise IntegrationError("invalid_arguments")
        batch = _repositories(await read(repository, arguments))
        for item in batch:
            seen[item["name"]] = item
        if not paged:
            truncated = len(batch) >= page_size
            break
        if len(batch) < page_size:
            break
        if page == limit:
            truncated = True
    visible = sorted(seen.values(), key=lambda item: item["name"].lower())
    authorized = sorted(name for name in seen
                        if name in adapter.policy.github_repository_allowlist)
    return {"account_id": account_id, "login": login,
            "visible": visible, "authorized": authorized, "truncated": truncated}
