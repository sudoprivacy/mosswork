# Trusted native host context

`octop.infra.native_host.context` implements the application consumer for current
host bindings and operation authorization. The production authority adapters,
native transport, application wiring and review dispatcher are still required.
The unit fixtures do not qualify a production grant, runtime or deployment.

## Callable interface

```python
host = await resolve_trusted_host(authenticated_principal, selection, authority)
await host.authorize(operation, registered_resource_ref)
await host.check_ready(operation, registered_resource_ref)
resolve_context = host.host_context_resolver(registered_resource_ref, read_context)
```

`HostSelection(agent_id, thread_id)` selects product resources. `HostAuthority`
resolves the current explicit mapping; a local numeric user ID, decoded personal
Moss reference or supplied owner ID does not authenticate a caller. Owner5's
personal Moss codec remains the identity codec; this module does not recreate it.

`HostBinding` keeps the original human account, selected organization, product
selection and native `CohostSessionBinding` distinct. The latter projects the
existing owner, native Agent, durable session and repository values. It is not a
new native wire schema. The original runtime handle/channel generation and host
incarnation stay with the injected `NativeHostPort`.

This consumer requires an already published durable binding. Fresh-session
startup must authenticate the current owner and establish the original managed
connection before opening remote storage, then bind the correlated new/load
result. Do not require a durable transcript ID as a prerequisite for obtaining
the credential needed to create that transcript. `HostAuthority.resolve` reads
an existing binding; it must not silently initialize a replacement session.

### Required injected adapters

| Adapter | Responsibility |
| --- | --- |
| `HostAuthority.resolve` | Authenticate and authorize the current selection before runtime/credential access; return the exact binding and one existing owner-authenticated runtime connection. |
| `HostAuthority.check_current` | Recheck current identity/membership, explicit revocation and the persisted product-to-native binding. |
| `HostAuthority.authorize` | Check the exact operation and registered resource against current grants. Model resources require the exact authority/model/source/projection binding, not just a rendering URL or context permission. |
| `NativeHostPort.check_current` | Check the original current owner/host incarnation, managed execution, authenticated endpoint and exact native durable binding. A controller-input epoch is insufficient. |
| `NativeHostPort.check_admission` | Consume shared native/DLP admission for that same allocation. Reuse `@sudo/egress-policy`, including applied status; do not implement policy inheritance again in Python. |

These interfaces require an implemented authority provider. There is no default
provider, credential issuer, guessed identity mapping or local/direct-model
fallback. Assertion ports complete with `None` or raise; boolean/JSON verdicts
are rejected, including `False`, rather than discarded as successful completion.
Provider failures refuse access. The actual dispatch must consume the
original native fence at the effect boundary; sequential application checks
cannot replace the native ownership or writer lease.

### Retained cohost authority composition

`infra/native_host/authority.py:RetainedCohostAuthority` implements the existing
`HostAuthority` interface for one request/attachment. Its required constructor
adapters are `current_actor`, `current_selection`, `require_owner_access`,
`require_operation` and `resolve_runtime`, plus the original
`RetainedCohostBindingRepo`. There are no default grant or native providers.

The actor adapter consumes Owner5's authenticated server carrier and returns
the snapshot from `await principal.revalidate()`. It must reject arbitrary
metadata, decoded claims or a service credential offered as a person. The
selection adapter reads the original product-to-application mapping without
credential access. The authority re-reads the real retained native binding and
checks the target owner's current membership/entitlement independently of the
caller's membership and exact action/resource grant.

Only then can `resolve_runtime` borrow a native port from its connection manager.
The authority repeats the checks after that await and before publishing the
binding. Its product operation label `session_connect` identifies this access
check; it is not a new native scope or credential. The grant adapter must resolve
it against the existing current authorization service for the original
application session. A changed actor, projection, native binding or grant
refuses the attempt. The same authority cannot retry resolution, including
after cancellation or an uncertain connection result.

`HostBinding.account_id` remains the actual human account. The original actor's
org-member ID and the native target owner remain separate. An administrator
caller needs an independent target-owner/action grant; its role alone does not
authorize another owner's Agent or browser. Current checks use only the
original authenticated carrier and selected binding for the life of this
attachment. Missing current evidence refuses access; no session or identity is
reconstructed from a display name or local integer.

The snapshot's account ID comes from the original account-membership join.
Native `/auth/me`, organization listings and zone membership alone cannot supply
that account's active status and Moss entitlement. Auth composition must select
the native issuer/origin rather than the numeric legacy-login alias. Credential
JTI is used for revocation checks; it cannot become an authentication SID,
native Agent or durable transcript ID.

Feishu dispatch uses Owner5's trusted live connection and sealed claim path,
including application, tenant, sender type and stable sender IDs. Retaining
those SDK fields does not authenticate arbitrary body metadata or make an IM
actor a human `CanonicalAuthenticatedRequest`. Reuse its current account/member
and target services rather than issuing a target login or copying an identity.

This implementation establishes consumer ordering and retained-binding checks.
Production adapters for the sealed caller, product selection, target access,
exact grants and native port still need joint composition. Tests use real
canonical/retained SQLite schemas and isolated verifier/grant/native fixtures.
They do not qualify a production caller or native writer. The connection
manager retains its attachment cleanup contract; this borrowing authority never
kills a shared writer or claims native cleanup after a local refusal.

`check_ready` rechecks current binding and grants around admission I/O. It never
caches a Ready flag. Its success is neither an operation receipt nor proof of a
stopped writer. A lost launch, enqueue, cancel or cleanup outcome remains Unknown
under the original native ownership until authoritative reconciliation.

## Existing live context protocol

The reference is archived Moss `server-v0.1.103`, source
`d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d`:

- `sessionManager.ts:BackendHostContextResolver`
- `backends/acpBridge.ts:_sudocode/session/host_context`
- `ontology/projectRegistry.ts:runnerContext/contextReader`

The host-bound resolver accepts `{sessionId, nativeSessionId}` and returns current
text for the existing `{appendSystemPrompt}` response. It checks both exact
session IDs and reauthorizes before and after domain I/O. It does not cache
context. Revoked access, changed bindings, withdrawn context, missing current
host or reader failure cannot return the previous text.

The bridge must still require the engine's `hostContext` capability version1 and
recheck the live ACP session/input epoch around the callback. This module does
not implement that bridge or its handshake. PC2's domain reader owns the current
registered model envelope and context validation. Model input never substitutes
for authorization or tool enforcement.

`host.close()` withdraws this application context immediately. It deliberately
does not cancel an execution or claim native cleanup succeeded; the original
transport/lifecycle owner must provide that evidence.

## Existing cohost snapshot adapter

`octop.infra.native_host.cohost.CohostRuntimeHost` implements the snapshot part
of `NativeHostPort` using the existing SDK `call(method, payload, auth_token)`
contract. Trusted composition injects the existing owner-mTLS connection;
requests always use an empty bearer token so deployment credentials cannot
override the owner. This module has no connection constructor or credential
issuer and does not implement the ACP message transport.

`PublishedCohostExecution(process_id, endpoint, workspace_path)` captures the
original published registry handle, `acp-mailbox/1` endpoint and procfs workspace.
The durable session remains in `HostBinding.native`. The public procfs workspace
path is different from the persisted canonical repository locator; neither may
be inferred from the other.

`check_current` calls `managed_agent.get_session_v1` once, checks the original
owner/Agent/durable ID/workspace/mailbox and a live state, and rechecks the
injected original execution proof around the RPC. Missing fields, malformed
replies, RPC failures, retired/initializing state and changed endpoints refuse.
There is no cached success, retry, resume-to-new or implicit cancellation.

The native execution proof callback is mandatory. It must consume the existing
authority's original host incarnation, custody, owner/connection and prepared
durable binding before attachment. The subsequent exact ACP load and qualified
managed I/O are additional requirements before prompt effects; a pending load
must not be required to authorize itself. Public snapshot equality does not
establish these facts.
`check_admission` similarly requires the injected shared policy check for this
exact execution and rechecks the original native state afterward. The actual
SDK connection is described below; the execution-proof callback and shared JS
policy binding still require their production providers. Snapshot fixture tests
are wire-contract checks rather than live runtime acceptance.

### Controller SDK connection

`octop.infra.native_host.sdk.ControllerSdkConnection` implements the existing
`call(method, payload, auth_token)` port through the shared
`@sudo/session-runtime` private controller RPC. Trusted composition supplies
verified Node/package artifact paths, the original canonical owner and existing
issuer PEM paths/placement. The Node component is part of the controller;
credentials remain separate from managed Agent/engine processes.

`connect(command, configuration, before_connect=..., before_dispatch=...)`
requires implemented assertion providers. It checks current connection access
before process/credential access and again after minting. Dispatch assertions
run after capacity/lock waits, immediately before the private write. Success
requires None; a boolean/JSON decision is refused. These sequential app checks
do not replace the native service's atomic effect/ownership fence.

Each connection fixes the owner/issuer/placement. Runtime calls use an empty
bearer. Node injection settings and inherited application credentials are
excluded from its process environment. Reply framing/correlation and pending
calls are bounded; timeout/cancellation after submission keeps an Unknown
effect and does not replay it. Exception text exposes stable codes; the
controller-only `native_failure` preserves an original structured verdict for
precise recovery and must not enter public errors or logs.

The private SDK ready frame is not native execution readiness. Closing this
connection releases its own IPC/renewal resources and does not prove that a
native task stopped. This adapter does not provide ACP/message transport,
product identity or grants, runtime provisioning, DLP policy evaluation or
trusted review ingress. PC3's existing ACP/product consumer remains the single
integration point for those corresponding providers.

## Composition and remaining integration

Owner6 owns this backend composition and native/DLP integration. PC3 owns browser
and frontend integration; shared manager/server/chat/browser edits need the
agreed single injection seam. Owner5 supplies current business principal,
membership and entitlement adapters. PC2 supplies domain model/context/review
semantics, consuming current host grants for the full registered model. Human
review requires its own read/write grants and trusted actor/event provenance.

### Registered domain ports

`native_host/sudograph.py:RegisteredSudographPorts` implements PC2's `HostPorts`
against the original cohost/model repositories and canonical audit. It takes
the resolved host, exact original session and model registration, original
authenticated actor member, mandatory full-model grant assertion and reviewed
transport. No complete production provider is installed. A reference or a
configured connection is not a grant.

| Domain port | Available source | Required composition |
| --- | --- | --- |
| `authorize(binding, operation)` | Exact tuple comparison, original session/model SQL rereads, current host authorization and mandatory full-model assertion around waits. | Current authenticated actor and full authority/model/source/project/projection/action grant service. Context access does not grant human review or model mutation. |
| `call_tool(binding, name, arguments)` | Request snapshot before awaits, full authorization and native/DLP admission at transport I/O, then original binding/grant recheck. MCP error status and original write operation ID are retained. | Reviewed descriptor/effect, registered worker, credentials and destination restrictions through `RegisteredModelTransport`. The private SDK carrier is native Call, not this MCP transport. |
| `read_review(binding, request)` | Exact original resource, validated request and full review-v2 envelope; independent `review` authorization before and after transport. | Full-model human-read/profile grant and reviewed registered-worker transport. This read does not accept comments or acknowledge delivery. |
| `audit(binding, operation, status)` | Exact bound model and captured original actor; offloaded `RetainedModelAuditRepo.record_access`, committed or raised. Denied/stale attempts remain auditable. | The factory must capture the actual authenticated actor member and install the single canonical writer. No request/result/comment payload is recorded. |

The trusted factory constructs the ports with the existing repositories and
services; `ports.binding` is derived from the unchanged original registration,
not browser arguments. It then constructs
`SudographClient(ports.binding, ports)`. Every grant call receives the original
`HostBinding`, actor member, complete `RetainedOntologyBinding`, exact domain
`ModelBinding` and operation. Generic `resource_ref` permission cannot satisfy
this full-model assertion. Missing assertions/transports have no default.

For live context, use
`resolve_context = ports.host_context_resolver(record_context)` in PC3's existing
ACP injection. This reads and validates one compiler response, captures its
immutable `ModelPointer(revision, context_digest)` and text, records that pointer
against the original active input lifecycle, rechecks authorization, then returns
that same text through `TrustedHostContext.host_context_resolver`. The mandatory
`ContextRecorder(host, registered, pointer)` is supplied by the existing input/run
owner; the adapter does not create a new lifecycle store or invent a run ID.
No rendered-text digest, cached context or latest-revision fallback is used.
PC2's existing `client.host_context` remains the simple text callback; passing it
alone does not record the pointer lifecycle required here.

Grant, transport and context-recorder production providers still need the joint
PC3 factory. These checks wrap real service I/O but do not replace its atomic
grant/native effect fences. A model-write or audit failure never implies that
an already committed operation was rolled back or may be replayed.

The canonical request carrier is Owner5's sealed
`CanonicalAuthenticatedRequest`; `revalidate()` checks the original credential
and identity. Its `identity.snapshot.member.owner_id` is the original actor's
member ID, distinct from account ID, product integer and selected target owner.
That producer does not itself grant model/native effects. Current WebSocket
authentication and durable post-queue actor rehydration still require the joint
adapter; the private principal and credential cannot enter queue metadata.

PC3's existing `ManagedExecutionBackend` constructor/manager/queue path remains
the single product consumer. Native events need projection into the existing
product chunk grammar; raw ACP updates are not already compatible. Native
uncertainty must raise so the original durable queue keeps custody. The selected
native branch bypasses harness middleware, so its browser tools separately need
the public Worker's current resource/effect authorization. Browser control epochs
are not native incarnation or grants. Coordinate changes to the existing
server/manager/chat/gateway hooks with PC3.

Owner6 also owns composing durable original-session review enqueue/dedup/ACK
through the existing authoritative session ingress. That dispatcher and trusted
review admission are not implemented by `host.authorize`; a successful generic
grant check must not be reported as accepted review delivery. PC2 retains target
and revision mapping. Root/W7 retain native event/session authority. No second
review queue or replacement session is introduced.

The first integration route remains owner-mTLS nexusd-cohost with in-process
SudoCode. Reference runtime: cohost0.2.40 f5e2f3f / Nexus0.8.26 / ABI7; its Moss
consumer pins the Node VFS client0.8.1. New raw-child bootstrap is conditional on
selecting that engine route. Current-host, exact load, managed I/O parity, artifact
checks and cleanup acceptance remain mandatory for the selected route.

Root's exact-source review identified a shared native custody gap in the pinned
Nexus0.8.26: the hosted handle map is keyed by PID without retaining its original
`AgentInstance`. A replacement present before lookup can therefore be combined
with an old handle. The same map serves in-process cohost. Root's candidate
`d3706e61763671bb90703a623663087febaa5920` retains instance plus handle; a
qualified shared correction or equivalent and matched normal runtime build are
required before accepting the native current-host gate. This is a source
counterexample, not evidence of a production incident. Do not reproduce native
custody logic in Python or privately patch a live binary. The new raw-child
bootstrap remains conditional on selecting that engine route.

Root's selected-source review also found a separate cleanup completion gap.
`SpawnHandle.abort` requests stopping; the cohost's worker join and receiver
shutdown failures do not become an authoritative completion result. A cancel
reply, registry absence or `Closed` mailbox event therefore cannot prove that
the old writer stopped. Root/W7 own the shared completion correction; no public
completion RPC/type has been delivered by that review. Until the native outcome
is confirmed, product cancel/close/recovery must retain Unknown and the original
exclusive custody, and refuse replacement. Do not invent a success token in
this consumer. Native `AgentInstance` equality remains inside the qualified
native service; it is not a new Python or public wire incarnation field.

SW3 owns the sole Tencent production cutover and rollback. This consumer source
does not qualify a candidate, migrate data, change current production or complete
Edge/Private/customer acceptance.

## Retained durable bindings

`infra/db/repos/retained_cohost.py:RetainedCohostBindingRepo` reads the original
SQLite `sessions`, `cohost_sessions` and owner `users` records with `mode=ro`.
It accepts the explicit original application session, organization and owner
selected by trusted projection. `get_for_resume` returns their published native
Agent, durable conversation and repository path; `assert_current` reads again
and refuses any changed binding. Call this synchronous repository off the async
event loop. It never initializes a database, publishes a binding or starts an
execution.

Missing bindings, deleted rows, disabled owners, changed organizations and
host/k8s rows refuse cohost resume. Application transcript IDs and native durable
IDs remain distinct. The validation follows archived `server-v0.1.103`
`runtime/cohostSessionRepository.ts`; tests use its actual public schema with
synthetic rows. This is not acceptance against a production backup or an
implementation of the product thread projection/importer. Fresh canonical
actor/resource grants, original native custody, exact load and confirmed
cleanup remain required before using a retained binding for effects.

## Original registered model resolution

`infra/db/repos/retained_ontology.py:RetainedOntologyBindingRepo` reads the
existing `ontology_projects` and `ontology_session_bindings` together with the
original session and active owner. Its synchronous
`resolve_original(application_session_id, organization_id=..., owner_id=...)`
returns the registered authority, worker, model, complete source set, project,
binding ID and registry generation. `assert_current(original)` re-reads all
these facts and refuses a changed binding, including disable/re-enable.
Call this repository off the async event loop.

The original human-review transport uses `project_id` as its rendering resource;
the returned `resource_ref` property preserves that identity. For PC2's domain
client, these exact values supply
`ModelBinding(authority_ref, model_ref, resource_ref, source_refs)`; the separate
registered `worker_ref` selects trusted transport. Incompatible reference formats
must refuse at the domain boundary. No path/URL, latest-project guess or local
numeric user ID may replace a registered reference. The registry generation is
an operator lifecycle fence, separate from the native host incarnation.

This lookup provides binding facts. `HostPorts.authorize` still needs the current
authenticated actor and full model/source/resource/operation grants. Context
permission is insufficient for human review or model mutation. Reviewed MCP
access, bound connector transport, production actor/writer installation and
trusted comment ingress remain required adapters. `RegisteredSudographPorts`
composes this reader and audit, without registering a new project. Reader tests
use the actual public `server-v0.1.103` project schema and
synthetic records, without production-store acceptance.

## Canonical model access audit

`infra/db/repos/retained_model_audit.py:RetainedModelAuditRepo.record_access`
appends to the original `mcp_audit_log` table. It accepts the host-resolved
`RetainedOntologyBinding`, the original authenticated actor's member ID, one
domain operation and `success`/`error`. The actor may differ from the selected
owner. The API has no request, response, comment, model, credential or diagnostic
payload. It retains the existing context/review action names; contract and apply
use `ontology.model.contract` and `ontology.model.apply` respectively.

The explicit absolute database must already exist. No table, fallback database,
schema migration or audit service is created. Each append commits with SQLite
`synchronous=FULL`, preserves its existing journal mode and either returns
`None` or raises a sanitized `ModelAuditUnavailable`. Async composition must
offload the call and propagate failure through `HostPorts.audit`. Do not check
current access before recording a denied attempt; original binding facts select
the audit metadata and do not authorize the attempted operation.

Deployment must first hand over the single canonical writer. This source does
not acquire that role or attach the repository to a live database. The actual
`RegisteredSudographPorts.audit` consumer offloads this repository with its
original binding and captured actor; production installation still requires
current actor/model grants and reviewed service I/O. Audit failure after a model write
does not prove rollback: preserve the original write operation ID and uncertain
outcome, without replay or a success ACK. An access audit is separate from
durable review enqueue/dedup/ACK and authoritative native cleanup.

The maintained domain client preserves the original `CancelledError` and task
cancellation state if required audit fails, adding only a sanitized audit note.
Cancellation remains cancellation even after possible model submission; it
does not establish rollback or native cleanup. The original operation ID and
owning queue/session remain authoritative for uncertain effects.

Tests use the unchanged published audit table/index schema and synthetic rows.
They verify committed metadata from a fresh connection, distinct actor/owner,
concurrent appends, refusal of missing stores/schema, foreign-key and insertion
failures, and omission of content. They do not qualify production durability,
database migration or the full domain host composition.

## Attachment and effect readiness

The original native preparation and the product ACP handshake are separate
steps. The injected execution proof first establishes the actual original
host/custody, owner/endpoint and prepared durable binding. It must not require
the product's pending `initialize` or `session/load` to have completed before
authorizing that attachment. A fixture callback or local ready flag cannot
substitute for the native proof.

The snapshot adapter requires an already published durable ID and active native
state. It supports the selected in-process cohost resume route. It cannot admit
a raw UNBOUND execution before load by weakening the guard: a later selected
raw route needs its separate authorized preparation phase before durable binding
is published. Authenticated child bootstrap remains conditional on that route.

PC3's ACP consumer then initializes the required capabilities and loads the
exact original durable session with its published execution workspace, checking
current grants and admission around awaits. Only after this handshake succeeds
may prompt/context effects proceed with current native authority/admission and
qualified I/O parity. `check_ready("session_load", ...)` is the admission check
for that attachment operation, not evidence that the future load has completed.
Ordinary turn Stop, transport close and authoritative joined native cleanup
remain distinct. This ordering does not introduce a new native phase token or
public credential/fence schema.

## Browser integration call sites

The PC3 worktree currently owns the following hooks. This table identifies the
intended integration boundary; the native wiring is not implemented here.

| Existing source and callable | Joint boundary |
| --- | --- |
| `api/routers/chat/turn.py:_prepare_dashboard_turn` and `build_dashboard_inbound` | Preserve the host-captured browser control epoch. Resolve the authenticated canonical caller and current thread grants; queued metadata cannot assert an owner, native session or grant. |
| `infra/gateway/process/processor.py:_build_dashboard_request` | Current code calls `resume_user_browser` before model dispatch. Reauthorize the selected original host at actual execution, including after queue waits, before allowing browser continuation. |
| `infra/agents/manager.py:AgentManager.stream/call/resume_hitl` | Owner6 supplies the single selected native execution adapter. Native deployments must not silently enter the harness/direct-model path. Preserve PC3 browser hooks while agreeing this injection point. |
| `infra/agents/middleware/browser_profile.py:BrowserProfileMiddleware.awrap_tool_call` | Current LangGraph-only path overwrites profile selection and calls `ProfileControl.run_ai`. It is not automatically executed by cohost's SudoCode engine. The native browser tool/Worker bridge must consume the same trusted host authorization. |
| `infra/browser/control.py:ProfileControl.run_ai` | PC3 retains the public core input barrier, observation and target epochs. The proposed native authorization callback belongs inside the admitted operation, immediately before browser effects, including after barrier waits. The callback and native-tool entry still require joint implementation. |
| `api/routers/browser/stream.py:run_browser_stream_session` and `browser/harness.py` | Human viewer/input needs current human-to-canonical-profile authorization. Opening a viewer does not require creating an Agent or durable chat session; do not force the execution-bound `TrustedHostContext` onto that human-only journey. The existing local integer profile key remains a projection. |
| `api/routers/chat/history.py:cancel_thread` and `infra/gateway/gateway.py:cancel_dashboard_turn` | Preserve authenticated thread cancellation and browser pause. Pausing browser input, acknowledging a cancel request, withdrawing a context and proving native writer termination are separate results. |
| `infra/server.py` startup/shutdown | Agree one native adapter injection and cleanup ordering with PC3. Keep its profile controls and input-recovery evidence; controller shutdown cannot stand in for authoritative native cleanup. |

### Execution-bound browser admission

`native_host/browser.py:CohostBrowserAdmission` supplies the current execution
consumer. Its constructor takes the existing trusted host, retained cohost
binding/repository and mandatory current-actor, exact-action, allocation and
existing-Worker resolver callbacks. It preserves the original personal browser
rule: same current canonical organization and member, active membership and
the same human account as the trusted host. An administrator inspecting another
member's conversation cannot borrow that browser profile.

`prepare(parsed_action)` snapshots the exact JSON before awaits, checks original
cohost/current actor, exact action/resource and native admission, and verifies
the registered allocation before resolving private credentials. It rechecks
after resolving the existing `BrowserWorkerClient`. The returned
`ResolvedBrowserAction` holds that client, fixed binding/controller, a fresh
action copy and `before_effect()` for PC3's actual Worker barrier. Preparation
does not send HTTP, start Chrome, rebind or dispatch native cancellation.

The Worker `sessionId` is the retained original **application** session. Product
thread and native durable IDs stay separate. `browser_controller` preserves
the original `user-` plus SHA256 of JSON `[orgId,userId]`; it never substitutes a
local user integer or native mailbox controller. Tests freeze five outputs by
executing the original archived TypeScript function body in Node.

The exact-action provider is an assertion, not a control dispatcher: it may be
called repeatedly and must not send cancel/take/return, create a run or mutate
native custody. It must consume the actual original actor/input/run admission.
Browser `take` drains control; `stop` closes the browser. `return` requires the
fresh admitted normal user turn. A Worker action/epoch, transport reconnect or
old queued message cannot establish those native facts. Native uncertain
outcomes and authoritative joined cleanup remain with the original executor.

The PC3 private Worker transport is reused unchanged. Its real Worker/Chrome
receipts cover both headless and native headful transport with fixture grants.
They do not qualify these new providers. The public Worker operation/lifecycle
hook, including missing-guard refusal and epoch rechecks, is published in
0.58.22 and consumed by PC3. The actual authority embedding still needs to call
`before_effect` after its lock wait. Preparation before HTTP alone does not fence remote effects. The
callback is an internal embedding interface, not a new native wire token or a
second browser service. Production providers and the single factory remain
uninstalled.

Human-only viewer/input must retain its existing current personal-profile path
without constructing this execution-bound class or creating an Agent/session.
The private client and Worker window-stream credentials also require the
existing host-owned public projection; they cannot be returned as a serialized
`ResolvedBrowserAction`.

For AI tools, the actual native actor/session/run must come from the original
authority. The harness's `[thread_id, checkpoint_ns]` observation key is a local
coordination hint, not a native identity or grant. Browser control/target epochs
also remain separate from native incarnation and execution fences.

Do not serialize a `TrustedHostContext`, credentials or a claimed grant into
browser/tool/queued request metadata. Trusted execution composition reconstructs
the current canonical binding from its authoritative references. Human viewer
authorization and AI execution authorization consume the same identity source
through their respective existing resource grants.

## Persisting the exact served review

`RegisteredSudographPorts.capture_review(endpoint, store)` obtains one validated
compiler document through the registered transport, then commits its complete
record and unchanged HTML before returning. PC3 must serve the returned document
only after this method succeeds. The private endpoint comes from the existing
trusted page grant; neither a browser-selected endpoint nor a document checksum
establishes that grant.

`RetainedReviewDocumentRepo` reuses the original canonical
`ontology_review_values` table with `served_review/v1:` keys. The immutable value
retains the original application/owner/Agent/durable-session/repository tuple,
the complete registered model/source/worker/generation tuple, the capturing
human account and member, and the domain document record plus HTML. Reusing an
endpoint with different content or provenance refuses rather than overwriting.
The constructor requires the same existing canonical SQLite database and writer
role as the other retained repositories; it never initializes a schema/store.

`RegisteredSudographPorts.restore_review(endpoint, store)` rechecks current
full-review grants and original bindings around the offloaded SQL read, applies
`ReviewDocument.restore` to the exact stored bytes, and commits a sanitized
canonical access audit before returning the frame. It never asks the worker for
head/context or fills missing legacy digests. Stored capturing-account/member
values preserve provenance; they do not grant the current reader access.

A cancelled persistence wait can leave its original SQL commit intact. No frame
is returned, no comment ACK is issued and no compiler effect is retried. Recover
that same endpoint under fresh grants. The existing rollback/backup manifest
must retain these rows with the original review inbox, claims, grants and native
history; restoring an older revision-only consumer is not proven compatible.

The actual page handler/private grant store, registered worker/grant providers,
single canonical writer takeover, authenticated comment provenance and original
native-session delivery/dedup/ACK still require joint installation and acceptance.
This persistence adapter creates no new comment dispatcher or session authority.

## Official mailbox and public ACP streams

Sudostack PR150 adds session operations to the existing private controller
carrier. `ControllerSdkConnection.session_operation(open|send|receive|close,
payload)` uses the same original owner-delegated connection, with no credential
override. Its dispatch assertion receives `controller.session.<operation>` as a
private adapter label, never a native RPC or grant. Outbound operations serialize
through the previous append acknowledgment, then check the current assertion.
An uncertain or cancelled open/send withdraws later sends even if a late reply
arrives. Ordinary original native observation remains separate.

The shared component uses official SDK0.8.1 `NexusSessionTransport`, keeping its
codec, participant/sequence checks, transcript cursor and same-cursor read
recovery. It opens one endpoint once, requiring its controller to equal this
same connection's delegated subject. A new credential cannot adopt another
live controller's attachment. Unread messages and active calls remain bounded;
write failure never causes an append retry or new endpoint/session fallback.

`ControllerMailboxStreams.attach(connection, original_endpoint,
check_attachment=...)` borrows that same carrier and returns public asyncio
`reader` and `writer` for PC3's `CohostAcpSession.attach`. A private socketpair
frames JSON lines for the official ACP parser. It has no listener, fd-native
identity or engine process. No private ACP method or copied mailbox codec is
used. The mandatory attachment assertion runs before and after open; it checks
original prepared owner/placement/host/binding, not the future ACP load.

An empty private receive poll is not EOF/input completion. Structured ACP
errors/data pass unchanged to the official parser. Local socket drain only
confirms local buffering; actual append/input completion still requires original
native acknowledgment. Malformed/lost replies withdraw the original streams
and preserve Unknown. `close()` closes this framing/transport, while the manager
retains the borrowed connection and authoritative native cleanup responsibilities.

Use the matched PR150 compiled controller package. PR147 connection-only and
PR148 Call-only roots, although also version0.1.0, lack this typed surface.
Select by source/artifact digest, including the installed dependency closure,
not the package version. `tools/accept_controller_mailbox.py` performs a bounded
model-free fixture check against an explicitly supplied installed package;
its local mTLS issuer and responses establish no real native grant/model pass.

### Typed managed-file observation

PR151 adds official SDK `Read` on that same owner connection. Select its actual
compiled component by digest; older version0.1.0 packages lack this operation.
`ControllerSdkConnection.connect(..., required_operations=frozenset({"file.read"}))`
probes private component operations before sending owner/configuration/PEM paths,
then rechecks the mandatory current connection assertion before bootstrap.
Missing/malformed capabilities refuse; this metadata does not prove native Ready,
load, cleanup, a grant or an original input outcome.

`await connection.read_file(original_trusted_path)` returns exact bytes, including
an empty file, using official typed Read and empty native bearer. The existing
dispatch assertion receives `("controller.file.read", unchanged_path)` after local
capacity/lock waits and again after I/O before exposing bytes. The actual provider
must resolve and authorize the original managed path and current resource/session;
caller paths, component capabilities and successful reads do not establish that
authority. Raw data remains bounded to4MiB and private frames to8MiB, with the same
16 pending-operation limit and original correlation. No read retry, generic Call
alias, token/owner/endpoint override or replacement session is introduced.

Read failure, malformed/oversized bytes and lost replies preserve uncertainty.
An empty file does not mean a missing session. Reading original evidence does not
clear an uncertain append, authorize a new send or prove writer/receiver cleanup.
The updated model-free acceptance entry uses the matched component for actual
Python/Node/SDK/typed gRPC mTLS, retaining fixture authority attribution.

The selected in-process cohost still requires the qualified original-instance
handle custody correction and separate authoritative joined writer/receiver
cleanup in its actual matched artifact. No extra public incarnation RPC/token
is supplied or invented by these adapters. Ordinary Stop, local stream close
and registry absence cannot qualify native completion or replacement admission.

## Remaining composition distinctions

PC3 owns the product Agent/thread-to-original-resource/kind projection/importer
and opaque HTTP/WS/queue admission consumer. Owner6 verifies that current
selection against original canonical/native data and supplies actual exact
action/resource/model/native/secret providers. Owner5 maintains the single
configured canonical business bundle. Product projection and facts are not
grants. Ordinary Agent owners do not universally need personal-Moss entitlement;
the trusted registered resource kind selects the appropriate current policy.
The strict personal-Moss getters remain strict, never a request-selected bypass.

Execution-bound `RegisteredSudographPorts` requires native admission for its
original host. It does not qualify standalone human graph/record review while
execution is absent. That human-only reader must use current full model/source/
document grants without creating an Agent/transcript or borrowing an execution
Ready result. Its actual factory/provider remains a distinct unfinished seam.
