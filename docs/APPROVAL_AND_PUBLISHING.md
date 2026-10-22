# Discord approval and test-repository publishing

Publishing is **off by default**. The first enabled operation is limited to a
validated `!max_fix` proposal in one explicitly named test repository. It
creates a new `max-fix-*` branch, replaces one approved file on that branch,
and opens one pull request against the repository's existing default branch.
It cannot merge, deploy, release, force-push, or commit to the default branch.
`!mex_fix` remains an alias. `!update_repo` still prepares a pending manifest;
its old branch-only staging write stays disabled. Remote memory writes remain
disabled.

## Configuration

Set all of these only after reviewing the implementation and offline tests:

| Variable | Meaning |
| --- | --- |
| `MAX_PUBLISH_ENABLED=true` | Explicitly enables the test workflow. Any other value disables it. |
| `MAX_PUBLISH_TEST_REPO=owner/repo` | Exact owner and repository name of the authorized test target. |
| `MAX_APPROVER_DISCORD_IDS=123,456` | Comma-separated Discord user IDs allowed to request and decide approvals. Missing, malformed, duplicate, or empty lists disable publishing. The requester must approve their own proposal. |
| `MAX_GITHUB_PUBLISH_TOKEN` | A separate fine-grained GitHub credential scoped to that test repository, with Contents read/write and Pull requests read/write permissions. Never put it in a command, prompt, message, URL, or log. |
| `MAX_PUBLISH_DB` | Optional path for the local SQLite operation ledger. Use persistent, private storage if recovery after restart matters. |

The existing `GITHUB_TOKEN` remains in use for the legacy read and discovery
paths. The publishing token is held only by the publishing GitHub client and is
not copied into task payloads or database records. `!max_fix` also retains its
existing `MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT=true` requirement because its
sandbox steps send repository content to a remote service. Model cloud routing
has its own independent privacy controls.

If any publishing setting, GitHub client, Discord interaction, or state store
is unavailable, publishing stops. No credentials are needed for the offline
tests, and this configuration is not installed by the code change.

## Approval and recovery

The Discord message identifies the requester, exact repository and branches,
changed file, bounded added/removed line count, payload digest, validation
and static review result, expiry, and effects of approval. A high-severity
static finding blocks preparation. Discord supplies the
interacting user's ID; the server checks the allowlist and matches that ID to
the request. A random per-request nonce and the task engine bind the decision
to the task, operation, target, requester, complete payload digest, and
deadline. Rejection, expiry, cancellation, mismatch, and replay cannot execute
the old request. Controls are disabled after a final interaction where the
Discord message can be edited. Pending controls are not restored after restart.

The ledger stores operational metadata, hashes, state, timestamps, and the
requester's ID. It stores no source, diff, prompt, validation log, or token.
Payloads exist in process memory only until a decision; restart invalidates
them. States include prepared, awaiting approval, approved, executing,
completed, failed, outcome uncertain, and reconciliation required.

A timeout or error during a GitHub write is treated as **outcome uncertain**.
The bot does not repeat the write. An authorized requester can run
`!max_publish_status <task-id>` to query the expected branch, file hash, and
pull request. This command makes read-only GitHub calls. A complete matching
branch, file, and PR marks the ledger completed. A partial or ambiguous result
requires manual inspection and a new proposal and approval before any further
write. GitHub writes are not transactional; a branch or file commit may remain
after a failed PR creation. No automatic rollback or write resume is attempted.
If the SQLite ledger is lost, inspect the test repository manually before
submitting any new request.
