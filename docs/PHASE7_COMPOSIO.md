# Phase 7: Composio and scoped memory

## What is available

Max can execute a **configured** Composio direct tool on one configured active
connected account at a time. The adapter uses the current Python `composio`
package, `connected_accounts.list`, `tools.get_raw_composio_tool_by_slug`, and
`tools.execute`. It checks the live action slug, schema and pinned version
before every call. No application or action is assumed available merely from
its name. The checked-in example names `GITHUB_GET_REPOS`; it is an example,
not an enabled integration. Gmail, Calendar, Slack, and other applications
require their own connected account and exact action entries.

The [current SDK reference](https://docs.composio.dev/reference/sdk-reference/python)
documents `composio`, while `composio-core` is a legacy package. The
[direct-tool methods](https://docs.composio.dev/reference/sdk-reference/python/tools)
and [connected-account flow](https://docs.composio.dev/docs/tools-direct/authenticating-tools)
were checked before implementation. The SDK repository is
[MIT licensed](https://github.com/ComposioHQ/composio).
Composio's [pricing](https://composio.dev/pricing) currently lists a free Hobby
tier with usage limits and paid tiers; check the current terms before adding
accounts or increasing volume. No paid plan is assumed by Max.

## Setup for a dedicated test account

1. Create a separate Composio project and connect a dedicated test account
   using a dashboard auth config or Composio Connect Link. Grant minimal scopes.
   Hosted links are created with `connected_accounts.link(user_id, auth_config_id)`.
2. Install the optional SDK with `python -m pip install -r requirements-composio.txt`.
   It is not needed when Composio is disabled.
3. Copy `docs/composio-policy.example.json` to a private file under `.max_state`.
   Replace account ID, Composio user ID, action slug, and exact tool version.
   Inspect the action via `Composio(api_key=...).tools.get_raw_composio_tool_by_slug(slug)`
   and use the returned input schema and version. Unsupported schema features
   fail closed; do not guess required parameters or version.
4. Put `COMPOSIO_API_KEY` in the private process environment. Set
   `MAX_COMPOSIO_POLICY_FILE` to the policy path,
   `MAX_COMPOSIO_ENABLED=true`, and `MAX_EXTERNAL_ACTIONS_DISABLED=false`.
   Set `MAX_APPROVER_DISCORD_IDS` to the exact Discord IDs allowed to operate
   the commands. Do not store keys in the policy file or Discord messages.
5. In a DM to the test bot, run `!max_capabilities`, then run a configured
   read action with `!max_tool <account_id> <SLUG> {"required_arg":"value"}`.
   Result content is marked untrusted and delivered by DM with basic redaction.
6. For a write test, use a harmless action on the test account. The bot creates
   a task and approval buttons. Confirm the original command arguments in the
   DM before approving. Inspect the external account afterward. Never repeat
   an uncertain write; use `!max_tool_status <task_id>` and read-only service
   inspection to reconcile it.

## Policy and limits

Policy entries specify the exact `slug`, `toolkit`, `account_id`, `user_id`,
`version`, `risk`, `category`, and a maximum number of calls per process.
`disabled_integrations` and `disabled_actions` are deny lists. `observe` and
`assist` execute configured reads only. `execute_approved` and
`trusted_autonomy` allow configured writes to be proposed, with an exact
requester-bound Discord approval for each task. The task engine still requires
approval for all writes. No configuration level grants automatic publishing,
deletion, access changes, money movement, or infrastructure changes.

`!max_tool_disable` stops all Composio actions in the current process.
`MAX_EXTERNAL_ACTIONS_DISABLED=true` persists that disable over restarts.
`!max_tool_cancel <task_id>` cancels a task; a running remote call can still
finish on the provider side, so its status becomes uncertain. The action audit
holds task ID, slug, account ID, status and error code, with no arguments or
result payload. The task engine also stores only sanitized metadata. Calls are
bounded by the task deadline, adapter timeout, two concurrent awaiters, and
the per-process call limit. The SDK's blocking worker thread may continue
after an await timeout; inspect the remote state before issuing another write.

The schema validator handles objects, arrays, primitive types, required
fields, string length, pattern, and enums. It rejects schemas with references,
union/intersection features, or unsupported constraints. This deliberately
limits which live actions Max can use until a full JSON Schema validator is
integrated and tested. Classification of risk is an operator policy choice;
read entries must also have a read-style slug and no obvious mutation verb.
External output is never inserted into a model prompt by this adapter.

## Memory and models

`MAX_MEMORY_DB` defaults to `.max_state/memories.sqlite3`. Authorized admins
can use `!max_memories [scope]`, `!max_memory_propose <category> <scope> <text>`,
`!max_memory_verify <id> <evidence>`, `!max_memory_correct <id> <text>`, and
`!max_memory_delete <id>` in DM. Categories are `preference`, `project_fact`,
`procedure`, `technical_finding`, `known_error`, and `task_context`.
Corrections return a record to proposed status. Expired records are omitted
from normal listing. External task completion records minimal task feedback;
it does not create a factual memory. Existing verified repository lessons
remain in their own database.

The existing `ModelRouter` uses loopback Ollama by default. FreeLLM is only
used when its cloud provider and per-data-class privacy options are configured.
Cloud requests now also redact common credential labels, token prefixes, and
private-key blocks before transmission. Redaction is a backup control, not a
guarantee against every secret format. Composio results are not passed to
either model automatically.
Existing `CodingWorkflow` still prepares an isolated temporary diff, runs its
bounded review and compile check, and returns a report. It does not apply its
patch to the running tree. Publication remains within the existing test-repo
approval workflow.

## Persistence and rollback

Back up `.max_state/tasks.sqlite3`, `.max_state/external_actions.sqlite3`, and
`.max_state/memories.sqlite3` before moving storage. The latter two are new,
separate SQLite files; there is no migration of existing lessons. To roll
back, set `MAX_COMPOSIO_ENABLED=false`, stop Max, retain a backup, then remove
only the two new files if their data is no longer needed. On Railway, local
files survive restarts only when placed on a persistent volume. No production
service or account has been configured by this change.

## Current gaps

There is no model-driven multi-step Composio agent, automatic trusted write,
general action discovery UI, provider-side idempotency negotiation, automatic
read-only reconciliation, or continuous self-improvement loop. The repair
workflow still runs compile checks rather than arbitrary repository tests.
These limits prevent claiming full autonomous operation or production readiness.
