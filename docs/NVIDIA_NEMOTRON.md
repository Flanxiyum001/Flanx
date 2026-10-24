# NVIDIA Nemotron 3.5 Lightning (opt-in)

Max can use the **official NVIDIA-hosted** OpenAI-compatible Chat Completions
API. The model is a separate `nvidia` provider, not an alias for FreeLLM.

- Official model: https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b
- API base: `https://integrate.api.nvidia.com/v1`
- Model ID: `nvidia/nemotron-3.5-lightning-30b-a3b`
- Existing dependency: `openai>=1.40.0`; no additional dependency required.

## Activation (manual; do not enable on Railway yet)

Leave `MAX_INFERENCE_PROVIDER=local` for current deployments. After tests
pass and the owner has reviewed which data may reach NVIDIA, set privately:

```dotenv
MAX_INFERENCE_PROVIDER=nvidia
MAX_INFERENCE_POLICY=cloud_allowed
NVIDIA_API_KEY=<retrieve-from-your-own-NVIDIA-account>
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
```

Never paste real keys into Discord, commits, policy JSON, or logs.

Cloud-data classification remains mandatory. Ordinary chat requires the
**separate**, deliberate opt-in `MAX_ALLOW_CLOUD_CHAT=true`. Private
repositories, source code, and memory require their existing class-specific
`MAX_ALLOW_CLOUD_PRIVATE_REPO`, `MAX_ALLOW_CLOUD_SOURCE`, and
`MAX_ALLOW_CLOUD_MEMORY` opt-ins respectively. Keep all of these false
unless separately approved. Credentials are denied even when a data class
is otherwise permitted. The keyword scanner is a defense in depth, not a
guarantee that all secret formats can be detected.

The provider does **not** change automatic fallback, select NVIDIA based on
`AI_MODEL`, or execute model tool calls. It uses `stream=false` and
`chat_template_kwargs.enable_thinking=false`; this avoids exposing reasoning
as the final answer. Streaming, reasoning output, and model-to-tool orchestration
are intentionally **not enabled** without separate compatibility/security tests.

`MAX_MODEL_TIMEOUT_SECONDS` bounds the request and NVIDIA has zero automatic
client retries. The adapter reports safe categories such as
`missing_credentials`, `privacy_blocked`, `timeout`,
`authentication`, `rate_limited`, `rejected`, and `malformed_response`,
without upstream response bodies or credentials.

## Offline verification

```sh
python -m unittest tests.test_nvidia_provider -v
python -m unittest discover -s tests -v
python -m compileall -q .
git diff --check
```

Unit tests use a mocked completion client. They make no NVIDIA calls.

After an explicitly authorized test request, the owner may DM
`!max_inference_status` to see the selected provider, configured model,
request status and safe error code, not prompts or credentials. This history
is in-memory and resets at restart.

## Composio separation

Composio is **not** tied to the NVIDIA client. Connections remain bound to
`max-discord-<DiscordID>`; the policy and task engine validate actions.
This increment does not enable model-driven execution. GitHub actions are
read-only; writes, merges, and deployment are not enabled by this integration.

See `docs/PHASE7_COMPOSIO.md` for dashboard/auth-config/action setup.
