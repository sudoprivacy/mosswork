# Managed host integration

Mosswork's managed execution consumer preserves the original durable session.
The selected first replacement integration is owner-authenticated
`nexusd-cluster` with `spawn_spec scode`, strict ACP and `serve_managed`. Its
provider must use the official owner connection and one `ManagedRuntimeContext`
for session, file, mailbox and model I/O. A failed attachment must not select a
local engine, direct model request or newly created session.

Trusted composition resolves the canonical owner and placement before credential
access. Authenticated child bootstrap and activation are required for this
selected cluster route. Controller credentials must not become owner or child
credentials; the historical optional `identity=null`/local path is refused.

The existing in-process cohost remains for current production sessions and safety
repairs until replacement acceptance. Its `start_session_v1` without `spawn_spec`
is a reference path, not the selected replacement launch. The consumer names
below do not establish an installed or qualified cluster provider.

## Implemented consumers

`infra/native_host/context.py` and `cohost.py` reuse the context and snapshot
consumers handed off by the shared policy workline. They require injected
identity, current grants, original native execution proof and admission checks.
A matching public session snapshot does not supply these proofs.

`infra/native_host/acp.py:CohostAcpSession.attach` uses the official Python ACP
SDK. It requires an already authenticated connection represented by RPC streams,
the exact published execution cwd, and a resolved `TrustedHostContext`:

1. Require current `session_load` authorization and native/DLP admission.
2. Initialize and require protocol 1, session loading, `hostContext` version 1
   and `inputDelivery` version 1.
3. Load the original native durable ID. A load error, malformed result or
   different returned ID refuses attachment. There is no `session/new` fallback.
4. Recheck the current binding, authorization and admission after load.

The inputDelivery consumer now exposes `submit_input(original_client_id, text,
delivery="queue" | "steer")`. It sends the existing authenticated
`_sudocode/session/input` submit action and returns the native allocated
`NativeInputSubmission`. It never supplies a numeric input ID. Acceptance does
not mean committed, completed or writer cleanup. Once this path is used, the
same attachment refuses legacy `session/prompt`; a legacy prompt attachment
cannot be reclassified as an inputDelivery attachment.

The caller must also supply the existing registered `mcp_servers` configuration
explicitly, including an empty list where appropriate. The consumer preserves
private connector selection; it does not select servers from prompt/tool text.

The registered execution resource is supplied by trusted composition, not by
tool arguments or queued browser metadata. The current authority must recognize
`session_load`, `session_prompt` and `session_cancel` for that resource; unknown
operations must deny. These are application authorization requests, not new
native capabilities or grants.

The execution-time `_sudocode/session/host_context` callback checks the original
native session, translates it to the bound product thread for the registered
domain reader, and checks current authorization before returning its text.
Stop or context withdrawal invalidates an in-flight read. Text is returned as
`appendSystemPrompt`; mutable model text is not inserted into load metadata,
the static system prefix or user history. No cached context replaces a failed
read. The domain reader must itself enforce the current model/source grant.

Prompt, Stop, context withdrawal, controller disconnect and native writer
termination have different outcomes. A prompt transport failure remains Unknown
and is not retried. Writing a Stop notification does not prove execution has
stopped. Closing this consumer withdraws its context and closes RPC tasks;
the original runtime owner must reconcile native cleanup. The unconnected native
permission UI currently refuses permission requests.

## Required composition

The consumers require the current owner credential and canonical organization
providers, binding persistence, native host incarnation proof and shared
admission. The adopted `ControllerMailboxStreams` now adapts the official
message-based `NexusSessionTransport` to public asyncio streams through a private
socketpair. Node retains the original SDK codec, cursor and read recovery;
Python ACP retains parsing and correlation. This is controller transport, not a
native file descriptor or a managed subprocess. Production attachment still
requires the matched package and actual current-authority assertions. Native
dispatch must consume its original fence at the effect boundary.

The product dispatch injection is implemented below. Its actual provider,
native input/question projection, durable existing-session comment ingress,
real model roundtrip, integrated Sudograph artifact qualification, production
rollout and phone acceptance remain integration gates.
The existing durable application queue must be preserved. Local integer user
IDs, browser control epochs and application queue run IDs are not native grants.

### Shared implementation boundaries

PC3 owns the product consumer orchestration, browser and one coordinated
server/AgentManager/chat/gateway consumer injection. Owner6 retains backend
implementation and event translation, including the actual provider for that
single injection. The backend workline owns the
current-host/grant/policy providers and trusted durable review admission,
enqueue, deduplication and acknowledgment. These consume the canonical identity
and current membership/business grants maintained by the business workline.
Sudograph retains domain model/context/review semantics; Root/W7 retain native
ownership, execution and cleanup contracts.

### Native browser MCP entry and effect authorization

PC3 owns the native browser tool consumer and its registration in the existing
`mcp_servers` argument to `CohostAcpSession.attach`. The selected tool entry is
the public adb Worker's `/mcp` endpoint. Reuse its existing `browser_launch`,
`browser_observe`, `browser_action`, `browser_stop` and `browser_restart` tools;
do not create another browser tool implementation or engine. Their input
schemas do not select a profile, owner, canonical actor or native run. Trusted
host composition selects the original browser allocation, connection and MCP
configuration before exposing these tools to the selected native session.

This registration is not installed in a production provider yet. Before
adb 0.58.22, `/mcp` and `/agent` authenticated a Worker AI token and dispatched
to `Worker.agent`. Those versions used the public
`BrowserControl.run` operation barrier. That barrier checked browser ownership
and epochs after waiting, but had no callback for the host's current native
actor, action or resource grant. AI launch also enters a separate lifecycle
barrier. A successful Worker token check does not supply these missing grants.

`BrowserProfileMiddleware` protects the legacy LangGraph tool path. It is not
executed by the selected native SudoCode branch. Likewise,
`ProfileControl.run_ai(before_effect=...)` is an implemented local consumer,
not an authorization hook inside a separate remote Worker's operation lock.
Checking `TrustedHostContext` before sending an HTTP request would still leave
a gap while that request waits inside the Worker.

The public embedding hook was delivered in
[ai-dev-browser 0.58.22](https://github.com/sudoprivacy/ai-dev-browser/releases/tag/v0.58.22)
through [PR 60](https://github.com/sudoprivacy/ai-dev-browser/pull/60). It supplies managed
`require_agent_authorization`, an HTTP `agent_authorization` factory and the
after-wait `before_effect` assertion. The product pins this published version.
The current native invocation provider and actual managed Worker embedding
remain required.

The shared public Worker embedding hook has the following contract
before the managed MCP registration can be enabled:

- Trusted server composition supplies one authorization callback for the
  original authenticated native invocation. The callback captures its original
  actor/session/run and exact operation/resource binding. No tool argument,
  decoded browser hint, Worker token or local integer user ID constructs it.
- The Worker invokes that callback after waiting for its public operation
  barrier and immediately before page observation or effects. Lifecycle
  creation, stop and restart must also check under their actual serialization
  boundary, before profile or browser effects; wrapping nested operation locks
  is not an acceptable substitute.
- Only `None` means successful authorization. A denial, missing managed
  callback, malformed completion or cancellation refuses the unsent action.
  After an awaited callback, recheck the original control/target epochs and
  control state. The native authority still owns its atomic grant/effect fence.
- Every enabled managed AI entry uses the same hook, including `/agent` if
  exposed. Do not publish an unguarded alternate endpoint or forward private
  Worker credentials to the renderer. An unavailable result remains Unknown
  and is not retried on another profile or session.

Owner6 supplies the actual current canonical/native action authorization and
owning runtime port. PC3 consumes that callback at the public browser barrier
and wires the MCP configuration into the one managed backend. Browser profile,
control and target epochs continue to coordinate observation and held input;
they are not native identity, grant generations or cleanup receipts. Public
Worker lifecycle/control remains the shared implementation, without a second
product browser journal for the native path.

The pinned dependency supplies this embedding hook. The managed native MCP
provider is not installed. SDK fixture workflows qualify the hook with real
MCP/Worker/Chrome; joint native qualification must use the actual current
authority and original native input:
queue an action behind input, revoke its grant before release and prove no page
effect; interrupt while authorization awaits and prove the unsent action stays
fenced; exercise a valid main/child invocation on the same browser and retain
the original session. Launch/restart refusal, exact profile isolation and lost
reply without replay need their own actual outcomes. Existing local middleware,
SDK fixture, transport and viewer receipts do not qualify the current native
authority bridge.

The owner connection library is available in Sudostack PR #147, merged as
`aa6311a704a83c65f40febd3ba6bc2c7bbfc903f`. Its unreleased
`@sudo/session-runtime` source version 0.1.0 provides
`connectSessionRuntime(config, authorizedOwnerId)` using the official Nexus SDK
0.8.1 and existing delegated mTLS renewal. Reuse it rather than porting credential
issuance or mailbox framing. PR #148 adds the private controller RPC entry,
merged as `1928fccf11c93905e9619a6a83c29429ca3e408e`. The Python
`ControllerSdkConnection` now consumes it with mandatory connect/dispatch
assertions and Unknown outcomes after uncertain effects. Its readiness means
SDK configuration; close releases its controller transport without proving
native writer cleanup. Adapting the official message-oriented ACP transport,
current resource grants and native execution proof remains required.

The selected cluster runtime and retained in-process cohost both require native
original-handle custody and authoritative writer/receiver cleanup completion. Root's custody candidate
`d3706e61763671bb90703a623663087febaa5920` does not establish the latter. Registry
absence, a cancellation acknowledgment or a closed mailbox must retain an Unknown
cleanup result until the native owner confirms completion. Preserve original
custody and refuse replacement while completion is unconfirmed. No new public
completion API is assumed here. Authenticated child bootstrap is required for
the selected cluster route; it is not evidence of joined cleanup.

The pinned reference Nexus 0.8.26 source stores native handles by PID. After a
registry instance is replaced, an instance check alone can therefore accompany
an old sidecar handle. Root's shared `HostedSession(instance, handle)` correction
at `d3706e61763671bb90703a623663087febaa5920`, or a qualified equivalent, is
required before current-host acceptance for both cohost and raw providers.
Mosswork consumes this native guarantee; it does not reproduce the handle map
or create a local custody proof. The correction's source is not a passed
validation or released AMD64 artifact.

SudoCode 0.2.40 source `f5e2f3f596c51a5336a18ded16ef38bf4eddb957`
already contains `hostContext` v1 and exact managed restore in the historical
cohost reference. Feature existence there does not qualify the selected cluster
binary. That candidate must demonstrate authenticated child activation, exact
load without creation, one managed I/O backend, advertised context/input
delivery, Unknown outcomes and authoritative cleanup using matched artifacts.

### Canonical API and queue boundary

The adopted `infra/identity/composition.py:build_canonical_business` constructs
the maintained business domains once. `CanonicalBusinessServices.app_kwargs()`
maps its eleven typed dependencies directly to the implemented keyword arguments
of `api/app.py:build_app`. Every human consumer shares the same snapshot/verifier,
human and machine issuance use the same maintained issuer, and writes share the
explicit writer assertion. The constructor reads no secret, opens no canonical
database, creates no schema and starts no SMS, SDK or native runtime.

The optional Feishu group requires the actual SDK channel, original plugin and
connection owner plus native session assertion together. Its sealed event claim
remains distinct from a human principal. Ordinary-member model facts and the
strict personal-Moss accessor remain separate; neither is an operation grant.

`bundle.model_store` describes LLM provider credentials and billing references.
Sudograph's `ModelBinding` instead identifies the registered graph authority,
model, rendering resource and full source set. The factory must supply Owner6's
independent `ModelAccess(host, actor_member_id, registered, binding, operation)`
assertion around graph I/O. Returning LLM credential facts from that assertion
is an invalid verdict, even when the facts are current. Resource access and LLM
entitlement cannot grant a graph action or projection. Human-only graph review
needs its own current full-model permission without creating a native session.

This source now exposes canonical lifecycle and readonly identity routes when
the bundle is explicitly injected. Each protected route checks its original
credential; adjacent local integer APIs receive no blanket JWT exemption or
canonical-to-local token conversion. The default `launch.py` call still supplies
no bundle or configured managed provider. Actual canonical workspace/HTTP/WS
ingress and the single configured startup factory remain incomplete.

Before initializing the product pool, that factory must establish distinct
canonical/product storage identities and preserve them. The bundle's later
path/hardlink guard cannot undo a wrong earlier initialization. The required
`phone_enabled` callable reads the actual effective setting on each status,
send-code and phone-login operation. Disabled SMS can omit both phone config
and sender while preserving the shared password/API-key issuer. Disabled new
phone login returns 404; an invalid setting or enabled SMS without actual ports
returns a sanitized 503. Existing credentials keep their original verification,
refresh, switch and logout rules. Status does not prove SMS delivery or writer
exclusion. The effective-setting and secret providers still need installation.

The bundle includes `canonical_profile` for current member profile, nickname
and password routes. It shares the same snapshots, original store and writer.
These routes do not allocate a product user or grant a resource operation.
The separate product `canonical_admissions` keyword and exact HTTP queue
reauthentication route remain composed alongside all eleven business providers.

The current business producer's `dashboard/src/api/modules/canonicalIdentity.ts`
defines `CanonicalIdentity`, `CanonicalMembership`, `CanonicalTokens` and
`canonicalIdentityApi`. Its existing `/api/identity/me` and `/organizations`
return the original account, organization and member identifiers, entitlement
and personal Agent reference. `/api/identity/switch-organization` returns the
new selected membership's normal credentials. These DTOs are UI projections
and selectors; they do not authorize an Agent, native session or browser effect.
Owner5 retains these APIs and login/profile/company UI seams. PC3 consumes them
in browser/frontend integration without duplicating account or membership logic.

For explicitly adapted effect routes, Owner5 supplies
`api/common/canonical_identity.py:canonical_authenticated_request` and
`canonical_authenticated_websocket`, returning the sealed
`CanonicalAuthenticatedRequest`. HTTP uses one Bearer header; the existing chat
WebSocket uses its one opaque token parameter. `await principal.revalidate()`
returns the current `CanonicalRequestIdentity`; its `.snapshot` supplies the
actual canonical business identity to Owner6. Matching `authority_id` text or
an unrelated snapshot instance cannot substitute for the same configured
authority/store.

These ingress adapters and the post-queue carrier are not installed in the
current product factory. The sealed principal and credential must remain outside
serialized request metadata, browser hints and durable queue payloads. The
provider must reauthenticate and check current resource grants after queue
waits. After restart, unsent work needs an authorized continuation of the same
original operation; submitted/Unknown work retains native custody and cannot
be replayed as a fresh conversation. The single `ManagedExecutionBackend`
consumer now accepts the sealed human as an explicit `admission` keyword for
stream/call/HITL and the awaited lifecycle/model effect ports below. The
process-local queue carrier is implemented; final ingress/factory installation
and actual resource/native grant providers remain.

Ordinary Agent resources and personal Moss resources have independent current
entitlement requirements. Owner5's registered-member model facts do not require
Moss; its personal-Moss owner accessor stays strict. The provider selects the
policy from the actual trusted registered resource kind. A display name, browser
hint, reference codec or caller-supplied flag cannot select that policy.

### Private ACP failure evidence

`AcpRequestFailed.receipt` retains the original immutable host binding, operation
phase, actual outgoing ACP request ID and correlated error code/message/data.
The official Python ACP implementation still parses and correlates responses.
These controller-private receipts reject serialization and are never public
error payloads. `AcpOutcomeUnknown` retains the same binding after a possibly
dispatched load/prompt loses its outcome. `AcpRequestCancelled` remains an
`asyncio.CancelledError`, so task cancellation is preserved with private evidence.
Local close failure is recorded separately and cannot replace the first error.
Input submission failures also retain the original `client_id` and any parsed
native `input_acceptance` privately. An accepted input followed by revoked
authority remains unavailable to the caller and is never replayed as a new ID.

An RPC rejection is not proof that no effects occurred. Only the authenticated
provider may interpret the exact selected missing-session code and original
session data; this consumer neither classifies generic errors as absence nor
creates a replacement. Local close never proves native writer/receiver joins.

### Native input and context ordering

The selected SudoCode source emits an ordered idle/pending state, then the active
turn's initial steering batch before asking for host context. Prompt persistence
and `committed` follow the context response. The consumer observes the public
ACP SDK's already parsed frames synchronously, preserving this order across
awaiting notification handlers. It does not read a mailbox, parse ACP privately,
schedule a batch or allocate an input identity.

Inside the existing context resolver/`ContextRecorder` task,
`session.context_input()` returns frozen controller-private `NativeInputContext`:
the original HostBinding, observed state revision, native turn ID and initial
batch's native IDs/clientIds/sources. Its stage is `pending_context`. The
registered-model provider must record the exact ModelPointer against those
original facts before returning context, rechecking the same evidence around
I/O. It must finalize only from the original committed/completion lifecycle.
No recording provider or atomic original-store association is supplied by this
read-only projection.

`session.input_context_recorder(record_pending)` adapts the actual existing-input
provider to Owner6's three-argument `ContextRecorder`. On each call it reads this
context task's current evidence, verifies the original host, awaits
`record_pending(host, registered, pointer, evidence)` with None-or-raise
completion, then checks the same input again. It caches no attachment-time input.
An observed paused state withdraws that context even if the same native turn
resumes during the recording wait. A paused initial active boundary cannot
establish context. Only a later observed idle-to-active boundary can establish
another pending batch; an additional queued input alone cannot retarget it.
The one factory retains the session reference, not its first input:

```python
async def record_context(host, registered, pointer):
    if session is None:
        raise HostUnavailable("Original input session is unavailable")
    await session.input_context_recorder(record_pending)(host, registered, pointer)

# Supply this keyword at the existing CohostAcpSession.attach call:
resolve_context = ports.host_context_resolver(record_context)
```

`record_pending` is the required trusted lifecycle persistence callback. Its
actual writer, original connection/custody verification and recovery/finalization
are provider work; this adapter creates no input store or replacement authority.
Each validated compiler response still supplies its own exact revision, semantic
digest and text. Recording cannot wait for a later native `committed` row.

If context resolution/recording fails or is cancelled after native acceptance,
the consumer withdraws new effects and retains its first private
`context_failure_receipt`: original binding, incoming ACP request correlation,
pending input evidence and original exception, with outcome `unknown`. It
releases no context text, replays no input and preserves that receipt after local
close. The receipt does not prove whether persistence or native input committed;
the original provider reconciles those separately. Existing lifecycle updates
remain subject to their own current checks; no context fetch is added for
terminal audit, ordinary Stop or authoritative cleanup.

The evidence is captured when the context request arrives, before authority
awaits. A different turn, Stop, pause, commit or withdrawal invalidates it;
later state cannot replace the original request's input. Overlapping context
requests refuse ambiguity. Without the observed idle-to-active boundary and
uncommitted initial batch, context refuses. A diagnostic `snapshot` action is
never sent: the original action enables/configures delivery and changes revision.

Native numbers remain local to the original execution/connection. State
revision, product run ID, token JTI and browser/control epochs are not native
incarnation or input identity. The private context object rejects serialization
and is available only during that actual context task. Current actor/resource,
model and native admission remain independent requirements.

The original transcript helper discovers native user association only after
commit. Waiting for that row inside ContextRecorder would wait for persistence
gated by the context response itself. It cannot be used as a pre-context receipt
source. Lost submission replies retain original custody and private correlation;
no new session, input ID, retry, committed ACK or joined-cleanup result is inferred.

### Current source interfaces and application injection sites

The following table separates callable consumers from the providers and wiring
still required for the first integrated candidate. It does not introduce a new
execution authority or claim that a provider is already installed.

| Source | Current callable or site | Implementation boundary |
| --- | --- | --- |
| `infra/native_host/context.py` | `resolve_trusted_host(principal, HostSelection(agent_id, thread_id), authority)` | Owner6 implements `HostAuthority.resolve/check_current/authorize` using the canonical authenticated caller and current grants. PC3 consumes the resulting `TrustedHostContext`. |
| `infra/native_host/cohost.py` | `CohostRuntimeHost(binding, execution, connection, assert_current_execution=..., assert_admission=...)` | Owner6 supplies the original owner connection, qualified native proof and applied policy. The existing snapshot consumer does not produce these proofs. |
| `infra/native_host/sdk.py` | `ControllerSdkConnection.connect(command, configuration, before_connect=..., before_dispatch=...)` and `session_operation(...)` | Owner6's private carrier reuses the official Node SDK and original owner configuration. Typed mailbox operations preserve the original SDK message transport; current grants/native proof remain required assertions. |
| `infra/native_host/mailbox.py` | `ControllerMailboxStreams.attach(sdk, endpoint, check_attachment=...)` | Adopted Owner6 socketpair adapter supplies public `reader`/`writer` to the existing strict ACP consumer. The endpoint must belong to the same delegated connection. Attachment authorization covers prepared original custody before the pending ACP load. The adapter creates no native session, retries no uncertain append and proves no native cleanup. |
| `infra/db/repos/retained_cohost.py` | `RetainedCohostBindingRepo.get_for_resume/assert_current` | Read original published owner/Agent/durable-session facts without initializing or writing the retained store. Product Agent/thread projection, current actor grants and native custody remain separate providers. |
| `infra/execution/selection.py` and `infra/db/repos/retained_session_projection.py` | `ProductCohostSelections.record_existing(...)` and `current(actor, selection)` | Product-owned references to existing Agent/thread/canonical-owner rows preserve the exact original app/owner/Agent/durable/repository tuple. `current` supplies Owner6's existing CurrentSelection port and rereads both stores. This is no resource-kind, grant or history-import authority. |
| `infra/db/repos/retained_ontology.py` | `RetainedOntologyBindingRepo.resolve_original/assert_current` | Recover the original project/authority/worker/model/source set and registry generation. Supply exact references to the domain client; this lookup grants no model or review operation. |
| `infra/db/repos/retained_model_audit.py` | `RetainedModelAuditRepo.record_access` | Append sanitized access metadata to the original canonical audit table. Owner6 binds the captured authenticated actor and registered model; SW3 must establish the single production writer role before installation. Audit failure propagates and cannot trigger replay of a possibly committed model write. |
| `infra/native_host/acp.py` | `CohostAcpSession.attach(host, reader=..., writer=..., execution_cwd=..., mcp_servers=..., resource_ref=..., resolve_context=..., on_update=...)` | PC3 owns strict ACP consumption. Owner6 supplies the actual shared-SDK carrier; local protocol streams are not evidence of the managed transport. |
| `infra/native_host/acp.py` and `inputs.py` | `session.submit_input(original_client_id, text, delivery=...)`; `session.context_input()` | Existing inputDelivery submit and ordered pending-context evidence are implemented. Owner6's provider must use this path for original input/context recording, preserve actual current connection custody and persist/finalize the pointer through the original lifecycle. No recorder store, new native identity or scheduler is supplied. |
| `infra/native_host/context.py` and `infra/sudograph/client.py` | `host.host_context_resolver(resource_ref, domain_client.host_context)` | Low-level text reader. Managed composition uses the registered ports below so the exact context revision/digest is retained for the original native input. |
| `infra/native_host/sudograph.py` | `RegisteredSudographPorts(host, original, registered, ..., require_model_access=..., transport=...)` | Implemented host-side `HostPorts` consumer. It checks the exact registered model/source tuple around service I/O and commits sanitized access audit. Actual full-model grants and reviewed worker transport remain mandatory providers. |
| `infra/native_host/sudograph.py` | `ports.host_context_resolver(record_context)` | Reads context once, retaining text and its validated revision/digest from the same response. The original native input owner must durably record that pointer before text is returned; this constructor does not create a run, input or pointer store. |
| `infra/native_host/sudograph.py` and `infra/db/repos/retained_review.py` | `ports.capture_review(endpoint, store)` and `ports.restore_review(endpoint, store)` | Persist the exact authorized compiler document in the existing canonical review table before serving, then restore the original pointer/HTML under fresh grants. Actual private page grants, frame serving and authenticated durable comment ingress remain to be installed. |
| `infra/identity/canonical.py` | `CanonicalSnapshotService.snapshot(opaque_access_token)` | Current business snapshot consumer preserves independent account/member/org identities and checks the retained store. Actual configured verifier, sealed authenticated request and product ingress installation remain required. |
| `infra/native_host/authority.py` | `RetainedCohostAuthority(...)` | Reuses the current business snapshot and exact retained execution binding before resolving a native connection. Current selection, target entitlement, operation grants and manager-owned native providers are mandatory. |
| `infra/native_host/browser.py` | `await admission.prepare(action)` and `prepared.before_effect()` | Binds the original canonical personal browser and freezes the action across awaits. Actual action/native-input and allocation assertions are required inside the public Worker hook; this consumer neither forwards the callback over HTTP nor installs a factory. |
| `infra/sudograph/client.py` and `protocol.py` | `SudographClient.review_document`; `ReviewDocument.record/restore` | Capture exact immutable HTML/revision/digest together. Trusted production storage and original-session provenance must be durable before PC3 serves that document. Restoring a checked record does not grant access or deliver a comment. |
| `infra/browser/worker.py` | `BrowserWorkerClient.invoke(action)` with fixed `WorkerBinding` | PC3 owns the public Worker transport, viewer and input consumers. Owner6 resolves current human/native browser grants and the authorized allocation. A browser session ID must not be assumed to equal a native conversation ID. |
| `infra/browser/control.py` | `ProfileControl.run_ai(..., before_effect=assert_current_browser_access)` | Invoke the host's None-or-raise assertion after the input barrier wait and before page observation or effects. Stop during that await fences the unsent action. Native tool registration and the service's atomic effect fence remain provider responsibilities. |
| `infra/agents/execution_backend.py` | `ManagedExecutionBackend` | PC3's product consumer port; Owner6 implements it with current shared authorities. Product IDs/requests are selectors, not grants. No production provider is installed by this protocol. |
| `infra/server.py` | `MossworkServer(..., managed_backend=backend)` and `_boot_runtime` | One explicit constructor injection passes the same backend into `AgentManager`. A failing selected provider never enables a legacy fallback. The production provider factory remains required. |
| `infra/execution/gateway.py` and `infra/agents/manager.py` | `GatewayExecution.execute`; `AgentManager.stream/call/resume_hitl/cancel_stream` | Preserve the existing durable application queue. `AgentManager.execution` remains its queue adapter. The injected managed branch bypasses legacy harness initialization/start and routes product execution to the selected provider. Actual native/provider qualification remains pending. |
| `api/routers/chat/history.py` and `infra/gateway/gateway.py` | `cancel_thread`; `Gateway.cancel_dashboard_turn` | PC3 preserves ordinary product Stop and browser input pause. Owner6 consumes original-session native cancellation and completion; these outcomes remain distinct. |
| Trusted review ingress | No production callable is delivered yet | Owner6 owns authenticated provenance, current grants, original-session enqueue/dedup/ACK. PC2 supplies domain target resolution; PC3 connects the product consumer after the actual API is delivered. |

The first joint qualification target uses owner-authenticated `nexusd-cluster`
with `spawn_spec scode`, strict ACP and `serve_managed` for exact original-session
resume, model/context, browser operations,
ordinary Stop, reconnect and existing-session comment delivery. It must use the
same immutable source/artifacts for business, domain and browser acceptance and
SW3 rollout/rollback qualification. Until providers, native fixes and artifact
identities are delivered, current exports are source assembly inputs, not a
qualified integrated release candidate. No code outside the owned worktree is
changed to assemble these inputs.

### Product dispatch port

`ManagedExecutionBackend` supplies `boot/aclose`, `start/stop/reload`,
`stream/call/resume_hitl`, `request_cancel`, and the existing product thread model
preference operations. `MossworkServer` accepts it explicitly as
`managed_backend`; the application creates no default managed provider. Once
injected, the same backend is retained across provider errors and close. Calls
before successful boot or after close refuse rather than entering the harness.
Successful lifecycle assertions complete with `None`; malformed verdicts refuse.
The local boot flag describes adapter initialization and never caches a grant.

Canonical chat uses the existing `/api/agents/{agent_id}/chat/ws` route and the
same execution service and private admission carrier. It requires an existing
thread and a stable client request key; no native input or replacement session
is allocated by the WebSocket. The same backend must implement
`assert_chat_current(agent_id, thread_id, admission=principal,
operation="submit" | "observe" | "cancel")`, returning only `None` or raising.
This assertion enforces the current original selection, registered resource
kind and exact actor/action grants after its own waits. The actual invocation
still requires independent current model/native/DLP admission. Missing or
malformed authorization refuses canonical chat; local integer users and roles
cannot substitute for it. Every delivered event rechecks the original actor
and current resource access. A reconnect observes the original run without
rebinding or reopening it. Disconnect cancels observation only. Cancellation
uses the existing original-run command and does not acknowledge native cleanup.
Steer, device and delegated multi-target frames remain explicitly unavailable
until their own current-authority consumers are installed.

`await manager.start/stop/reload(agent_id, admission=principal)` retains the
same sealed human through the per-agent lifecycle lock. After the lock, the
manager revalidates the principal or recovers the exact held principal from the
current original queue invocation. Foreign selections, changed held principals,
Unknown occupancy, missing principals and metadata substitutes refuse before
provider dispatch. Each backend method requires the keyword `admission` and
still enforces the actual signed action ceiling and current exact grants after
its own waits. Managed bulk reload preserves the first refusal or Unknown.

Model writes use `await manager.update_thread_model(agent_id, thread_id,
model_or_none, admission=principal)` after the existing thread lock. The same
backend's `set_thread_model` and `clear_thread_model` are awaited and require
that principal; `None` selects clearing. The old synchronous setters retain
standalone behavior and explicitly refuse in managed mode. They do not schedule
a detached effect. Existing local-int HTTP and synchronous IM/slash consumers
must be adapted to the sealed ingress before using managed effects; these port
changes install no new credential channel or default human/IM actor.

`request_cancel` remains only a synchronous signal to the original invocation.
Its owner checks current admission and original native custody before a native
effect. That signal, a local close and transport completion are not joined native
writer/receiver completion or permission to replace an uncertain execution.

The existing durable queue still admits, deduplicates and records product tasks.
At execution its gateway skips legacy `registry.start` preparation for the
managed route. The provider must resolve fresh canonical/native grants after
queue waits. Product activity counters and thread locks coordinate local work;
they are not native fences or another session authority.

`CanonicalRunAdmissions(repo)` adds a human carrier to that same queue.
`submit(principal, user=resource_owner_projection, agent=..., thread=...,
operation=..., key=original_request_key, request=...)` persists only actor IDs
beside the original request. The separate resource owner is never inferred from
the actor. `reauthenticate(original_run_id, fresh_principal)` accepts only the
same actor and never-claimed queued request. Its original request/session
selectors remain unchanged. Running, approval-waiting and Unknown requests
cannot become a new attempt through reauthentication.

The private `reauthenticate_human(original_run_id, fresh_opaque_access_token,
resolver=bundle.requests)` bridge reads the original actor constraint from that
server-owned persisted row. It calls Owner5's nonallocating
`request_principal.reauthenticate` and checks the same original run/resource/
request across credential I/O before binding its new sealed principal. A fresh
JTI is allowed; authority/account/org/member/local projection must match, and
current role/scopes/revision come from fresh verification. Missing attribution
or projection refuses without allocation or a resource-owner fallback. The
credential remains private to that principal.

`build_app(server, **bundle.app_kwargs(), canonical_admissions=carrier)` mounts
`POST /api/executions/{run_id}/reauthenticate` only when the carrier, actual
ExecutionService share one queue repository and the server/resolver share its
exact Product pool, with the selected managed backend present. This is a
separate product keyword; the business bundle supplies eleven arguments. The route accepts the
existing single human Bearer header, verifies it without Product allocation
before reading any run, then calls the nonallocating bridge above. It returns
204 only after that original unsent request is bound. This response is not a
native input/delivery/cleanup acknowledgment. Success and errors use no-store;
no local sliding JWT is issued, no token/principal enters durable JSON and no
caller-supplied actor/resource metadata is used. Neighboring status/replay/
cancel routes keep their existing authentication until explicitly adapted.
Normal login supplies the fresh credential; the default launch still does not
install this carrier. Recovery UI, the remaining WS/IM ingress, exact resource
grants and the actual configured provider/factory remain incomplete.

`ExecutionService(..., human_admissions=carrier)` filters claims through live
process-local principals while preserving the existing database fairness and
capacity rules. Unadapted workers cannot claim canonical attribution-only rows.
After queue/capacity waits, `ActiveExecution.canonical_principal()` revalidates
the original carrier. `AgentManager` repeats that check after its thread lock
and passes the sealed object outside request JSON to the selected backend.
This is human authentication, not the required resource/model/native grant.
Native A2A, IM, cancellation, lifecycle/model changes, replay grants, actual
HTTP/WS consumers and final startup composition still need their exact adapters.

### Existing-session product projection

`ProductCohostSelections.record_existing(HostSelection(...), authority_id=...,
original_app_session_id=..., organization_id=..., owner_id=...)` is private
migration/composition I/O. It references already-created product rows, checks
their separate canonical target-owner projection, and reads the original
published binding before committing an immutable reference in the product DB.
It allocates no account, Agent, thread, native session or replacement ID.
Missing, foreign, deleted, host/k8s and unpublished cohost bindings refuse.
That refusal never removes their retained data from migration/backup coverage.

`current(actor, HostSelection(...))` implements Owner6's existing
`CurrentSelection` signature. It reads the product mapping, validates the current
actor's organization, rereads the exact original binding and checks both again.
Owner6's authority must still revalidate the sealed caller and apply independent
target entitlement, exact operation/model grants and native/DLP admission. An
administrator caller can select a different target only with those actual grants;
the product owner integer is never the caller identity.

The product-only table is included in the existing unreleased v30 migration and
its idempotent development repair. Referenced product rows cannot be physically
deleted through this mapping. Actual history/tombstone import and delete UX,
legacy host/k8s support, registered ordinary-versus-Moss resource-kind selection,
backup/rollback and final factory installation remain required. A stored tuple
or successful read does not resolve native Unknown or establish joined cleanup.

`request_cancel` only signals the provider. If native cancellation or cleanup
is unconfirmed, the provider must retain custody and raise rather than ending a
stream normally; the durable queue preserves `unknown` on that error. `aclose`
withdraws product resources without asserting native completion. Historical
transcript, team/A2A and full API/UX parity still need the actual provider and
joint acceptance. The dispatch regression uses an explicit backend fixture,
real SQLite queue and actual server boot; it makes no model/provider call.

## Original input terminal recording

`CohostAcpSession.attach(..., on_input_lifecycle=record_original_update)` accepts
an original-input recording provider separate from the current human/UI update
sink. `submit_input` refuses before dispatch if this provider is missing. The
callback receives the exact `HostBinding` and a private copy of an original
`committed`, `turn_complete` or `error` input notification. It must correlate the
original durable pending association, validate the authenticated native
connection/custody and current writer around I/O, and complete with `None` or
raise. A session ID or received update alone is not that authority.

Recording callbacks are serialized and awaited before the ordinary update
path. Current human grants still guard UI updates and new effects. Revocation
withdraws those effects while allowing later original terminal observations to
reach the independently authorized recording provider. No fresh human login,
context fetch, replay or new input ID is needed to record an original outcome.
An explicitly configured sink also receives terminal observations on an
existing attachment before any local submission; it must use the original
persisted association and custody. These observations create no pending-context
evidence and do not enable new input or issue an inventory snapshot.
After explicit local transport close, recovery remains the original owner's
responsibility; this consumer does not claim native joined completion.

Failed, malformed or cancelled recording, including cancellation while waiting
for the serialization lock, withdraws new effects and retains the
first controller-private `input_lifecycle_failure_receipt`, including the
original update copy and binding with outcome `unknown`. This is uncertainty
about recording, not a promoted commit/cleanup proof. It survives local close.
The actual durable provider and its revocation/restart reconciliation remain
required; this adapter creates no input table, ledger, event ID or public ACK.

The ordering remains active native input/batch, one validated context, durable
pending/context-served record, the same context text, native prompt persistence,
then original committed/completion observations. The pending recorder must not
wait for the later committed user row. No `snapshot` inventory request is
issued by this consumer; input actions require existing native admission.

## Explicit canonical monolith startup

The existing `run_foreground(..., canonical_configuration=configuration)` entry
accepts a trusted `CanonicalProductConfiguration`. The controller supplies the
original absolute SQLite path, persistent signing secret, effective phone
setting, actual writer/SMS/optional Feishu dependencies, configured managed
backend factory and separate Product executor identity/capacity. There is no request,
environment-mode or metadata selection and no default backend. This callable
does not supply the still-required production config/secret/resource/native
providers or deployment writer exclusion.

Before `open_database` constructs a pool or migrations initialize Product
schemas, startup verifies that the original canonical file already exists and
the selected Product file is physically separate, including resolved aliases,
hardlinks and both databases' WAL/SHM/journal paths. Every Product main/sidecar
path is checked against every protected canonical main/sidecar path. Filesystem observation errors
refuse. Deployment must preserve those mount/file identities; the check is not
a filesystem lease. This initial route supports one SQLite monolith without
reload. Distributed init/web/worker roles and multiple processes refuse before
their initialization paths run.

Startup constructs the maintained business bundle and captures one
`ExecutionRepo` instance for `CanonicalRunAdmissions` and `ExecutionService`.
The required `CanonicalManagedBackendFactory(CanonicalRuntimeInputs)` then
receives those exact business/services/execution instances, the original cohost
reader and `ProductCohostSelections`. It constructs the one managed backend;
its asynchronous connection/readiness belongs to the existing `boot()` method.
This ordering avoids creating another business authority before the Product
pool exists. A missing factory or malformed backend refuses. The original
resource-kind importer, actual grants, native/secret/recorder/review providers
remain required inside that configured provider and are not supplied by a tuple
reader. This is a required constructor port with no production default.
The same service is installed on the existing manager/gateway route; the launch
root passes its gate plus all eleven business adapters to `build_app`. An
omitted or swapped bundle/gate refuses instead of selecting standalone auth.
No Product local JWT or initial administrator is seeded. Native custody,
current exact grants and input-context recording remain the backend provider's
independent requirements; a configured object is not acceptance evidence.

Until their canonical consumers are installed, local setup/login/resource HTTP
paths return unavailable, and the local integer-user WebSocket decoder refuses
canonical mode before decoding a token. Canonical identity and original-run
reauthentication retain their existing strict credential checks. Legacy channel,
cron/proactive, communication and bridge auto-starts remain paused. This staged
callable therefore does not establish workspace, IM, nine-WS-path or background
job parity. A hot Product DB rebind refuses; a controlled restart must construct
the same verified composition again.

### Owner-bound typed file observations

A provider that needs the existing owner's typed Read must connect with
`required_operations=frozenset({"file.read"})`. The private capability probe
precedes configuration and PEM paths; current connection authority is checked
again after the probe and Ready. This metadata does not establish native Ready
or grant access to a file. Select the exact PR151 component by digest; the
same-version PR147/148/150 components cannot substitute for the Read surface.

`await sdk.read_file(original_trusted_path)` uses the same owner SDK connection
and checks current dispatch authority after capacity/lock waits and after I/O.
The provider must resolve that unchanged path from the original runtime and
resource binding. A read error or empty bytes cannot establish session absence,
clear an uncertain append, authorize replay or prove native cleanup. A mailbox
codec transcript is not an ordinary user/result JSONL transcript. These source
ports do not install the pending input writer, terminal finalizer or review
dispatcher.

Startup failures retain the first rejection/cancellation while attempting
controller-resource withdrawal and Product pool closure. A withdrawal failure
adds a sanitized private note. Shutdown does not assert that the original
native writer and receiver have joined, and cannot release Unknown custody.

## Verification

Run socket-level regressions with the locked workspace:

```sh
uv run --locked pytest tests/unit/infra/native_host -q
```

`tools/accept_cohost_acp.py --scode PATH --output NEW_DIRECTORY` runs a supplied
real SudoCode executable with an isolated home/config and no inherited API
credentials. It snapshots and hashes the executable, checks actual handshake
capabilities, and requires a real missing-session load error without creation.
It makes no model request. Its explicit fixture authority does not qualify
managed transport, native proof, an existing session resume or production.

The browser CI workflow protects the socket regressions. Real managed execution
acceptance additionally needs the selected, jointly qualified runtime artifacts
and host credentials; the missing-session CLI check cannot replace it.


### Deletion and retained history

Product deletion is unavailable for retained Agent/thread references and for
resources owned by a canonical Product user, including resources not yet mapped.
The normal delete routes return HTTP 409 `RESOURCE_RETAINED` before runtime,
workspace, checkpoint, session or trajectory cleanup. The dashboard keeps the
resource visible and displays the refusal. Offline Agent deletion and failed
creation cleanup preserve these resources too. Local user deletion also checks
before removing cached identity or directories and repeats the check in its SQL
transaction, so account cascades cannot bypass resource retention.

The check reads raw Product retention evidence without an access-reader join.
Disabled users, revoked grants, a changed original binding, or a reference that
committed before original revalidation failed cannot make history deletable.
Repositories repeat the check inside the delete transaction; the existing
reference foreign keys remain `RESTRICT`. No mapping is removed or retargeted.

This refusal is not a tombstone or successful deletion. There is no installed
canonical deletion/tombstone provider yet. Existing original tombstones,
unsupported host/k8s history, unpublished bindings and unrelated retained data
must survive the accepted migration/backup/rollback plan. Neither this policy
nor a reader refusal selects rows to import or discard. Native Stop, local close
and ended Product rows do not prove joined native cleanup.
