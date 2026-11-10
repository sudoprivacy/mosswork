# Native execution and DLP integration

The production frontend and the Moss backend both move to Mosswork under the
user-confirmed direction of 2026-11-10. Preserve the Mosswork experience and reuse
SudoCode/Nexus management, identity, sessions and A2A. This document specifies the
remaining integration; it does not establish compatibility or deployment.

## Current implementation boundary

| Capability | Working implementation | Mosswork integration |
| --- | --- | --- |
| Model execution | SudoCode in nexusd-cohost ManagedAgentService | Pending; the current harness execution path is a separate engine |
| Identity and durable session binding | Existing production owner/organization/agent/native-session contracts | Pending; do not infer a production UUID from a Mosswork numeric user ID |
| Native content enforcement | Nexus `egress_gate`, on explicitly selected outbound mounts | Reuse the existing hook service |
| Detection and business policy | Shared Presidio configuration and regression/evaluation tools in sudostack | Reuse deployment configuration; no detector fork required |
| Policy persistence and reconciliation | `@sudo/egress-policy` in sudostack | Shared service extraction is in sudostack PR145; product wiring pending |
| Administration | Current production Moss API/UI, with default OFF | Move authenticated management and policy presentation into Mosswork |

Sudograph's published MCP worker integration in `architecture.md` remains a
separate domain capability. It does not establish native agent/session or DLP
integration. The browser workline owns viewer/input changes; this workline owns
the shared DLP control/admission integration.

## Contracts required before native admission

1. **Identity:** authenticate the human with Mosswork, then resolve the existing
   organization, owner and agent through the shared identity authority. Bind the
   native credential to that resolved owner and its intended audience. Reject
   missing mappings and cross-organization selections; do not create a second
   authority or borrow an administrator's model key.
2. **Session:** persist the product-to-native durable session and repository
   binding. Resume, streaming, approvals, cancellation and history must use the
   selected ManagedAgentService. A new thread must not silently fall back to a
   harness/direct-model session when the deployment requires native execution.
3. **Policy:** use the shared repository and reconciliation service for platform
   defaults and organization overrides. Keep configuration writes in the trusted
   controller. Deliver versioned policy through the existing runtime configuration
   channel into an agent-read-only directory.
4. **Admission:** resolve the selected owner's allocation, read its policy status
   over that authenticated Nexus connection, and run the shared
   `assertEgressRuntimeAdmission` check. When policy is enabled, absent/stale/error
   receipts and direct execution engines must fail before a model request. A
   matching revision from a different allocation is insufficient.
5. **Presentation:** expose desired revision and actual per-allocation application
   state in the existing Mosswork settings experience. Saving a policy does not
   mean it was enforced. Local GPU mounts may be excluded from the outbound set.

The current shared policy code is JavaScript. The Python backend must consume
that checked implementation through a bounded controller adapter, or use a
shared binding with wire conformance checks. Do not independently reimplement
policy validation/inheritance in a Mosswork router. This adapter must carry no
agent-accessible policy mutation tool or duplicate public management authority.

## Acceptance and deployment

Keep current Cloud/Edge services running until the native adapter is accepted.
Retain backups and original identity/session/workspace bindings; production data
migration needs an explicit reviewed plan. Stage the complete candidate, keep
rollback available, and coordinate the Tencent deployment owner before cutover.

The acceptance candidate must prove:

- Existing owner and original-session recovery, real tools, multiple turns,
  cancel, reconnect and history retention using the native product path.
- Default OFF, administrator enable/disable, organization inheritance, stale
  policy conflicts, and saved versus applied revisions on the selected allocation.
- Real cloud redaction, public-content allow, explicit deny, malformed detector
  results and detector outage. Denied content must not reach the upstream; local
  history retains the original input and audit output omits detected values.
- Refusal of a newly selected legacy/direct engine under enabled policy, plus
  cross-owner and cross-organization refusal before execution.
- Controller/runtime restart and credential restoration. Qualify physical power
  cycling separately from service or whole-machine restart.

Standalone scode CLI without Nexus remains optional and outside this managed DLP
path. Nexus-connected CLI, Cloud, Private and Edge reuse the shared contracts.
Private has code acceptance only until a real customer deployment exists.

## Retained production evidence

Tencent Cloud's Moss 0.1.103 / cohost 0.2.40 / Nexus 0.8.26 DLP evidence and the
DGX persistent gVisor/recovery evidence predate Mosswork integration. The DGX
whole-machine restart restored services, Vault credentials, model catalogs and
original-session bindings. Its strict post-restart model-math journey failed on
the first response and remains unaccepted. These receipts must not be relabelled
as Mosswork acceptance.

Device evidence: [SudoEdge persistent sandbox receipt](https://github.com/sudoprivacy/sudoedge/blob/be4c10e5bfcae59a16de4a0569e5c361b59aeb5f/profiles/acceptance/2026-11-09-persistent-sandbox.json).
Architecture source: [Sudo Cloud plan](https://github.com/sudoprivacy/sudostack/blob/8226939dcbb3378f2e462c70425b54f0fcfececa/docs/sudo-cloud-plan.md).
