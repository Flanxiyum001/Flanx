# Local inference and privacy

Max uses Ollama locally by default. The general model is `qwen3:8b`.
`qwen3-vl:8b` is selected only for an explicit vision task or override; image
input is outside this phase. No model is downloaded automatically and no cloud
key is required for local inference.

## Model selection

The default for planner, coder, reviewer, and tester roles is `qwen3:8b`.
Planner selects the general route, coder the coding route, and reviewer and
tester the review route. Specialist adapters remain opt-in; the static reviewer
and tester do not make model calls by default.

`MAX_LOCAL_MODEL` changes the default text model. Task overrides are
`MAX_MODEL_CHAT`, `MAX_MODEL_GENERAL`, `MAX_MODEL_RESEARCH`,
`MAX_MODEL_CODING`, and `MAX_MODEL_REVIEW`. Role overrides are
`MAX_MODEL_PLANNER`, `MAX_MODEL_CODER`, `MAX_MODEL_REVIEWER`, and
`MAX_MODEL_TESTER`. `MAX_MODEL_VISION` or `MAX_VISION_MODEL` selects a
vision model explicitly. Existing `AI_MODEL` and `AI_RETRY_MODEL` values
remain model ID overrides. Configure IDs installed on your Ollama server when
using local inference. With `auto` provider selection, Ollama uses its local
model setting; FreeLLM uses `AI_MODEL` or a task override.

## Privacy and cloud

`MAX_INFERENCE_PROVIDER=local` and `MAX_INFERENCE_POLICY=local_only` are
the defaults. To explicitly use the Railway-hosted FreeLLM gateway, set
`MAX_INFERENCE_PROVIDER=freellm`, `MAX_INFERENCE_POLICY=cloud_allowed`,
`AI_BASE_URL` to that service's OpenAI-compatible base URL, `AI_API_KEY`,
and `AI_MODEL=auto` if that gateway supports `auto`. `cloud` remains a
compatibility alias. No public URL is assumed. Leaving the provider unset
selects Ollama even when the FreeLLM URL and model are present.

Repository source, memory, and Discord chat content are treated as private.
Cloud use for them also requires an explicit data-class opt-in. Ordinary chat
uses `chat` and requires `MAX_ALLOW_CLOUD_CHAT=true`. Pasted repository names
or references use `private_repo` and require
`MAX_ALLOW_CLOUD_PRIVATE_REPO=true`; fenced source uses `source` and requires
`MAX_ALLOW_CLOUD_SOURCE=true`. Memory requires `MAX_ALLOW_CLOUD_MEMORY=true`.
One narrow setting never authorizes another class. The broad
`MAX_ALLOW_CLOUD_PRIVATE_CONTEXT=true` setting permits all private classes.
Each setting still requires `MAX_INFERENCE_POLICY=cloud_allowed`
and an explicitly selected cloud provider.
The repair prompt contains both source and memory, so its cloud route requires
both narrow permissions or the broad private-context permission.
Credential or authentication material is rejected before a FreeLLM request,
even when its data class is approved. Secret redaction remains a backup guard.
Blocked FreeLLM requests explain the policy and offer a local route. In
`MAX_INFERENCE_PROVIDER=auto`, a local failure can trigger FreeLLM only when
`MAX_AUTO_CLOUD_FALLBACK=true` and all policy and data-class gates permit it.
The separate
`MAX_ALLOW_CLOUD_REPOSITORY_CONTEXT=true` setting enables the remote repair
sandbox and web workflow; it does not permit cloud model inference.

Each inference request now records a correlation ID, requested provider,
adapter that actually executed, configured model, success or failure, fallback
attempt and use, and a safe error category. A normal Discord chat reply
includes its Request ID. An authorized owner can DM
`!max_inference_status <request-id>` (or omit the ID for the latest bounded
record) to inspect the adapter outcome. Server logs include the same ID and
routing status, without prompts, credentials, or provider response bodies.
For FreeLLM, Max reports the **FreeLLM gateway** and configured model;
the gateway's upstream model provider is **unknown** unless a future trusted
API explicitly supplies that identity. `AI_MODEL=auto` does not identify an
upstream model. Records are in memory and reset on restart.

## Resource limits

`MAX_OLLAMA_BASE_URL` (or `OLLAMA_BASE_URL`) defaults to
`http://127.0.0.1:11434`. The router requires a loopback endpoint and calls
Ollama's native `/api/chat` endpoint without following redirects.
`MAX_LOCAL_INFERENCE_CONCURRENCY` defaults to `1`;
`MAX_CLOUD_INFERENCE_CONCURRENCY` defaults to `2`.
`MAX_OLLAMA_KEEP_ALIVE` defaults to `0`, unloading a local model after
each reply so large models are not retained together by default.
`MAX_MODEL_TIMEOUT_SECONDS` defaults to `110`,
`MAX_MODEL_CONTEXT_TOKENS` to `8192`, and `MAX_MODEL_INPUT_CHARS` to
`50000`. Increase limits only if your chosen model and available memory
support them. A timed-out or cancelled request may continue on the model
server, so the process limit cannot guarantee that an earlier server-side
inference has stopped.

## Windows smoke test

Run `ollama list` to verify `qwen3:8b` is installed, and ensure Ollama is
running. In PowerShell, start `python` from this directory and enter:

```python
import asyncio
from model_router import ModelRouter
reply = asyncio.run(ModelRouter().generate([{"role": "user", "content": "Say hello"}]))
print(reply.model, reply.text)
```

The automated tests use mocked clients and require no running model server,
API key, or model download.
