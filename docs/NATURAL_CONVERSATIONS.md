# Natural conversation and owner-safe commands

Max now supports **prefix-free text conversation**. This changes message
routing, not Composio, publishing, or access-control policy.

## Where no prefix is needed

- **DMs to Max:** simply type `hi`, `explain this error`, `connect github`,
  `show my connections`, or another request. Ordinary chat is available to
  users in DMs; privileged commands still require the existing owner checks.
- **Servers:** mention Max or use the legacy `max ...` trigger as before.
  To speak without *either* in a specific server text channel, configure
  `MAX_NATURAL_CHANNEL_IDS` to a comma-separated list of **Discord channel
  IDs**, such as `123456789012345678,234567890123456789`. Only messages from
  `MAX_OWNER_DISCORD_IDS` (or the existing legacy fallback) trigger passive
  replies in these explicitly selected channels. This is **off by default**.
- `!` commands are still supported and dispatched **once** by Discord.py.
  Other server messages and attachments are ignored. Bot-authored messages
  are ignored.

## Example requests

| Write in a DM | Behavior |
| --- | --- |
| `hey, how are you?` | Natural conversational response |
| `connect github` | Existing owner-checked, private Composio Connect Link |
| `please connect to Google Calendar` | Same owner-checked connection handler |
| `show my connections` | Existing private connection inventory |
| `check my composio setup status` | Existing DM-only owner diagnostic |
| `show my tools` | Existing DM-only scoped capabilities listing |
| `show my github repos` | Existing DM-only, GitHub-allowlisted visibility |
| `check model status` | Existing owner-only inference diagnostic |
| `send email to person@example.com subject Hello body Hi` | Existing owner-restricted email integration |
| `fix src/app.py in owner/repo: ...` | Existing permissioned repair + verification/approval workflow; no unsafe direct writes |
| `run tool ca_ACCOUNT GITHUB_WHO_AM_I {}` | Exact Composio tool request; still requires private DM, ownership, account binding, policy action allowlist, live schema/version, and approvals for any supported write |
| `join my voice channel` | Existing owner-only voice path (if enabled) |

These are **anchored, deterministic command patterns**. The model cannot
invent commands or silently execute tools. For actions without a supported
handler, Max should answer or explain the limitation rather than claim work
occurred. Existing write approval and emergency kill switches are unchanged.

## Privacy & permissions

- Prefix-free server text is an **opt-in per channel** convenience only.
  No setting allows all users in a server to trigger privileged actions.
- Merely connecting a Composio account does not authorize its actions.
  The env-only GitHub connection fallback explicitly enables **zero actions**.
- Repository context and owner-only memory/diagnostics remain scoped by the
  existing `MAX_GITHUB_ALLOWED_USER_IDS` and `MAX_OWNER_DISCORD_IDS`.
- Speakable voice chat still requires its own locally enabled voice mode and
  `Hey Max` wake phrase. This change applies to **Discord text** messages.

## Tests

```sh
python -m unittest tests.test_prefix_free_natural_commands -v
python -m unittest discover -s tests -v
python -m compileall -q .
```

Tests use mocked Discord messages, connections, and tools; they do not
execute external actions. To use prefix-free chat in a server, add channel IDs
privately to Railway Variables for the selected service and deploy the config
change. No Railway updates are made by this PR.
