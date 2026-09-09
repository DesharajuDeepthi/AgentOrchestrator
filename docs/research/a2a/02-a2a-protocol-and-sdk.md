# A2A Protocol + SDK — Verified Facts

Everything in this file was verified against the actual installed SDK
(`a2a-sdk==0.3.26`, pulled in a clean venv on 2026-08-27) by introspection —
not recalled from training or docs. Where the SDK diverges from what the
protocol spec suggests, the SDK's behavior is recorded, because that is what
we integrate against.

## Protocol essentials (as implemented by SDK 0.3.x)

- **Discovery**: Agent Card served at `/.well-known/agent-card.json`
  (`A2ACardResolver`'s default path). Card fields (pydantic model
  `a2a.types.AgentCard`): `name`, `description`, `url`, `version`,
  `protocol_version`, `preferred_transport`, `additional_interfaces`,
  `capabilities` (streaming / push notifications flags), `default_input_modes`,
  `default_output_modes`, `security`, `security_schemes`, `signatures`,
  `skills`, `supports_authenticated_extended_card`, `provider`,
  `documentation_url`, `icon_url`.
- **Skills** (`a2a.types.AgentSkill`): `id`, `name`, `description`, `tags`,
  `examples`, `input_modes`, `output_modes`, per-skill `security`. Skills are
  **advisory routing metadata** — invocation is agent-level `message/send`;
  there is no per-skill RPC signature.
- **Task lifecycle** (`a2a.types.TaskState`, exact enum values):
  `submitted`, `working`, `input-required`, `completed`, `canceled`, `failed`,
  `rejected`, `auth-required`, `unknown`.
  Three states beyond the commonly-cited six: `rejected`, `auth-required`,
  `unknown` — our internal mapping must total-match all nine.
- **Messages**: `a2a.types.Message` — `role`, `parts[]` (text/file/data),
  `message_id`, `task_id`, `context_id`, `reference_task_ids`, `metadata`,
  `extensions`. Continuing a paused (`input-required`) task = send a new
  `Message` carrying that `task_id`.

## SDK client surface (the integration points we use)

```python
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.client.auth import AuthInterceptor, CredentialService

resolver = A2ACardResolver(httpx_client, base_url)          # default card path
card = await resolver.get_agent_card()                       # pydantic AgentCard

factory = ClientFactory(ClientConfig(streaming=..., polling=..., httpx_client=...))
client = factory.create(card, interceptors=[AuthInterceptor(credential_service)])

# ONE call shape for both streaming and polling transports — the factory
# picks the transport from the card + config:
async for event in client.send_message(message):
    # yields (Task, TaskStatusUpdateEvent | TaskArtifactUpdateEvent | None)
    # tuples, or a bare Message for message-only replies
    ...

task = await client.get_task(...)      # recheck a parked task
await client.cancel_task(...)          # Stop-button propagation
```

Key property: `Client.send_message` returns an **async iterator regardless of
transport** — streaming (SSE) and polling agents are consumed identically.
Our port can therefore expose one `send()` API with no transport branching.

## SDK auth surface — and its exact coverage gap

`AuthInterceptor` (in `a2a.client.auth.interceptor`) walks the card's
`security` requirements in order, asks the injected `CredentialService` for
each scheme name, and applies the **first** scheme for which credentials
exist. The `CredentialService` contract is a single method:

```python
async def get_credentials(security_scheme_name: str, context) -> str | None
```

It returns a *string credential*; the interceptor decides placement from the
card's scheme declaration. Verified placement coverage in 0.3.26:

| Scheme in card | SDK placement | Covered? |
|---|---|---|
| `http` bearer | `Authorization: Bearer <cred>` | ✅ |
| `oauth2` (any flow) | `Authorization: Bearer <cred>` | ✅ (placement only — **token acquisition is the CredentialService's job**) |
| `openIdConnect` | `Authorization: Bearer <cred>` | ✅ |
| `apiKey` in header | `<name>: <cred>` header | ✅ |
| `apiKey` in query / cookie | — | ❌ explicitly skipped (source comment) |
| `http` basic | — | ❌ falls through unmatched |

Two consequences for our design:

1. **OAuth2 client_credentials lives in OUR CredentialService**: authlib's
   `AsyncOAuth2Client` fetches/caches/refreshes the token; `get_credentials`
   returns the current access token; the SDK interceptor handles the header.
   Clean seam, no interceptor surgery for the main flow.
2. **`http` basic and `apiKey` query/cookie need our own interceptor** — a
   small subclass that first delegates to the SDK interceptor and handles the
   two uncovered placements itself (basic = base64 `Authorization: Basic`,
   query/cookie = merge into `http_kwargs`). ~40 lines, contract-tested.

## SDK server surface (used for the scripted test counterparty only)

`a2a.server.apps.A2AStarletteApplication` / `A2AFastAPIApplication` +
`DefaultRequestHandler` + `InMemoryTaskStore` + an `AgentExecutor`
implementation. This is how the deterministic in-process fake A2A agent for
contract tests (and the local acceptance counterparty) is built — mirroring
the fake LLM provider discipline of spec §11: key-free, scriptable,
byte-exact assertions. It is NOT part of the shipped product surface in this
wave (outbound only).

## Dependency note

`a2a-sdk` requires `google-api-core`, `protobuf`, `httpx`, `httpx-sse`,
`pydantic`. The protobuf/gRPC machinery rides along even though we only use
JSON-RPC/HTTP — accepted cost of the official SDK; imports stay confined to
`backend/app/a2a/` so a future slimming (or SDK swap) touches one package.
`authlib` adds `cryptography` + `joserfc`.

Python: SDK supports 3.10+; the backend image is 3.12 — compatible.
