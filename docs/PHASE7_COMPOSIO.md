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

1. Create a separate Composio project and a toolkit auth config for a
   dedicated test account. Grant minimal scopes. Max creates hosted links
   through `connected_accounts.link(user_id, auth_config_id)` after setup.
2. Install the optional SDK with `python -m pip install -r requirements-composio.txt`.
   It is not needed when Composio is disabled.
3. Copy `docs/composio-policy.example.json` to a private file under `.max_state`.
   Replace account ID, Composio user ID (`max-discord-<your-ID>` for a new
   conversational connection), action slug, and exact tool version.
   Inspect the action via `Composio(api_key=...).tools.get_raw_composio_tool_by_slug(slug)`
   and use the returned input schema and version. Unsupported schema features
   fail closed; do not guess required parameters or version.
4. Put `COMPOSIO_API_KEY` in the private process environment. Set
   `MAX_COMPOSIO_POLICY_FILE` to the policy path,
   `MAX_COMPOSIO_ENABLED=true`, and `MAX_EXTERNAL_ACTIONS_DISABLED=false`.
   Set `MAX_OWNER_DISCORD_IDS` to the exact Discord IDs allowed to operate
   owner commands. `MAX_APPROVER_DISCORD_IDS` remains the publishing approval
   list and is used for owner commands only when `MAX_OWNER_DISCORD_IDS` is
   absent. Do not store keys in the policy file or Discord messages.
5. In a DM to the test bot, run `!max_capabilities`, then run a configured
   read action with `!max_tool <account_id> <SLUG> {"required_arg":"value"}`.
   Result content is marked untrusted and delivered by DM with basic redaction.
6. For a write test, use a harmless action on the test account. The bot creates
   a task and approval buttons. Confirm the original command arguments in the
   DM before approving. Inspect the external account afterward. Never repeat
   an uncertain write; use `!max_tool_status <task_id>` and read-only service
   inspection to reconcile it.

## GitHub read-only rollout (NVIDIA integration increment)

This rollout **refuses all configured GitHub actions classified as writes**,
including `trusted_autonomy`. It does not authorize publishing, PR merges,
repository modification, or deployment. A connected GitHub account alone does
not grant any action permissions.

1. In the [Composio dashboard](https://app.composio.dev/), choose the intended
   Composio project and inspect **GitHub** under toolkit/authentication setup.
   Create or select an auth config with minimal GitHub read permissions. Keep
   the real auth-config ID private; do not invent it.
2. Use the hosted connection flow with `!max_connect github` in a DM from an
   authorized owner. Complete the hosted consent link yourself. Inspect
   `!max_connections` or `!max_connection_status github` in DM. The
   connection belongs to the derived `max-discord-<DiscordID>` owner.
3. Discover the exact read-only GitHub action slugs, input JSON Schemas and
   toolkit versions in the real account using Composio's SDK/toolkit catalog.
   Do not substitute example placeholders or use `latest` without a pinned
   version. Supported account-inventory candidates are listed below.
4. Copy `docs/composio-policy.example.json` to
   `.max_state/composio-policy.json` (an ignored/private location). Set
   `MAX_COMPOSIO_POLICY_FILE` to that exact location. Populate the real
   `connections.github` auth-config ID, the precise connected account ID,
   `user_id=max-discord-<DiscordID>`, exact discovered action slugs and
   versions. Set `github_repository_allowlist` only to reviewed `owner/repo`
   names. `actions` must remain `risk=read`.
5. Configure `MAX_OWNER_DISCORD_IDS`, `MAX_GITHUB_ALLOWED_USER_IDS`,
   `COMPOSIO_API_KEY`, `MAX_COMPOSIO_ENABLED=true`, and
   `MAX_EXTERNAL_ACTIONS_DISABLED=false` only in an explicitly approved
   **test** bot environment. The kill switches default to disabled.
6. Verify via owner DM: `!max_capabilities`, `!max_github_repos`, and then
   `!max_tool <account_id> <EXACT_READ_SLUG> {}` only if the discovered input
   schema accepts empty arguments. For nonempty inputs, validate exact fields
   and repository scope. Unauthorized or cross-account requests must fail.
   No live credentials are required for CI tests.

The generic external action entrypoint now rechecks authenticated owner,
GitHub-specific user allowlist, and connected-account owner before every read.
The SDK, model and tool results remain isolated. Structured model-generated
tool invocation remains disabled pending a separately reviewed orchestrator.

See [NVIDIA model configuration](NVIDIA_NEMOTRON.md) for the independent
inference provider.

## Diagnose `max connect github` reporting disabled

The connection handler checks owner authorization **first**. If the owner
passes but the adapter was disabled or failed to initialize, no Connect Link
or GitHub authorization request is attempted.

1. In a **DM** from the authorized Discord owner, run
   `!max_composio_status`. The command reports missing settings, missing
   or malformed private policy JSON, missing GitHub auth config, sample
   placeholder IDs, and the emergency disable switch. It **never returns**
   API key values, private policy contents, account IDs, or OAuth links.
2. In your Composio dashboard, select the intended project, create a
   minimally scoped GitHub **auth config**, and find its **real** auth-config
   ID. The auth-config ID is not the API key and not a connected-account ID.
3. Place a private JSON file at the container path named by
   `MAX_COMPOSIO_POLICY_FILE`, following
   `docs/composio-policy.example.json`. For connecting **only**, start
   with `{"autonomy":"observe","connections":{"github":"<actual-auth-config-id>"},"github_repository_allowlist":[],"actions":[]}`.
   This enables the hosted consent link but enables **zero GitHub tools**.
   Do not paste any real key, private ID, or token into your Git repo.
4. Set `COMPOSIO_API_KEY` privately, `MAX_COMPOSIO_ENABLED=true`,
   and after verifying the test account and policy,
   `MAX_EXTERNAL_ACTIONS_DISABLED=false`. Keep the owner allowlist
   `MAX_OWNER_DISCORD_IDS` valid (legacy approver fallback works **only**
   when the primary variable is absent).
5. **Railway:** `.max_state/` is gitignored, so the policy file is not
   deployed with GitHub code. Put it on a mounted persistent volume or
   provision it securely in the container before bot startup. The value of
   `MAX_COMPOSIO_POLICY_FILE` must be an **absolute or correctly
   resolved path inside the Railway container**, not a PC file path.
   Files outside a volume are ephemeral and can disappear at redeployment.
6. Restart the test bot and DM `!max_composio_status` again. A
   locally ready report **does not prove Composio credentials are valid**.
   Next DM `!max_connect github`, complete the private hosted consent
   link, and check `!max_connection_status github`.
   Enabling a GitHub connection **never enables repository writes**.

If the status command reports that the policy file is missing, toggling
`MAX_COMPOSIO_ENABLED=true` alone does not fix it. Do not alter the
Composio emergency switch before the owner reviews the policy.

## Conversational connections

The optional `composio==0.18.2` SDK provides `connected_accounts.link`, `list`,
`get`, `wait_for_connection`, and `delete`. Max uses the hosted Connect Link and
explicit status checks; it never handles OAuth passwords or tokens. The pinned
SDK does not expose `connected_accounts.revoke`, so disconnect deletes the
Composio connection. Check the provider's connected-app settings if you need
to verify upstream revocation. See the [Connect Link guide](https://docs.composio.dev/docs/tools-direct/authenticating-tools)
and [Python connected accounts reference](https://docs.composio.dev/reference/sdk-reference/python/connected-accounts).

In the private `MAX_COMPOSIO_POLICY_FILE`, add `connections` mapping exact
toolkit slugs to existing Composio auth config IDs, for example `github` and
`googlecalendar`. The example policy shows the shape. Max checks that the
auth config belongs to the requested toolkit. Set `COMPOSIO_API_KEY`,
`MAX_COMPOSIO_ENABLED=true`, `MAX_EXTERNAL_ACTIONS_DISABLED=false`, and
`MAX_OWNER_DISCORD_IDS` to trusted Discord user IDs. The policy and SDK
must be available at startup. No connection is created when they are missing.

An authorized owner can write `Max, connect to GitHub`, `Max, connect to Google
Calendar`, `Show my connected apps`, `What permissions does my GitHub connection
have?`, or `Disconnect GitHub`. Deterministic commands are `!max_connect
<toolkit>`, `!max_connections`, `!max_connection_status <toolkit-or-account-id>`,
and `!max_disconnect <toolkit-or-account-id>`. Max sends links and account
details by DM, even when the request came from a server channel. The owner
completes consent on the hosted page, then checks status; only `ACTIVE` is
reported as connected. `INITIALIZING` and `INITIATED` remain pending, and
`EXPIRED` requires a fresh link. Every operation binds to the Discord account's
derived `max-discord-<id>` Composio user ID and verifies account ownership.

Connection details include the account ID, status, reported scopes when
available, and action categories explicitly configured for that account.
Connecting does not add actions to the policy or run them. `!max_tool` retains
the action allowlist and per-write approval. With multiple accounts for one
toolkit, use the exact account ID from `!max_connections`; Max will not guess.

`Max, which repositories do you have access to?` or `!max_github_repos`
creates an owner-only, read-only GitHub visibility report by DM. Add
`GITHUB_WHO_AM_I` and `GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER`
as separate read entries for the active connected account, with exact live
versions from tool discovery. The list action may instead be one of the
supported app-installation repository list actions. Max validates the live
schema and arguments before each read and uses the external action task path.
The report separates the API identity, visible repositories, and the
intersection with `github_repository_allowlist` in the policy. This allowlist
defaults to empty and is enforced for other repository-scoped Composio GitHub
actions. Those actions need an exact `owner` and `repo`, `full_name`, or
`repository` argument matching the allowlist; actions without a resolvable
repository are denied. Only permission flags actually returned for each
repository are shown. Visibility does not authorize writes. Configure
`MAX_GITHUB_ALLOWED_USER_IDS` and `MAX_OWNER_DISCORD_IDS` for the same
trusted Discord owner. No GitHub names, permissions, or account login are
inferred when a connection, action, or API field is absent.
This allowlist governs Composio GitHub actions; the separate `GITHUB_TOKEN`
repository workflows retain their existing GitHub operator gate and token
permissions.

### Owner configuration and diagnostics

Discord supplies `message.author.id` as an integer. Max never accepts an ID
claimed in message text. Connection management, Composio admin commands,
external action approval, memory admin commands, and `!max_inference_status`
use the same owner check. Set `MAX_OWNER_DISCORD_IDS` to unique,
comma-separated numeric IDs in the running bot's private Railway variables.
If this variable is absent, Max uses `MAX_APPROVER_DISCORD_IDS` for these
commands for compatibility. An explicitly empty or malformed owner variable
does not fall back. Publishing approval still requires its separate
`MAX_APPROVER_DISCORD_IDS`; GitHub repository operations also require
`MAX_GITHUB_ALLOWED_USER_IDS`. A missing list, malformed list, or requester
outside the list gives a distinct safe diagnostic without exposing IDs.
Changes to Railway variables require a bot restart or redeployment before the
running process can use them; `.env.example` does not configure Railway.

To copy your own Discord user ID, enable Developer Mode under **User Settings
→ Advanced**, then right-click your user in a DM or server and choose **Copy
User ID**. Paste the number into private Railway variables, not a Discord
message or repository file. See [Discord's ID guide](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID).

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

The existing `ModelRouter` uses loopback Ollama by default. `AI_MODEL=auto`
selects a model on the FreeLLM gateway; it does not select FreeLLM as Max's
provider. For the Railway-hosted FreeLLM service, keep its existing private
`AI_BASE_URL` ending in the OpenAI-compatible API base path (usually `/v1`),
set `AI_API_KEY`, `AI_MODEL=auto`, `MAX_INFERENCE_PROVIDER=freellm`, and
`MAX_INFERENCE_POLICY=cloud_allowed`. `cloud` remains a compatibility alias
for `freellm`. Ordinary Discord chat is classified as `chat`, so it also needs
`MAX_ALLOW_CLOUD_CHAT=true` after the owner approves that data flow. Pasted
repository context uses `private_repo` and needs its own specific opt-in.
Repair prompts include `source` and `memory` and require both
`MAX_ALLOW_CLOUD_SOURCE=true` and `MAX_ALLOW_CLOUD_MEMORY=true` (or the broad
`MAX_ALLOW_CLOUD_PRIVATE_CONTEXT=true` opt-in). The bot never infers cloud
consent from the URL or API key.
`MAX_INFERENCE_PROVIDER=auto` prefers Ollama and uses FreeLLM after a local
failure only when `MAX_AUTO_CLOUD_FALLBACK=true`, the cloud policy is enabled,
and the request's data classes allow it. Private chat, source, and memory
remain blocked unless the corresponding cloud privacy options grant them.
Credential and authentication material stays blocked for every cloud class.
The router reports provider-specific missing key, disabled policy,
connection, timeout, rate limit, request rejection, and invalid response
errors without echoing provider bodies or credentials. It logs provider,
request ID, final status, fallback flags, and safe error category only.
If chat says Ollama is unavailable, verify `MAX_INFERENCE_PROVIDER` first:
leaving it unset selects local Ollama even when `AI_BASE_URL` is configured.
Check `AI_BASE_URL` and `AI_MODEL` privately against the deployed FreeLLM
service. No live provider call is part of the offline tests.

FreeLLM is only used when its provider and per-data-class privacy options are configured.
Cloud requests are blocked when credential redaction detects common credential
labels, token prefixes, or private-key blocks. Detection cannot guarantee
coverage of every secret format. Composio results are not passed to
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
