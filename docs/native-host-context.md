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

PC2's `SudographClient` consumes the following `HostPorts`. No complete production
provider is installed. The table distinguishes available source from missing
composition; a reference or a configured connection is not a grant.

| Domain port | Available source | Required composition |
| --- | --- | --- |
| `authorize(binding, operation)` | `TrustedHostContext.authorize/check_ready` and the original registered-binding reader. | Current authenticated actor and exact full authority/model/source/project/projection/action grants at I/O. Context access does not grant human review or model mutation. |
| `call_tool(binding, name, arguments)` | Existing connector registration and the private official SDK carrier. The SDK carrier is native Call, not a reviewed MCP client. | Reviewed descriptor/effect, registered worker, credentials and destination restrictions, with current grant checks around waits and service I/O. Preserve MCP error status and the original write operation ID; never replay an uncertain write. |
| `read_review(binding, request)` | Domain review-envelope and exact captured document/pointer validation. | Independent full-model human-read/profile authorization and reviewed registered-worker transport. Return the complete review-v2 envelope. This read does not accept comments or acknowledge delivery. |
| `audit(binding, operation, status)` | `RetainedModelAuditRepo.record_access` appends sanitized metadata to the original audit table. | Capture the authenticated original actor and registered binding, validate the domain binding, offload SQL and propagate audit failure. Install only after the single canonical writer handoff. |

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

The selected first replacement integration is owner-authenticated
`nexusd-cluster` with `spawn_spec scode`, strict ACP and `serve_managed` through
one `ManagedRuntimeContext`. Authenticated child bootstrap and activation are
required for that route. Current-host custody, exact load, managed I/O parity,
matched artifact checks and authoritative joined cleanup remain mandatory.

The existing owner-mTLS in-process cohost remains for current production and
safety repairs until replacement acceptance. Its historical reference is
cohost0.2.40 f5e2f3f / Nexus0.8.26 / ABI7 and Node VFS client0.8.1. These reference
pins and the consumer APIs below do not select or qualify replacement artifacts.

Root's exact-source review identified a shared native custody gap in the pinned
Nexus0.8.26: the hosted handle map is keyed by PID without retaining its original
`AgentInstance`. A replacement present before lookup can therefore be combined
with an old handle. The same map serves in-process cohost. Root's candidate
`d3706e61763671bb90703a623663087febaa5920` retains instance plus handle; a
qualified shared correction or equivalent and matched normal runtime build are
required before accepting the native current-host gate. This is a source
counterexample, not evidence of a production incident. Do not reproduce native
custody logic in Python or privately patch a live binary. The selected cluster
route separately requires authenticated child bootstrap and activation.

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
access, bound connector transport, audit composition and trusted comment ingress
remain required adapters; the reader does not implement them or register a new
project. Its tests use the actual public `server-v0.1.103` project schema and
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
`HostPorts.audit` consumer wiring remains to be supplied together with current
actor/model grants and reviewed service I/O. Audit failure after a model write
does not prove rollback: preserve the original write operation ID and uncertain
outcome, without replay or a success ACK. An access audit is separate from
durable review enqueue/dedup/ACK and authoritative native cleanup.

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

For AI tools, the actual native actor/session/run must come from the original
authority. The harness's `[thread_id, checkpoint_ns]` observation key is a local
coordination hint, not a native identity or grant. Browser control/target epochs
also remain separate from native incarnation and execution fences.

Do not serialize a `TrustedHostContext`, credentials or a claimed grant into
browser/tool/queued request metadata. Trusted execution composition reconstructs
the current canonical binding from its authoritative references. Human viewer
authorization and AI execution authorization consume the same identity source
through their respective existing resource grants.

## Original reviewed MCP resolution

`RetainedReviewedMcpRepo` reads the original `mcp_servers`, `mcp_policies`,
`agent_tool_reviews`, active account/member and department rows in one read-only
transaction. `ReviewedModelResolvers.resolve_tool(registered, operation)` returns
the approved `ToolConnection`, independently reviewed `ToolDescriptor`, and a
mandatory current assertion for the registered transport. `resolve_review`
requires the original `moss/host-review-profile` and returns the approved MCP
connection plus that assertion; the transport derives its fixed review endpoint.

Each current assertion rechecks the original registration, current member,
review revision/digests, policy, credential definition and resolved credential
values. The transport must run it after DNS/TLS and before HTTP bytes. It never
substitutes remote annotations, administrator role or LLM billing facts for the
separate full graph/action/native/DLP grant supplied through `check_access`.
Confirmation-required writes and task/skill-restricted host registrations refuse.

System secret references use the existing organization-scoped ConfigItem,
`org:{org}:system:{pinyin}` namespace and `system:moss:{org}` subject. An actual
authorized Nexus `listSecrets` capability is required for those values. The
session SDK has no corresponding secret API; session file Read and controller
credentials do not supply it. No fallback credential is constructed.

These resolvers do not install the product factory, create human authentication,
provide native effect fencing, or implement comment/input persistence. The
execution composition still supplies real current native/full-model assertions;
the independent human reader supplies its actual human/document/source grant.

After a model-apply reply, `RegisteredSudographPorts` withholds the result as
`MODEL_WRITE_UNCONFIRMED` if reply validation, snapshotting or current admission
fails. This preserves possible commit and recovery with the original operation
ID; it does not authorize replay or a replacement operation. Known compiler
`isError` results retain their classification, and cancellation stays task
cancellation. A subsequent audit failure retains the same recovery instruction.

`ToolResult` content is decoded separately by the domain client. Possible
non-error writes are now retained before decoding, so malformed content plus
audit failure preserves recovery with the original operation ID. The registered
transport consumes the current reviewed tuples and preserves uncertain HTTP/MCP
writes. These checks still require the actual grant, secret and native providers;
source qualification alone does not establish runtime acceptance.

Non-error apply replies pass through the existing `ToolResult.decode()` inside
this guard, so malformed payloads also retain possible-write recovery before
the result reaches the domain caller. The domain client's independent
pre-decode write latch remains in place for other HostPorts implementations.

## Independent human model review

`HumanReviewResolvers` and `HumanReviewReader` consume the existing sealed
`CanonicalAuthenticatedRequest` and original `RetainedOntologyProject`. They
reuse `RegisteredModelTransport` for review-v2 reads. No Agent or native session
is allocated, and all tool/context/model-write entries refuse. The principal
must come from the same normal canonical business composition; a principal ID,
account role, worker credential or LLM/billing fact is not a graph permission.

The required `HumanReviewAccess` callback checks the full `ModelBinding` and the
exact captured document/source/record request. Project facts and the original
human credential are revalidated around awaited I/O, including dispatch after
address selection/TLS and result release after audit. Supported reviewed page
arguments and current credential identity remain mandatory. Each read owns its
request snapshot, so concurrent requests cannot substitute the latest pointer.

`HumanReviewAudit` must use the actual canonical writer. The existing
`RetainedModelAuditRepo.record_project_access` appends only sanitized metadata to
`mcp_audit_log`, with the original member and SQL NULL session. Error audit must
remain available after human grant withdrawal; it grants no additional read.
Failed or malformed audit completion prevents result release. A cancelled read
remains cancelled even if its audit fails.

`HumanReviewReader.capture_review(endpoint, store, writer=...)` persists the
validated document before returning its unchanged HTML. The explicit writer
uses the existing `CanonicalWriterAdmission` contract. The human store uses the
same original canonical database and `ontology_review_values` table, with the
original project generation, capturing account/member, endpoint, pointer,
checksums and HTML. It creates no schema, session or new document identity.
Conflicting bytes at the same original tuple refuse instead of overwriting.

`restore_review(endpoint, store)` reads those exact bytes without contacting the
worker or selecting a new head. It checks current human/model authority,
project facts, stored provenance and domain checksums, then completes the
existing independent audit and checks access again before returning. Failed or
cancelled persistence can follow an actual commit; it releases no page and
never triggers an automatic refetch. Explicit recovery retains the same tuple.

These methods do not mount an HTTP route or publish a frame. The product
factory must install current human/model/projection/DLP grants, secret access
and canonical writer exclusion before private page serving. Original-session
comment provenance, durable enqueue/dedup/ACK and native cleanup remain
separate ports.
