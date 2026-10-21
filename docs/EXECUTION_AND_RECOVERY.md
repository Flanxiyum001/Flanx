# Execution checks and task recovery

## Local check boundary

The specialist tester runs only the named `compile` check by default. It
invokes the current Python executable with fixed `-I -S -B` flags and
`compileall` arguments, an explicit temporary-copy working directory, and
an environment containing only Windows system directory variables, workspace
`TEMP`/`TMP`, and UTF-8 output configuration. It rejects symlinks in the
copy. `MAX_CHECK_TIMEOUT_SECONDS` defaults to 10 seconds and is capped at 30;
`MAX_CHECK_OUTPUT_BYTES` defaults to 4,000 and is capped at 16,000.
Timeouts and cancellation request termination of the direct child process.
Descendant processes and work already started cannot be guaranteed stopped.

The temporary directory, Python isolated mode, and subprocess are **not** an
operating-system security sandbox. They cannot reliably prevent a Python test
from reading or writing other files accessible to the user. For this reason
the `unittest` specialist check is skipped by default, even when test files
exist. The workflow never treats a skipped check as a passing validation. Do
not enable execution of untrusted repository tests without a separately
configured stronger isolation mechanism. No such backend is supplied or
automatically installed in this phase. The existing OnlineCompiler.io sandbox
is a separate opt-in remote workflow with its own privacy gate.

## Durable task metadata

The Discord chat and repository-action orchestrators use the same SQLite file
at `.max_state/tasks.sqlite3`, or `MAX_TASK_DB` when explicitly set. Writes
are committed as SQLite transactions. The file stores task IDs, task types,
status, timestamps, bounded attempt counts, permitted capability names, and
generic outcome codes. It does not store descriptions, target names, payloads,
prompts, source, credentials, approval grants, or full results. Invalid rows
and unsupported schema versions are ignored. An unreadable or corrupt database
does not crash startup; new task execution fails closed until the file is
repaired. Keep the file private to the Max process.

On restart, pending, running, and awaiting-approval tasks become
`interrupted`. An external action that might have started is marked
`outcome_uncertain`; it is never replayed. Pending approvals expire and no
approval is restored. Repeating an uncertain action requires reconciliation
of its actual external state and a new task with fresh authorization.
Completed, failed, and cancelled metadata remain terminal. Recent task
history is bounded to 256 records per orchestrator; old terminal records are
removed as new tasks arrive.

This durability depends on the filesystem that holds the SQLite file. A
Railway instance without a persistent volume can lose local state on redeploy.
The current production repository write handlers remain disabled.

## Opt-in Windows Ollama smoke test

From PowerShell in the repository directory:

```powershell
ollama list
python -B scripts/smoke_local_ollama.py
```

Ensure the Ollama Windows app/server is running first. To override the model
for this PowerShell session, run `$env:MAX_LOCAL_MODEL = 'qwen3:8b'` before
the script. The script checks the local `/api/tags` listing, sends one small
prompt through the local router, and succeeds only for a non-empty response.
It forces local-only routing, refuses non-loopback endpoints, does not print
the response or credentials, and does not modify the repository or run
generated code. This is separate from the offline unit suite. It has not
been run automatically.
