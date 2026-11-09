# Sudograph domain client

`octop.infra.sudograph.client.SudographClient` consumes the existing compiler
worker's four MCP tools and `sudograph-worker-review/v2` review transport. It
checks the exact authority, model, source set and rendering resource on context
and review envelopes. Context revision and semantic digest remain distinct;
record reads retain the existing 100-row bound. Model writes preserve the
caller's operation ID and compiler repair diagnostics without automatic retries.

The host constructs an immutable `ModelBinding` from its registered project.
These references are not authorization. A required `HostPorts` implementation
supplies current caller authorization, bound tool/review transport and audit.
It must use the shared organization/user/agent/session/run authority, check the
specific operation and projection before and after an operation, and reject
changed bindings or revoked access. The domain module does not infer a native
owner from a numeric Mosswork user ID, mint credentials, create sessions, expose
a route or select a worker URL from request text. Transport, credential renewal
and network policy remain host responsibilities.
Authorization and audit assertion ports must complete with `None` or raise;
boolean/JSON results are refused, including `False`. Returning a verdict does
not satisfy this assertion contract.

`host_context()` returns fresh checked context text for the existing
`BackendHostContextResolver` / `hostContext v1` callback. It adds no procedural
system prompt and caches no text. The trusted host must fence the exact logical
and native session before and after the callback; the bridge owns the
`appendSystemPrompt` RPC envelope. Engine capability, current owner mTLS,
durable load and native cleanup remain host/runtime checks. The first selected
cohost path does not require a new child bootstrap; a selected child engine does.

Human review requires its own full-model read authorization. Context access is
insufficient. The client inserts the rendering resource from the binding,
rejects caller-selected resources/actors/paths, and returns the checked review
envelope. It does not implement arbitrary export operations or source writes.
Any narrower field/row scope must be provided by the authorized registered
projection; this full-model worker is not a production row-policy engine.

`resolve_review_target(ModelPointer(revision, context_digest), target_id)` reads
the exact compiler node after host admission of a comment. It preserves the
comment's original revision, checks the returned stable identity and resolves
objects, fields and business definitions with the existing exact-read tool.
It reads no records and never searches translated labels or falls back to the
latest revision. Relation/document anchors are not covered by this node helper;
they require their own compiler read contract. The helper authenticates no
comment actor and creates no event, session, queue, deduplication claim or ACK.
The host must authorize comment ingress separately from the node read.

`review_document(endpoint)` captures a `ReviewDocument` containing the HTML and
revision/digest from one compiler document response. Document responses missing
either pointer value or HTML fail before successful audit; the client never
reads a separate latest head to fill them in. `record()` serializes the bound
model/source/resource, pointer, endpoint and exact UTF-8 HTML hash, with a
checksum over the metadata. `ReviewDocument.restore(binding, record, html)`
requires the complete original record and unchanged HTML; inconsistent records,
legacy revision-only metadata and cross-model recovery refuse. These checksums
detect inconsistent bytes and are not actor authentication or access grants.

The host must persist this record and HTML (or its immutable content reference)
with the existing original-session binding before serving the document. On
restart, migration or rollback it must recover that stored binding rather than
derive one from the current conversation or current model. This domain helper
does not implement the production storage adapter, authenticate stored metadata,
or supply a missing session identity. The recovery tests use a temporary SQLite
fixture and actual compiler output; production served-pointer persistence and
trusted review delivery still require the host's persistence and ingress wiring.

The client refuses results if post-authorization or audit fails. That does not
undo an already committed model write. A lost/cancelled write response must be
reconciled with its original operation ID; the client never retries, invents a
replacement ID or cancels another conversation. Compiler diagnostics retain
their repair guidance. Unexpected transport errors omit upstream exception
text. The HTTP/product adapter must map domain codes through the existing
localized error layer, rather than display private transport exceptions.
Cancellation stays observable as `CancelledError` even when the audit also
fails. In that case the exception carries a fixed `AUDIT_UNAVAILABLE` note;
the host must retain that failure evidence without reporting successful audit
or replaying the original operation.

## Qualification boundary

The module is not wired into a production host. The accepted trusted caller,
grant adapter and durable existing-session comment ingress are still required.
It does not qualify real model execution, customer access, durable audit/inbox,
phone interaction or rollout/rollback.

Unit tests cover cross-model/profile/source rejection, pre/post authorization,
audit failure, caller mutation during an await, compiler diagnostics and
uncertain-write/cancellation behavior. The explicit installed-artifact test
uses the real MCP loader, compiler and worker for context/live rows, model
edits, same-operation retry, review HTML and refusal/revocation behavior. Its
grant and in-memory audit are fixtures, not production acceptance.

The Windows/Linux CI assembly supplies verified worker/compiler paths through
`MOSSWORK_TEST_SUDOGRAPH_WORKER`, `MOSSWORK_TEST_SUDOGRAPH_WORKER_SHA256` and
`MOSSWORK_TEST_SUDOGRAPH_COMPILER`. The test fetches no artifacts and skips only
when neither path is supplied; partial/mismatched configuration fails. The
optional `MOSSWORK_TEST_SUDOGRAPH_NODE` chooses the worker's Node executable
separately from the host/frontend toolchain; otherwise it uses `node` on PATH.
This permits the existing Node20 frontend build and Node22 worker in one gate.
It does not change any test assertion, deadline or worker configuration. The
current worker is built and packed from `packages/sudograph-worker`, installed
in an isolated directory and byte-verified against its source/build manifest.
Run `python tools/package_sudograph_worker.py --output <new-directory>` to
reproduce that route. It preserves the existing shared runtime dependency and
protocol, with production dependencies locked by `npm-shrinkwrap.json`. The
earlier archived Moss `server-v0.1.104-rc.2` fixture receipts remain historical
compatibility evidence. Neither package acceptance qualifies the full backend.
An actual integrated candidate still needs its active source/version/artifact
hashes and the joint domain, business and production acceptance receipts.

For browser integration, run `tools/accept_sudograph_worker.py` with its verified
artifact paths, a new `--output` directory and `--emit-review-fixture`. The same
acceptance run exports `browser-fixture/` containing unchanged compiler HTML,
the canonical model and overlay, exact revision/digest, stable node identities,
and a bounded synthetic record response. The manifest records each file's
hash and the actual record endpoint. No database, worker configuration or
credential is exported. CI retains this directory alongside the parent
`receipt.json`; a fixture left by a failed run is not a passing acceptance.
Serving the fixture requires the browser host's real read/comment integration.
The generated HTML's pending anchor stub does not constitute a working comment
composer or authenticated comment delivery.
